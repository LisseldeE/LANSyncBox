"""
发送串行化封装
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