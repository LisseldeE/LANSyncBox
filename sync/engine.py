# -*- coding: utf-8 -*-
"""
LANSyncBox 同步引擎核心
管理操作队列、路径锁定、冲突解决
"""

import time
import threading
from typing import Set, List, Optional, Dict
from PyQt5.QtCore import QObject, pyqtSignal

from sync.operation import OpType, OpStatus, SyncOperation, OperationIDGenerator, should_ignore_path
from sync.file_hash import FileHashCache, FileInfo
from sync.rename_detector import RenameDetector, FileEvent
from sync.local_fs import LocalFS


# 队列最大长度
MAX_QUEUE_SIZE = 100


class SyncEngine(QObject):
    """
    同步引擎
    
    核心职责：
    1. 接收文件事件，生成同步操作
    2. 管理操作队列（合并、排序）
    3. 路径级锁定（防止循环同步）
    4. 冲突检测与解决
    5. 执行同步操作
    """
    
    # 信号定义
    operation_ready = pyqtSignal(object)  # SyncOperation 准备发送
    operation_complete = pyqtSignal(str, bool)  # (op_id, success)
    conflict_detected = pyqtSignal(object)  # SyncOperation 冲突
    error_occurred = pyqtSignal(str)  # 错误消息
    queue_changed = pyqtSignal(int)  # 队列长度变化
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 同步文件夹
        self._sync_folder: str = ""
        
        # 节点ID
        self._node_id: str = ""
        
        # 路径锁定集合（已弃用，使用同步文件集合代替）
        self._locked_paths: Set[str] = set()
        
        # 同步文件集合（正在同步的文件）
        self._syncing_files: Set[str] = set()
        
        # 操作队列
        self._operation_queue: List[SyncOperation] = []
        
        # 是否正在处理
        self._processing: bool = False
        
        # 文件哈希缓存
        self._file_hash_cache: FileHashCache = FileHashCache()
        
        # 重命名识别器
        self._rename_detector: RenameDetector = RenameDetector(self)
        self._rename_detector.operation_detected.connect(self._on_operation_detected)
        
        # 本地文件操作执行器
        self._local_fs: LocalFS = LocalFS(self)
        self._local_fs.operation_complete.connect(self._on_local_operation_complete)
        self._local_fs.error_occurred.connect(self.error_occurred)
        
        # 锁（保护队列和锁定集合）
        self._lock = threading.Lock()
        
        # 接收操作回调（用于网络层）
        self._send_operation_callback: Optional[callable] = None
    
    def initialize(self, sync_folder: str, node_id: str):
        """
        初始化同步引擎
        Args:
            sync_folder: 同步文件夹
            node_id: 节点ID
        """
        self._sync_folder = sync_folder
        self._node_id = node_id
        
        # 设置节点ID
        OperationIDGenerator.set_node_id(node_id)
        
        # 初始化文件哈希缓存
        self._file_hash_cache.set_sync_folder(sync_folder)
        
        # 初始化重命名识别器
        self._rename_detector.set_sync_folder(sync_folder)
        self._rename_detector.set_node_id(node_id)
        self._rename_detector.set_hash_cache(
            {path: info.hash for path, info in self._file_hash_cache.get_all_files().items()}
        )
        
        # 初始化本地文件操作执行器
        self._local_fs.set_sync_folder(sync_folder)
        self._local_fs.set_file_hash_cache(self._file_hash_cache)
    
    def set_send_operation_callback(self, callback: callable):
        """设置发送操作的回调函数（用于网络层）"""
        self._send_operation_callback = callback
    
    def on_file_event(self, event_type: str, path: str, is_dir: bool = False):
        """
        处理文件事件（来自 FileWatcher）
        Args:
            event_type: 事件类型 ('created', 'deleted', 'modified')
            path: 相对路径
            is_dir: 是否是目录
        """
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        
        # 检查是否应该忽略（临时文件）
        if should_ignore_path(normalized_path):
            return
        
        # 检查是否正在同步（远程写入）
        if self.is_syncing(normalized_path):
            # 正在同步，丢弃事件
            return
        
        # 获取文件信息
        size = 0
        hash_value = ""
        mtime = time.time()
        
        if event_type != 'deleted' and not is_dir:
            # 获取文件大小和哈希
            import os
            filepath = os.path.join(self._sync_folder, normalized_path)
            
            if os.path.exists(filepath):
                size = os.path.getsize(filepath)
                mtime = os.path.getmtime(filepath)
                hash_value = self._file_hash_cache.calculate_hash_for_path(normalized_path)
        
        # 创建事件
        event = FileEvent(
            event_type=event_type,
            path=normalized_path,
            timestamp=time.time(),
            size=size,
            hash=hash_value,
            is_dir=is_dir
        )
        
        # 推送到重命名识别器
        self._rename_detector.push_event(event)
    
    def _on_operation_detected(self, op: SyncOperation):
        """处理重命名识别器检测到的操作"""
        # 加入队列
        self.push_to_queue(op)
    
    def push_to_queue(self, op: SyncOperation):
        """
        将操作加入队列
        Args:
            op: 同步操作
        """
        with self._lock:
            # 检查队列长度
            if len(self._operation_queue) >= MAX_QUEUE_SIZE:
                # 队列满，丢弃最旧的操作
                self._operation_queue.pop(0)
            
            # 查找队列中是否有同路径操作
            merged = False
            for existing in self._operation_queue:
                if existing.is_same_path(op):
                    if existing.can_merge_with(op):
                        existing.merge_with(op)
                        merged = True
                        break
            
            if not merged:
                # 添加到队列
                self._operation_queue.append(op)
            
            self.queue_changed.emit(len(self._operation_queue))
        
        # 如果没有正在处理，立即开始
        if not self._processing:
            self.process_next()
    
    def process_next(self):
        """处理下一个操作"""
        with self._lock:
            if not self._operation_queue:
                self._processing = False
                return
            
            self._processing = True
            op = self._operation_queue.pop(0)
            self.queue_changed.emit(len(self._operation_queue))
        
        # 锁定路径
        self.lock_path(op.path)
        
        # 更新操作状态
        op.status = OpStatus.SYNCING
        
        # 发出操作准备信号
        self.operation_ready.emit(op)
        
        # 发送操作（通过回调）
        if self._send_operation_callback:
            self._send_operation_callback(op)
        
        # 对于 DELETE 操作，立即解锁（不需要等待传输完成）
        if op.op_type == OpType.DELETE:
            self.unlock_path(op.path)
            op.status = OpStatus.DONE
            self.operation_complete.emit(op.op_id, True)
        
        # 对于 CREATE/MODIFY 操作，不立即解锁
        # 等待传输完成后由 UI 调用 unlock_path
        
        # 继续处理下一个操作
        self._processing = False
        self.process_next()
    
    def lock_path(self, path: str):
        """锁定路径（已弃用，使用 mark_syncing 代替）"""
        import os
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        with self._lock:
            self._locked_paths.add(normalized_path)
    
    def unlock_path(self, path: str):
        """解锁路径（已弃用，使用 unmark_syncing 代替）"""
        import os
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        with self._lock:
            self._locked_paths.discard(normalized_path)
    
    def is_path_locked(self, path: str) -> bool:
        """检查路径是否锁定（已弃用，使用 is_syncing 代替）"""
        import os
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        with self._lock:
            return normalized_path in self._locked_paths
    
    def mark_syncing(self, path: str):
        """
        标记文件正在同步（发送或接收）
        用于区分本地操作和远程写入
        
        Args:
            path: 相对路径
        """
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        with self._lock:
            self._syncing_files.add(normalized_path)
    
    def unmark_syncing(self, path: str):
        """
        取消标记文件正在同步
        用于在同步完成后移除标记
        
        Args:
            path: 相对路径
        """
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        with self._lock:
            self._syncing_files.discard(normalized_path)
    
    def is_syncing(self, path: str) -> bool:
        """
        检查文件是否正在同步
        用于判断是否应该丢弃文件事件
        
        Args:
            path: 相对路径
        Returns:
            True: 正在同步（远程写入），应丢弃事件
            False: 本地操作，应处理事件
        """
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        with self._lock:
            return normalized_path in self._syncing_files
    
    def receive_operation(self, op: SyncOperation, content: bytes = None):
        """
        接收远程操作
        Args:
            op: 同步操作
            content: 文件内容（可选）
        """
        # 检查冲突
        if self._check_conflict(op):
            # 有冲突，发送冲突信号
            self.conflict_detected.emit(op)
            return
        
        # 锁定路径
        self.lock_path(op.path)
        
        # 执行操作
        op.status = OpStatus.EXECUTING
        success = self._local_fs.execute_operation(op, content)
        
        # 解锁
        self.unlock_path(op.path)
        
        # 更新状态
        op.status = OpStatus.DONE if success else OpStatus.FAILED
        self.operation_complete.emit(op.op_id, success)
    
    def _check_conflict(self, op: SyncOperation) -> bool:
        """
        检查是否有冲突
        Args:
            op: 接收的操作
        Returns:
            是否有冲突
        """
        # 获取本地文件信息
        local_info = self._file_hash_cache.get_file_info(op.path)
        
        if not local_info:
            # 本地没有该文件，无冲突
            return False
        
        # 如果本地文件正在被同步（锁定），忽略远程操作
        if self.is_path_locked(op.path):
            return True
        
        # 检查哈希是否相同
        if op.content_hash and local_info.hash == op.content_hash:
            # 内容相同，无冲突
            return False
        
        # 简化冲突检查：只在本地有未同步的修改时才认为有冲突
        # 如果本地文件的 last_sync_time 为 0，说明是新文件或从未同步过
        if local_info.last_sync_time == 0:
            return False
        
        # 如果本地文件在最后同步后有修改（mtime > last_sync_time）
        if local_info.mtime > local_info.last_sync_time:
            # 本地有未同步的修改，冲突
            return True
        
        return False
    
    def resolve_conflict(self, op: SyncOperation, local_info: FileInfo) -> str:
        """
        解决冲突
        Args:
            op: 远程操作
            local_info: 本地文件信息
        Returns:
            解决策略: 'accept_remote' 或 'keep_local'
        """
        # 简单规则：最后修改者获胜
        if op.timestamp > local_info.last_sync_time:
            return 'accept_remote'
        else:
            return 'keep_local'
    
    def _on_local_operation_complete(self, op_id: str, success: bool):
        """本地操作完成回调"""
        # 查找对应的操作
        # 这里需要从队列中找到对应的操作并解锁
        # 由于操作已经从队列中移除，这里不做处理
        pass
    
    def get_queue_length(self) -> int:
        """获取队列长度"""
        with self._lock:
            return len(self._operation_queue)
    
    def get_locked_paths(self) -> Set[str]:
        """获取锁定路径集合"""
        with self._lock:
            return self._locked_paths.copy()
    
    def get_file_index(self) -> Dict[str, FileInfo]:
        """获取文件索引"""
        return self._file_hash_cache.get_all_files()
    
    def mark_file_received(self, path: str, size: int, mtime: float):
        """
        标记文件已接收（立即更新哈希缓存）
        用于在文件接收完成时立即更新，避免循环触发
        
        Args:
            path: 相对路径
            size: 文件大小
            mtime: 文件修改时间
        """
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        
        # 计算哈希
        hash_value = self._file_hash_cache.calculate_hash_for_path(normalized_path)
        
        # 更新哈希缓存
        self._file_hash_cache.update_file_info(
            normalized_path,
            hash_value=hash_value,
            size=size,
            mtime=mtime,
            last_sync_time=time.time()
        )
        
        # 更新 rename_detector 的哈希缓存
        if hash_value:
            self._rename_detector._hash_cache[normalized_path] = hash_value
        
        # 解锁路径
        self.unlock_path(normalized_path)
    
    def mark_file_sent(self, path: str, size: int, mtime: float):
        """
        标记文件已发送（立即更新哈希缓存）
        用于在文件发送完成时立即更新，避免循环触发
        
        Args:
            path: 相对路径
            size: 文件大小
            mtime: 文件修改时间
        """
        # 标准化路径：统一使用正斜杠
        normalized_path = path.replace('\\', '/')
        
        # 计算哈希
        hash_value = self._file_hash_cache.calculate_hash_for_path(normalized_path)
        
        # 更新哈希缓存
        self._file_hash_cache.update_file_info(
            normalized_path,
            hash_value=hash_value,
            size=size,
            mtime=mtime,
            last_sync_time=time.time()
        )
        
        # 更新 rename_detector 的哈希缓存
        if hash_value:
            self._rename_detector._hash_cache[normalized_path] = hash_value
        
        # 解锁路径
        self.unlock_path(normalized_path)
    
    def force_process_pending(self):
        """强制处理所有待匹配事件（用于测试）"""
        self._rename_detector.force_process_pending()
    
    def rebuild_file_index(self):
        """重建文件索引"""
        self._file_hash_cache.rebuild_index()
        self._rename_detector.set_hash_cache(
            {path: info.hash for path, info in self._file_hash_cache.get_all_files().items()}
        )
    
    def clear(self):
        """清除所有状态"""
        with self._lock:
            self._operation_queue.clear()
            self._locked_paths.clear()
            self._processing = False
        
        self._rename_detector.clear()
    
    def stop(self):
        """停止同步引擎"""
        self.clear()
        
        # 等待当前操作完成
        # 这里可以添加等待逻辑