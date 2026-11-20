"""
单线程哈希队列：MD5 + 文件大小，pending 门禁的内容罗盘（阶段 B）。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import hashlib
import os
import queue
import threading
import uuid

_CHUNK = 1024 * 1024


class FileHasher:
    """单线程串行计算文件哈希；入队记录 size/mtime，算完校验未变才回写。"""

    def __init__(self, callback=None, log=None, autostart: bool = True):
        self._callback = callback
        self._log = log
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._jobs = {}       # job_id -> rel_path
        self._cancelled = set()
        self._thread = None
        if autostart:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    @staticmethod
    def hash_file(abs_path: str):
        """分块计算 MD5，返回 (hex, 实际字节数)。"""
        md5 = hashlib.md5()
        size = 0
        with open(abs_path, 'rb') as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                md5.update(chunk)
                size += len(chunk)
        return md5.hexdigest(), size

    def set_callback(self, callback):
        self._callback = callback

    def submit(self, abs_path: str, rel_path: str, file_id: str = '',
               kind: str = 'local_add', extra: dict = None) -> str:
        job_id = uuid.uuid4().hex
        try:
            st = os.stat(abs_path)
            snapshot = (int(st.st_size), float(st.st_mtime))
        except OSError:
            snapshot = None
        job = {'job_id': job_id, 'abs_path': abs_path, 'rel_path': rel_path,
               'file_id': file_id or '', 'kind': kind, 'extra': extra or {},
               'snapshot': snapshot}
        with self._lock:
            self._jobs[job_id] = rel_path
        self._queue.put(job)
        return job_id

    def cancel(self, rel_path: str, file_id: str = None):
        """取消某路径待算任务；已算出的结果由回调侧按状态校验丢弃。"""
        with self._lock:
            for jid, rel in list(self._jobs.items()):
                if rel == rel_path:
                    self._cancelled.add(jid)
                    self._jobs.pop(jid, None)

    def run_once(self, timeout: float = 0.5) -> bool:
        """同步处理一个任务（测试/无事件循环场景）。"""
        try:
            job = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        self._run_job(job)
        return True

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._run_job(job)

    def _run_job(self, job: dict):
        with self._lock:
            if job['job_id'] in self._cancelled:
                self._cancelled.discard(job['job_id'])
                return
            self._jobs.pop(job['job_id'], None)
        result = {'rel_path': job['rel_path'], 'file_id': job['file_id'],
                  'kind': job['kind'], 'extra': job['extra'],
                  'hash': '', 'size': 0, 'mtime': 0.0,
                  'changed': False, 'error': ''}
        try:
            md5, size = self.hash_file(job['abs_path'])
            st = os.stat(job['abs_path'])
        except OSError as e:
            result['error'] = 'missing' if not os.path.exists(
                job['abs_path']) else str(e)
            self._emit(result)
            return
        snap = job.get('snapshot')
        if snap is not None and (int(snap[0]) != int(st.st_size)
                                 or abs(float(snap[1]) - float(st.st_mtime)) > 1e-6):
            # 入队后文件已被改写：丢弃本次哈希，交由调用方提版本后重算
            result.update(changed=True, size=int(st.st_size),
                          mtime=float(st.st_mtime))
            self._emit(result)
            return
        result.update(hash=md5, size=int(st.st_size), mtime=float(st.st_mtime))
        self._emit(result)

    def _emit(self, result: dict):
        callback = self._callback
        if callback is None:
            return
        try:
            callback(result)
        except Exception as e:
            if self._log:
                try:
                    self._log(f"哈希回调失败: {e}")
                except Exception:
                    pass
