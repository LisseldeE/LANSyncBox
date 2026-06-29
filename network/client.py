"""
同步客户端
连接端运行，连接服务器并发送/接收文件
"""
import socket
import threading
import os
import time
from typing import Optional
from PySide6.QtCore import QObject, Signal

from config import Config
from network.protocol import Protocol, MessageType, MessageReceiver
from utils.transfer_queue import TransferQueue


class SyncClient(QObject):
    """同步客户端"""
    
    # 信号
    connected = Signal()              # 连接成功
    disconnected = Signal()           # 断开连接
    error_occurred = Signal(str)      # 错误
    auth_failed = Signal(str)         # 验证失败
    file_received = Signal(str)       # 收到文件
    file_receive_start = Signal(str)  # 开始接收文件
    file_receive_progress = Signal(str, int, int)  # 文件接收进度 (filename, current, total)
    file_receive_cancelled = Signal(str)  # 文件接收被取消
    file_deleted = Signal(str)        # 文件已删除
    file_renamed = Signal(str, str)   # 文件已重命名 (old_name, new_name)
    dir_created = Signal(str)         # 目录已创建
    file_sent = Signal(str)           # 发送文件完成
    file_send_progress = Signal(str, int, int)     # 文件发送进度 (filename, current, total)
    log_message = Signal(str)         # 日志消息
    file_list_received = Signal(list) # 收到文件列表
    
    # 大文件阈值（1MB）
    LARGE_FILE_THRESHOLD = 1024 * 1024
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
        self.sync_folder = Config.get_room_folder(room_code)
        self.receiving_file = None
        self.receiving_file_handle = None  # 大文件句柄
        
        # 创建传输队列，控制并发传输数量
        self.transfer_queue = TransferQueue(max_concurrent=3)
    
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
            
            self.running = True
            
            # 发送验证请求
            auth_msg = Protocol.create_auth_request(self.room_code, self.password)
            self.socket.sendall(auth_msg)
            
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
        
        # 清理大文件接收状态：关闭句柄、删除临时文件
        if self.receiving_file_handle:
            try:
                self.receiving_file_handle.close()
            except Exception:
                pass
            self.receiving_file_handle = None
        if self.receiving_file:
            temp_path = self.receiving_file.get('temp_path')
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            self.receiving_file = None
        
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None
    
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
        
        elif msg_type == MessageType.FILE:
            # 小文件一次性传输
            self._receive_file(filename, content, mtime)
        
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
                self.receiving_file = {
                    'filename': filename,
                    'file_size': file_size,
                    'mtime': mtime,
                    'received_size': 0,
                    'temp_path': temp_file_path  # 记录临时文件路径
                }
                self.receiving_file_handle = file_handle
                self.log_message.emit(f"开始接收大文件: {filename} ({self._format_size(file_size)})")
                
                # 发射开始接收信号
                self.file_receive_start.emit(filename)
            except Exception as e:
                self.log_message.emit(f"创建文件失败: {e}")
        
        elif msg_type == MessageType.FILE_DATA:
            # 大文件数据块 - 流式写入
            if self.receiving_file and self.receiving_file_handle:
                chunk_index, chunk_data = content
                try:
                    # 用 chunk_index 定位写入，避免乱序/重复/丢失导致文件损坏
                    offset = chunk_index * self.CHUNK_SIZE
                    self.receiving_file_handle.seek(offset)
                    self.receiving_file_handle.write(chunk_data)
                    self.receiving_file['received_size'] += len(chunk_data)

                    # 发送进度信号（转换为KB避免溢出）
                    rf = self.receiving_file
                    received_kb = rf['received_size'] // 1024
                    total_kb = rf['file_size'] // 1024
                    self.file_receive_progress.emit(
                        rf['filename'],
                        received_kb,
                        total_kb
                    )
                except Exception as e:
                    self.log_message.emit(f"写入数据块失败: {e}")
        
        elif msg_type == MessageType.FILE_END:
            # 大文件传输结束 - 重命名临时文件为正式文件
            if self.receiving_file and self.receiving_file_handle:
                rf = self.receiving_file
                file_handle = self.receiving_file_handle

                # 关闭文件句柄
                try:
                    file_handle.close()
                except Exception:
                    pass

                # 校验完整性：received_size 可能因重复写入而偏大，用实际文件大小校验
                temp_file_path = rf['temp_path']
                actual_size = os.path.getsize(temp_file_path) if os.path.exists(temp_file_path) else 0
                if actual_size != rf['file_size']:
                    self.log_message.emit(
                        f"大文件接收不完整: {rf['filename']} "
                        f"(实际 {actual_size}/期望 {rf['file_size']})，丢弃"
                    )
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    self.receiving_file = None
                    self.receiving_file_handle = None
                    return

                # 重命名临时文件为正式文件
                try:
                    final_file_path = self._safe_join(rf['filename'])
                except ValueError as e:
                    self.log_message.emit(f"拒绝非法路径: {e}")
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    self.receiving_file = None
                    self.receiving_file_handle = None
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
                    self.log_message.emit(f"大文件接收完成: {rf['filename']}")
                    self.file_received.emit(rf['filename'])
                    
                except Exception as e:
                    self.log_message.emit(f"重命名文件失败: {e}")
                    # 删除临时文件
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                
                # 清理状态
                self.receiving_file = None
                self.receiving_file_handle = None
        
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
            else:
                self.log_message.emit(f"验证失败: {message}")
                # 发射验证失败信号
                self.auth_failed.emit(message)
                self.disconnect()
                
        except Exception as e:
            self.log_message.emit(f"验证响应解析错误: {e}")
            self.auth_failed.emit(f"验证响应解析错误: {e}")
            self.disconnect()
    
    def _receive_file(self, filename: str, content: bytes, mtime: float):
        """接收文件"""
        # 发送开始接收信号（用于标记同步）
        self.file_receive_start.emit(filename)
        
        # 保存到本地（校验路径安全性）
        try:
            file_path = self._safe_join(filename)
        except ValueError as e:
            self.log_message.emit(f"拒绝非法路径: {e}")
            return
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        with open(file_path, 'wb') as f:
            f.write(content)
        
        os.utime(file_path, (mtime, mtime))
        
        self.log_message.emit(f"收到文件: {filename}")
        self.file_received.emit(filename)
    
    def _handle_delete(self, filename: str):
        """处理删除指令"""
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
        """处理重命名"""
        try:
            data = content.decode('utf-8').split('|')
            old_name = data[0]
            new_name = data[1]
            
            old_path = self._safe_join(old_name)
            new_path = self._safe_join(new_name)
            
            os.rename(old_path, new_path)
            
            self.log_message.emit(f"重命名: {old_name} -> {new_name}")
            
            # 发射重命名信号
            self.file_renamed.emit(old_name, new_name)
            
        except Exception as e:
            self.log_message.emit(f"重命名失败: {e}")
    
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
        """发送文件给主机端
        
        Args:
            filename: 文件名（相对路径）
            file_path: 文件绝对路径
            stop_event: 停止标志（可选）
        """
        try:
            file_size = os.path.getsize(file_path)
            mtime = os.path.getmtime(file_path)
            
            self.log_message.emit(f"发送文件给主机端: {filename} ({self._format_size(file_size)})")
            
            # 选择传输方式
            if file_size > self.LARGE_FILE_THRESHOLD:
                # 大文件：流式分块传输
                self._send_large_file_to_server(filename, file_path, file_size, mtime, stop_event)
            else:
                # 小文件：一次性传输
                # 检查是否需要停止
                if stop_event and stop_event.is_set():
                    return
                
                with open(file_path, 'rb') as f:
                    content = f.read()
                message = Protocol.pack_message(
                    MessageType.FILE, filename, file_size, False, content, mtime
                )
                if not self._send_with_cancel(self.socket, message, stop_event):
                    # 被取消或连接异常，通知接收端清理
                    try:
                        self.socket.sendall(Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    self.log_message.emit(f"取消发送文件: {filename}")
                    return
            
            self.log_message.emit(f"文件发送完成: {filename}")
            
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
            if not self._send_with_cancel(self.socket, begin_msg, stop_event):
                # 被取消或连接异常，通知接收端清理
                try:
                    self.socket.sendall(Protocol.create_file_cancel(filename))
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
                            self.socket.sendall(Protocol.create_file_cancel(filename))
                        except Exception:
                            pass
                        self.log_message.emit(f"取消发送文件: {filename}")
                        return
                    
                    chunk = f.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                    if not self._send_with_cancel(self.socket, chunk_msg, stop_event):
                        # 被取消或连接异常，通知接收端清理
                        try:
                            self.socket.sendall(Protocol.create_file_cancel(filename))
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
            if not self._send_with_cancel(self.socket, end_msg, stop_event):
                try:
                    self.socket.sendall(Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送文件: {filename}")
                return

            self.log_message.emit(f"大文件发送完成: {filename}")
            
        except Exception as e:
            self.log_message.emit(f"发送大文件失败: {e}")
    
    def _handle_file_cancel(self, filename: str):
        """处理文件传输取消"""
        try:
            # 如果正在接收该文件，停止接收
            if self.receiving_file:
                if self.receiving_file['filename'] == filename:
                    # 关闭文件句柄
                    if self.receiving_file_handle:
                        try:
                            self.receiving_file_handle.close()
                        except Exception:
                            pass
                    
                    # 删除临时文件
                    temp_file_path = self.receiving_file['temp_path']
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    
                    # 清理状态
                    self.receiving_file = None
                    self.receiving_file_handle = None

                    # 通知 UI 清理接收进度条
                    self.file_receive_cancelled.emit(filename)
                    self.log_message.emit(f"取消接收文件: {filename}")
            
        except Exception as e:
            self.log_message.emit(f"取消文件传输失败: {e}")
    
    # ========== 发送方法 ==========
    
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
            
            self.log_message.emit(f"发送文件: {rel_path} ({self._format_size(file_size)})")
            
            # 选择传输方式
            if file_size > self.LARGE_FILE_THRESHOLD:
                # 大文件：流式分块传输
                self._send_large_file_streaming(rel_path, filepath, file_size, mtime, stop_event)
            else:
                # 小文件：一次性传输
                with open(filepath, 'rb') as f:
                    content = f.read()
                self._send_file(rel_path, content, mtime, stop_event)
            
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
        if not self._send_with_cancel(self.socket, begin_msg, stop_event):
            # 被取消或连接异常，通知接收端清理
            try:
                self.socket.sendall(Protocol.create_file_cancel(filename))
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
                        self.socket.sendall(Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    self.log_message.emit(f"取消发送文件: {filename}")
                    return
                
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                
                chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                if not self._send_with_cancel(self.socket, chunk_msg, stop_event):
                    # 被取消或连接异常，通知接收端清理
                    try:
                        self.socket.sendall(Protocol.create_file_cancel(filename))
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
        if not self._send_with_cancel(self.socket, end_msg, stop_event):
            try:
                self.socket.sendall(Protocol.create_file_cancel(filename))
            except Exception:
                pass
            self.log_message.emit(f"取消发送文件: {filename}")
            return
        
        # 发射发送完成信号
        self.file_sent.emit(filename)
        self.log_message.emit(f"大文件发送完成: {filename}")
    
    def _format_size(self, size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    
    def _send_file(self, filename: str, content: bytes, mtime: float, stop_event: threading.Event = None):
        """发送文件（内部方法）"""
        file_size = len(content)
        
        # 选择传输方式
        if file_size > self.LARGE_FILE_THRESHOLD:
            # 大文件分块传输
            message = Protocol.pack_message(
                MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
            )
            if not self._send_with_cancel(self.socket, message, stop_event):
                try:
                    self.socket.sendall(Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送文件: {filename}")
                return
            
            # 发送数据块
            sent_size = 0
            for i in range(0, file_size, self.CHUNK_SIZE):
                if stop_event and stop_event.is_set():
                    try:
                        self.socket.sendall(Protocol.create_file_cancel(filename))
                    except Exception:
                        pass
                    self.log_message.emit(f"取消发送文件: {filename}")
                    return
                chunk = content[i:i + self.CHUNK_SIZE]
                chunk_msg = Protocol.create_file_data_message(filename, i // self.CHUNK_SIZE, chunk)
                if not self._send_with_cancel(self.socket, chunk_msg, stop_event):
                    try:
                        self.socket.sendall(Protocol.create_file_cancel(filename))
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
            if not self._send_with_cancel(self.socket, end_msg, stop_event):
                try:
                    self.socket.sendall(Protocol.create_file_cancel(filename))
                except Exception:
                    pass
                self.log_message.emit(f"取消发送文件: {filename}")
                return
        else:
            # 小文件一次性传输
            message = Protocol.pack_message(
                MessageType.FILE, filename, file_size, False, content, mtime
            )
            if not self._send_with_cancel(self.socket, message, stop_event):
                try:
                    self.socket.sendall(Protocol.create_file_cancel(filename))
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
        self.socket.sendall(message)
        self.log_message.emit(f"发送删除指令: {rel_path}")
    
    def send_dir_create(self, dirpath: str):
        """发送创建目录指令"""
        if not self.authenticated:
            return
        
        rel_path = os.path.relpath(dirpath, self.sync_folder).replace('\\', '/')
        message = Protocol.create_dir_create_message(dirpath, self.sync_folder)
        self.socket.sendall(message)
        self.log_message.emit(f"发送创建目录指令: {rel_path}")
    
    def send_rename(self, old_path: str, new_path: str):
        """发送重命名指令"""
        if not self.authenticated:
            return
        
        message = Protocol.create_rename_message(old_path, new_path, self.sync_folder)
        self.socket.sendall(message)
        
        old_rel = os.path.relpath(old_path, self.sync_folder).replace('\\', '/')
        new_rel = os.path.relpath(new_path, self.sync_folder).replace('\\', '/')
        self.log_message.emit(f"发送重命名指令: {old_rel} -> {new_rel}")
    
    def request_file_list(self):
        """请求文件列表"""
        if not self.authenticated:
            return
        
        message = Protocol.create_file_list_request()
        self.socket.sendall(message)
        self.log_message.emit("请求文件列表")
    
    def request_file(self, filename: str):
        """请求特定文件"""
        if not self.authenticated:
            return
        
        message = Protocol.create_file_request(filename)
        self.socket.sendall(message)
        self.log_message.emit(f"请求文件: {filename}")
    
    def send_file_cancel(self, filename: str):
        """发送文件取消传输指令"""
        if not self.authenticated:
            return
        
        message = Protocol.create_file_cancel(filename)
        self.socket.sendall(message)
        self.log_message.emit(f"发送取消传输指令: {filename}")
