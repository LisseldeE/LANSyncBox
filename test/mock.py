# -*- coding: utf-8 -*-
"""
LANSyncBox 测试模拟环境
模拟双端环境、文件监控和网络传输
"""

import os
import shutil
import tempfile
import time
from typing import Dict, List, Optional
from PyQt5.QtCore import QObject, pyqtSignal

from sync import SyncEngine, SyncOperation, OpType


class MockEnvironment:
    """
    模拟测试环境
    
    模拟双端同步环境：
    - host: 主机端
    - client: 连接端
    - 模拟网络传输
    - 模拟文件监控
    """
    
    def __init__(self):
        # 创建临时目录
        self._temp_dir = tempfile.mkdtemp(prefix="lansyncbox_test_")
        
        # 主机端目录
        self._host_dir = os.path.join(self._temp_dir, "host")
        os.makedirs(self._host_dir)
        
        # 连接端目录
        self._client_dir = os.path.join(self._temp_dir, "client")
        os.makedirs(self._client_dir)
        
        # 主机端引擎
        self.host_engine = SyncEngine()
        self.host_engine.initialize(self._host_dir, "host_node")
        
        # 连接端引擎
        self.client_engine = SyncEngine()
        self.client_engine.initialize(self._client_dir, "client_node")
        
        # 模拟网络（连接两个引擎）
        self._mock_network = MockNetwork(self.host_engine, self.client_engine)
        
        # 操作记录
        self._host_ops: List[SyncOperation] = []
        self._client_ops: List[SyncOperation] = []
        
        # 连接信号
        self.host_engine.operation_ready.connect(self._on_host_op_ready)
        self.client_engine.operation_ready.connect(self._on_client_op_ready)
    
    def _on_host_op_ready(self, op: SyncOperation):
        """主机端操作准备发送"""
        self._host_ops.append(op)
    
    def _on_client_op_ready(self, op: SyncOperation):
        """连接端操作准备发送"""
        self._client_ops.append(op)
    
    def create_file(self, side: str, path: str, content: str = "test content") -> str:
        """
        创建文件
        Args:
            side: 'host' 或 'client'
            path: 相对路径
            content: 文件内容
        Returns:
            文件绝对路径
        """
        if side == 'host':
            filepath = os.path.join(self._host_dir, path)
        else:
            filepath = os.path.join(self._client_dir, path)
        
        # 确保父目录存在
        parent_dir = os.path.dirname(filepath)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir)
        
        # 写入文件
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # 触发文件事件
        if side == 'host':
            self.host_engine.on_file_event('created', path)
        else:
            self.client_engine.on_file_event('created', path)
        
        return filepath
    
    def modify_file(self, side: str, path: str, content: str) -> str:
        """
        修改文件
        Args:
            side: 'host' 或 'client'
            path: 相对路径
            content: 新内容
        Returns:
            文件绝对路径
        """
        if side == 'host':
            filepath = os.path.join(self._host_dir, path)
        else:
            filepath = os.path.join(self._client_dir, path)
        
        # 修改文件
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # 触发文件事件
        if side == 'host':
            self.host_engine.on_file_event('modified', path)
        else:
            self.client_engine.on_file_event('modified', path)
        
        return filepath
    
    def delete_file(self, side: str, path: str):
        """
        删除文件
        Args:
            side: 'host' 或 'client'
            path: 相对路径
        """
        if side == 'host':
            filepath = os.path.join(self._host_dir, path)
        else:
            filepath = os.path.join(self._client_dir, path)
        
        # 删除文件
        if os.path.exists(filepath):
            if os.path.isdir(filepath):
                shutil.rmtree(filepath)
            else:
                os.remove(filepath)
        
        # 触发文件事件
        if side == 'host':
            self.host_engine.on_file_event('deleted', path)
        else:
            self.client_engine.on_file_event('deleted', path)
    
    def rename_file(self, side: str, old_path: str, new_path: str):
        """
        重命名文件
        Args:
            side: 'host' 或 'client'
            old_path: 旧路径
            new_path: 新路径
        """
        if side == 'host':
            old_filepath = os.path.join(self._host_dir, old_path)
            new_filepath = os.path.join(self._host_dir, new_path)
        else:
            old_filepath = os.path.join(self._client_dir, old_path)
            new_filepath = os.path.join(self._client_dir, new_path)
        
        # 重命名
        if os.path.exists(old_filepath):
            shutil.move(old_filepath, new_filepath)
        
        # 触发文件事件（删除 + 创建）
        if side == 'host':
            self.host_engine.on_file_event('deleted', old_path)
            self.host_engine.on_file_event('created', new_path)
        else:
            self.client_engine.on_file_event('deleted', old_path)
            self.client_engine.on_file_event('created', new_path)
    
    def sync_all(self):
        """同步所有操作"""
        # 强制处理所有待匹配事件（删除事件等待30秒，测试需要立即处理）
        self.host_engine.force_process_pending()
        self.client_engine.force_process_pending()
        
        # 处理主机端队列
        while self.host_engine.get_queue_length() > 0:
            self.host_engine.process_next()
            time.sleep(0.1)
        
        # 处理连接端队列
        while self.client_engine.get_queue_length() > 0:
            self.client_engine.process_next()
            time.sleep(0.1)
        
        # 模拟网络传输
        self._mock_network.sync_all()
    
    def file_exists(self, side: str, path: str) -> bool:
        """检查文件是否存在"""
        if side == 'host':
            filepath = os.path.join(self._host_dir, path)
        else:
            filepath = os.path.join(self._client_dir, path)
        
        return os.path.exists(filepath)
    
    def read_file(self, side: str, path: str) -> Optional[str]:
        """读取文件内容"""
        if side == 'host':
            filepath = os.path.join(self._host_dir, path)
        else:
            filepath = os.path.join(self._client_dir, path)
        
        if not os.path.exists(filepath):
            return None
        
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    
    def get_host_ops(self) -> List[SyncOperation]:
        """获取主机端操作记录"""
        return self._host_ops.copy()
    
    def get_client_ops(self) -> List[SyncOperation]:
        """获取连接端操作记录"""
        return self._client_ops.copy()
    
    def cleanup(self):
        """清理测试环境"""
        shutil.rmtree(self._temp_dir, ignore_errors=True)


class MockNetwork:
    """模拟网络传输"""
    
    def __init__(self, host_engine: SyncEngine, client_engine: SyncEngine):
        self._host_engine = host_engine
        self._client_engine = client_engine
        
        # 设置回调
        host_engine.set_send_operation_callback(self._on_host_send)
        client_engine.set_send_operation_callback(self._on_client_send)
        
        # 操作队列
        self._host_to_client: List[tuple] = []  # (op, content)
        self._client_to_host: List[tuple] = []
    
    def _on_host_send(self, op: SyncOperation):
        """主机端发送操作"""
        # 读取文件内容（如果是 CREATE/MODIFY）
        content = None
        if op.op_type in (OpType.CREATE, OpType.MODIFY) and not op.is_dir:
            content = self._host_engine._local_fs.read_file_content(op.path)
        
        # 立即传递给连接端
        self._client_engine.receive_operation(op, content)
        
        # 模拟传输完成，解锁路径
        if op.op_type in (OpType.CREATE, OpType.MODIFY, OpType.RENAME):
            self._host_engine.unlock_path(op.path)
            if op.op_type == OpType.RENAME:
                self._host_engine.unlock_path(op.old_path)
    
    def _on_client_send(self, op: SyncOperation):
        """连接端发送操作"""
        # 读取文件内容
        content = None
        if op.op_type in (OpType.CREATE, OpType.MODIFY) and not op.is_dir:
            content = self._client_engine._local_fs.read_file_content(op.path)
        
        # 立即传递给主机端
        self._host_engine.receive_operation(op, content)
        
        # 模拟传输完成，解锁路径
        if op.op_type in (OpType.CREATE, OpType.MODIFY, OpType.RENAME):
            self._client_engine.unlock_path(op.path)
            if op.op_type == OpType.RENAME:
                self._client_engine.unlock_path(op.old_path)
    
    def sync_all(self):
        """同步所有操作（现在不需要了，因为操作立即传递）"""
        pass


class MockFileWatcher(QObject):
    """模拟文件监控器"""
    
    file_created = pyqtSignal(str)
    file_modified = pyqtSignal(str)
    file_deleted = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._events: List[tuple] = []
    
    def emit_created(self, path: str):
        self._events.append(('created', path))
        self.file_created.emit(path)
    
    def emit_modified(self, path: str):
        self._events.append(('modified', path))
        self.file_modified.emit(path)
    
    def emit_deleted(self, path: str):
        self._events.append(('deleted', path))
        self.file_deleted.emit(path)
    
    def get_events(self) -> List[tuple]:
        return self._events.copy()