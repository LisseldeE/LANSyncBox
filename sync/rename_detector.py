# -*- coding: utf-8 -*-
"""
LANSyncBox 文件事件处理器
将文件事件转换为同步操作
"""

import time
from typing import Dict
from dataclasses import dataclass
from PyQt5.QtCore import QObject, pyqtSignal

from sync.operation import OpType, SyncOperation, OperationIDGenerator, should_ignore_path


@dataclass
class FileEvent:
    """原始文件事件"""
    event_type: str    # 'created', 'deleted', 'modified'
    path: str          # 相对路径
    timestamp: float   # 事件时间戳
    size: int = 0      # 文件大小
    hash: str = ""     # 文件哈希（如果有）
    is_dir: bool = False  # 是否是目录


class RenameDetector(QObject):
    """
    文件事件处理器
    
    工作原理：实时同步，不延迟
    - 删除事件 → 立即生成 DELETE 操作
    - 创建事件 → 立即生成 CREATE 操作
    - 修改事件 → 立即生成 MODIFY 操作
    
    重命名 = DELETE（旧文件） + CREATE（新文件）
    """
    
    # 信号定义
    operation_detected = pyqtSignal(object)  # SyncOperation 检测到操作
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 文件哈希缓存（用于传递给操作）
        self._hash_cache: Dict[str, str] = {}
        
        # 同步文件夹
        self._sync_folder: str = ""
        
        # 节点ID
        self._node_id: str = ""
    
    def set_sync_folder(self, folder: str):
        """设置同步文件夹"""
        self._sync_folder = folder
    
    def set_node_id(self, node_id: str):
        """设置节点ID"""
        self._node_id = node_id
        OperationIDGenerator.set_node_id(node_id)
    
    def set_hash_cache(self, cache: Dict[str, str]):
        """设置哈希缓存（来自 FileHashCache）"""
        self._hash_cache = cache
    
    def push_event(self, event: FileEvent):
        """
        推送文件事件
        Args:
            event: 文件事件
        """
        # 忽略临时文件
        if should_ignore_path(event.path):
            return
        
        if event.event_type == 'deleted':
            self._emit_delete_operation(event)
        elif event.event_type == 'created':
            self._emit_create_operation(event)
        elif event.event_type == 'modified':
            self._emit_modify_operation(event)
    
    def _emit_create_operation(self, event: FileEvent):
        """生成 CREATE 操作"""
        # 标准化路径：统一使用正斜杠
        normalized_path = event.path.replace('\\', '/')
        
        op = SyncOperation(
            op_id=OperationIDGenerator.generate(),
            op_type=OpType.CREATE,
            path=normalized_path,
            content_hash=event.hash,
            file_size=event.size,
            mtime=event.timestamp,
            is_dir=event.is_dir,
            source_node=self._node_id,
            timestamp=time.time()
        )
        self.operation_detected.emit(op)
    
    def _emit_delete_operation(self, event: FileEvent):
        """生成 DELETE 操作"""
        # 标准化路径：统一使用正斜杠
        normalized_path = event.path.replace('\\', '/')
        
        op = SyncOperation(
            op_id=OperationIDGenerator.generate(),
            op_type=OpType.DELETE,
            path=normalized_path,
            is_dir=event.is_dir,
            source_node=self._node_id,
            timestamp=time.time()
        )
        self.operation_detected.emit(op)
    
    def _emit_modify_operation(self, event: FileEvent):
        """生成 MODIFY 操作"""
        # 标准化路径：统一使用正斜杠
        normalized_path = event.path.replace('\\', '/')
        
        op = SyncOperation(
            op_id=OperationIDGenerator.generate(),
            op_type=OpType.MODIFY,
            path=normalized_path,
            content_hash=event.hash,
            file_size=event.size,
            mtime=event.timestamp,
            is_dir=event.is_dir,
            source_node=self._node_id,
            timestamp=time.time()
        )
        self.operation_detected.emit(op)
    
    def force_process_pending(self):
        """强制处理所有待匹配事件（用于测试）- 现在没有待匹配事件"""
        pass
    
    def clear(self):
        """清除所有状态"""
        pass
    
    def get_pending_count(self):
        """获取待匹配事件数量"""
        return (0, 0)