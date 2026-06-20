"""
同步服务器
主机端运行，接收连接并转发文件
"""
import socket
import threading
import os
import time
from typing import Dict, Optional
from PySide6.QtCore import QObject, Signal

from config import Config
from network.protocol import Protocol, MessageType, MessageReceiver


class SyncServer(QObject):
    """同步服务器"""
    
    # 信号
    client_connected = Signal(str)       # 客户端连接
    client_disconnected = Signal(str)    # 客户端断开
    error_occurred = Signal(str)         # 错误
    file_received = Signal(str)          # 收到文件
    file_receive_start = Signal(str)     # 开始接收文件
    file_receive_progress = Signal(str, int, int)  # 文件接收进度 (filename, current, total)
    file_deleted = Signal(str)           # 文件已删除
    file_renamed = Signal(str, str)      # 文件已重命名 (old_name, new_name)
    dir_created = Signal(str)            # 目录已创建
    file_sent = Signal(str)              # 发送文件完成
    file_send_progress = Signal(str, int, int)     # 文件发送进度 (filename, current, total)
    log_message = Signal(str)            # 日志消息
    
    # 大文件阈值（1MB）
    LARGE_FILE_THRESHOLD = 1024 * 1024
    # 数据块大小（64KB）
    CHUNK_SIZE = 64 * 1024
    
    def __init__(self, room_code: str, password: str = "", parent=None):
        super().__init__(parent)
        self.room_code = room_code
        self.password = password
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self.clients: Dict[str, dict] = {}  # {client_id: {socket, receiver, thread}}
        self.sync_folder = Config.get_room_folder(room_code)
        self._lock = threading.Lock()
    
    def start(self, port: int = None) -> bool:
        """启动服务器"""
        port = port or Config.DEFAULT_PORT
        
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(('0.0.0.0', port))
            self.server_socket.listen(10)
            self.server_socket.settimeout(1.0)
            
            self.running = True
            
            # 启动接受连接线程
            accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            accept_thread.start()
            
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"启动服务器失败: {e}")
            return False
    
    def stop(self):
        """停止服务器"""
        self.running = False
        
        # 关闭所有客户端连接
        with self._lock:
            for client_id, client_info in list(self.clients.items()):
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
    
    def _accept_loop(self):
        """接受连接循环"""
        while self.running:
            try:
                client_socket, addr = self.server_socket.accept()
                client_id = f"{addr[0]}:{addr[1]}"
                
                self.log_message.emit(f"客户端连接: {client_id}")
                
                # 创建客户端信息
                with self._lock:
                    self.clients[client_id] = {
                        'socket': client_socket,
                        'receiver': MessageReceiver(),
                        'authenticated': False,
                        'receiving_file': None,  # 大文件接收状态
                        'receiving_file_handle': None  # 大文件句柄
                    }
                
                # 启动客户端处理线程
                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_id,),
                    daemon=True
                )
                client_thread.start()
                
                self.client_connected.emit(client_id)
                
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.error_occurred.emit(f"接受连接错误: {e}")
    
    def _handle_client(self, client_id: str):
        """处理客户端"""
        client_socket = self.clients[client_id]['socket']
        receiver = self.clients[client_id]['receiver']
        
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
        
        elif msg_type == MessageType.FILE:
            # 小文件一次性传输
            self._receive_file(client_id, filename, content, mtime)
        
        elif msg_type == MessageType.FILE_BEGIN:
            # 大文件传输开始
            file_path = os.path.join(self.sync_folder, filename)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # 创建文件句柄，准备流式写入
            try:
                file_handle = open(file_path, 'wb')
                client_info['receiving_file'] = {
                    'filename': filename,
                    'file_size': file_size,
                    'mtime': mtime,
                    'received_size': 0
                }
                client_info['receiving_file_handle'] = file_handle
                self.log_message.emit(f"开始接收大文件: {filename} ({self._format_size(file_size)})")
                
                # 发射开始接收信号
                self.file_receive_start.emit(filename)
            except Exception as e:
                self.log_message.emit(f"创建文件失败: {e}")
        
        elif msg_type == MessageType.FILE_DATA:
            # 大文件数据块 - 流式写入
            if client_info['receiving_file'] and client_info['receiving_file_handle']:
                chunk_index, chunk_data = content
                try:
                    client_info['receiving_file_handle'].write(chunk_data)
                    client_info['receiving_file']['received_size'] += len(chunk_data)
                    
                    # 发送进度信号（转换为MB避免溢出）
                    rf = client_info['receiving_file']
                    # 将字节转换为KB，避免大文件溢出
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
            # 大文件传输结束
            if client_info['receiving_file'] and client_info['receiving_file_handle']:
                rf = client_info['receiving_file']
                file_handle = client_info['receiving_file_handle']
                
                # 关闭文件句柄
                try:
                    file_handle.close()
                except Exception:
                    pass
                
                # 设置修改时间
                file_path = os.path.join(self.sync_folder, rf['filename'])
                try:
                    os.utime(file_path, (rf['mtime'], rf['mtime']))
                except Exception:
                    pass
                
                # 通知接收完成
                self.log_message.emit(f"大文件接收完成: {rf['filename']}")
                self.file_received.emit(rf['filename'])
                
                # 转发给其他客户端
                # 注意：大文件需要重新读取并发送（或使用缓存文件）
                self._broadcast_existing_file(rf['filename'], exclude_client=client_id)
                
                # 清理状态
                client_info['receiving_file'] = None
                client_info['receiving_file_handle'] = None
        
        elif msg_type == MessageType.DELETE:
            self._handle_delete(client_id, filename)
        
        elif msg_type == MessageType.DIR_CREATE:
            self._handle_dir_create(client_id, filename)
        
        elif msg_type == 0x05:  # RENAME
            self._handle_rename(client_id, content)
    
    def _handle_auth(self, client_id: str, content: bytes):
        """处理验证请求"""
        try:
            data = content.decode('utf-8').split(':')
            room_code = data[0]
            password_hash = data[1] if len(data) > 1 else ''
            
            import hashlib
            expected_hash = hashlib.sha256(self.password.encode()).hexdigest() if self.password else ''
            
            if room_code == self.room_code and password_hash == expected_hash:
                self.clients[client_id]['authenticated'] = True
                response = Protocol.create_auth_response(True, "验证成功")
                self.clients[client_id]['socket'].sendall(response)
                self.log_message.emit(f"客户端 {client_id} 验证成功")
            else:
                response = Protocol.create_auth_response(False, "验证失败")
                self.clients[client_id]['socket'].sendall(response)
                self._remove_client(client_id)
                
        except Exception as e:
            self.log_message.emit(f"验证错误: {e}")
            self._remove_client(client_id)
    
    def _receive_file(self, client_id: str, filename: str, content: bytes, mtime: float):
        """接收文件并转发给其他客户端"""
        # 发送开始接收信号（用于标记同步）
        self.file_receive_start.emit(filename)
        
        # 保存到本地
        file_path = os.path.join(self.sync_folder, filename)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        with open(file_path, 'wb') as f:
            f.write(content)
        
        os.utime(file_path, (mtime, mtime))
        
        self.log_message.emit(f"收到文件: {filename}")
        self.file_received.emit(filename)
        
        # 转发给其他客户端（排除发送者）
        self._broadcast_file(filename, content, mtime, exclude_client=client_id)
    
    def _handle_delete(self, client_id: str, filename: str):
        """处理删除请求"""
        file_path = os.path.join(self.sync_folder, filename)
        
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                import shutil
                shutil.rmtree(file_path)
            
            self.log_message.emit(f"删除: {filename}")
            self.file_deleted.emit(filename)
            
            # 转发给其他客户端
            self._broadcast_delete(filename, exclude_client=client_id)
            
        except Exception as e:
            self.log_message.emit(f"删除失败: {e}")
    
    def _handle_dir_create(self, client_id: str, dirname: str):
        """处理目录创建"""
        dir_path = os.path.join(self.sync_folder, dirname)
        os.makedirs(dir_path, exist_ok=True)
        
        self.log_message.emit(f"创建目录: {dirname}")
        
        # 发射目录创建信号
        self.dir_created.emit(dirname)
        
        # 转发给其他客户端
        self._broadcast_dir_create(dirname, exclude_client=client_id)
    
    def _handle_rename(self, client_id: str, content: bytes):
        """处理重命名"""
        try:
            data = content.decode('utf-8').split('|')
            old_name = data[0]
            new_name = data[1]
            
            old_path = os.path.join(self.sync_folder, old_name)
            new_path = os.path.join(self.sync_folder, new_name)
            
            os.rename(old_path, new_path)
            
            self.log_message.emit(f"重命名: {old_name} -> {new_name}")
            
            # 发射重命名信号
            self.file_renamed.emit(old_name, new_name)
            
            # 转发给其他客户端
            self._broadcast_rename(old_name, new_name, exclude_client=client_id)
            
        except Exception as e:
            self.log_message.emit(f"重命名失败: {e}")
    
    def _remove_client(self, client_id: str):
        """移除客户端"""
        with self._lock:
            if client_id in self.clients:
                try:
                    self.clients[client_id]['socket'].close()
                except Exception:
                    pass
                del self.clients[client_id]
        
        self.client_disconnected.emit(client_id)
    
    # ========== 广播方法 ==========
    
    def broadcast_file(self, filepath: str):
        """
        广播文件给所有客户端（主机端本地添加文件时调用）
        这是主机端添加文件时的同步入口
        
        使用流式传输，避免大文件占用过多内存
        """
        try:
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
            
            # 选择传输方式
            if file_size > self.LARGE_FILE_THRESHOLD:
                # 大文件：流式分块传输
                self._broadcast_large_file_streaming(rel_path, filepath, file_size, mtime)
            else:
                # 小文件：一次性传输
                with open(filepath, 'rb') as f:
                    content = f.read()
                self._broadcast_file(rel_path, content, mtime)
            
        except Exception as e:
            self.log_message.emit(f"广播文件失败: {e}")
    
    def _broadcast_large_file_streaming(self, filename: str, filepath: str, file_size: int, mtime: float):
        """
        流式广播大文件
        边读边发送，避免一次性占用大量内存
        """
        # 发送文件开始消息
        begin_msg = Protocol.pack_message(
            MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
        )
        self._broadcast_data(begin_msg)
        
        # 流式读取并发送数据块
        chunk_index = 0
        sent_size = 0
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                
                chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                self._broadcast_data(chunk_msg)
                chunk_index += 1
                sent_size += len(chunk)
                
                # 发射发送进度信号（转换为KB避免溢出）
                sent_kb = sent_size // 1024
                total_kb = file_size // 1024
                self.file_send_progress.emit(filename, sent_kb, total_kb)
        
        # 发送文件结束消息
        end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
        self._broadcast_data(end_msg)
        
        # 发射发送完成信号
        self.file_sent.emit(filename)
        self.log_message.emit(f"大文件发送完成: {filename}")
    
    def _broadcast_existing_file(self, filename: str, exclude_client: str = None):
        """
        广播已存在的文件（用于转发接收的大文件）
        从磁盘流式读取并发送
        """
        file_path = os.path.join(self.sync_folder, filename)
        
        if not os.path.exists(file_path):
            self.log_message.emit(f"文件不存在，无法转发: {filename}")
            return
        
        try:
            file_size = os.path.getsize(file_path)
            mtime = os.path.getmtime(file_path)
            
            self.log_message.emit(f"转发文件: {filename} ({self._format_size(file_size)})")
            
            # 使用流式传输
            if file_size > self.LARGE_FILE_THRESHOLD:
                # 大文件：流式分块传输
                # 发送文件开始消息
                begin_msg = Protocol.pack_message(
                    MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
                )
                self._broadcast_data(begin_msg, exclude_client)
                
                # 流式读取并发送数据块
                chunk_index = 0
                with open(file_path, 'rb') as f:
                    while True:
                        chunk = f.read(self.CHUNK_SIZE)
                        if not chunk:
                            break
                        
                        chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                        self._broadcast_data(chunk_msg, exclude_client)
                        chunk_index += 1
                
                # 发送文件结束消息
                end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
                self._broadcast_data(end_msg, exclude_client)
            else:
                # 小文件：一次性传输
                with open(file_path, 'rb') as f:
                    content = f.read()
                self._broadcast_file(filename, content, mtime, exclude_client)
            
        except Exception as e:
            self.log_message.emit(f"转发文件失败: {e}")
    
    def _format_size(self, size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    
    def _broadcast_file(self, filename: str, content: bytes, mtime: float, exclude_client: str = None):
        """广播文件给所有客户端（内部方法）"""
        file_size = len(content)
        
        # 选择传输方式
        if file_size > self.LARGE_FILE_THRESHOLD:
            # 大文件分块传输
            message = Protocol.pack_message(
                MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
            )
            self._broadcast_data(message, exclude_client)
            
            # 发送数据块
            sent_size = 0
            for i in range(0, file_size, self.CHUNK_SIZE):
                chunk = content[i:i + self.CHUNK_SIZE]
                chunk_msg = Protocol.create_file_data_message(filename, i // self.CHUNK_SIZE, chunk)
                self._broadcast_data(chunk_msg, exclude_client)
                sent_size += len(chunk)
                
                # 发射发送进度信号
                self.file_send_progress.emit(filename, sent_size, file_size)
            
            # 发送结束标记
            end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
            self._broadcast_data(end_msg, exclude_client)
            
            # 发射发送完成信号
            self.file_sent.emit(filename)
        else:
            # 小文件一次性传输
            message = Protocol.pack_message(
                MessageType.FILE, filename, file_size, False, content, mtime
            )
            self._broadcast_data(message, exclude_client)
            
            # 发射发送进度和完成信号
            self.file_send_progress.emit(filename, file_size, file_size)
            self.file_sent.emit(filename)
    
    def broadcast_delete(self, filepath: str):
        """
        广播删除指令（主机端本地删除文件时调用）
        """
        rel_path = os.path.relpath(filepath, self.sync_folder).replace('\\', '/')
        self._broadcast_delete(rel_path)
        self.log_message.emit(f"广播删除: {rel_path}")
    
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
        self.log_message.emit(f"广播重命名: {old_rel} -> {new_rel}")
    
    def _broadcast_rename(self, old_name: str, new_name: str, exclude_client: str = None):
        """广播重命名（内部方法）"""
        message = Protocol.create_rename_message(
            os.path.join(self.sync_folder, old_name),
            os.path.join(self.sync_folder, new_name),
            self.sync_folder
        )
        self._broadcast_data(message, exclude_client)
    
    def _broadcast_data(self, data: bytes, exclude_client: str = None):
        """广播数据给所有客户端"""
        with self._lock:
            for client_id, client_info in list(self.clients.items()):
                if client_id == exclude_client:
                    continue
                if not client_info['authenticated']:
                    continue
                
                try:
                    client_info['socket'].sendall(data)
                except Exception as e:
                    self.log_message.emit(f"发送给 {client_id} 失败: {e}")
