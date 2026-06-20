"""
同步引擎
负责文件同步逻辑
"""
import threading
from pathlib import Path
from typing import Dict, List, Optional
from queue import Queue

from PySide6.QtCore import QObject, Signal

from sync.file_manager import FileManager
from network.protocol import SyncProtocol, SyncMessage, MessageType
from config import Config


class SyncEngine(QObject):
    """同步引擎"""
    
    # 信号
    sync_started = Signal()  # 同步开始
    sync_completed = Signal()  # 同步完成
    sync_error = Signal(str)  # 同步错误
    file_synced = Signal(str)  # 文件同步完成
    progress_updated = Signal(int, int)  # 进度更新（当前，总数）
    
    def __init__(self, file_manager: FileManager):
        super().__init__()
        self.file_manager = file_manager
        
        # 操作队列
        self.operation_queue = Queue()
        
        # 同步状态
        self.is_syncing = False
        
        # 同步线程
        self.sync_thread: Optional[threading.Thread] = None
        
        # 文件状态跟踪
        self.file_status: Dict[str, str] = {}  # 文件路径 -> 状态
    
    def start_sync(self):
        """开始同步"""
        if self.is_syncing:
            return
        
        self.is_syncing = True
        self.sync_started.emit()
        
        # 启动同步线程
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()
    
    def stop_sync(self):
        """停止同步"""
        self.is_syncing = False
        
        # 清空队列
        while not self.operation_queue.empty():
            self.operation_queue.get()
    
    def add_operation(self, operation: Dict):
        """添加操作到队列"""
        self.operation_queue.put(operation)
    
    def _sync_loop(self):
        """同步循环"""
        while self.is_syncing:
            try:
                # 从队列获取操作
                operation = self.operation_queue.get(timeout=1.0)
                
                # 处理操作
                self._process_operation(operation)
                
                # 标记队列任务完成
                self.operation_queue.task_done()
            except:
                # 队列为空，继续等待
                continue
        
        self.sync_completed.emit()
    
    def _process_operation(self, operation: Dict):
        """处理操作"""
        op_type = operation.get('type')
        file_path = operation.get('file_path')
        
        try:
            # 更新文件状态为正在同步
            self.file_status[file_path] = 'syncing'
            
            if op_type == 'add':
                # 添加文件
                src_path = Path(operation.get('src_path'))
                dst_path = self.file_manager.get_absolute_path(file_path)
                
                success = self.file_manager.add_file(src_path, dst_path)
                
                if success:
                    self.file_status[file_path] = 'synced'
                    self.file_synced.emit(file_path)
                else:
                    self.file_status[file_path] = 'failed'
                    self.sync_error.emit(f"添加文件失败: {file_path}")
            
            elif op_type == 'delete':
                # 删除文件
                abs_path = self.file_manager.get_absolute_path(file_path)
                
                success = self.file_manager.delete_file(abs_path)
                
                if success:
                    self.file_status.pop(file_path, None)
                    self.file_synced.emit(file_path)
                else:
                    self.file_status[file_path] = 'failed'
                    self.sync_error.emit(f"删除文件失败: {file_path}")
            
            elif op_type == 'rename':
                # 重命名文件
                old_path = self.file_manager.get_absolute_path(operation.get('old_path'))
                new_path = self.file_manager.get_absolute_path(operation.get('new_path'))
                
                success = self.file_manager.rename_file(old_path, new_path)
                
                if success:
                    self.file_status.pop(operation.get('old_path'), None)
                    self.file_status[operation.get('new_path')] = 'synced'
                    self.file_synced.emit(operation.get('new_path'))
                else:
                    self.sync_error.emit(f"重命名文件失败: {operation.get('old_path')}")
            
            elif op_type == 'move':
                # 移动文件
                src_path = self.file_manager.get_absolute_path(operation.get('src_path'))
                dst_path = self.file_manager.get_absolute_path(operation.get('dst_path'))
                
                success = self.file_manager.move_file(src_path, dst_path)
                
                if success:
                    self.file_status.pop(operation.get('src_path'), None)
                    self.file_status[operation.get('dst_path')] = 'synced'
                    self.file_synced.emit(operation.get('dst_path'))
                else:
                    self.sync_error.emit(f"移动文件失败: {operation.get('src_path')}")
        
        except Exception as e:
            self.file_status[file_path] = 'failed'
            self.sync_error.emit(f"同步操作失败: {str(e)}")
    
    def get_file_status(self, file_path: str) -> str:
        """获取文件状态"""
        return self.file_status.get(file_path, 'synced')
    
    def sync_file_list(self, remote_files: List[Dict]):
        """同步文件列表"""
        # 获取本地文件列表
        local_files = self.file_manager.get_file_list()
        
        # 比较文件列表
        local_dict = {f['path']: f for f in local_files}
        remote_dict = {f['path']: f for f in remote_files}
        
        # 找出需要添加的文件（远程有，本地没有）
        for path, file_info in remote_dict.items():
            if path not in local_dict:
                # 需要下载
                self.add_operation({
                    'type': 'download',
                    'file_path': path,
                    'file_info': file_info
                })
        
        # 找出需要删除的文件（本地有，远程没有）
        for path in local_dict:
            if path not in remote_dict:
                # 需要删除
                self.add_operation({
                    'type': 'delete',
                    'file_path': path
                })
        
        # 找出需要更新的文件（哈希值不同）
        for path in local_dict:
            if path in remote_dict:
                local_hash = local_dict[path]['hash']
                remote_hash = remote_dict[path]['hash']
                
                if local_hash != remote_hash:
                    # 需要更新（冲突检测）
                    self.add_operation({
                        'type': 'conflict',
                        'file_path': path,
                        'local_info': local_dict[path],
                        'remote_info': remote_dict[path]
                    })
    
    def create_sync_message(self, operation: Dict) -> SyncMessage:
        """创建同步消息"""
        op_type = operation.get('type')
        
        if op_type == 'add':
            return SyncProtocol.create_file_add_message(
                operation.get('file_path'),
                operation.get('file_size', 0),
                operation.get('file_hash', '')
            )
        
        elif op_type == 'delete':
            return SyncProtocol.create_file_delete_message(
                operation.get('file_path')
            )
        
        elif op_type == 'rename':
            return SyncProtocol.create_file_rename_message(
                operation.get('old_path'),
                operation.get('new_path')
            )
        
        return SyncProtocol.create_message(MessageType.FILE_ADD, {})