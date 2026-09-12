"""分布式文件轻量服务（局域网剪切板 - 文件/图片 端到端 TCP 直连）。

架构约定（与 PRD 分布式微服务一致）：
- 每台设备在房间就绪后运行一个 FileProvider：绑定临时端口监听，负责向"接收端"提供
  本机被复制的文件/图片字节。
- 复制端登记一个文件会话（session_id/token/本地绝对路径表）。主机只转发会话元信息，
  文件字节不经过主机。
- 接收端按元信息直连复制端 FileProvider，发送 CLIPBOARD_FILE_PULL_REQ，接收
  FILE_BEGIN/FILE_DATA/FILE_END 流式分块（与主机同步分发同一套协议），写出到目标位置。
- 传输逻辑与同步文件传输（server._send_large_file_to_client / 服务端接收循环）同一套：
  发送前取真实大小作为 FILE_BEGIN 的 file_size，分块流式发送、进度分母恒定，
  FILE_END 携带真实大小并校验完整度（不符则丢弃）。并发上限 5、失败不重发。

路径安全：只按「会话 + 条目名」查本机登记表中的绝对路径，拒绝任意路径请求，天然防穿越。
"""
import json
import os
import socket
import tempfile
import threading

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
    # 投递发送进度（服务线程 → UI）：(session_id, 条目名, 已发送字节, 文件真实大小)
    # 字节数用 64 位整型（'qlonglong'）：大文件（>2GB）超出 32 位 int 会溢出为 0，
    # 导致进度分母丢失（旧"发送端 7MB/瞬间走满"根因之一）
    send_progress = Signal(str, str, 'qlonglong', 'qlonglong')
    # 单文件发送结束：(session_id, 条目名, 是否成功发完 FILE_END)
    send_finished = Signal(str, str, bool)

    CHUNK_SIZE = 64 * 1024
    DEFAULT_START_PORT = 21300  # 独立于同步主端口的目录服务起始端口

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.port = None
        self.host = None            # 本机局域网 IP（通知给接收端用）
        self.server_socket = None
        self.sessions = {}          # session_id -> FileSession
        self._lock = threading.Lock()
        self._conns = {}            # conn_id -> {socket, receiver, send_guard}

    # ---- 会话登记 ----

    def register_session(self, session_id: str, token: str, files: dict) -> FileSession:
        """登记一个待投递会话；同 session_id 的旧会话会被新会话顶掉（"最新为主"）。"""
        session = FileSession(session_id, token, files)
        with self._lock:
            self.sessions[session_id] = session
        return session

    def remove_session(self, session_id: str):
        with self._lock:
            self.sessions.pop(session_id, None)

    def get_session(self, session_id: str):
        with self._lock:
            return self.sessions.get(session_id)

    def _verify(self, session_id: str, token: str) -> bool:
        session = self.get_session(session_id)
        return session is not None and session.token == token

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
                sock.listen(50)
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
                data = conn_socket.recv(65536)
                if not data:
                    break
                receiver.feed(data)
                while receiver.has_complete_message():
                    message = receiver.get_message()
                    if message:
                        self._process_pull_message(conn_id, message)
            # 单条连接服务完一次拉取后即关闭
        except OSError:
            pass
        finally:
            with self._lock:
                pull_session = info.get('pull_session')
                pull_name = info.get('pull_name')
                pull_done = info.get('pull_done')
            # 连接关闭且未发完 FILE_END：向 UI 上报该文件发送失败（取消/中断）
            if pull_session and pull_name and not pull_done:
                self.send_finished.emit(pull_session, pull_name, False)
            self._close_conn(conn_id)

    def _process_pull_message(self, conn_id: str, message):
        with self._lock:
            info = self._conns.get(conn_id)
        if not info:
            return
        msg_type, filename, file_size, mtime, hide, content = message
        if msg_type != MessageType.CLIPBOARD_FILE_PULL_REQ:
            return
        try:
            req = json.loads(content.decode('utf-8'))
            session_id = req.get('session_id', '')
            token = req.get('token', '')
            name = req.get('name', '')
        except Exception:
            return

        if not self._verify(session_id, token):
            return  # 会话无效：直接关闭连接，不发送任何文件

        session = self.get_session(session_id)
        abs_path = session.resolve(name) if session else None
        if not abs_path or not os.path.isfile(abs_path):
            return

        with self._lock:
            info['pull_session'] = session_id
            info['pull_name'] = name
            info['pull_done'] = False
        ok = self._stream_file(info['socket'], info['send_guard'], session_id, name, abs_path)
        with self._lock:
            info['pull_done'] = ok

    def _stream_file(self, conn_socket, send_guard: SendLock, session_id: str, name: str,
                     abs_path: str) -> bool:
        """从本机文件流式发送（与同步传输 server._send_large_file_to_client 同一套逻辑）。

        发送前取真实大小作为 FILE_BEGIN 的 file_size，分块流式发送、进度分母恒定，
        FILE_END 携带真实大小；成功发完 FILE_END 上报 send_finished(True) 并返回 True，
        任何中断返回 False（连接关闭时由 _handle_pull 兜底上报失败）。
        """
        try:
            total_size = os.path.getsize(abs_path)
            mtime = os.path.getmtime(abs_path)
        except OSError:
            return False
        ok = False
        try:
            send_guard.send(conn_socket, Protocol.pack_message(
                MessageType.FILE_BEGIN, name, total_size, False, b'', mtime))
            sent = 0
            with open(abs_path, 'rb') as fh:
                chunk_index = 0
                while self.running:
                    chunk = fh.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    send_guard.send(conn_socket,
                                    Protocol.create_file_data_message(name, chunk_index, chunk))
                    sent += len(chunk)
                    chunk_index += 1
                    self.send_progress.emit(session_id, name, sent, total_size)
            if self.running:
                send_guard.send(conn_socket,
                                Protocol.create_file_end_message(name, total_size, mtime))
                ok = True
                self.send_finished.emit(session_id, name, True)
        except Exception:
            ok = False
        return ok

    def _close_conn(self, conn_id: str):
        with self._lock:
            info = self._conns.pop(conn_id, None)
        if info:
            try:
                info['socket'].close()
            except Exception:
                pass


def pull_file(host: str, port: int, session_id: str, token: str, name: str, dest_path: str,
              progress_cb=None):
    """接收端单文件拉取：直连复制端目录端口，以 FILE_BEGIN/FILE_DATA/FILE_END 流式收文件。

    与同步接收同一套逻辑：FILE_BEGIN 携带真实大小作为进度分母（恒定），分块顺序写入，
    FILE_END 校验完整度（实际接收字节数 == FILE_BEGIN 的真实大小，不一致则丢弃）。

    Args:
        host: 复制端局域网 IP
        port: 复制端 FileProvider 端口
        session_id / token / name: 拉取目标
        dest_path: 接收端写入的最终路径
        progress_cb: 可选，回调 (received_bytes, total_bytes)

    Returns:
        (成功?, 实际接收字节数, 错误消息)
    """
    total = 0
    got_end = False
    expected_total = 0
    mtime = 0.0
    try:
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 增大 TCP 收发缓冲（与同步传输 client.connect_to_server 同款）：
        # 接收端窗口由 SO_RCVBUF 决定，默认 64KB 会限制大文件吞吐（实测投递比同步慢 ~1.5 倍）
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except Exception:
            pass
        conn.settimeout(5.0)
        conn.connect((host, port))
    except OSError as e:
        return False, 0, f"无法连接复制端 {host}:{port}: {e}"

    conn.settimeout(30.0)
    receiver = MessageReceiver()

    # 目标路径准备（写入临时文件，成功后再原子改名，避免失败留下半成品）
    dest_dir = os.path.dirname(os.path.abspath(dest_path)) or '.'
    os.makedirs(dest_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix='.tcp_', suffix='.part', dir=dest_dir)
    try:
        with os.fdopen(tmp_fd, 'wb') as fh:
            conn.sendall(Protocol.create_pull_request(session_id, token, name))
            while True:
                try:
                    raw = conn.recv(65536)
                except socket.timeout:
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
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        _safe_remove(tmp_path)
        return False, total, f"拉取失败: {e}"

    if not got_end:
        _safe_remove(tmp_path)
        return False, total, "复制端未提供数据（会话无效或文件不存在）"

    # 完整度校验（与同步接收一致）：FILE_END 携带真实大小，实际接收字节数不符则丢弃
    if expected_total > 0 and total != expected_total:
        _safe_remove(tmp_path)
        return False, total, f"文件不完整（实际 {total}/期望 {expected_total}）"

    # 完成：原子替换为目标文件，恢复源文件修改时间
    os.replace(tmp_path, dest_path)
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
