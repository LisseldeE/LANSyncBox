# -*- coding: utf-8 -*-
"""
LANSyncBox 自定义传输协议
协议格式: [消息类型(4B)][文件名长度(4B)][文件大小(8B)][隐藏标记(1B)][文件名+内容]
支持大文件分块传输：FILE_BEGIN -> FILE_DATA -> FILE_END
"""

import struct
import os
from config import (
    MSG_TYPE_FILE, MSG_TYPE_DELETE, MSG_TYPE_AUTH_REQ, MSG_TYPE_AUTH_RESP,
    MSG_TYPE_FILE_LIST_REQ, MSG_TYPE_FILE_LIST_RESP, MSG_TYPE_HEARTBEAT,
    MSG_TYPE_FULL_SYNC_REQ, MSG_TYPE_FULL_SYNC_RESP, MSG_TYPE_CLIENT_INFO,
    MSG_TYPE_DIR_CREATE, MSG_TYPE_FILE_BEGIN, MSG_TYPE_FILE_DATA, MSG_TYPE_FILE_END,
    MSG_TYPE_FILE_ACK, MSG_TYPE_FILE_CANCEL, MSG_TYPE_SYNC_REQUEST,
    BUFFER_SIZE, CHUNKED_TRANSFER_THRESHOLD, CHUNK_SIZE
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
        创建文件传输消息（用于小文件一次性传输）
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
    def create_file_begin_message(filepath: str, base_dir: str, 
                                   hide_from_others: bool = False) -> tuple:
        """
        创建大文件传输开始消息
        Args:
            filepath: 文件绝对路径
            base_dir: 同步文件夹根目录
            hide_from_others: 是否对外隐藏
        Returns:
            (消息, 文件大小, 修改时间, 相对路径)
        """
        rel_path = os.path.relpath(filepath, base_dir)
        file_size = os.path.getsize(filepath)
        mtime = os.path.getmtime(filepath)
        
        message = Protocol.pack_message(
            MSG_TYPE_FILE_BEGIN, rel_path, file_size, hide_from_others, b'', mtime
        )
        
        return message, file_size, mtime, rel_path
    
    @staticmethod
    def create_file_data_message(rel_path: str, chunk_index: int, 
                                  chunk_data: bytes) -> bytes:
        """
        创建文件数据块消息
        Args:
            rel_path: 文件相对路径
            chunk_index: 数据块索引（从0开始）
            chunk_data: 数据块内容
        Returns:
            打包后的消息
        """
        # 数据块消息格式：文件名 + 块索引(4B) + 数据
        filename_bytes = rel_path.encode('utf-8')
        chunk_index_bytes = struct.pack('!I', chunk_index)
        
        header = struct.pack(
            Protocol.HEADER_FORMAT,
            MSG_TYPE_FILE_DATA,
            len(filename_bytes),
            len(chunk_data) + 4,  # 数据大小 = 块数据 + 块索引
            0.0,  # mtime不适用
            0     # hide不适用
        )
        
        return header + filename_bytes + chunk_index_bytes + chunk_data
    
    @staticmethod
    def create_file_end_message(rel_path: str, file_size: int, 
                                 mtime: float, hide_from_others: bool) -> bytes:
        """
        创建文件传输结束消息
        Args:
            rel_path: 文件相对路径
            file_size: 文件总大小
            mtime: 文件修改时间
            hide_from_others: 是否对外隐藏
        Returns:
            打包后的消息
        """
        return Protocol.pack_message(
            MSG_TYPE_FILE_END, rel_path, file_size, hide_from_others, b'', mtime
        )
    
    @staticmethod
    def create_file_ack_message(rel_path: str, chunk_index: int, 
                                 received_bytes: int) -> bytes:
        """
        创建数据块确认消息（用于流控）
        Args:
            rel_path: 文件相对路径
            chunk_index: 已处理的数据块索引
            received_bytes: 已接收的总字节数
        Returns:
            打包后的消息
        """
        # 确认消息格式：文件名 + 块索引(4B) + 已接收字节数(8B)
        filename_bytes = rel_path.encode('utf-8')
        chunk_index_bytes = struct.pack('!I', chunk_index)
        received_bytes_bytes = struct.pack('!Q', received_bytes)
        
        header = struct.pack(
            Protocol.HEADER_FORMAT,
            MSG_TYPE_FILE_ACK,
            len(filename_bytes),
            12,  # 数据大小 = 块索引(4B) + 已接收字节数(8B)
            0.0,  # mtime不适用
            0     # hide不适用
        )
        
        return header + filename_bytes + chunk_index_bytes + received_bytes_bytes
    
    @staticmethod
    def create_file_cancel_message(rel_path: str, reason: str = '') -> bytes:
        """
        创建文件传输取消消息
        Args:
            rel_path: 文件相对路径
            reason: 取消原因
        Returns:
            打包后的消息
        """
        reason_bytes = reason.encode('utf-8')
        return Protocol.pack_message(
            MSG_TYPE_FILE_CANCEL, rel_path, len(reason_bytes), False, reason_bytes
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
    def create_auth_response(success: bool, message: str = '') -> bytes:
        """
        创建验证响应消息
        Args:
            success: 是否成功
            message: 消息内容
        Returns:
            打包后的消息
        """
        # 格式: success:message
        content = f"{'1' if success else '0'}:{message}".encode('utf-8')
        
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
    def create_sync_request(need_receive: list, need_send: list) -> bytes:
        """
        创建双向同步请求消息
        Args:
            need_receive: 需要从主机端接收的文件列表 [[path, size, mtime], ...]
            need_send: 需要发送到主机端的文件列表 [[path, size, mtime], ...]
        Returns:
            打包后的消息
        """
        import json
        content = json.dumps({
            'need_receive': need_receive,
            'need_send': need_send
        }).encode('utf-8')
        return Protocol.pack_message(MSG_TYPE_SYNC_REQUEST, '', len(content), False, content)
    
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
        
        try:
            msg_type, filename_len, file_size, _, _ = Protocol.unpack_header(self.buffer)
            
            # 根据消息类型判断消息长度
            # FILE_BEGIN (0x0C) 和 FILE_END (0x0E) 消息的 file_size 字段存储的是文件总大小，
            # 但消息本身不包含文件内容，所以消息长度应该是 HEADER_SIZE + filename_len + 0
            if msg_type == MSG_TYPE_FILE_BEGIN or msg_type == MSG_TYPE_FILE_END:
                content_size = 0  # 这些消息没有 content
            else:
                content_size = file_size  # 其他消息的 file_size 是 content 的大小
            
            required_size = Protocol.HEADER_SIZE + filename_len + content_size
            has_complete = len(self.buffer) >= required_size
            
            return has_complete
        except Exception as e:
            return False
    
    def get_message(self) -> tuple:
        """
        获取一条完整消息
        Returns:
            (msg_type, filename, file_size, mtime, hide_from_others, content)
            对于FILE_DATA消息，content包含(chunk_index, chunk_data)
        """
        if not self.has_complete_message():
            return None
        
        # 解析头部
        msg_type, filename_len, file_size, mtime, hide_flag = Protocol.unpack_header(self.buffer)
        
        # 根据消息类型判断 content 大小
        if msg_type == MSG_TYPE_FILE_BEGIN or msg_type == MSG_TYPE_FILE_END:
            content_size = 0  # 这些消息没有 content
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
        if msg_type == MSG_TYPE_FILE_DATA:
            chunk_index = struct.unpack('!I', content[:4])[0]
            chunk_data = content[4:]
            return msg_type, filename, file_size, mtime, hide_flag, (chunk_index, chunk_data)
        
        # 对于FILE_ACK消息，解析确认信息
        if msg_type == MSG_TYPE_FILE_ACK:
            chunk_index = struct.unpack('!I', content[:4])[0]
            received_bytes = struct.unpack('!Q', content[4:12])[0]
            return msg_type, filename, file_size, mtime, hide_flag, (chunk_index, received_bytes)
        
        return msg_type, filename, file_size, mtime, hide_flag, content
    
    def clear(self):
        """清空缓冲区"""
        self.buffer = b''