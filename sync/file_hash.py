# -*- coding: utf-8 -*-
"""
LANSyncBox 文件哈希缓存模块
管理文件哈希值缓存，用于快速判断文件是否变化
"""

import os
import hashlib
import time
from typing import Dict, Optional, Tuple
from dataclasses import dataclass


@dataclass
class FileInfo:
    """文件信息"""
    path: str           # 相对路径
    hash: str           # 内容哈希（MD5）
    size: int           # 文件大小
    mtime: float        # 修改时间
    last_sync_time: float = 0.0  # 最后同步时间
    last_op_id: str = ""         # 最后操作ID


class FileHashCache:
    """文件哈希缓存"""
    
    def __init__(self):
        self._cache: Dict[str, FileInfo] = {}  # {rel_path: FileInfo}
        self._sync_folder: str = ""
    
    def set_sync_folder(self, folder: str):
        """设置同步文件夹"""
        self._sync_folder = folder
        self.rebuild_index()
    
    def rebuild_index(self):
        """重建文件索引（扫描所有文件）"""
        self._cache.clear()
        
        if not self._sync_folder or not os.path.isdir(self._sync_folder):
            return
        
        for root, dirs, files in os.walk(self._sync_folder):
            for filename in files:
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, self._sync_folder)
                
                # 标准化路径：统一使用正斜杠
                normalized_path = rel_path.replace('\\', '/')
                
                # 忽略临时文件
                from sync.operation import should_ignore_path
                if should_ignore_path(normalized_path):
                    continue
                
                # 计算哈希
                hash_value, size, mtime = self._calculate_file_info(filepath)
                if hash_value:
                    self._cache[normalized_path] = FileInfo(
                        path=normalized_path,
                        hash=hash_value,
                        size=size,
                        mtime=mtime
                    )
    
    def get_file_info(self, rel_path: str) -> Optional[FileInfo]:
        """获取文件信息（标准化路径格式）"""
        normalized_path = rel_path.replace('\\', '/')
        return self._cache.get(normalized_path)
    
    def has_file(self, rel_path: str) -> bool:
        """检查文件是否存在（在索引中）"""
        normalized_path = rel_path.replace('\\', '/')
        return normalized_path in self._cache
    
    def update_file_info(self, rel_path: str, hash_value: str = None,
                         size: int = None, mtime: float = None,
                         last_sync_time: float = None, last_op_id: str = None):
        """更新文件信息（标准化路径格式）"""
        # 标准化路径：统一使用正斜杠
        normalized_path = rel_path.replace('\\', '/')
        
        if normalized_path not in self._cache:
            # 新文件，需要完整信息
            if hash_value is None:
                filepath = os.path.join(self._sync_folder, normalized_path)
                if os.path.exists(filepath):
                    hash_value, size, mtime = self._calculate_file_info(filepath)
            
            if hash_value:
                self._cache[normalized_path] = FileInfo(
                    path=normalized_path,
                    hash=hash_value,
                    size=size or 0,
                    mtime=mtime or time.time()
                )
        else:
            # 更新现有信息
            info = self._cache[normalized_path]
            if hash_value is not None:
                info.hash = hash_value
            if size is not None:
                info.size = size
            if mtime is not None:
                info.mtime = mtime
            if last_sync_time is not None:
                info.last_sync_time = last_sync_time
            if last_op_id is not None:
                info.last_op_id = last_op_id
    
    def remove_file(self, rel_path: str):
        """从索引中移除文件"""
        normalized_path = rel_path.replace('\\', '/')
        if normalized_path in self._cache:
            del self._cache[normalized_path]
    
    def file_has_changed(self, rel_path: str, new_size: int = None,
                          new_mtime: float = None) -> bool:
        """
        检查文件是否变化（标准化路径格式）
        Args:
            rel_path: 相对路径
            new_size: 新文件大小（可选，用于快速判断）
            new_mtime: 新修改时间（可选，用于快速判断）
        Returns:
            True=已变化，False=未变化
        """
        # 标准化路径：统一使用正斜杠
        normalized_path = rel_path.replace('\\', '/')
        
        filepath = os.path.join(self._sync_folder, normalized_path)
        
        # 文件不存在
        if not os.path.exists(filepath):
            # 如果索引中有记录，说明被删除了
            return normalized_path in self._cache
        
        # 获取当前文件信息
        current_size = os.path.getsize(filepath)
        current_mtime = os.path.getmtime(filepath)
        
        # 如果提供了新信息，直接使用
        if new_size is not None:
            current_size = new_size
        if new_mtime is not None:
            current_mtime = new_mtime
        
        # 检查索引
        cached_info = self._cache.get(normalized_path)
        
        if not cached_info:
            # 索引中没有，是新文件
            return True
        
        # 快速判断：大小或修改时间变化
        if current_size != cached_info.size or current_mtime != cached_info.mtime:
            # 需要重新计算哈希确认
            new_hash = self._calculate_hash(filepath)
            return new_hash != cached_info.hash
        
        # 大小和时间都没变，认为未变化
        return False
    
    def get_all_files(self) -> Dict[str, FileInfo]:
        """获取所有文件信息"""
        return self._cache.copy()
    
    def get_missing_files(self, remote_files: Dict[str, FileInfo]) -> list:
        """
        获取本地缺失的文件
        Args:
            remote_files: 远程文件索引
        Returns:
            缺失的文件路径列表
        """
        missing = []
        for rel_path in remote_files:
            if rel_path not in self._cache:
                missing.append(rel_path)
        return missing
    
    def get_extra_files(self, remote_files: Dict[str, FileInfo]) -> list:
        """
        获取本地多余的文件（远程没有）
        Args:
            remote_files: 远程文件索引
        Returns:
            多余的文件路径列表
        """
        extra = []
        for rel_path in self._cache:
            if rel_path not in remote_files:
                extra.append(rel_path)
        return extra
    
    def get_conflict_files(self, remote_files: Dict[str, FileInfo]) -> list:
        """
        获取冲突文件（双方都有但哈希不同）
        Args:
            remote_files: 远程文件索引
        Returns:
            冲突文件路径列表
        """
        conflicts = []
        for rel_path, remote_info in remote_files.items():
            if rel_path in self._cache:
                local_info = self._cache[rel_path]
                if local_info.hash != remote_info.hash:
                    conflicts.append(rel_path)
        return conflicts
    
    def _calculate_file_info(self, filepath: str) -> Tuple[str, int, float]:
        """
        计算文件信息（哈希、大小、修改时间）
        Args:
            filepath: 文件绝对路径
        Returns:
            (hash, size, mtime)
        """
        try:
            size = os.path.getsize(filepath)
            mtime = os.path.getmtime(filepath)
            hash_value = self._calculate_hash(filepath)
            return (hash_value, size, mtime)
        except Exception:
            return ("", 0, 0.0)
    
    def _calculate_hash(self, filepath: str, chunk_size: int = 65536) -> str:
        """
        计算文件哈希值（MD5）
        Args:
            filepath: 文件路径
            chunk_size: 分块大小（字节）
        Returns:
            MD5 哈希值（十六进制字符串）
        """
        try:
            hasher = hashlib.md5()
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception:
            return ""
    
    def calculate_hash_for_path(self, rel_path: str) -> str:
        """计算指定路径的哈希值（标准化路径格式）"""
        normalized_path = rel_path.replace('\\', '/')
        filepath = os.path.join(self._sync_folder, normalized_path)
        return self._calculate_hash(filepath)
    
    def get_file_size_mtime(self, rel_path: str) -> Tuple[int, float]:
        """获取文件大小和修改时间（标准化路径格式）"""
        normalized_path = rel_path.replace('\\', '/')
        filepath = os.path.join(self._sync_folder, normalized_path)
        try:
            return (os.path.getsize(filepath), os.path.getmtime(filepath))
        except Exception:
            return (0, 0.0)