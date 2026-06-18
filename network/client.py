# -*- coding: utf-8 -*-
"""
LANSyncBox TCP客户端模块（连接端）
"""

import socket
import threading
import time
from typing import Optional, Callable
from PyQt5.QtCore import QObject, pyqtSignal

from config import (
    BUFFER_SIZE, CONNECTION_TIMEOUT, HEARTBEAT_INTERVAL
)
from protocol import Protocol, MessageReceiver


class SyncClient(QObject):
    """同步客户端（连接端）"""
    
    # 信号定义
    connected = pyqtSignal()  # 连接成功
    disconnected = pyqtSignal()  # 断开连接
    auth_success = pyqtSignal()  # 验证成功
    auth_failed = pyqtSignal(str)  # 验证失败 (message)
    file_received = pyqtSignal(str, int)  # 文件接收 (filename, size)
    delete_received = pyqtSignal(str)  # 删除指令 (filename)
    file_list_received = pyqtSignal(list)  # 文件列表接收
    sync_progress = pyqtSignal(int, int)  # 同步进度 (current, total)
    log_message = pyqtSignal(str)  # 日志消息
    error_occurred = pyqtSignal(str)  # 错误消息
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket: Optional[socket.socket] = None
        self.connected_flag = False
        self.authenticated = False
        self.allow_peer_sync = False  # 主机端是否允许互相同步
        self.server_address = ""
        self.server_port = 9527
        self.room_code = ""
        self.password = ""
        self.sync_folder = ""
        self.hide_from_others = False
        
        self._receiver = MessageReceiver()
        self._running = False
        self._lock = threading.Lock()
    
    def connect_to_server(self, host: str, port: int, room_code: str,
                          password: str = "", sync_folder: str = "") -> bool:
        """
        连接到服务器
        Args:
            host: 服务器地址
            port: 端口号
            room_code: 房间号
            password: 密码
            sync_folder: 同步文件夹
        Returns:
            是否连接成功
        """
        try:
            self.server_address = host
            self.server_port = port
            self.room_code = room_code
            self.password = password
            self.sync_folder = sync_folder
            
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(CONNECTION_TIMEOUT)
            self.socket.connect((host, port))
            
            self.connected_flag = True
            self._running = True
            
            # 发送验证请求
            auth_msg = Protocol.create_auth_request(room_code, password)
            self.socket.sendall(auth_msg)
            
            # 启动接收线程
            receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            receive_thread.start()
            
            # 启动心跳线程
            heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            heartbeat_thread.start()
            
            self.connected.emit()
            self.log_message.emit(f"已连接到 {host}:{port}")
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"连接失败: {e}")
            return False
    
    def disconnect(self):
        """断开连接"""
        self._running = False
        self.connected_flag = False
        self.authenticated = False
        
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        
        self.disconnected.emit()
        self.log_message.emit("已断开连接")
    
    def send_file(self, filepath: str, hide_from_others: bool = False) -> bool:
        """
        发送文件到服务器
        Args:
            filepath: 文件路径
            hide_from_others: 是否对外隐藏
        Returns:
            是否发送成功
        """
        if not self.authenticated:
            self.error_occurred.emit("未验证，无法发送文件")
            return False
        
        try:
            message = Protocol.create_file_message(filepath, self.sync_folder, hide_from_others)
            with self._lock:
                self.socket.sendall(message)
            self.log_message.emit(f"发送文件: {filepath}")
            return True
        except Exception as e:
            self.error_occurred.emit(f"发送文件失败: {e}")
            return False
    
    def send_delete(self, filepath: str) -> bool:
        """
        发送删除指令到服务器
        Args:
            filepath: 文件路径
        Returns:
            是否发送成功
        """
        if not self.authenticated:
            self.error_occurred.emit("未验证，无法发送删除指令")
            return False
        
        try:
            message = Protocol.create_delete_message(filepath, self.sync_folder)
            with self._lock:
                self.socket.sendall(message)
            self.log_message.emit(f"发送删除指令: {filepath}")
            return True
        except Exception as e:
            self.error_occurred.emit(f"发送删除指令失败: {e}")
            return False
    
    def send_dir_create(self, dirpath: str) -> bool:
        """
        发送目录创建到服务器
        Args:
            dirpath: 目录路径
        Returns:
            是否发送成功
        """
        print(f"[DEBUG] send_dir_create called: {dirpath}")
        
        if not self.authenticated:
            self.error_occurred.emit("未验证，无法发送目录创建")
            return False
        
        try:
            message = Protocol.create_dir_create_message(dirpath, self.sync_folder)
            with self._lock:
                self.socket.sendall(message)
            self.log_message.emit(f"发送目录创建: {dirpath}")
            return True
        except Exception as e:
            self.error_occurred.emit(f"发送目录创建失败: {e}")
            return False
    
    def request_file_list(self):
        """请求文件列表"""
        if not self.authenticated:
            self.error_occurred.emit("未验证，无法请求文件列表")
            return
        
        try:
            message = Protocol.create_file_list_request()
            with self._lock:
                self.socket.sendall(message)
            self.log_message.emit("请求文件列表")
        except Exception as e:
            self.error_occurred.emit(f"请求文件列表失败: {e}")
    
    def request_full_sync(self):
        """请求全量同步"""
        if not self.authenticated:
            self.error_occurred.emit("未验证，无法请求全量同步")
            return
        
        try:
            message = Protocol.create_full_sync_request()
            with self._lock:
                self.socket.sendall(message)
            self.log_message.emit("请求全量同步")
        except Exception as e:
            self.error_occurred.emit(f"请求全量同步失败: {e}")
    
    def update_hide_status(self, hide_from_others: bool):
        """更新隐藏状态"""
        if not self.authenticated:
            return
        
        self.hide_from_others = hide_from_others
        try:
            message = Protocol.create_client_info_update(hide_from_others)
            with self._lock:
                self.socket.sendall(message)
        except Exception as e:
            self.error_occurred.emit(f"更新状态失败: {e}")
    
    def _receive_loop(self):
        """接收数据循环"""
        while self._running:
            try:
                # 使用较短的timeout以便定期检查运行状态
                self.socket.settimeout(5.0)
                data = self.socket.recv(BUFFER_SIZE)
                
                if not data:
                    # 服务器主动关闭连接
                    break
                
                self._receiver.feed(data)
                
                while self._receiver.has_complete_message():
                    message = self._receiver.get_message()
                    if message:
                        self._process_message(message)
                        
            except socket.timeout:
                # 超时继续循环，不断开连接
                continue
            except ConnectionResetError:
                if self._running:
                    self.error_occurred.emit("连接被服务器重置")
                break
            except ConnectionAbortedError:
                if self._running:
                    self.error_occurred.emit("连接被中止")
                break
            except Exception as e:
                if self._running:
                    self.error_occurred.emit(f"接收数据错误: {e}")
                break
        
        # 只有在非主动断开时才触发断开信号
        if self._running:
            self.disconnect()
    
    def _process_message(self, message: tuple):
        """处理服务器消息"""
        import os
        import json
        
        msg_type, filename, file_size, mtime, hide_flag, content = message
        print(f"[DEBUG] _process_message: type={msg_type}, filename={filename}, size={file_size}")
        
        if msg_type == 0x04:  # AUTH_RESP
            # 验证响应
            try:
                resp_data = content.decode('utf-8').split(':')
                success = resp_data[0] == '1'
                message_text = resp_data[1] if len(resp_data) > 1 else ''
                allow_peer_sync = resp_data[2] == '1' if len(resp_data) > 2 else False
                
                print(f"[DEBUG] AUTH_RESP: success={success}, allow_peer_sync={allow_peer_sync}")
                
                if success:
                    self.authenticated = True
                    self.allow_peer_sync = allow_peer_sync  # 保存主机端的设置
                    self.auth_success.emit()
                    self.log_message.emit(f"验证成功，互相同步: {'开启' if allow_peer_sync else '关闭'}")
                else:
                    self.auth_failed.emit(message_text)
                    self.disconnect()
                    
            except Exception as e:
                self.auth_failed.emit(f"验证响应解析失败: {e}")
                self.disconnect()
                
        elif msg_type == 0x01:  # FILE
            # 接收文件
            print(f"[DEBUG] FILE message received: {filename}, mtime={mtime}")
            self._handle_file_receive(filename, content, mtime)
            
        elif msg_type == 0x02:  # DELETE
            # 处理删除指令
            print(f"[DEBUG] DELETE message received: {filename}")
            self._handle_delete_receive(filename)
            
        elif msg_type == 0x0B:  # DIR_CREATE
            # 处理目录创建
            print(f"[DEBUG] DIR_CREATE message received: {filename}")
            self._handle_dir_create_receive(filename)
            
        elif msg_type == 0x06:  # FILE_LIST_RESP
            # 文件列表响应
            try:
                file_list = json.loads(content.decode('utf-8'))
                self.file_list_received.emit(file_list)
            except Exception as e:
                self.error_occurred.emit(f"解析文件列表失败: {e}")
    
    def _handle_file_receive(self, filename: str, content: bytes, mtime: float):
        """处理接收到的文件"""
        import os
        
        print(f"[DEBUG] _handle_file_receive: {filename}, size={len(content)}, mtime={mtime}")
        
        try:
            filepath = os.path.join(self.sync_folder, filename)
            print(f"[DEBUG] Saving to: {filepath}")
            
            # 检查本地是否存在同名文件，比较时间戳
            if os.path.exists(filepath):
                local_mtime = os.path.getmtime(filepath)
                # 如果本地文件更新，跳过接收
                if local_mtime > mtime:
                    print(f"[DEBUG] Local file is newer, skip: {filename} (local={local_mtime}, remote={mtime})")
                    self.log_message.emit(f"跳过文件: {filename} (本地文件更新)")
                    return
                # 如果时间戳相同（误差1秒内），也跳过避免重复同步
                if abs(local_mtime - mtime) < 1.0:
                    print(f"[DEBUG] File timestamps are equal, skip: {filename}")
                    return
            
            dir_path = os.path.dirname(filepath)
            if dir_path:  # 只有当目录路径非空时才创建
                os.makedirs(dir_path, exist_ok=True)
            
            with open(filepath, 'wb') as f:
                f.write(content)
            
            # 设置文件的修改时间为原始时间
            os.utime(filepath, (mtime, mtime))
            
            print(f"[DEBUG] File saved successfully: {filepath}")
            self.file_received.emit(filename, len(content))
            self.log_message.emit(f"接收文件: {filename}")
            
        except Exception as e:
            print(f"[DEBUG] Save file error: {e}")
            self.error_occurred.emit(f"保存文件失败 {filename}: {e}")
    
    def _handle_delete_receive(self, filename: str):
        """处理接收到的删除指令"""
        import os
        import shutil
        
        try:
            filepath = os.path.join(self.sync_folder, filename)
            if os.path.exists(filepath):
                if os.path.isdir(filepath):
                    shutil.rmtree(filepath)  # 删除目录
                else:
                    os.remove(filepath)  # 删除文件
            
            self.delete_received.emit(filename)
            self.log_message.emit(f"删除文件: {filename}")
            
        except Exception as e:
            self.error_occurred.emit(f"删除文件失败 {filename}: {e}")
    
    def _handle_dir_create_receive(self, dirname: str):
        """处理接收到的目录创建"""
        import os
        
        print(f"[DEBUG] _handle_dir_create_receive: {dirname}")
        
        try:
            dirpath = os.path.join(self.sync_folder, dirname)
            print(f"[DEBUG] Creating directory: {dirpath}")
            
            if not os.path.exists(dirpath):
                os.makedirs(dirpath, exist_ok=True)
                print(f"[DEBUG] Directory created: {dirpath}")
            
            self.log_message.emit(f"创建目录: {dirname}")
            
        except Exception as e:
            print(f"[DEBUG] Create directory error: {e}")
            self.error_occurred.emit(f"创建目录失败 {dirname}: {e}")
    
    def _heartbeat_loop(self):
        """心跳发送循环"""
        while self._running and self.connected_flag:
            try:
                message = Protocol.create_heartbeat()
                with self._lock:
                    if self.socket:
                        self.socket.sendall(message)
            except Exception:
                pass
            
            time.sleep(HEARTBEAT_INTERVAL)