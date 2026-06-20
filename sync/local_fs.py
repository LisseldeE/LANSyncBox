# -*- coding: utf-8 -*-
"""
LANSyncBox 本地文件操作模块
执行文件操作（创建、修改、删除、重命名）
"""

import os
import shutil
import time
from typing import Optional, Tuple
from PyQt5.QtCore import QObject, pyqtSignal

from sync.operation import OpType, SyncOperation
from sync.file_hash import FileHashCache


class LocalFS(QObject):
    """
    本地文件操作执行器
    
    负责执行同步操作，并更新文件索引
    """
    
    # 信号定义
    operation_complete = pyqtSignal(str, bool)  # (op_id, success)
    error_occurred = pyqtSignal(str)  # 错误消息
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self._sync_folder: str = ""
        self._file_hash_cache: Optional[FileHashCache] = None
    
    def set_sync_folder(self, folder: str):
        """设置同步文件夹"""
        self._sync_folder = folder
    
    def set_file_hash_cache(self, cache: FileHashCache):
        """设置文件哈希缓存"""
        self._file_hash_cache = cache
    
    def execute_operation(self, op: SyncOperation, content: bytes = None) -> bool:
        """
        执行同步操作
        Args:
            op: 同步操作
            content: 文件内容（可选，用于 CREATE/MODIFY）
        Returns:
            是否成功
        """
        try:
            success = False
            
            if op.op_type == OpType.CREATE:
                success = self._execute_create(op, content)
            elif op.op_type == OpType.MODIFY:
                success = self._execute_modify(op, content)
            elif op.op_type == OpType.DELETE:
                success = self._execute_delete(op)
            
            # 更新文件索引
            if success and self._file_hash_cache:
                self._update_file_index(op)
            
            self.operation_complete.emit(op.op_id, success)
            return success
            
        except Exception as e:
            self.error_occurred.emit(f"执行操作失败: {e}")
            self.operation_complete.emit(op.op_id, False)
            return False
    
    def _execute_create(self, op: SyncOperation, content: bytes = None) -> bool:
        """执行创建操作"""
        filepath = os.path.join(self._sync_folder, op.path)
        
        # 检查是否已存在
        if os.path.exists(filepath):
            # 已存在，转为修改
            return self._execute_modify(op, content)
        
        if op.is_dir:
            # 创建目录
            os.makedirs(filepath, exist_ok=True)
            return True
        else:
            # 创建文件
            # 确保父目录存在
            parent_dir = os.path.dirname(filepath)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            
            # 写入内容
            if content:
                with open(filepath, 'wb') as f:
                    f.write(content)
            else:
                # 空文件
                with open(filepath, 'wb') as f:
                    pass
            
            # 设置修改时间
            if op.mtime > 0:
                os.utime(filepath, (op.mtime, op.mtime))
            
            return True
    
    def _execute_modify(self, op: SyncOperation, content: bytes = None) -> bool:
        """执行修改操作"""
        filepath = os.path.join(self._sync_folder, op.path)
        
        # 检查是否存在
        if not os.path.exists(filepath):
            # 不存在，转为创建
            return self._execute_create(op, content)
        
        # 检查是否是目录
        if os.path.isdir(filepath):
            # 目录不能修改内容
            return True
        
        # 写入内容
        if content:
            # 确保文件可写
            if not os.access(filepath, os.W_OK):
                os.chmod(filepath, 0o666)
            
            with open(filepath, 'wb') as f:
                f.write(content)
        
        # 设置修改时间
        if op.mtime > 0:
            os.utime(filepath, (op.mtime, op.mtime))
        
        return True
    
    def _execute_delete(self, op: SyncOperation) -> bool:
        """执行删除操作"""
        filepath = os.path.join(self._sync_folder, op.path)
        
        # 检查是否存在
        if not os.path.exists(filepath):
            # 已不存在，忽略
            return True
        
        # 删除
        if op.is_dir or os.path.isdir(filepath):
            shutil.rmtree(filepath)
        else:
            os.remove(filepath)
        
        return True
    
    def _update_file_index(self, op: SyncOperation):
        """更新文件索引"""
        if not self._file_hash_cache:
            return
        
        if op.op_type == OpType.DELETE:
            # 删除操作，移除索引
            self._file_hash_cache.remove_file(op.path)
        
        else:
            # CREATE/MODIFY 操作，更新索引
            filepath = os.path.join(self._sync_folder, op.path)
            if os.path.exists(filepath):
                # 如果操作携带了哈希，直接使用
                if op.content_hash:
                    self._file_hash_cache.update_file_info(
                        op.path,
                        hash_value=op.content_hash,
                        size=op.file_size,
                        mtime=op.mtime,
                        last_sync_time=time.time(),
                        last_op_id=op.op_id
                    )
                else:
                    # 否则重新计算
                    hash_value = self._file_hash_cache.calculate_hash_for_path(op.path)
                    size, mtime = self._file_hash_cache.get_file_size_mtime(op.path)
                    self._file_hash_cache.update_file_info(
                        op.path,
                        hash_value=hash_value,
                        size=size,
                        mtime=mtime,
                        last_sync_time=time.time(),
                        last_op_id=op.op_id
                    )
    
    def read_file_content(self, rel_path: str) -> Optional[bytes]:
        """读取文件内容"""
        filepath = os.path.join(self._sync_folder, rel_path)
        
        try:
            with open(filepath, 'rb') as f:
                return f.read()
        except Exception:
            return None
    
    def get_file_info(self, rel_path: str) -> Tuple[int, float, str]:
        """
        获取文件信息
        Args:
            rel_path: 相对路径
        Returns:
            (size, mtime, hash)
        """
        filepath = os.path.join(self._sync_folder, rel_path)
        
        try:
            size = os.path.getsize(filepath)
            mtime = os.path.getmtime(filepath)
            
            # 计算哈希
            if self._file_hash_cache:
                hash_value = self._file_hash_cache.calculate_hash_for_path(rel_path)
            else:
                hash_value = self._calculate_hash(filepath)
            
            return (size, mtime, hash_value)
        except Exception:
            return (0, 0.0, "")
    
    def _calculate_hash(self, filepath: str) -> str:
        """计算文件哈希"""
        import hashlib
        try:
            hasher = hashlib.md5()
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception:
            return ""
    
    def file_exists(self, rel_path: str) -> bool:
        """检查文件是否存在"""
        filepath = os.path.join(self._sync_folder, rel_path)
        return os.path.exists(filepath)
    
    def is_directory(self, rel_path: str) -> bool:
        """检查是否是目录"""
        filepath = os.path.join(self._sync_folder, rel_path)
        return os.path.isdir(filepath)