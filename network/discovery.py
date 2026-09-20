"""
房间发现模块
使用UDP广播发现局域网内的房间
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import socket
import threading
import json
import time
from typing import Dict, Optional, List
from PySide6.QtCore import QObject, Signal

from config import Config, UserConfig


class RoomDiscovery(QObject):
    """房间发现服务（客户端运行，发现房间）"""

    # 信号
    # version = 展示用应用版本（APP_VERSION，不参与校验）
    # sync_version = 同步逻辑版本号（加入房间只校验此号一致）
    room_found = Signal(str, str, int, str, str)  # (ip, room_code, port, version, sync_version)
    discovery_finished = Signal(list)  # 发现完成 [(ip, room_code, port, version, sync_version), ...]
    error_occurred = Signal(str)  # 错误消息

    # UDP端口范围
    DISCOVERY_PORT_START = 9528  # 发现端口起始
    DISCOVERY_PORT_END = 9537    # 发现端口结束（包含）
    DISCOVERY_TIMEOUT = 3  # 发现超时（秒）
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.discovered_rooms: Dict[tuple, dict] = {}  # {(ip, port): {ip, room_code, port, version, sync_version, host_id, timestamp}}
        self._lock = threading.Lock()
        self._timer = None  # 超时定时器（stop_discovery 时需 cancel，避免取消探测后仍触发 _finish_discovery）
        self._receive_thread = None  # 接收线程（stop_discovery 时须回收，避免线程堆积）
    
    def discover_room(self, room_code: str, timeout: int = None) -> bool:
        """
        发现指定房间
        Args:
            room_code: 房间号（空字符串表示扫描所有房间）
            timeout: 超时时间（秒）
        Returns:
            是否启动发现成功
        """
        try:
            timeout = timeout or self.DISCOVERY_TIMEOUT
            self.discovered_rooms.clear()

            # 创建UDP socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(('0.0.0.0', 0))  # 使用随机端口
            self.socket.settimeout(0.5)

            self.running = True

            # 启动接收线程
            self._receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self._receive_thread.start()

            # 发送发现请求（room_code 为空表示扫描所有房间）
            discovery_msg = json.dumps({
                'type': 'discovery_request',
                'room_code': room_code or ''  # 空字符串表示扫描所有
            }).encode('utf-8')

            # 向所有发现端口发送请求
            # 枚举本机全部 IPv4 接口：Windows 的 255.255.255.255 常只从默认路由
            # 网卡出去，桥接/多网卡段可能漏发；对每个接口再发一次"子网定向广播
            # x.x.x.255 + 该接口IP单播"，保证共享任一广播域的端（如主机桥接的
            # VM、或双网卡中的 172 段）都能收到发现请求。
            local_ips = self._get_all_local_v4()
            for port in range(self.DISCOVERY_PORT_START, self.DISCOVERY_PORT_END + 1):
                # 发送到广播地址
                self.socket.sendto(discovery_msg, ('<broadcast>', port))

                # 发送到本机地址（支持同一台机器双开）
                self.socket.sendto(discovery_msg, ('127.0.0.1', port))

                # 逐接口发送：子网定向广播 + 该接口自身 IP（单播到本机口）
                for ip in local_ips:
                    try:
                        self.socket.sendto(discovery_msg, (ip, port))
                        parts = ip.split('.')
                        if len(parts) == 4:
                            bcast = "%s.%s.%s.255" % (parts[0], parts[1], parts[2])
                            self.socket.sendto(discovery_msg, (bcast, port))
                    except Exception:
                        continue

            # 设置超时结束
            self._timer = threading.Timer(timeout, self._finish_discovery)
            self._timer.start()

            return True

        except Exception as e:
            self.error_occurred.emit(f"启动发现失败: {e}")
            return False

    def discover_all_rooms(self, timeout: int = None) -> bool:
        """
        扫描局域网内所有房间
        Args:
            timeout: 超时时间（秒）
        Returns:
            是否启动扫描成功
        """
        return self.discover_room('', timeout)  # 空房间号表示扫描所有

    def discover_room_at(self, ip: str, room_code: str, timeout: int = None) -> bool:
        """
        针对指定 IP 定向探测该 room_code 是否存在
        Args:
            ip: 目标主机 IP
            room_code: 房间号（空字符串表示探测该 IP 是否有任何房间）
            timeout: 超时时间（秒）
        Returns:
            是否启动探测成功
        """
        try:
            timeout = timeout or self.DISCOVERY_TIMEOUT
            self.discovered_rooms.clear()

            # 创建UDP socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(('0.0.0.0', 0))  # 使用随机端口
            self.socket.settimeout(0.5)

            self.running = True

            # 启动接收线程
            self._receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self._receive_thread.start()

            # 发送定向发现请求至该 IP 的所有发现端口
            discovery_msg = json.dumps({
                'type': 'discovery_request',
                'room_code': room_code or ''  # 空字符串表示探测该 IP 上所有房间
            }).encode('utf-8')

            for port in range(self.DISCOVERY_PORT_START, self.DISCOVERY_PORT_END + 1):
                try:
                    self.socket.sendto(discovery_msg, (ip, port))
                except Exception:
                    continue

            # 设置超时结束
            self._timer = threading.Timer(timeout, self._finish_discovery)
            self._timer.start()

            return True

        except Exception as e:
            self.error_occurred.emit(f"启动定向探测失败: {e}")
            return False

    def _get_local_ip(self) -> str:
        """获取本机IP地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _get_all_local_v4(self) -> set:
        """枚举本机全部非回环 IPv4 地址（多网卡/桥接场景逐接口广播用）。

        stdlib 无平台无关的 net_if_addrs，这里用 getaddrinfo(gethostname) 尽量收全
        （Windows 上通常返回每个网卡接口的地址），再补 `_get_local_ip` 兜底默认口。
        失败时退化为空集——调用方仍会发 255.255.255.255 + 127.0.0.1，不受影响。
        """
        addrs: set = set()
        try:
            for info in socket.getaddrinfo(
                    socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM):
                ip = info[4][0]
                if ip and ip != '127.0.0.1':
                    addrs.add(ip)
        except Exception:
            pass
        lip = self._get_local_ip()
        if lip and lip != '127.0.0.1':
            addrs.add(lip)
        return addrs
    
    def stop_discovery(self):
        """停止发现

        取消超时定时器并回收接收线程——仅在关 socket 无法阻止：
        Timer 到点仍会触发 _finish_discovery、接收线程短暂存活，
        快速连续取消/探测时会残留堆积导致卡死。故此处一并回收。
        """
        self.running = False

        # 取消超时定时器，避免取消探测后到点仍触发 _finish_discovery
        timer = self._timer
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
            self._timer = None

        # 关闭 socket，解除阻塞在 recvfrom 的接收线程
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

        # 回收接收线程（socket 已关闭，recvfrom 会立即返回异常退出循环）
        thread = self._receive_thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=0.2)
            except Exception:
                pass
            self._receive_thread = None
    
    def _receive_loop(self):
        """接收响应循环"""
        while self.running:
            try:
                data, addr = self.socket.recvfrom(1024)
                response = json.loads(data.decode('utf-8'))
                
                if response.get('type') == 'discovery_response':
                    host_ip = addr[0]
                    room_code = response.get('room_code')
                    port = response.get('port', Config.DEFAULT_PORT)
                    version = response.get('version', '')
                    sync_version = response.get('sync_version', '')
                    
                    # 以 (ip, port) 为键：同一台机可能有多个存活应答（主机 mgmt + 本机
                    # 连接端 reuse 管理监听等），单以 host_ip 为键会导致尾答覆盖前答，
                    # 使聚合少算成员、host_id 取错。每个应答端点都保留一条。
                    with self._lock:
                        self.discovered_rooms[(host_ip, port)] = {
                            'ip': host_ip,
                            'room_code': room_code,
                            'port': port,
                            'version': version,
                            'sync_version': sync_version,
                            'host_id': response.get('host_id', ''),
                            'timestamp': time.time()
                        }
                    
                    # 安全发射信号
                    try:
                        self.room_found.emit(host_ip, room_code, port, version, sync_version)
                    except RuntimeError:
                        # 对象已被删除，停止循环
                        break
                    
            except socket.timeout:
                continue
            except Exception:
                continue
    
    def _finish_discovery(self):
        """完成发现"""
        self.stop_discovery()
        
        with self._lock:
            rooms = [
                {
                    'ip': info['ip'],
                    'room_code': info['room_code'],
                    'port': info['port'],
                    'version': info.get('version', ''),
                    'sync_version': info.get('sync_version', ''),
                    'host_id': info.get('host_id', '')
                }
                for info in self.discovered_rooms.values()
            ]
        
        # 安全发射信号，避免对象已删除的错误
        try:
            self.discovery_finished.emit(rooms)
        except RuntimeError:
            # 对象已被删除，忽略
            pass

    def get_host_id(self, room_code: str) -> str:
        """返回最近一次发现中指定房间所在主机的持久 end_id（用于"创建被占且宿主即本机"判断）

        同一房间会有多个应答端点（主机 mgmt + 各已连接成员）。真宿主 end_id 由宿主
        连同所有已连接成员一致上报（连接端经 host_id_provider 注入 client._host_id）；
        个别成员自报自身 id / 空值只是离群单条。故取「出现频率最高的非空 host_id」，
        避免第一个应答来自离群成员时误判宿主非本机（多方连带导致"回归"按钮置灰）。
        """
        counts: dict = {}
        with self._lock:
            for info in self.discovered_rooms.values():
                if info.get('room_code') != room_code:
                    continue
                hid = (info.get('host_id') or '').strip()
                if not hid:
                    continue
                counts[hid] = counts.get(hid, 0) + 1
        if not counts:
            return ""
        return max(counts, key=counts.get)

    def get_discovered_rooms(self) -> List[dict]:
        """获取已发现的房间列表（按应答 IP 逐条）"""
        with self._lock:
            return [
                {
                    'ip': info['ip'],
                    'room_code': info['room_code'],
                    'port': info['port'],
                    'version': info.get('version', ''),
                    'sync_version': info.get('sync_version', '')
                }
                for info in self.discovered_rooms.values()
            ]

    def get_room_aggregates(self) -> List[dict]:
        """获取按房间号聚合的房间列表（改版后的记录方式）

        以 room_code 为键聚合：每个响应的存活端只携带自身端点信息，作为该房间的一名成员。
        仅统计 sync_version 与 Config.SYNC_LOGIC_VERSION 一致的兼容成员；整房无兼容成员
        则视为不可加入（joinable=False）。没有『第一个响应端』的特殊角色——所有应答者对称。

        Returns:
            [
                {
                    'room_code': str,
                    'members': [{'ip': str, 'port': int, 'sync_version': str}, ...],  # 兼容成员，按首答序去重
                    'count': int,       # 兼容成员去重数（在线 N）
                    'joinable': bool,   # count > 0
                },
                ...
            ]
            按 count 降序（房间在线端越多越靠前），同数按 room_code 升序。
        """
        compatible = Config.SYNC_LOGIC_VERSION
        groups: Dict[str, dict] = {}
        with self._lock:
            for info in self.discovered_rooms.values():
                # 兼容性过滤：同步逻辑版本不一致的成员不计入 N 且不可作为加入目标
                if info.get('sync_version') != compatible:
                    continue
                code = info.get('room_code')
                if not code:
                    continue
                group = groups.setdefault(code, {'room_code': code, 'members': []})
                group['members'].append({
                    'ip': info.get('ip'),
                    'port': info.get('port', Config.DEFAULT_PORT),
                    'sync_version': info.get('sync_version', ''),
                })

        # 同一物理机去重：主机(mgmt) 与同机连接端(reuse 管理监听) 的 UDP 应答会同 IP
        # 命中多端口，若逐 (ip,port) 各计一条会让"在线 N"虚高。按 ip 聚合成 1 名成员
        #（取首答）；ip 缺失视为不可去重，保留原条目（不影响 join 决策）。
        result = []
        for code, group in groups.items():
            seen_ip = set()
            members = []
            for m in group['members']:
                ip = m.get('ip')
                if ip is None:
                    members.append(m)  # 无 ip 无法去重，整条保留（理论不含）
                    continue
                if ip in seen_ip:
                    continue
                seen_ip.add(ip)
                members.append(m)
            result.append({
                'room_code': code,
                'members': members,
                'count': len(members),
                'joinable': len(members) > 0,
            })
        result.sort(key=lambda r: (-r['count'], r['room_code']))
        return result


class RoomResponder(QObject):
    """房间响应服务（主机端运行，响应发现请求）"""

    # 信号
    error_occurred = Signal(str)

    DISCOVERY_PORT_START = 9528  # 发现端口起始
    DISCOVERY_PORT_END = 9537    # 发现端口结束（包含）

    def __init__(self, parent=None, host_id: str = None, host_id_provider=None):
        super().__init__(parent)
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.room_code = ""
        self.port = Config.DEFAULT_PORT
        self.discovery_port = None  # 实际使用的发现端口
        # 本房间主机的持久 end_id：主机端即本机 → 取本端标识；连接端则由使用方传入
        # 真主机 end_id（client._host_id），让"创建被占且宿主即本机"的判断在连接端
        # 回退/主机离线静默待机期间仍能识别出自己曾是宿主，从而在创建里变"回归为主机"。
        self.host_id = host_id or UserConfig.get_end_id()
        # host_id_provider：连接端传入可调用对象，每次应答时现行取真主机 end_id
        # （END_INFO 在 join 之后才到达，host_id 需延迟解析）；None 则用固定 self.host_id。
        self._host_id_provider = host_id_provider

    def start(self, room_code: str, port: int = None) -> bool:
        """
        启动响应服务
        Args:
            room_code: 房间号
            port: 同步端口
        Returns:
            是否启动成功
        """
        self.room_code = room_code
        self.port = port or Config.DEFAULT_PORT

        # 尝试多个发现端口（9528-9537）
        for try_port in range(self.DISCOVERY_PORT_START, self.DISCOVERY_PORT_END + 1):
            try:
                # 创建UDP socket
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.socket.bind(('0.0.0.0', try_port))
                self.socket.settimeout(1.0)

                self.running = True
                self.discovery_port = try_port  # 记录实际使用的发现端口

                # 启动响应线程
                response_thread = threading.Thread(target=self._response_loop, daemon=True)
                response_thread.start()

                return True

            except OSError as e:
                # 端口被占用，尝试下一个端口
                if self.socket:
                    try:
                        self.socket.close()
                    except Exception:
                        pass
                self.socket = None
                continue
            except Exception as e:
                self.error_occurred.emit(f"启动响应服务失败: {e}")
                return False

        # 所有发现端口都尝试失败
        self.error_occurred.emit(f"启动响应服务失败: 发现端口 {self.DISCOVERY_PORT_START}-{self.DISCOVERY_PORT_END} 均被占用")
        return False
    
    def stop(self):
        """停止响应服务"""
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None
    
    def _response_loop(self):
        """响应发现请求循环"""
        while self.running:
            try:
                data, addr = self.socket.recvfrom(1024)
                request = json.loads(data.decode('utf-8'))

                if request.get('type') == 'discovery_request':
                    # 检查是否匹配房间号
                    # target_room 为空表示扫描所有房间，需要响应
                    target_room = request.get('room_code', '')
                    if target_room and target_room != self.room_code:
                        # 指定了房间号但不匹配，跳过
                        continue

                    # target_room 为空或匹配时，发送响应（携带版本号供客户端核对：
                    # version=展示用应用版本；sync_version=同步逻辑版本号，加入房间
                    # 只校验后者一致，UI 等非同步变更不要求全员升级）
                    host_id = self._host_id_provider() if self._host_id_provider else self.host_id
                    response = json.dumps({
                        'type': 'discovery_response',
                        'room_code': self.room_code,
                        'port': self.port,
                        'version': Config.APP_VERSION,
                        'sync_version': Config.SYNC_LOGIC_VERSION,
                        'host_id': host_id
                    }).encode('utf-8')

                    self.socket.sendto(response, addr)

            except socket.timeout:
                continue
            except Exception:
                continue
