# -*- coding: utf-8 -*-
"""
LANSyncBox 局域网主机发现模块
使用UDP广播发现局域网内的主机
"""

import socket
import threading
import json
import time
from typing import Dict, Optional, Callable
from PyQt5.QtCore import QObject, pyqtSignal

from config import DEFAULT_PORT


class HostDiscovery(QObject):
    """主机发现服务"""
    
    # 信号定义
    host_found = pyqtSignal(str, str)  # 发现主机 (ip, room_code)
    discovery_finished = pyqtSignal(list)  # 发现完成 (hosts列表)
    error_occurred = pyqtSignal(str)  # 错误消息
    
    # UDP端口
    DISCOVERY_PORT = 9528  # 发现端口（与同步端口区分）
    BROADCAST_INTERVAL = 1  # 广播间隔（秒）
    DISCOVERY_TIMEOUT = 3  # 发现超时（秒）
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.discovered_hosts: Dict[str, dict] = {}  # {ip: {room_code, port, timestamp}}
        self._lock = threading.Lock()
    
    def start_discovery(self, room_code: str = None, timeout: int = None) -> bool:
        """
        启动主机发现
        Args:
            room_code: 指定房间号（可选，如果指定则只发现该房间）
            timeout: 发现超时时间（秒）
        Returns:
            是否启动成功
        """
        try:
            timeout = timeout or self.DISCOVERY_TIMEOUT
            self.discovered_hosts.clear()
            
            # 创建UDP socket（发送端使用随机端口，避免端口冲突）
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 发送端绑定到随机端口，不绑定到DISCOVERY_PORT
            self.socket.bind(('0.0.0.0', 0))  # 使用随机端口
            self.socket.settimeout(0.5)
            
            self.running = True
            
            # 启动接收线程
            receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            receive_thread.start()
            
            # 发送发现请求
            discovery_msg = json.dumps({
                'type': 'discovery_request',
                'room_code': room_code
            }).encode('utf-8')
            
            # 发送到广播地址
            self.socket.sendto(discovery_msg, ('<broadcast>', self.DISCOVERY_PORT))
            
            # 同时发送到本机地址（解决同一台机器双开无法发现的问题）
            self.socket.sendto(discovery_msg, ('127.0.0.1', self.DISCOVERY_PORT))
            
            # 获取本机IP并发送到本机IP
            try:
                local_ip = self._get_local_ip()
                if local_ip and local_ip != '127.0.0.1':
                    self.socket.sendto(discovery_msg, (local_ip, self.DISCOVERY_PORT))
            except Exception:
                pass
            
            # 设置超时结束
            timer = threading.Timer(timeout, self._finish_discovery)
            timer.start()
            
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"启动发现失败: {e}")
            return False
    
    def _get_local_ip(self) -> str:
        """获取本机IP地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
    
    def stop_discovery(self):
        """停止发现"""
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None
    
    def _receive_loop(self):
        """接收响应循环"""
        while self.running:
            try:
                data, addr = self.socket.recvfrom(1024)
                response = json.loads(data.decode('utf-8'))
                
                if response.get('type') == 'discovery_response':
                    host_ip = addr[0]
                    room_code = response.get('room_code')
                    port = response.get('port', DEFAULT_PORT)
                    
                    with self._lock:
                        self.discovered_hosts[host_ip] = {
                            'room_code': room_code,
                            'port': port,
                            'timestamp': time.time()
                        }
                    
                    self.host_found.emit(host_ip, room_code)
                    
            except socket.timeout:
                continue
            except Exception:
                continue
    
    def _finish_discovery(self):
        """完成发现"""
        self.stop_discovery()
        
        with self._lock:
            hosts = [
                {
                    'ip': ip,
                    'room_code': info['room_code'],
                    'port': info['port']
                }
                for ip, info in self.discovered_hosts.items()
            ]
        
        self.discovery_finished.emit(hosts)
    
    def get_discovered_hosts(self) -> list:
        """获取已发现的主机列表"""
        with self._lock:
            return [
                {
                    'ip': ip,
                    'room_code': info['room_code'],
                    'port': info['port']
                }
                for ip, info in self.discovered_hosts.items()
            ]


class HostResponder(QObject):
    """主机响应服务（主机端运行，响应发现请求）"""
    
    # 信号定义
    error_occurred = pyqtSignal(str)
    
    DISCOVERY_PORT = 9528
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.room_code = ""
        self.port = DEFAULT_PORT
    
    def start(self, room_code: str, port: int = DEFAULT_PORT) -> bool:
        """
        启动响应服务
        Args:
            room_code: 房间号
            port: 同步端口
        Returns:
            是否启动成功
        """
        try:
            self.room_code = room_code
            self.port = port
            
            # 创建UDP socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(('0.0.0.0', self.DISCOVERY_PORT))
            self.socket.settimeout(1.0)
            
            self.running = True
            
            # 启动响应线程
            response_thread = threading.Thread(target=self._response_loop, daemon=True)
            response_thread.start()
            
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"启动响应服务失败: {e}")
            return False
    
    def stop(self):
        """停止响应服务"""
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None
    
    def _response_loop(self):
        """响应发现请求循环"""
        while self.running:
            try:
                data, addr = self.socket.recvfrom(1024)
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
                    
                    self.socket.sendto(response, addr)
                    
            except socket.timeout:
                continue
            except Exception:
                continue