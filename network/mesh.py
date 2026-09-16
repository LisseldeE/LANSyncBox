"""
去中心化同步：网状直连连接管理
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.

职责（对应实施计划阶段 0）：
- 对端发现引导：主机下发 MESH_PEER_LIST，各端据此建直连
- 双向直连建连（防半开）：每端同时监听 + 主动拨号，双方都拨号时按
  「end_id 字典序小者作为连接发起方」的确定性规则保留唯一连接
- 断线退避重连：直连断开自动重连（退避递增，成功后重置）
- MESH_PEER_JOIN / MESH_PEER_LEAVE 生命周期：新端加入建连、端离线拆除
- 端身份：每条网状连接建立后立即交换 END_INFO（含 mesh_port）

传输约定与全库一致：线程 + socket 1s 超时 + SendLock.send_resumable 背压退避。
"""
import socket
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

from config import Config, UserConfig
from network.protocol import Protocol, MessageType, MessageReceiver
from sync.vector import Endpoint
from utils.send_guard import SendLock


class MeshManager(QObject):
    """网状连接管理器：本端常驻监听端口，维护对端表与直连连接集合。"""

    log_message = Signal(str)
    peer_connected = Signal(str, str)      # (end_id, name)
    peer_disconnected = Signal(str)        # (end_id)
    # (end_id, msg_type, filename, file_size, mtime, hide_flag, content)
    mesh_message = Signal(str, int, str, int, float, bool, object)

    DEFAULT_MESH_PORT = 21400   # 独立于同步主端口(9527)与投递目录服务(21300)的网状监听端口起始值
    MESH_PORT_RANGE = 10        # 端口尝试范围（21300 同款策略）
    RECONNECT_BASE = 2.0        # 断线重连初始退避（秒）
    RECONNECT_MAX = 30.0        # 断线重连最大退避（秒）
    HEARTBEAT_INTERVAL = 2.0    # 网状连接心跳间隔（秒）

    def __init__(self, name: str = '', end_id: str = '', parent=None):
        super().__init__(parent)
        self.end_id = end_id or UserConfig.get_end_id()
        self.name = name or socket.gethostname()
        self.running = False
        self.port = None
        self.mesh_port = 0
        self.ip = self._find_local_ip()
        self.server_socket = None

        self._lock = threading.Lock()
        self.peers: dict = {}        # end_id -> Endpoint（对端表）
        self.conns: dict = {}        # end_id -> {socket, receiver, send_guard, role, name, heartbeat_stop}
        self._reconnect_threads: dict = {}   # end_id -> Thread
        self._reconnect_wait = threading.Event()  # 唤醒所有重连线程（stop 用）
        self._on_message = None      # 可选回调 on_message(end_id, message)

    # ---- 属性 ----

    @property
    def endpoint(self) -> Endpoint:
        """本端端点信息（供广播/引导清单使用）。"""
        return Endpoint(end_id=self.end_id, name=self.name, ip=self.ip, mesh_port=self.mesh_port)

    def connected_end_ids(self) -> set:
        """当前已建立网状直连的对端 end_id 集合。"""
        with self._lock:
            return set(self.conns.keys())

    def get_peer(self, end_id: str) -> Optional[Endpoint]:
        with self._lock:
            return self.peers.get(end_id)

    def set_message_handler(self, cb):
        """设置消息回调 cb(end_id, message)；None 表示仅发 Qt 信号。"""
        self._on_message = cb

    @staticmethod
    def _find_local_ip() -> str:
        """通过向网关空连接获取本机局域网 IP（与 FileProvider 同款）。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
            s.close()
            return ip if ip and ip != '127.0.0.1' else '127.0.0.1'
        except Exception:
            return '127.0.0.1'

    # ---- 生命周期 ----

    def start(self) -> bool:
        """绑定本端网状监听端口并开始接受对端直连。返回是否成功。"""
        if self.running:
            return True
        for try_port in range(self.DEFAULT_MESH_PORT, self.DEFAULT_MESH_PORT + self.MESH_PORT_RANGE):
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
            self.mesh_port = try_port
            self.port = try_port
            self.running = True
            threading.Thread(target=self._accept_loop, daemon=True).start()
            self.log_message.emit(f"网状监听端口: {try_port}")
            return True
        self.log_message.emit(f"网状监听启动失败: 端口 {self.DEFAULT_MESH_PORT}-"
                              f"{self.DEFAULT_MESH_PORT + self.MESH_PORT_RANGE - 1} 全被占用")
        return False

    def stop(self):
        """关闭监听、全部直连与重连线程。"""
        self.running = False
        self._reconnect_wait.set()
        with self._lock:
            conns = list(self.conns.values())
            self.conns.clear()
            peers = dict(self.peers)
            self.peers.clear()
            threads = list(self._reconnect_threads.values())
            self._reconnect_threads.clear()
        for info in conns:
            self._close_conn_info(info)
        for t in threads:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        self.server_socket = None
        del peers

    # ---- 对端表维护（引导/JOIN/LEAVE 入口） ----

    def add_peer(self, ep: Endpoint):
        """登记对端（引导清单/JOIN 通告入口）；若未连通则启动/唤醒重连。"""
        if not ep or not ep.end_id or ep.end_id == self.end_id:
            return
        with self._lock:
            self.peers[ep.end_id] = ep
        self._ensure_reconnect(ep.end_id)

    def remove_peer(self, end_id: str):
        """移除对端（LEAVE 通告入口）：拆除直连并停止重连。"""
        with self._lock:
            self.peers.pop(end_id, None)
            info = self.conns.pop(end_id, None)
            thread = self._reconnect_threads.pop(end_id, None)
        if info:
            self._close_conn_info(info)
            self.peer_disconnected.emit(end_id)

    def bootstrap_peers(self, peer_dicts: list):
        """主机引导：批量登记对端清单 [{end_id, name, ip, mesh_port}, ...]。"""
        for d in peer_dicts or []:
            if isinstance(d, dict):
                self.add_peer(Endpoint.from_dict(d))

    # ---- 主动拨号 / 重连 ----

    def _ensure_reconnect(self, end_id: str):
        """确保对端有一个重连守护线程（已在重连则不重复启动）。"""
        with self._lock:
            t = self._reconnect_threads.get(end_id)
            if t and t.is_alive():
                return
            t = threading.Thread(target=self._reconnect_loop, args=(end_id,), daemon=True)
            self._reconnect_threads[end_id] = t
        t.start()

    def _reconnect_loop(self, end_id: str):
        """断线退避重连：未连通则拨号，失败递增退避；对端被移除即退出。"""
        backoff = self.RECONNECT_BASE
        while self.running:
            with self._lock:
                ep = self.peers.get(end_id)
                connected = end_id in self.conns
            if not ep or not ep.is_valid():
                break  # 对端已移除/信息不全，退出
            if connected:
                self._reconnect_wait.wait(1.0)
                continue
            if self._dial(ep):
                backoff = self.RECONNECT_BASE
                self._reconnect_wait.wait(1.0)
            else:
                self._reconnect_wait.wait(backoff)
                backoff = min(backoff * 2, self.RECONNECT_MAX)

    def _dial(self, ep: Endpoint) -> bool:
        """主动拨号对端：TCP 建连（END_INFO 由连接处理线程统一发送）。成功返回 True。"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except Exception:
                pass
            sock.settimeout(5.0)  # 建连超时：本地局域网 5s 足够
            sock.connect((ep.ip, ep.mesh_port))
            sock.settimeout(1.0)
        except OSError:
            try:
                sock.close()
            except Exception:
                pass
            return False
        threading.Thread(target=self._handle_mesh_conn,
                         args=(sock, ep.end_id, 'out'), daemon=True).start()
        return True

    # ---- 监听 / 连接处理 ----

    def _accept_loop(self):
        while self.running:
            try:
                conn_socket, addr = self.server_socket.accept()
            except socket.timeout:
                continue
            except Exception:
                if self.running:
                    continue
                break
            try:
                conn_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                conn_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except Exception:
                pass
            threading.Thread(target=self._handle_mesh_conn,
                             args=(conn_socket, None, 'in'), daemon=True).start()

    def _handle_mesh_conn(self, conn_socket, expected_end_id: Optional[str], role: str):
        """处理一条网状直连：握手（双向交换 END_INFO 识别对端）→ 注册去重 → 消息分发。

        双方在建连后都立即发送自己的 END_INFO，故拨号方与接受方都能在对端
        处理线程中识别彼此；END_INFO 与本连接后续所有发送共用同一把发送锁，
        避免并发写交错。
        """
        send_guard = SendLock()
        receiver = MessageReceiver()
        pending = []
        peer_end_id = None
        peer_ep = None
        conn_socket.settimeout(1.0)

        # ---- 握手阶段：发送自身 END_INFO，等待对端 END_INFO ----
        try:
            send_guard.send(conn_socket, Protocol.create_end_info(
                self.end_id, self.name, self.mesh_port))
            while self.running:
                try:
                    data = conn_socket.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                receiver.feed(data)
                while receiver.has_complete_message():
                    message = receiver.get_message()
                    msg_type, filename, file_size, mtime, hide, content = message
                    if msg_type == MessageType.END_INFO and isinstance(content, dict):
                        peer_end_id = content.get('end_id', '')
                        if not peer_end_id or peer_end_id == self.end_id:
                            break  # 非法身份
                        if expected_end_id and peer_end_id != expected_end_id:
                            break  # 身份与拨号目标不符
                        peer_ep = Endpoint(
                            end_id=peer_end_id,
                            name=content.get('name', ''),
                            ip=conn_socket.getpeername()[0] if role == 'in' else self._sock_peer_ip(conn_socket),
                            mesh_port=int(content.get('mesh_port', 0) or 0),
                        )
                        break
                    pending.append(message)
                if peer_ep is not None:
                    break
        except Exception:
            peer_ep = None

        if not peer_ep:
            self._close_socket(conn_socket)
            return

        # ---- 注册 + 双向建连去重（保连接发起方为 end_id 小者一侧） ----
        if not self._register_peer_conn(peer_ep, conn_socket, role, send_guard):
            return  # 本连接是去重输家，已关闭

        # ---- 主循环：分发握手期积压消息 + 后续消息 ----
        self._dispatch_pending(peer_ep.end_id, pending)
        heartbeat_stop = self._start_heartbeat(conn_socket, peer_ep.end_id)
        try:
            while self.running:
                try:
                    data = conn_socket.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                receiver.feed(data)
                while receiver.has_complete_message():
                    message = receiver.get_message()
                    self._dispatch(peer_ep.end_id, message)
        except Exception:
            pass
        finally:
            heartbeat_stop.set()
            # 若仍是本连接（未被新连接替换），拆除并通知
            with self._lock:
                info = self.conns.get(peer_ep.end_id)
                is_current = info is not None and info.get('socket') is conn_socket
                if is_current:
                    self.conns.pop(peer_ep.end_id, None)
            if is_current:
                self._close_socket(conn_socket)
                self.peer_disconnected.emit(peer_ep.end_id)

    def _sock_peer_ip(self, conn_socket):
        try:
            return conn_socket.getpeername()[0]
        except Exception:
            return ''

    def _register_peer_conn(self, ep: Endpoint, conn_socket, role: str,
                            send_guard: SendLock) -> bool:
        """注册对端连接；双向拨号重复连接时按确定性规则保留唯一连接。

        规则：保留「发起方 end_id 字典序小」的物理连接。
        - 本端 end_id 小 → 保留我方主动拨号的连接（'out'）
        - 本端 end_id 大 → 保留对端拨来的连接（'in'）
        双方对同一条 TCP 连接达成一致，另一条随即关闭，防半开/双连接。

        send_guard 为握手阶段已用于发送 END_INFO 的那把发送锁，注册后继续
        用于本连接全部发送（心跳/业务），保证同一 socket 上所有写串行化。

        Returns:
            True = 本连接被采用（调用方继续其消息循环）；
            False = 本连接是输家，已被关闭（调用方应结束处理）。
        """
        survivor_role = 'out' if self.end_id < ep.end_id else 'in'
        new_info = {
            'socket': conn_socket,
            'receiver': MessageReceiver(),
            'send_guard': send_guard,
            'role': role,
            'name': ep.name,
            'endpoint': ep,
        }
        loser_sock = None
        adopted = False
        with self._lock:
            existing = self.conns.get(ep.end_id)
            if existing is not None and existing.get('socket') is conn_socket:
                return True  # 同一连接重复注册，忽略
            if existing is not None:
                if role != survivor_role:
                    loser_sock = conn_socket          # 本连接是输家：关闭自己
                else:
                    loser_sock = existing['socket']   # 旧连接是输家：关闭旧的
                    self.conns[ep.end_id] = new_info
                    adopted = True
            else:
                self.conns[ep.end_id] = new_info
                adopted = True
            self.peers[ep.end_id] = ep
        if loser_sock is not None:
            self._close_socket(loser_sock)
        if adopted:
            self.peer_connected.emit(ep.end_id, ep.name)
        return adopted

    # ---- 心跳（探测半开连接） ----

    def _start_heartbeat(self, conn_socket, end_id: str) -> threading.Event:
        stop = threading.Event()
        guard = None
        with self._lock:
            info = self.conns.get(end_id)
            if info and info.get('socket') is conn_socket:
                guard = info['send_guard']
        if not guard:
            stop.set()
            return stop

        def _loop():
            while not stop.wait(self.HEARTBEAT_INTERVAL):
                if not self.running:
                    return
                try:
                    guard.send(conn_socket, Protocol.create_heartbeat())
                except Exception:
                    stop.set()
                    return

        threading.Thread(target=_loop, daemon=True).start()
        return stop

    # ---- 发送 ----

    def send_to_peer(self, end_id: str, data: bytes) -> bool:
        """向指定对端发送一条消息（可恢复发送，失败返回 False）。"""
        with self._lock:
            info = self.conns.get(end_id)
        if not info:
            return False
        try:
            return info['send_guard'].send_resumable(info['socket'], data)
        except Exception:
            return False

    def send_to_all(self, data: bytes, except_end_id: Optional[str] = None) -> int:
        """向全部已直连对端广播一条消息，返回发送成功的端数。"""
        with self._lock:
            targets = [(eid, info) for eid, info in self.conns.items() if eid != except_end_id]
        ok = 0
        for eid, info in targets:
            try:
                if info['send_guard'].send_resumable(info['socket'], data):
                    ok += 1
            except Exception:
                pass
        return ok

    # ---- 消息分发 ----

    def _dispatch_pending(self, end_id: str, pending: list):
        for message in pending:
            self._dispatch(end_id, message)

    def _dispatch(self, end_id: str, message):
        msg_type, filename, file_size, mtime, hide, content = message
        if self._on_message is not None:
            try:
                self._on_message(end_id, message)
            except Exception:
                pass
        try:
            self.mesh_message.emit(end_id, msg_type, filename, file_size, mtime, hide, content)
        except Exception:
            pass

    # ---- 工具 ----

    @staticmethod
    def _close_socket(sock):
        try:
            sock.close()
        except Exception:
            pass

    def _close_conn_info(self, info: dict):
        stop = info.get('heartbeat_stop')
        if stop:
            try:
                stop.set()
            except Exception:
                pass
        self._close_socket(info.get('socket'))
