"""分布式文件轻量服务（局域网剪切板 - 文件/图片 端到端 TCP 直连）。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import json
import os
import socket
import tempfile
import threading
import time

from PySide6.QtCore import QObject, Signal

from network.protocol import Protocol, MessageType, MessageReceiver
from utils.send_guard import SendLock


class FileSession:
    """一个待投递的文件会话（登记在复制端的 FileProvider 上）。"""

    def __init__(self, session_id: str, token: str, files: dict):
        """files: {条目名: 本机绝对路径}。"""
        self.session_id = session_id
        self.token = token
        self.files = dict(files)
        self._lock = threading.Lock()

    def resolve(self, name: str) -> str:
        """按条目名解析本机绝对路径；条目不存在返回 None。"""
        with self._lock:
            return self.files.get(name)


class FileProvider(QObject):
    """复制端目录服务：接受拉取请求并按会话提供文件字节（端到端 TCP 直连）。"""

    log_message = Signal(str)
    # 投递发送进度（服务线程 → UI）：(连接标识, session_id, 条目名, 已发送字节, 文件真实大小)
    # 连接标识区分不同接收端（同一文件被多台设备同时拉取时各行独立，互不打架）。
    # 字节数用 64 位整型（'qlonglong'）：大文件（>2GB）超出 32 位 int 会溢出为 0，
    # 导致进度分母丢失（旧"发送端 7MB/瞬间走满"根因之一）
    send_progress = Signal(str, str, str, 'qlonglong', 'qlonglong')
    # 单文件发送结束：(连接标识, session_id, 条目名, 是否成功发完 FILE_END)
    send_finished = Signal(str, str, str, bool)

    CHUNK_SIZE = 64 * 1024
    DEFAULT_START_PORT = 21300  # 独立于同步主端口的目录服务起始端口
    MAX_SYNC_SESSIONS = 8       # 自同步会话链长度上限（防无限膨胀）
    MAX_CONCURRENT_STREAMS = 5  # 同一时刻最多并行服务的文件流出站上限（发送侧限制）：
                                # 多端同时缺同一文件时防提供方一台被打爆；接收侧每端
                                # 已有各自 5 并发池，本侧与之对称，单节点最大在途流数
                                # ≈ 发送 5 + 自身接收 5 = 10，可解释且不失控。

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.port = None
        self.host = None            # 本机局域网 IP（通知给接收端用）
        self.server_socket = None
        self.sessions = {}          # session_id -> FileSession
        self._lock = threading.Lock()
        self._conns = {}            # conn_id -> {socket, receiver, send_guard}
        # 出站并发上限信号量：_stream_file 进入流式发送前获取、结束（含异常/提前返回）
        # 后释放。同一时刻仅 MAX_CONCURRENT_STREAMS 条文件流在途，其余拉取连接在槽上
        # 排队——提供方被多端同时拉取时其单点并发被钳住，接收侧另有各自 5 并发池兜底。
        self._send_slots = threading.Semaphore(self.MAX_CONCURRENT_STREAMS)

    # ---- 会话登记 ----

    def register_session(self, session_id: str, token: str, files: dict,
                         replace_all: bool = True) -> FileSession:
        """登记一个待投递会话。

        replace_all=True（默认，剪贴板语义）：登记新会话时清空全部旧会话
        （"最新为主"，与 Windows 剪贴板一致——已进行中的传输不受影响，迟到的
        旧会话拉取会被拒绝）；replace_all=False（自同步会话）：按 session_id
        保留最近若干会话链——多轮对账 REQ 会以新 token 覆盖同 id，若旧 token
        被直接清空则旧会话的在途拉取报「会话无效」→ 字节缺口永不收敛；保留
        旧会话使在途拉取仍可命中，链长度上限防无限膨胀。
        """
        session = FileSession(session_id, token, files)
        with self._lock:
            if replace_all:
                self.sessions.clear()
                self.sessions[session_id] = session
            else:
                chain = self.sessions.get(session_id)
                if isinstance(chain, list):
                    chain.append(session)
                    if len(chain) > self.MAX_SYNC_SESSIONS:
                        del chain[:len(chain) - self.MAX_SYNC_SESSIONS]
                else:
                    chain = [session]
                self.sessions[session_id] = chain
        return session

    def remove_session(self, session_id: str):
        with self._lock:
            self.sessions.pop(session_id, None)

    def get_session(self, session_id: str):
        """取最新登记的同 id 会话（剪贴板单会话/自同步链取链尾）。"""
        with self._lock:
            entry = self.sessions.get(session_id)
            if isinstance(entry, list):
                return entry[-1] if entry else None
            return entry

    def _find_session(self, session_id: str, token: str):
        """按 id+token 定位会话（兼容单会话与多会话链）。

        链中会话按登记时间追加（尾部最新、文件快照最新）。同 token（每请求方
        token 缓存固定）时取链尾最新会话：取链首会命中最早登记的快照，多轮对账
        后新增文件不在旧快照内 → 拉取误判「文件不存在」→ 该轮拉取全失败。
        旧会话（token 已更换的在途拉取）仍在链内按 token 逐项命中。
        """
        with self._lock:
            entry = self.sessions.get(session_id)
            if entry is None:
                return None
            if isinstance(entry, FileSession):
                return entry if entry.token == token else None
            for s in reversed(entry):
                if s.token == token:
                    return s
            return None

    def _verify(self, session_id: str, token: str) -> bool:
        return self._find_session(session_id, token) is not None

    # ---- 服务生命周期 ----

    def start(self, port: int = None) -> bool:
        """在本机绑定临时端口并开始监听。返回是否成功。"""
        start_port = port or self.DEFAULT_START_PORT
        for try_port in range(start_port, start_port + 10):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                sock.bind(('0.0.0.0', try_port))
                sock.listen(128)
                sock.settimeout(1.0)
            except OSError:
                try:
                    sock.close()
                except Exception:
                    pass
                continue

            self.server_socket = sock
            self.port = try_port
            self.host = self._find_local_ip()
            self.running = True
            threading.Thread(target=self._accept_loop, daemon=True).start()
            return True

        self.log_message.emit(f"文件目录服务启动失败: 端口 {start_port}-{start_port + 9} 全被占用")
        return False

    def stop(self):
        self.running = False
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
            self.sessions.clear()
        for info in conns:
            try:
                info['socket'].close()
            except Exception:
                pass
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        self.server_socket = None

    @staticmethod
    def _find_local_ip() -> str:
        """通过向网关空连接获取本机局域网 IP。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(('10.255.255.255', 1))  # 无需真实可达，连接即取本机出网口 IP
            ip = s.getsockname()[0]
            s.close()
            return ip if ip and ip != '127.0.0.1' else '127.0.0.1'
        except Exception:
            return '127.0.0.1'

    def _accept_loop(self):
        while self.running:
            try:
                client_socket, addr = self.server_socket.accept()
            except socket.timeout:
                continue
            except Exception:
                if self.running:
                    continue
                break
            conn_id = f"{addr[0]}:{addr[1]}"
            try:
                client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except Exception:
                pass
            with self._lock:
                self._conns[conn_id] = {
                    'socket': client_socket,
                    'receiver': MessageReceiver(),
                    'send_guard': SendLock(),
                    'pull_session': None,   # 当前正在服务的会话（用于发送进度上报）
                    'pull_name': None,
                    'pull_done': False,     # 是否已成功发完 FILE_END（防重复上报失败）
                }
            threading.Thread(target=self._handle_pull, args=(conn_id,), daemon=True).start()

    def _handle_pull(self, conn_id: str):
        """处理一条接收端的拉取连接：解析请求 -> 校验会话 -> 分块发送文件。"""
        with self._lock:
            info = self._conns.get(conn_id)
        if not info:
            return
        conn_socket = info['socket']
        receiver = info['receiver']
        conn_socket.settimeout(1.0)

        try:
            while self.running:
                try:
                    data = conn_socket.recv(65536)
                except socket.timeout:
                    # 网络慢/请求未达：静默等待（与同步 server._handle_client 一致），
                    # 避免网络差时把拉取连接误判为断开而提前关闭
                    continue
                if not data:
                    break
                receiver.feed(data)
                while receiver.has_complete_message():
                    message = receiver.get_message()
                    if message:
                        self._process_pull_message(conn_id, message)
                # 单条连接服务完一次拉取后即关闭（发送方主动收尾，不等对端 close）
                with self._lock:
                    done = info.get('pull_done')
                if done:
                    break
        except OSError:
            pass
        finally:
            with self._lock:
                pull_session = info.get('pull_session')
                pull_name = info.get('pull_name')
                pull_done = info.get('pull_done')
            # 连接关闭且未发完 FILE_END：向 UI 上报该文件发送失败（取消/中断）
            if pull_session and pull_name and not pull_done:
                self.send_finished.emit(conn_id, pull_session, pull_name, False)
            self._close_conn(conn_id)

    def _process_pull_message(self, conn_id: str, message):
        with self._lock:
            info = self._conns.get(conn_id)
        if not info:
            return
        msg_type, filename, file_size, mtime, hide, content = message
        # 剪贴板拉取（0x18，content=bytes JSON）与同步拉取（0x23，content 已由
        # MessageReceiver 解析为 dict）同构：统一为 {session_id, token, name}。
        if msg_type == MessageType.SYNC_PULL_REQ and isinstance(content, dict):
            req = content
        elif msg_type == MessageType.CLIPBOARD_FILE_PULL_REQ and isinstance(content, bytes):
            try:
                req = json.loads(content.decode('utf-8'))
            except Exception:
                return
        else:
            return
        try:
            session_id = req.get('session_id', '')
            token = req.get('token', '')
            name = req.get('name', '')
        except Exception:
            return

        if not self._verify(session_id, token):
            # 会话无效：本连接处理完毕，关闭让接收端快速失败（不挂起等待）
            with self._lock:
                info['pull_done'] = True
            return

        session = self._find_session(session_id, token)
        abs_path = session.resolve(name) if session else None
        if not abs_path or not os.path.isfile(abs_path):
            with self._lock:
                info['pull_done'] = True
            return

        with self._lock:
            info['pull_session'] = session_id
            info['pull_name'] = name
            info['pull_done'] = False
        ok = self._stream_file(conn_id, info['socket'], info['send_guard'],
                               session_id, name, abs_path)
        with self._lock:
            info['pull_done'] = ok

    def _stream_file(self, conn_id: str, conn_socket, send_guard: SendLock,
                     session_id: str, name: str, abs_path: str) -> bool:
        """从本机文件流式发送（与同步传输 server._send_large_file_to_client 同一套逻辑）。

        发送前取真实大小作为 FILE_BEGIN 的 file_size，分块流式发送、进度分母恒定，
        FILE_END 携带真实大小；成功发完 FILE_END 上报 send_finished(True) 并返回 True，
        任何中断返回 False（连接关闭时由 _handle_pull 兜底上报失败）。
        每条消息经 _send_retry 发送：背压超时（socket.timeout）不重发已发送字节、
        短暂退避后续发剩余部分，网络差/接收端处理慢时大文件传输不会因一次 sendall
        超时失败（与同步 _send_with_cancel 同一策略）。
        """
        # 发送前先解析目标大小/时间（不占发送槽，瞬时操作）
        try:
            total_size = os.path.getsize(abs_path)
            mtime = os.path.getmtime(abs_path)
        except OSError:
            return False
        # 全局出站并发上限：进入流式发送前获取发送槽，结束后释放。用 with 保证
        # 任何路径（发送失败/取消/异常/提前 return）都 release，不泄漏槽；等待的
        # 拉取连接在槽上自然排队，钳住提供方单点被多端同时拉取的压力。接收侧每端
        # 已有各自的 5 并发池，本侧与之对称构成双保险。
        with self._send_slots:
            ok = False
            try:
                if not self._send_retry(conn_socket, send_guard, Protocol.pack_message(
                        MessageType.FILE_BEGIN, name, total_size, False, b'', mtime)):
                    return False
                sent = 0
                with open(abs_path, 'rb') as fh:
                    chunk_index = 0
                    while self.running:
                        chunk = fh.read(self.CHUNK_SIZE)
                        if not chunk:
                            break
                        if not self._send_retry(
                                conn_socket, send_guard,
                                Protocol.create_file_data_message(name, chunk_index, chunk)):
                            return False
                        sent += len(chunk)
                        chunk_index += 1
                        self.send_progress.emit(conn_id, session_id, name, sent, total_size)
                if self.running:
                    if not self._send_retry(
                            conn_socket, send_guard,
                            Protocol.create_file_end_message(name, total_size, mtime)):
                        return False
                    ok = True
                    self.send_finished.emit(conn_id, session_id, name, True)
            except Exception:
                ok = False
            return ok

    def _send_retry(self, conn_socket, send_guard: SendLock, data: bytes) -> bool:
        """发送一条消息：可恢复发送，连接错误返回 False。

        与同步 server._send_with_cancel 同一策略：背压超时（接收端处理慢导致发送
        缓冲区满）时不重发已发送字节、短暂退避后续发剩余部分，直至整条消息完整写出
        （避免 sendall 超时后整条重发、重复数据流加剧背压）；连接错误
        （BrokenPipe/Reset/Aborted/OSError）立即返回 False。
        """
        while self.running:
            try:
                return send_guard.send_resumable(conn_socket, data)
            except Exception:
                return False
        return False

    def _close_conn(self, conn_id: str):
        with self._lock:
            info = self._conns.pop(conn_id, None)
        if info:
            try:
                info['socket'].close()
            except Exception:
                pass


def pull_file(host: str, port: int, session_id: str, token: str, name: str, dest_path: str,
              progress_cb=None, stop_event=None, msg_type: int = None,
              overall_timeout: float = None):
    """接收端单文件拉取：直连复制端目录端口，以 FILE_BEGIN/FILE_DATA/FILE_END 流式收文件。

    与同步接收同一套逻辑：FILE_BEGIN 携带真实大小作为进度分母（恒定），分块顺序写入，
    FILE_END 校验完整度（实际接收字节数 == FILE_BEGIN 的真实大小，不一致则丢弃）。

    Args:
        host: 复制端局域网 IP
        port: 复制端 FileProvider 端口
        session_id / token / name: 拉取目标
        dest_path: 接收端写入的最终路径
        progress_cb: 可选，回调 (received_bytes, total_bytes)
        stop_event: 可选，threading.Event。置位时中止拉取并清理临时文件（窗口关闭/传输取消）。
        msg_type: 拉取请求消息类型。None=剪贴板（0x18）；阶段 2 同步拉取传
            MessageType.SYNC_PULL_REQ（0x23，同构复用 FileProvider 会话校验）。
        overall_timeout: 可选，数据阶段整体超时（秒）。对端半开（断电/拔线无
            FIN/RST）时 recv 永不返回，调用方（如自同步单 worker）会无限阻塞；
            传入则超时中止拉取并清理临时文件。None 保持"静默无限等待"语义
            （投递路径沿用，发送端打开/读取慢时继续等待）。

    Returns:
        (成功?, 实际接收字节数, 错误消息)
    """
    total = 0
    got_end = False
    expected_total = 0
    mtime = 0.0
    cancelled = False
    timed_out = False
    conn = None
    # 建连阶段本地重试（数据阶段不重试，不违背"失败不重发"）：打掉弱网瞬时抖动
    # （SYN 丢失 / 拥塞 / backlog 满被拒）。广播刚发出时复制端必然在线，慢 SYN 少见，
    # 单次超时降到 6s；尝试间轮询 stop_event，关窗立即中断。
    for attempt in range(3):
        if stop_event is not None and stop_event.is_set():
            return False, 0, "已取消"
        try:
            conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # 增大 TCP 收发缓冲（与同步传输 client.connect_to_server 同款）：
            # 接收端窗口由 SO_RCVBUF 决定，默认 64KB 会限制大文件吞吐（实测投递比同步慢 ~1.5 倍）
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except Exception:
                pass
            conn.settimeout(6.0)   # 单次建连超时：复制端刚广播过必然在线，6s 足够，重试兜底瞬断
            conn.connect((host, port))
            break
        except OSError as e:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            if attempt >= 2:
                return False, 0, f"无法连接复制端 {host}:{port}: {e}"
            if stop_event is not None and stop_event.is_set():
                return False, 0, "已取消"
            time.sleep(0.5 if attempt == 0 else 1.0)

    # recv 超时 1s 供 stop_event 轮询（弱网语义不变：timeout 后 continue 静默等待，
    # 发送端打开/读取慢时依然无限等待；仅新增取消响应能力与整体超时兜底）
    conn.settimeout(1.0)
    deadline = None
    if overall_timeout:
        deadline = time.monotonic() + overall_timeout
    receiver = MessageReceiver()

    # 目标路径准备（写入临时文件，成功后再原子改名，避免失败留下半成品）
    dest_dir = os.path.dirname(os.path.abspath(dest_path)) or '.'
    os.makedirs(dest_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix='.tcp_', suffix='.part', dir=dest_dir)
    try:
        with os.fdopen(tmp_fd, 'wb') as fh:
            if msg_type == MessageType.SYNC_PULL_REQ:
                conn.sendall(Protocol.create_sync_pull_req(session_id, token, name))
            else:
                conn.sendall(Protocol.create_pull_request(session_id, token, name))
            while True:
                if stop_event is not None and stop_event.is_set():
                    cancelled = True
                    break
                try:
                    raw = conn.recv(65536)
                except socket.timeout:
                    # 整体超时兜底：对端半开（断电/拔线）时 recv 永不返回，超时中止
                    if deadline is not None and time.monotonic() > deadline:
                        timed_out = True
                        break
                    continue  # 发送端打开/读取大文件时静默等待（与同步接收一致）
                if not raw:
                    break  # 对端关闭：若已收完（got_end）才算成功，否则视为失败
                receiver.feed(raw)
                while receiver.has_complete_message():
                    mtype, fname, fsize, msg_mtime, _hide, content = receiver.get_message()
                    if mtype == MessageType.FILE_BEGIN:
                        expected_total = fsize  # 发送端开流时的真实大小（进度分母，恒定）
                        mtime = msg_mtime
                    elif mtype == MessageType.FILE_DATA:
                        _chunk_index, chunk = content
                        fh.write(chunk)
                        total += len(chunk)
                        if progress_cb:
                            progress_cb(total, expected_total)
                    elif mtype == MessageType.FILE_END:
                        got_end = True
                        break
                    else:
                        continue
                if got_end:
                    break
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        _safe_remove(tmp_path)
        return False, total, f"拉取失败: {e}"
    # 关闭连接独立收尾：文件已完整收完时 close 异常不推翻已成功的下载
    try:
        conn.close()
    except Exception:
        pass

    if not got_end:
        _safe_remove(tmp_path)
        if cancelled:
            return False, total, "已取消"
        if timed_out:
            return False, total, "拉取超时"
        return False, total, "复制端未提供数据（会话无效或文件不存在）"

    # 完整度校验（与同步接收一致）：FILE_END 携带真实大小，实际接收字节数不符则丢弃
    if expected_total > 0 and total != expected_total:
        _safe_remove(tmp_path)
        return False, total, f"文件不完整（实际 {total}/期望 {expected_total}）"

    # 完成：原子替换为目标文件，恢复源文件修改时间；
    # 落盘改名失败时清理临时文件并报错，不留半成品与泄漏。
    # Windows 独占锁：本端 FileProvider 正对外服务同一文件（open 'rb' 共享读/写
    # 但不共享删除）时，os.replace 需对目标取得删除权而撞 WinError 5（访问/共享
    # 冲突）——短退避重试数轮等服务线程释放句柄后再改，避免拉取整轮失败、字节
    # 缺口反复重入对账导致「来回拉取」。服务线程通常毫秒级完成，重试即收敛。
    last_err = None
    for _attempt in range(5):
        try:
            os.replace(tmp_path, dest_path)
            last_err = None
            break
        except PermissionError as e:
            last_err = e
            time.sleep(0.05 * (_attempt + 1))
        except OSError as e:
            last_err = e
            break
    if last_err is not None:
        _safe_remove(tmp_path)
        return False, total, f"写入目标文件失败: {last_err}"
    if mtime:
        try:
            os.utime(dest_path, (mtime, mtime))
        except Exception:
            pass
    return True, total, ""


def _safe_remove(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
