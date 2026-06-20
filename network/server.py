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
    HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT, DEFAULT_PORT,
    CHUNKED_TRANSFER_THRESHOLD, CHUNK_SIZE, WINDOW_SIZE, ACK_TIMEOUT, MAX_RETRY_COUNT
)
from protocol import Protocol, MessageReceiver, MSG_TYPE_FILE_ACK, MSG_TYPE_FILE_CANCEL, MSG_TYPE_SYNC_REQUEST


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
        
        # 大文件分块传输临时存储（接收端）
        self.temp_file_info = {}  # {filename: {file_size, mtime, hide, temp_path, received_bytes, file_handle}}
        
        # 发送状态跟踪（用于滑动窗口流控）
        self.send_state = {}  # {filename: {chunks: [(index, data)], total_chunks, sent_index, acked_index, file_size, mtime, hide, retry_count}}
        
        # 确认事件（用于等待确认）
        self.ack_events = {}  # {filename: threading.Event}
        
        # 传输锁（保护发送状态）
        self.transfer_lock = threading.Lock()
    
    def send(self, data: bytes) -> bool:
        """发送数据"""
        try:
            self.socket.sendall(data)
            return True
        except Exception as e:
            print(f"[ERROR] Send failed to {self.client_id}: {e}")
            return False
    
    def close(self):
        """关闭连接"""
        try:
            self.socket.close()
        except Exception:
            pass
        
        # 清理所有确认事件
        for event in self.ack_events.values():
            event.set()  # 解除等待
        self.ack_events.clear()


class SyncServer(QObject):
    """同步服务器（主机端）"""
    
    # 信号定义
    client_connected = pyqtSignal(str)  # 客户端连接 (client_id)
    client_disconnected = pyqtSignal(str)  # 客户端断开 (client_id)
    file_received = pyqtSignal(str, str, bool)  # 文件接收 (client_id, filename, hide_from_others)
    delete_received = pyqtSignal(str, str)  # 删除指令 (client_id, filename)
    log_message = pyqtSignal(str)  # 日志消息
    error_occurred = pyqtSignal(str)  # 错误消息
    
    # 传输状态信号
    transfer_started = pyqtSignal(str, int, str)  # 传输开始 (filename, file_size, direction: 'send'/'receive')
    transfer_progress = pyqtSignal(str, int, int)  # 传输进度 (filename, current, total)
    transfer_finished = pyqtSignal(str, str)  # 传输结束 (filename, direction)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server_socket: Optional[socket.socket] = None
        self.discovery_socket: Optional[socket.socket] = None  # UDP发现socket
        self.clients: Dict[str, ClientConnection] = {}
        self.running = False
        self.room_code = ""
        self.password_hash = ""
        self.sync_folder = ""
        self.port = DEFAULT_PORT
        
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(MAX_CONCURRENT_TRANSFERS)
    
    def start(self, room_code: str, password: str, sync_folder: str,
              port: int = DEFAULT_PORT) -> bool:
        """
        启动服务器
        """
        try:
            self.room_code = room_code
            self.password_hash = hashlib.sha256(password.encode()).hexdigest() if password else ""
            self.sync_folder = sync_folder
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
        广播文件到所有客户端（所有文件都使用分块传输）
        Args:
            filepath: 文件路径
            exclude_client: 排除的客户端ID
            hide_from_others: 是否对外隐藏（注意：调用此函数前应已检查此条件）
        """
        import os
        
        try:
            # 所有文件都使用分块传输
            self._broadcast_file_chunked(filepath, exclude_client, hide_from_others)
        except Exception as e:
            self.error_occurred.emit(f"广播文件失败: {e}")
    
    def _broadcast_file_chunked(self, filepath: str, exclude_client: str,
                                 hide_from_others: bool):
        """
        分块广播大文件到所有客户端（简化流控：连续发送，不阻塞等待）
        Args:
            filepath: 文件路径
            exclude_client: 排除的客户端ID
            hide_from_others: 是否对外隐藏
        """
        import os
        import time
        
        try:
            # 发送文件开始消息
            begin_msg, file_size, mtime, rel_path = Protocol.create_file_begin_message(
                filepath, self.sync_folder, hide_from_others
            )
            
            # 标记文件正在同步（发送）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = rel_path.replace('\\', '/')
                    parent.sync_engine.mark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
            # 发送传输开始信号
            self.transfer_started.emit(rel_path, file_size, 'send')
            self.log_message.emit(f"开始广播大文件: {filepath} ({file_size / 1024 / 1024:.2f} MB)")
            
            # 获取目标客户端列表（复制列表，避免在发送过程中被修改）
            with self._lock:
                target_clients = [client for client_id, client in self.clients.items()
                                  if client_id != exclude_client]
            
            if not target_clients:
                self.transfer_finished.emit(rel_path, 'send')
                return
            
            # 发送开始消息
            failed_clients = []
            for client in target_clients:
                if not client.send(begin_msg):
                    failed_clients.append(client)
            
            # 移除发送失败的客户端
            for client in failed_clients:
                target_clients.remove(client)
            
            if not target_clients:
                self.transfer_finished.emit(rel_path, 'send')
                return
            
            # 连续发送数据块（不阻塞等待确认）
            sent_bytes = 0
            last_progress_bytes = 0
            progress_interval = max(CHUNK_SIZE * 4, file_size // 50)  # 每2%或至少256KB发送一次进度
            
            with open(filepath, 'rb') as f:
                chunk_index = 0
                while True:
                    chunk_data = f.read(CHUNK_SIZE)
                    if not chunk_data:
                        break
                    
                    # 发送数据块
                    data_msg = Protocol.create_file_data_message(rel_path, chunk_index, chunk_data)
                    failed_clients = []
                    for client in target_clients:
                        if not client.send(data_msg):
                            failed_clients.append(client)
                    
                    # 移除发送失败的客户端
                    for client in failed_clients:
                        target_clients.remove(client)
                    
                    # 如果所有客户端都失败了，停止发送
                    if not target_clients:
                        self.transfer_finished.emit(rel_path, 'send')
                        return
                    
                    sent_bytes += len(chunk_data)
                    chunk_index += 1
                    
                    # 发送进度信号（降低频率）
                    if sent_bytes - last_progress_bytes >= progress_interval or sent_bytes >= file_size:
                        self.transfer_progress.emit(rel_path, sent_bytes, file_size)
                        last_progress_bytes = sent_bytes
                    
                    # 添加微小延迟，避免 TCP 缓冲区过满（每发送一定量数据后暂停）
                    if chunk_index % 50 == 0:
                        time.sleep(0.001)  # 1ms 延迟
            
            
            # 发送文件结束消息
            end_msg = Protocol.create_file_end_message(rel_path, file_size, mtime, hide_from_others)
            for client in target_clients:
                client.send(end_msg)
            
            # 发送传输结束信号
            self.transfer_finished.emit(rel_path, 'send')
            self.log_message.emit(f"大文件广播完成: {filepath}")
            
            # 取消标记文件正在同步（发送完成）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = rel_path.replace('\\', '/')
                    parent.sync_engine.unmark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
        except Exception as e:
            self.error_occurred.emit(f"分块广播文件失败: {e}")
    
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
        
        while self.running:
            try:
                # 使用更短的timeout，提高响应速度
                client.socket.settimeout(1.0)
                data = client.socket.recv(BUFFER_SIZE)
                
                if not data:
                    # 连接被客户端主动关闭
                    break
                
                client.receiver.feed(data)
                
                # 持续处理所有完整消息（不阻塞）
                while client.receiver.has_complete_message():
                    message = client.receiver.get_message()
                    if message:
                        self._process_message(client, message)
                        
            except socket.timeout:
                # 超时继续循环，不断开连接
                # 检查心跳时间决定是否断开
                current_time = time.time()
                if current_time - client.last_heartbeat > HEARTBEAT_TIMEOUT:
                    self.log_message.emit(f"客户端 {client.client_id} 心跳超时")
                    break  # 心跳超时，退出循环
                else:
                    # 更新心跳时间，继续循环
                    client.last_heartbeat = current_time
                    continue  # 继续循环
            except ConnectionResetError:
                self.log_message.emit(f"客户端 {client.client_id} 连接被重置")
                break
            except ConnectionAbortedError:
                self.log_message.emit(f"客户端 {client.client_id} 连接被中止")
                break
            except Exception as e:
                if self.running:
                    self.log_message.emit(f"客户端 {client.client_id} 连接错误: {e}")
                break
        
        # 移除客户端
        self._remove_client(client)
    
    def _process_message(self, client: ClientConnection, message: tuple):
        """处理客户端消息"""
        import time
        import json
        
        msg_type, filename, file_size, mtime, hide_flag, content = message
        
        if msg_type == 0x03:  # AUTH_REQ
            # 验证请求
            try:
                auth_data = content.decode('utf-8').split(':')
                room_code = auth_data[0] if len(auth_data) > 0 else ''
                password_hash = auth_data[1] if len(auth_data) > 1 else ''
                
                if room_code != self.room_code:
                    resp = Protocol.create_auth_response(False, "房间号错误")
                    client.send(resp)
                    return
                
                if self.password_hash and password_hash != self.password_hash:
                    resp = Protocol.create_auth_response(False, "密码错误")
                    client.send(resp)
                    return
                
                # 验证成功，添加到客户端列表
                with self._lock:
                    self.clients[client.client_id] = client
                
                # 发送验证成功响应
                resp = Protocol.create_auth_response(True, "验证成功")
                client.send(resp)
                
                self.client_connected.emit(client.client_id)
                self.log_message.emit(f"客户端 {client.client_id} 已连接")
                
            except Exception as e:
                resp = Protocol.create_auth_response(False, f"验证失败: {e}")
                client.send(resp)
                
        elif msg_type == 0x07:  # HEARTBEAT
            client.last_heartbeat = time.time()
            
        elif msg_type == 0x01:  # FILE
            # 接收文件
            self._handle_file_receive(client, filename, content, hide_flag, mtime)
            
        elif msg_type == 0x02:  # DELETE
            # 处理删除指令
            self._handle_delete_receive(client, filename)
            
        elif msg_type == 0x0B:  # DIR_CREATE
            # 处理目录创建
            self._handle_dir_create_receive(client, filename)
            
        elif msg_type == 0x05:  # FILE_LIST_REQ
            # 发送文件列表
            self._send_file_list(client)
            
        elif msg_type == 0x08:  # FULL_SYNC_REQ
            # 全量同步请求
            self._handle_full_sync_request(client)
            
        elif msg_type == 0x0A:  # CLIENT_INFO
            # 更新客户端信息
            client.hide_from_others = hide_flag
            self.log_message.emit(f"客户端 {client.client_id} 更新隐藏状态: {hide_flag}")
            
        elif msg_type == 0x0C:  # FILE_BEGIN
            # 大文件传输开始
            self._handle_file_begin(client, filename, file_size, hide_flag, mtime)
            
        elif msg_type == 0x0D:  # FILE_DATA
            # 大文件数据块
            chunk_index, chunk_data = content
            self._handle_file_data(client, filename, chunk_index, chunk_data)
            
        elif msg_type == 0x0E:  # FILE_END
            # 大文件传输结束
            self._handle_file_end(client, filename, file_size, hide_flag, mtime)
        
        elif msg_type == MSG_TYPE_FILE_ACK:  # FILE_ACK (0x0F)
            # 数据块确认（流控）
            chunk_index, received_bytes = content
            self._handle_file_ack(client, filename, chunk_index, received_bytes)
        
        elif msg_type == MSG_TYPE_FILE_CANCEL:  # FILE_CANCEL (0x10)
            # 文件传输取消
            self._handle_file_cancel(client, filename, content)
        
        elif msg_type == MSG_TYPE_SYNC_REQUEST:  # SYNC_REQUEST (0x11)
            # 双向同步请求
            self._handle_sync_request(client, content)
    
    def _handle_file_receive(self, client: ClientConnection, filename: str,
                             content: bytes, hide_from_others: bool, mtime: float):
        """处理接收到的文件"""
        import os
        
        try:
            # 标记文件正在同步（接收）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = filename.replace('\\', '/')
                    parent.sync_engine.mark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
            # 保存文件到同步文件夹
            filepath = os.path.join(self.sync_folder, filename)
            
            # 检查本地是否存在同名文件，比较时间戳
            if os.path.exists(filepath):
                local_mtime = os.path.getmtime(filepath)
                # 如果本地文件更新，跳过接收
                if local_mtime > mtime:
                    self.log_message.emit(f"跳过文件: {filename} (本地文件更新)")
                    # 取消标记
                    try:
                        parent = self.parent()
                        if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                            normalized_path = filename.replace('\\', '/')
                            parent.sync_engine.unmark_syncing(normalized_path)
                    except Exception:
                        pass
                    return
                # 如果时间戳相同（误差1秒内），也跳过避免重复同步
                if abs(local_mtime - mtime) < 1.0:
                    # 取消标记
                    try:
                        parent = self.parent()
                        if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                            normalized_path = filename.replace('\\', '/')
                            parent.sync_engine.unmark_syncing(normalized_path)
                    except Exception:
                        pass
                    return
            
            dir_path = os.path.dirname(filepath)
            if dir_path:  # 只有当目录路径非空时才创建
                os.makedirs(dir_path, exist_ok=True)
            
            with open(filepath, 'wb') as f:
                f.write(content)
            
            # 设置文件的修改时间为原始时间
            os.utime(filepath, (mtime, mtime))
            
            # 取消标记文件正在同步（接收完成）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = filename.replace('\\', '/')
                    parent.sync_engine.unmark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
            self.file_received.emit(client.client_id, filename, hide_from_others)
            self.log_message.emit(f"接收文件: {filename} (来自 {client.client_id})")
                
        except Exception as e:
            self.error_occurred.emit(f"保存文件失败 {filename}: {e}")
            # 取消标记
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    normalized_path = filename.replace('\\', '/')
                    parent.sync_engine.unmark_syncing(normalized_path)
            except Exception:
                pass
    
    def _handle_file_begin(self, client: ClientConnection, filename: str,
                           file_size: int, hide_from_others: bool, mtime: float):
        """处理大文件传输开始"""
        import os
        import tempfile
        
        try:
            filepath = os.path.join(self.sync_folder, filename)
            
            # 检查本地是否存在同名文件，比较时间戳
            if os.path.exists(filepath):
                local_mtime = os.path.getmtime(filepath)
                if local_mtime > mtime:
                    self.log_message.emit(f"跳过文件: {filename} (本地文件更新)")
                    return
                if abs(local_mtime - mtime) < 1.0:
                    return
            
            # 创建临时文件
            dir_path = os.path.dirname(filepath)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            
            temp_path = filepath + '.tmp'
            
            # 记录文件信息
            client.temp_file_info[filename] = {
                'file_size': file_size,
                'mtime': mtime,
                'hide': hide_from_others,
                'temp_path': temp_path,
                'received_bytes': 0,
                'last_progress_bytes': 0,
                'progress_interval': max(CHUNK_SIZE * 4, file_size // 50),  # 每2%或至少256KB发送一次进度
                'file_handle': None  # 文件句柄（保持打开）
            }
            
            # 创建并打开临时文件（保持打开直到传输结束）
            client.temp_file_info[filename]['file_handle'] = open(temp_path, 'wb')
            
            # 标记文件正在同步（接收）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = filename.replace('\\', '/')
                    parent.sync_engine.mark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
            # 发送传输开始信号
            self.transfer_started.emit(filename, file_size, 'receive')
            self.log_message.emit(f"开始接收大文件: {filename} ({file_size / 1024 / 1024:.2f} MB)")
            
        except Exception as e:
            self.error_occurred.emit(f"准备接收大文件失败 {filename}: {e}")
    
    def _handle_file_data(self, client: ClientConnection, filename: str,
                          chunk_index: int, chunk_data: bytes):
        """处理大文件数据块"""
        import os
        
        try:
            if filename not in client.temp_file_info:
                return
            
            info = client.temp_file_info[filename]
            file_handle = info['file_handle']
            
            # 使用已打开的文件句柄写入数据（避免频繁打开/关闭）
            if file_handle:
                file_handle.write(chunk_data)
            
            info['received_bytes'] += len(chunk_data)
            
            # 降低进度信号发送频率（每2%或每256KB发送一次）
            if info['received_bytes'] - info['last_progress_bytes'] >= info['progress_interval'] or info['received_bytes'] >= info['file_size']:
                self.transfer_progress.emit(filename, info['received_bytes'], info['file_size'])
                info['last_progress_bytes'] = info['received_bytes']
            
        except Exception as e:
            self.error_occurred.emit(f"接收文件数据块失败 {filename}: {e}")
    
    def _handle_file_end(self, client: ClientConnection, filename: str,
                         file_size: int, hide_from_others: bool, mtime: float):
        """处理大文件传输结束"""
        import os
        
        try:
            if filename not in client.temp_file_info:
                return
            
            info = client.temp_file_info[filename]
            temp_path = info['temp_path']
            file_handle = info['file_handle']
            
            # 关闭文件句柄
            if file_handle:
                file_handle.close()
            
            # 检查接收完整性
            actual_size = os.path.getsize(temp_path)
            if actual_size != info['file_size']:
                self.error_occurred.emit(f"文件大小不匹配: {filename} (期望 {info['file_size']}, 实际 {actual_size})")
                os.remove(temp_path)
                del client.temp_file_info[filename]
                return
            
            # 重命名临时文件为正式文件
            filepath = os.path.join(self.sync_folder, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
            os.rename(temp_path, filepath)
            
            # 设置文件的修改时间为原始时间
            os.utime(filepath, (mtime, mtime))
            
            # 取消标记文件正在同步（接收完成）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = filename.replace('\\', '/')
                    parent.sync_engine.unmark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
            # 清理临时信息
            del client.temp_file_info[filename]
            
            # 发送传输结束信号
            self.transfer_finished.emit(filename, 'receive')
            self.file_received.emit(client.client_id, filename, hide_from_others)
            self.log_message.emit(f"大文件接收完成: {filename} (来自 {client.client_id})")
                
        except Exception as e:
            self.error_occurred.emit(f"完成接收大文件失败 {filename}: {e}")
    
    def _handle_file_ack(self, client: ClientConnection, filename: str,
                         chunk_index: int, received_bytes: int):
        """处理数据块确认（流控）"""
        try:
            with client.transfer_lock:
                # 更新确认状态
                if filename in client.send_state:
                    client.send_state[filename]['acked_index'] = chunk_index
                
                # 触发确认事件
                if filename in client.ack_events:
                    client.ack_events[filename].set()
        except Exception as e:
            self.log_message.emit(f"处理确认消息失败 {filename}: {e}")
    
    def _handle_file_cancel(self, client: ClientConnection, filename: str, reason: bytes):
        """处理文件传输取消"""
        try:
            reason_text = reason.decode('utf-8') if reason else ''
            self.log_message.emit(f"客户端 {client.client_id} 取消传输: {filename} ({reason_text})")
            
            # 清理发送状态
            with client.transfer_lock:
                client.send_state.pop(filename, None)
                if filename in client.ack_events:
                    client.ack_events[filename].set()
                    client.ack_events.pop(filename, None)
        except Exception as e:
            self.log_message.emit(f"处理取消消息失败 {filename}: {e}")
    
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
                
        except Exception as e:
            self.error_occurred.emit(f"删除文件失败 {filename}: {e}")
    
    def _handle_dir_create_receive(self, client: ClientConnection, dirname: str):
        """处理接收到的目录创建"""
        import os
        
        try:
            dirpath = os.path.join(self.sync_folder, dirname)
            
            if not os.path.exists(dirpath):
                os.makedirs(dirpath, exist_ok=True)
            
            self.log_message.emit(f"创建目录: {dirname} (来自 {client.client_id})")
                
        except Exception as e:
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
        """处理全量同步请求（双向同步）"""
        import os
        
        self.log_message.emit(f"开始双向同步到 {client.client_id}")
        
        # 发送文件列表给连接端
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
        
        self.log_message.emit(f"已发送文件列表到 {client.client_id} ({len(file_list)} 个文件)")
    
    def _handle_sync_request(self, client: ClientConnection, content: bytes):
        """处理双向同步请求"""
        import os
        import json
        
        try:
            data = json.loads(content.decode('utf-8'))
            need_receive = data.get('need_receive', [])  # 需要从主机端接收的文件
            need_send = data.get('need_send', [])  # 需要发送到主机端的文件
            
            self.log_message.emit(f"收到同步请求: 需接收 {len(need_receive)} 个，需发送 {len(need_send)} 个")
            
            # 发送 need_receive 中的文件到连接端
            for file_info in need_receive:
                rel_path = file_info[0]
                filepath = os.path.join(self.sync_folder, rel_path)
                if os.path.exists(filepath):
                    try:
                        self._send_file_chunked_to_client(filepath, client)
                    except Exception as e:
                        self.error_occurred.emit(f"发送文件失败 {rel_path}: {e}")
            
            self.log_message.emit(f"双向同步完成: {client.client_id}")
            
        except Exception as e:
            self.error_occurred.emit(f"处理同步请求失败: {e}")
    
    def _send_file_chunked_to_client(self, filepath: str, client: ClientConnection):
        """
        分块发送大文件到指定客户端
        Args:
            filepath: 文件路径
            client: 目标客户端
        """
        import os
        
        try:
            # 发送文件开始消息
            begin_msg, file_size, mtime, rel_path = Protocol.create_file_begin_message(
                filepath, self.sync_folder, False
            )
            
            # 标记文件正在同步（发送）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = rel_path.replace('\\', '/')
                    parent.sync_engine.mark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
            client.send(begin_msg)
            
            # 分块发送文件内容
            with open(filepath, 'rb') as f:
                chunk_index = 0
                while True:
                    chunk_data = f.read(CHUNK_SIZE)
                    if not chunk_data:
                        break
                    
                    data_msg = Protocol.create_file_data_message(rel_path, chunk_index, chunk_data)
                    client.send(data_msg)
                    
                    chunk_index += 1
            
            # 发送文件结束消息
            end_msg = Protocol.create_file_end_message(rel_path, file_size, mtime, False)
            client.send(end_msg)
            
            # 取消标记文件正在同步（发送完成）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = rel_path.replace('\\', '/')
                    parent.sync_engine.unmark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
        except Exception as e:
            self.error_occurred.emit(f"分块发送文件失败 {filepath}: {e}")
    
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