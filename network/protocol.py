"""
同步协议
使用二进制协议进行高效文件传输
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import struct
import os
import json
import time
from typing import Tuple, Optional


class MessageType:
    """消息类型"""
    FILE = 0x01           # 文件传输（已废弃，保留向后兼容）
    DELETE = 0x02         # 删除指令
    AUTH_REQ = 0x03       # 验证请求
    AUTH_RESP = 0x04      # 验证响应
    FILE_LIST_REQ = 0x08  # 请求文件列表
    FILE_LIST_RESP = 0x09 # 响应文件列表（JSON格式）
    FILE_REQUEST = 0x0A   # 请求特定文件
    HEARTBEAT = 0x07      # 心跳
    FILE_BEGIN = 0x0C     # 文件传输开始（流式）
    FILE_DATA = 0x0D      # 文件数据块
    FILE_END = 0x0E       # 文件传输结束
    DIR_CREATE = 0x0B     # 目录创建
    FILE_CANCEL = 0x0F    # 取消文件传输
    FILE_NOTIFY = 0x10    # 文件通知（静默，通知连接端有新文件）
    FILE_REQUEST_FORWARD = 0x11  # 请求转发文件
    SYNC_REQUEST = 0x12   # 手动同步请求（主机→连接端，触发连接端重新上报并进行差异同步）
    SYNC_RESULT = 0x13    # 同步结果（主机→连接端，告知是否有差异，用于显示"一致/补齐"通知）
    PING = 0x14           # 延迟探测（发起端→对端，content 携带发送时刻，对端收到原样回传为 PONG）
    PONG = 0x15           # 延迟回包（对端→发起端，原样带回 PING 的发送时刻，发起端用于计算 RTT）
    CLIPBOARD_DATA = 0x16 # 剪切板文本内容（仅文本走系统剪贴板广播；图片/文件统一走文件 P2P 链路；filename 字段承载类型标识 "text"）
    CLIPBOARD_FILES_NOTIFY = 0x17  # 剪切板文件会话通知（复制端→主机→其余端；content=JSON 会话元数据）
    CLIPBOARD_FILE_PULL_REQ = 0x18 # 文件拉取请求（接收端→复制端目录端口；content=JSON {session_id,token,name,offset}）
    P2P_FILE_DATA = 0x19 # 分布式文件数据块（复制端→接收端 P2P；content=struct头部+原始字节）


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
    def create_auth_request(version: str, room_code: str, password: str = '') -> bytes:
        """创建验证请求（包含版本号）"""
        import hashlib
        password_hash = hashlib.sha256(password.encode()).hexdigest() if password else ''
        content = f"{version}:{room_code}:{password_hash}".encode('utf-8')
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
    
    @staticmethod
    def create_file_list_request() -> bytes:
        """创建文件列表请求消息"""
        return Protocol.pack_message(MessageType.FILE_LIST_REQ)
    
    @staticmethod
    def create_file_list_response(file_list: list) -> bytes:
        """创建文件列表响应消息
        
        Args:
            file_list: 文件列表，格式为 [{"filename": "test.txt", "size": 1024, "mtime": 1234567890.123}, ...]
        
        Returns:
            消息字节
        """
        content = json.dumps(file_list).encode('utf-8')
        return Protocol.pack_message(MessageType.FILE_LIST_RESP, '', len(content), False, content)
    
    @staticmethod
    def create_file_request(filename: str) -> bytes:
        """创建文件请求消息
        
        Args:
            filename: 请求的文件名（相对路径）
        
        Returns:
            消息字节
        """
        return Protocol.pack_message(MessageType.FILE_REQUEST, filename)
    
    @staticmethod
    def create_file_cancel(filename: str) -> bytes:
        """创建文件取消传输消息
        
        Args:
            filename: 要取消传输的文件名（相对路径）
        
        Returns:
            消息字节
        """
        return Protocol.pack_message(MessageType.FILE_CANCEL, filename)

    @staticmethod
    def create_sync_request() -> bytes:
        """创建手动同步请求消息（主机→连接端）
        
        Returns:
            消息字节
        """
        return Protocol.pack_message(MessageType.SYNC_REQUEST)

    @staticmethod
    def create_sync_result(has_diff: bool) -> bytes:
        """创建同步结果消息（主机→连接端，用于显示"一致/补齐"通知）
        
        Args:
            has_diff: 是否存在差异（True=正在补齐差异项，False=列表一致无需同步）
        
        Returns:
            消息字节
        """
        content = b'1' if has_diff else b'0'
        return Protocol.pack_message(MessageType.SYNC_RESULT, '', len(content), False, content)

    @staticmethod
    def create_ping(send_time: float) -> bytes:
        """创建延迟探测消息 PING（发起端→对端）

        Args:
            send_time: 本地发送时刻（秒，毫秒精度足够），原样回传用于计算 RTT

        Returns:
            消息字节
        """
        content = struct.pack('!d', send_time)
        return Protocol.pack_message(MessageType.PING, '', len(content), False, content)

    @staticmethod
    def create_pong(send_time: float) -> bytes:
        """创建延迟回包消息 PONG（对端→发起端，原样带回 PING 的发送时刻）

        Args:
            send_time: 从收到的 PING 解出的发送时刻

        Returns:
            消息字节
        """
        content = struct.pack('!d', send_time)
        return Protocol.pack_message(MessageType.PONG, '', len(content), False, content)

    @staticmethod
    def create_clipboard_message(mime_type: str, data: bytes) -> bytes:
        """创建剪切板内容消息（仅文本，由主机分发给各端；图片/文件走文件 P2P 链路）

        Args:
            mime_type: 内容类型标识，约定恒为 "text"
            data: 内容字节（文本为 utf-8 编码）

        Returns:
            消息字节
        """
        return Protocol.pack_message(
            MessageType.CLIPBOARD_DATA,
            filename=mime_type,
            file_size=len(data),
            content=data
        )

    # ---- 分布式文件会话 / P2P 直连 ----

    @staticmethod
    def create_files_notify(notify_dict: dict) -> bytes:
        """创建文件会话通知消息（复制端→主机→其余端）

        Args:
            notify_dict: 会话元数据（含 session_id/token/files/source_ip/source_port 等）

        Returns:
            消息字节
        """
        import json
        content = json.dumps(notify_dict, ensure_ascii=False).encode('utf-8')
        return Protocol.pack_message(
            MessageType.CLIPBOARD_FILES_NOTIFY,
            filename='clipboard_files',
            file_size=len(content),
            content=content
        )

    @staticmethod
    def create_pull_request(session_id: str, token: str, name: str, offset: int = 0) -> bytes:
        """创建文件拉取请求消息（接收端→复制端）

        Args:
            session_id: 会话标识
            token: 会话校验令牌
            name: 会话内的文件条目名
            offset: 起始字节偏移（断点/续传，阶段A固定为 0）

        Returns:
            消息字节
        """
        import json
        content = json.dumps({
            'session_id': session_id,
            'token': token,
            'name': name,
            'offset': int(offset),
        }, ensure_ascii=False).encode('utf-8')
        return Protocol.pack_message(
            MessageType.CLIPBOARD_FILE_PULL_REQ,
            filename=name,
            file_size=len(content),
            content=content
        )

    # P2P 分块负载的头部格式：offset(8B,有符号) + total_size(8B) + is_last(1B)
    P2P_HEADER_FORMAT = '!qQ?'
    P2P_HEADER_SIZE = struct.calcsize(P2P_HEADER_FORMAT)

    @staticmethod
    def create_p2p_data(name: str, offset: int, total_size: int, chunk: bytes, is_last: bool = False) -> bytes:
        """创建分布式文件数据块消息（复制端→接收端 P2P）

        Args:
            name: 会话内的文件条目名
            offset: 本块起始偏移
            total_size: 文件总大小
            chunk: 本块原始字节
            is_last: 是否最后一块

        Returns:
            消息字节
        """
        header = struct.pack(Protocol.P2P_HEADER_FORMAT, offset, total_size, is_last)
        content = header + chunk
        return Protocol.pack_message(
            MessageType.P2P_FILE_DATA,
            filename=name,
            file_size=len(content),  # 含头部：接收端据此读取完整 content 再切分
            content=content
        )

    @staticmethod
    def unpack_p2p_data(content: bytes):
        """解包 P2P 数据块：返回 (offset, total_size, is_last, payload)

        Args:
            content: create_p2p_data 的 content 字段

        Returns:
            (offset, total_size, is_last, payload)
        """
        offset, total_size, is_last = struct.unpack(Protocol.P2P_HEADER_FORMAT, content[:Protocol.P2P_HEADER_SIZE])
        return offset, total_size, is_last, content[Protocol.P2P_HEADER_SIZE:]


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

            # 这些消息类型没有 content（即使 file_size 参数不为 0）
            if msg_type in [MessageType.FILE_BEGIN, MessageType.FILE_END,
                           MessageType.FILE_LIST_REQ, MessageType.FILE_REQUEST,
                           MessageType.FILE_CANCEL, MessageType.FILE_NOTIFY,
                           MessageType.FILE_REQUEST_FORWARD, MessageType.SYNC_REQUEST]:
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
        if msg_type in [MessageType.FILE_BEGIN, MessageType.FILE_END,
                       MessageType.FILE_LIST_REQ, MessageType.FILE_REQUEST,
                       MessageType.FILE_CANCEL, MessageType.FILE_NOTIFY,
                       MessageType.FILE_REQUEST_FORWARD, MessageType.SYNC_REQUEST]:
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

        # 对于FILE_LIST_RESP消息，解析JSON
        if msg_type == MessageType.FILE_LIST_RESP:
            file_list = json.loads(content.decode('utf-8'))
            return msg_type, filename, file_size, mtime, hide_flag, file_list

        return msg_type, filename, file_size, mtime, hide_flag, content
    
    def clear(self):
        """清空缓冲区"""
        self.buffer = b''
