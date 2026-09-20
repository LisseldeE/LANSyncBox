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
    MAX_RESUME_ATTEMPTS = 3     # 断点续传重连上限（接收端）：连接中断且有真实向前进度
                                # 时，保留临时文件、带 offset 重连续传；无进度/会话失效
                                # 等不可续传故障不重试，避免对已死的提供方反复连接。

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
        # 在途流式发送的接管表：(session_id, name) -> conn_id。
        # 同一接收端对同一文件重连续传时，新拉取连接接管（作废）旧的半开连接，
        # 保证同文件同端只有一条在途发送、无"旧+新并发"。键用 session_id 而非
        # client_ip：自同步会话 sync_{end_id} 每接收端唯一，接收端重连换 IP 时
        # 仍能命中同一键正确接管（不依赖动态 IP）；剪贴板广播会话由接收端各自
        # 拉取时以 token 校验与会话解析区分。
        self._active_streams = {}
        # 跨通道互斥（阶段 6）：指向在途投递登记表（SyncServer 经 set_deliver_controller
        # 注入）。服务 sync_{end_id} 自同步拉取时登记 'B'——主机直推(A)据此让位，避免
        # 同文件两通道重复投递。None 时不登记（独立/单测场景，保持向后兼容）。
        self._deliver_controller = None

    # ---- 会话登记 ----

    def set_deliver_controller(self, controller):
        """注入跨通道互斥登记表（SyncServer）；None 时不登记（独立/单测场景）。"""
        self._deliver_controller = controller

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
                    'client_ip': addr[0],   # 接收端 IP（同文件接管去重的键之一）
                    'cancel': threading.Event(),  # 被新拉取接管时置位，中断在途流式发送
                    'superseded': False,    # 已被同文件新连接接管（忽略其失败上报）
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
                superseded = info.get('superseded', False)
            # 连接关闭且未发完 FILE_END：向 UI 上报该文件发送失败（取消/中断）。
            # 被同文件新拉取连接接管（superseded）是断点续传的预期接管，非失败，
            # 不上报以免 UI 误弹"发送取消"。
            if pull_session and pull_name and not pull_done and not superseded:
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
            offset = int(req.get('offset', 0) or 0)
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
        cancel_event = info.get('cancel')
        ok = self._stream_file(conn_id, info['socket'], info['send_guard'],
                               session_id, name, abs_path, offset, cancel_event)
        with self._lock:
            info['pull_done'] = ok

    def _stream_file(self, conn_id: str, conn_socket, send_guard: SendLock,
                     session_id: str, name: str, abs_path: str,
                     offset: int = 0,
                     cancel_event=None) -> bool:
        """从本机文件流式发送（与同步传输 server._send_large_file_to_client 同一套逻辑）。

        发送前取真实大小作为 FILE_BEGIN 的 file_size，分块流式发送、进度分母恒定，
        FILE_END 携带真实大小；成功发完 FILE_END 上报 send_finished(True) 并返回 True，
        任何中断返回 False（连接关闭时由 _handle_pull 兜底上报失败）。
        offset: 断点续传起始字节（接收端已有该长度字节，据此 seek 后继传；0=从头）。
                FILE_BEGIN 仍携带完整真实大小（进度分母恒定），进度从 offset 起算。
        cancel_event: 同文件接管去重的中断信号。同一接收端（以 session_id 标识——
            自同步会话 sync_{end_id} 每接收端唯一）对同一文件重连续传时，本连接成为
            该 (session_id, name) 的新 owner，并作废旧 owner 连接（置其 cancel + 关
            socket），从而同文件同端始终只有一条在途流式发送，不会出现"旧+新并发"。
            接管去重不依赖 client_ip：接收端重连换 IP（如 VM 192.168→172.125）时仍能
            正确接管，靠稳定的端身份而非动态 IP。
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
        # 断点续传：钳制偏移量到文件范围内（offset==total_size 时已收全，发完即收尾）
        offset = max(0, min(int(offset or 0), total_size))
        # 同文件接管去重：本连接登记为 owner，并把仍在服务的旧 owner 作废（半开连接
        # 在新连接到来后被关停，只有新连接继续按 offset 续传，无第二条同文件发送）。
        # 键 (session_id, name) 不依赖 client_ip：自同步会话 sync_{end_id} 每接收端
        # 唯一，重连换 IP 也能命中同一键正确接管旧连接。
        key = (session_id, name)
        with self._lock:
            prev = self._active_streams.get(key)
            self._active_streams[key] = conn_id
        if prev is not None and prev != conn_id:
            self._supersede_takeover(key, prev)
        # 跨通道互斥（阶段 6）：自同步拉取会话 sync_{end_id} 服务开始前，在主机端共享
        # 登记表登记 'B'——主机直推(A)与同端同文件竞争时据此让位，避免两通道重复投递；
        # finally 统一注销。仅 sync_* 会话（自同步）参与；剪贴板会话不登记、保持原行为。
        deliver_end = None
        if (self._deliver_controller is not None and session_id.startswith('sync_')
                and len(session_id) > 5):
            deliver_end = session_id[5:]
            try:
                self._deliver_controller._mark_delivering(deliver_end, name, 'B')
            except Exception:
                pass
        try:
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
                    sent = offset
                    with open(abs_path, 'rb') as fh:
                        fh.seek(offset)
                        chunk_index = 0
                        while (self.running
                               and not (cancel_event is not None and cancel_event.is_set())):
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
                    if (self.running
                            and not (cancel_event is not None and cancel_event.is_set())):
                        if not self._send_retry(
                                conn_socket, send_guard,
                                Protocol.create_file_end_message(name, total_size, mtime)):
                            return False
                        ok = True
                        self.send_finished.emit(conn_id, session_id, name, True)
                except Exception:
                    ok = False
                return ok
        finally:
            # 发送结束（成功/失败/被接管）：若本连接仍是该文件的 owner 则移除，避免接管表残留
            with self._lock:
                still_owner = self._active_streams.get(key) == conn_id
                if still_owner:
                    self._active_streams.pop(key, None)
            # 跨通道 'B' 登记仅在【本连接仍为 owner】时才注销：被同文件新连接接管
            # （断点续传）时旧连接不得清掉新连接仍占用的投递标记，否则主机直推(A)会
            # 误判 B 已结束而并发直推同一文件。
            if deliver_end is not None and still_owner:
                try:
                    self._deliver_controller._unmark_delivering(deliver_end, name, 'B')
                except Exception:
                    pass

    def _supersede_takeover(self, key, old_conn_id: str):
        """同一接收端对同一文件重连续传时，作废仍在服务该文件的旧连接。

        置 old 连接的 cancel 事件并关闭其 socket：使其在途 _stream_file 的 while
        循环立刻退出；若正阻塞在 send()（半开连接背压）则经 send 超时（≤1s）被关
        socket 打断而返回 False。旧连接被标 superseded，_handle_pull 收尾不再误报
        失败。多条新连接几乎同时到达时，只有最新一条保任 owner，旧的全被作废。
        """
        with self._lock:
            info = self._conns.get(old_conn_id)
        if not info:
            return
        info['superseded'] = True
        cancel = info.get('cancel')
        if cancel is not None:
            cancel.set()
        try:
            info['socket'].close()
        except Exception:
            pass

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


def _connect_provider(host: str, port: int, stop_event):
    """直连复制端 FileProvider（建连阶段本地重试，打掉弱网瞬时抖动）。

    SYN 丢失 / 拥塞 / backlog 满被拒等瞬时故障重试 3 次；广播刚发出时复制端必然
    在线，慢 SYN 少见。尝试间轮询 stop_event，关窗立即返回 None。返回已连接的
    socket 或 None（最终失败）。复用同一套大缓冲设置（接收端窗口由 SO_RCVBUF
    决定，默认 64KB 会限制大文件吞吐）。
    """
    conn = None
    for attempt in range(3):
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except Exception:
                pass
            conn.settimeout(6.0)   # 单次建连超时：复制端刚广播过必然在线，6s 足够，重试兜底瞬断
            conn.connect((host, port))
            return conn
        except OSError:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            if attempt >= 2:
                return None
            if stop_event is not None and stop_event.is_set():
                return None
            time.sleep(0.5 if attempt == 0 else 1.0)
    return None


def pull_file(host: str, port: int, session_id: str, token: str, name: str, dest_path: str,
              progress_cb=None, stop_event=None, msg_type: int = None,
              overall_timeout: float = None, max_resume: int = None):
    """接收端单文件拉取：直连复制端目录端口，以 FILE_BEGIN/FILE_DATA/FILE_END 流式收文件。

    与同步接收同一套逻辑：FILE_BEGIN 携带真实大小作为进度分母（恒定），分块顺序写入，
    FILE_END 校验完整度（实际接收字节数 == FILE_BEGIN 的真实大小，不一致则丢弃）。

    断点续传：连接中断且本文件已有真实向前进度时，保留临时文件、带 offset（已有
    字节数）重连请求续传，发送端据此 seek 后从断点继续，不从头全量重来；最多重连
    max_resume 次（默认 MAX_RESUME_ATTEMPTS）。无新字节的可续传故障（会话失效/源端
    无文件/对端一次字节未发即断开）不重试，避免对已死的提供方反复连接。

    Args:
        host: 复制端局域网 IP
        port: 复制端 FileProvider 端口
        session_id / token / name: 拉取目标
        dest_path: 接收端写入的最终路径
        progress_cb: 可选，回调 (received_bytes, total_bytes)（received 含续传前已收字节）
        stop_event: 可选，threading.Event。置位时中止拉取并清理临时文件（窗口关闭/传输取消）。
        msg_type: 拉取请求消息类型。None=剪贴板（0x18）；阶段 2 同步拉取传
            MessageType.SYNC_PULL_REQ（0x23，同构复用 FileProvider 会话校验）。
        overall_timeout: 可选，数据阶段整体超时（秒）。对端半开（断电/拔线无
            FIN/RST）时 recv 永不返回，调用方（如自同步单 worker）会无限阻塞；
            传入则超时中止拉取并清理临时文件。None 保持"静默无限等待"语义
            （投递路径沿用，发送端打开/读取慢时继续等待）。
        max_resume: 断点续传重连次数上限（None=用 FileProvider.MAX_RESUME_ATTEMPTS）

    Returns:
        (成功?, 实际接收字节数, 错误消息)
    """
    dest_dir = os.path.dirname(os.path.abspath(dest_path)) or '.'
    os.makedirs(dest_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix='.tcp_', suffix='.part', dir=dest_dir)
    resume_attempts = max_resume if max_resume is not None \
        else FileProvider.MAX_RESUME_ATTEMPTS

    if stop_event is not None and stop_event.is_set():
        _safe_remove(tmp_path)
        return False, 0, "已取消"

    deadline = None
    if overall_timeout:
        deadline = time.monotonic() + overall_timeout

    # 断点续传状态（跨重连共享同一临时文件句柄与计数）：
    written = 0          # 临时文件当前字节数 == 下次续传的 offset
    expected_total = 0   # FILE_BEGIN 携带的真实大小（进度分母，恒定）
    mtime = 0.0
    ok = False
    cancelled = False
    timed_out = False
    got_end = False

    try:
        with os.fdopen(tmp_fd, 'rb+') as fh:
            for attempt_no in range(resume_attempts):
                if stop_event is not None and stop_event.is_set():
                    cancelled = True
                    break
                conn = _connect_provider(host, port, stop_event)
                if conn is None:
                    if stop_event is not None and stop_event.is_set():
                        cancelled = True
                        break
                    break  # 建连 3 次全失败：非瞬时，放弃（同样不再续传）
                try:
                    # recv 超时 1s 供 stop_event 轮询（弱网语义不变：timeout 后 continue
                    # 静默等待，发送端打开/读取慢时依然无限等待；仅新增取消与整体超时兜底）
                    conn.settimeout(1.0)
                    receiver = MessageReceiver()
                    fh.seek(0, os.SEEK_END)          # 追加写入（position 移到已收端）
                    offset = written
                    if msg_type == MessageType.SYNC_PULL_REQ:
                        conn.sendall(Protocol.create_sync_pull_req(
                            session_id, token, name, offset))
                    else:
                        conn.sendall(Protocol.create_pull_request(
                            session_id, token, name, offset))
                    attempt_start = written
                    got_end = False
                    while True:
                        if stop_event is not None and stop_event.is_set():
                            cancelled = True
                            break
                        try:
                            raw = conn.recv(65536)
                        except socket.timeout:
                            # 整体超时兜底：对端半开（断电/拔线）时 recv 永不返回
                            if deadline is not None and time.monotonic() > deadline:
                                timed_out = True
                                break
                            continue  # 发送端打开/读取大文件时静默等待（与同步接收一致）
                        if not raw:
                            break  # 对端关闭：若未收完，按是否有本段进度决定续传或放弃
                        receiver.feed(raw)
                        while receiver.has_complete_message():
                            mtype, fname, fsize, msg_mtime, _hide, content = receiver.get_message()
                            if mtype == MessageType.FILE_BEGIN:
                                expected_total = fsize  # 发送端开流时的真实大小（进度分母，恒定）
                                mtime = msg_mtime
                            elif mtype == MessageType.FILE_DATA:
                                _chunk_index, chunk = content
                                fh.write(chunk)
                                written += len(chunk)
                                if progress_cb:
                                    progress_cb(written, expected_total)
                            elif mtype == MessageType.FILE_END:
                                got_end = True
                                break
                            else:
                                continue
                        if got_end:
                            break
                except OSError:
                    pass  # 连接中断（Reset/BrokenPipe/Aborted）：按本段是否收过字节决定续传
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if cancelled or timed_out:
                    break
                if got_end:
                    ok = (expected_total <= 0) or (written == expected_total)
                    break
                # 未收完：
                if written == attempt_start:
                    # 本段无任何新字节即中断 → 会话无效/源端无文件/对端立即关闭，
                    # 这类故障不可续传（重连也不会来字节），放弃
                    break
                # 本段有真实前进 → 断点续传：进入下一轮，从 written 重连续传
    except Exception as e:
        _safe_remove(tmp_path)
        return False, written, f"拉取失败: {e}"

    # 收尾（成功路径复用原原子替换 + mtime 恢复；任何失败清理临时文件）
    if ok and got_end:
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
            return False, written, f"写入目标文件失败: {last_err}"
        if mtime:
            try:
                os.utime(dest_path, (mtime, mtime))
            except Exception:
                pass
        return True, written, ""

    _safe_remove(tmp_path)
    if cancelled:
        return False, written, "已取消"
    if timed_out:
        return False, written, "拉取超时"
    if got_end:
        # 收到 FILE_END 但字节数不符（含续传场景下文件在传输期间被改动）
        return False, written, f"文件不完整（实际 {written}/期望 {expected_total}）"
    return False, written, "复制端未提供数据（会话无效或文件不存在）"


def _safe_remove(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
