# -*- coding: utf-8 -*-
"""
LANSyncBox TCP客户端模块（连接端）
"""

import os
import socket
import threading
import time
from typing import Optional, Callable
from PyQt5.QtCore import QObject, pyqtSignal

from config import (
    BUFFER_SIZE, CONNECTION_TIMEOUT, HEARTBEAT_INTERVAL,
    CHUNKED_TRANSFER_THRESHOLD, CHUNK_SIZE, WINDOW_SIZE, ACK_TIMEOUT, MAX_RETRY_COUNT
)
from protocol import Protocol, MessageReceiver, MSG_TYPE_FILE_ACK, MSG_TYPE_FILE_CANCEL, MSG_TYPE_SYNC_REQUEST


class SyncClient(QObject):
    """同步客户端（连接端）"""
    
    # 信号定义
    connected = pyqtSignal()  # 连接成功
    disconnected = pyqtSignal()  # 断开连接
    auth_success = pyqtSignal()  # 验证成功
    auth_failed = pyqtSignal(str)  # 验证失败 (message)
    file_received = pyqtSignal(str, int)  # 文件接收完成 (filename, size)
    file_receiving = pyqtSignal(str)  # 文件开始接收 (filepath) - 用于提前添加到忽略列表
    delete_received = pyqtSignal(str)  # 删除指令 (filename)
    file_list_received = pyqtSignal(list)  # 文件列表接收
    log_message = pyqtSignal(str)  # 日志消息
    error_occurred = pyqtSignal(str)  # 错误消息
    
    # 传输状态信号
    transfer_started = pyqtSignal(str, int, str)  # 传输开始 (filename, file_size, direction: 'send'/'receive')
    transfer_progress = pyqtSignal(str, int, int)  # 传输进度 (filename, current, total)
    transfer_finished = pyqtSignal(str, str)  # 传输结束 (filename, direction)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket: Optional[socket.socket] = None
        self.connected_flag = False
        self.authenticated = False
        self.server_address = ""
        self.server_port = 9527
        self.room_code = ""
        self.password = ""
        self.sync_folder = ""
        self.hide_from_others = False  # 已弃用，所有端保持一致
        
        self._receiver = MessageReceiver()
        self._running = False
        self._lock = threading.Lock()
        
        # 大文件分块传输临时存储（接收端）
        self.temp_file_info = {}  # {filename: {file_size, mtime, hide, temp_path, received_bytes, file_handle}}
        
        # 发送状态跟踪（用于滑动窗口流控）
        self.send_state = {}  # {filename: {acked_index, file_size, mtime, hide}}
        
        # 确认事件（用于等待确认）
        self.ack_events = {}  # {filename: threading.Event}
        
        # 传输锁（保护发送状态）
        self.transfer_lock = threading.Lock()
    
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
        
        # 清理所有确认事件
        with self.transfer_lock:
            for event in self.ack_events.values():
                event.set()  # 解除等待
            self.ack_events.clear()
            self.send_state.clear()
        
        self.disconnected.emit()
        self.log_message.emit("已断开连接")
    
    def send_file(self, filepath: str, hide_from_others: bool = False) -> bool:
        """
        发送文件到服务器（所有文件都使用分块传输）
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
            # 所有文件都使用分块传输
            return self._send_file_chunked(filepath, hide_from_others)
        except Exception as e:
            self.error_occurred.emit(f"发送文件失败: {e}")
            return False
    
    def _send_file_chunked(self, filepath: str, hide_from_others: bool) -> bool:
        """
        分块发送大文件（简化流控：连续发送，不阻塞等待）
        Args:
            filepath: 文件路径
            hide_from_others: 是否对外隐藏
        Returns:
            是否发送成功
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
            self.log_message.emit(f"开始发送大文件: {filepath} ({file_size / 1024 / 1024:.2f} MB)")
            
            # 发送开始消息
            with self._lock:
                if not self._send_raw(begin_msg):
                    self.transfer_finished.emit(rel_path, 'send')
                    return False
            
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
                    with self._lock:
                        if not self._send_raw(data_msg):
                            self.transfer_finished.emit(rel_path, 'send')
                            return False
                    
                    sent_bytes += len(chunk_data)
                    chunk_index += 1
                    
                    # 发送进度信号（降低频率）
                    if sent_bytes - last_progress_bytes >= progress_interval or sent_bytes >= file_size:
                        self.transfer_progress.emit(rel_path, sent_bytes, file_size)
                        last_progress_bytes = sent_bytes
                    
                    # 添加微小延迟，避免 TCP 缓冲区过满
                    if chunk_index % 50 == 0:
                        time.sleep(0.001)  # 1ms 延迟
            
            # 发送文件结束消息
            end_msg = Protocol.create_file_end_message(rel_path, file_size, mtime, hide_from_others)
            with self._lock:
                if not self._send_raw(end_msg):
                    self.transfer_finished.emit(rel_path, 'send')
                    return False
            
            # 发送传输结束信号
            self.transfer_finished.emit(rel_path, 'send')
            self.log_message.emit(f"大文件发送完成: {filepath}")
            
            # 取消标记文件正在同步（发送完成）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = rel_path.replace('\\', '/')
                    parent.sync_engine.unmark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"分块发送文件失败: {e}")
            return False
    
    def _send_raw(self, data: bytes) -> bool:
        """发送原始数据（内部方法）"""
        try:
            if self.socket and self.connected_flag:
                self.socket.sendall(data)
                return True
            return False
        except Exception as e:
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
    
    def _receive_loop(self):
        """接收数据循环"""
        while self._running:
            try:
                # 使用较短的timeout以便定期检查运行状态
                self.socket.settimeout(1.0)  # 缩短timeout到1秒，提高响应速度
                data = self.socket.recv(BUFFER_SIZE)
                
                if not data:
                    # 服务器主动关闭连接
                    break
                
                self._receiver.feed(data)
                
                # 持续处理所有完整消息（不阻塞）
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
        
        if msg_type == 0x04:  # AUTH_RESP
            # 验证响应
            try:
                resp_data = content.decode('utf-8').split(':')
                success = resp_data[0] == '1'
                message_text = resp_data[1] if len(resp_data) > 1 else ''
                
                if success:
                    self.authenticated = True
                    self.auth_success.emit()
                    self.log_message.emit(f"验证成功")
                else:
                    self.auth_failed.emit(message_text)
                    self.disconnect()
                    
            except Exception as e:
                self.auth_failed.emit(f"验证响应解析失败: {e}")
                self.disconnect()
                
        elif msg_type == 0x01:  # FILE
            # 接收文件
            self._handle_file_receive(filename, content, mtime)
            
        elif msg_type == 0x02:  # DELETE
            # 处理删除指令
            self._handle_delete_receive(filename)
            
        elif msg_type == 0x0B:  # DIR_CREATE
            # 处理目录创建
            self._handle_dir_create_receive(filename)
            
        elif msg_type == 0x06:  # FILE_LIST_RESP
            # 文件列表响应（用于双向同步）
            try:
                import os
                import json
                file_list = json.loads(content.decode('utf-8'))
                self.log_message.emit(f"收到文件列表 ({len(file_list)} 个文件)")
                
                # 比较本地文件，生成同步请求
                need_receive = []  # 需要从主机端接收的文件
                need_send = []  # 需要发送到主机端的文件
                
                for file_info in file_list:
                    rel_path = file_info[0]
                    host_size = file_info[1]
                    host_mtime = file_info[2]
                    
                    filepath = os.path.join(self.sync_folder, rel_path)
                    
                    if os.path.exists(filepath):
                        local_mtime = os.path.getmtime(filepath)
                        # 如果本地文件更新，需要发送到主机端
                        if local_mtime > host_mtime + 1.0:  # 误差1秒
                            local_size = os.path.getsize(filepath)
                            need_send.append([rel_path, local_size, local_mtime])
                        # 如果主机端文件更新，需要从主机端接收
                        elif host_mtime > local_mtime + 1.0:  # 误差1秒
                            need_receive.append(file_info)
                        # 时间戳相同，跳过
                    else:
                        # 本地缺失，需要从主机端接收
                        need_receive.append(file_info)
                
                # 发送同步请求
                sync_request = Protocol.create_sync_request(need_receive, need_send)
                with self._lock:
                    self.socket.sendall(sync_request)
                
                self.log_message.emit(f"发送同步请求: 需接收 {len(need_receive)} 个，需发送 {len(need_send)} 个")
                
                # 发送 need_send 中的文件到主机端
                for file_info in need_send:
                    rel_path = file_info[0]
                    filepath = os.path.join(self.sync_folder, rel_path)
                    try:
                        self.send_file(filepath)
                    except Exception as e:
                        self.error_occurred.emit(f"发送文件失败 {rel_path}: {e}")
                
            except Exception as e:
                self.error_occurred.emit(f"处理文件列表失败: {e}")
                
        elif msg_type == 0x0C:  # FILE_BEGIN
            # 大文件传输开始
            self._handle_file_begin(filename, file_size, hide_flag, mtime)
            
        elif msg_type == 0x0D:  # FILE_DATA
            # 大文件数据块
            chunk_index, chunk_data = content
            self._handle_file_data(filename, chunk_index, chunk_data)
            
        elif msg_type == 0x0E:  # FILE_END
            # 大文件传输结束
            self._handle_file_end(filename, file_size, hide_flag, mtime)
        
        elif msg_type == MSG_TYPE_FILE_ACK:  # FILE_ACK (0x0F)
            # 数据块确认（流控）
            chunk_index, received_bytes = content
            self._handle_file_ack(filename, chunk_index, received_bytes)
        
        elif msg_type == MSG_TYPE_FILE_CANCEL:  # FILE_CANCEL (0x10)
            # 文件传输取消
            self._handle_file_cancel(filename, content)
    
    def _handle_file_receive(self, filename: str, content: bytes, mtime: float):
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
            
            self.file_received.emit(filename, len(content))
            self.log_message.emit(f"接收文件: {filename}")
            
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
    
    def _handle_file_begin(self, filename: str, file_size: int,
                           hide_from_others: bool, mtime: float):
        """处理大文件传输开始"""
        import os
        
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
            self.temp_file_info[filename] = {
                'file_size': file_size,
                'mtime': mtime,
                'hide': hide_from_others,
                'temp_path': temp_path,
                'received_bytes': 0,
                'last_progress_bytes': 0,
                'progress_interval': max(CHUNK_SIZE * 4, file_size // 50),  # 每2%或至少256KB发送一次进度
                'file_handle': None,  # 文件句柄（保持打开）
                'filepath': filepath  # 保存最终文件路径
            }
            
            # 创建并打开临时文件（保持打开直到传输结束）
            self.temp_file_info[filename]['file_handle'] = open(temp_path, 'wb')
            
            # 标记文件正在同步（接收）
            try:
                parent = self.parent()
                if parent and hasattr(parent, 'sync_engine') and parent.sync_engine:
                    # 标准化路径：统一使用正斜杠
                    normalized_path = filename.replace('\\', '/')
                    parent.sync_engine.mark_syncing(normalized_path)
            except Exception:
                pass  # 忽略错误，继续后续流程
            
            # 发送文件接收信号（提前通知 UI 添加到忽略列表）
            self.file_receiving.emit(filepath)
            
            # 发送传输开始信号
            self.transfer_started.emit(filename, file_size, 'receive')
            self.log_message.emit(f"开始接收大文件: {filename} ({file_size / 1024 / 1024:.2f} MB)")
            
        except Exception as e:
            self.error_occurred.emit(f"准备接收大文件失败 {filename}: {e}")
    
    def _handle_file_data(self, filename: str, chunk_index: int, chunk_data: bytes):
        """处理大文件数据块"""
        import os
        
        try:
            if filename not in self.temp_file_info:
                return
            
            info = self.temp_file_info[filename]
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
    
    def _handle_file_end(self, filename: str, file_size: int,
                         hide_from_others: bool, mtime: float):
        """处理大文件传输结束"""
        import os
        
        try:
            if filename not in self.temp_file_info:
                return
            
            info = self.temp_file_info[filename]
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
                del self.temp_file_info[filename]
                return
            
            # 重命名临时文件为正式文件（使用 os.replace 避免触发删除事件）
            filepath = os.path.join(self.sync_folder, filename)
            os.replace(temp_path, filepath)  # 直接替换，不会触发删除事件
            
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
            del self.temp_file_info[filename]
            
            # 发送传输结束信号
            self.transfer_finished.emit(filename, 'receive')
            self.file_received.emit(filename, file_size)
            self.log_message.emit(f"大文件接收完成: {filename}")
            
        except Exception as e:
            self.error_occurred.emit(f"完成接收大文件失败 {filename}: {e}")
    
    def _handle_file_ack(self, filename: str, chunk_index: int, received_bytes: int):
        """处理数据块确认（流控）"""
        try:
            with self.transfer_lock:
                # 更新确认状态
                if filename in self.send_state:
                    self.send_state[filename]['acked_index'] = chunk_index
                
                # 触发确认事件
                if filename in self.ack_events:
                    self.ack_events[filename].set()
        except Exception as e:
            self.log_message.emit(f"处理确认消息失败 {filename}: {e}")
    
    def _handle_file_cancel(self, filename: str, reason: bytes):
        """处理文件传输取消"""
        try:
            reason_text = reason.decode('utf-8') if reason else ''
            self.log_message.emit(f"服务器取消传输: {filename} ({reason_text})")
            
            # 清理发送状态
            with self.transfer_lock:
                self.send_state.pop(filename, None)
                if filename in self.ack_events:
                    self.ack_events[filename].set()
                    self.ack_events.pop(filename, None)
        except Exception as e:
            self.log_message.emit(f"处理取消消息失败 {filename}: {e}")
    
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
        
        try:
            dirpath = os.path.join(self.sync_folder, dirname)
            
            if not os.path.exists(dirpath):
                os.makedirs(dirpath, exist_ok=True)
            
            self.log_message.emit(f"创建目录: {dirname}")
            
        except Exception as e:
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