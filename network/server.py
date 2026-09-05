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
from typing import Dict, Optional
from PySide6.QtCore import QObject, Signal

from config import Config
from network.protocol import Protocol, MessageType, MessageReceiver
from utils.transfer_queue import TransferQueue


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
    latency_updated = Signal(str, float)  # 延迟更新 (client_id, rtt_ms)
    
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

        # 延迟探测线程控制
        self._ping_stop = threading.Event()
        self._ping_thread = None
        self.PING_INTERVAL = 2.0  # 每 2 秒向每个已认证客户端发送一次 PING
    
    def start(self, port: int = None, exclude_port: int = None) -> bool:
        """启动服务器，尝试多个端口（9527-9536）

        Args:
            port: 指定端口（可选）
            exclude_port: 需要避开的端口（可选，如已占用的发现端口）
        """
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
        """延迟探测线程：每 2 秒向所有已认证客户端发送一次 PING"""
        while not self._ping_stop.is_set():
            # 每隔 PING_INTERVAL 秒探测一轮，同时保留 CPU
            if self._ping_stop.wait(self.PING_INTERVAL):
                break
            if not self.running:
                break
            # 锁内收集已认证客户端，锁外执行网络IO（符合项目既有「锁内收集、锁外IO」约定）
            with self._lock:
                targets = [(cid, info['socket']) for cid, info in list(self.clients.items()) if info.get('authenticated')]
            for cid, sock in targets:
                try:
                    sock.sendall(Protocol.create_ping(time.time()))
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
                        'latency': None,     # 延迟（RTT毫秒，N/A表示未知），由 PING/PONG 更新
                        'receiving_files': {}  # 大文件接收状态：{filename: {handle, file_size, mtime, received_size, temp_path}}
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

        elif msg_type == MessageType.PING:
            # 连接端探测延迟：立即回 PONG 并原样带回发送时刻
            try:
                send_time = struct.unpack('!d', content)[0]
                client_info['socket'].sendall(Protocol.create_pong(send_time))
            except Exception:
                pass

        elif msg_type == MessageType.PONG:
            # 收到对端回包：RTT = 当前时刻 - 发起时刻（同一端时钟，无跨机偏差）
            # 同时更新该连接端的延迟，供主机端悬浮窗显示
            try:
                send_time = struct.unpack('!d', content)[0]
                rtt_ms = (time.time() - send_time) * 1000.0
                client_info['latency'] = rtt_ms
                self.latency_updated.emit(client_id, rtt_ms)
            except Exception:
                pass

        elif msg_type == MessageType.FILE_BEGIN:
            # 大文件传输开始 - 使用临时文件
            try:
                file_path = self._safe_join(filename)
            except ValueError as e:
                self.log_message.emit(f"拒绝非法路径: {e}")
                return
            temp_file_path = file_path + '.tmp'  # 临时文件

            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # 检查是否已经有其他客户端正在接收该文件
            cancel_self = False
            with self._lock:
                for other_client_id, other_client_info in self.clients.items():
                    if other_client_id != client_id:
                        other_rf = other_client_info.get('receiving_files', {}).get(filename)
                        if other_rf:
                            # 比较修改时间，只接收最新版本的文件
                            other_mtime = other_rf['mtime']
                            if mtime > other_mtime:
                                # 当前文件更新，取消其他客户端的接收
                                self.log_message.emit(f"取消 {other_client_id} 的文件接收（版本较旧）")
                                # 关闭文件句柄
                                handle = other_rf.get('handle')
                                if handle:
                                    try:
                                        handle.close()
                                    except Exception:
                                        pass
                                # 删除临时文件
                                other_temp_path = other_rf['temp_path']
                                if os.path.exists(other_temp_path):
                                    os.remove(other_temp_path)
                                # 清理状态
                                del other_client_info['receiving_files'][filename]
                            else:
                                # 当前文件较旧，取消接收
                                self.log_message.emit(f"取消 {client_id} 的文件接收（版本较旧）")
                                cancel_self = True
                                break

            # 在锁外发送取消消息，避免阻塞其他线程
            if cancel_self:
                cancel_msg = Protocol.create_file_cancel(filename)
                try:
                    client_info['socket'].sendall(cancel_msg)
                except Exception:
                    pass
                return

            # 创建临时文件句柄，准备流式写入
            try:
                file_handle = open(temp_file_path, 'wb')
                client_info['receiving_files'][filename] = {
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

                # 重命名临时文件为正式文件
                try:
                    final_file_path = self._safe_join(filename)
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

                    # 静默通知其他客户端（按需转发）
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
    
    def _handle_auth(self, client_id: str, content: bytes):
        """处理验证请求"""
        try:
            data = content.decode('utf-8').split(':')
            version = data[0] if len(data) > 0 else ''
            room_code = data[1] if len(data) > 1 else ''
            password_hash = data[2] if len(data) > 2 else ''

            import hashlib
            expected_hash = hashlib.sha256(self.password.encode()).hexdigest() if self.password else ''

            # 版本比对（优先级最高）
            if version != Config.APP_VERSION:
                fail_msg = f"版本不匹配（客户端 {version}，主机 {Config.APP_VERSION}）"
                response = Protocol.create_auth_response(False, fail_msg)
                self.clients[client_id]['socket'].sendall(response)
                self.log_message.emit(f"客户端 {client_id} 验证失败: {fail_msg}")
                self._remove_client(client_id)
                return

            # 房间号验证
            if room_code != self.room_code:
                fail_msg = "房间号错误"
                response = Protocol.create_auth_response(False, fail_msg)
                self.clients[client_id]['socket'].sendall(response)
                self.log_message.emit(f"客户端 {client_id} 验证失败: {fail_msg}")
                self._remove_client(client_id)
                return

            # 密码验证
            if password_hash != expected_hash:
                fail_msg = "密码错误"
                response = Protocol.create_auth_response(False, fail_msg)
                self.clients[client_id]['socket'].sendall(response)
                self.log_message.emit(f"客户端 {client_id} 验证失败: {fail_msg}")
                self._remove_client(client_id)
                return

            # 验证成功
            self.clients[client_id]['authenticated'] = True
            response = Protocol.create_auth_response(True, "验证成功")
            self.clients[client_id]['socket'].sendall(response)
            self.log_message.emit(f"客户端 {client_id} 验证成功（版本 {version}）")
            # 认证成功后通知 UI 更新连接数
            self.client_connected.emit(client_id)

        except Exception as e:
            self.log_message.emit(f"验证错误: {e}")
            self._remove_client(client_id)
    
    def _handle_delete(self, client_id: str, filename: str):
        """处理删除请求"""
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
            
            # 转发给其他客户端
            self._broadcast_delete(filename, exclude_client=client_id)
            
        except Exception as e:
            self.log_message.emit(f"删除失败: {e}")
    
    def _handle_dir_create(self, client_id: str, dirname: str):
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
        
        # 转发给其他客户端
        self._broadcast_dir_create(dirname, exclude_client=client_id)
    
    def _handle_rename(self, client_id: str, content: bytes):
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

            self.log_message.emit(f"变更: {old_name} -> {new_name}")

            # 发射变更信号
            self.file_renamed.emit(old_name, new_name)

            # 转发给其他客户端
            self._broadcast_rename(old_name, new_name, exclude_client=client_id)

        except Exception as e:
            self.log_message.emit(f"变更失败: {e}")
    
    def _handle_file_list_request(self, client_id: str):
        """处理文件列表请求"""
        try:
            # 获取文件列表
            from sync.file_manager import FileManager
            from pathlib import Path
            
            file_manager = FileManager(Path(self.sync_folder))
            file_list = file_manager.get_file_list_for_sync()
            
            # 发送文件列表响应
            response = Protocol.create_file_list_response(file_list)
            self.clients[client_id]['socket'].sendall(response)
            
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
            
            # 同步空目录结构
            dir_diff_sent = False
            try:
                from sync.file_manager import FileManager
                from pathlib import Path
                host_fm = FileManager(Path(self.sync_folder))
                host_empty_dirs = host_fm.get_empty_directory_list()
                
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
                            self._send_file_to_client(client_id_arg, filename_arg, file_path_arg, stop_event)
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
                client_sock = self.clients.get(client_id, {}).get('socket')
                if client_sock is not None and client_sock.fileno() != -1:
                    client_sock.sendall(Protocol.create_sync_result(has_diff))
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
            targets = [(cid, c.get('socket')) for cid, c in self.clients.items()
                       if c.get('authenticated') and c.get('socket') is not None]
        for client_id, sock in targets:
            try:
                if sock.fileno() == -1:
                    continue
                sock.sendall(Protocol.create_sync_request())
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
            
            # 发送文件请求消息
            request_msg = Protocol.create_file_request(filename)
            self.clients[client_id]['socket'].sendall(request_msg)
            
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
            sock.sendall(msg)
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
                    self._send_file_to_client(client_id_arg, filename_arg, file_path_arg, stop_event)
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
                client_info['socket'].sendall(notify_msg)
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
        try:
            if stop_event and stop_event.is_set():
                return

            # 发送 FILE_BEGIN
            begin_msg = Protocol.pack_message(MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime)
            if not self._send_with_cancel(sock, begin_msg, stop_event):
                self.log_message.emit(f"重新发送失败: {filename} -> {client_id}")
                return

            # 发送数据块
            sent_size = 0
            for i in range(0, file_size, self.CHUNK_SIZE):
                if stop_event and stop_event.is_set():
                    try:
                        sock.sendall(Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    return
                chunk = content[i:i + self.CHUNK_SIZE]
                chunk_msg = Protocol.create_file_data_message(filename, i // self.CHUNK_SIZE, chunk)
                if not self._send_with_cancel(sock, chunk_msg, stop_event):
                    # 中途失败，通知接收端清理
                    try:
                        sock.sendall(Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    self.log_message.emit(f"重新发送失败: {filename} -> {client_id}")
                    return
                sent_size += len(chunk)

                # 发射发送进度信号（转换为KB避免溢出）
                sent_kb = sent_size // 1024
                total_kb = file_size // 1024
                self.file_send_progress.emit(filename, sent_kb, total_kb)

            # 发送 FILE_END
            end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
            if not self._send_with_cancel(sock, end_msg, stop_event):
                # 中途失败，通知接收端清理
                try:
                    sock.sendall(Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"重新发送失败: {filename} -> {client_id}")
                return
            self.log_message.emit(f"重新发送完成: {filename} -> {client_id}")
        except Exception as e:
            self.log_message.emit(f"重新发送异常: {filename} -> {client_id}: {e}")

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
    
    @staticmethod
    def _send_with_cancel(sock: socket.socket, data: bytes, stop_event: threading.Event = None) -> bool:
        """
        发送数据，支持取消。

        socket 为阻塞模式带 1 秒超时。sendall 最多阻塞 1 秒。
        - stop_event 被设置：立即返回 False
        - 连接错误（BrokenPipe/ConnectionReset/ConnectionAborted/OSError）：返回 False，不重试
        - 超时（socket.timeout）：可能是背压（接收端处理慢），只要 stop_event 未设置就继续重试

        Returns:
            True 表示发送完成，False 表示被取消或连接异常
        """
        if stop_event and stop_event.is_set():
            return False
        while True:
            if stop_event and stop_event.is_set():
                return False
            try:
                sock.sendall(data)
                return True
            except socket.timeout:
                # 背压超时：接收端处理慢导致发送缓冲区满
                # 只要没被取消就继续重试，确保大文件传输不会因背压而失败
                continue
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return False
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
            if not self._send_with_cancel(client_socket, begin_msg, stop_event):
                # 被取消或连接异常，通知接收端清理
                try:
                    client_socket.sendall(Protocol.create_file_cancel(filename))
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
                            client_socket.sendall(Protocol.create_file_cancel(filename))
                        except Exception:
                            pass
                        self.log_message.emit(f"取消发送大文件: {filename}")
                        self._notify_forward_cancelled(client_id, filename, is_forward)
                        return

                    chunk = f.read(self.CHUNK_SIZE)
                    if not chunk:
                        break

                    chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                    if not self._send_with_cancel(client_socket, chunk_msg, stop_event):
                        # 被取消或连接异常，通知接收端清理
                        try:
                            client_socket.sendall(Protocol.create_file_cancel(filename))
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
            if not self._send_with_cancel(client_socket, end_msg, stop_event):
                try:
                    client_socket.sendall(Protocol.create_file_cancel(filename))
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
        with self._lock:
            if client_id in self.clients:
                client_info = self.clients[client_id]
                # 清理大文件接收状态：关闭句柄、删除临时文件
                self._cleanup_client_receiving(client_info)
                try:
                    client_info['socket'].close()
                except Exception:
                    pass
                del self.clients[client_id]
        
        self.client_disconnected.emit(client_id)
    
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
            self._send_file_to_client(failed_client_id, filename, filepath, stop_event)

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
        """
        rel_path = os.path.relpath(filepath, self.sync_folder).replace('\\', '/')
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
        """广播创建目录"""
        rel_path = os.path.relpath(dirpath, self.sync_folder).replace('\\', '/')
        self._broadcast_dir_create(rel_path)
        self.log_message.emit(f"广播创建目录: {rel_path}")
    
    def _broadcast_dir_create(self, dirname: str, exclude_client: str = None):
        """广播创建目录（内部方法）"""
        message = Protocol.create_dir_create_message(
            os.path.join(self.sync_folder, dirname), self.sync_folder
        )
        self._broadcast_data(message, exclude_client)
    
    def broadcast_rename(self, old_path: str, new_path: str):
        """广播重命名"""
        old_rel = os.path.relpath(old_path, self.sync_folder).replace('\\', '/')
        new_rel = os.path.relpath(new_path, self.sync_folder).replace('\\', '/')
        self._broadcast_rename(old_rel, new_rel)
        self.log_message.emit(f"广播变更: {old_rel} -> {new_rel}")
    
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
        # 锁内只收集 socket 引用，避免锁内执行 IO 操作
        with self._lock:
            targets = [
                (client_id, client_info['socket'])
                for client_id, client_info in list(self.clients.items())
                if client_id != exclude_client
                and client_info['authenticated']
                and (failed_clients is None or client_id not in failed_clients)
            ]

        if not targets:
            return True

        # 锁外发送，避免阻塞其他线程
        for client_id, sock in targets:
            if stop_event and stop_event.is_set():
                return False
            try:
                if not self._send_with_cancel(sock, data, stop_event):
                    # stop_event 触发导致发送失败，中断广播
                    if stop_event and stop_event.is_set():
                        return False
                    # 连接异常导致发送失败，标记该客户端失败，后续不再发送
                    if failed_clients is not None:
                        failed_clients.add(client_id)
                    # 发送 FILE_CANCEL 清理接收端状态，避免残留临时文件
                    if cancel_filename:
                        self._send_with_cancel(sock, Protocol.create_file_cancel(cancel_filename), None)
                    self.log_message.emit(f"发送给 {client_id} 失败，已取消该客户端传输")
            except Exception as e:
                if failed_clients is not None:
                    failed_clients.add(client_id)
                if cancel_filename:
                    self._send_with_cancel(sock, Protocol.create_file_cancel(cancel_filename), None)
                self.log_message.emit(f"发送给 {client_id} 失败: {e}")
        return True
