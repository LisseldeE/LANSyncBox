"""
清理队列模块
用于异步清理缓存文件夹，避免文件锁问题
Copyright (c) 2026 Lisselde_E.
Licensed under the GNU General Public License v3.0.
"""
import threading
import queue
import time
from pathlib import Path
from typing import Optional


class CleanQueue:
    """清理队列（单例模式）
    
    用于在后台线程中异步清理文件夹，避免：
    1. 文件锁导致的删除失败
    2. QFileSystemWatcher监控冲突
    3. 主线程阻塞
    """
    
    _instance: Optional['CleanQueue'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """单例模式：确保只有一个清理队列实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初始化清理队列"""
        if self._initialized:
            return
        
        self._initialized = True
        self.queue: queue.Queue = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
    
    def add_clean_task(self, path: Path):
        """添加清理任务到队列
        
        Args:
            path: 要清理的文件夹路径
        """
        if not isinstance(path, Path):
            path = Path(path)
        
        # 添加到队列
        self.queue.put(path)
        
        # 启动工作线程（如果未启动）
        self._start_worker_if_needed()
    
    def _start_worker_if_needed(self):
        """启动工作线程（如果需要）"""
        with self._worker_lock:
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(
                    target=self._clean_worker,
                    daemon=True,  # 守护线程，主线程退出时自动结束
                    name="CleanQueueWorker"
                )
                self.worker.start()
    
    def _clean_worker(self):
        """清理工作线程
        
        工作流程：
        1. 从队列获取清理任务
        2. 等待100ms（确保文件锁释放）
        3. 执行清理
        4. 如果队列为空，退出线程
        """
        while True:
            try:
                # 从队列获取任务，最多等待5秒
                path = self.queue.get(timeout=5)
                
                # 等待文件锁释放（100ms）
                time.sleep(0.1)
                
                # 执行清理
                self._clean_path(path)
                
                # 标记任务完成
                self.queue.task_done()
                
            except queue.Empty:
                # 队列为空，退出线程
                break
            except Exception as e:
                # 忽略所有错误，继续处理下一个任务
                print(f"清理队列错误: {e}")
                try:
                    self.queue.task_done()
                except:
                    pass
    
    def _clean_path(self, path: Path):
        """清理指定路径
        
        Args:
            path: 要清理的路径
        """
        try:
            if not path.exists():
                return
            
            # 导入清理函数（避免循环导入）
            from sync.file_manager import safe_rmtree
            
            # 执行清理
            safe_rmtree(path)
            
        except Exception as e:
            # 清理失败，忽略错误
            print(f"清理文件夹失败 {path}: {e}")
    
    def wait_completion(self, timeout: float = 10.0):
        """等待所有清理任务完成
        
        Args:
            timeout: 超时时间（秒）
        """
        try:
            self.queue.join()
        except Exception:
            pass
    
    def get_queue_size(self) -> int:
        """获取队列中待处理的任务数量"""
        return self.queue.qsize()
    
    def is_worker_running(self) -> bool:
        """检查工作线程是否正在运行"""
        return self.worker is not None and self.worker.is_alive()


# 全局清理队列实例
_clean_queue: Optional[CleanQueue] = None


def get_clean_queue() -> CleanQueue:
    """获取全局清理队列实例
    
    Returns:
        CleanQueue实例
    """
    global _clean_queue
    if _clean_queue is None:
        _clean_queue = CleanQueue()
    return _clean_queue