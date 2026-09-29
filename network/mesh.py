"""
去中心化同步：网状直连连接管理
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
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
    """网状连接管理器：本端常驻监听端口，维护地址线索表与当前活跃直连集合。

    peers 仅为「拨号缓存」（知道地址就试连，不代表在线）；对外辐射的清单只反映
    此刻的真实直连（conns），另置顶一条主机条目（is_host）供主机回归统领权限。
    """

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
    MESH_BROADCAST_TOTAL_TIMEOUT = 2.0  # 单端广播发送总时限（秒）：半开对端不拖慢整个 send_to_all
    MAX_SILENT_SECONDS = 8.0    # 对端静默判死阈值（秒）：双方均每 2s 发心跳，
                                # 连续超过该时长收不到对端任何字节（含心跳）即判半开/
                                # 静默死亡，拆除连接并触发退避重连（防断电/拔线/WiFi
                                # 断这类无 FIN/RST 的半开链路永久留在 conns 中）

    def __init__(self, name: str = '', end_id: str = '', parent=None,
                 room_code: str = '', password: str = ''):
        super().__init__(parent)
        self.end_id = end_id or UserConfig.get_end_id()
        self.name = name or socket.gethostname()
        self.running = False
        self.port = None
        self.mesh_port = 0
        self.ip = self._find_local_ip()
        self.server_socket = None
        self.room_code = room_code
        self._mesh_auth = Protocol.mesh_auth(room_code, password)  # 握手准入凭据

        self._lock = threading.Lock()
        self.peers: dict = {}        # end_id -> Endpoint（地址线索/拨号缓存，非在线状态）
        self.conns: dict = {}        # end_id -> {socket, receiver, send_guard, role, name, endpoint, heartbeat_stop}
        self._reconnect_threads: dict = {}   # end_id -> Thread
        self.host_ep: Optional[Endpoint] = None  # 房间主机端点线索（辐射清单置顶条目，带 is_host 标识）
        self.host_id = ''            # 主机权威身份，仅由管理面认证路径写入（wire 声明须与此一致）
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

    def peer_list(self) -> list:
        """地址线索清单（供 UDP/TCP 应答与建连互换，清单即「此刻的直连快照」）。

        置顶一条主机条目（is_host=True，供主机回归统领权限），其后为当前活跃直连
        的端；不含已知但未连通的历史地址（那些留在 peers 拨号缓存里）。旧端忽略
        is_host 未知字段，混版安全。
        """
        out = []
        seen = set()
        with self._lock:
            host = self.host_ep
            if host is not None and host.is_valid() and host.end_id not in seen:
                d = host.to_dict()
                d['is_host'] = True
                out.append(d)
                seen.add(host.end_id)
            for info in self.conns.values():
                ep = info.get('endpoint')
                if ep is None or not ep.is_valid() or ep.end_id in seen:
                    continue
                seen.add(ep.end_id)
                out.append(ep.to_dict())
        return out

    def set_host(self, ep: Optional[Endpoint]):
        """登记/刷新房间主机端点线索（置顶辐射用），同时记下主机权威身份。

        仅由管理面认证路径（自身、主机 END_INFO、HOST_INFO 重定向）调用；wire 上
        他人清单里的 is_host 声明须先与本端已知 host_id 一致才允许刷新（见 merge_peers）。
        """
        if not ep or not ep.end_id:
            return
        with self._lock:
            self.host_ep = ep
            self.host_id = ep.end_id

    def merge_peers(self, peer_dicts: list):
        """合并地址线索（gossip/发现入口）：登记拨号缓存；带 is_host 的条目刷新主机置顶线索。

        is_host 声明须与已认证得到的主机身份一致才采信，防任意端置顶冒充主机。
        """
        for d in peer_dicts or []:
            if not isinstance(d, dict):
                continue
            ep = Endpoint.from_dict(d)
            if not ep.end_id or ep.end_id == self.end_id:
                continue
            if d.get('is_host'):
                with self._lock:
                    known = self.host_id
                if known and ep.end_id == known:
                    self.set_host(ep)
            self.add_peer(ep)

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

    # ---- 地址线索维护（引导/JOIN/GOSSIP/LEAVE 入口） ----

    def add_peer(self, ep: Endpoint):
        """登记地址线索（引导清单/JOIN/gossip 入口）：知道地址即试连，不代表在线。"""
        if not ep or not ep.end_id or ep.end_id == self.end_id:
            return
        with self._lock:
            self.peers[ep.end_id] = ep
        self._ensure_reconnect(ep.end_id)

    def remove_peer(self, end_id: str):
        """移除对端（LEAVE 通告入口）：拆除直连、停止重连并清掉地址线索。

        不立墓碑：mesh 层不维护生命周期裁决，被移除端若再次宣告/拨入即重新登记，
        离房语义由主机成员配置（认证面）把关。
        """
        with self._lock:
            self.peers.pop(end_id, None)
            info = self.conns.pop(end_id, None)
            self._reconnect_threads.pop(end_id, None)
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
        """断线重连：未连通则拨号，失败递增退避；退避到上限仍连不上即停止。

        地址线索留在 peers（不删），待对方主动宣告或被重新介绍时经 add_peer 再试，
        避免跨网段死对端在后台无限重试。
        """
        backoff = self.RECONNECT_BASE
        while self.running:
            with self._lock:
                ep = self.peers.get(end_id)
                connected = end_id in self.conns
            if not ep or not ep.is_valid():
                break  # 地址线索已移除/信息不全，退出
            if connected:
                self._reconnect_wait.wait(1.0)
                continue
            if self._dial(ep):
                backoff = self.RECONNECT_BASE
                self._reconnect_wait.wait(1.0)
                continue
            if backoff >= self.RECONNECT_MAX:
                break  # 退避到上限仍连不上 → 停止重试（等其自报或被重新介绍）
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

        双方在建连后都立即发送自己的 END_INFO（含房间号与准入凭据），故拨号方与
        接受方都能在对端处理线程中识别彼此并校验准入；未通过者静默关闭。END_INFO
        与本连接后续所有发送共用同一把发送锁，避免并发写交错。
        """
        send_guard = SendLock()
        receiver = MessageReceiver()
        pending = []
        peer_end_id = None
        peer_ep = None
        rejected = False
        conn_socket.settimeout(1.0)

        # ---- 握手阶段：发送自身 END_INFO（带准入字段），等待对端 END_INFO ----
        try:
            send_guard.send(conn_socket, Protocol.create_end_info(
                self.end_id, self.name, self.mesh_port,
                room_code=self.room_code, auth=self._mesh_auth))
            last_activity = time.time()
            while self.running:
                try:
                    data = conn_socket.recv(65536)
                except socket.timeout:
                    # 静默超时：收不到对端任何字节（含心跳）→ 判半开/静默死亡
                    if time.time() - last_activity > self.MAX_SILENT_SECONDS:
                        break
                    continue
                except OSError:
                    break
                if not data:
                    break
                last_activity = time.time()
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
                        if not self._verify_peer(content):
                            rejected = True
                            break  # 房间/凭据不符 → 拒绝
                        peer_ep = Endpoint(
                            end_id=peer_end_id,
                            name=content.get('name', ''),
                            ip=conn_socket.getpeername()[0] if role == 'in' else self._sock_peer_ip(conn_socket),
                            mesh_port=int(content.get('mesh_port', 0) or 0),
                            mgmt_port=int(content.get('mgmt_port', 0) or 0),
                        )
                        break
                    pending.append(message)
                if rejected or peer_ep is not None:
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
        last_activity = time.time()
        try:
            while self.running:
                try:
                    data = conn_socket.recv(65536)
                except socket.timeout:
                    # 静默判死：半开/断电链路无 FIN/RST，只能以"多久没收到对端
                    # 字节"判定。上限留足心跳间隔余量，正常相连每秒都能收到对端心跳。
                    if time.time() - last_activity > self.MAX_SILENT_SECONDS:
                        break
                    continue
                except OSError:
                    break
                if not data:
                    break
                last_activity = time.time()
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

    def _verify_peer(self, content: dict) -> bool:
        """网状准入校验：房间号一致 + 准入凭据匹配，任一不符即拒。

        不做旧端放行：版本不同者在认证面就被挡在房间之外，成不了本房间的成员。
        未通过者由调用方静默关闭（端口公开，不刷日志、不发 peer_disconnected）。
        """
        if self.room_code and content.get('room_code') != self.room_code:
            return False
        return (content.get('auth') or '') == self._mesh_auth

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
            self._announce_peers(ep.end_id)
        return adopted

    def _announce_peers(self, end_id: str):
        """向刚建连的对端互换地址线索（此刻直连快照 + 置顶主机条目）：主机不在场也能扩散。"""
        peers = self.peer_list()
        if peers:
            self.send_to_peer(end_id, Protocol.create_mesh_peer_list(peers))

    # ---- 心跳（探测半开连接） ----

    def _start_heartbeat(self, conn_socket, end_id: str) -> threading.Event:
        stop = threading.Event()
        guard = None
        with self._lock:
            info = self.conns.get(end_id)
            if info and info.get('socket') is conn_socket:
                guard = info['send_guard']
                # 写入 conns，使 _close_conn_info 在单条连接拆除（remove_peer/换路）
                # 时能立即停拍心跳线程，而非依赖下一次 guard.send 抛异常自愈（滞后
                # 至多 HEARTBEAT_INTERVAL=2s）。
                info['heartbeat_stop'] = stop
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
        """向全部已直连对端广播一条消息，返回发送成功的端数。

        每端施加独立总时限（MESH_BROADCAST_TOTAL_TIMEOUT）：半开对端（心跳 8s
        判定前的窗口期内发送缓冲写不进）不会把整个广播循环拖住秒级，逐端各让出
        有限预算后继续下一端。
        """
        with self._lock:
            targets = [(eid, info) for eid, info in self.conns.items() if eid != except_end_id]
        ok = 0
        for eid, info in targets:
            try:
                guard = info['send_guard']
                if guard.send_resumable(
                        info['socket'], data,
                        total_timeout=MeshManager.MESH_BROADCAST_TOTAL_TIMEOUT):
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
        if msg_type == MessageType.MESH_PEER_LIST:
            # 地址线索互换：mesh 内部消化（并入拨号缓存 + 刷新主机置顶线索），不上抛业务层
            if isinstance(content, dict):
                self.merge_peers(content.get('peers'))
            return
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
