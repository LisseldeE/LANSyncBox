# -*- coding: utf-8 -*-
"""
LANSyncBox 自定义传输协议
协议格式: [消息类型(4B)][文件名长度(4B)][文件大小(8B)][隐藏标记(1B)][文件名+内容]
"""

import struct
import os
from config import (
    MSG_TYPE_FILE, MSG_TYPE_DELETE, MSG_TYPE_AUTH_REQ, MSG_TYPE_AUTH_RESP,
    MSG_TYPE_FILE_LIST_REQ, MSG_TYPE_FILE_LIST_RESP, MSG_TYPE_HEARTBEAT,
    MSG_TYPE_FULL_SYNC_REQ, MSG_TYPE_FULL_SYNC_RESP, MSG_TYPE_CLIENT_INFO,
    MSG_TYPE_DIR_CREATE, BUFFER_SIZE
)


class Protocol:
    """自定义文件传输协议"""
    
    # 消息头格式: 类型(4B) + 文件名长度(4B) + 文件大小(8B) + 修改时间(8B) + 隐藏标记(1B) = 25字节
    HEADER_FORMAT = '!I I Q d B'  # 网络字节序, 无符号整型, 无符号整型, 无符号长整型, double(时间戳), 无符号字符
    HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
    
    @staticmethod
    def pack_message(msg_type: int, filename: str = '', file_size: int = 0,
                     hide_from_others: bool = False, content: bytes = b'',
                     mtime: float = 0.0) -> bytes:
        """
        打包消息
        Args:
            msg_type: 消息类型
            filename: 文件名（相对路径）
            file_size: 文件大小
            hide_from_others: 是否对外隐藏
            content: 文件内容
            mtime: 文件修改时间戳
        Returns:
            打包后的字节数据
        """
        filename_bytes = filename.encode('utf-8')
        hide_flag = 1 if hide_from_others else 0
        
        header = struct.pack(
            Protocol.HEADER_FORMAT,
            msg_type,
            len(filename_bytes),
            file_size,
            mtime,
            hide_flag
        )
        
        return header + filename_bytes + content
    
    @staticmethod
    def unpack_header(header_data: bytes) -> tuple:
        """
        解包消息头
        Args:
            header_data: 25字节的头部数据
        Returns:
            (msg_type, filename_length, file_size, mtime, hide_from_others)
        """
        if len(header_data) < Protocol.HEADER_SIZE:
            raise ValueError(f"头部数据不足: 需要{Protocol.HEADER_SIZE}字节, 实际{len(header_data)}字节")
        
        msg_type, filename_len, file_size, mtime, hide_flag = struct.unpack(
            Protocol.HEADER_FORMAT, header_data[:Protocol.HEADER_SIZE]
        )
        
        return msg_type, filename_len, file_size, mtime, bool(hide_flag)
    
    @staticmethod
    def create_file_message(filepath: str, base_dir: str, hide_from_others: bool = False) -> bytes:
        """
        创建文件传输消息
        Args:
            filepath: 文件绝对路径
            base_dir: 同步文件夹根目录
            hide_from_others: 是否对外隐藏
        Returns:
            打包后的消息
        """
        # 获取相对路径
        rel_path = os.path.relpath(filepath, base_dir)
        file_size = os.path.getsize(filepath)
        # 获取文件修改时间
        mtime = os.path.getmtime(filepath)
        
        # 读取文件内容
        with open(filepath, 'rb') as f:
            content = f.read()
        
        return Protocol.pack_message(
            MSG_TYPE_FILE, rel_path, file_size, hide_from_others, content, mtime
        )
    
    @staticmethod
    def create_delete_message(filepath: str, base_dir: str) -> bytes:
        """
        创建删除指令消息
        Args:
            filepath: 被删除文件的绝对路径
            base_dir: 同步文件夹根目录
        Returns:
            打包后的消息
        """
        rel_path = os.path.relpath(filepath, base_dir)
        return Protocol.pack_message(MSG_TYPE_DELETE, rel_path)
    
    @staticmethod
    def create_auth_request(room_code: str, password: str = '') -> bytes:
        """
        创建房间验证请求
        Args:
            room_code: 房间号
            password: 密码（可选）
        Returns:
            打包后的消息
        """
        import hashlib
        password_hash = hashlib.sha256(password.encode()).hexdigest() if password else ''
        content = f"{room_code}:{password_hash}".encode('utf-8')
        return Protocol.pack_message(MSG_TYPE_AUTH_REQ, '', len(content), False, content)
    
    @staticmethod
    def create_auth_response(success: bool, message: str = '', allow_peer_sync: bool = False) -> bytes:
        """
        创建房间验证响应
        Args:
            success: 是否验证成功
            message: 附加消息
            allow_peer_sync: 是否允许连接端互相同步
        Returns:
            打包后的消息
        """
        # 格式: success:message:allow_peer_sync
        content = f"{'1' if success else '0'}:{message}:{'1' if allow_peer_sync else '0'}".encode('utf-8')
        return Protocol.pack_message(MSG_TYPE_AUTH_RESP, '', len(content), False, content)
    
    @staticmethod
    def create_heartbeat() -> bytes:
        """创建心跳包"""
        return Protocol.pack_message(MSG_TYPE_HEARTBEAT)
    
    @staticmethod
    def create_file_list_request() -> bytes:
        """创建文件列表请求"""
        return Protocol.pack_message(MSG_TYPE_FILE_LIST_REQ)
    
    @staticmethod
    def create_file_list_response(file_list: list) -> bytes:
        """
        创建文件列表响应
        Args:
            file_list: 文件列表 [(相对路径, 大小, 修改时间), ...]
        Returns:
            打包后的消息
        """
        import json
        content = json.dumps(file_list).encode('utf-8')
        return Protocol.pack_message(MSG_TYPE_FILE_LIST_RESP, '', len(content), False, content)
    
    @staticmethod
    def create_full_sync_request() -> bytes:
        """创建全量同步请求"""
        return Protocol.pack_message(MSG_TYPE_FULL_SYNC_REQ)
    
    @staticmethod
    def create_client_info_update(hide_from_others: bool) -> bytes:
        """
        创建客户端信息更新消息
        Args:
            hide_from_others: 是否对外隐藏
        Returns:
            打包后的消息
        """
        content = b'1' if hide_from_others else b'0'
        return Protocol.pack_message(MSG_TYPE_CLIENT_INFO, '', 1, hide_from_others, content)
    
    @staticmethod
    def create_dir_create_message(dirpath: str, base_dir: str) -> bytes:
        """
        创建目录创建消息
        Args:
            dirpath: 目录绝对路径
            base_dir: 同步文件夹根目录
        Returns:
            打包后的消息
        """
        rel_path = os.path.relpath(dirpath, base_dir)
        return Protocol.pack_message(MSG_TYPE_DIR_CREATE, rel_path)


class MessageReceiver:
    """消息接收器 - 处理TCP流式数据的分包"""
    
    def __init__(self):
        self.buffer = b''
    
    def feed(self, data: bytes):
        """添加接收到的数据"""
        self.buffer += data
    
    def has_complete_message(self) -> bool:
        """检查是否有完整的消息"""
        if len(self.buffer) < Protocol.HEADER_SIZE:
            return False
        
        _, filename_len, file_size, _, _ = Protocol.unpack_header(self.buffer)
        return len(self.buffer) >= Protocol.HEADER_SIZE + filename_len + file_size
    
    def get_message(self) -> tuple:
        """
        获取一条完整消息
        Returns:
            (msg_type, filename, file_size, mtime, hide_from_others, content)
        """
        if not self.has_complete_message():
            return None
        
        # 解析头部
        msg_type, filename_len, file_size, mtime, hide_flag = Protocol.unpack_header(self.buffer)
        
        # 提取文件名和内容
        filename = self.buffer[Protocol.HEADER_SIZE:Protocol.HEADER_SIZE + filename_len].decode('utf-8')
        content_start = Protocol.HEADER_SIZE + filename_len
        content_end = content_start + file_size
        content = self.buffer[content_start:content_end]
        
        # 移除已处理的数据
        self.buffer = self.buffer[content_end:]
        
        return msg_type, filename, file_size, mtime, hide_flag, content
    
    def clear(self):
        """清空缓冲区"""
        self.buffer = b''