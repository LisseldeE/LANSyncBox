"""
发送串行化封装
每个 TCP 连接（socket）配一把发送锁，保证同一 socket 的多个并发线程串行写入，
一条完整消息（一次 sendall）不会被其他线程的字节交错，从根上消除「跨界污染」。
锁粒度 = 单次 sendall（消息级）：大文件数据块之间可插入 ping/取消等小包，节奏不受影响。
调用方需自行捕获发送异常，保持与原有 sendall 一致的异常语义。

仅依赖标准库，可在非 Qt 环境下单测。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import threading
import socket


class SendLock:
    """每 socket 一把发送锁的封装。

    用法：
        guard = SendLock()
        guard.send(sock, data)   # 内部 with 锁：sock.sendall(data)
    """

    __slots__ = ('_lock',)

    def __init__(self):
        self._lock = threading.Lock()

    def send(self, sock: socket.socket, data: bytes):
        """将 data 作为一条完整消息原样写入 sock（持锁串行化，避免并发交错）。"""
        with self._lock:
            sock.sendall(data)