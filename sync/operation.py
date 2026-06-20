# -*- coding: utf-8 -*-
"""
LANSyncBox 同步操作定义
定义操作类型、操作状态和操作数据结构
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import IntEnum
import time


class OpType(IntEnum):
    """操作类型"""
    CREATE = 1   # 新建文件/目录
    MODIFY = 2   # 内容修改
    DELETE = 3   # 删除
    RENAME = 4   # 重命名/移动


class OpStatus(IntEnum):
    """操作状态"""
    PENDING = 0      # 待处理（在队列中）
    SYNCING = 1      # 正在同步（传输中）
    EXECUTING = 2    # 正在执行（写入文件）
    DONE = 3         # 完成
    FAILED = 4       # 失败
    CONFLICT = 5     # 冲突（需要解决）


@dataclass
class SyncOperation:
    """同步操作数据结构"""
    op_id: str                    # 全局唯一ID: "timestamp-nodeid-sequence"
    op_type: OpType               # 操作类型
    path: str                     # 相对路径
    old_path: Optional[str] = None  # 仅 RENAME 使用（旧路径）
    content_hash: Optional[str] = None  # 文件内容哈希（可选）
    file_size: int = 0            # 文件大小
    mtime: float = 0.0            # 修改时间
    is_dir: bool = False          # 是否是目录
    source_node: str = ""         # 发起节点ID
    timestamp: float = field(default_factory=time.time)  # 操作时间戳
    status: OpStatus = OpStatus.PENDING  # 操作状态
    
    def __post_init__(self):
        """初始化后处理"""
        # 确保 timestamp 有值
        if self.timestamp == 0:
            self.timestamp = time.time()
    
    def to_dict(self) -> dict:
        """转换为字典（用于序列化）"""
        return {
            'op_id': self.op_id,
            'op_type': int(self.op_type),
            'path': self.path,
            'old_path': self.old_path,
            'content_hash': self.content_hash,
            'file_size': self.file_size,
            'mtime': self.mtime,
            'is_dir': self.is_dir,
            'source_node': self.source_node,
            'timestamp': self.timestamp,
            'status': int(self.status)
        }
    
    @staticmethod
    def from_dict(data: dict) -> 'SyncOperation':
        """从字典创建（用于反序列化）"""
        return SyncOperation(
            op_id=data['op_id'],
            op_type=OpType(data['op_type']),
            path=data['path'],
            old_path=data.get('old_path'),
            content_hash=data.get('content_hash'),
            file_size=data.get('file_size', 0),
            mtime=data.get('mtime', 0.0),
            is_dir=data.get('is_dir', False),
            source_node=data.get('source_node', ''),
            timestamp=data.get('timestamp', time.time()),
            status=OpStatus(data.get('status', 0))
        )
    
    def is_same_path(self, other: 'SyncOperation') -> bool:
        """判断是否操作同一路径"""
        if self.op_type == OpType.RENAME:
            # RENAME 操作比较新路径
            return self.path == other.path or self.old_path == other.path
        return self.path == other.path
    
    def can_merge_with(self, other: 'SyncOperation') -> bool:
        """判断是否可以与另一个操作合并"""
        # 必须是同一路径
        if not self.is_same_path(other):
            return False
        
        # CREATE + CREATE 可以合并为最新的 CREATE（避免多次发送）
        if self.op_type == OpType.CREATE and other.op_type == OpType.CREATE:
            return True
        
        # CREATE + MODIFY 可以合并为 CREATE
        if self.op_type == OpType.CREATE and other.op_type == OpType.MODIFY:
            return True
        
        # MODIFY + MODIFY 可以合并为最新的 MODIFY
        if self.op_type == OpType.MODIFY and other.op_type == OpType.MODIFY:
            return True
        
        # 其他情况不合并
        return False
    
    def merge_with(self, other: 'SyncOperation'):
        """与另一个操作合并（更新为最新状态）"""
        if not self.can_merge_with(other):
            return
        
        # 更新内容信息
        self.content_hash = other.content_hash
        self.file_size = other.file_size
        self.mtime = other.mtime
        self.timestamp = other.timestamp
        
        # CREATE + MODIFY 保持 CREATE 类型
        # MODIFY + MODIFY 保持 MODIFY 类型


class OperationIDGenerator:
    """操作ID生成器"""
    
    _sequence: int = 0
    _node_id: str = ""
    
    @classmethod
    def set_node_id(cls, node_id: str):
        """设置节点ID"""
        cls._node_id = node_id
    
    @classmethod
    def generate(cls) -> str:
        """
        生成全局唯一操作ID
        格式: timestamp-nodeid-sequence
        示例: 1703123456.789-host1-001
        """
        timestamp = time.time()
        cls._sequence += 1
        return f"{timestamp:.3f}-{cls._node_id}-{cls._sequence:03d}"
    
    @classmethod
    def parse(cls, op_id: str) -> tuple:
        """
        解析操作ID
        Returns: (timestamp, node_id, sequence)
        """
        parts = op_id.split('-')
        if len(parts) != 3:
            return (0.0, '', 0)
        return (float(parts[0]), parts[1], int(parts[2]))


# 临时文件模式（需要忽略）
TEMP_FILE_PATTERNS = [
    '~*',       # Word 临时文件 ~wrd000.tmp
    '*.tmp',    # 通用临时文件
    '~$*',      # Excel 临时文件 ~$Book1.xlsx
    '.DS_Store', # macOS 系统文件
    'Thumbs.db', # Windows 缩略图缓存
]


def should_ignore_path(path: str) -> bool:
    """
    检查路径是否应该被忽略（临时文件等）
    Args:
        path: 文件路径（相对路径或绝对路径）
    Returns:
        是否应该忽略
    """
    import os
    import fnmatch
    
    filename = os.path.basename(path)
    
    for pattern in TEMP_FILE_PATTERNS:
        if fnmatch.fnmatch(filename, pattern):
            return True
    
    return False