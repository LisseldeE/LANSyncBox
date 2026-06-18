# -*- coding: utf-8 -*-
"""
LANSyncBox 文件传输管理模块
"""

import os
import hashlib
from typing import Dict, List, Tuple, Optional
from PyQt5.QtCore import QObject, pyqtSignal


class FileTransferManager(QObject):
    """文件传输管理器"""
    
    # 信号定义
    transfer_progress = pyqtSignal(str, int, int)  # 传输进度 (filename, current, total)
    transfer_complete = pyqtSignal(str, bool)  # 传输完成 (filename, success)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sync_folder = ""
        self._file_index: Dict[str, dict] = {}  # 文件索引 {相对路径: {size, mtime, hash}}
    
    def set_sync_folder(self, folder: str):
        """设置同步文件夹"""
        self.sync_folder = folder
        self._build_file_index()
    
    def _build_file_index(self):
        """构建文件索引"""
        self._file_index.clear()
        
        if not os.path.isdir(self.sync_folder):
            return
        
        for root, dirs, files in os.walk(self.sync_folder):
            for file in files:
                filepath = os.path.join(root, file)
                rel_path = os.path.relpath(filepath, self.sync_folder)
                
                try:
                    stat = os.stat(filepath)
                    self._file_index[rel_path] = {
                        'size': stat.st_size,
                        'mtime': stat.st_mtime,
                        'hash': self._calculate_file_hash(filepath)
                    }
                except Exception:
                    pass
    
    def _calculate_file_hash(self, filepath: str) -> str:
        """计算文件哈希值"""
        hasher = hashlib.md5()
        try:
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception:
            return ""
    
    def get_file_list(self) -> List[Tuple[str, int, float]]:
        """
        获取文件列表
        Returns:
            [(相对路径, 大小, 修改时间), ...]
        """
        result = []
        for rel_path, info in self._file_index.items():
            result.append((rel_path, info['size'], info['mtime']))
        return result
    
    def get_missing_files(self, remote_file_list: List[Tuple[str, int, float]]) -> List[str]:
        """
        获取本地缺失的文件
        Args:
            remote_file_list: 远程文件列表 [(相对路径, 大小, 修改时间), ...]
        Returns:
            缺失的文件路径列表
        """
        missing = []
        for rel_path, size, mtime in remote_file_list:
            if rel_path not in self._file_index:
                missing.append(rel_path)
        return missing
    
    def get_conflict_files(self, remote_file_list: List[Tuple[str, int, float]]) -> List[str]:
        """
        获取冲突文件（双方都有但修改时间不同）
        Args:
            remote_file_list: 远程文件列表
        Returns:
            冲突文件路径列表
        """
        conflicts = []
        for rel_path, size, mtime in remote_file_list:
            if rel_path in self._file_index:
                local_mtime = self._file_index[rel_path]['mtime']
                if abs(local_mtime - mtime) > 1:  # 超过1秒差异视为冲突
                    conflicts.append(rel_path)
        return conflicts
    
    def resolve_conflict(self, local_path: str, remote_path: str) -> str:
        """
        解决文件冲突
        Args:
            local_path: 本地文件路径
            remote_path: 远程文件路径（用于比较修改时间）
        Returns:
            'local' 或 'remote' 表示保留哪个版本
        """
        # 以修改时间较新的为准
        if not os.path.exists(local_path):
            return 'remote'
        
        local_mtime = os.path.getmtime(local_path)
        # 这里假设remote_mtime已经传入，实际使用时需要调整
        return 'local'  # 默认保留本地
    
    def update_file_index(self, rel_path: str, size: int = None, mtime: float = None):
        """更新文件索引"""
        if rel_path not in self._file_index:
            self._file_index[rel_path] = {}
        
        if size is not None:
            self._file_index[rel_path]['size'] = size
        if mtime is not None:
            self._file_index[rel_path]['mtime'] = mtime
        
        filepath = os.path.join(self.sync_folder, rel_path)
        if os.path.exists(filepath):
            self._file_index[rel_path]['hash'] = self._calculate_file_hash(filepath)
    
    def remove_from_index(self, rel_path: str):
        """从索引中移除文件"""
        if rel_path in self._file_index:
            del self._file_index[rel_path]
    
    def get_file_info(self, rel_path: str) -> Optional[dict]:
        """获取文件信息"""
        return self._file_index.get(rel_path)