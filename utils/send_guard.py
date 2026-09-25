"""
发送串行化封装
每个 TCP 连接（socket）配一把发送锁，保证同一 socket 的多个并发线程串行写入，
一条完整消息不会被其他线程的字节交错，从根上消除「跨界污染」。
锁粒度 = 单条消息：大文件数据块之间可插入 ping/取消等小包，节奏不受影响。
调用方需自行捕获发送异常，保持与原有 sendall 一致的异常语义。

仅依赖标准库，可在非 Qt 环境下单测。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import threading
import socket
import time


class SendLock:
    """每 socket 一把发送锁的封装。

    用法：
        guard = SendLock()
        guard.send(sock, data)              # 内部 with 锁：sock.sendall(data)
        guard.send_resumable(sock, data)    # 可恢复发送（背压不重发已发送字节）
    """

    __slots__ = ('_lock',)

    def __init__(self):
        self._lock = threading.Lock()

    def send(self, sock: socket.socket, data: bytes):
        """将 data 作为一条完整消息原样写入 sock（持锁串行化，避免并发交错）。"""
        with self._lock:
            sock.sendall(data)

    def send_resumable(self, sock: socket.socket, data: bytes, timeout: float = 1.0,
                       backoff: float = 0.05, stop_event: threading.Event = None,
                       total_timeout: float = None) -> bool:
        """将 data 作为一条完整消息可恢复地写入 sock（持锁串行化，避免并发交错）。

        与 send（sendall）的区别：socket.timeout（背压，接收端处理慢导致发送缓冲区满）
        时不重发已发送的字节——sendall 会整条重发，重复数据流会加剧背压，把
        PING/PONG 等控制消息长时间挡在发送锁外（同步延迟虚高上万毫秒的根因）；
        而是短暂睡眠（backoff 秒）让接收端排空缓冲后，从剩余字节继续，直至整条
        消息完整写出。一次 send 最多阻塞 timeout 秒。

        Args:
            sock: 目标 socket（阻塞模式）
            data: 一条完整消息字节
            timeout: 单次 send 超时秒数
            backoff: 背压超时后的退避秒数
            stop_event: 可选，置位时立即中止并返回 False
            total_timeout: 可选，整条消息写出的总时限（秒）。超过即放弃返回 False，
                防止半开对端单端长时间占住发送锁/拖垮广播循环。

        Returns:
            True 表示整条消息已完整写出；False 表示被中止、超总时限或连接异常
            （BrokenPipe/ConnectionReset/ConnectionAborted/OSError 等）
        """
        total_sent = 0
        sock.settimeout(timeout)
        deadline = None
        if total_timeout is not None:
            deadline = time.time() + total_timeout
        with self._lock:
            while total_sent < len(data):
                if stop_event is not None and stop_event.is_set():
                    return False
                if deadline is not None and time.time() > deadline:
                    return False
                try:
                    sent = sock.send(data[total_sent:])
                except socket.timeout:
                    # 背压：发送缓冲区满且 timeout 秒内无空间，等待接收端排空后继续
                    time.sleep(backoff)
                    continue
                except Exception:
                    return False
                if sent == 0:
                    return False
                total_sent += sent
        return True