"""
主线程调用桥（HTTP 线程 → Qt 主线程）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import queue
import threading

from PySide6.QtCore import QObject, QTimer


class WebToolBridge(QObject):
    """后台线程 → 主线程的同步调用桥。

    HTTP 线程调用 ``call(fn)`` 后阻塞等待；主线程的 QTimer 抽干队列并执行，
    结果/异常回传。所有涉及 Qt 控件或同步引擎的操作都必须经此通道。
    单独成模块，建桥时无需导入 fastapi/uvicorn。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = queue.Queue()
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    def call(self, fn, timeout: float = 8.0):
        """在主线程执行 fn 并返回其结果（跨线程安全）。"""
        done = threading.Event()
        box = {}
        self._queue.put((fn, done, box))
        if not done.wait(timeout):
            raise TimeoutError('主程序未响应')
        if 'error' in box:
            raise box['error']
        return box.get('result')

    def _drain(self):
        # 单次最多处理 64 个，避免长时间占用主线程事件循环
        for _ in range(64):
            try:
                fn, done, box = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                box['result'] = fn()
            except Exception as exc:  # 交给调用线程抛出
                box['error'] = exc
            done.set()
