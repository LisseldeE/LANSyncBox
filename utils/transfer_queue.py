"""
传输队列管理器
限制同时传输的文件数量，避免内存占用过大
"""
import threading
import queue
from typing import Callable, Dict, Any, Optional
from collections import deque


class TransferQueue:
    """传输队列管理器"""
    
    def __init__(self, max_concurrent: int = 3):
        """
        初始化传输队列
        
        Args:
            max_concurrent: 同时传输的最大文件数量，默认为3
        """
        self.max_concurrent = max_concurrent
        self.queue = deque()  # 待传输队列
        self.active_count = 0  # 当前正在传输的数量
        self.lock = threading.Lock()  # 线程锁
        
        # 正在传输的任务字典（文件名 -> 停止标志）
        self.active_tasks: Dict[str, threading.Event] = {}
        
        # 任务ID到文件名的映射（用于取消任务）
        self.task_id_to_filename: Dict[int, str] = {}
    
    def add_task(self, task_type: str, task_func: Callable, filename: str, *args, **kwargs):
        """
        添加传输任务到队列
        
        Args:
            task_type: 任务类型（file, delete, rename）
            task_func: 任务执行函数
            filename: 文件名（用于取消任务）
            *args, **kwargs: 任务参数
        """
        with self.lock:
            # 检查是否已经在队列中或正在传输
            if filename in self.active_tasks:
                # 如果活跃任务已被取消（stop_event 已设置），允许新任务替换
                if not self.active_tasks[filename].is_set():
                    # 活跃且未取消，不需要重复添加
                    return
                # 已取消但尚未清理，允许替换（旧任务退出时不会误删新任务的 stop_event）
            else:
                # 检查队列中是否已经有该文件
                for task in self.queue:
                    if task.get('filename') == filename:
                        # 已经在队列中，不需要重复添加
                        return
            
            # 将任务加入队列
            task = {
                'type': task_type,
                'func': task_func,
                'filename': filename,
                'args': args,
                'kwargs': kwargs,
                'stop_event': threading.Event()  # 停止标志
            }
            self.queue.append(task)
            
            # 尝试启动任务（在锁内部调用，不需要再次获取锁）
            self._try_start_task_internal()
    
    def _try_start_task_internal(self):
        """尝试启动队列中的任务（在锁内部调用，不获取锁）"""
        # 如果当前传输数量小于最大值，且队列不为空，则启动新任务
        while self.active_count < self.max_concurrent and self.queue:
            task = self.queue.popleft()
            self.active_count += 1
            
            # 记录正在传输的任务
            filename = task['filename']
            stop_event = task['stop_event']
            self.active_tasks[filename] = stop_event
            
            # 在新线程中执行任务
            thread = threading.Thread(
                target=self._execute_task,
                args=(task,),
                daemon=True
            )
            thread.start()
    
    def _execute_task(self, task: Dict[str, Any]):
        """
        执行传输任务
        
        Args:
            task: 任务字典
        """
        filename = task['filename']
        stop_event = task['stop_event']
        
        try:
            # 执行任务（传入停止标志）
            task['func'](stop_event, *task['args'], **task['kwargs'])
        except Exception as e:
            print(f"传输任务执行失败: {e}")
        finally:
            # 任务完成，减少计数
            with self.lock:
                self.active_count -= 1
                
                # 仅当 active_tasks 中记录的仍是本任务的 stop_event 时才删除
                # 避免误删被新任务替换后的条目
                if filename in self.active_tasks and self.active_tasks[filename] is stop_event:
                    del self.active_tasks[filename]
                
                # 尝试启动下一个任务（在锁内部调用）
                self._try_start_task_internal()
    
    def cancel_task(self, filename: str):
        """
        取消特定文件的传输
        
        Args:
            filename: 文件名（用于取消任务）
        """
        with self.lock:
            # 如果正在传输，设置停止标志
            if filename in self.active_tasks:
                self.active_tasks[filename].set()  # 设置停止标志
            
            # 从队列中移除
            self.queue = deque([task for task in self.queue if task.get('filename') != filename])
    
    def cancel_tasks_by_filename(self, filename: str):
        """
        按文件名取消所有相关任务（支持复合键后缀匹配）。
        
        同时匹配：
        - task_key 等于 filename（客户端发送场景）
        - task_key 以 ":filename" 结尾（服务端多客户端发送场景，如 "client_id:filename"）
        
        Args:
            filename: 文件名（相对路径）
        """
        suffix = ':' + filename
        with self.lock:
            # 取消正在传输的任务
            for task_key, stop_event in list(self.active_tasks.items()):
                if task_key == filename or task_key.endswith(suffix):
                    stop_event.set()
            
            # 从队列中移除
            self.queue = deque([
                task for task in self.queue
                if task.get('filename') != filename and not task.get('filename', '').endswith(suffix)
            ])
    
    def cancel_all_tasks(self):
        """取消所有传输任务"""
        with self.lock:
            # 设置所有正在传输的任务的停止标志
            for stop_event in self.active_tasks.values():
                stop_event.set()
            
            # 清空队列
            self.queue.clear()
    
    def is_task_active(self, filename: str) -> bool:
        """
        检查特定文件是否正在传输
        
        Args:
            filename: 文件名
        
        Returns:
            是否正在传输
        """
        with self.lock:
            return filename in self.active_tasks
    
    def get_queue_size(self) -> int:
        """获取队列中待传输的任务数量"""
        with self.lock:
            return len(self.queue)
    
    def get_active_count(self) -> int:
        """获取当前正在传输的任务数量"""
        with self.lock:
            return self.active_count
    
    def clear(self):
        """清空队列"""
        self.cancel_all_tasks()