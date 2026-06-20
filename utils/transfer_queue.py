"""
传输队列管理器
限制同时传输的文件数量，避免内存占用过大
"""
import threading
import queue
from typing import Callable, Dict, Any
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
    
    def add_task(self, task_type: str, task_func: Callable, *args, **kwargs):
        """
        添加传输任务到队列
        
        Args:
            task_type: 任务类型（file, delete, rename）
            task_func: 任务执行函数
            *args, **kwargs: 任务参数
        """
        with self.lock:
            # 将任务加入队列
            task = {
                'type': task_type,
                'func': task_func,
                'args': args,
                'kwargs': kwargs
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
        try:
            # 执行任务
            task['func'](*task['args'], **task['kwargs'])
        except Exception as e:
            print(f"传输任务执行失败: {e}")
        finally:
            # 任务完成，减少计数
            with self.lock:
                self.active_count -= 1
                
                # 尝试启动下一个任务（在锁内部调用）
                self._try_start_task_internal()
    
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
        with self.lock:
            self.queue.clear()