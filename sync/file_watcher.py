# -*- coding: utf-8 -*-
"""
LANSyncBox 文件监控模块
使用watchdog监控文件变化
"""

import os
import time
from typing import Callable, Set, Dict
from PyQt5.QtCore import QObject, pyqtSignal, QTimer
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from config import WATCHDOG_DEBOUNCE_SECONDS


class FileWatcher(QObject):
    """文件监控器"""
    
    # 信号定义
    file_created = pyqtSignal(str)  # 文件创建 (filepath)
    file_modified = pyqtSignal(str)  # 文件修改 (filepath)
    file_deleted = pyqtSignal(str)  # 文件删除 (filepath)
    directory_created = pyqtSignal(str)  # 目录创建 (dirpath)
    error_occurred = pyqtSignal(str)  # 错误消息
    
    # 内部信号：用于跨线程调度延迟创建事件
    _schedule_create_delay = pyqtSignal(str)  # 在主线程中调度延迟创建
    _schedule_cancel_create = pyqtSignal(str)  # 在主线程中取消创建
    
    # 创建事件延迟时间（毫秒），用于等待可能的重命名
    CREATE_EVENT_DELAY_MS = 800
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.watch_path = ""
        self.observer: Observer = None
        self.event_handler: FileEventHandler = None
        self.running = False
        
        # 防抖处理 - 使用时间戳记录
        self._last_event_time: dict = {}  # {filepath: timestamp}
        
        # 忽略列表：刚接收的文件，短时间内不触发同步
        # 格式: {filepath: timestamp}
        self._ignore_list: dict = {}
        
        # 待处理的创建事件：延迟发出创建信号，等待可能的重命名
        # 格式: {original_path: QTimer}
        self._pending_creates: Dict[str, QTimer] = {}
        
        # 已取消的创建事件：记录被取消的创建路径，避免重复处理
        self._cancelled_creates: Set[str] = set()
        
        # 刚创建的文件路径标记（线程安全，用于在 watchdog 线程中检测重命名）
        self._recently_created_paths: Set[str] = set()
        
        # 连接内部信号（确保在主线程中处理）
        self._schedule_create_delay.connect(self._do_schedule_create_delay)
        self._schedule_cancel_create.connect(self._do_cancel_pending_create)
    
    def add_ignore(self, filepath: str, duration: float = 2.0):
        """
        添加文件到忽略列表
        Args:
            filepath: 文件路径
            duration: 忽略时长（秒）
        """
        self._ignore_list[filepath] = time.time() + duration
        print(f"[DEBUG] Added to ignore list: {filepath} for {duration}s")
    
    def should_ignore_file(self, filepath: str) -> bool:
        """
        检查文件是否应该被忽略
        Args:
            filepath: 文件路径
        Returns:
            是否忽略
        """
        if filepath in self._ignore_list:
            expire_time = self._ignore_list[filepath]
            if time.time() < expire_time:
                print(f"[DEBUG] Ignoring file (in ignore list): {filepath}")
                return True
            else:
                # 过期，移除
                self._ignore_list.pop(filepath, None)
        return False
    
    def _cancel_pending_create(self, filepath: str):
        """
        取消待处理的创建事件（跨线程安全）
        Args:
            filepath: 文件路径
        """
        # 使用信号在主线程中执行
        self._schedule_cancel_create.emit(filepath)
    
    def _do_cancel_pending_create(self, filepath: str):
        """
        在主线程中取消待处理的创建事件
        Args:
            filepath: 文件路径
        """
        if filepath in self._pending_creates:
            timer = self._pending_creates.pop(filepath)
            timer.stop()
            timer.deleteLater()
            print(f"[DEBUG] Cancelled pending create for: {filepath}")
        
        # 记录已取消，防止定时器回调时重复处理
        self._cancelled_creates.add(filepath)
    
    def _do_schedule_create_delay(self, filepath: str):
        """
        在主线程中调度延迟创建事件
        Args:
            filepath: 文件路径
        """
        # 取消之前的待处理创建事件（如果存在）
        if filepath in self._pending_creates:
            old_timer = self._pending_creates.pop(filepath)
            old_timer.stop()
            old_timer.deleteLater()
        
        # 创建延迟定时器（现在在主线程中）
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._emit_create_signal(filepath))
        timer.start(self.CREATE_EVENT_DELAY_MS)
        self._pending_creates[filepath] = timer
        print(f"[DEBUG] Delayed create event scheduled for: {filepath} (waiting {self.CREATE_EVENT_DELAY_MS}ms)")
    
    def _emit_create_signal(self, filepath: str):
        """
        延迟发出创建信号
        Args:
            filepath: 文件路径
        """
        # 从待处理列表中移除
        self._pending_creates.pop(filepath, None)
        
        # 从刚创建标记中移除
        self._recently_created_paths.discard(filepath)
        
        # 检查是否已被取消（例如被重命名）
        # 但只有在文件确实不存在时才认为是被取消
        # 如果文件仍然存在，说明可能是残留的取消记录，应该正常发出信号
        if filepath in self._cancelled_creates:
            # 清理取消记录
            self._cancelled_creates.discard(filepath)
            # 如果文件不存在，才认为是被取消
            if not os.path.exists(filepath):
                print(f"[DEBUG] Create signal cancelled (file was renamed): {filepath}")
                return
            else:
                # 文件存在，可能是残留的取消记录，继续发出信号
                print(f"[DEBUG] File exists despite cancel record, proceeding: {filepath}")
        
        # 检查文件是否仍然存在
        if not os.path.exists(filepath):
            print(f"[DEBUG] File no longer exists, skip create signal: {filepath}")
            return
        
        # 发出创建信号
        print(f"[DEBUG] Emitting file_created signal: {filepath}")
        if os.path.isfile(filepath):
            self.file_created.emit(filepath)
        elif os.path.isdir(filepath):
            self.directory_created.emit(filepath)
    
    def start(self, watch_path: str) -> bool:
        """
        开始监控指定路径
        Args:
            watch_path: 监控路径
        Returns:
            是否启动成功
        """
        print(f"[DEBUG] FileWatcher.start called with path: {watch_path}")
        
        if not os.path.isdir(watch_path):
            self.error_occurred.emit(f"监控路径不存在: {watch_path}")
            print(f"[DEBUG] Path does not exist: {watch_path}")
            return False
        
        try:
            self.watch_path = watch_path
            
            # 创建事件处理器
            self.event_handler = FileEventHandler(self)
            print(f"[DEBUG] Event handler created")
            
            # 创建观察者
            self.observer = Observer()
            print(f"[DEBUG] Observer created, type: {type(self.observer)}")
            
            self.observer.schedule(self.event_handler, watch_path, recursive=True)
            print(f"[DEBUG] Observer scheduled for path: {watch_path}")
            
            self.observer.start()
            print(f"[DEBUG] Observer started, running: {self.observer.is_alive()}")
            
            self.running = True
            print(f"[DEBUG] FileWatcher started successfully")
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"启动文件监控失败: {e}")
            print(f"[DEBUG] Failed to start FileWatcher: {e}")
            return False
    
    def stop(self):
        """停止监控"""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        
        self.running = False
    
    def _handle_event(self, event_type: str, src_path: str):
        """
        处理文件事件（带防抖）
        Args:
            event_type: 事件类型 (created, modified, deleted)
            src_path: 文件路径
        """
        print(f"[DEBUG] _handle_event: {event_type} - {src_path}")
        
        # 忽略临时文件
        if self._should_ignore(src_path):
            print(f"[DEBUG] Ignored: {src_path}")
            return
        
        # 检查是否在忽略列表中（刚接收的文件）
        if self.should_ignore_file(src_path):
            print(f"[DEBUG] Ignored (in ignore list): {src_path}")
            return
        
        # 防抖：检查是否在短时间内重复触发
        current_time = time.time()
        if src_path in self._last_event_time:
            last_time = self._last_event_time[src_path]
            if current_time - last_time < WATCHDOG_DEBOUNCE_SECONDS:
                print(f"[DEBUG] Debounced: {src_path} (time diff: {current_time - last_time:.2f}s)")
                return
        
        # 更新时间戳
        self._last_event_time[src_path] = current_time
        
        # 对于创建事件，延迟处理以等待可能的重命名
        if event_type == 'created':
            # 标记为刚创建的文件（用于重命名检测）
            self._recently_created_paths.add(src_path)
            # 使用信号在主线程中调度延迟创建
            self._schedule_create_delay.emit(src_path)
            print(f"[DEBUG] Requesting delayed create event for: {src_path}")
        elif event_type == 'modified':
            if os.path.isfile(src_path):
                self.file_modified.emit(src_path)
        elif event_type == 'deleted':
            # 清理刚创建标记
            self._recently_created_paths.discard(src_path)
            # 如果有待处理的创建事件，取消它
            self._cancel_pending_create(src_path)
            self.file_deleted.emit(src_path)
    
    def _should_ignore(self, path: str) -> bool:
        """
        判断是否应该忽略该路径
        Args:
            path: 文件路径
        Returns:
            是否忽略
        """
        # 忽略临时文件
        ignore_patterns = [
            '~$',  # Office临时文件
            '.tmp',  # 临时文件
            '.temp',  # 临时文件
            '.swp',  # Vim临时文件
            '.DS_Store',  # macOS
            'Thumbs.db',  # Windows
            '.git',  # Git目录
            '__pycache__',  # Python缓存
        ]
        
        for pattern in ignore_patterns:
            if pattern in path:
                return True
        
        return False


class FileEventHandler(FileSystemEventHandler):
    """文件系统事件处理器"""
    
    def __init__(self, watcher: FileWatcher):
        super().__init__()
        self.watcher = watcher
    
    def on_created(self, event: FileSystemEvent):
        """文件/目录创建事件"""
        print(f"[DEBUG] on_created: {event.src_path}, is_directory={event.is_directory}")
        # 处理文件和目录创建
        self.watcher._handle_event('created', event.src_path)
    
    def on_modified(self, event: FileSystemEvent):
        """文件修改事件"""
        print(f"[DEBUG] on_modified: {event.src_path}, is_directory={event.is_directory}")
        if not event.is_directory:
            self.watcher._handle_event('modified', event.src_path)
    
    def on_deleted(self, event: FileSystemEvent):
        """文件/目录删除事件"""
        print(f"[DEBUG] on_deleted: {event.src_path}, is_directory={event.is_directory}")
        self.watcher._handle_event('deleted', event.src_path)
    
    def on_moved(self, event: FileSystemEvent):
        """文件/目录移动事件"""
        print(f"[DEBUG] on_moved: {event.src_path} -> {event.dest_path}")
        
        # 检查原始文件是否是刚创建的（使用线程安全的标记）
        if event.src_path in self.watcher._recently_created_paths:
            # 刚创建的文件被重命名
            # 从刚创建标记中移除
            self.watcher._recently_created_paths.discard(event.src_path)
            # 取消原始创建事件
            self.watcher._cancel_pending_create(event.src_path)
            print(f"[DEBUG] Recently created file was renamed: {event.src_path} -> {event.dest_path}")
            
            # 直接发出新文件名的创建信号（不延迟，因为用户已经确认了文件名）
            print(f"[DEBUG] Emitting create signal for renamed file: {event.dest_path}")
            if os.path.isfile(event.dest_path):
                self.watcher.file_created.emit(event.dest_path)
            elif os.path.isdir(event.dest_path):
                self.watcher.directory_created.emit(event.dest_path)
        else:
            # 已存在的文件被重命名
            # 发送删除旧文件 + 创建新文件的信号
            print(f"[DEBUG] Existing file was renamed: {event.src_path} -> {event.dest_path}")
            self.watcher._handle_event('deleted', event.src_path)
            # 直接发出新文件名的创建信号（不延迟，确保删除和创建同时发送）
            print(f"[DEBUG] Emitting create signal for renamed file: {event.dest_path}")
            if os.path.isfile(event.dest_path):
                self.watcher.file_created.emit(event.dest_path)
            elif os.path.isdir(event.dest_path):
                self.watcher.directory_created.emit(event.dest_path)