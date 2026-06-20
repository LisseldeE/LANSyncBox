"""
文件管理器
负责本地文件操作和管理
"""
import os
import hashlib
import shutil
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

from config import Config


class FileManager:
    """文件管理器"""
    
    def __init__(self, folder_path: Path):
        self.folder_path = folder_path
    
    def get_file_list(self) -> List[Dict]:
        """获取文件列表"""
        files = []
        
        if not self.folder_path.exists():
            return files
        
        for item in self.folder_path.rglob('*'):
            if item.is_file():
                file_info = {
                    'path': str(item.relative_to(self.folder_path)),
                    'name': item.name,
                    'size': item.stat().st_size,
                    'mtime': item.stat().st_mtime,
                    'hash': self.calculate_file_hash(item)
                }
                files.append(file_info)
        
        return files
    
    def calculate_file_hash(self, file_path: Path) -> str:
        """计算文件哈希值"""
        hasher = hashlib.md5()
        
        try:
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception:
            return ""
    
    def add_file(self, src_path: Path, dst_path: Optional[Path] = None) -> bool:
        """添加文件"""
        if not src_path.exists():
            return False
        
        if dst_path is None:
            dst_path = self.folder_path / src_path.name
        
        try:
            if src_path.is_dir():
                shutil.copytree(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)
            return True
        except Exception:
            return False
    
    def delete_file(self, file_path: Path) -> bool:
        """删除文件"""
        try:
            if file_path.is_dir():
                shutil.rmtree(file_path)
            else:
                file.unlink(file_path)
            return True
        except Exception:
            return False
    
    def rename_file(self, old_path: Path, new_path: Path) -> bool:
        """重命名文件"""
        try:
            old_path.rename(new_path)
            return True
        except Exception:
            return False
    
    def move_file(self, src_path: Path, dst_path: Path) -> bool:
        """移动文件"""
        try:
            shutil.move(str(src_path), str(dst_path))
            return True
        except Exception:
            return False
    
    def get_file_info(self, file_path: Path) -> Optional[Dict]:
        """获取文件信息"""
        if not file_path.exists():
            return None
        
        return {
            'path': str(file_path.relative_to(self.folder_path)),
            'name': file_path.name,
            'size': file_path.stat().st_size,
            'mtime': file_path.stat().st_mtime,
            'hash': self.calculate_file_hash(file_path),
            'is_dir': file_path.is_dir()
        }
    
    def create_folder(self, folder_path: Path) -> bool:
        """创建文件夹"""
        try:
            folder_path.mkdir(parents=True, exist_ok=True)
            return True
        except Exception:
            return False
    
    def file_exists(self, file_path: Path) -> bool:
        """检查文件是否存在"""
        return file_path.exists()
    
    def get_relative_path(self, file_path: Path) -> str:
        """获取相对路径"""
        try:
            return str(file_path.relative_to(self.folder_path))
        except ValueError:
            return str(file_path)
    
    def get_absolute_path(self, relative_path: str) -> Path:
        """获取绝对路径"""
        return self.folder_path / relative_path