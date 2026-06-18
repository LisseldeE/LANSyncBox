# -*- coding: utf-8 -*-
"""
LANSyncBox TCP服务器模块（主机端）
"""

import socket
import threading
import hashlib
import json
from typing import Dict, Callable, Optional
from PyQt5.QtCore import QObject, pyqtSignal

from config import (
    BUFFER_SIZE, MAX_CONCURRENT_TRANSFERS, CONNECTION_TIMEOUT,
    HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT, DEFAULT_PORT
)
from protocol import Protocol, MessageReceiver


# UDP发现端口
DISCOVERY_PORT = 9528


class ClientConnection:
    """客户端连接信息"""
    
    def __init__(self, socket: socket.socket, address: tuple):
        self.socket = socket
        self.address = address
        self.client_id = f"{address[0]}:{address[1]}"
        self.hide_from_others = False  # 是否对外隐藏本机文件
        self.last_heartbeat = 0
        self.receiver = MessageReceiver()
        self.sync_folder = ""  # 客户端同步文件夹路径（仅记录用）
    
    def send(self, data: bytes) -> bool:
        """发送数据"""
        try:
            print(f"[DEBUG] ClientConnection.send: {len(data)} bytes to {self.client_id}")
            self.socket.sendall(data)
            print(f"[DEBUG] Send successful to {self.client_id}")
            return True
        except Exception as e:
            print(f"[DEBUG] Send failed to {self.client_id}: {e}")
            return False
    
    def close(self):
        """关闭连接"""
        try:
            self.socket.close()
        except Exception:
            pass


class SyncServer(QObject):
    """同步服务器（主机端）"""
    
    # 信号定义
    client_connected = pyqtSignal(str)  # 客户端连接 (client_id)
    client_disconnected = pyqtSignal(str)  # 客户端断开 (client_id)
    file_received = pyqtSignal(str, str, bool)  # 文件接收 (client_id, filename, hide_from_others)
    delete_received = pyqtSignal(str, str)  # 删除指令 (client_id, filename)
    log_message = pyqtSignal(str)  # 日志消息
    error_occurred = pyqtSignal(str)  # 错误消息
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server_socket: Optional[socket.socket] = None
        self.discovery_socket: Optional[socket.socket] = None  # UDP发现socket
        self.clients: Dict[str, ClientConnection] = {}
        self.running = False
        self.room_code = ""
        self.password_hash = ""
        self.sync_folder = ""
        self.allow_peer_sync = False
        self.port = DEFAULT_PORT
        
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(MAX_CONCURRENT_TRANSFERS)
    
    def start(self, room_code: str, password: str, sync_folder: str,
              port: int = DEFAULT_PORT, allow_peer_sync: bool = False) -> bool:
        """
        启动服务器
        """
        try:
            self.room_code = room_code
            self.password_hash = hashlib.sha256(password.encode()).hexdigest() if password else ""
            self.sync_folder = sync_folder
            self.allow_peer_sync = allow_peer_sync
            self.port = port
            
            # 启动TCP服务器
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(('0.0.0.0', port))
            self.server_socket.listen(10)
            
            # 启动UDP发现服务
            self.discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.discovery_socket.bind(('0.0.0.0', DISCOVERY_PORT))
            self.discovery_socket.settimeout(1.0)
            
            self.running = True
            
            # 启动接受连接线程
            accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            accept_thread.start()
            
            # 启动心跳检测线程
            heartbeat_thread = threading.Thread(target=self._heartbeat_check, daemon=True)
            heartbeat_thread.start()
            
            # 启动UDP发现响应线程
            discovery_thread = threading.Thread(target=self._discovery_loop, daemon=True)
            discovery_thread.start()
            
            self.log_message.emit(f"服务器启动成功，监听端口 {port}")
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"服务器启动失败: {e}")
            return False
    
    def stop(self):
        """停止服务器"""
        self.running = False
        
        # 关闭所有客户端连接
        with self._lock:
            for client in self.clients.values():
                client.close()
            self.clients.clear()
        
        # 关闭服务器socket
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        
        # 关闭UDP发现socket
        if self.discovery_socket:
            try:
                self.discovery_socket.close()
            except Exception:
                pass
        
        self.log_message.emit("服务器已停止")
    
    def broadcast_file(self, filepath: str, exclude_client: str = None,
                       hide_from_others: bool = False):
        """
        广播文件到所有客户端
        Args:
            filepath: 文件路径
            exclude_client: 排除的客户端ID
            hide_from_others: 是否对外隐藏
        """
        print(f"[DEBUG] broadcast_file called: {filepath}, exclude: {exclude_client}, hide: {hide_from_others}")
        print(f"[DEBUG] Current clients: {list(self.clients.keys())}")
        
        try:
            message = Protocol.create_file_message(filepath, self.sync_folder, hide_from_others)
            print(f"[DEBUG] Message created, size: {len(message)} bytes")
            
            with self._lock:
                sent_count = 0
                for client_id, client in self.clients.items():
                    if client_id == exclude_client:
                        print(f"[DEBUG] Skipping excluded client: {client_id}")
                        continue
                    if hide_from_others and not self.allow_peer_sync:
                        print(f"[DEBUG] Skipping due to hide_from_others: {client_id}")
                        continue
                    print(f"[DEBUG] Sending to client: {client_id}")
                    client.send(message)
                    sent_count += 1
                    
            print(f"[DEBUG] Sent to {sent_count} clients")
                    
        except Exception as e:
            print(f"[DEBUG] broadcast_file error: {e}")
            self.error_occurred.emit(f"广播文件失败: {e}")
    
    def broadcast_delete(self, filepath: str, exclude_client: str = None):
        """
        广播删除指令到所有客户端
        Args:
            filepath: 文件路径
            exclude_client: 排除的客户端ID
        """
        try:
            message = Protocol.create_delete_message(filepath, self.sync_folder)
            
            with self._lock:
                for client_id, client in self.clients.items():
                    if client_id == exclude_client:
                        continue
                    client.send(message)
                    
        except Exception as e:
            self.error_occurred.emit(f"广播删除指令失败: {e}")
    
    def broadcast_dir_create(self, dirpath: str, exclude_client: str = None):
        """
        广播目录创建到所有客户端
        Args:
            dirpath: 目录路径
            exclude_client: 排除的客户端ID
        """
        print(f"[DEBUG] broadcast_dir_create called: {dirpath}")
        try:
            message = Protocol.create_dir_create_message(dirpath, self.sync_folder)
            
            with self._lock:
                for client_id, client in self.clients.items():
                    if client_id == exclude_client:
                        continue
                    client.send(message)
                    
        except Exception as e:
            self.error_occurred.emit(f"广播目录创建失败: {e}")
    
    def send_to_client(self, client_id: str, data: bytes) -> bool:
        """发送数据到指定客户端"""
        with self._lock:
            client = self.clients.get(client_id)
            if client:
                return client.send(data)
        return False
    
    def _accept_loop(self):
        """接受连接循环"""
        while self.running:
            try:
                self.server_socket.settimeout(1.0)
                client_socket, address = self.server_socket.accept()
                
                client = ClientConnection(client_socket, address)
                
                # 启动客户端处理线程
                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(client,),
                    daemon=True
                )
                client_thread.start()
                
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.log_message.emit(f"接受连接错误: {e}")
    
    def _handle_client(self, client: ClientConnection):
        """处理客户端连接"""
        import time
        client.last_heartbeat = time.time()
        
        print(f"[DEBUG] _handle_client started for: {client.client_id}")
        
        try:
            while self.running:
                # 使用更长的超时时间，避免频繁断开
                client.socket.settimeout(CONNECTION_TIMEOUT)
                data = client.socket.recv(BUFFER_SIZE)
                
                if not data:
                    # 连接被客户端主动关闭
                    print(f"[DEBUG] Client {client.client_id} disconnected (no data)")
                    break
                
                print(f"[DEBUG] Received {len(data)} bytes from {client.client_id}")
                client.receiver.feed(data)
                
                while client.receiver.has_complete_message():
                    message = client.receiver.get_message()
                    if message:
                        self._process_message(client, message)
                        
        except socket.timeout:
            # 超时不一定断开，可能是空闲状态
            # 检查心跳时间决定是否断开
            current_time = time.time()
            if current_time - client.last_heartbeat > HEARTBEAT_TIMEOUT:
                print(f"[DEBUG] Client {client.client_id} heartbeat timeout")
                self.log_message.emit(f"客户端 {client.client_id} 心跳超时")
            else:
                print(f"[DEBUG] Client {client.client_id} idle timeout")
                self.log_message.emit(f"客户端 {client.client_id} 连接空闲超时")
        except ConnectionResetError:
            print(f"[DEBUG] Client {client.client_id} connection reset")
            self.log_message.emit(f"客户端 {client.client_id} 连接被重置")
        except ConnectionAbortedError:
            print(f"[DEBUG] Client {client.client_id} connection aborted")
            self.log_message.emit(f"客户端 {client.client_id} 连接被中止")
        except Exception as e:
            if self.running:
                print(f"[DEBUG] Client {client.client_id} error: {e}")
                self.log_message.emit(f"客户端 {client.client_id} 连接错误: {e}")
        finally:
            print(f"[DEBUG] Removing client: {client.client_id}")
            self._remove_client(client)
    
    def _process_message(self, client: ClientConnection, message: tuple):
        """处理客户端消息"""
        import time
        import json
        
        msg_type, filename, file_size, mtime, hide_flag, content = message
        print(f"[DEBUG] _process_message from {client.client_id}: type={msg_type}")
        
        if msg_type == 0x03:  # AUTH_REQ
            # 验证请求
            print(f"[DEBUG] AUTH_REQ received from {client.client_id}")
            try:
                auth_data = content.decode('utf-8').split(':')
                room_code = auth_data[0] if len(auth_data) > 0 else ''
                password_hash = auth_data[1] if len(auth_data) > 1 else ''
                
                print(f"[DEBUG] Auth data: room={room_code}, expected_room={self.room_code}")
                
                if room_code != self.room_code:
                    print(f"[DEBUG] Room code mismatch")
                    resp = Protocol.create_auth_response(False, "房间号错误")
                    client.send(resp)
                    return
                
                if self.password_hash and password_hash != self.password_hash:
                    print(f"[DEBUG] Password mismatch")
                    resp = Protocol.create_auth_response(False, "密码错误")
                    client.send(resp)
                    return
                
                # 验证成功，添加到客户端列表
                print(f"[DEBUG] Auth success, adding client to list")
                with self._lock:
                    self.clients[client.client_id] = client
                    print(f"[DEBUG] Current clients after auth: {list(self.clients.keys())}")
                
                # 发送验证成功响应，包含是否允许互相同步
                resp = Protocol.create_auth_response(True, "验证成功", self.allow_peer_sync)
                client.send(resp)
                
                self.client_connected.emit(client.client_id)
                self.log_message.emit(f"客户端 {client.client_id} 已连接")
                
            except Exception as e:
                print(f"[DEBUG] Auth error: {e}")
                resp = Protocol.create_auth_response(False, f"验证失败: {e}")
                client.send(resp)
                
        elif msg_type == 0x07:  # HEARTBEAT
            print(f"[DEBUG] HEARTBEAT from {client.client_id}")
            client.last_heartbeat = time.time()
            
        elif msg_type == 0x01:  # FILE
            # 接收文件
            print(f"[DEBUG] FILE from {client.client_id}: {filename}, mtime={mtime}")
            self._handle_file_receive(client, filename, content, hide_flag, mtime)
            
        elif msg_type == 0x02:  # DELETE
            # 处理删除指令
            print(f"[DEBUG] DELETE from {client.client_id}: {filename}")
            self._handle_delete_receive(client, filename)
            
        elif msg_type == 0x0B:  # DIR_CREATE
            # 处理目录创建
            print(f"[DEBUG] DIR_CREATE from {client.client_id}: {filename}")
            self._handle_dir_create_receive(client, filename)
            
        elif msg_type == 0x05:  # FILE_LIST_REQ
            # 发送文件列表
            print(f"[DEBUG] FILE_LIST_REQ from {client.client_id}")
            self._send_file_list(client)
            
        elif msg_type == 0x08:  # FULL_SYNC_REQ
            # 全量同步请求
            print(f"[DEBUG] FULL_SYNC_REQ from {client.client_id}")
            self._handle_full_sync_request(client)
            
        elif msg_type == 0x0A:  # CLIENT_INFO
            # 更新客户端信息
            print(f"[DEBUG] CLIENT_INFO from {client.client_id}: hide={hide_flag}")
            client.hide_from_others = hide_flag
            self.log_message.emit(f"客户端 {client.client_id} 更新隐藏状态: {hide_flag}")
    
    def _handle_file_receive(self, client: ClientConnection, filename: str,
                             content: bytes, hide_from_others: bool, mtime: float):
        """处理接收到的文件"""
        import os
        
        try:
            # 保存文件到同步文件夹
            filepath = os.path.join(self.sync_folder, filename)
            
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
            
            self.file_received.emit(client.client_id, filename, hide_from_others)
            self.log_message.emit(f"接收文件: {filename} (来自 {client.client_id})")
            
            # 如果不隐藏且允许互相同步，转发给其他客户端
            if not hide_from_others and self.allow_peer_sync:
                self.broadcast_file(filepath, exclude_client=client.client_id)
                
        except Exception as e:
            self.error_occurred.emit(f"保存文件失败 {filename}: {e}")
    
    def _handle_delete_receive(self, client: ClientConnection, filename: str):
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
            
            self.delete_received.emit(client.client_id, filename)
            self.log_message.emit(f"删除文件: {filename} (来自 {client.client_id})")
            
            # 转发删除指令给其他客户端
            if self.allow_peer_sync:
                self.broadcast_delete(filepath, exclude_client=client.client_id)
                
        except Exception as e:
            self.error_occurred.emit(f"删除文件失败 {filename}: {e}")
    
    def _handle_dir_create_receive(self, client: ClientConnection, dirname: str):
        """处理接收到的目录创建"""
        import os
        
        print(f"[DEBUG] _handle_dir_create_receive: {dirname}")
        
        try:
            dirpath = os.path.join(self.sync_folder, dirname)
            print(f"[DEBUG] Creating directory: {dirpath}")
            
            if not os.path.exists(dirpath):
                os.makedirs(dirpath, exist_ok=True)
                print(f"[DEBUG] Directory created: {dirpath}")
            
            self.log_message.emit(f"创建目录: {dirname} (来自 {client.client_id})")
            
            # 转发目录创建给其他客户端
            if self.allow_peer_sync:
                self.broadcast_dir_create(dirpath, exclude_client=client.client_id)
                
        except Exception as e:
            print(f"[DEBUG] Create directory error: {e}")
            self.error_occurred.emit(f"创建目录失败 {dirname}: {e}")
    
    def _send_file_list(self, client: ClientConnection):
        """发送文件列表给客户端"""
        import os
        
        file_list = []
        for root, dirs, files in os.walk(self.sync_folder):
            for file in files:
                filepath = os.path.join(root, file)
                rel_path = os.path.relpath(filepath, self.sync_folder)
                file_size = os.path.getsize(filepath)
                mtime = os.path.getmtime(filepath)
                file_list.append([rel_path, file_size, mtime])
        
        message = Protocol.create_file_list_response(file_list)
        client.send(message)
    
    def _handle_full_sync_request(self, client: ClientConnection):
        """处理全量同步请求"""
        import os
        
        self.log_message.emit(f"开始全量同步到 {client.client_id}")
        
        # 发送所有文件
        for root, dirs, files in os.walk(self.sync_folder):
            for file in files:
                filepath = os.path.join(root, file)
                try:
                    message = Protocol.create_file_message(filepath, self.sync_folder, False)
                    client.send(message)
                except Exception as e:
                    self.error_occurred.emit(f"同步文件失败 {file}: {e}")
        
        self.log_message.emit(f"全量同步完成: {client.client_id}")
    
    def _remove_client(self, client: ClientConnection):
        """移除客户端"""
        with self._lock:
            if client.client_id in self.clients:
                del self.clients[client.client_id]
        
        client.close()
        self.client_disconnected.emit(client.client_id)
        self.log_message.emit(f"客户端 {client.client_id} 已断开")
    
    def _heartbeat_check(self):
        """心跳检测"""
        import time
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            
            current_time = time.time()
            with self._lock:
                clients_to_remove = []
                for client_id, client in self.clients.items():
                    if current_time - client.last_heartbeat > HEARTBEAT_TIMEOUT:
                        clients_to_remove.append(client)
                
                for client in clients_to_remove:
                    self._remove_client(client)
    
    def _discovery_loop(self):
        """UDP发现响应循环"""
        while self.running:
            try:
                data, addr = self.discovery_socket.recvfrom(1024)
                request = json.loads(data.decode('utf-8'))
                
                if request.get('type') == 'discovery_request':
                    # 检查是否匹配房间号
                    target_room = request.get('room_code')
                    if target_room and target_room != self.room_code:
                        continue
                    
                    # 发送响应
                    response = json.dumps({
                        'type': 'discovery_response',
                        'room_code': self.room_code,
                        'port': self.port
                    }).encode('utf-8')
                    
                    self.discovery_socket.sendto(response, addr)
                    
            except socket.timeout:
                continue
            except Exception:
                continue
    
    def get_client_list(self) -> list:
        """获取客户端列表"""
        with self._lock:
            return [
                {
                    'client_id': client_id,
                    'address': client.address,
                    'hide_from_others': client.hide_from_others
                }
                for client_id, client in self.clients.items()
            ]