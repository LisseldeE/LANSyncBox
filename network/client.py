"""
同步客户端
连接端运行，连接服务器并发送/接收文件
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import socket
import threading
import os
import time
import struct
from typing import Optional
from PySide6.QtCore import QObject, Signal

from config import Config, UserConfig
from network.protocol import Protocol, MessageType, MessageReceiver
from network.mesh import MeshManager
from sync.distributor import Distributor
from sync.file_state_store import FileStateStore
from sync.vector import Endpoint
from utils.transfer_queue import TransferQueue
from utils.send_guard import SendLock


class SyncClient(QObject):
    """同步客户端"""
    
    # 信号
    connected = Signal()              # 连接成功
    disconnected = Signal()           # 断开连接
    error_occurred = Signal(str)      # 错误
    auth_failed = Signal(str)         # 验证失败
    file_received = Signal(str)       # 收到文件
    file_receive_start = Signal(str, int)  # 开始接收文件 (filename, file_size)
    file_receive_progress = Signal(str, int, int)  # 文件接收进度 (filename, current, total)
    file_receive_cancelled = Signal(str)  # 文件接收被取消
    file_deleted = Signal(str)        # 文件已删除
    file_renamed = Signal(str, str)   # 文件已重命名 (old_name, new_name)
    dir_created = Signal(str)         # 目录已创建
    file_sent = Signal(str)           # 发送文件完成
    file_send_progress = Signal(str, int, int)     # 文件发送进度 (filename, current, total)
    log_message = Signal(str)         # 日志消息
    file_list_received = Signal(list) # 收到文件列表
    sync_requested = Signal()          # 收到主机端手动同步请求，需重新上报文件列表
    sync_result = Signal(bool)         # 收到同步结果（True=存在差异需补齐，False=列表一致无需同步）
    clipboard_received = Signal(str, bytes)  # 收到剪切板内容 (mime_type, data)，交 UI 写系统剪贴板
    files_notify_received = Signal(bytes)    # 收到文件会话通知（content=JSON 字节），交 UI 展示远程文件胶囊
    mode_changed = Signal(str)         # 模式变更 (new_mode)，"sync"/"collect"
    perm_changed = Signal(str)         # 权限变更 (new_perm)，"rw"（读写）/ "ro"（只读），交 UI 应用禁操作/单向下拉
    # 去中心化阶段 2：自同步链路信号
    state_sync_done = Signal(bool)     # 一轮端到端对比结束（True=有差异正在补齐，False=一致）
    file_state_added = Signal(str)     # 自同步拉取完成落盘（相对路径），UI 刷新文件列表
    sync_pull_progress = Signal(str, 'qlonglong', 'qlonglong')  # 自同步拉取进度（相对路径, 已收字节, 总字节）
    sync_pull_done = Signal(str, bool)  # 自同步拉取结束（相对路径, 成功?）
    
    # 数据块大小（64KB）
    CHUNK_SIZE = 64 * 1024
    
    def __init__(self, room_code: str, password: str = "", parent=None):
        super().__init__(parent)
        self.room_code = room_code
        self.password = password
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.authenticated = False
        self.receiver = MessageReceiver()
        self.host_ip: Optional[str] = None        # 主机地址（连接对端，END_INFO 记录用）
        self.mesh: Optional[MeshManager] = None   # 网状连接管理器（认证成功后启动）
        self.host_endpoint: Optional[Endpoint] = None  # 主机端身份（END_INFO 交换结果）
        self.distributor: Optional[Distributor] = None  # 分发链路引擎（认证成功后启动）
        self.file_state_store: Optional[FileStateStore] = None  # 自同步链路（阶段 2，认证后启动）
        self._file_provider = None   # 本端 FileProvider（UI 注入；会话服务能力）
        self.sync_folder = Config.get_room_folder(room_code)
        self.receiving_files = {}  # 大文件接收状态：{filename: {handle, file_size, mtime, received_size, temp_path}}
        self._receiving_lock = threading.Lock()  # 保护 receiving_files 的线程锁
        self._send_guard = SendLock()            # 发送串行化：同一 socket 并发写不交错

        # 心跳状态（在线/离线判定）：PONG 在接收线程更新，_ping_loop 读取判超时
        self._last_pong: Optional[float] = None
        self._ping_lock = threading.Lock()

        # 当前模式：认证后由主机端下发（"sync"同步 / "collect"收集）
        self.mode = "sync"
        self.mode_received = False  # 是否已收到主机模式下发（复用连接时用于补偿 UI 状态）

        # 当前权限：认证后由主机端下发（"rw"读写 / "ro"只读）。
        # 回退默认读写（向后兼容），但若本房间上次被主机指定为只读，则跨重启/主机
        # 离线期间加载持久化档位保持只读——不再因无主机下发而放松成读写。
        self.perm = UserConfig.get_room_perm(self.room_code)
        self.perm_received = False  # 是否已收到主机权限下发（复用连接时用于补偿 UI 状态）

        # 心跳探测线程控制：周期性 PING 主机并核对 PONG 回包，判定本端在线/离线
        self._ping_stop = threading.Event()
        self._ping_thread = None
        self._ping_gen = 0  # 心跳线程代际号：重连启动新线程后旧线程据让位退出
        self.PING_INTERVAL = 1.0  # 每 1 秒发送一次 PING 给主机（连接端单机独立探测，主机端汇总不增加负担）
        self.OFFLINE_TIMEOUT = 5.0  # 心跳超时阈值：连续超过该时长未收到主机 PONG → 判定离线并断开

        # 创建传输队列，控制并发传输数量
        self.transfer_queue = TransferQueue(max_concurrent=5)

        # ---- 全网状管理平面（故障切换 / 主机回归挂回） ----
        # 可用端点列表：首位=原始主机，其余=已登记对端（含各自管理端口）；
        # 管理连接失效时按序试连（一个不可用立即试下一个），整轮失败 1s→30s 退避。
        self._endpoint_list = []           # [{end_id, name, ip, mesh_port, mgmt_port}]
        self._ep_lock = threading.Lock()   # 保护端点列表
        self._attached_ep = None           # 当前管理连接挂接的端点（试连成功时记录）
        self._reported_offline = False     # 是否已发过 disconnected（整轮失败才发一次）
        self._mgmt_server = None           # 本端管理监听（reuse 模式 SyncServer）
        self._auth_wait = threading.Condition()  # 认证结果等待（_try_attach 试连用）
        self._auth_result = None           # 最近一次认证结果（True/False/None）
        self._reconnect_stop = threading.Event()  # 停止自动重连
        self._reconnect_wake = threading.Event()  # 唤醒重连循环
        self._reconnect_lock = threading.Lock()   # 保护重连线程启动
        self._reconnect_thread = None      # 重连循环线程
        self._reconnect_active = False     # 自动重连是否接管管理连接（试连被拒不弹窗、
                                           # 断连不直接发 disconnected，交由循环收敛）
        self._backoff = 1.0                # 重连退避（整轮失败递增，1s→30s）
        self._perm_auth_denied = False     # 是否已就"永久拒绝"弹过一次 auth_failed（去重）
        # 失败日志节流：同端点（ip:port）在窗口内去重，防断连风暴刷屏日志
        self._last_fail_key = None
        self._last_fail_time = 0.0
        # 端点失败计数：连续失败达阈值且列表不止一项时降级移除该端点
        # （对端离线后不再每轮超时干等；主机回归经 JOIN/END_INFO 重新登记）。
        # 首位占位主机（end_id=''）永不降级。
        self._fail_counts = {}
        # 已识别的主机 end_id：来自管理连接上收到的 END_INFO（真主机身份）。
        # 以此按"身份"而非"IP"回填首位占位，主机多网卡/IP 变化时仍稳定排在候选首位，
        # 使重连始终"先够主机"，避免主机被挤到列表末尾而错挂到对端上。
        self._host_id = ""
        # 控制面"只连主机"待机状态：主机离线时静默待机（静默=不刷端点连接失败日志，
        # 只提示一次"主机离线，静默待机"）；不再连对端 mgmt，"不互相取暖"。
        self._host_offline_logged = False   # 是否已就"主机离线，静默待机"提示过（主机回归后复位）
        self._silent_standby = False        # 是否处于主机静默待机（屏蔽 connect 失败日志）

        # 自动重连参数
        self.RECONNECT_BASE = 1.0          # 整轮端点试连失败后的初始退避（秒）
        self.RECONNECT_MAX = 30.0          # 最大退避（秒）
        self.PREFERRED_REPROBE_INTERVAL = 10.0  # 挂非主机端点时周期回探主机（秒）
        self.AUTH_WAIT_TIMEOUT = 3.0       # 单端点试连的认证等待上限（秒）
        self.FAIL_LOG_THROTTLE = 10.0      # 端点连接失败日志节流窗口（秒）
        self.FAIL_DROP_LIMIT = 3           # 端点连续失败降级阈值（次）
    
    def _safe_join(self, filename: str) -> str:
        """
        安全拼接同步文件夹路径，防止路径穿越攻击。
        如果 filename 包含 .. 或绝对路径等危险成分，抛出 ValueError。
        """
        if not filename:
            raise ValueError("文件名为空")
        file_path = os.path.normpath(os.path.join(self.sync_folder, filename))
        sync_abs = os.path.abspath(self.sync_folder)
        file_abs = os.path.abspath(file_path)
        if file_abs != sync_abs and not file_abs.startswith(sync_abs + os.sep):
            raise ValueError(f"非法路径: {filename}")
        return file_path
    
    def connect_to_server(self, host: str, port: int = None, reset_list: bool = True) -> bool:
        """连接到服务器（管理连接）

        reset_list=True（UI 首次连/重连）：重建可用端点列表，首位=原始主机；
        reset_list=False（自动重连试连）：保留列表，仅建连认证。
        """
        port = port or Config.DEFAULT_PORT
        if reset_list:
            self._reset_endpoint_list(host, port)

        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # 增大 TCP 缓冲区，避免大文件传输时 sendall 因缓冲区满而 1 秒超时
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)  # 4MB
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)  # 4MB
            except Exception:
                pass
            self.socket.settimeout(2.0 if self._reconnect_active else 5.0)
            self.socket.connect((host, port))
            self.socket.settimeout(1.0)
            self.host_ip = host

            # 竞态防护：自动重连过程中发生了手动断开（disconnect 已置 _reconnect_stop）。
            # 不在此时复活 running 状态、也不启动接收线程，回收半开 socket 交重连循环退出，
            # 避免"已断开却复活"成幽灵连接（旧连接资源泄漏 + 重连被重新武装）。
            # 手动加入路径 _reconnect_active=False，不受此闸门影响。
            if self._reconnect_active and self._reconnect_stop.is_set():
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
                return False

            self.running = True
            self._reported_offline = False

            # 发送验证请求（一致性校验只比同步逻辑版本号，UI/展示变更不要求全员升级）
            auth_msg = Protocol.create_auth_request(Config.SYNC_LOGIC_VERSION, self.room_code, self.password)
            self._send_guard.send(self.socket, auth_msg)

            # 启动接收线程
            receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            receive_thread.start()

            return True

        except Exception as e:
            # 建连失败：回收半开 socket，交调用方处理
            try:
                if self.socket:
                    self.socket.close()
            except Exception:
                pass
            self.socket = None
            if self._reconnect_active:
                # 自动重连试连失败：节流去重（同端点窗口内仅记一次，防断连风暴
                # 刷屏），交重连循环试下一端点。主机静默待机期（_silent_standby）
                # 静默：已提示过"主机离线"，不再逐轮刷"端点连接失败"。
                if self._silent_standby:
                    return False
                now = time.time()
                key = f"{host}:{port}"
                if key != self._last_fail_key \
                        or now - self._last_fail_time >= self.FAIL_LOG_THROTTLE:
                    self._last_fail_key = key
                    self._last_fail_time = now
                    self.log_message.emit(f"端点连接失败: {e}")
            else:
                self.error_occurred.emit(f"连接服务器失败: {e}")
            return False
    
    def disconnect(self):
        """断开连接（手动/退出）：停止自动重连、拆除全部链路并补发离线信号。"""
        # 停止自动重连（唤醒循环线程让其尽快退出）
        self._reconnect_stop.set()
        self._reconnect_wake.set()
        with self._reconnect_lock:
            self._reconnect_active = False
        with self._auth_wait:
            self._auth_result = None
            self._auth_wait.notify_all()

        self.running = False
        self.authenticated = False
        self._ping_stop.set()
        self._attached_ep = None
        self._reported_offline = True

        # 关闭本端管理监听（reuse 模式 SyncServer：共享宿主对象，stop 不回收宿主数据平面）
        if self._mgmt_server:
            try:
                self._mgmt_server.stop()
            except Exception:
                pass
            self._mgmt_server = None

        # 关闭网状连接（网状直连随本端退出一并拆除）
        if self.mesh:
            self.mesh.stop()

        # 停止分发链路引擎
        if self.distributor:
            self.distributor.stop()
            self.distributor = None

        # 停止自同步链路
        if self.file_state_store:
            self.file_state_store.stop()
            self.file_state_store = None

        # 清理大文件接收状态：关闭句柄、删除临时文件
        # 先在锁内收集所有需要清理的条目，然后在锁外执行 IO 操作
        with self._receiving_lock:
            items_to_cleanup = list(self.receiving_files.items())
            self.receiving_files.clear()

        for filename, rf in items_to_cleanup:
            handle = rf.get('handle')
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass
            temp_path = rf.get('temp_path')
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None

        # 手动断开：UI 落定离线态（原由接收线程退出时补发；现接收线程让位不越权）
        self.disconnected.emit()

    def _start_ping_thread(self):
        """启动心跳探测线程（每次连接建立新线程）。

        快速重连时旧线程可能仍在收尾（disconnect 已置位 _ping_stop）：先清停拍
        标志并递增代际号，旧线程在下一拍发现代际不符即让位退出——避免新连接
        永久无心跳（静默失效），也避免新旧两线程并发 PING。
        """
        self._ping_stop.clear()
        self._ping_gen += 1
        gen = self._ping_gen
        self._ping_thread = threading.Thread(target=self._ping_loop, args=(gen,), daemon=True)
        self._ping_thread.start()

    def _ping_loop(self, gen: int):
        """心跳探测线程：每 1 秒 PING 当前管理连接一次；超过 OFFLINE_TIMEOUT 未收到
        PONG 回包即判定离线（主机拔线/杀进程等半开场景），触发故障切换
        （_handle_management_lost：保留数据平面，按可用端点列表切至下一端点）。
        gen 为线程代际号：新连接启动新线程后，旧线程据此让位退出（防重复 PING）。"""
        while not self._ping_stop.is_set():
            if gen != self._ping_gen:
                break  # 已被新连接的新线程取代，让位退出
            if self._ping_stop.wait(self.PING_INTERVAL):
                break
            if not self.socket or not self.authenticated:
                continue
            with self._ping_lock:
                last = self._last_pong
            if last is not None and time.time() - last > self.OFFLINE_TIMEOUT:
                self.log_message.emit("主机心跳超时，判定离线，进入主机待机")
                self._handle_management_lost()
                break
            try:
                # 可恢复发送：大文件传输背压时 PING 不丢失，避免误判离线
                self._send_guard.send_resumable(
                    self.socket, Protocol.create_ping(time.time()))
            except Exception:
                pass

    # ---- 全网状管理平面：故障切换 / 主机回归挂回 ----

    def _handle_management_lost(self):
        """管理连接失效：保留数据平面（mesh/distributor/store），关闭管理 socket，
        进入自动重连（按序试连可用端点）。"""
        self._close_mgmt_socket()
        self._start_reconnect()

    def _start_reconnect(self):
        """启动自动重连循环（幂等：已有存活线程则仅唤醒，避免并发多个循环）。"""
        with self._reconnect_lock:
            if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
                self._reconnect_wake.set()
                return
            self._reconnect_stop.clear()
            self._reconnect_wake.clear()
            self._reconnect_active = True
            self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
            self._reconnect_thread.start()
            self._reconnect_wake.set()

    def _reconnect_loop(self):
        """自动重连循环：控制面只连主机（_host_candidate）。

        - 已保持管理连接且挂在真主机 → 仅等待不探活；
        - 挂在非主机对端（异常态，不应发生）→ 不互相取暖，立即丢开对端管理连接，
          回到主机静默待机（只够主机）；
        - 无管理连接 → 只试主机候选；连不上则静默待机（固定 10s 回探，不连对端，
          不叠加 30s 退避，也不刷"端点连接失败"）。"""
        while self.running and not self._reconnect_stop.is_set():
            try:
                if self.socket and self.authenticated:
                    # 已保持管理连接：确认挂在主机（index0 恒为主机占位），挂在非主机
                    # 对端则丢开回待机（不互相取暖）
                    idx = self._current_ep_index()
                    if idx == 0 or idx == -1:
                        # 已挂主机（含 end_id='' 的占位主机） / 无法识别挂接端点：
                        # 仅等待不探活
                        self._silent_standby = False
                        self._reconnect_wake.wait(self.PREFERRED_REPROBE_INTERVAL)
                        self._reconnect_wake.clear()
                        continue
                    # 挂在非主机对端（end_id 非空且 != 主机）：不互相取暖，立即丢开
                    self.log_message.emit(
                        "已在非主机端点，释放对端连接，回主机静默待机")
                    self._close_mgmt_socket()
                    self._report_offline_once()
                    self._host_offline_logged = True
                    self._enter_host_standby()
                    continue
                # 无管理连接：只连主机
                self._silent_standby = True
                host = self._host_candidate()
                if not host or not int(host.get('mgmt_port', 0) or 0):
                    self._enter_host_standby()
                    continue
                if self._try_attach(host):
                    # 主机回归：复位待机标志，管理连接已挂主机
                    self._host_offline_logged = False
                    self._silent_standby = False
                    self._backoff = self.RECONNECT_BASE
                    continue
                # 主机连不上：静默待机，周期重探（不连对端）
                self._enter_host_standby()
            except Exception:
                pass

    def _host_candidate(self) -> Optional[dict]:
        """主机候选：控制面唯一允许连接的目标（真主机）。

        - 已识别主机身份 _host_id → 列表内 end_id==_host_id 的端点；
        - 未知身份 → 首位占位（end_id=='' 的原始主机）。
        对端 end_id 非空且 !=_host_id 永不作管理候选 —— 控制面只连主机。"""
        if self._host_id:
            with self._ep_lock:
                for ep in self._endpoint_list:
                    if ep.get('end_id') == self._host_id:
                        return dict(ep)
            return None  # 主机不在列表：静默待机
        return self._best_endpoint(0)

    def _enter_host_standby(self):
        """进入/维持主机静默待机：固定间隔重探主机，只提示一次"主机离线"。

        待机期屏蔽 connect 失败日志（_silent_standby=True），避免刷屏。"""
        self._report_offline_once()
        if not self._host_offline_logged:
            self._host_offline_logged = True
            self.log_message.emit("主机离线，静默待机，仅等待主机回归")
        self._silent_standby = True
        self._reconnect_wake.wait(self.PREFERRED_REPROBE_INTERVAL)
        self._reconnect_wake.clear()

    def _handle_host_info_redirect(self, info: dict):
        """H1：接入点命中的是"连接端复用管理监听"，它告知真主机地址。

        把真主机钉为"只连主机"的唯一候选（host_id/ip/mgmt_port 落位），关闭当前
        中间端管理连接，进入重连——由 _reconnect_loop 的 _host_candidate 重新只够
        真主机，完成从中间端到真主机的管理连接换接。
        """
        host_ip = info.get('ip', '') or ''
        host_port = int(info.get('port', 0) or 0)
        host_id = info.get('host_id', '') or ''
        if not host_ip or host_port <= 0:
            return
        with self._ep_lock:
            self._endpoint_list = [{
                'end_id': host_id,
                'name': '主机',
                'ip': host_ip,
                'mesh_port': 0,
                'mgmt_port': host_port,
            }]
            self._host_id = host_id
            self._attached_ep = None
        self.host_ip = host_ip
        self._reported_offline = False
        self._host_offline_logged = False
        # 换接真主机：丢开当前中间端管理连接，交给"只连主机"重连收敛
        self._close_mgmt_socket()
        self._start_reconnect()

    def _try_attach(self, ep: dict) -> bool:
        """试连单个端点（管理连接）：TCP 建连 + 等待认证结果。
        失败返回 False 并累计失败计数（达阈值降级移除）；成功记录挂接端点并重置退避。"""
        ip = ep.get('ip', '')
        mgmt_port = int(ep.get('mgmt_port', 0) or 0)
        if not ip or mgmt_port <= 0:
            return False
        with self._auth_wait:
            self._auth_result = None
        if not self.connect_to_server(ip, mgmt_port, reset_list=False):
            self._note_ep_fail(ep)
            return False
        with self._auth_wait:
            self._auth_wait.wait_for(
                lambda: self._auth_result is not None
                        or self._reconnect_stop.is_set() or not self.running,
                timeout=self.AUTH_WAIT_TIMEOUT)
            result = self._auth_result
            self._auth_result = None
        if not result:
            self._close_mgmt_socket()
            self._note_ep_fail(ep)
            return False
        self._note_ep_ok(ep)
        self._attached_ep = dict(ep)
        self._reported_offline = False
        self._backoff = self.RECONNECT_BASE
        self.log_message.emit(
            f"端点切换: 已连接 {ep.get('name', '')} ({ep.get('end_id', '')}) {ip}")
        return True

    def _note_ep_fail(self, ep: dict):
        """端点试连失败：累计连续失败计数，达阈值且列表不止一项 → 降级移除该端点。
        首位占位主机（end_id=''）只计数不降级——主机离线后不应被永久丢，回归后
        经 END_INFO/MESH_PEER_JOIN 重新登记（_upsert_endpoint 清除失败计数）。"""
        end_id = ep.get('end_id', '')
        with self._ep_lock:
            self._fail_counts[end_id] = self._fail_counts.get(end_id, 0) + 1
            if (end_id and end_id != self._host_id
                    and self._fail_counts[end_id] >= self.FAIL_DROP_LIMIT
                    and len(self._endpoint_list) > 1):
                self._endpoint_list = [
                    e for e in self._endpoint_list if e.get('end_id') != end_id
                ]
                self._fail_counts.pop(end_id, None)

    def _note_ep_ok(self, ep: dict):
        """端点试连成功：清除该端失败计数（重新计连续失败）。"""
        with self._ep_lock:
            self._fail_counts.pop(ep.get('end_id', ''), None)

    @staticmethod
    def _probe_endpoint(ep: dict) -> bool:
        """裸 TCP 探活：仅建连即关，不认证（未认证连接不计入主机在线数，
        避免周期回探导致主机在线数闪烁）。"""
        ip = ep.get('ip', '')
        mgmt_port = int(ep.get('mgmt_port', 0) or 0)
        if not ip or mgmt_port <= 0:
            return False
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((ip, mgmt_port))
            return True
        except OSError:
            return False
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    def _report_offline_once(self):
        """整轮端点试连失败后补发一次离线信号（避免每轮退避重复弹离线）。"""
        if not self._reported_offline:
            self._reported_offline = True
            self.disconnected.emit()

    def _close_mgmt_socket(self):
        """关闭当前管理连接（保留数据平面 mesh/distributor/store）。"""
        self.authenticated = False
        self._ping_stop.set()
        self._attached_ep = None
        sock = self.socket
        self.socket = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def _reset_endpoint_list(self, host: str, port: int):
        """重置可用端点列表：首位=原始主机（初始仅有主机，其余对端随
        MESH_PEER_LIST/JOIN/END_INFO 逐步登记）。"""
        with self._ep_lock:
            self._endpoint_list = [{
                'end_id': '',
                'name': '主机',
                'ip': host,
                'mesh_port': 0,
                'mgmt_port': int(port or 0),
            }]
        self._attached_ep = None
        self._host_id = ""  # 重置后主机身份未知，待首条 END_INFO 重新识别
        self._host_offline_logged = False  # 手动重连复位：下次离线仍提示一次
        self._silent_standby = False

    def _upsert_endpoint(self, info: dict):
        """登记/更新可用端点（来自 END_INFO/MESH_PEER_LIST/MESH_PEER_JOIN）。

        按 end_id 匹配合并（非空字段覆盖）；end_id 为空或本端跳过。新端点登记：
        - 若其 end_id 是本端已识别的主机 _host_id → 按"身份"回填/更新首位占位
          （无论 IP 是否变化，主机稳定排在首位，重连先够主机）。
        - 若字段 IP 与首位占位（原主机）一致，且在获知主机身份前 → 回填占位并记录身份。
        - 否则视为普通对端追加到列表尾部（保持首位=主机）。
        """
        if not isinstance(info, dict):
            return
        end_id = info.get('end_id', '')
        if not end_id or end_id == UserConfig.get_end_id():
            return
        entry = {
            'end_id': end_id,
            'name': info.get('name', ''),
            'ip': info.get('ip', ''),
            'mesh_port': int(info.get('mesh_port', 0) or 0),
            'mgmt_port': int(info.get('mgmt_port', 0) or 0),
        }
        with self._ep_lock:
            self._fail_counts.pop(end_id, None)  # 重新登记 → 重置失败计数

            # 1) 已识别的真主机：按 end_id 身份稳定放在首位占位，即使 IP 已变
            if end_id == self._host_id:
                if self._endpoint_list and not self._endpoint_list[0].get('end_id'):
                    # 占位转正：原位合并且补上身份
                    self._endpoint_list[0] = self._merge_endpoint(self._endpoint_list[0], entry)
                for i, ep in enumerate(self._endpoint_list):
                    if ep.get('end_id') == end_id:
                        self._endpoint_list[i] = self._merge_endpoint(ep, entry)
                        return
                self._endpoint_list.insert(0, entry)  # 理论不会到：兜底前置为 0 号
                return

            # 2) 常规：按 end_id 合并已登记端点
            for i, ep in enumerate(self._endpoint_list):
                if ep.get('end_id') == end_id:
                    self._endpoint_list[i] = self._merge_endpoint(ep, entry)
                    return

            # 3) 新端点：与首位占位同 ip（尚未获知主机身份时的首连主机）→ 回填占位并记录身份；
            #    否则追加到列表尾部（保持首位=主机）
            for i, ep in enumerate(self._endpoint_list):
                if not ep.get('end_id') and entry.get('ip') == ep.get('ip'):
                    self._endpoint_list[i] = self._merge_endpoint(ep, entry)
                    self._host_id = end_id
                    return
            self._endpoint_list.append(entry)

    @staticmethod
    def _merge_endpoint(old: dict, new: dict) -> dict:
        """合并端点信息：新字段非空则覆盖，缺省保留旧值。"""
        merged = dict(old)
        for k, v in new.items():
            if v:
                merged[k] = v
        return merged

    def _drop_endpoint(self, end_id: str):
        """移除可用端点（MESH_PEER_LEAVE：对端离线）。"""
        if not end_id:
            return
        with self._ep_lock:
            self._fail_counts.pop(end_id, None)
            self._endpoint_list = [
                ep for ep in self._endpoint_list if ep.get('end_id') != end_id
            ]

    def _endpoint_snapshot(self) -> list:
        """可用端点列表快照（线程安全拷贝，供重连循环遍历）。"""
        with self._ep_lock:
            return [dict(e) for e in self._endpoint_list]

    def _best_endpoint(self, index: int = 0) -> Optional[dict]:
        """取列表中第 index 个端点（默认首位=原始主机）；越界返回 None。"""
        eps = self._endpoint_snapshot()
        if 0 <= index < len(eps):
            return eps[index]
        return None

    def _current_ep_index(self) -> int:
        """当前挂接端点在列表中的下标；-1=无法识别（仅等待不探活，防误关正常连接）。"""
        attached = self._attached_ep
        if not attached:
            return -1
        with self._ep_lock:
            if attached.get('end_id'):
                for i, ep in enumerate(self._endpoint_list):
                    if ep.get('end_id') == attached.get('end_id'):
                        return i
            else:
                # 挂接端点无 end_id（占位主机）：按 ip 匹配
                for i, ep in enumerate(self._endpoint_list):
                    if ep.get('ip') == attached.get('ip'):
                        return i
        return -1

    def send_clipboard(self, mime_type: str, data: bytes):
        """连接端本端复制时，上报剪切板内容给主机（由主机分发给其余端）

        Args:
            mime_type: "text"
            data: 内容字节
        """
        if mime_type != "text":
            return
        if not self.socket or not self.authenticated:
            return
        try:
            self._send_guard.send(self.socket, Protocol.create_clipboard_message(mime_type, data))
        except Exception:
            pass

    def send_files_notify(self, notify_dict: dict):
        """连接端本端复制时，上报文件会话通知给主机（由主机转达其余端）。

        文件字节不进主机，由接收端后续直连本端 FileProvider 拉取。
        """
        if not self.socket or not self.authenticated:
            return
        try:
            self._send_guard.send(self.socket, Protocol.create_files_notify(notify_dict))
        except Exception:
            pass

    def emit_files_notify(self, notify_dict: dict) -> bool:
        """投递通知沿网状分发链路广播（阶段 5）：mesh 就绪返回 True，否则 False。

        返回 False 时调用方回退旧路径（send_files_notify，经主机转发）。
        """
        if self.distributor:
            return self.distributor.emit_files_notify(notify_dict)
        return False

    def _receive_loop(self):
        """接收数据循环（每次连接独立 receiver，防新旧连接线程交错污染缓冲）"""
        sock = self.socket
        receiver = MessageReceiver()
        while self.running and self.socket is sock:
            try:
                data = sock.recv(65536)
                if not data:
                    break

                receiver.feed(data)

                # 处理所有完整消息
                while receiver.has_complete_message():
                    message = receiver.get_message()
                    if message:
                        self._process_message(message)

            except socket.timeout:
                continue
            except Exception as e:
                if self.running and self.socket is sock:
                    self.log_message.emit(f"接收错误: {e}")
                break

        # 退出路径
        if not self.running or self.socket is not sock:
            # 手动断开 / 已被新连接替换：旧线程让位，不越权清 authenticated/发信号
            return
        # 当前管理连接被对端关闭/异常：保留数据平面（mesh），进入自动重连
        if self._reconnect_active:
            self._handle_management_lost()
            return
        self.authenticated = False
        self.disconnected.emit()
    
    def _process_message(self, message: tuple):
        """处理服务器消息"""
        msg_type, filename, file_size, mtime, hide_flag, content = message
        
        if msg_type == MessageType.AUTH_RESP:
            self._handle_auth_response(content)

        elif msg_type == MessageType.FILE_BEGIN:
            # 大文件传输开始 - 使用临时文件
            try:
                file_path = self._safe_join(filename)
            except ValueError as e:
                self.log_message.emit(f"拒绝非法路径: {e}")
                return
            temp_file_path = file_path + '.tmp'  # 临时文件

            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # 同名重发防御（阶段 4 已移除 mtime 互取消）：上一同名会话尚未结束
            # （句柄未关）时先回收旧条目，避免旧会话 DATA/END 与新会话错位写
            # 同一临时文件造成字节交错、旧句柄泄漏；'wb' 打开天然截断旧 temp。
            with self._receiving_lock:
                old_rf = self.receiving_files.pop(filename, None)
            if old_rf:
                old_handle = old_rf.get('handle')
                if old_handle:
                    try:
                        old_handle.close()
                    except Exception:
                        pass

            # 创建临时文件句柄，准备流式写入
            try:
                file_handle = open(temp_file_path, 'wb')
                # 锁内只做字典操作
                with self._receiving_lock:
                    self.receiving_files[filename] = {
                        'file_size': file_size,
                        'mtime': mtime,
                        'received_size': 0,
                        'temp_path': temp_file_path,
                        'handle': file_handle
                    }

                # 发射开始接收信号（传递文件大小）
                self.file_receive_start.emit(filename, file_size)
            except Exception as e:
                self.log_message.emit(f"创建文件失败: {e}")

        elif msg_type == MessageType.FILE_DATA:
            # 大文件数据块 - 流式写入
            # 锁内获取引用，锁外执行 IO
            with self._receiving_lock:
                rf = self.receiving_files.get(filename)
                if rf and rf.get('handle'):
                    handle = rf['handle']
                else:
                    handle = None

            if handle:
                chunk_index, chunk_data = content
                try:
                    # 用 chunk_index 定位写入，避免乱序/重复/丢失导致文件损坏
                    offset = chunk_index * self.CHUNK_SIZE
                    handle.seek(offset)
                    handle.write(chunk_data)
                    with self._receiving_lock:
                        rf['received_size'] += len(chunk_data)
                        received_kb = rf['received_size'] // 1024
                        total_kb = rf['file_size'] // 1024

                    # 发送进度信号（转换为KB避免溢出）
                    self.file_receive_progress.emit(
                        filename,
                        received_kb,
                        total_kb
                    )
                except Exception as e:
                    self.log_message.emit(f"写入数据块失败: {e}")

        elif msg_type == MessageType.FILE_END:
            # 大文件传输结束 - 重命名临时文件为正式文件
            # 锁内获取引用并删除条目，锁外执行 IO
            with self._receiving_lock:
                rf = self.receiving_files.get(filename)
                if rf and rf.get('handle'):
                    handle = rf['handle']
                    del self.receiving_files[filename]
                else:
                    handle = None
                    rf = None

            if rf and handle:
                # 关闭文件句柄
                try:
                    handle.close()
                except Exception:
                    pass

                # 校验完整性：received_size 可能因重复写入而偏大，用实际文件大小校验
                temp_file_path = rf['temp_path']
                actual_size = os.path.getsize(temp_file_path) if os.path.exists(temp_file_path) else 0
                if actual_size != rf['file_size']:
                    self.log_message.emit(
                        f"大文件接收不完整: {filename} "
                        f"(实际 {actual_size}/期望 {rf['file_size']})，丢弃"
                    )
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    return

                # 重命名临时文件为正式文件
                try:
                    final_file_path = self._safe_join(filename)
                except ValueError as e:
                    self.log_message.emit(f"拒绝非法路径: {e}")
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    return

                try:
                    # 如果正式文件已存在，先删除
                    if os.path.exists(final_file_path):
                        os.remove(final_file_path)

                    # 重命名临时文件
                    os.rename(temp_file_path, final_file_path)

                    # 设置修改时间
                    os.utime(final_file_path, (rf['mtime'], rf['mtime']))

                    # 通知接收完成
                    self.file_received.emit(filename)

                except Exception as e:
                    self.log_message.emit(f"重命名文件失败: {e}")
                    # 删除临时文件
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
        
        elif msg_type == MessageType.DELETE:
            self._handle_delete(filename)
        
        elif msg_type == MessageType.DIR_CREATE:
            self._handle_dir_create(filename)
        
        elif msg_type == 0x05:  # RENAME
            self._handle_rename(content)
        
        elif msg_type == MessageType.FILE_LIST_RESP:
            # 文件列表响应
            self._handle_file_list_response(content)
        
        elif msg_type == MessageType.FILE_REQUEST:
            # 文件请求（主机端请求连接端的文件）
            self._handle_file_request_from_server(filename)
        
        elif msg_type == MessageType.FILE_CANCEL:
            # 文件传输取消
            self._handle_file_cancel(filename)

        elif msg_type == MessageType.FILE_NOTIFY:
            # 文件通知（静默，通知有新文件可用）
            self._handle_file_notify(filename, file_size, mtime)
        
        elif msg_type == MessageType.SYNC_REQUEST:
            # 主机端请求手动同步：重新上报文件列表，触发差异补齐
            # 收集模式下不触发全量同步（不会同步各端列表）
            if self.mode == "collect":
                return
            # 阶段 4：同步模式 + 自同步链路就绪 → 端到端对比（替代上报主机仲裁）；
            # 无网状对端/链路未就绪（旧版本混连等）时回退旧路径由 UI 上报列表
            if (self.mode == "sync" and self.file_state_store
                    and self.file_state_store.request_all() > 0):
                return
            self.sync_requested.emit()

        elif msg_type == MessageType.SYNC_RESULT:
            # 主机端同步结果：告知是否存在差异（用于显示"列表一致/正在补齐差异项"）
            has_diff = content == b'1'
            self.sync_result.emit(has_diff)

        elif msg_type == MessageType.PING:
            # 主机心跳探测：立即回 PONG 并原样带回发送时刻（可恢复发送，背压不丢包）
            try:
                send_time = struct.unpack('!d', content)[0]
                self._send_guard.send_resumable(
                    self.socket, Protocol.create_pong(send_time))
            except Exception:
                pass

        elif msg_type == MessageType.PONG:
            # 收到主机心跳回包：记录时间戳供 _ping_loop 判定在线/离线
            with self._ping_lock:
                self._last_pong = time.time()

        elif msg_type == MessageType.CLIPBOARD_DATA:
            # 收到主机分发的剪切板内容（文本/图片）：交 UI 写系统剪贴板
            self.clipboard_received.emit(filename, content)

        elif msg_type == MessageType.CLIPBOARD_FILES_NOTIFY:
            # 收到主机转发的文件会话通知：交 UI 展示远程文件胶囊
            self.files_notify_received.emit(content)

        elif msg_type == MessageType.MODE_RESP:
            # 主机端响应模式请求：设置当前模式
            mode = content.decode('utf-8', errors='ignore').strip()
            self._handle_mode_response(mode)

        elif msg_type == MessageType.MODE_SWITCH:
            # 主机端模式切换指令：切换本端模式并回执 ACK
            mode = content.decode('utf-8', errors='ignore').strip()
            self._handle_mode_switch(mode)

        elif msg_type == MessageType.PERM_UPDATE:
            # 主机端权限更新指令：应用权限并回执 ACK（幂等，重复收到同档位无副作用）
            perm = content.decode('utf-8', errors='ignore').strip()
            self._handle_perm_update(perm)

        elif msg_type == MessageType.END_INFO:
            # 去中心化：主机端身份信息（含 mesh_port/mgmt_port），记录主机端点供
            # 引导/建连，并登记进可用端点列表（故障切换候选）
            if isinstance(content, dict):
                end_id = content.get('end_id', '')
                if end_id and end_id != UserConfig.get_end_id():
                    self.host_endpoint = Endpoint.from_dict({
                        'end_id': end_id,
                        'name': content.get('name', ''),
                        'ip': self.host_ip or '',
                        'mesh_port': int(content.get('mesh_port', 0) or 0),
                    })
                    # END_INFO 来自管理连接上的真主机：按身份稳定其首位排序，IP 变化不失位
                    self._host_id = end_id
                    self._upsert_endpoint({
                        'end_id': end_id,
                        'name': content.get('name', ''),
                        'ip': self.host_ip or '',
                        'mesh_port': int(content.get('mesh_port', 0) or 0),
                        'mgmt_port': int(content.get('mgmt_port', 0) or 0),
                    })

        elif msg_type == MessageType.MESH_PEER_LIST:
            # 去中心化：主机引导 → 批量登记对端（含管理端口）并建立网状直连
            if self.mesh and isinstance(content, dict):
                peers = content.get('peers', []) or []
                self.mesh.bootstrap_peers(peers)
                for p in peers:
                    if isinstance(p, dict):
                        self._upsert_endpoint(p)

        elif msg_type == MessageType.MESH_PEER_JOIN:
            # 去中心化：新端加入通告 → 反向与新端建直连，并登记进可用端点列表
            if self.mesh and isinstance(content, dict):
                peer = content.get('peer')
                if isinstance(peer, dict):
                    self.mesh.add_peer(Endpoint.from_dict(peer))
                    self._upsert_endpoint(peer)

        elif msg_type == MessageType.MESH_PEER_LEAVE:
            # 去中心化：端离线通告 → 拆除与该端的直连，并移除可用端点
            if self.mesh and isinstance(content, dict):
                self.mesh.remove_peer(content.get('end_id', ''))
                self._drop_endpoint(content.get('end_id', ''))

        elif msg_type == MessageType.HOST_INFO:
            # H1：接入点命中"连接端复用管理监听"，它告知真主机地址 → 换接真主机
            if isinstance(content, dict):
                self._handle_host_info_redirect(content)
    
    def _send_mode_request(self):
        """认证成功后向主机端请求当前模式"""
        if not self.socket:
            return
        try:
            self._send_guard.send(self.socket, Protocol.create_mode_message(MessageType.MODE_REQ, "sync"))
        except Exception:
            pass

    def _start_mesh_and_send_end_info(self):
        """去中心化：认证成功后确保网状链路与管理监听就绪，并向当前管理连接
        交换 END_INFO（含本端 mesh_port/mgmt_port）。

        故障切换（主机下线切至对端）时复用同一 mesh/distributor/store（数据平面
        保留不重建），仅重新交换 END_INFO；主机随后回 END_INFO 记录主机端点，
        新端加入的引导（MESH_PEER_LIST）与其余端的 JOIN 通告均在此认证链路上接收。
        """
        self._ensure_mesh_chain()
        self._ensure_mgmt_server()
        if not self.socket or not self.mesh or not self.mesh.mesh_port:
            return
        try:
            self._send_guard.send(self.socket, Protocol.create_end_info(
                UserConfig.get_end_id(), socket.gethostname(), self.mesh.mesh_port,
                mgmt_port=self._mgmt_server.port if self._mgmt_server else 0))
        except Exception:
            pass

    def _ensure_mesh_chain(self):
        """确保数据平面就绪：mesh/distributor/file_state_store（已存在则保留，
        仅 mesh 停止时重新 start）。故障切换重复调用不重建、不重连信号。"""
        if self.mesh is None:
            # 注意：此处运行在接收线程，而 SyncClient 的线程亲和在 UI 线程；
            # 不能把 mesh/distributor/store 作为 self 的子对象创建（"Cannot create
            # children for a parent that is in a different thread"）。故 parent=None，
            # 由本端自持引用装卸载（disconnect 显式 stop），避免跨线程挂父导致告警。
            self.mesh = MeshManager()
            self.mesh.log_message.connect(self.log_message)
            self.mesh.start()
            # 分发链路引擎：本地操作 → 网状直连传播；收到信号去重/应用/转发
            self.distributor = Distributor(
                UserConfig.get_end_id(), self.sync_folder,
                mesh=self.mesh)
            self.distributor.log_message.connect(self.log_message)
            self.distributor.signal_applied.connect(self._on_distributor_applied)
            self.mesh.set_message_handler(self._on_mesh_message)
            # 自同步链路（阶段 2）：端到端状态对比与拉取；直连建立自动补齐
            self.file_state_store = FileStateStore(
                UserConfig.get_end_id(), self.sync_folder,
                mesh=self.mesh, distributor=self.distributor,
                provider=self._file_provider)
            # 跨通道互斥（阶段 6）：同一文件被主机直推通道(A)经 FILE_BEGIN..FILE_END
            # 正在接收时，自同步拉取(B)应让位，避免同文件两通道重复投递/字节翻倍。
            self.file_state_store.set_host_push_guard(
                lambda name: self._is_host_pushing_file(name))
            self.file_state_store.log_message.connect(self.log_message)
            self.file_state_store.sync_done.connect(self.state_sync_done)
            self.file_state_store.file_added.connect(self.file_state_added)
            self.file_state_store.pull_progress.connect(self.sync_pull_progress)
            self.file_state_store.pull_done.connect(self.sync_pull_done)
            # 阶段 3：冲突覆盖（远端胜出）→ 自同步链路拉取胜方字节
            self.distributor.set_conflict_pull_handler(
                self.file_state_store.request_conflict_pull)
            # 阶段 5：网状投递通知 → 复用既有 files_notify_received 信号回投 UI
            self.distributor.files_notify_received.connect(self.files_notify_received)
            self.mesh.peer_connected.connect(self._on_mesh_peer_connected)
        elif not self.mesh.running:
            self.mesh.start()

    def _is_host_pushing_file(self, name: str) -> bool:
        """探测某文件是否正被主机直推通道(A)接收（FILE_BEGIN 已到、FILE_END/CANCEL 未到）。

        供自同步拉取(B)跨通道让位判定：A 在途窗口内 B 不重复拉取同一文件。receiving_files
        仅在 _receive_loop 线程内被读写，经 _receiving_lock 同步，避免与 B 的 worker 线程
        并发访问错位；条目名即相对路径（与自同步 name 口径一致，直接匹配）。
        """
        if not name:
            return False
        try:
            with self._receiving_lock:
                return name in self.receiving_files
        except Exception:
            return False

    def _ensure_mgmt_server(self):
        """确保本端管理监听就绪（reuse 模式 SyncServer：共享本端 mesh/distributor/
        store，供其余端作为管理连接端点接入；模式与当前保持一致）。"""
        if self._mgmt_server is not None:
            # 复用已启动的管理监听：仅同步当前模式（认证/切换后保持一致）
            self._mgmt_server.mode = self.mode
            return
        try:
            from network.server import SyncServer
            server = SyncServer(self.room_code, self.password)
            # reuse 模式：注入宿主对象，start(reuse=True) 不重建数据平面
            server.mesh = self.mesh
            server.distributor = self.distributor
            server.file_state_store = self.file_state_store
            server._file_provider = self._file_provider
            server.mode = self.mode
            # H1：向接入本端复用管理监听的"新端"提供真主机信息，令其换接真主机
            server._host_provider = self._host_candidate
            if not server.start(port=Config.DEFAULT_PORT, reuse=True):
                self.log_message.emit("本端管理监听启动失败")
                return
            self._mgmt_server = server
            self.log_message.emit(f"本端管理监听端口: {server.port}（全网状管理平面）")
        except Exception as e:
            self.log_message.emit(f"本端管理监听启动失败: {e}")

    def mgmt_port(self) -> int:
        """返回本端实际监听的管理端口（用于向他人广播自己的可接入端点）。

        reuse 模式下若 DEFAULT_PORT 被占，服务器会顺延绑定到后续端口。去中心化房间
        发现要求广播真实端口，否则其它新端按 9527 拨不到本端 → 本端虽存活却无法被
        作为房间入口发现（又成了单点盲区）。未启动时回落默认端口。
        """
        server = getattr(self, '_mgmt_server', None)
        if server is not None:
            try:
                return int(server.port)
            except (TypeError, ValueError):
                pass
        return Config.DEFAULT_PORT

    # ---- 分发链路（去中心化阶段 1）：本地操作沿网状直连传播 ----

    def emit_signal(self, op: str, file: str, old: str = None) -> Optional[dict]:
        """本地文件操作 → 沿网状分发链路发出 DISTRIBUTE_SIGNAL。

        返回生成的信号 dict；分发链路未就绪（无 distributor）时返回 None，
        调用方应回退旧路径（收集模式/旧版本混连场景）。
        """
        if self.distributor:
            return self.distributor.emit(op, file, old=old)
        return None

    def _on_mesh_message(self, end_id: str, message: tuple):
        """网状直连消息入口（MeshManager 回调线程）：分发信号交给 Distributor，
        文件状态请求/响应交给自同步链路（阶段 2）。"""
        try:
            msg_type, _filename, _file_size, _mtime, _hide, content = message
        except Exception:
            return
        if msg_type == MessageType.DISTRIBUTE_SIGNAL and isinstance(content, dict):
            if self.distributor:
                self.distributor.on_signal(content)
        elif msg_type == MessageType.FILE_STATE_REQ and isinstance(content, dict):
            if self.file_state_store:
                self.file_state_store.handle_state_req(end_id)
        elif msg_type == MessageType.FILE_STATE_RESP and isinstance(content, dict):
            if self.file_state_store:
                self.file_state_store.handle_state_resp(end_id, content)
        elif msg_type == MessageType.CLIPBOARD_NOTIFY_SIGNAL:
            # 阶段 5：投递通知沿网状直连到达（不经主机转发）→ 交 UI 展示远程文件胶囊
            if self.distributor:
                self.distributor.on_files_notify(content)

    def _on_mesh_peer_connected(self, end_id: str, name: str):
        """网状直连建立：同步模式自动发起一轮状态对比（断线重连自动补齐，阶段 2）。

        只读端同样发起（仅单向下拉：从对端拉取本端缺失文件，本端不供他端拉取
        由 handle_state_req 回空 entries 保证）。
        """
        if self.mode == "sync" and self.file_state_store:
            # 本地补扫兜底：mesh 未就绪窗口内添加/启动前已放置的文件入同步
            try:
                self.file_state_store.emit_local_missing()
            except Exception:
                pass
            self.file_state_store.request_all()

    def set_file_provider(self, provider):
        """注入本端 FileProvider（会话服务能力；UI 启动 provider 后调用）。"""
        self._file_provider = provider
        if self.file_state_store:
            self.file_state_store.set_file_provider(provider)

    def request_state_sync(self) -> int:
        """端到端手动同步：向各网状对端请求 FILE_STATE_REQ 并对比补齐（阶段 2）。

        Returns:
            成功下发请求的对端数；链路未就绪返回 0（调用方回退旧路径）。
        """
        if not self.file_state_store:
            return 0
        return self.file_state_store.request_all()

    def _on_distributor_applied(self, signal: dict):
        """分发信号已应用：远端信号回投既有 UI 信号（文件列表刷新/记录）。

        仅回投远端信号（src_id != 本端）；本端 emit 的本地信号由 UI 本地操作
        入口自行处理，避免重复记录。
        """
        if not isinstance(signal, dict):
            return
        if signal.get('src_id') == UserConfig.get_end_id():
            return
        op = signal.get('op', '')
        file = signal.get('file', '')
        if op == Distributor.OP_DELETE:
            self.file_deleted.emit(file)
        elif op in (Distributor.OP_RENAME, Distributor.OP_MOVE):
            self.file_renamed.emit(signal.get('old', file), file)
        elif op == Distributor.OP_DIR_CREATE:
            self.dir_created.emit(file)
        # OP_ADD：阶段 1 字节仍走既有通道（FILE_NOTIFY → 拉取），远端 add 信号
        # 仅更新状态表，UI 刷新由字节到达时触发，不回投避免重复。

    def _handle_mode_response(self, mode: str):
        """处理主机端返回的模式（认证后首次下发）

        初次加入时：
        - sync 模式 → 触发一次全量差异同步（原连接成功即同步的行为）
        - collect 模式 → 不触发全量同步（连接端加入不会触发同步逻辑）
        """
        if mode not in ("sync", "collect"):
            mode = "sync"
        self.mode = mode
        self.mode_received = True  # 标记已收到模式下发（复用连接时供 UI 补偿状态）
        if self._mgmt_server is not None:
            self._mgmt_server.mode = self.mode  # 本端管理监听同步模式
        self.mode_changed.emit(mode)
        if mode == "sync":
            # 阶段 4：同步模式下初次加入走端到端对比（自同步链路就绪且有网状
            # 对端时）；网状对端未就绪时回退旧路径，由 _on_mesh_peer_connected
            # 在直连建立后自动发起一轮对比补齐。
            if (self.file_state_store
                    and self.file_state_store.request_all() > 0):
                return
            self.sync_requested.emit()

    def _handle_mode_switch(self, new_mode: str):
        """处理主机端模式切换指令

        - 切换前先处理传输队列：收集转同步取消全部传输；同步转收集仅清空排队
        - 回执 MODE_ACK 给主机端，由主机端统计全部回执后执行后续逻辑
        """
        if new_mode not in ("sync", "collect"):
            return
        old_mode = self.mode
        if new_mode == "sync" and old_mode == "collect":
            # 收集转同步：取消同步中的文件并清空传输列表
            self.transfer_queue.cancel_all_tasks()
        else:
            # 同步转收集：正在传输的继续传输，仅清空排队列表
            with self.transfer_queue.lock:
                self.transfer_queue.queue.clear()
        self.mode = new_mode
        self.mode_received = True  # 切换指令同样视为已收到模式下发
        if self._mgmt_server is not None:
            self._mgmt_server.mode = self.mode  # 本端管理监听同步模式

        try:
            self._send_guard.send(self.socket, Protocol.create_mode_message(MessageType.MODE_ACK, new_mode))
        except Exception:
            pass

        self.mode_changed.emit(new_mode)
        self.log_message.emit(f"模式切换: {self._mode_cn(old_mode)} → {self._mode_cn(new_mode)}")

    @staticmethod
    def _mode_cn(mode: str) -> str:
        """模式中文名（日志显示用，避免残留英文）"""
        return "同步" if mode == "sync" else "收集"

    def _handle_perm_update(self, new_perm: str):
        """处理主机端权限更新指令

        - 仅 "rw"/"ro" 合法；同档位重复下发幂等（不重复取消、不重复回执副作用）
        - 切只读时取消全部上传任务：在传任务于发送块间中止并补发 FILE_CANCEL，
          主机端 _handle_file_cancel 关句柄/删临时文件/清状态，双端进度行随之清理；
          排队任务直接清空，彻底无法上传（队列只装上传任务，不会误杀下载）
        - 回执 PERM_ACK 给主机端，由主机端统计回执后落定 UI 胶囊
        """
        if new_perm not in ("rw", "ro"):
            return
        old_perm = self.perm
        if old_perm == new_perm:
            # 同档位重复下发：仍标记已收到并回执（幂等，供主机端补发/握手自证）
            self.perm_received = True
            try:
                self._send_guard.send(self.socket, Protocol.create_perm_message(MessageType.PERM_ACK, new_perm))
            except Exception:
                pass
            return
        if new_perm == "ro":
            # 只读：取消全部上传任务（在传中止补发 FILE_CANCEL，排队清空）
            self.transfer_queue.cancel_all_tasks()
        self.perm = new_perm
        self.perm_received = True
        # 持久化本房间权限档位：主机离线期间据此保持只读不放松，跨重启亦生效
        UserConfig.set_room_perm(self.room_code, new_perm)
        # 阶段 4：只读端不发起自同步推送（不供他端拉取其文件），仅单向下拉
        if self.file_state_store:
            self.file_state_store.set_readonly(new_perm == "ro")
        try:
            self._send_guard.send(self.socket, Protocol.create_perm_message(MessageType.PERM_ACK, new_perm))
        except Exception:
            pass
        self.perm_changed.emit(new_perm)
        self.log_message.emit(f"权限切换: {self._perm_cn(old_perm)} → {self._perm_cn(new_perm)}")

    @staticmethod
    def _perm_cn(perm: str) -> str:
        """权限中文名（日志显示用，避免残留英文）"""
        return "只读" if perm == "ro" else "读写"
    
    def _handle_auth_response(self, content: bytes):
        """处理验证响应"""
        try:
            data = content.decode('utf-8').split(':', 1)
            success = data[0] == '1'
            message = data[1] if len(data) > 1 else ''
            
            if success:
                self.authenticated = True
                self._perm_auth_denied = False  # 重新认证成功，重置"永久拒绝"提示标记
                # 通知试连方（_try_attach）认证结果
                with self._auth_wait:
                    self._auth_result = True
                    self._auth_wait.notify_all()
                # 首次认证成功后自动重连接管管理连接（此后断连不直接弹窗/发离线，
                # 交由重连循环收敛：先试可用端点，整轮失败才发一次离线）
                self._reconnect_active = True
                # 去中心化：先启动网状监听并交换 END_INFO（本端身份 + 网状/管理端口），
                # 确保 mgmt 监听就绪、_mgmt_server 已赋值，再接 connected（UI on_connected
                # 里会读 mgmt_port() 广播自己的可接入端点）。在 connected.emit() 之后再
                # 启动会因跨线程 queued 信号产生竞态，UI 可能读到未就绪的端口而回落默认值。
                self._start_mesh_and_send_end_info()
                self.connected.emit()
                # 主机离线期间保持先前只读档位：若主机此刻未下发权限（离线/挂其他端点），
                # 仍按本机持久化档位落定只读态（禁自同步推送 + UI 只读），不放松成读写。
                if not self.perm_received and self.perm == "ro":
                    if self.file_state_store:
                        self.file_state_store.set_readonly(True)
                    self.perm_changed.emit("ro")
                self.log_message.emit("验证成功")
                # 认证成功：心跳计时起点（此后周期 PING 主机并核对 PONG 判定在线/离线）
                with self._ping_lock:
                    self._last_pong = time.time()
                self._start_ping_thread()
                # 认证成功后请求当前模式，由主机端决定本端模式
                self._send_mode_request()
            else:
                self.log_message.emit(f"验证失败: {message}")
                with self._auth_wait:
                    self._auth_result = False
                    self._auth_wait.notify_all()
                if self._reconnect_active:
                    # 自动重连中被明确拒绝（密码/房间号/逻辑版本不匹配）属永久性错误：
                    # 弹一次 auth_failed 并退出静默自动重试，避免对不可变配置错误无限退避。
                    # 不拆数据平面（mesh/distributor 保留），交由用户重新手动加入。
                    if not self._perm_auth_denied:
                        self._perm_auth_denied = True
                        self._reconnect_stop.set()
                        self._reconnect_active = False
                        self.auth_failed.emit(message)
                    return
                # 发射验证失败信号
                self.auth_failed.emit(message)
                self.disconnect()
                
        except Exception as e:
            self.log_message.emit(f"验证响应解析错误: {e}")
            with self._auth_wait:
                self._auth_result = False
                self._auth_wait.notify_all()
            if self._reconnect_active:
                # 自动重连试连异常：不弹窗、不拆数据平面，交重连循环关闭并试下一端点
                return
            self.auth_failed.emit(f"验证响应解析错误: {e}")
            self.disconnect()

    def _handle_delete(self, filename: str):
        """处理删除指令

        空文件名（主机端删除对应 IP 文件夹）时清空本端根目录全部内容。
        """
        if not filename:
            # 主机端删除 IP 文件夹：连接端清空根目录
            try:
                from sync.file_manager import safe_rmtree
                for item in os.listdir(self.sync_folder):
                    item_path = os.path.join(self.sync_folder, item)
                    if os.path.isdir(item_path):
                        safe_rmtree(item_path)
                    else:
                        os.remove(item_path)
                self.log_message.emit("清空根目录")
                self.file_deleted.emit('')
                return
            except Exception as e:
                self.log_message.emit(f"清空根目录失败: {e}")
                return
        try:
            file_path = self._safe_join(filename)
        except ValueError as e:
            self.log_message.emit(f"拒绝非法路径: {e}")
            return
        
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                from sync.file_manager import safe_rmtree
                safe_rmtree(file_path)

            self.log_message.emit(f"删除: {filename}")
            self.file_deleted.emit(filename)
            
        except Exception as e:
            self.log_message.emit(f"删除失败: {e}")
    
    def _handle_dir_create(self, dirname: str):
        """处理目录创建"""
        try:
            dir_path = self._safe_join(dirname)
        except ValueError as e:
            self.log_message.emit(f"拒绝非法路径: {e}")
            return
        os.makedirs(dir_path, exist_ok=True)
        self.log_message.emit(f"创建目录: {dirname}")
        
        # 发射目录创建信号
        self.dir_created.emit(dirname)
    
    def _handle_rename(self, content: bytes):
        """处理变更（重命名/移动）"""
        try:
            data = content.decode('utf-8').split('|')
            old_name = data[0]
            new_name = data[1]

            old_path = self._safe_join(old_name)
            new_path = self._safe_join(new_name)

            # 如果目标文件已存在，先删除
            if os.path.exists(new_path):
                if os.path.isdir(new_path):
                    from sync.file_manager import safe_rmtree
                    safe_rmtree(new_path)
                else:
                    os.unlink(new_path)

            os.rename(old_path, new_path)

            self.log_message.emit(f"变更: {old_name} → {new_name}")

            # 发射变更信号
            self.file_renamed.emit(old_name, new_name)

        except Exception as e:
            self.log_message.emit(f"变更失败: {e}")
    
    def _handle_file_list_response(self, file_list: list):
        """处理文件列表响应
        
        Args:
            file_list: 文件列表，格式为 [{"filename": "test.txt", "size": 1024, "mtime": 1234567890.123}, ...]
        """
        # 发射文件列表接收信号（让 SyncWindow 处理）
        self.file_list_received.emit(file_list)
    
    def _handle_file_request_from_server(self, filename: str):
        """处理主机端的文件请求
        
        Args:
            filename: 文件名（相对路径）
        """
        try:
            try:
                file_path = self._safe_join(filename)
            except ValueError as e:
                self.log_message.emit(f"拒绝非法路径: {e}")
                return
            
            if not os.path.exists(file_path):
                self.log_message.emit(f"文件不存在，无法发送: {filename}")
                return
            
            # 定义发送函数
            def send_file_func(stop_event: threading.Event, filename_arg: str, file_path_arg: str):
                try:
                    # 检查是否需要停止
                    if stop_event.is_set():
                        return
                    
                    # 发送文件给主机端（传递 stop_event 以支持中途取消）
                    self._send_file_to_server(filename_arg, file_path_arg, stop_event)
                except Exception as e:
                    self.log_message.emit(f"发送文件失败: {e}")
            
            # 将任务加入传输队列
            self.transfer_queue.add_task('file', send_file_func, filename, filename, file_path)
            
        except Exception as e:
            self.log_message.emit(f"发送文件失败: {e}")
    
    def _send_file_to_server(self, filename: str, file_path: str, stop_event: threading.Event = None):
        """发送文件给主机端（流式传输）

        Args:
            filename: 文件名（相对路径）
            file_path: 文件绝对路径
            stop_event: 停止标志（可选）
        """
        try:
            file_size = os.path.getsize(file_path)
            mtime = os.path.getmtime(file_path)

            # 统一使用流式分块传输
            self._send_large_file_to_server(filename, file_path, file_size, mtime, stop_event)

            # UI层通过进度条显示完成状态，无需额外日志

        except Exception as e:
            self.log_message.emit(f"发送文件失败: {e}")
    
    def _send_with_cancel(self, data: bytes, stop_event: threading.Event = None) -> bool:
        """
        发送数据，支持取消（已内置发送锁，保证一条消息不被并发写交错）。

        可恢复发送：背压超时（接收端处理慢导致发送缓冲区满）时不重发已发送字节、
        短暂退避后续发剩余部分，直至整条消息完整写出。相比 sendall 超时后整条重发
        的旧逻辑：重复数据流会加剧背压，把 PING/PONG 等控制消息长时间挡在发送锁外
        （同步延迟虚高上万毫秒的根因；投递走端到端直连、数据不经过主机 socket，故无此干扰）。
        - stop_event 被设置：立即返回 False
        - 连接错误（BrokenPipe/ConnectionReset/ConnectionAborted/OSError）：返回 False

        Returns:
            True 表示发送完成，False 表示被取消或连接异常
        """
        if stop_event and stop_event.is_set():
            return False
        try:
            return self._send_guard.send_resumable(self.socket, data, stop_event=stop_event)
        except Exception:
            return False
    
    def _send_large_file_to_server(self, filename: str, file_path: str, file_size: int, mtime: float, stop_event: threading.Event = None):
        """流式发送大文件给主机端
        
        Args:
            filename: 文件名（相对路径）
            file_path: 文件绝对路径
            file_size: 文件大小
            mtime: 修改时间
            stop_event: 停止标志（可选）
        """
        try:
            # 检查是否需要停止
            if stop_event and stop_event.is_set():
                return
            
            # 发送文件开始消息
            begin_msg = Protocol.pack_message(
                MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
            )
            if not self._send_with_cancel(begin_msg, stop_event):
                # 被取消或连接异常，通知接收端清理
                try:
                    self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送文件: {filename}")
                return
            
            # 流式读取并发送数据块
            chunk_index = 0
            sent_size = 0
            with open(file_path, 'rb') as f:
                while True:
                    # 检查是否需要停止
                    if stop_event and stop_event.is_set():
                        # 发送取消消息
                        try:
                            self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
                        except Exception:
                            pass
                        self.log_message.emit(f"取消发送文件: {filename}")
                        return
                    
                    chunk = f.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                    if not self._send_with_cancel(chunk_msg, stop_event):
                        # 被取消或连接异常，通知接收端清理
                        try:
                            self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
                        except Exception:
                            pass
                        self.log_message.emit(f"取消发送文件: {filename}")
                        return
                    chunk_index += 1
                    sent_size += len(chunk)

                    # 发射发送进度信号（转换为KB避免溢出）
                    sent_kb = sent_size // 1024
                    total_kb = file_size // 1024
                    self.file_send_progress.emit(filename, sent_kb, total_kb)

            # 发送文件结束消息
            end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
            if not self._send_with_cancel(end_msg, stop_event):
                try:
                    self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送文件: {filename}")
                return

            # 发送完成，发射完成信号，UI 据此移除进度条并固定在底部
            self.file_sent.emit(filename)

        except Exception as e:
            self.log_message.emit(f"发送大文件失败: {e}")
    
    def _handle_file_cancel(self, filename: str):
        """处理文件传输取消"""
        try:
            # 锁内获取引用并删除条目，锁外执行 IO
            with self._receiving_lock:
                rf = self.receiving_files.get(filename)
                if rf:
                    handle = rf.get('handle')
                    del self.receiving_files[filename]
                else:
                    handle = None

            if rf:
                # 关闭文件句柄
                if handle:
                    try:
                        handle.close()
                    except Exception:
                        pass

                # 删除临时文件
                temp_file_path = rf.get('temp_path')
                if temp_file_path and os.path.exists(temp_file_path):
                    os.remove(temp_file_path)

                # 通知 UI 清理接收进度条
                self.file_receive_cancelled.emit(filename)
                self.log_message.emit(f"取消接收文件: {filename}")

        except Exception as e:
            self.log_message.emit(f"取消文件传输失败: {e}")

    def _handle_file_notify(self, filename: str, file_size: int, mtime: float):
        """处理文件通知（静默，通知有新文件可用）

        Args:
            filename: 文件名（相对路径）
            file_size: 文件大小（字节）
            mtime: 修改时间
        """
        try:
            # 检查文件是否存在
            try:
                file_path = self._safe_join(filename)
            except ValueError as e:
                self.log_message.emit(f"拒绝非法路径: {e}")
                return

            need_request = False

            if not os.path.exists(file_path):
                # 文件不存在，需要请求
                need_request = True
            else:
                # 文件存在，比较大小和修改时间
                local_size = os.path.getsize(file_path)
                local_mtime = os.path.getmtime(file_path)

                # 检查文件大小是否不同
                size_different = local_size != file_size

                # 检查修改时间是否不同（允许2秒误差，兼容不同文件系统时间精度）
                mtime_different = abs(local_mtime - mtime) > 2.0

                # 如果大小或修改时间不同，需要请求
                if size_different or mtime_different:
                    need_request = True

            if need_request:
                # 发送 FILE_REQUEST_FORWARD 请求
                self._request_file_forward(filename)
            # else: 文件已存在且相同，静默跳过

        except Exception as e:
            self.log_message.emit(f"处理文件通知失败: {e}")

    def _request_file_forward(self, filename: str):
        """请求转发文件

        Args:
            filename: 文件名（相对路径）
        """
        try:
            if not self.authenticated:
                return

            # 发送 FILE_REQUEST_FORWARD 消息
            request_msg = Protocol.pack_message(MessageType.FILE_REQUEST_FORWARD, filename)
            self._send_guard.send(self.socket, request_msg)

        except Exception as e:
            self.log_message.emit(f"请求转发文件失败: {e}")
    
    # ========== 发送方法 ==========

    def send_bytes(self, data: bytes):
        """公开发送入口：将一条完整消息写入 socket（持发送锁，供非网络线程如 GUI 上报列表使用）"""
        self._send_guard.send(self.socket, data)

    def send_file(self, filepath: str, stop_event: threading.Event = None):
        """
        发送文件给服务器（连接端本地添加文件时调用）
        这是连接端添加文件时的同步入口

        使用流式传输，避免大文件占用过多内存

        Args:
            filepath: 文件绝对路径
            stop_event: 停止标志（可选，用于取消传输）
        """
        if not self.authenticated:
            self.log_message.emit("未连接，无法发送文件")
            return

        try:
            # 检查是否需要停止
            if stop_event and stop_event.is_set():
                return

            # 检查是否是文件夹
            if os.path.isdir(filepath):
                # 发送创建目录
                self.send_dir_create(filepath)
                return

            file_size = os.path.getsize(filepath)
            mtime = os.path.getmtime(filepath)
            rel_path = os.path.relpath(filepath, self.sync_folder).replace('\\', '/')

            # 统一使用流式分块传输
            self._send_large_file_streaming(rel_path, filepath, file_size, mtime, stop_event)

        except Exception as e:
            self.log_message.emit(f"发送文件失败: {e}")
    
    def _send_large_file_streaming(self, filename: str, filepath: str, file_size: int, mtime: float, stop_event: threading.Event = None):
        """
        流式发送大文件
        边读边发送，避免一次性占用大量内存
        
        Args:
            stop_event: 停止标志（可选，用于取消传输）
        """
        # 检查是否需要停止
        if stop_event and stop_event.is_set():
            return
        
        # 发送文件开始消息
        begin_msg = Protocol.pack_message(
            MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
        )
        if not self._send_with_cancel(begin_msg, stop_event):
            # 被取消或连接异常，通知接收端清理
            try:
                self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
            except Exception:
                pass
            self.log_message.emit(f"取消发送文件: {filename}")
            return
        
        # 发射开始发送信号
        self.file_send_progress.emit(filename, 0, file_size // 1024)
        
        # 流式读取并发送数据块
        chunk_index = 0
        sent_size = 0
        with open(filepath, 'rb') as f:
            while True:
                # 检查是否需要停止
                if stop_event and stop_event.is_set():
                    # 发送取消消息
                    try:
                        self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    self.log_message.emit(f"取消发送文件: {filename}")
                    return
                
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                
                chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                if not self._send_with_cancel(chunk_msg, stop_event):
                    # 被取消或连接异常，通知接收端清理
                    try:
                        self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    self.log_message.emit(f"取消发送文件: {filename}")
                    return
                
                sent_size += len(chunk)
                chunk_index += 1
                
                # 发送进度信号（转换为KB避免溢出）
                sent_kb = sent_size // 1024
                total_kb = file_size // 1024
                self.file_send_progress.emit(filename, sent_kb, total_kb)
        
        # 发送文件结束消息
        end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
        if not self._send_with_cancel(end_msg, stop_event):
            try:
                self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
            except Exception:
                pass
            self.log_message.emit(f"取消发送文件: {filename}")
            return
        
        # 发射发送完成信号
        self.file_sent.emit(filename)
        pass  # UI层通过进度条显示完成状态，无需额外日志
    
    def _format_size(self, size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    
    def _send_file(self, filename: str, content: bytes, mtime: float, stop_event: threading.Event = None):
        """发送文件（内部方法，流式传输）"""
        file_size = len(content)

        # 统一使用流式分块传输
        message = Protocol.pack_message(
            MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
        )
        if not self._send_with_cancel(message, stop_event):
            try:
                self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
            except Exception:
                pass
            self.log_message.emit(f"取消发送文件: {filename}")
            return

        # 发送数据块
        sent_size = 0
        for i in range(0, file_size, self.CHUNK_SIZE):
            if stop_event and stop_event.is_set():
                try:
                    self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送文件: {filename}")
                return
            chunk = content[i:i + self.CHUNK_SIZE]
            chunk_msg = Protocol.create_file_data_message(filename, i // self.CHUNK_SIZE, chunk)
            if not self._send_with_cancel(chunk_msg, stop_event):
                try:
                    self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送文件: {filename}")
                return
            sent_size += len(chunk)

            # 发射发送进度信号（转换为KB避免溢出）
            sent_kb = sent_size // 1024
            total_kb = file_size // 1024
            self.file_send_progress.emit(filename, sent_kb, total_kb)

        # 发送结束标记
        end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
        if not self._send_with_cancel(end_msg, stop_event):
            try:
                self._send_guard.send(self.socket, Protocol.create_file_cancel(filename))
            except Exception:
                pass
            self.log_message.emit(f"取消发送文件: {filename}")
            return
    
    def send_delete(self, filepath: str):
        """发送删除指令"""
        if not self.authenticated:
            return
        
        rel_path = os.path.relpath(filepath, self.sync_folder).replace('\\', '/')
        message = Protocol.create_delete_message(filepath, self.sync_folder)
        self._send_guard.send(self.socket, message)
        self.log_message.emit(f"发送删除指令: {rel_path}")
    
    def send_dir_create(self, dirpath: str):
        """发送创建目录指令"""
        if not self.authenticated:
            return
        
        rel_path = os.path.relpath(dirpath, self.sync_folder).replace('\\', '/')
        message = Protocol.create_dir_create_message(dirpath, self.sync_folder)
        self._send_guard.send(self.socket, message)
        self.log_message.emit(f"发送创建目录指令: {rel_path}")
    
    def send_rename(self, old_path: str, new_path: str):
        """发送变更指令"""
        if not self.authenticated:
            return
        
        message = Protocol.create_rename_message(old_path, new_path, self.sync_folder)
        self._send_guard.send(self.socket, message)
        
        old_rel = os.path.relpath(old_path, self.sync_folder).replace('\\', '/')
        new_rel = os.path.relpath(new_path, self.sync_folder).replace('\\', '/')
        self.log_message.emit(f"发送变更指令: {old_rel} → {new_rel}")
    
    def request_file_list(self):
        """请求文件列表"""
        if not self.authenticated:
            return
        
        message = Protocol.create_file_list_request()
        self._send_guard.send(self.socket, message)
        self.log_message.emit("请求文件列表")
    
    def request_file(self, filename: str):
        """请求特定文件"""
        if not self.authenticated:
            return
        
        message = Protocol.create_file_request(filename)
        self._send_guard.send(self.socket, message)
        self.log_message.emit(f"请求文件: {filename}")
    
    def send_file_cancel(self, filename: str):
        """发送文件取消传输指令"""
        if not self.authenticated:
            return
        
        message = Protocol.create_file_cancel(filename)
        self._send_guard.send(self.socket, message)
        self.log_message.emit(f"发送取消传输指令: {filename}")
