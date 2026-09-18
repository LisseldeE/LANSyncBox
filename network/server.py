"""
同步服务器
主机端运行，接收连接并转发文件
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import socket
import threading
import os
import time
import struct
import json
from typing import Dict, Optional
from PySide6.QtCore import QObject, Signal

from config import Config, UserConfig
from network.protocol import Protocol, MessageType, MessageReceiver
from network.mesh import MeshManager
from sync.distributor import Distributor
from sync.file_state_store import FileStateStore
from sync.vector import Endpoint
from utils.transfer_queue import TransferQueue
from utils.send_guard import SendLock


class SyncServer(QObject):
    """同步服务器"""
    
    # 信号
    client_connected = Signal(str)       # 客户端连接
    client_disconnected = Signal(str)    # 客户端断开
    error_occurred = Signal(str)         # 错误
    file_received = Signal(str)          # 收到文件
    file_receive_start = Signal(str, int)     # 开始接收文件 (filename, file_size)
    file_receive_progress = Signal(str, int, int)  # 文件接收进度 (filename, current, total)
    file_receive_cancelled = Signal(str)  # 文件接收被取消
    file_deleted = Signal(str)           # 文件已删除
    file_renamed = Signal(str, str)      # 文件已重命名 (old_name, new_name)
    dir_created = Signal(str)            # 目录已创建
    file_sent = Signal(str)              # 发送文件完成
    file_send_progress = Signal(str, int, int)     # 文件发送进度 (filename, current, total)
    file_forward_progress = Signal(str, str, int, int)  # 文件转发进度 (target_ip, filename, current, total)
    file_forward_sent = Signal(str, str)  # 文件转发完成 (target_ip, filename)
    file_forward_cancelled = Signal(str, str)  # 文件转发被取消 (target_ip, filename)
    log_message = Signal(str)            # 日志消息
    clipboard_received = Signal(str, bytes)  # 收到剪切板内容 (mime_type, data)，交 UI 写入主机自身剪贴板
    files_notify_received = Signal(bytes)    # 收到文件会话通知（content=JSON 字节），交 UI 展示远程文件胶囊
    mode_switching = Signal(str)      # 模式切换发起 (new_mode)，ACK 未收齐期间 UI 置灰切换按钮
    mode_changed = Signal(str, str)   # 模式切换完成 (old_mode, new_mode)
    perm_switching = Signal(str, str)       # 权限切换发起 (client_id, new_perm)，ACK 未收齐期间 UI 置灰该行胶囊
    perm_ack_received = Signal(str, str)    # 权限应用成功回执 (client_id, perm)，UI 落定胶囊
    perm_switch_failed = Signal(str, str, str)  # 权限切换失败（对端无响应） (client_id, old_perm, new_perm)，UI 回滚胶囊 + toast
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
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self.port = Config.DEFAULT_PORT  # 实际使用的端口
        self.clients: Dict[str, dict] = {}  # {client_id: {socket, receiver, thread}}
        self.sync_folder = Config.get_room_folder(room_code)
        self._lock = threading.Lock()
        
        # 创建传输队列，控制并发传输数量
        self.transfer_queue = TransferQueue(max_concurrent=5)
        
        # 记录正在请求的文件（文件名 -> 客户端ID）
        self.requesting_files: Dict[str, str] = {}

        # 心跳探测线程控制：周期性向各客户端发 PING 并核对 PONG，判定各端在线/离线
        self._ping_stop = threading.Event()
        self._ping_thread = None
        self.PING_INTERVAL = 2.0  # 每 2 秒向每个已认证客户端发送一次 PING
        self.OFFLINE_TIMEOUT = 8.0  # 心跳超时阈值：连续超过该时长未收到客户端 PONG → 判定离线并踢出

        # 模式状态：主机端管理模式切换
        self.mode = "sync"             # 当前模式："sync"（同步）/ "collect"（收集）
        self._pending_mode = None       # 切换中的目标模式（等待连接端 ACK）
        self.mode_ack_pending = {}      # {client_id: expected_mode}，等待 ACK 的客户端
        self._mode_ack_stop = threading.Event()
        self._mode_ack_thread = None
        self._ip_folders_created = set()  # 收集模式下创建过的 IP 文件夹名集合（含已断开连接的遗留文件夹）

        # 权限状态：主机端管理各连接端权限（默认读写，向后兼容）
        # 权限存储于 self.clients[client_id]['perm'] = "rw"（读写）/ "ro"（只读）
        self.perm_ack_pending = {}      # {client_id: {'perm': new_perm, 'old_perm': old_perm, 'tries': 重发次数}}
        self._perm_ack_stop = threading.Event()
        self._perm_ack_thread = None
        self.PERM_ACK_MAX_TRIES = 3     # 权限 ACK 补发上限，超过后回滚旧档位并通知 UI

        # 网状连接管理器（去中心化数据平面）：主机也是网状小节点，仅做引导 + 管理平面
        self.mesh: Optional[MeshManager] = None

        # 分发链路引擎（去中心化阶段 1）：本地操作沿网状直连传播，不依赖主机转发
        self.distributor: Optional[Distributor] = None

        # 自同步链路（去中心化阶段 2）：端到端文件状态对比与拉取
        self.file_state_store: Optional[FileStateStore] = None
        self._file_provider = None   # 本端 FileProvider（UI 注入；会话服务能力）
        # reuse 模式（全网状管理平面）：连接端复用的管理监听服务，共享宿主
        # mesh/distributor/store，不重建、不回收；start(reuse=True) 时置 True
        self._reuse = False
    
    def start(self, port: int = None, exclude_port: int = None,
              reuse: bool = False) -> bool:
        """启动服务器，尝试多个端口（9527-9536）

        Args:
            port: 指定端口（可选）
            exclude_port: 需要避开的端口（可选，如已占用的发现端口）
            reuse: reuse 模式（全网状管理平面）——本服务仅作管理监听，共享
                调用方注入的 mesh/distributor/file_state_store（不重建、不重连
                信号，避免打断宿主数据平面）；stop() 也不回收共享对象。
        """
        self._reuse = reuse
        start_port = port or Config.DEFAULT_PORT
        max_port = start_port + 10  # 尝试最多10个端口

        for try_port in range(start_port, max_port):
            # 避开已占用的发现端口
            if exclude_port and try_port == exclude_port:
                continue

            try:
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # 不使用 SO_REUSEADDR，避免端口被占用时仍能绑定
                self.server_socket.bind(('0.0.0.0', try_port))
                self.server_socket.listen(50)  # 支持最多50个设备同时连接，应对突发流量
                self.server_socket.settimeout(1.0)
                
                self.running = True
                self.port = try_port  # 记录实际使用的端口
                
                # 启动接受连接线程
                accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
                accept_thread.start()

                # 启动延迟探测线程
                self._ping_stop.clear()
                self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
                self._ping_thread.start()

                # reuse 模式跳过数据平面初始化：mesh/distributor/store 由调用方
                # （连接端宿主）注入，此处仅承担管理监听职责。
                if not reuse:
                    # 启动网状监听（去中心化数据平面，主机作为网状节点；失败不阻断主服务）
                    self.mesh = MeshManager(parent=self)
                    self.mesh.log_message.connect(self.log_message)
                    self.mesh.start()

                    # 分发链路引擎：本地操作 → 网状直连传播；收到信号去重/应用/转发
                    self.distributor = Distributor(
                        UserConfig.get_end_id(), self.sync_folder,
                        mesh=self.mesh, parent=self)
                    self.distributor.log_message.connect(self.log_message)
                    self.distributor.signal_applied.connect(self._on_distributor_applied)
                    self.distributor.set_protected_dirs(self._collect_ip_folders())
                    self.mesh.set_message_handler(self._on_mesh_message)

                    # 自同步链路（阶段 2）：端到端状态对比与拉取；直连建立自动补齐
                    self.file_state_store = FileStateStore(
                        UserConfig.get_end_id(), self.sync_folder,
                        mesh=self.mesh, distributor=self.distributor,
                        provider=self._file_provider, parent=self)
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

                return True
                
            except OSError as e:
                # 端口被占用，尝试下一个端口
                if self.server_socket:
                    try:
                        self.server_socket.close()
                    except Exception:
                        pass
                self.server_socket = None
                continue
            except Exception as e:
                self.error_occurred.emit(f"启动服务器失败: {e}")
                return False
        
        # 所有端口都尝试失败
        self.error_occurred.emit(f"启动服务器失败: 端口 {start_port}-{max_port-1} 均被占用")
        return False
    
    def stop(self):
        """停止服务器"""
        self.running = False
        self._ping_stop.set()
        
        # 关闭所有客户端连接，清理大文件接收状态
        with self._lock:
            for client_id, client_info in list(self.clients.items()):
                # 关闭大文件接收句柄，删除临时文件
                self._cleanup_client_receiving(client_info)
                try:
                    client_info['socket'].close()
                except Exception:
                    pass
            self.clients.clear()
        
        # 关闭服务器socket
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        self.server_socket = None

        # 关闭网状连接（reuse 模式共享宿主数据平面，不回收）
        if self.mesh and not self._reuse:
            self.mesh.stop()

        # 停止分发链路引擎（reuse 模式共享宿主数据平面，不回收）
        if self.distributor and not self._reuse:
            self.distributor.stop()
            self.distributor = None

        # 停止自同步链路（reuse 模式共享宿主数据平面，不回收）
        if self.file_state_store and not self._reuse:
            self.file_state_store.stop()
            self.file_state_store = None
    
    @staticmethod
    def _cleanup_client_receiving(client_info: dict):
        """清理客户端的大文件接收状态（关闭句柄、删除临时文件）"""
        receiving_files = client_info.get('receiving_files', {})
        for filename, rf in list(receiving_files.items()):
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
        client_info['receiving_files'] = {}

    def _safe_join(self, filename: str) -> str:
        """
        安全拼接同步文件夹路径，防止路径穿越攻击。
        如果 filename 包含 .. 或绝对路径等危险成分，抛出 ValueError。
        """
        if not filename:
            raise ValueError("文件名为空")
        # 标准化并获取绝对路径
        file_path = os.path.normpath(os.path.join(self.sync_folder, filename))
        sync_abs = os.path.abspath(self.sync_folder)
        file_abs = os.path.abspath(file_path)
        # 确保结果路径在同步文件夹内
        if file_abs != sync_abs and not file_abs.startswith(sync_abs + os.sep):
            raise ValueError(f"非法路径: {filename}")
        return file_path
    
    def _ping_loop(self):
        """心跳探测线程：每 2 秒向所有已认证客户端发送一次 PING；
        超过 OFFLINE_TIMEOUT 未收到某客户端 PONG 回包（拔线/杀进程等半开场景）
        即判定离线，关闭其 socket 令接收线程退出并触发 _remove_client。"""
        while not self._ping_stop.is_set():
            # 每隔 PING_INTERVAL 秒探测一轮，同时保留 CPU
            if self._ping_stop.wait(self.PING_INTERVAL):
                break
            if not self.running:
                break
            # 锁内收集已认证客户端，锁外执行网络IO（符合项目既有「锁内收集、锁外IO」约定）
            with self._lock:
                targets = [(cid, info) for cid, info in list(self.clients.items())
                           if info.get('authenticated')]
            now = time.time()
            for cid, info in targets:
                last = info.get('last_pong')
                if last is not None and now - last > self.OFFLINE_TIMEOUT:
                    self.log_message.emit(f"客户端 {cid} 心跳超时，判定离线")
                    try:
                        info['socket'].close()
                    except Exception:
                        pass
                    continue
                try:
                    # 可恢复发送：大文件传输背压时 PING 不丢失，避免误判离线
                    info['send_guard'].send_resumable(
                        info['socket'], Protocol.create_ping(now))
                except Exception:
                    pass

    def _accept_loop(self):
        """接受连接循环"""
        while self.running:
            try:
                client_socket, addr = self.server_socket.accept()
                client_id = f"{addr[0]}:{addr[1]}"

                self.log_message.emit(f"客户端连接: {client_id}")

                # 增大 TCP 缓冲区，避免大文件传输时 sendall 因缓冲区满而 1 秒超时
                # 单机多开场景下，发送端写入速度远超接收端处理速度，
                # 默认 64KB 缓冲区会迅速填满，导致 sendall 阻塞超过 1 秒超时失败
                try:
                    client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)  # 4MB
                    client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)  # 4MB
                except Exception:
                    pass
                
                # 创建客户端信息
                with self._lock:
                    self.clients[client_id] = {
                        'socket': client_socket,
                        'receiver': MessageReceiver(),
                        'authenticated': False,
                        'ip': addr[0],       # 客户端IP（自 client_id 取地址部分）
                        'last_pong': None,   # 心跳时间戳（收到 PONG 时更新），供 _ping_loop 判定在线/离线
                        'receiving_files': {},  # 大文件接收状态：{filename: {handle, file_size, mtime, received_size, temp_path}}
                        'send_guard': SendLock()  # 发送串行化：同一 socket 并发写不交错
                    }
                
                # 启动客户端处理线程
                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_id,),
                    daemon=True
                )
                client_thread.start()
                
                # 不在连接时发出信号，等认证成功后再发出
                
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.error_occurred.emit(f"接受连接错误: {e}")
    
    def _handle_client(self, client_id: str):
        """处理客户端"""
        with self._lock:
            client_info = self.clients.get(client_id)
            if not client_info:
                return
            client_socket = client_info['socket']
            receiver = client_info['receiver']
        
        client_socket.settimeout(1.0)
        
        while self.running:
            try:
                data = client_socket.recv(65536)
                if not data:
                    break
                
                receiver.feed(data)
                
                # 处理所有完整消息
                while receiver.has_complete_message():
                    message = receiver.get_message()
                    if message:
                        self._process_message(client_id, message)
                        
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.log_message.emit(f"客户端 {client_id} 错误: {e}")
                break
        
        # 客户端断开
        self._remove_client(client_id)
    
    def _process_message(self, client_id: str, message: tuple):
        """处理客户端消息"""
        msg_type, filename, file_size, mtime, hide_flag, content = message
        
        client_info = self.clients.get(client_id)
        if not client_info:
            return
        
        # 验证检查
        if not client_info['authenticated'] and msg_type != MessageType.AUTH_REQ:
            return
        
        if msg_type == MessageType.AUTH_REQ:
            self._handle_auth(client_id, content)

        elif msg_type == MessageType.END_INFO:
            # 去中心化：连接端身份登记 → 下发引导清单 + 广播 JOIN
            if isinstance(content, dict):
                self._handle_end_info(client_id, content)

        elif msg_type == MessageType.PING:
            # 连接端心跳探测：立即回 PONG 并原样带回发送时刻（可恢复发送，背压不丢包）
            try:
                send_time = struct.unpack('!d', content)[0]
                client_info['send_guard'].send_resumable(
                    client_info['socket'], Protocol.create_pong(send_time))
            except Exception:
                pass

        elif msg_type == MessageType.PONG:
            # 收到对端心跳回包：记录时间戳供 _ping_loop 判定该端在线/离线
            client_info['last_pong'] = time.time()

        elif msg_type == MessageType.FILE_BEGIN:
            # 大文件传输开始 - 使用临时文件
            # 主机兜底：只读端不允许上传（竞态窗口漏网拦截）
            if self._is_readonly(client_id):
                self._reject_readonly(client_id, "上传文件", filename)
                cancel_msg = Protocol.create_file_cancel(filename)
                try:
                    self._socket_send(client_info, cancel_msg)
                except Exception:
                    pass
                return
            # 收集模式下路由到连接端 IP 文件夹（接收端 key 仍用原始文件名，落盘路径记录在 target_filename）
            if self.mode == "collect":
                ip = client_info.get('ip', '')
                target_filename = f"{ip}/{filename}" if ip else filename
            else:
                target_filename = filename
            try:
                file_path = self._safe_join(target_filename)
            except ValueError as e:
                self.log_message.emit(f"拒绝非法路径: {e}")
                return
            temp_file_path = file_path + '.tmp'  # 临时文件

            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # 阶段 4：同步模式不再按 mtime 互取消并发传输（版本冲突由三层规则
            # 在分发链路收敛）；并发上传各自落盘，冲突覆盖拉取由自同步链路处理。
            # 收集模式下各连接端文件路由到各自 IP 文件夹，路径隔离无需比较版本。

            # 创建临时文件句柄，准备流式写入
            try:
                file_handle = open(temp_file_path, 'wb')
                client_info['receiving_files'][filename] = {
                    'file_size': file_size,
                    'mtime': mtime,
                    'received_size': 0,
                    'temp_path': temp_file_path,
                    'target_filename': target_filename,
                    'handle': file_handle
                }

                # 发射开始接收信号（传递文件大小）
                self.file_receive_start.emit(filename, file_size)
            except Exception as e:
                self.log_message.emit(f"创建文件失败: {e}")
        
        elif msg_type == MessageType.FILE_DATA:
            # 大文件数据块 - 流式写入
            rf = client_info['receiving_files'].get(filename)
            if rf and rf.get('handle'):
                handle = rf['handle']
                chunk_index, chunk_data = content
                try:
                    # 用 chunk_index 定位写入，避免乱序/重复/丢失导致文件损坏
                    offset = chunk_index * self.CHUNK_SIZE
                    handle.seek(offset)
                    handle.write(chunk_data)
                    rf['received_size'] += len(chunk_data)

                    # 发送进度信号（转换为MB避免溢出）
                    # 将字节转换为KB，避免大文件溢出
                    received_kb = rf['received_size'] // 1024
                    total_kb = rf['file_size'] // 1024
                    self.file_receive_progress.emit(
                        filename,
                        received_kb,
                        total_kb
                    )
                except Exception as e:
                    self.log_message.emit(f"写入数据块失败: {e}")
        
        elif msg_type == MessageType.FILE_END:
            # 大文件传输结束 - 重命名临时文件为正式文件
            rf = client_info['receiving_files'].get(filename)
            if rf and rf.get('handle'):
                handle = rf['handle']

                # 关闭文件句柄
                try:
                    handle.close()
                except Exception:
                    pass

                # 移除正在请求的文件记录
                with self._lock:
                    if filename in self.requesting_files:
                        del self.requesting_files[filename]

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
                    del client_info['receiving_files'][filename]
                    return

                # 重命名临时文件为正式文件（收集模式下落到连接端 IP 文件夹）
                try:
                    final_file_path = self._safe_join(rf.get('target_filename') or filename)
                except ValueError as e:
                    self.log_message.emit(f"拒绝非法路径: {e}")
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    del client_info['receiving_files'][filename]
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

                    # 静默通知其他客户端（按需转发；收集模式下不转发）。
                    # 阶段 4：同步模式 + 分发链路就绪时不再走旧字节通道——接收端
                    # 由网状 add 信号触发缺失拉取（向源端直连取字节），避免双路径。
                    if self.mode == "sync" and not (self.mesh and self.distributor):
                        self._notify_file_available(filename, rf['file_size'], rf['mtime'], exclude_client=client_id)

                except Exception as e:
                    self.log_message.emit(f"重命名文件失败: {e}")
                    # 删除临时文件
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)

                # 清理状态
                del client_info['receiving_files'][filename]
        
        elif msg_type == MessageType.DELETE:
            self._handle_delete(client_id, filename)
        
        elif msg_type == MessageType.DIR_CREATE:
            self._handle_dir_create(client_id, filename)
        
        elif msg_type == 0x05:  # RENAME
            self._handle_rename(client_id, content)
        
        elif msg_type == MessageType.FILE_LIST_REQ:
            # 文件列表请求
            self._handle_file_list_request(client_id)
        
        elif msg_type == MessageType.FILE_LIST_RESP:
            # 文件列表响应（连接端发送自己的文件列表）
            self._handle_client_file_list(client_id, content)
        
        elif msg_type == MessageType.FILE_REQUEST:
            # 文件请求
            self._handle_file_request(client_id, filename)
        
        elif msg_type == MessageType.FILE_CANCEL:
            # 文件传输取消
            self._handle_file_cancel(client_id, filename)

        elif msg_type == MessageType.FILE_REQUEST_FORWARD:
            # 文件转发请求（连接端请求转发文件）
            self._handle_file_request_forward(client_id, filename)

        elif msg_type == MessageType.CLIPBOARD_DATA:
            # 剪切板内容（文本/图片）：排除源端广播给其余连接端，并回投 UI 写主机自身剪贴板
            self._handle_clipboard(client_id, filename, content)

        elif msg_type == MessageType.CLIPBOARD_FILES_NOTIFY:
            # 剪切板文件会话通知：主机只转发元信息给其余连接端 + 回投主机自身 UI（不中转文件字节）
            self._handle_files_notify(client_id, content)

        elif msg_type == MessageType.MODE_REQ:
            # 连接端认证成功后请求当前模式：返回有效模式，收集模式下同时确保其 IP 文件夹已创建
            mode = self._pending_mode or self.mode
            resp = Protocol.create_mode_message(MessageType.MODE_RESP, mode)
            try:
                self._socket_send(client_info, resp)
            except Exception:
                pass
            if mode == "collect":
                self._ensure_client_ip_folder(client_id)
            # 随握手补发当前权限（复用 mode_received 回补机制，防信号早于 UI 连接丢失）
            perm = client_info.get('perm', "rw")
            perm_msg = Protocol.create_perm_message(MessageType.PERM_UPDATE, perm)
            try:
                self._socket_send(client_info, perm_msg)
            except Exception:
                pass

        elif msg_type == MessageType.MODE_ACK:
            # 连接端确认切换完成：从等待集合移除，全部到齐后执行切换后续逻辑
            mode = content.decode('utf-8', errors='ignore').strip()
            self._handle_mode_ack(client_id, mode)

        elif msg_type == MessageType.PERM_ACK:
            # 连接端确认权限应用完成：从等待集合移除，UI 落定胶囊
            perm = content.decode('utf-8', errors='ignore').strip()
            self._handle_perm_ack(client_id, perm)

    def _handle_files_notify(self, client_id: str, content: bytes):
        """处理主机收到的文件会话通知（复制端→主机）

        - 将该会话元信息广播给其余连接端（排除源端）。
        - 同时通过 files_notify_received 信号回投 UI 主线程，主机自身也展示可用远程文件胶囊。
        """
        if not content:
            return
        try:
            notify_dict = json.loads(content.decode('utf-8'))
        except Exception:
            return
        fwd = Protocol.create_files_notify(notify_dict)
        self._broadcast_data(fwd, exclude_client=client_id)
        self.files_notify_received.emit(content)

    def send_files_notify(self, notify_dict: dict):
        """主机本端复制时，向所有连接端广播文件会话通知。"""
        msg = Protocol.create_files_notify(notify_dict)
        self._broadcast_data(msg)

    def emit_files_notify(self, notify_dict: dict) -> bool:
        """投递通知沿网状分发链路广播（阶段 5）：mesh 就绪返回 True，否则 False。

        返回 False 时调用方回退旧路径（send_files_notify，经主机转发）。
        """
        if self.distributor:
            return self.distributor.emit_files_notify(notify_dict)
        return False

    def _handle_clipboard(self, client_id: str, mime_type: str, data: bytes):
        """处理主机收到的剪切板内容（文本/图片）

        - 数据由某个连接端上报；将该内容广播给其余连接端（不回给源端）。
        - 同时通过 clipboard_received 信号回投 UI 主线程，写入主机自身的系统剪贴板（满足"含主机"）。
        """
        if mime_type != "text":
            return  # 忽略非法类型，防伪（文本走剪贴板；图片/文件走文件 TCP 直连链路）
        msg = Protocol.create_clipboard_message(mime_type, data)
        # 排除源端，避免把内容回写给复制发起者（它本地已显示剪贴板）
        self._broadcast_data(msg, exclude_client=client_id)
        # 交 UI 写主机自身剪贴板（工作线程不直接碰 QClipboard）
        self.clipboard_received.emit(mime_type, data)

    def send_clipboard(self, mime_type: str, data: bytes):
        """主机本端复制时，向所有连接端广播剪切板内容

        Args:
            mime_type: "text"
            data: 内容字节
        """
        if mime_type != "text":
            return
        msg = Protocol.create_clipboard_message(mime_type, data)
        self._broadcast_data(msg)
    
    def _handle_auth(self, client_id: str, content: bytes):
        """处理验证请求"""
        try:
            data = content.decode('utf-8').split(':')
            sync_version = data[0] if len(data) > 0 else ''
            room_code = data[1] if len(data) > 1 else ''
            password_hash = data[2] if len(data) > 2 else ''

            import hashlib
            expected_hash = hashlib.sha256(self.password.encode()).hexdigest() if self.password else ''

            # 同步逻辑版本比对（优先级最高；只比内部号，UI/展示变更不要求全员升级）
            if sync_version != Config.SYNC_LOGIC_VERSION:
                fail_msg = f"同步逻辑版本不匹配（客户端 {sync_version}，本机 {Config.SYNC_LOGIC_VERSION}）"
                response = Protocol.create_auth_response(False, fail_msg)
                self._socket_send(self.clients[client_id], response)
                self.log_message.emit(f"客户端 {client_id} 验证失败: {fail_msg}")
                self._remove_client(client_id)
                return

            # 房间号验证
            if room_code != self.room_code:
                fail_msg = "房间号错误"
                response = Protocol.create_auth_response(False, fail_msg)
                self._socket_send(self.clients[client_id], response)
                self.log_message.emit(f"客户端 {client_id} 验证失败: {fail_msg}")
                self._remove_client(client_id)
                return

            # 密码验证
            if password_hash != expected_hash:
                fail_msg = "密码错误"
                response = Protocol.create_auth_response(False, fail_msg)
                self._socket_send(self.clients[client_id], response)
                self.log_message.emit(f"客户端 {client_id} 验证失败: {fail_msg}")
                self._remove_client(client_id)
                return

            # 验证成功
            self.clients[client_id]['authenticated'] = True
            self.clients[client_id]['perm'] = "rw"  # 新连接端默认读写（向后兼容，老用户无感）
            self.clients[client_id]['last_pong'] = time.time()  # 心跳计时起点（防认证后立即误判离线）
            response = Protocol.create_auth_response(True, "验证成功")
            self._socket_send(self.clients[client_id], response)
            self.log_message.emit(f"客户端 {client_id} 验证成功（同步逻辑版本 {sync_version}）")
            # 认证成功后通知 UI 更新连接数
            self.client_connected.emit(client_id)

            # 去中心化：认证握手后立即交换 END_INFO（本端身份 + 网状监听端口 + 管理端口）
            if self.mesh and self.mesh.mesh_port:
                try:
                    self._socket_send(self.clients[client_id], Protocol.create_end_info(
                        UserConfig.get_end_id(), socket.gethostname(),
                        self.mesh.mesh_port, mgmt_port=self.port))
                except Exception:
                    pass

        except Exception as e:
            self.log_message.emit(f"验证错误: {e}")
            self._remove_client(client_id)

    def _handle_end_info(self, client_id: str, content: dict):
        """处理连接端身份信息（0x24）：登记 end_id/mesh_port，下发引导清单并广播 JOIN。

        引导时序：认证握手后主机已下发自身 END_INFO，连接端回其 END_INFO；
        主机据此：① 把该端加入自身网状对端表（主机直连之）；② 向该端下发
        MESH_PEER_LIST（主机 + 其他已登记端）；③ 向其他已登记端广播
        MESH_PEER_JOIN，令其反向与该新端建直连。
        """
        end_id = content.get('end_id', '')
        name = content.get('name', '') or ''
        mesh_port = int(content.get('mesh_port', 0) or 0)
        mgmt_port = int(content.get('mgmt_port', 0) or 0)
        if not end_id or end_id == UserConfig.get_end_id():
            return
        ip = self.clients.get(client_id, {}).get('ip', '')
        with self._lock:
            info = self.clients.get(client_id)
            if not info or info.get('mesh_registered'):
                return  # 已登记过（幂等）
            info['end_id'] = end_id
            info['name'] = name
            info['mesh_port'] = mesh_port
            info['mgmt_port'] = mgmt_port  # 全网状管理平面：对端据此故障切换连接本端管理端口
            info['mesh_registered'] = True
        self.log_message.emit(f"端身份登记: {name}({end_id}) @ {ip}:{mesh_port}")

        # 主机作为网状节点，与新端直连（双向拨号由 mesh 去重规则收敛）
        if self.mesh:
            self.mesh.add_peer(Endpoint(end_id=end_id, name=name, ip=ip, mesh_port=mesh_port))

        # 该新端的引导清单：主机自身 + 其他已登记端（不含该端自己）
        peers = []
        if self.mesh and self.mesh.mesh_port:
            host_ep = self.mesh.endpoint.to_dict()
            host_ep['mgmt_port'] = self.port
            peers.append(host_ep)
        with self._lock:
            for cid, cinfo in list(self.clients.items()):
                if cid == client_id:
                    continue
                if cinfo.get('end_id') and cinfo.get('mesh_port'):
                    peers.append({
                        'end_id': cinfo['end_id'],
                        'name': cinfo.get('name', ''),
                        'ip': cinfo.get('ip', ''),
                        'mesh_port': cinfo['mesh_port'],
                        'mgmt_port': cinfo.get('mgmt_port', 0),
                    })
        if not peers:
            return
        try:
            self._socket_send(self.clients[client_id], Protocol.create_mesh_peer_list(peers))
        except Exception:
            pass
        # 广播 JOIN 给其他已登记端（旧端反向与新端建直连）
        self._broadcast_to_mesh_peers(Protocol.create_mesh_peer_join({
            'end_id': end_id,
            'name': name,
            'ip': ip,
            'mesh_port': mesh_port,
            'mgmt_port': mgmt_port,
        }), except_end_id=end_id)

    def _broadcast_to_mesh_peers(self, data: bytes, except_end_id: str = ''):
        """向所有已登记网状身份的已认证连接端广播一条管理消息（主机权威下发通道）。"""
        with self._lock:
            targets = [(cid, info) for cid, info in list(self.clients.items())
                       if info.get('authenticated') and info.get('end_id')
                       and info.get('end_id') != except_end_id]
        for cid, info in targets:
            try:
                self._socket_send(info, data)
            except Exception:
                pass

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
        """网状直连建立：同步模式下自动发起一轮状态对比（断线重连自动补齐，阶段 2）。"""
        if self.mode == "sync" and self.file_state_store:
            # 本地补扫兜底：mesh 未就绪窗口内添加/启动前已放置的文件入同步。
            # 排除各连接端 IP 私有文件夹，避免把收集区文件误作共享广播。
            try:
                self.file_state_store.emit_local_missing(
                    exclude_dirs=self._collect_ip_folders())
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

    def _handle_delete(self, client_id: str, filename: str):
        """处理删除请求（收集模式下路由到连接端 IP 文件夹，且不转发）"""
        # 主机兜底：只读端不允许修改同步列表
        if self._is_readonly(client_id):
            self._reject_readonly(client_id, "删除文件", filename)
            return
        # 收集模式下：连接端删除自身文件 → 在对应 IP 文件夹内删除
        target = filename
        if self.mode == "collect":
            ip = self.clients.get(client_id, {}).get('ip', '')
            if ip:
                target = f"{ip}/{filename}"
        try:
            file_path = self._safe_join(target)
        except ValueError as e:
            self.log_message.emit(f"拒绝非法路径: {e}")
            return

        # 受保护目录（连接端 IP 文件夹本体）不可删：判定「目标 == 某 IP 文件夹本身」
        # （owner 命中且其后无剩余子路径）。文件夹内部文件/子目录照常可删。
        owner = self._ip_folder_owner(target)
        if owner and not target[len(owner):].lstrip('/').lstrip('\\'):
            self.log_message.emit(f"拒绝删除受保护的连接端文件夹: {target}")
            return

        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                from sync.file_manager import safe_rmtree
                safe_rmtree(file_path)

            self.log_message.emit(f"删除: {target}")
            self.file_deleted.emit(filename)
            
            # 转发给其他客户端（收集模式下不转发）
            if self.mode == "sync":
                self._broadcast_delete(filename, exclude_client=client_id)
            
        except Exception as e:
            self.log_message.emit(f"删除失败: {e}")
    
    def _handle_dir_create(self, client_id: str, dirname: str):
        """处理目录创建（收集模式下路由到连接端 IP 文件夹，且不转发）"""
        # 主机兜底：只读端不允许修改同步列表
        if self._is_readonly(client_id):
            self._reject_readonly(client_id, "创建目录", dirname)
            return
        target = dirname
        if self.mode == "collect":
            ip = self.clients.get(client_id, {}).get('ip', '')
            if ip:
                target = f"{ip}/{dirname}"
        try:
            dir_path = self._safe_join(target)
        except ValueError as e:
            self.log_message.emit(f"拒绝非法路径: {e}")
            return
        os.makedirs(dir_path, exist_ok=True)
        
        self.log_message.emit(f"创建目录: {target}")
        
        # 发射目录创建信号
        self.dir_created.emit(dirname)
        
        # 转发给其他客户端（收集模式下不转发）
        if self.mode == "sync":
            self._broadcast_dir_create(dirname, exclude_client=client_id)
    
    def _handle_rename(self, client_id: str, content: bytes):
        """处理变更（重命名/移动）（收集模式下路由到连接端 IP 文件夹，且不转发）"""
        # 主机兜底：只读端不允许修改同步列表
        if self._is_readonly(client_id):
            try:
                data = content.decode('utf-8').split('|')
                detail = f"{data[0]} → {data[1]}" if len(data) > 1 else ''
            except Exception:
                detail = ''
            self._reject_readonly(client_id, "变更文件", detail)
            return
        try:
            data = content.decode('utf-8').split('|')
            old_name = data[0]
            new_name = data[1]

            old_target = old_name
            new_target = new_name
            if self.mode == "collect":
                ip = self.clients.get(client_id, {}).get('ip', '')
                if ip:
                    old_target = f"{ip}/{old_name}"
                    new_target = f"{ip}/{new_name}"

            old_path = self._safe_join(old_target)
            new_path = self._safe_join(new_target)

            # 如果目标文件已存在，先删除
            if os.path.exists(new_path):
                if os.path.isdir(new_path):
                    from sync.file_manager import safe_rmtree
                    safe_rmtree(new_path)
                else:
                    os.unlink(new_path)

            os.rename(old_path, new_path)

            self.log_message.emit(f"变更: {old_target} → {new_target}")

            # 发射变更信号
            self.file_renamed.emit(old_name, new_name)

            # 转发给其他客户端（收集模式下不转发）
            if self.mode == "sync":
                self._broadcast_rename(old_name, new_name, exclude_client=client_id)

        except Exception as e:
            self.log_message.emit(f"变更失败: {e}")
    
    def _handle_file_list_request(self, client_id: str):
        """处理文件列表请求"""
        try:
            # 获取文件列表（排除收集 IP 文件夹内容，收集区不参与同步）
            from sync.file_manager import FileManager
            from pathlib import Path
            
            file_manager = FileManager(Path(self.sync_folder))
            file_list = file_manager.get_file_list_for_sync()
            ip_folders = self._collect_ip_folders()
            if ip_folders:
                file_list = [
                    f for f in file_list
                    if not f['filename'].split('/', 1)[0] in ip_folders
                ]
            
            # 发送文件列表响应
            response = Protocol.create_file_list_response(file_list)
            self._socket_send(self.clients[client_id], response)
            
            self.log_message.emit(f"发送文件列表给 {client_id}: {len(file_list)} 个文件")
            
        except Exception as e:
            self.log_message.emit(f"发送文件列表失败: {e}")
    
    def _handle_client_file_list(self, client_id: str, client_file_list: list):
        """处理连接端发送的文件列表
        
        Args:
            client_id: 客户端ID
            client_file_list: 连接端返回的内容，可为两种格式：
                1) 兼容旧格式： [{"filename": "test.txt", "size": 1024, "mtime": ...}, ...]
                2) 新格式 dict： {"files": [...], "empty_dirs": ["subdir1", ...]}
                    其中 empty_dirs 为连接端的空目录，需在主机端补建
        """
        # 收集模式下不做差异仲裁：连接端加入不会触发全量同步逻辑
        if self.mode == "collect":
            self.log_message.emit(f"收集模式，忽略 {client_id} 的文件列表")
            return
        # 只读端仲裁：单向下拉（只核查本地相对远程缺少的内容）。
        # 不推送本地改动（不请求其文件）、不覆盖差异文件、不删除本地多余文件、
        # 不补建其上报的空目录；主机→连接端的缺失补齐与目录结构同步保留。
        readonly = self._is_readonly(client_id)
        # 阶段 4：同步模式 + 自同步链路就绪 → 端到端对比补齐（不再中心化仲裁）。
        # 主机作为网状节点参与 FILE_STATE 对比，各端独立拉取缺失文件。
        if self.mode == "sync" and self.file_state_store is not None:
            if self.file_state_store.request_all() > 0:
                return
        try:
            # 兼容解析新格式（dict）与旧格式（list）
            if isinstance(client_file_list, dict):
                client_files = client_file_list.get('files', []) or []
                client_empty_dirs = client_file_list.get('empty_dirs', []) or []
            else:
                client_files = client_file_list or []
                client_empty_dirs = []
            client_file_list = client_files

            # 同步连接端上报的空目录到主机端（连接端有、主机端缺失的空目录）
            # 每个目录独立容错：单目录失败（如与现有文件同名冲突）不影响其余目录的补建
            # 只读端不推送本地结构：跳过其空目录补建
            if not readonly:
                for dirname in client_empty_dirs:
                    if not dirname:
                        continue
                    try:
                        dir_path = self._safe_join(dirname)
                        if os.path.isdir(dir_path):
                            continue
                        os.makedirs(dir_path, exist_ok=True)
                        self.log_message.emit(f"创建目录: {dirname}")
                        # 发射目录创建信号，并广播给其他连接端（与收到 DIR_CREATE 行为一致）
                        self.dir_created.emit(dirname)
                        self._broadcast_dir_create(dirname, exclude_client=client_id)
                    except Exception as e:
                        self.log_message.emit(f"同步目录结构失败: {e}")

            # 获取主机端的文件列表
            from sync.file_manager import FileManager
            from pathlib import Path
            
            file_manager = FileManager(Path(self.sync_folder))
            host_file_list = file_manager.get_file_list_for_sync()

            # 排除收集 IP 文件夹内容：收集区文件只归属对应连接端，不参与差异同步
            ip_folders = self._collect_ip_folders()
            if ip_folders:
                host_file_list = [
                    f for f in host_file_list
                    if not f['filename'].split('/', 1)[0] in ip_folders
                ]
            
            # 创建主机端文件字典（文件名 -> 文件信息）
            host_dict = {f['filename']: f for f in host_file_list}
            
            # 创建连接端文件字典（文件名 -> 文件信息）
            client_dict = {f['filename']: f for f in client_file_list}
            
            # 找出需要同步的文件
            files_to_send_to_client = []  # 主机端需要发送给连接端的文件
            files_to_request_from_client = []  # 主机端需要从连接端请求的文件
            
            for filename, host_info in host_dict.items():
                if filename not in client_dict:
                    # 连接端缺失的文件，主机端直接发送给连接端
                    files_to_send_to_client.append(filename)
                else:
                    # 文件存在，比较修改时间和文件大小
                    client_info = client_dict[filename]
                    
                    # 如果文件大小不同，需要同步
                    if host_info['size'] != client_info['size']:
                        # 比较修改时间，发送最新版本
                        if host_info['mtime'] >= client_info['mtime']:
                            files_to_send_to_client.append(filename)
                        else:
                            files_to_request_from_client.append(filename)
                    # 如果文件大小相同，但修改时间不同，也需要同步
                    elif host_info['mtime'] > client_info['mtime']:
                        # 主机端文件更新，发送给连接端
                        files_to_send_to_client.append(filename)
                    elif client_info['mtime'] > host_info['mtime']:
                        # 连接端文件更新，请求连接端发送
                        files_to_request_from_client.append(filename)
            
            # 找出主机端缺失的文件（连接端有，主机没有）
            for filename, client_info in client_dict.items():
                if filename not in host_dict:
                    # 主机端缺失的文件，请求连接端发送
                    files_to_request_from_client.append(filename)

            # 只读端不推送本地改动：不请求其任何文件（单向下拉，主机侧不下发请求）
            if readonly:
                files_to_request_from_client = []
            
            # 同步空目录结构
            dir_diff_sent = False
            try:
                from sync.file_manager import FileManager
                from pathlib import Path
                host_fm = FileManager(Path(self.sync_folder))
                host_empty_dirs = host_fm.get_empty_directory_list()
                # 排除收集 IP 文件夹结构：空 IP 文件夹及其空子目录同样不参与同步
                if ip_folders:
                    host_empty_dirs = [
                        d for d in host_empty_dirs
                        if not d.split('/', 1)[0] in ip_folders
                    ]
                
                # 构建连接端目录集合（从文件路径推断父目录）
                client_dirs = set()
                for f in client_file_list:
                    filename = f['filename']
                    parts = filename.split('/')
                    for i in range(len(parts) - 1):
                        parent = '/'.join(parts[:i+1])
                        if parent:
                            client_dirs.add(parent)
                
                # 发送缺失的目录创建指令
                for dirname in host_empty_dirs:
                    if dirname not in client_dirs:
                        self._send_dir_create_to_client(client_id, dirname)
                        dir_diff_sent = True
            except Exception as e:
                self.log_message.emit(f"同步目录结构失败: {e}")
            
            # 发送缺失的文件给连接端
            if files_to_send_to_client:
                self.log_message.emit(f"发送 {len(files_to_send_to_client)} 个文件给 {client_id}")
                for filename in files_to_send_to_client:
                    try:
                        file_path = self._safe_join(filename)
                    except ValueError as e:
                        self.log_message.emit(f"跳过非法路径: {e}")
                        continue
                    
                    # 定义发送函数
                    def send_file_func(stop_event: threading.Event, client_id_arg: str, filename_arg: str, file_path_arg: str):
                        try:
                            # 检查是否需要停止
                            if stop_event.is_set():
                                return
                            
                            # 发送文件给客户端（传递 stop_event 以支持中途取消）
                            # 用带目标IP的转发路径：进度条按 ip:filename 分隔，避免与广播批次共享 filename 键而重叠
                            self._send_file_to_client_with_target(client_id_arg, filename_arg, file_path_arg, stop_event)
                        except Exception as e:
                            self.log_message.emit(f"发送文件失败: {e}")
                    
                    # 将任务加入传输队列（使用 client_id:filename 作为去重键，支持多客户端同文件并发传输）
                    task_key = f"{client_id}:{filename}"
                    self.transfer_queue.add_task('file', send_file_func, task_key, client_id, filename, file_path)
            
            # 请求连接端发送缺失的文件
            if files_to_request_from_client:
                self.log_message.emit(f"从 {client_id} 请求 {len(files_to_request_from_client)} 个文件")
                for filename in files_to_request_from_client:
                    self._request_file_from_client(client_id, filename)
            
            # 向连接端回传同步结果，用于显示"列表一致/正在补齐差异项"通知。
            # 初次加入、自动重连、手动同步（响应 SYNC_REQUEST）都会走到此处，行为统一。
            has_diff = bool(files_to_send_to_client or files_to_request_from_client) or dir_diff_sent
            try:
                from network.protocol import Protocol
                client_info = self.clients.get(client_id)
                client_sock = client_info.get('socket') if client_info else None
                if client_sock is not None and client_sock.fileno() != -1:
                    self._socket_send(client_info, Protocol.create_sync_result(has_diff))
            except Exception as e:
                self.log_message.emit(f"发送同步结果失败: {e}")
            
            # 如果没有需要同步的文件
            if not files_to_send_to_client and not files_to_request_from_client:
                self.log_message.emit(f"与 {client_id} 无需同步")
            
        except Exception as e:
            self.log_message.emit(f"处理连接端文件列表失败: {e}")
    
    def request_sync_all(self) -> int:
        """向所有已认证的连接端发送手动同步请求，触发其重新上报列表并差异化补齐

        Returns:
            成功下发请求的连接端数量
        """
        from network.protocol import Protocol
        sent = 0
        with self._lock:
            targets = [(cid, c) for cid, c in self.clients.items()
                       if c.get('authenticated') and c.get('socket') is not None]
        for client_id, client_info in targets:
            try:
                if client_info['socket'].fileno() == -1:
                    continue
                self._socket_send(client_info, Protocol.create_sync_request())
                sent += 1
                self.log_message.emit(f"已请求 {client_id} 同步")
            except Exception as e:
                self.log_message.emit(f"请求 {client_id} 同步失败: {e}")
        return sent
    
    def _compare_file_lists(self, host_files: list, client_files: list) -> list:
        """对比文件列表，找出需要请求的文件
        
        Args:
            host_files: 主机端文件列表
            client_files: 连接端文件列表
        
        Returns:
            需要请求的文件名列表
        """
        # 创建主机端文件字典（文件名 -> 文件信息）
        host_dict = {f['filename']: f for f in host_files}
        
        # 创建连接端文件字典（文件名 -> 文件信息）
        client_dict = {f['filename']: f for f in client_files}
        
        # 找出需要请求的文件
        files_to_request = []
        
        for filename, client_info in client_dict.items():
            if filename not in host_dict:
                # 主机端缺失的文件，需要请求
                files_to_request.append(filename)
            else:
                # 文件存在，比较修改时间
                host_info = host_dict[filename]
                if client_info['mtime'] > host_info['mtime']:
                    # 连接端文件更新，需要请求
                    files_to_request.append(filename)
        
        return files_to_request
    
    def _request_file_from_client(self, client_id: str, filename: str):
        """从连接端请求文件
        
        Args:
            client_id: 客户端ID
            filename: 文件名（相对路径）
        """
        try:
            # 记录正在请求的文件
            with self._lock:
                self.requesting_files[filename] = client_id
                client_info = self.clients.get(client_id)
                if not client_info:
                    return

            # 发送文件请求消息（持该客户端发送锁，避免并发写交错）
            request_msg = Protocol.create_file_request(filename)
            self._socket_send(client_info, request_msg)
            
            self.log_message.emit(f"请求文件 {filename} 从 {client_id}")
            
        except Exception as e:
            self.log_message.emit(f"请求文件失败: {e}")
            # 清理记录
            with self._lock:
                if filename in self.requesting_files:
                    del self.requesting_files[filename]
    
    def _send_dir_create_to_client(self, client_id: str, dirname: str):
        """发送创建目录指令给连接端
        
        Args:
            client_id: 客户端ID
            dirname: 目录名（相对路径）
        """
        try:
            msg = Protocol.pack_message(MessageType.DIR_CREATE, dirname)
            with self._lock:
                client_info = self.clients.get(client_id)
                if not client_info:
                    return
                sock = client_info.get('socket')
                if not sock:
                    return
            self._socket_send(client_info, msg)
            self.log_message.emit(f"创建目录: {dirname}")
        except Exception as e:
            self.log_message.emit(f"创建目录失败: {e}")
    
    def _handle_file_request(self, client_id: str, filename: str):
        """处理文件请求"""
        try:
            try:
                file_path = self._safe_join(filename)
            except ValueError as e:
                self.log_message.emit(f"拒绝非法路径: {e}")
                return

            if not os.path.exists(file_path):
                self.log_message.emit(f"文件不存在，无法发送: {filename}")
                return

            # 定义发送函数（用于传输队列，与 _handle_file_request_forward 一致）
            def send_file_func(stop_event: threading.Event, client_id_arg: str, filename_arg: str, file_path_arg: str):
                try:
                    if stop_event.is_set():
                        return
                    self._send_file_to_client_with_target(client_id_arg, filename_arg, file_path_arg, stop_event)
                except Exception as e:
                    self.log_message.emit(f"发送文件失败: {e}")

            # 将任务加入传输队列（使用 client_id:filename 作为去重键）
            task_key = f"{client_id}:{filename}"
            self.transfer_queue.add_task('file', send_file_func, task_key, client_id, filename, file_path)

        except Exception as e:
            self.log_message.emit(f"发送文件失败: {e}")
    
    def _handle_file_cancel(self, client_id: str, filename: str):
        """处理文件传输取消"""
        try:
            # 如果正在接收该文件，停止接收
            client_info = self.clients.get(client_id)
            if client_info:
                rf = client_info['receiving_files'].get(filename)
                if rf:
                    # 关闭文件句柄
                    handle = rf.get('handle')
                    if handle:
                        try:
                            handle.close()
                        except Exception:
                            pass

                    # 删除临时文件
                    temp_file_path = rf.get('temp_path')
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.remove(temp_file_path)

                    # 清理状态
                    del client_info['receiving_files'][filename]

                    # 通知 UI 清理接收进度条
                    self.file_receive_cancelled.emit(filename)
                    self.log_message.emit(f"取消接收文件: {filename}")
                else:
                    # 非"主机正在接收"场景：主机正在转发该文件给此客户端，取消对应转发任务。
                    # 转发线程看到 stop_event 后会中止并通过 _notify_forward_cancelled 清理转发进度条。
                    self.transfer_queue.cancel_task(f"{client_id}:{filename}")
                    self.log_message.emit(f"取消转发文件: {filename} → {client_id}")

        except Exception as e:
            self.log_message.emit(f"取消文件传输失败: {e}")

    def _notify_file_available(self, filename: str, file_size: int, mtime: float, exclude_client: str = None):
        """静默通知所有连接端有新文件可用（按需转发）

        Args:
            filename: 文件名（相对路径）
            file_size: 文件大小（字节）
            mtime: 修改时间
            exclude_client: 排除的客户端ID（发送者）
        """
        # 检查是否有其他客户端需要通知（排除发送者）
        with self._lock:
            other_clients = [
                (client_id, client_info)
                for client_id, client_info in self.clients.items()
                if client_id != exclude_client and client_info.get('authenticated')
            ]

        # 如果没有其他客户端，直接返回
        if not other_clients:
            return

        # 发送 FILE_NOTIFY 消息给其他客户端
        for client_id, client_info in other_clients:
            try:
                # 创建 FILE_NOTIFY 消息（包含文件名、大小、修改时间）
                notify_msg = Protocol.pack_message(
                    MessageType.FILE_NOTIFY, filename, file_size, False, b'', mtime
                )
                client_info['send_guard'].send(client_info['socket'], notify_msg)
            except Exception as e:
                self.log_message.emit(f"通知 {client_id} 文件可用失败: {e}")

    def _handle_file_request_forward(self, client_id: str, filename: str):
        """处理连接端的文件转发请求（按需转发）

        Args:
            client_id: 请求转发的客户端ID
            filename: 文件名（相对路径）
        """
        try:
            # 检查文件是否存在
            try:
                file_path = self._safe_join(filename)
            except ValueError as e:
                self.log_message.emit(f"拒绝非法路径: {e}")
                return

            if not os.path.exists(file_path):
                self.log_message.emit(f"文件不存在，无法转发: {filename}")
                return

            file_size = os.path.getsize(file_path)
            mtime = os.path.getmtime(file_path)

            # 定义发送函数（用于传输队列）
            def send_file_func(stop_event: threading.Event, client_id_arg: str, filename_arg: str, file_path_arg: str):
                try:
                    # 检查是否需要停止
                    if stop_event.is_set():
                        return

                    # 发送文件给客户端（传递目标IP用于进度条显示）
                    self._send_file_to_client_with_target(client_id_arg, filename_arg, file_path_arg, stop_event)
                except Exception as e:
                    self.log_message.emit(f"转发文件失败: {e}")

            # 将任务加入传输队列（使用 client_id:filename 作为去重键）
            task_key = f"{client_id}:{filename}"
            self.transfer_queue.add_task('file', send_file_func, task_key, client_id, filename, file_path)

        except Exception as e:
            self.log_message.emit(f"处理转发请求失败: {e}")

    def _send_file_in_memory(self, sock: socket.socket, client_id: str, filename: str, content: bytes, mtime: float, stop_event: threading.Event = None):
        """用内存中的 content 重新发送整个文件给单个客户端（用于广播失败后重试，流式传输）"""
        file_size = len(content)
        # 解析该客户端的发送守卫（含 socket 引用），统一走持锁发送
        with self._lock:
            client_info = self.clients.get(client_id)
        if not client_info:
            self.log_message.emit(f"重新发送失败: {filename} → {client_id}（客户端已断开）")
            return
        try:
            if stop_event and stop_event.is_set():
                return

            # 发送 FILE_BEGIN
            begin_msg = Protocol.pack_message(MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime)
            if not self._send_with_cancel(client_info, begin_msg, stop_event):
                self.log_message.emit(f"重新发送失败: {filename} → {client_id}")
                return

            # 发送数据块
            sent_size = 0
            for i in range(0, file_size, self.CHUNK_SIZE):
                if stop_event and stop_event.is_set():
                    try:
                        self._socket_send(client_info, Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    return
                chunk = content[i:i + self.CHUNK_SIZE]
                chunk_msg = Protocol.create_file_data_message(filename, i // self.CHUNK_SIZE, chunk)
                if not self._send_with_cancel(client_info, chunk_msg, stop_event):
                    # 中途失败，通知接收端清理
                    try:
                        self._socket_send(client_info, Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    self.log_message.emit(f"重新发送失败: {filename} → {client_id}")
                    return
                sent_size += len(chunk)

                # 发射发送进度信号（转换为KB避免溢出）
                sent_kb = sent_size // 1024
                total_kb = file_size // 1024
                self.file_send_progress.emit(filename, sent_kb, total_kb)

            # 发送 FILE_END
            end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
            if not self._send_with_cancel(client_info, end_msg, stop_event):
                # 中途失败，通知接收端清理
                try:
                    self._socket_send(client_info, Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"重新发送失败: {filename} → {client_id}")
                return
            self.log_message.emit(f"重新发送完成: {filename} → {client_id}")
        except Exception as e:
            self.log_message.emit(f"重新发送异常: {filename} → {client_id}: {e}")

    def _send_file_to_client(self, client_id: str, filename: str, file_path: str, stop_event: threading.Event = None, is_forward: bool = False):
        """发送文件给特定客户端（流式传输）

        Args:
            client_id: 客户端ID
            filename: 文件名（相对路径）
            file_path: 文件绝对路径
            stop_event: 停止标志（可选）
            is_forward: 是否为转发（可选，用于区分进度信号）
        """
        try:
            # 检查是否需要停止
            if stop_event and stop_event.is_set():
                return

            # 检查客户端是否仍然存在
            with self._lock:
                client_info = self.clients.get(client_id)
                if not client_info:
                    self.log_message.emit(f"客户端 {client_id} 不存在，无法发送文件: {filename}")
                    return
                client_socket = client_info['socket']

            file_size = os.path.getsize(file_path)
            mtime = os.path.getmtime(file_path)

            self.log_message.emit(f"发送文件给 {client_id}: {filename} ({self._format_size(file_size)})")

            # 统一使用流式分块传输
            self._send_large_file_to_client(client_id, filename, file_path, file_size, mtime, stop_event, is_forward)

        except Exception as e:
            self.log_message.emit(f"发送文件失败: {e}")
    
    def _socket_send(self, client_info: dict, data: bytes):
        """持发送锁将一条完整消息写入某客户端的 socket（串行化，避免并发写交错）。

        返回 True；发送异常向上抛出，由调用方处理（与直接 sendall 语义一致）。
        """
        client_info['send_guard'].send(client_info['socket'], data)
        return True

    @staticmethod
    def _send_with_cancel(client_info: dict, data: bytes, stop_event: threading.Event = None) -> bool:
        """
        发送数据给某客户端，支持取消（内部经其 send_guard 持锁发送，保证一条消息不被并发写交错）。

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
            return client_info['send_guard'].send_resumable(
                client_info['socket'], data, stop_event=stop_event)
        except Exception:
            return False
    
    def _send_large_file_to_client(self, client_id: str, filename: str, file_path: str, file_size: int, mtime: float, stop_event: threading.Event = None, is_forward: bool = False):
        """流式发送大文件给特定客户端

        Args:
            client_id: 客户端ID
            filename: 文件名（相对路径）
            file_path: 文件绝对路径
            file_size: 文件大小
            mtime: 修改时间
            stop_event: 停止标志（可选）
            is_forward: 是否为转发（可选，用于区分进度信号）
        """
        try:
            # 检查是否需要停止
            if stop_event and stop_event.is_set():
                return

            # 检查客户端是否仍然存在
            with self._lock:
                client_info = self.clients.get(client_id)
                if not client_info:
                    self.log_message.emit(f"客户端 {client_id} 不存在，无法发送大文件: {filename}")
                    return
                client_socket = client_info['socket']

            # 发送文件开始消息
            begin_msg = Protocol.pack_message(
                MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
            )
            if not self._send_with_cancel(client_info, begin_msg, stop_event):
                # 被取消或连接异常，通知接收端清理
                try:
                    self._socket_send(client_info, Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送大文件: {filename}")
                self._notify_forward_cancelled(client_id, filename, is_forward)
                return

            # 流式读取并发送数据块
            chunk_index = 0
            sent_size = 0
            with open(file_path, 'rb') as f:
                while True:
                    # 检查是否需要停止
                    if stop_event and stop_event.is_set():
                        # 发送取消消息给接收端
                        try:
                            self._socket_send(client_info, Protocol.create_file_cancel(filename))
                        except Exception:
                            pass
                        self.log_message.emit(f"取消发送大文件: {filename}")
                        self._notify_forward_cancelled(client_id, filename, is_forward)
                        return

                    chunk = f.read(self.CHUNK_SIZE)
                    if not chunk:
                        break

                    chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                    if not self._send_with_cancel(client_info, chunk_msg, stop_event):
                        # 被取消或连接异常，通知接收端清理
                        try:
                            self._socket_send(client_info, Protocol.create_file_cancel(filename))
                        except Exception:
                            pass
                        self.log_message.emit(f"取消发送大文件: {filename}")
                        self._notify_forward_cancelled(client_id, filename, is_forward)
                        return
                    chunk_index += 1
                    sent_size += len(chunk)

                    # 发射发送进度信号（转换为KB避免溢出）
                    sent_kb = sent_size // 1024
                    total_kb = file_size // 1024
                    if is_forward:
                        # 转发时使用 file_forward_progress 信号，包含目标IP
                        target_ip = client_id.split(':')[0]  # 从 client_id 提取IP
                        self.file_forward_progress.emit(target_ip, filename, sent_kb, total_kb)
                    else:
                        # 普通发送使用 file_send_progress 信号
                        self.file_send_progress.emit(filename, sent_kb, total_kb)

            # 发送文件结束消息
            end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
            if not self._send_with_cancel(client_info, end_msg, stop_event):
                try:
                    self._socket_send(client_info, Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送大文件: {filename}")
                self._notify_forward_cancelled(client_id, filename, is_forward)
                return

            # 发射发送完成信号
            if is_forward:
                target_ip = client_id.split(':')[0]  # 从 client_id 提取IP
                self.file_forward_sent.emit(target_ip, filename)
            else:
                self.file_sent.emit(filename)
            
        except Exception as e:
            self.log_message.emit(f"发送大文件失败: {e}")
            self._notify_forward_cancelled(client_id, filename, is_forward)

    def _send_file_to_client_with_target(self, client_id: str, filename: str, file_path: str, stop_event: threading.Event = None):
        """发送文件给特定客户端（转发模式，显示目标IP）

        Args:
            client_id: 客户端ID
            filename: 文件名（相对路径）
            file_path: 文件绝对路径
            stop_event: 停止标志（可选）
        """
        # 调用 _send_file_to_client，传递 is_forward=True
        self._send_file_to_client(client_id, filename, file_path, stop_event, is_forward=True)

    def _notify_forward_cancelled(self, client_id: str, filename: str, is_forward: bool):
        """转发被取消时通知 UI 清理对应进度条（携带目标IP）"""
        if not is_forward:
            return
        target_ip = client_id.split(':')[0]  # 从 client_id 提取IP
        self.file_forward_cancelled.emit(target_ip, filename)
    
    def _remove_client(self, client_id: str):
        """移除客户端"""
        end_id = None
        with self._lock:
            if client_id in self.clients:
                client_info = self.clients[client_id]
                end_id = client_info.get('end_id') or None  # 网状身份（LEAVE 通告用）
                # 清理大文件接收状态：关闭句柄、删除临时文件
                self._cleanup_client_receiving(client_info)
                try:
                    client_info['socket'].close()
                except Exception:
                    pass
                del self.clients[client_id]
        
        self.client_disconnected.emit(client_id)

        # 去中心化：端离线 → 通告其余端拆除直连 + 主机侧拆除网状直连。
        # reuse 模式（全网状管理平面）下本服务仅作管理监听：guest 直连断开
        # 不代表该端离线（其 mesh 数据平面可能仍存活），不广播 LEAVE、
        # 不 mesh.remove_peer（避免墓碑阻断其网状重连），由主机权威下发生命周期。
        if end_id and not self._reuse:
            try:
                self._broadcast_to_mesh_peers(Protocol.create_mesh_peer_leave(end_id))
            except Exception:
                pass
            if self.mesh:
                self.mesh.remove_peer(end_id)

        # 该客户端若正等待模式切换 ACK，直接移除（其已断开，无需再等）
        with self._lock:
            if client_id in self.mode_ack_pending:
                del self.mode_ack_pending[client_id]
                remaining = len(self.mode_ack_pending)
                pending = self._pending_mode
            else:
                remaining = None
                pending = None
            # 该客户端若正等待权限 ACK，直接移除（断连无需回滚，UI 由断连信号清理）
            if client_id in self.perm_ack_pending:
                del self.perm_ack_pending[client_id]
        if pending is not None and remaining == 0:
            self._complete_mode_switch(pending)
    
    # ========== 模式管理 ==========

    def switch_mode(self, new_mode: str) -> bool:
        """主机端发起模式切换（同步模式 <-> 收集模式）

        - 当前模式立即切换，文件路由等按新模式生效；
        - 广播 MODE_SWITCH 给所有已认证连接端并记录 ACK 等待集合；
        - 全部 ACK 到齐（或无在线连接端）后执行切换后续逻辑（清空队列 / 全量同步）。
        """
        if new_mode not in ("sync", "collect"):
            return False
        with self._lock:
            if self._pending_mode is not None:
                return False  # 已有切换进行中
            if new_mode == self.mode:
                return False  # 模式未变化
            old_mode = self.mode
            self.mode = new_mode
            self._pending_mode = new_mode
            self.mode_ack_pending = {
                cid: new_mode
                for cid, info in self.clients.items()
                if info.get('authenticated')
            }
            targets = [(cid, info) for cid, info in self.clients.items()
                       if info.get('authenticated')]

        # 发射切换发起信号（UI 置灰切换按钮）
        self.mode_switching.emit(new_mode)
        self.log_message.emit(
            f"切换模式: {self._mode_cn(old_mode)} → {self._mode_cn(new_mode)}，等待 {len(targets)} 个连接端确认"
        )

        # 广播模式切换指令
        msg = Protocol.create_mode_message(MessageType.MODE_SWITCH, new_mode)
        self._broadcast_data(msg)

        # 启动定时补发线程
        self._start_mode_ack_thread()

        # 无在线连接端：立即完成切换
        if not targets:
            self._complete_mode_switch(new_mode)
        return True

    @staticmethod
    def _mode_cn(mode: str) -> str:
        """模式中文名（日志显示用，避免残留英文）"""
        return "同步" if mode == "sync" else "收集"

    def _handle_mode_ack(self, client_id: str, mode: str):
        """处理连接端模式切换完成回执"""
        with self._lock:
            expected = self.mode_ack_pending.get(client_id)
            if expected is None:
                return  # 未在等待该客户端的 ACK（如重复回执）
            if mode and mode != expected:
                return  # 回执模式与预期不符，忽略
            del self.mode_ack_pending[client_id]
            pending = self._pending_mode
            remaining = len(self.mode_ack_pending)
        self.log_message.emit(f"连接端 {client_id} 已切换至 {self._mode_cn(mode)}")
        if pending is not None and remaining == 0:
            self._complete_mode_switch(pending)

    def _complete_mode_switch(self, new_mode: str):
        """全部连接端 ACK 到齐后执行切换后续逻辑（仅执行一次）"""
        with self._lock:
            if self._pending_mode is None:
                return
            old_mode = "collect" if new_mode == "sync" else "sync"
            self._pending_mode = None
            self.mode_ack_pending = {}
        self._stop_mode_ack_thread()

        if new_mode == "collect":
            # 正在传输的文件继续传输，仅清空排队列表
            self._clear_queued_tasks()
            # 为所有已认证连接端创建对应 IP 文件夹
            self._ensure_ip_folders()
        elif new_mode == "sync":
            # 取消同步中的文件并清空传输列表
            self.transfer_queue.cancel_all_tasks()
            # 收集期主机对根目录的本地增删被「收集不转发」跳过，未进状态表；
            # 切回同步补扫广播（排除各连接端 IP 私有文件夹），再触发全量差异补齐
            if self.file_state_store:
                try:
                    self.file_state_store.emit_local_missing(
                        exclude_dirs=self._collect_ip_folders())
                except Exception:
                    pass
            # 触发一次全量同步广播，各连接端上报列表后差异补齐
            self.request_sync_all()

        self.mode_changed.emit(old_mode, new_mode)
        self.log_message.emit(f"模式切换完成: {self._mode_cn(old_mode)} → {self._mode_cn(new_mode)}")

    def _start_mode_ack_thread(self):
        """启动模式切换 ACK 定时补发线程"""
        if self._mode_ack_thread and self._mode_ack_thread.is_alive():
            return
        self._mode_ack_stop.clear()
        self._mode_ack_thread = threading.Thread(target=self._mode_ack_loop, daemon=True)
        self._mode_ack_thread.start()

    def _stop_mode_ack_thread(self):
        """停止模式切换 ACK 补发线程"""
        self._mode_ack_stop.set()

    def _mode_ack_loop(self):
        """定时向未完成切换的连接端补发 MODE_SWITCH，直到全部 ACK 或切换被取消"""
        while not self._mode_ack_stop.wait(2.0):
            if not self.running:
                break
            with self._lock:
                pending = self._pending_mode
                if pending is None:
                    break
                targets = [
                    (cid, info) for cid, info in self.clients.items()
                    if cid in self.mode_ack_pending and info.get('authenticated')
                ]
                remaining = len(self.mode_ack_pending)
            if pending is None:
                break
            if remaining == 0:
                self._complete_mode_switch(pending)
                break
            if not targets:
                # 等待集合非空但目标都已断开（_remove_client 会清理），无需再补发
                continue
            msg = Protocol.create_mode_message(MessageType.MODE_SWITCH, pending)
            for cid, info in targets:
                try:
                    self._socket_send(info, msg)
                except Exception:
                    pass

    def _clear_queued_tasks(self):
        """清空传输队列中的排队任务（保留正在传输的任务）"""
        with self.transfer_queue.lock:
            self.transfer_queue.queue.clear()

    # ========== 权限管理 ==========

    @staticmethod
    def _perm_cn(perm: str) -> str:
        """权限中文名（日志显示用，避免残留英文）"""
        return "只读" if perm == "ro" else "读写"

    def set_perm(self, client_id: str, new_perm: str) -> bool:
        """主机端发起对某连接端的权限切换（读写 <-> 只读）

        - 权限立即存储生效（主机兜底侧即刻按新权限执行：只读端发来的
          FILE_BEGIN/DELETE/DIR_CREATE/RENAME 一律拒收）；
        - 发送 PERM_UPDATE 给该连接端并记录 ACK 等待；
        - ACK 到齐后发 perm_ack_received；重发超限无响应则回滚旧档位并发 perm_switch_failed。

        Returns:
            True 表示已发起切换；False 表示参数非法/客户端不在线/权限未变化。
        """
        if new_perm not in ("rw", "ro"):
            return False
        with self._lock:
            client_info = self.clients.get(client_id)
            if not client_info or not client_info.get('authenticated'):
                return False
            old_perm = client_info.get('perm', "rw")
            if old_perm == new_perm:
                return False  # 权限未变化，幂等
            if client_id in self.perm_ack_pending:
                return False  # 该端已有权限切换进行中
            client_info['perm'] = new_perm  # 立即生效
            self.perm_ack_pending[client_id] = {
                'perm': new_perm,
                'old_perm': old_perm,
                'tries': 0,
            }
        # 发射切换发起信号（UI 置灰该行胶囊）
        self.perm_switching.emit(client_id, new_perm)
        self.log_message.emit(
            f"切换权限: {client_id} {self._perm_cn(old_perm)} → {self._perm_cn(new_perm)}，等待连接端确认"
        )
        # 发送权限更新指令
        msg = Protocol.create_perm_message(MessageType.PERM_UPDATE, new_perm)
        try:
            self._socket_send(client_info, msg)
        except Exception:
            pass
        self._start_perm_ack_thread()
        return True

    def _handle_perm_ack(self, client_id: str, perm: str):
        """处理连接端权限应用完成回执"""
        with self._lock:
            pending = self.perm_ack_pending.get(client_id)
            if pending is None:
                return  # 未在等待该客户端的 ACK（如重复回执/握手指令自证）
            if perm and perm != pending['perm']:
                return  # 回执权限与预期不符，忽略
            del self.perm_ack_pending[client_id]
            applied_perm = pending['perm']
        self.perm_ack_received.emit(client_id, applied_perm)
        self.log_message.emit(f"连接端 {client_id} 已应用权限 {self._perm_cn(applied_perm)}")

    def _start_perm_ack_thread(self):
        """启动权限 ACK 定时补发线程"""
        if self._perm_ack_thread and self._perm_ack_thread.is_alive():
            return
        self._perm_ack_stop.clear()
        self._perm_ack_thread = threading.Thread(target=self._perm_ack_loop, daemon=True)
        self._perm_ack_thread.start()

    def _stop_perm_ack_thread(self):
        """停止权限 ACK 补发线程"""
        self._perm_ack_stop.set()

    def _perm_ack_loop(self):
        """定时向未完成权限切换的连接端补发 PERM_UPDATE，直到 ACK 或重发超限回滚"""
        while not self._perm_ack_stop.wait(2.0):
            if not self.running:
                break
            with self._lock:
                if not self.perm_ack_pending:
                    break
                targets = []      # 需要补发的 [(client_id, info, perm)]
                to_rollback = []  # 重发超限需要回滚的 [(client_id, old_perm, new_perm)]
                for cid, pend in list(self.perm_ack_pending.items()):
                    info = self.clients.get(cid)
                    if not info or not info.get('authenticated'):
                        # 已断开：无需补发，直接移除（UI 由断连信号清理）
                        del self.perm_ack_pending[cid]
                        continue
                    if pend['tries'] >= self.PERM_ACK_MAX_TRIES:
                        to_rollback.append((cid, pend['old_perm'], pend['perm']))
                        del self.perm_ack_pending[cid]
                        continue
                    pend['tries'] += 1
                    targets.append((cid, info, pend['perm']))
            for cid, old_perm, new_perm in to_rollback:
                with self._lock:
                    info = self.clients.get(cid)
                    if info:
                        info['perm'] = old_perm  # 回滚旧档位
                self.perm_switch_failed.emit(cid, old_perm, new_perm)
                self.log_message.emit(f"权限切换失败（对端无响应）: {cid} 回滚为 {old_perm}")
            for cid, info, perm in targets:
                msg = Protocol.create_perm_message(MessageType.PERM_UPDATE, perm)
                try:
                    self._socket_send(info, msg)
                except Exception:
                    pass

    def get_perm(self, client_id: str) -> str:
        """查询连接端当前权限（默认读写）"""
        info = self.clients.get(client_id)
        return info.get('perm', "rw") if info else "rw"

    def _is_readonly(self, client_id: str) -> bool:
        """连接端是否为只读权限"""
        info = self.clients.get(client_id)
        return bool(info and info.get('perm') == "ro")

    def _reject_readonly(self, client_id: str, action: str, filename: str = ''):
        """只读端越权操作：主机兜底拒收并记录日志（竞态窗口漏网拦截）"""
        detail = f" {filename}" if filename else ''
        self.log_message.emit(f"拒绝只读连接端 {client_id} 的{action}{detail}")

    def _clear_queued_tasks(self):
        """清空传输队列中的排队任务（保留正在传输的任务）"""
        with self.transfer_queue.lock:
            self.transfer_queue.queue.clear()

    def _ensure_client_ip_folder(self, client_id: str):
        """在收集模式下为指定连接端创建对应 IP 文件夹（收集区）

        文件夹仅用于收集该连接端投递的文件；创建后通过 dir_created 信号
        刷新主机端列表，但不广播（收集模式下 IP 文件夹隔离）。
        """
        client_info = self.clients.get(client_id)
        if not client_info:
            return
        ip = client_info.get('ip', '')
        if not ip:
            return
        try:
            folder = os.path.join(self.sync_folder, ip)
            os.makedirs(folder, exist_ok=True)
            # 记录已创建过的 IP 文件夹：即使该连接端后续断开，遗留文件夹仍不参与同步
            self._ip_folders_created.add(ip)
            self.dir_created.emit(ip)
            # 刷新受保护目录集：使新连接端 IP 文件夹立即受删除保护
            if self.distributor:
                self.distributor.set_protected_dirs(self._collect_ip_folders())
        except Exception as e:
            self.log_message.emit(f"创建IP文件夹失败: {e}")

    def _ensure_ip_folders(self):
        """为所有已认证连接端创建 IP 文件夹"""
        with self._lock:
            client_ids = [
                cid for cid, info in self.clients.items() if info.get('authenticated')
            ]
        for cid in client_ids:
            self._ensure_client_ip_folder(cid)

    def _collect_ip_folders(self) -> set:
        """返回收集模式下应排除的 IP 文件夹名集合

        = 当前已认证连接端的 IP ∪ 历史上创建过的 IP 文件夹名。
        遗留文件夹（连接端已断开）同样排除，避免切换回同步模式时被误同步。
        """
        with self._lock:
            connected = {info.get('ip', '') for info in self.clients.values()
                         if info.get('authenticated') and info.get('ip')}
            return connected | set(self._ip_folders_created)

    def _ip_folder_owner(self, rel_path: str) -> str:
        """判断相对路径是否位于某连接端的 IP 文件夹内

        Returns:
            命中的 IP（文件夹名），否则返回空字符串
        """
        if not rel_path:
            return ''
        first = rel_path.split('/', 1)[0]
        if first in self._collect_ip_folders():
            return first
        return ''

    def _send_mode_msg_to(self, client_id: str, data: bytes):
        """发送原始消息给指定客户端（按需转发用）"""
        with self._lock:
            client_info = self.clients.get(client_id)
            if not client_info:
                return
            sock = client_info.get('socket')
            if not sock:
                return
        try:
            self._socket_send(client_info, data)
        except Exception:
            pass

    def _find_clients_by_ip(self, ip: str) -> list:
        """按 IP 查找已认证连接端 client_id 列表（同 IP 多端时逐个下发）"""
        if not ip:
            return []
        with self._lock:
            return [cid for cid, info in self.clients.items()
                    if info.get('ip') == ip and info.get('authenticated')]
    
    # ========== 广播方法 ==========
    
    def broadcast_file(self, filepath: str, stop_event: threading.Event = None):
        """
        广播文件给所有客户端（主机端本地添加文件时调用）
        这是主机端添加文件时的同步入口

        使用流式传输，避免大文件占用过多内存

        Args:
            filepath: 文件绝对路径
            stop_event: 停止标志（可选，用于取消传输）
        """
        try:
            # 检查是否需要停止
            if stop_event and stop_event.is_set():
                return

            # 收集模式下主机端不转发文件：本地添加的文件仅落在主机同步文件夹
            if self.mode == "collect":
                rel_path = os.path.relpath(filepath, self.sync_folder).replace('\\', '/')
                self.log_message.emit(f"收集模式，不转发文件: {rel_path}")
                return

            # 检查是否是文件夹
            if os.path.isdir(filepath):
                # 广播创建目录
                self.broadcast_dir_create(filepath)
                return

            file_size = os.path.getsize(filepath)
            mtime = os.path.getmtime(filepath)
            rel_path = os.path.relpath(filepath, self.sync_folder).replace('\\', '/')

            # 检查是否有已验证的客户端
            with self._lock:
                authenticated_clients = [
                    (client_id, client_info)
                    for client_id, client_info in self.clients.items()
                    if client_info['authenticated']
                ]

            if not authenticated_clients:
                self.log_message.emit(f"广播文件: {rel_path} (无客户端)")
                return

            self.log_message.emit(f"广播文件: {rel_path} ({self._format_size(file_size)})")

            # 统一使用流式分块传输
            self._broadcast_large_file_streaming(rel_path, filepath, file_size, mtime, stop_event)

        except Exception as e:
            self.log_message.emit(f"广播文件失败: {e}")
    
    def _broadcast_large_file_streaming(self, filename: str, filepath: str, file_size: int, mtime: float, stop_event: threading.Event = None):
        """
        流式广播大文件
        边读边发送，避免一次性占用大量内存
        
        Args:
            stop_event: 停止标志（可选，用于取消传输）
        """
        # 检查是否需要停止
        if stop_event and stop_event.is_set():
            return
        
        # 维护失败客户端集合，一旦某客户端某次发送失败，后续不再发给它
        failed_clients = set()
        
        # 发送文件开始消息
        begin_msg = Protocol.pack_message(
            MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
        )
        if not self._broadcast_data(begin_msg, stop_event=stop_event, failed_clients=failed_clients, cancel_filename=filename):
            # 被 stop_event 取消，通知接收端清理
            self._broadcast_data(Protocol.create_file_cancel(filename), failed_clients=failed_clients)
            self.log_message.emit(f"取消广播大文件: {filename}")
            return
        
        # 流式读取并发送数据块
        chunk_index = 0
        sent_size = 0
        with open(filepath, 'rb') as f:
            while True:
                # 检查是否需要停止
                if stop_event and stop_event.is_set():
                    # 发送取消消息给所有接收端
                    self._broadcast_data(Protocol.create_file_cancel(filename), failed_clients=failed_clients)
                    self.log_message.emit(f"取消广播大文件: {filename}")
                    return
                
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                
                chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                if not self._broadcast_data(chunk_msg, stop_event=stop_event, failed_clients=failed_clients, cancel_filename=filename):
                    # 被 stop_event 取消，通知接收端清理
                    self._broadcast_data(Protocol.create_file_cancel(filename), failed_clients=failed_clients)
                    self.log_message.emit(f"取消广播大文件: {filename}")
                    return
                chunk_index += 1
                sent_size += len(chunk)
                
                # 发射发送进度信号（转换为KB避免溢出）
                sent_kb = sent_size // 1024
                total_kb = file_size // 1024
                self.file_send_progress.emit(filename, sent_kb, total_kb)
        
        # 发送文件结束消息
        end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
        if not self._broadcast_data(end_msg, stop_event=stop_event, failed_clients=failed_clients, cancel_filename=filename):
            # 被 stop_event 取消，通知接收端清理
            self._broadcast_data(Protocol.create_file_cancel(filename), failed_clients=failed_clients)
            self.log_message.emit(f"取消广播大文件: {filename}")
            return
        
        # 发射发送完成信号
        self.file_sent.emit(filename)
        pass  # UI层通过进度条显示完成状态，无需额外日志
        
        # 对失败客户端重新发送整个文件，确保最终同步完成
        # 失败客户端之前已收到 FILE_CANCEL 清理了接收状态，重新发送从 FILE_BEGIN 开始是安全的
        for failed_client_id in list(failed_clients):
            if stop_event and stop_event.is_set():
                break
            # 检查客户端是否仍然连接
            with self._lock:
                if failed_client_id not in self.clients or not self.clients[failed_client_id].get('authenticated'):
                    continue
            self.log_message.emit(f"重新发送文件给失败客户端: {failed_client_id}")
            # 补发语义与转发完全等价：必须传 is_forward=True。
            # 否则走非转发路径重建"发送"进度条，而其失败时 _notify_forward_cancelled 不发布取消信号，
            # 会导致重发的进度条永久残留（与历史 0-FIN 进度条问题同源）。
            self._send_file_to_client(failed_client_id, filename, filepath, stop_event, is_forward=True)

    def _format_size(self, size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    
    def broadcast_delete(self, filepath: str):
        """
        广播删除指令（主机端本地删除文件时调用）

        收集模式下：只有删除某连接端 IP 文件夹（或其内部文件）时才定向通知该连接端；
        删除 IP 文件夹本身时发送空文件名指令，对应连接端清空根目录；
        IP 文件夹之外的内容修改不发送任何信号。
        """
        rel_path = os.path.relpath(filepath, self.sync_folder).replace('\\', '/')
        if self.mode == "collect":
            ip = self._ip_folder_owner(rel_path)
            if not ip:
                self.log_message.emit(f"收集模式，忽略根目录变更: {rel_path}")
                return
            if rel_path == ip:
                # 主机删除整个 IP 文件夹 → 通知对应连接端清空根目录
                target_rel = ''
            else:
                target_rel = rel_path[len(ip) + 1:]
            msg = Protocol.create_delete_message(
                os.path.join(self.sync_folder, target_rel), self.sync_folder
            )
            for cid in self._find_clients_by_ip(ip):
                self._send_mode_msg_to(cid, msg)
            self.log_message.emit(f"收集模式，通知 {ip} 删除: {target_rel or '(清空根目录)'}")
            return
        self._broadcast_delete(rel_path)
        self.log_message.emit(f"广播删除: {rel_path}")
    
    def broadcast_cancel(self, filename: str):
        """
        广播取消传输指令（主机端取消传输时调用）
        
        Args:
            filename: 文件名（相对路径）
        """
        self._broadcast_cancel(filename)
        self.log_message.emit(f"广播取消传输: {filename}")
    
    def _broadcast_cancel(self, filename: str, exclude_client: str = None):
        """广播取消传输指令（内部方法）"""
        message = Protocol.create_file_cancel(filename)
        self._broadcast_data(message, exclude_client)
    
    def _broadcast_delete(self, filename: str, exclude_client: str = None):
        """广播删除指令（内部方法）"""
        message = Protocol.create_delete_message(
            os.path.join(self.sync_folder, filename), self.sync_folder
        )
        self._broadcast_data(message, exclude_client)
    
    def broadcast_dir_create(self, dirpath: str):
        """广播创建目录

        收集模式下：仅当目录位于某连接端 IP 文件夹内时定向通知该连接端；
        IP 文件夹之外的内容修改不发送任何信号。
        """
        rel_path = os.path.relpath(dirpath, self.sync_folder).replace('\\', '/')
        if self.mode == "collect":
            ip = self._ip_folder_owner(rel_path)
            if not ip:
                self.log_message.emit(f"收集模式，忽略根目录变更: {rel_path}")
                return
            target_rel = rel_path[len(ip) + 1:]
            msg = Protocol.create_dir_create_message(
                os.path.join(self.sync_folder, target_rel), self.sync_folder
            )
            for cid in self._find_clients_by_ip(ip):
                self._send_mode_msg_to(cid, msg)
            self.log_message.emit(f"收集模式，通知 {ip} 创建目录: {target_rel}")
            return
        self._broadcast_dir_create(rel_path)
        self.log_message.emit(f"广播创建目录: {rel_path}")
    
    def _broadcast_dir_create(self, dirname: str, exclude_client: str = None):
        """广播创建目录（内部方法）"""
        message = Protocol.create_dir_create_message(
            os.path.join(self.sync_folder, dirname), self.sync_folder
        )
        self._broadcast_data(message, exclude_client)
    
    def broadcast_rename(self, old_path: str, new_path: str):
        """广播重命名

        收集模式下：仅当旧/新路径都位于同一连接端 IP 文件夹内时定向通知该连接端；
        IP 文件夹之外的内容修改不发送任何信号。
        """
        old_rel = os.path.relpath(old_path, self.sync_folder).replace('\\', '/')
        new_rel = os.path.relpath(new_path, self.sync_folder).replace('\\', '/')
        if self.mode == "collect":
            old_ip = self._ip_folder_owner(old_rel)
            new_ip = self._ip_folder_owner(new_rel)
            if not old_ip or old_ip != new_ip:
                self.log_message.emit(f"收集模式，忽略根目录变更: {old_rel} → {new_rel}")
                return
            old_target = old_rel[len(old_ip) + 1:]
            new_target = new_rel[len(new_ip) + 1:]
            msg = Protocol.create_rename_message(
                os.path.join(self.sync_folder, old_target),
                os.path.join(self.sync_folder, new_target),
                self.sync_folder
            )
            for cid in self._find_clients_by_ip(old_ip):
                self._send_mode_msg_to(cid, msg)
            self.log_message.emit(f"收集模式，通知 {old_ip} 变更: {old_target} → {new_target}")
            return
        self._broadcast_rename(old_rel, new_rel)
        self.log_message.emit(f"广播变更: {old_rel} → {new_rel}")
    
    def _broadcast_rename(self, old_name: str, new_name: str, exclude_client: str = None):
        """广播重命名（内部方法）"""
        message = Protocol.create_rename_message(
            os.path.join(self.sync_folder, old_name),
            os.path.join(self.sync_folder, new_name),
            self.sync_folder
        )
        self._broadcast_data(message, exclude_client)
    
    def _broadcast_data(self, data: bytes, exclude_client: str = None, stop_event: threading.Event = None, failed_clients: set = None, cancel_filename: str = None) -> bool:
        """广播数据给所有客户端

        单个客户端发送失败时跳过该客户端继续发送给其他客户端，避免协议错乱。
        失败的客户端会被加入 failed_clients 集合，后续调用将跳过这些客户端。
        如果提供了 cancel_filename，失败客户端会收到 FILE_CANCEL 消息清理接收状态。
        只有 stop_event 被设置时才中断广播并返回 False。

        Args:
            data: 要广播的数据
            exclude_client: 排除的客户端ID
            stop_event: 停止标志（可选，用于取消传输）
            failed_clients: 已失败客户端集合（可选，会被原地修改）
            cancel_filename: 如果提供，发送失败的客户端会收到 FILE_CANCEL

        Returns:
            True 表示发送完成（或无目标），False 表示被 stop_event 取消
        """
        # 锁内只收集客户端引用，锁外执行网络IO（符合项目既有「锁内收集、锁外IO」约定）
        with self._lock:
            targets = [
                (client_id, client_info)
                for client_id, client_info in list(self.clients.items())
                if client_id != exclude_client
                and client_info['authenticated']
                and (failed_clients is None or client_id not in failed_clients)
            ]

        if not targets:
            return True

        # 锁外发送，避免阻塞其他线程
        for client_id, client_info in targets:
            if stop_event and stop_event.is_set():
                return False
            try:
                if not self._send_with_cancel(client_info, data, stop_event):
                    # stop_event 触发导致发送失败，中断广播
                    if stop_event and stop_event.is_set():
                        return False
                    # 连接异常导致发送失败，标记该客户端失败，后续不再发送
                    if failed_clients is not None:
                        failed_clients.add(client_id)
                    # 发送 FILE_CANCEL 清理接收端状态，避免残留临时文件
                    if cancel_filename:
                        self._send_with_cancel(client_info, Protocol.create_file_cancel(cancel_filename), None)
                    self.log_message.emit(f"发送给 {client_id} 失败，已取消该客户端传输")
            except Exception as e:
                if failed_clients is not None:
                    failed_clients.add(client_id)
                if cancel_filename:
                    self._send_with_cancel(client_info, Protocol.create_file_cancel(cancel_filename), None)
                self.log_message.emit(f"发送给 {client_id} 失败: {e}")
        return True
