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


class SyncClient(QObject):
    """同步客户端"""
    
    # 信号
    connected = Signal()              # 连接成功
    disconnected = Signal()           # 断开连接
    error_occurred = Signal(str)      # 错误
    file_received = Signal(str)       # 收到文件
    file_receive_start = Signal(str)  # 开始接收文件
    file_receive_progress = Signal(str, int, int)  # 文件接收进度 (filename, current, total)
    file_deleted = Signal(str)        # 文件已删除
    file_sent = Signal(str)           # 发送文件完成
    file_send_progress = Signal(str, int, int)     # 文件发送进度 (filename, current, total)
    log_message = Signal(str)         # 日志消息
    
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
    
    def connect_to_server(self, host: str, port: int = None) -> bool:
        """连接到服务器"""
        port = port or Config.DEFAULT_PORT
        
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
            # 大文件传输开始
            file_path = os.path.join(self.sync_folder, filename)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # 创建文件句柄，准备流式写入
            try:
                file_handle = open(file_path, 'wb')
                self.receiving_file = {
                    'filename': filename,
                    'file_size': file_size,
                    'mtime': mtime,
                    'received_size': 0
                }
                self.receiving_file_handle = file_handle
                self.log_message.emit(f"开始接收大文件: {filename} ({self._format_size(file_size)})")
            except Exception as e:
                self.log_message.emit(f"创建文件失败: {e}")
        
        elif msg_type == MessageType.FILE_DATA:
            # 大文件数据块 - 流式写入
            if self.receiving_file and self.receiving_file_handle:
                chunk_index, chunk_data = content
                try:
                    self.receiving_file_handle.write(chunk_data)
                    self.receiving_file['received_size'] += len(chunk_data)
                    
                    # 发送进度信号
                    self.file_receive_progress.emit(
                        self.receiving_file['filename'], 
                        self.receiving_file['received_size'], 
                        self.receiving_file['file_size']
                    )
                except Exception as e:
                    self.log_message.emit(f"写入数据块失败: {e}")
        
        elif msg_type == MessageType.FILE_END:
            # 大文件传输结束
            if self.receiving_file and self.receiving_file_handle:
                rf = self.receiving_file
                file_handle = self.receiving_file_handle
                
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
                
                # 清理状态
                self.receiving_file = None
                self.receiving_file_handle = None
        
        elif msg_type == MessageType.DELETE:
            self._handle_delete(filename)
        
        elif msg_type == MessageType.DIR_CREATE:
            self._handle_dir_create(filename)
        
        elif msg_type == 0x05:  # RENAME
            self._handle_rename(content)
    
    def _handle_auth_response(self, content: bytes):
        """处理验证响应"""
        try:
            data = content.decode('utf-8').split(':')
            success = data[0] == '1'
            message = data[1] if len(data) > 1 else ''
            
            if success:
                self.authenticated = True
                self.connected.emit()
                self.log_message.emit("验证成功")
            else:
                self.log_message.emit(f"验证失败: {message}")
                self.disconnect()
                
        except Exception as e:
            self.log_message.emit(f"验证响应解析错误: {e}")
            self.disconnect()
    
    def _receive_file(self, filename: str, content: bytes, mtime: float):
        """接收文件"""
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
    
    def _handle_delete(self, filename: str):
        """处理删除指令"""
        file_path = os.path.join(self.sync_folder, filename)
        
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                import shutil
                shutil.rmtree(file_path)
            
            self.log_message.emit(f"删除: {filename}")
            self.file_deleted.emit(filename)
            
        except Exception as e:
            self.log_message.emit(f"删除失败: {e}")
    
    def _handle_dir_create(self, dirname: str):
        """处理目录创建"""
        dir_path = os.path.join(self.sync_folder, dirname)
        os.makedirs(dir_path, exist_ok=True)
        self.log_message.emit(f"创建目录: {dirname}")
    
    def _handle_rename(self, content: bytes):
        """处理重命名"""
        try:
            data = content.decode('utf-8').split('|')
            old_name = data[0]
            new_name = data[1]
            
            old_path = os.path.join(self.sync_folder, old_name)
            new_path = os.path.join(self.sync_folder, new_name)
            
            os.rename(old_path, new_path)
            
            self.log_message.emit(f"重命名: {old_name} -> {new_name}")
            
        except Exception as e:
            self.log_message.emit(f"重命名失败: {e}")
    
    # ========== 发送方法 ==========
    
    def send_file(self, filepath: str):
        """
        发送文件给服务器（连接端本地添加文件时调用）
        这是连接端添加文件时的同步入口
        
        使用流式传输，避免大文件占用过多内存
        """
        if not self.authenticated:
            self.log_message.emit("未连接，无法发送文件")
            return
        
        try:
            file_size = os.path.getsize(filepath)
            mtime = os.path.getmtime(filepath)
            rel_path = os.path.relpath(filepath, self.sync_folder).replace('\\', '/')
            
            self.log_message.emit(f"发送文件: {rel_path} ({self._format_size(file_size)})")
            
            # 选择传输方式
            if file_size > self.LARGE_FILE_THRESHOLD:
                # 大文件：流式分块传输
                self._send_large_file_streaming(rel_path, filepath, file_size, mtime)
            else:
                # 小文件：一次性传输
                with open(filepath, 'rb') as f:
                    content = f.read()
                self._send_file(rel_path, content, mtime)
            
        except Exception as e:
            self.log_message.emit(f"发送文件失败: {e}")
    
    def _send_large_file_streaming(self, filename: str, filepath: str, file_size: int, mtime: float):
        """
        流式发送大文件
        边读边发送，避免一次性占用大量内存
        """
        # 发送文件开始消息
        begin_msg = Protocol.pack_message(
            MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
        )
        self.socket.sendall(begin_msg)
        
        # 流式读取并发送数据块
        chunk_index = 0
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                
                chunk_msg = Protocol.create_file_data_message(filename, chunk_index, chunk)
                self.socket.sendall(chunk_msg)
                chunk_index += 1
        
        # 发送文件结束消息
        end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
        self.socket.sendall(end_msg)
        
        self.log_message.emit(f"大文件发送完成: {filename}")
    
    def _format_size(self, size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    
    def _send_file(self, filename: str, content: bytes, mtime: float):
        """发送文件（内部方法）"""
        file_size = len(content)
        
        # 选择传输方式
        if file_size > self.LARGE_FILE_THRESHOLD:
            # 大文件分块传输
            message = Protocol.pack_message(
                MessageType.FILE_BEGIN, filename, file_size, False, b'', mtime
            )
            self.socket.sendall(message)
            
            # 发送数据块
            for i in range(0, file_size, self.CHUNK_SIZE):
                chunk = content[i:i + self.CHUNK_SIZE]
                chunk_msg = Protocol.create_file_data_message(filename, i // self.CHUNK_SIZE, chunk)
                self.socket.sendall(chunk_msg)
            
            # 发送结束标记
            end_msg = Protocol.create_file_end_message(filename, file_size, mtime)
            self.socket.sendall(end_msg)
        else:
            # 小文件一次性传输
            message = Protocol.pack_message(
                MessageType.FILE, filename, file_size, False, content, mtime
            )
            self.socket.sendall(message)
    
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
