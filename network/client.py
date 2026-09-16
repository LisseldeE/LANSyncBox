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

        # 当前权限：认证后由主机端下发（"rw"读写 / "ro"只读），默认读写（向后兼容）
        self.perm = "rw"
        self.perm_received = False  # 是否已收到主机权限下发（复用连接时用于补偿 UI 状态）

        # 心跳探测线程控制：周期性 PING 主机并核对 PONG 回包，判定本端在线/离线
        self._ping_stop = threading.Event()
        self._ping_thread = None
        self.PING_INTERVAL = 1.0  # 每 1 秒发送一次 PING 给主机（连接端单机独立探测，主机端汇总不增加负担）
        self.OFFLINE_TIMEOUT = 5.0  # 心跳超时阈值：连续超过该时长未收到主机 PONG → 判定离线并断开

        # 创建传输队列，控制并发传输数量
        self.transfer_queue = TransferQueue(max_concurrent=5)
    
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
    
    def connect_to_server(self, host: str, port: int = None) -> bool:
        """连接到服务器"""
        port = port or Config.DEFAULT_PORT
        
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # 增大 TCP 缓冲区，避免大文件传输时 sendall 因缓冲区满而 1 秒超时
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)  # 4MB
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)  # 4MB
            except Exception:
                pass
            self.socket.settimeout(5.0)
            self.socket.connect((host, port))
            self.socket.settimeout(1.0)
            self.host_ip = host
            
            self.running = True
            
            # 发送验证请求
            auth_msg = Protocol.create_auth_request(Config.APP_VERSION, self.room_code, self.password)
            self._send_guard.send(self.socket, auth_msg)
            
            # 启动接收线程
            receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            receive_thread.start()
            
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"连接服务器失败: {e}")
            return False
    
    def disconnect(self):
        """断开连接"""
        self.running = False
        self.authenticated = False
        self._ping_stop.set()

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

    def _start_ping_thread(self):
        """启动心跳探测线程"""
        if self._ping_thread and self._ping_thread.is_alive():
            return
        self._ping_stop.clear()
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self._ping_thread.start()

    def _ping_loop(self):
        """心跳探测线程：每 1 秒 PING 主机一次；超过 OFFLINE_TIMEOUT 未收到 PONG 回包
        即判定离线（主机拔线/杀进程等半开场景），主动断开连接触发 disconnected。"""
        while not self._ping_stop.is_set():
            if self._ping_stop.wait(self.PING_INTERVAL):
                break
            if not self.socket or not self.authenticated:
                continue
            with self._ping_lock:
                last = self._last_pong
            if last is not None and time.time() - last > self.OFFLINE_TIMEOUT:
                self.log_message.emit("主机心跳超时，判定离线")
                self.disconnect()
                break
            try:
                # 可恢复发送：大文件传输背压时 PING 不丢失，避免误判离线
                self._send_guard.send_resumable(
                    self.socket, Protocol.create_ping(time.time()))
            except Exception:
                pass

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
        """接收数据循环"""
        while self.running:
            try:
                data = self.socket.recv(65536)
                if not data:
                    break
                
                self.receiver.feed(data)
                
                # 处理所有完整消息
                while self.receiver.has_complete_message():
                    message = self.receiver.get_message()
                    if message:
                        self._process_message(message)
                        
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.log_message.emit(f"接收错误: {e}")
                break
        
        # 断开连接
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
            # 去中心化：主机端身份信息（含 mesh_port），记录主机端点供引导/建连
            if isinstance(content, dict):
                end_id = content.get('end_id', '')
                if end_id and end_id != UserConfig.get_end_id():
                    self.host_endpoint = Endpoint.from_dict({
                        'end_id': end_id,
                        'name': content.get('name', ''),
                        'ip': self.host_ip or '',
                        'mesh_port': int(content.get('mesh_port', 0) or 0),
                    })

        elif msg_type == MessageType.MESH_PEER_LIST:
            # 去中心化：主机引导 → 批量登记对端并建立网状直连
            if self.mesh and isinstance(content, dict):
                self.mesh.bootstrap_peers(content.get('peers', []))

        elif msg_type == MessageType.MESH_PEER_JOIN:
            # 去中心化：新端加入通告 → 反向与新端建直连
            if self.mesh and isinstance(content, dict):
                peer = content.get('peer')
                if isinstance(peer, dict):
                    self.mesh.add_peer(Endpoint.from_dict(peer))

        elif msg_type == MessageType.MESH_PEER_LEAVE:
            # 去中心化：端离线通告 → 拆除与该端的直连
            if self.mesh and isinstance(content, dict):
                self.mesh.remove_peer(content.get('end_id', ''))
    
    def _send_mode_request(self):
        """认证成功后向主机端请求当前模式"""
        if not self.socket:
            return
        try:
            self._send_guard.send(self.socket, Protocol.create_mode_message(MessageType.MODE_REQ, "sync"))
        except Exception:
            pass

    def _start_mesh_and_send_end_info(self):
        """去中心化：认证成功后启动网状监听，并向主机交换 END_INFO（含本端 mesh_port）。

        主机随后回 END_INFO 记录主机端点；新端加入的引导（MESH_PEER_LIST）与
        其余端的 JOIN 通告均在此认证链路上接收。
        """
        if self.mesh is None:
            self.mesh = MeshManager(parent=self)
            self.mesh.log_message.connect(self.log_message)
        self.mesh.start()
        # 分发链路引擎：本地操作 → 网状直连传播；收到信号去重/应用/转发
        self.distributor = Distributor(
            UserConfig.get_end_id(), self.sync_folder,
            mesh=self.mesh, parent=self)
        self.distributor.log_message.connect(self.log_message)
        self.distributor.signal_applied.connect(self._on_distributor_applied)
        self.mesh.set_message_handler(self._on_mesh_message)
        # 自同步链路（阶段 2）：端到端状态对比与拉取；直连建立自动补齐
        self.file_state_store = FileStateStore(
            UserConfig.get_end_id(), self.sync_folder,
            mesh=self.mesh, distributor=self.distributor,
            provider=self._file_provider, parent=self)
        self.file_state_store.log_message.connect(self.log_message)
        self.file_state_store.sync_done.connect(self.state_sync_done)
        self.file_state_store.file_added.connect(self.file_state_added)
        # 阶段 3：冲突覆盖（远端胜出）→ 自同步链路拉取胜方字节
        self.distributor.set_conflict_pull_handler(
            self.file_state_store.request_conflict_pull)
        # 阶段 5：网状投递通知 → 复用既有 files_notify_received 信号回投 UI
        self.distributor.files_notify_received.connect(self.files_notify_received)
        self.mesh.peer_connected.connect(self._on_mesh_peer_connected)
        if not self.socket or not self.mesh.mesh_port:
            return
        try:
            self._send_guard.send(self.socket, Protocol.create_end_info(
                UserConfig.get_end_id(), socket.gethostname(), self.mesh.mesh_port))
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
        """网状直连建立：同步模式 + 非只读下自动发起一轮状态对比（断线重连自动补齐，阶段 2）。"""
        if self.mode == "sync" and self.perm != "ro" and self.file_state_store:
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
                self.connected.emit()
                self.log_message.emit("验证成功")
                # 认证成功：心跳计时起点（此后周期 PING 主机并核对 PONG 判定在线/离线）
                with self._ping_lock:
                    self._last_pong = time.time()
                self._start_ping_thread()
                # 认证成功后请求当前模式，由主机端决定本端模式
                self._send_mode_request()
                # 去中心化：启动网状监听并交换 END_INFO（本端身份 + 网状监听端口）
                self._start_mesh_and_send_end_info()
            else:
                self.log_message.emit(f"验证失败: {message}")
                # 发射验证失败信号
                self.auth_failed.emit(message)
                self.disconnect()
                
        except Exception as e:
            self.log_message.emit(f"验证响应解析错误: {e}")
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
