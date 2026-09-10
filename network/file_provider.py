"""分布式文件轻量服务（局域网剪切板 - 文件/图片 P2P 直连）。

架构约定（与 PRD 分布式微服务一致）：
- 每台设备在房间就绪后运行一个 FileProvider：绑定临时端口监听，负责向"接收端"提供
  本机被复制的文件/图片字节。
- 复制端登记一个文件会话（session_id/token/本地绝对路径表）。主机只转发会话元信息，
  文件字节不经过主机。
- 接收端按元信息直连复制端 FileProvider，发送 CLIPBOARD_FILE_PULL_REQ，接收
  P2P_FILE_DATA 分块，写出到目标位置。并发上限 5、失败不重发（由上层浏览器/队列控制）。

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
        """files: {条目名: 本机绝对路径}。大小在提供时按文件实际大小读取。"""
        self.session_id = session_id
        self.token = token
        self.files = dict(files)  # {name: abs_path}
        self.total_bytes = 0
        self._lock = threading.Lock()
        for _name, path in self.files.items():
            try:
                self.total_bytes += os.path.getsize(path)
            except OSError:
                self.total_bytes += 0

    def resolve(self, name: str) -> str:
        """按条目名解析本机绝对路径；条目不存在返回 None。"""
        with self._lock:
            return self.files.get(name)


class FileProvider(QObject):
    """复制端目录服务：接受 P2P 拉取请求并按会话提供文件字节。"""

    log_message = Signal(str)

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
            except Exception:
                pass
            with self._lock:
                self._conns[conn_id] = {
                    'socket': client_socket,
                    'receiver': MessageReceiver(),
                    'send_guard': SendLock(),
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
            offset = int(req.get('offset', 0))
        except Exception:
            return

        if not self._verify(session_id, token):
            return  # 会话无效：直接关闭连接，不发送任何文件

        session = self.get_session(session_id)
        abs_path = session.resolve(name) if session else None
        if not abs_path or not os.path.isfile(abs_path):
            return

        self._stream_file(info['socket'], info['send_guard'], name, abs_path, offset)

    def _stream_file(self, conn_socket, send_guard: SendLock, name: str, abs_path: str, offset: int):
        """从本机文件按 offset 起读取并以 P2P_FILE_DATA 分块发送。"""
        total_size = os.path.getsize(abs_path)
        try:
            with open(abs_path, 'rb') as fh:
                fh.seek(offset)
                sent = 0
                while self.running:
                    chunk = fh.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    sent += len(chunk)
                    is_last = (offset + sent) >= total_size
                    msg = Protocol.create_p2p_data(name, offset + sent - len(chunk), total_size, chunk, is_last)
                    send_guard.send(conn_socket, msg)
                    if is_last:
                        break
        except Exception:
            pass

    def _close_conn(self, conn_id: str):
        with self._lock:
            info = self._conns.pop(conn_id, None)
        if info:
            try:
                info['socket'].close()
            except Exception:
                pass


def pull_file(host: str, port: int, session_id: str, token: str, name: str, dest_path: str,
              offset: int = 0, progress_cb=None):
    """接收端 P2P 单文件拉取：直连复制端目录端口，写文件到 dest_path（原子替换）。

    Args:
        host: 复制端局域网 IP
        port: 复制端 FileProvider 端口
        session_id / token / name: 拉取目标
        dest_path: 接收端写入的最终路径
        offset: 起始偏移（阶段A固定 0）
        progress_cb: 可选，回调 (received_bytes, total_bytes)

    Returns:
        (成功?, 实际接收字节数, 错误消息)
    """
    total = 0
    got_last = False
    try:
        conn = socket.create_connection((host, port), timeout=5.0)
    except OSError as e:
        return False, 0, f"无法连接复制端 {host}:{port}: {e}"

    conn.settimeout(5.0)
    receiver = MessageReceiver()
    send_guard = SendLock()

    # 目标路径准备（写入临时文件，成功后再原子改名，避免失败留下半成品）
    dest_dir = os.path.dirname(os.path.abspath(dest_path)) or '.'
    os.makedirs(dest_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix='.p2p_', suffix='.part', dir=dest_dir)
    try:
        with os.fdopen(tmp_fd, 'wb') as fh:
            send_guard.send(conn, Protocol.create_pull_request(session_id, token, name, offset))
            while True:
                raw = conn.recv(65536)
                if not raw:
                    break  # 对端关闭：若已收完（got_last）才算成功，否则视为失败
                receiver.feed(raw)
                while receiver.has_complete_message():
                    mtype, fname, fsize, _mtime, _hide, content = receiver.get_message()
                    if mtype != MessageType.P2P_FILE_DATA:
                        continue
                    _off, total_size, is_last, payload = Protocol.unpack_p2p_data(content)
                    fh.write(payload)
                    total += len(payload)
                    if progress_cb:
                        progress_cb(total, total_size)
                    if is_last:
                        got_last = True
                if got_last:
                    break
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        _safe_remove(tmp_path)
        return False, total, f"拉取失败: {e}"

    if not got_last or total == 0:
        _safe_remove(tmp_path)
        return False, total, "复制端未提供数据（会话无效或文件不存在）"

    # 完成：原子替换为目标文件
    os.replace(tmp_path, dest_path)
    return True, total, ""


def _safe_remove(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass