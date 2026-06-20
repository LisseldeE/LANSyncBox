# -*- coding: utf-8 -*-
"""
LANSyncBox 房间管理模块
"""

import random
import hashlib
import threading
import socket
from typing import Dict, Optional
from PyQt5.QtCore import QObject, pyqtSignal

from config import ROOM_CODE_LENGTH


class Room:
    """房间信息"""
    
    def __init__(self, room_code: str, password_hash: str = "", sync_folder: str = "",
                 host_ip: str = ""):
        self.room_code = room_code
        self.password_hash = password_hash
        self.sync_folder = sync_folder
        self.host_ip = host_ip  # 主机IP地址
        self.clients: Dict[str, dict] = {}  # {client_id: {hide_from_others, address}}
        self.created_at = 0
        self.status = "waiting"  # waiting, syncing, closed


class RoomManager(QObject):
    """房间管理器"""
    
    # 信号定义
    room_created = pyqtSignal(str)  # 房间创建成功 (room_code)
    room_closed = pyqtSignal(str)  # 房间关闭 (room_code)
    error_occurred = pyqtSignal(str)  # 错误消息
    
    # 类级别的房间存储（单例模式）
    _instance = None
    _rooms: Dict[str, Room] = {}  # {room_code: Room}
    _ip_rooms: Dict[str, str] = {}  # {ip: room_code} 一个IP只能开一个房间
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, parent=None):
        super().__init__(parent)
        if not hasattr(self, '_initialized'):
            self._initialized = True
    
    @staticmethod
    def get_local_ip() -> str:
        """获取本机IP地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
    
    @staticmethod
    def generate_room_code() -> str:
        """
        生成随机房间号
        Returns:
            6位数字房间号
        """
        return ''.join([str(random.randint(0, 9)) for _ in range(ROOM_CODE_LENGTH)])
    
    @staticmethod
    def hash_password(password: str) -> str:
        """
        哈希密码
        Args:
            password: 明文密码
        Returns:
            哈希后的密码
        """
        if not password:
            return ""
        return hashlib.sha256(password.encode()).hexdigest()
    
    def create_room(self, sync_folder: str, password: str = "",
                    room_code: str = None) -> Optional[str]:
        """
        创建房间
        Args:
            sync_folder: 同步文件夹
            password: 密码（可选）
            room_code: 指定房间号（可选，如果不指定则自动生成）
        Returns:
            房间号，失败返回None
        """
        import time
        
        local_ip = RoomManager.get_local_ip()
        
        with RoomManager._lock:
            # 检查本机IP是否已经开了房间
            if local_ip in RoomManager._ip_rooms:
                existing_room_code = RoomManager._ip_rooms[local_ip]
                self.error_occurred.emit(f"本机已创建房间 {existing_room_code}，一个IP只能开一个房间")
                return None
            
            # 如果指定了房间号，使用它；否则生成新的
            if room_code:
                # 检查指定的房间号是否已被占用
                if room_code in RoomManager._rooms:
                    self.error_occurred.emit(f"房间号 {room_code} 已被占用")
                    return None
            else:
                # 尝试生成唯一房间号
                for _ in range(10):  # 最多尝试10次
                    room_code = RoomManager.generate_room_code()
                    if room_code not in RoomManager._rooms:
                        break
                else:
                    self.error_occurred.emit("无法生成唯一房间号，请稍后重试")
                    return None
            
            # 创建房间
            room = Room(
                room_code=room_code,
                password_hash=RoomManager.hash_password(password),
                sync_folder=sync_folder,
                host_ip=local_ip
            )
            room.created_at = time.time()
            room.status = "waiting"
            
            RoomManager._rooms[room_code] = room
            RoomManager._ip_rooms[local_ip] = room_code  # 记录IP与房间的映射
            self.room_created.emit(room_code)
            
            return room_code
    
    def verify_room(self, room_code: str, password: str = "") -> tuple:
        """
        验证房间
        Args:
            room_code: 房间号
            password: 密码
        Returns:
            (success, message, room)
        """
        with RoomManager._lock:
            room = RoomManager._rooms.get(room_code)
            
            if not room:
                return False, "房间不存在", None
            
            if room.status == "closed":
                return False, "房间已关闭", None
            
            # 验证密码
            if room.password_hash:
                input_hash = RoomManager.hash_password(password)
                if input_hash != room.password_hash:
                    return False, "密码错误", None
            
            return True, "验证成功", room
    
    def close_room(self, room_code: str):
        """
        关闭房间
        Args:
            room_code: 房间号
        """
        with RoomManager._lock:
            if room_code in RoomManager._rooms:
                room = RoomManager._rooms[room_code]
                # 移除IP映射
                if room.host_ip in RoomManager._ip_rooms:
                    del RoomManager._ip_rooms[room.host_ip]
                
                room.status = "closed"
                del RoomManager._rooms[room_code]
                self.room_closed.emit(room_code)
    
    def get_room(self, room_code: str) -> Optional[Room]:
        """
        获取房间信息
        Args:
            room_code: 房间号
        Returns:
            房间对象，不存在返回None
        """
        return RoomManager._rooms.get(room_code)
    
    def add_client_to_room(self, room_code: str, client_id: str,
                           address: tuple, hide_from_others: bool = False):
        """
        添加客户端到房间
        Args:
            room_code: 房间号
            client_id: 客户端ID
            address: 客户端地址
            hide_from_others: 是否对外隐藏
        """
        with RoomManager._lock:
            room = RoomManager._rooms.get(room_code)
            if room:
                room.clients[client_id] = {
                    'address': address,
                    'hide_from_others': hide_from_others
                }
                room.status = "syncing"
    
    def remove_client_from_room(self, room_code: str, client_id: str):
        """
        从房间移除客户端
        Args:
            room_code: 房间号
            client_id: 客户端ID
        """
        with RoomManager._lock:
            room = RoomManager._rooms.get(room_code)
            if room and client_id in room.clients:
                del room.clients[client_id]
    
    def update_client_status(self, room_code: str, client_id: str, hide_from_others: bool):
        """
        更新客户端状态
        Args:
            room_code: 房间号
            client_id: 客户端ID
            hide_from_others: 是否对外隐藏
        """
        with RoomManager._lock:
            room = RoomManager._rooms.get(room_code)
            if room and client_id in room.clients:
                room.clients[client_id]['hide_from_others'] = hide_from_others
    
    def get_room_clients(self, room_code: str) -> list:
        """
        获取房间内的客户端列表
        Args:
            room_code: 房间号
        Returns:
            客户端列表
        """
        room = RoomManager._rooms.get(room_code)
        if room:
            return [
                {
                    'client_id': client_id,
                    'address': info['address'],
                    'hide_from_others': info['hide_from_others']
                }
                for client_id, info in room.clients.items()
            ]
        return []
    
    def is_room_exists(self, room_code: str) -> bool:
        """
        检查房间是否存在
        Args:
            room_code: 房间号
        Returns:
            是否存在
        """
        return room_code in RoomManager._rooms
    
    def get_all_rooms(self) -> list:
        """获取所有房间"""
        with RoomManager._lock:
            return list(RoomManager._rooms.keys())
    
    def has_room_on_ip(self, ip: str) -> bool:
        """
        检查指定IP是否已经开了房间
        Args:
            ip: IP地址
        Returns:
            是否已开房间
        """
        return ip in RoomManager._ip_rooms
    
    def get_room_by_ip(self, ip: str) -> Optional[str]:
        """
        根据IP获取房间号
        Args:
            ip: IP地址
        Returns:
            房间号，不存在返回None
        """
        return RoomManager._ip_rooms.get(ip)