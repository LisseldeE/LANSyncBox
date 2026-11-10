"""
收发串行化封装
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import select
import threading
import socket
import time


class SendLock:
    """每 socket 一把接收/发送锁的封装。

    用法：
        guard = SendLock()
        guard.send(sock, data)              # 内部 with 锁：sock.sendall(data)
        guard.send_resumable(sock, data)    # 可恢复发送（背压不重发已发送字节）
        guard.recv(sock, 65536)             # 接收线程专用：与发送互斥
    """

    __slots__ = ('_lock', '_io')

    def __init__(self):
        self._lock = threading.Lock()  # 消息级：一条消息不被并发写交错
        self._io = threading.Lock()    # 连接级：同一连接的读与写不得并发

    def send(self, sock: socket.socket, data: bytes):
        """将 data 作为一条完整消息原样写入 sock（持锁串行化，避免并发交错）。"""
        with self._lock:
            with self._io:
                sock.sendall(data)

    def send_resumable(self, sock: socket.socket, data: bytes, timeout: float = 1.0,
                       backoff: float = 0.05, stop_event: threading.Event = None,
                       total_timeout: float = None, best_effort: bool = False) -> bool:
        """将 data 作为一条完整消息可恢复地写入 sock（持锁串行化，避免并发交错）。

        与 send（sendall）的区别：socket.timeout（背压，接收端处理慢导致发送缓冲区满）
        时不重发已发送的字节——sendall 会整条重发，重复数据流会加剧背压，把
        PING/PONG 等控制消息长时间挡在发送锁外（同步延迟虚高上万毫秒的根因）；
        而是短暂睡眠（backoff 秒）让接收端排空缓冲后，从剩余字节继续，直至整条
        消息完整写出。一次 send 最多阻塞 timeout 秒。

        背压退避期间只持消息锁（保证不与他条消息交错），不持 I/O 锁——让接收
        线程仍能取到 I/O 锁排空本端接收缓冲，反过来解开对端的背压，避免双向
        大流量时互等。

        Args:
            sock: 目标 socket（阻塞模式）
            data: 一条完整消息字节
            timeout: 单次 send 超时秒数
            backoff: 背压超时后的退避秒数
            stop_event: 可选，置位时立即中止并返回 False
            total_timeout: 可选，整条消息写出的总时限（秒）。超过即放弃返回 False，
                防止半开对端（心跳超时判定前的窗口期）单端长时间占住发送锁/拖垮
                send_to_all 广播循环。
            best_effort: 可选，心跳专用。发送锁被占用时不等待、直接返回 False，
                心跳丢失一次不影响判活（下一拍重发）。

        Returns:
            True 表示整条消息已完整写出；False 表示被中止、超总时限或连接异常
            （BrokenPipe/ConnectionReset/ConnectionAborted/OSError 等）
        """
        total_sent = 0
        # 仅当超时不同才改：settimeout 会重设底层阻塞模式，接收线程可能正阻塞在
        # 同一 socket 的 recv/select 上，无谓改动会让该次等待提前返回。
        if sock.gettimeout() != timeout:
            sock.settimeout(timeout)
        deadline = None
        if total_timeout is not None:
            deadline = time.time() + total_timeout
        if best_effort:
            # 心跳路径：抢不到发送锁就放弃本次（1 秒后还有下一次），
            # 避免接收线程回 PONG 时被大文件发送锁堵住、形成双向互等。
            if not self._lock.acquire(blocking=False):
                return False
        else:
            self._lock.acquire()
        try:
            while total_sent < len(data):
                if stop_event is not None and stop_event.is_set():
                    return False
                if deadline is not None and time.time() > deadline:
                    return False
                try:
                    with self._io:
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
        finally:
            self._lock.release()

    def recv(self, sock: socket.socket, n: int) -> bytes:
        """从 sock 读取数据，与发送互斥。

        CPython 的 ssl 模块不支持同一连接并发读写：写线程正在 send 时，接收线程
        recv 会偶发返回 b''（伪 EOF，实际对端并未关闭）。上层据此判定"对端关闭"
        并重连，表现为连接每隔十几秒断一次、频繁重连。故读写统一走本锁。

        空闲等待用 select 完成且不持 I/O 锁，确认可读后才持锁读取：既保证同一
        时刻只有一个线程操作 SSL 对象，又不让空闲读长期占锁拖慢大文件发送。
        超时语义与原 socket 一致：超过 sock 的 timeout 仍无数据则抛 socket.timeout。
        """
        timeout = sock.gettimeout()
        deadline = None if timeout is None else time.time() + timeout
        while True:
            try:
                with self._io:
                    # TLS 已解密但内核无数据的缓冲，select 看不到；须与发送互斥
                    buffered = sock.pending()
            except AttributeError:
                buffered = False
            if buffered:
                break
            # socket 可能已被其他线程 close（fd=-1）：select 抛 ValueError，
            # 归一为 OSError，与正常连接错误路径（调用方捕获）一致
            try:
                if deadline is None:
                    select.select([sock], [], [])
                    break
                left = deadline - time.time()
                if left <= 0:
                    raise socket.timeout("timed out")
                if select.select([sock], [], [], left)[0]:
                    break
            except ValueError as e:
                raise OSError(str(e))
        with self._io:
            return sock.recv(n)
