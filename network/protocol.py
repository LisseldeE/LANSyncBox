"""
同步协议
使用二进制协议进行高效文件传输
"""
import struct
import os
import json
import time
from typing import Tuple, Optional


class MessageType:
    """消息类型"""
    FILE = 0x01           # 文件传输（小文件一次性）
    DELETE = 0x02         # 删除指令
    AUTH_REQ = 0x03       # 验证请求
    AUTH_RESP = 0x04      # 验证响应
    HEARTBEAT = 0x07      # 心跳
    FILE_BEGIN = 0x0C     # 大文件传输开始
    FILE_DATA = 0x0D      # 大文件数据块
    FILE_END = 0x0E       # 大文件传输结束
    DIR_CREATE = 0x0B     # 目录创建


class Protocol:
    """自定义文件传输协议"""
    
    # 消息头格式: 类型(4B) + 文件名长度(4B) + 文件大小(8B) + 修改时间(8B) + 隐藏标记(1B) = 25字节
    HEADER_FORMAT = '!I I Q d B'
    HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
    
    @staticmethod
    def pack_message(msg_type: int, filename: str = '', file_size: int = 0,
                     hide_flag: bool = False, content: bytes = b'',
                     mtime: float = 0.0) -> bytes:
        """打包消息"""
        filename_bytes = filename.encode('utf-8')
        hide = 1 if hide_flag else 0
        
        header = struct.pack(
            Protocol.HEADER_FORMAT,
            msg_type,
            len(filename_bytes),
            file_size,
            mtime,
            hide
        )
        
        return header + filename_bytes + content
    
    @staticmethod
    def unpack_header(header_data: bytes) -> Tuple[int, int, int, float, bool]:
        """解包消息头"""
        msg_type, filename_len, file_size, mtime, hide_flag = struct.unpack(
            Protocol.HEADER_FORMAT, header_data[:Protocol.HEADER_SIZE]
        )
        return msg_type, filename_len, file_size, mtime, bool(hide_flag)
    
    @staticmethod
    def create_file_begin_message(filepath: str, base_dir: str) -> Tuple[bytes, int, float, str]:
        """创建大文件传输开始消息"""
        rel_path = os.path.relpath(filepath, base_dir).replace('\\', '/')
        file_size = os.path.getsize(filepath)
        mtime = os.path.getmtime(filepath)
        
        message = Protocol.pack_message(
            MessageType.FILE_BEGIN, rel_path, file_size, False, b'', mtime
        )
        
        return message, file_size, mtime, rel_path
    
    @staticmethod
    def create_file_data_message(rel_path: str, chunk_index: int, chunk_data: bytes) -> bytes:
        """创建文件数据块消息"""
        filename_bytes = rel_path.encode('utf-8')
        chunk_index_bytes = struct.pack('!I', chunk_index)
        
        header = struct.pack(
            Protocol.HEADER_FORMAT,
            MessageType.FILE_DATA,
            len(filename_bytes),
            len(chunk_data) + 4,
            0.0,
            0
        )
        
        return header + filename_bytes + chunk_index_bytes + chunk_data
    
    @staticmethod
    def create_file_end_message(rel_path: str, file_size: int, mtime: float) -> bytes:
        """创建文件传输结束消息"""
        return Protocol.pack_message(
            MessageType.FILE_END, rel_path, file_size, False, b'', mtime
        )
    
    @staticmethod
    def create_delete_message(filepath: str, base_dir: str) -> bytes:
        """创建删除指令消息"""
        rel_path = os.path.relpath(filepath, base_dir).replace('\\', '/')
        return Protocol.pack_message(MessageType.DELETE, rel_path)
    
    @staticmethod
    def create_rename_message(old_path: str, new_path: str, base_dir: str) -> bytes:
        """创建重命名消息"""
        old_rel = os.path.relpath(old_path, base_dir).replace('\\', '/')
        new_rel = os.path.relpath(new_path, base_dir).replace('\\', '/')
        content = f"{old_rel}|{new_rel}".encode('utf-8')
        return Protocol.pack_message(0x05, '', len(content), False, content)  # 0x05 = RENAME
    
    @staticmethod
    def create_auth_request(room_code: str, password: str = '') -> bytes:
        """创建验证请求"""
        import hashlib
        password_hash = hashlib.sha256(password.encode()).hexdigest() if password else ''
        content = f"{room_code}:{password_hash}".encode('utf-8')
        return Protocol.pack_message(MessageType.AUTH_REQ, '', len(content), False, content)
    
    @staticmethod
    def create_auth_response(success: bool, message: str = '') -> bytes:
        """创建验证响应"""
        content = f"{'1' if success else '0'}:{message}".encode('utf-8')
        return Protocol.pack_message(MessageType.AUTH_RESP, '', len(content), False, content)
    
    @staticmethod
    def create_heartbeat() -> bytes:
        """创建心跳包"""
        return Protocol.pack_message(MessageType.HEARTBEAT)
    
    @staticmethod
    def create_dir_create_message(dirpath: str, base_dir: str) -> bytes:
        """创建目录创建消息"""
        rel_path = os.path.relpath(dirpath, base_dir).replace('\\', '/')
        return Protocol.pack_message(MessageType.DIR_CREATE, rel_path)


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
        
        try:
            msg_type, filename_len, file_size, _, _ = Protocol.unpack_header(self.buffer)
            
            # FILE_BEGIN 和 FILE_END 消息没有 content
            if msg_type == MessageType.FILE_BEGIN or msg_type == MessageType.FILE_END:
                content_size = 0
            else:
                content_size = file_size
            
            required_size = Protocol.HEADER_SIZE + filename_len + content_size
            return len(self.buffer) >= required_size
        except Exception:
            return False
    
    def get_message(self) -> Optional[Tuple]:
        """获取一条完整消息"""
        if not self.has_complete_message():
            return None
        
        # 解析头部
        msg_type, filename_len, file_size, mtime, hide_flag = Protocol.unpack_header(self.buffer)
        
        # 根据消息类型判断 content 大小
        if msg_type == MessageType.FILE_BEGIN or msg_type == MessageType.FILE_END:
            content_size = 0
        else:
            content_size = file_size
        
        # 提取文件名和内容
        filename = self.buffer[Protocol.HEADER_SIZE:Protocol.HEADER_SIZE + filename_len].decode('utf-8')
        content_start = Protocol.HEADER_SIZE + filename_len
        content_end = content_start + content_size
        content = self.buffer[content_start:content_end]
        
        # 移除已处理的数据
        self.buffer = self.buffer[content_end:]
        
        # 对于FILE_DATA消息，解析块索引
        if msg_type == MessageType.FILE_DATA:
            chunk_index = struct.unpack('!I', content[:4])[0]
            chunk_data = content[4:]
            return msg_type, filename, file_size, mtime, hide_flag, (chunk_index, chunk_data)
        
        return msg_type, filename, file_size, mtime, hide_flag, content
    
    def clear(self):
        """清空缓冲区"""
        self.buffer = b''
