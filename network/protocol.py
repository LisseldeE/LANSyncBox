"""
同步协议
使用二进制协议进行高效文件传输
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import struct
import os
import json
import time
from typing import Tuple, Optional


class MessageType:
    """消息类型"""
    FILE = 0x01           # 文件传输（已废弃，保留向后兼容）
    DELETE = 0x02         # 删除指令
    AUTH_REQ = 0x03       # 验证请求
    AUTH_RESP = 0x04      # 验证响应
    FILE_LIST_REQ = 0x08  # 请求文件列表
    FILE_LIST_RESP = 0x09 # 响应文件列表（JSON格式）
    FILE_REQUEST = 0x0A   # 请求特定文件
    HEARTBEAT = 0x07      # 心跳
    FILE_BEGIN = 0x0C     # 文件传输开始（流式）
    FILE_DATA = 0x0D      # 文件数据块
    FILE_END = 0x0E       # 文件传输结束
    DIR_CREATE = 0x0B     # 目录创建
    FILE_CANCEL = 0x0F    # 取消文件传输
    FILE_NOTIFY = 0x10    # 文件通知（静默，通知连接端有新文件）
    FILE_REQUEST_FORWARD = 0x11  # 请求转发文件
    SYNC_REQUEST = 0x12   # 手动同步请求（主机→连接端，触发连接端重新上报并进行差异同步）
    SYNC_RESULT = 0x13    # 同步结果（主机→连接端，告知是否有差异，用于显示"一致/补齐"通知）
    PING = 0x14           # 延迟探测（发起端→对端，content 携带发送时刻，对端收到原样回传为 PONG）
    PONG = 0x15           # 延迟回包（对端→发起端，原样带回 PING 的发送时刻，发起端用于计算 RTT）
    CLIPBOARD_DATA = 0x16 # 剪切板文本内容（仅文本走系统剪贴板广播；图片/文件统一走文件 TCP 直连链路；filename 字段承载类型标识 "text"）
    CLIPBOARD_FILES_NOTIFY = 0x17  # 剪切板文件会话通知（复制端→主机→其余端；content=JSON 会话元数据）
    CLIPBOARD_FILE_PULL_REQ = 0x18 # 文件拉取请求（接收端→复制端目录端口；content=JSON {session_id,token,name,offset}）
    P2P_FILE_DATA = 0x19 # 预留常量（投递已改走 FILE_BEGIN/FILE_DATA/FILE_END 端到端 TCP 流式传输，不再使用）
    MODE_SWITCH = 0x1A    # 模式切换指令（主机→连接端；content="sync"/"collect"）
    MODE_ACK = 0x1B       # 模式切换完成回执（连接端→主机；content=切换后的模式）
    MODE_REQ = 0x1C       # 模式请求（连接端认证成功后→主机，请求当前模式）
    MODE_RESP = 0x1D      # 模式响应（主机→连接端；content="sync"/"collect"）
    PERM_UPDATE = 0x1E    # 权限更新（主机→连接端；content="rw"（读写）/"ro"（只读））
    PERM_ACK = 0x1F       # 权限应用回执（连接端→主机；content=应用后的权限）

    # ---- 去中心化架构消息（content=JSON；0x20-0x27 均为网状/直连链路） ----
    DISTRIBUTE_SIGNAL = 0x20  # 分发信号（端→网状各直连对端）：{src_id, op_no, op, file, state, clock, ts, dst?}
    FILE_STATE_REQ = 0x21     # 文件状态请求（端→对端）：{src_id, dirs?（26.9C2 可选，本端目录清单）}
    FILE_STATE_RESP = 0x22    # 文件状态响应（对端→端）：{src_id, entries:[{name, op_no, state, exists, clock, ts}],
                              #   dirs_missing?（26.9C2 可选，"我有你无"的目录差异集）}
    SYNC_PULL_REQ = 0x23      # 同步拉取请求（端→对端 FileProvider 会话）：{session_id, token, name}
    END_INFO = 0x24           # 端身份信息（握手后交换）：{end_id, name, mesh_port, mgmt_port}
    MESH_PEER_LIST = 0x25     # 对端清单（主机→新加入端引导；mesh 建连后亦端间互换实现"互相介绍"自扩散）：
                              #   {peers:[{end_id, name, ip, mesh_port, mgmt_port}]}
    MESH_PEER_JOIN = 0x26     # 新端加入通告（主机→各端）：{peer:{end_id, name, ip, mesh_port, mgmt_port}}
    MESH_PEER_LEAVE = 0x27    # 端离线通告（主机→各端）：{end_id}
    CLIPBOARD_NOTIFY_SIGNAL = 0x28  # 投递通知（端→网状各直连对端；阶段 5 替换主机转发）：content=JSON 会话元数据
    HOST_INFO = 0x29          # 主机信息指示（连接端"复用管理监听"→接入的新端）：{host_id, ip, port}，
                              # 告知真主机地址，令接入端把管理连接换接到真主机（H1：控制面只连主机）
    ROOM_PROBE = 0x2A         # 房间探活请求（探测端→目标端点）：content="{sync_version}:{room_code}"，
                              # 只回状态不注册客户端/不写日志（UDP 摸不到时的 TCP 定向探活）
    ROOM_PROBE_RESP = 0x2B    # 房间探活响应（目标端点→探测端）：content=RoomProbeState 单字符状态码；
                              #   filename=宿主 end_id（探测端据此判定"宿主即本机"）
    CLIPBOARD_TEXT_SIGNAL = 0x2C  # 文本投递（端→网状各直连对端）：与 CLIPBOARD_DATA 同构但走网状直连
                                  # （不经主机转发）；filename=mime_type、content=原始 utf-8 字节，不可加入 JSON 解析集
    ROOM_PEER_QUERY = 0x2D    # 对端查询请求（探测端→房间任一存活端）：content="{sync_version}:{room_code}"，
                              # 免认证、零日志；用于 UDP 摸不到的 TCP 定向取回对端清单
    ROOM_PEER_LIST = 0x2E     # 对端清单（应答方→探测端）：{peers:[{end_id, name, ip, mesh_port, mgmt_port}]}，
                              # 用于 ROOM_PEER_QUERY 应答与 ROOM_PROBE 顺带推送（mesh 建连后的互换走 0x25）

    # ---- 私信（房间内端到端私发；全部走网状直连，不经主机中继） ----
    # 版本兼容：三型均为加法，旧端 MessageReceiver 正常分帧后由 mesh._dispatch 上抛
    # 业务层，无对应分支即静默忽略，不影响既有功能（SYNC_LOGIC_VERSION 不提升）。
    CHAT_TEXT = 0x2F          # 私信文本（端→端）：content=JSON {from_id, msg_id, ts, text}
    CHAT_FILE_OFFER = 0x30    # 私信文件会话通知（端→端）：content=JSON
                              #   {from_id, msg_id, session_id, token, files:[{name,size}], ts, ttl}
                              #   文件字节仍由接收端直连发送端 FileProvider 拉取（复用 0x23 通道）
    CHAT_SESSION_CLOSE = 0x31 # 私信文件会话失效（端→端）：content=JSON {from_id, session_id, reason}


class RoomProbeState:
    """房间探活状态码（ROOM_PROBE_RESP 的 content 取值）

    ONLINE/HOST_UNAVAILABLE 均表示"该房间有存活端点"（前者含真主机可用，后者为
    连接端复用监听且主机暂不可用）；ROOM_MISMATCH/VERSION_MISMATCH 表示该端点
    不是目标房间或版本不兼容，探测方据此回退广播发现。
    """
    ONLINE = '1'
    HOST_UNAVAILABLE = '2'
    ROOM_MISMATCH = '3'
    VERSION_MISMATCH = '4'


class Protocol:
    """自定义文件传输协议"""
    
    # 消息头格式: 类型(4B) + 文件名长度(4B) + 文件大小(8B) + 修改时间(8B) + 隐藏标记(1B) = 25字节
    HEADER_FORMAT = '!I I Q d B'
    HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
    
    @staticmethod
    def pack_message(msg_type: int, filename: str = '', file_size: int = 0,
                     hide_flag: bool = False, content: bytes = b'',
                     mtime: float = 0.0) -> bytes:
        """打包消息"""
        filename_bytes = filename.encode('utf-8')
        hide = 1 if hide_flag else 0
        
        header = struct.pack(
            Protocol.HEADER_FORMAT,
            msg_type,
            len(filename_bytes),
            file_size,
            mtime,
            hide
        )
        
        return header + filename_bytes + content
    
    @staticmethod
    def unpack_header(header_data: bytes) -> Tuple[int, int, int, float, bool]:
        """解包消息头"""
        msg_type, filename_len, file_size, mtime, hide_flag = struct.unpack(
            Protocol.HEADER_FORMAT, header_data[:Protocol.HEADER_SIZE]
        )
        return msg_type, filename_len, file_size, mtime, bool(hide_flag)
    
    @staticmethod
    def create_file_begin_message(filepath: str, base_dir: str) -> Tuple[bytes, int, float, str]:
        """创建大文件传输开始消息"""
        rel_path = os.path.relpath(filepath, base_dir).replace('\\', '/')
        file_size = os.path.getsize(filepath)
        mtime = os.path.getmtime(filepath)
        
        message = Protocol.pack_message(
            MessageType.FILE_BEGIN, rel_path, file_size, False, b'', mtime
        )
        
        return message, file_size, mtime, rel_path
    
    @staticmethod
    def create_file_data_message(rel_path: str, chunk_index: int, chunk_data: bytes) -> bytes:
        """创建文件数据块消息"""
        filename_bytes = rel_path.encode('utf-8')
        chunk_index_bytes = struct.pack('!I', chunk_index)
        
        header = struct.pack(
            Protocol.HEADER_FORMAT,
            MessageType.FILE_DATA,
            len(filename_bytes),
            len(chunk_data) + 4,
            0.0,
            0
        )
        
        return header + filename_bytes + chunk_index_bytes + chunk_data
    
    @staticmethod
    def create_file_end_message(rel_path: str, file_size: int, mtime: float) -> bytes:
        """创建文件传输结束消息"""
        return Protocol.pack_message(
            MessageType.FILE_END, rel_path, file_size, False, b'', mtime
        )
    
    @staticmethod
    def create_delete_message(filepath: str, base_dir: str) -> bytes:
        """创建删除指令消息"""
        rel_path = os.path.relpath(filepath, base_dir).replace('\\', '/')
        return Protocol.pack_message(MessageType.DELETE, rel_path)
    
    @staticmethod
    def create_rename_message(old_path: str, new_path: str, base_dir: str) -> bytes:
        """创建重命名消息"""
        old_rel = os.path.relpath(old_path, base_dir).replace('\\', '/')
        new_rel = os.path.relpath(new_path, base_dir).replace('\\', '/')
        content = f"{old_rel}|{new_rel}".encode('utf-8')
        return Protocol.pack_message(0x05, '', len(content), False, content)  # 0x05 = RENAME
    
    @staticmethod
    def create_auth_request(sync_version: str, room_code: str, password: str = '') -> bytes:
        """创建验证请求（携带同步逻辑版本号：加入房间一致性校验只比此号）"""
        import hashlib
        password_hash = hashlib.sha256(password.encode()).hexdigest() if password else ''
        content = f"{sync_version}:{room_code}:{password_hash}".encode('utf-8')
        return Protocol.pack_message(MessageType.AUTH_REQ, '', len(content), False, content)
    
    @staticmethod
    def create_auth_response(success: bool, message: str = '',
                             transient: bool = False) -> bytes:
        """创建验证响应（0x04）：content="{状态}:{消息}"。

        状态三态：`1`=成功、`2`=暂时不可用（瞬时，客户端不得视为永久拒绝）、
        `0`=永久失败。旧端只判首字符是否 `1`，`2` 落到失败分支，混版安全。
        """
        state = '2' if transient else ('1' if success else '0')
        content = f"{state}:{message}".encode('utf-8')
        return Protocol.pack_message(MessageType.AUTH_RESP, '', len(content), False, content)
    
    @staticmethod
    def create_heartbeat() -> bytes:
        """创建心跳包"""
        return Protocol.pack_message(MessageType.HEARTBEAT)
    
    @staticmethod
    def create_dir_create_message(dirpath: str, base_dir: str) -> bytes:
        """创建目录创建消息"""
        rel_path = os.path.relpath(dirpath, base_dir).replace('\\', '/')
        return Protocol.pack_message(MessageType.DIR_CREATE, rel_path)
    
    @staticmethod
    def create_file_list_request() -> bytes:
        """创建文件列表请求消息"""
        return Protocol.pack_message(MessageType.FILE_LIST_REQ)
    
    @staticmethod
    def create_file_list_response(file_list: list) -> bytes:
        """创建文件列表响应消息
        
        Args:
            file_list: 文件列表，格式为 [{"filename": "test.txt", "size": 1024, "mtime": 1234567890.123}, ...]
        
        Returns:
            消息字节
        """
        content = json.dumps(file_list).encode('utf-8')
        return Protocol.pack_message(MessageType.FILE_LIST_RESP, '', len(content), False, content)
    
    @staticmethod
    def create_file_request(filename: str) -> bytes:
        """创建文件请求消息
        
        Args:
            filename: 请求的文件名（相对路径）
        
        Returns:
            消息字节
        """
        return Protocol.pack_message(MessageType.FILE_REQUEST, filename)
    
    @staticmethod
    def create_file_cancel(filename: str) -> bytes:
        """创建文件取消传输消息
        
        Args:
            filename: 要取消传输的文件名（相对路径）
        
        Returns:
            消息字节
        """
        return Protocol.pack_message(MessageType.FILE_CANCEL, filename)

    @staticmethod
    def create_file_cancel_for_pull(session_id: str, name: str) -> bytes:
        """创建"接收端主动取消拉取"控制帧（独立控制连接，路由信息编码在文件名）。

        数据连接正在被拆除时，其上内联的 FILE_CANCEL 可能随关闭/RST 一起丢弃，
        不足以可靠通知发送端；故接收端取消时另开一条健康短暂连接，发送带
        session_id/name 的取消帧，发送端据此精确中断在途流式发送并上报"已取消"。

        路由信息不放进 content：FILE_CANCEL 是 MessageReceiver 的"无 content"类型
        （has_complete_message/get_message 一律按 content_size=0 解析，content 会被
        忽略并残留为下一条消息的头字节而解析错乱）。故把 (session_id, name) 用
        分隔符 \\x1f 拼接编码到 filename 字段，file_size/content 均留空。

        Args:
            session_id: 取消的拉取会话
            name: 取消的条目名（与 _stream_file 登记的键 (session_id, name) 一致）

        Returns:
            消息字节（filename=session_id + \\x1f + name，无 content）
        """
        filename = f"{session_id}\x1f{name}"
        return Protocol.pack_message(MessageType.FILE_CANCEL, filename)

    @staticmethod
    def create_sync_request() -> bytes:
        """创建手动同步请求消息（主机→连接端）
        
        Returns:
            消息字节
        """
        return Protocol.pack_message(MessageType.SYNC_REQUEST)

    @staticmethod
    def create_sync_result(has_diff: bool) -> bytes:
        """创建同步结果消息（主机→连接端，用于显示"一致/补齐"通知）
        
        Args:
            has_diff: 是否存在差异（True=正在补齐差异项，False=列表一致无需同步）
        
        Returns:
            消息字节
        """
        content = b'1' if has_diff else b'0'
        return Protocol.pack_message(MessageType.SYNC_RESULT, '', len(content), False, content)

    @staticmethod
    def create_ping(send_time: float) -> bytes:
        """创建延迟探测消息 PING（发起端→对端）

        Args:
            send_time: 本地发送时刻（秒，毫秒精度足够），原样回传用于计算 RTT

        Returns:
            消息字节
        """
        content = struct.pack('!d', send_time)
        return Protocol.pack_message(MessageType.PING, '', len(content), False, content)

    @staticmethod
    def create_pong(send_time: float) -> bytes:
        """创建延迟回包消息 PONG（对端→发起端，原样带回 PING 的发送时刻）

        Args:
            send_time: 从收到的 PING 解出的发送时刻

        Returns:
            消息字节
        """
        content = struct.pack('!d', send_time)
        return Protocol.pack_message(MessageType.PONG, '', len(content), False, content)

    @staticmethod
    def create_mode_message(msg_type: int, mode: str) -> bytes:
        """创建模式相关消息（MODE_SWITCH / MODE_ACK / MODE_REQ / MODE_RESP）

        Args:
            msg_type: 模式消息类型常量
            mode: "sync"（同步模式）或 "collect"（收集模式）

        Returns:
            消息字节
        """
        content = mode.encode('utf-8')
        return Protocol.pack_message(msg_type, '', len(content), False, content)

    @staticmethod
    def create_perm_message(msg_type: int, perm: str) -> bytes:
        """创建权限相关消息（PERM_UPDATE / PERM_ACK）

        Args:
            msg_type: 权限消息类型常量
            perm: "rw"（读写）或 "ro"（只读）

        Returns:
            消息字节
        """
        content = perm.encode('utf-8')
        return Protocol.pack_message(msg_type, '', len(content), False, content)

    @staticmethod
    def create_clipboard_message(mime_type: str, data: bytes) -> bytes:
        """创建剪切板内容消息（仅文本，由主机分发给各端；图片/文件走文件 TCP 直连链路）

        Args:
            mime_type: 内容类型标识，约定恒为 "text"
            data: 内容字节（文本为 utf-8 编码）

        Returns:
            消息字节
        """
        return Protocol.pack_message(
            MessageType.CLIPBOARD_DATA,
            filename=mime_type,
            file_size=len(data),
            content=data
        )

    # ---- 分布式文件会话 / TCP 直连 ----

    @staticmethod
    def create_files_notify(notify_dict: dict) -> bytes:
        """创建文件会话通知消息（复制端→主机→其余端）

        Args:
            notify_dict: 会话元数据（含 session_id/token/files/source_ip/source_port 等）

        Returns:
            消息字节
        """
        import json
        content = json.dumps(notify_dict, ensure_ascii=False).encode('utf-8')
        return Protocol.pack_message(
            MessageType.CLIPBOARD_FILES_NOTIFY,
            filename='clipboard_files',
            file_size=len(content),
            content=content
        )

    @staticmethod
    def create_pull_request(session_id: str, token: str, name: str, offset: int = 0) -> bytes:
        """创建文件拉取请求消息（接收端→复制端）

        Args:
            session_id: 会话标识
            token: 会话校验令牌
            name: 会话内的文件条目名
            offset: 断点续传起始字节（接收端已有部分字节，据此从该处续传；0=从头）

        Returns:
            消息字节
        """
        import json
        content = json.dumps({
            'session_id': session_id,
            'token': token,
            'name': name,
            'offset': int(offset or 0),
        }, ensure_ascii=False).encode('utf-8')
        return Protocol.pack_message(
            MessageType.CLIPBOARD_FILE_PULL_REQ,
            filename=name,
            file_size=len(content),
            content=content
        )

    # ---- 去中心化架构消息（0x20-0x27，content=JSON，不动头格式） ----

    @staticmethod
    def _pack_json(msg_type: int, data: dict, filename: str = '') -> bytes:
        """将 dict 打包为 JSON content 消息（统一入口，所有 0x20-0x27 走此方法）。"""
        content = json.dumps(data, ensure_ascii=False).encode('utf-8')
        return Protocol.pack_message(
            msg_type, filename, len(content), False, content
        )

    @staticmethod
    def create_distribute_signal(signal: dict) -> bytes:
        """创建分发信号消息（0x20，端→网状各直连对端）

        signal 结构：{src_id, op_no, op, file, state, clock, ts, dst?}
        op 取值 add/delete/rename/move；rename/move 携带 old/new 字段。
        """
        return Protocol._pack_json(MessageType.DISTRIBUTE_SIGNAL, signal)

    @staticmethod
    def create_file_state_req(src_id: str, dirs: list = None) -> bytes:
        """创建文件状态请求消息（0x21，端→对端）

        dirs（可选，26.9C2）：本端存在的目录相对路径清单，供对端回「我有你无」
        的差异集——空目录不进快照，仅靠 dir_create 信号会因丢失而永久分叉。
        旧端忽略该字段，行为不变。
        """
        data = {'src_id': src_id}
        if dirs is not None:
            data['dirs'] = dirs  # 空清单也须显式带上：区分「本端无目录」与旧端（无此字段）
        return Protocol._pack_json(MessageType.FILE_STATE_REQ, data)

    @staticmethod
    def create_file_state_resp(src_id: str, entries: list, session: dict = None,
                              dirs_missing: list = None) -> bytes:
        """创建文件状态响应消息（0x22，对端→端）

        entries: [{name, op_no, state, exists, clock, ts}]（vv 由 src_id 隐式关联）
        session（可选）: {session_id, token, host, port} 自同步会话——拉取方据此
            直连本端 FileProvider 端到端拉取（阶段 2 自同步链路）。
        dirs_missing（可选，26.9C2）：本端有而请求方清单无的目录名，由请求方本地
            判定落地（删除优先在其本端生效，避免补发信号的新时间戳复活已删目录）。
        """
        data = {'src_id': src_id, 'entries': entries}
        if session:
            data['session'] = session
        if dirs_missing:
            data['dirs_missing'] = dirs_missing
        return Protocol._pack_json(MessageType.FILE_STATE_RESP, data)

    @staticmethod
    def create_sync_pull_req(session_id: str, token: str, name: str, offset: int = 0) -> bytes:
        """创建同步拉取请求消息（0x23，端→对端 FileProvider 会话服务）

        与 CLIPBOARD_FILE_PULL_REQ 同构，复用 FileProvider 会话校验与流式传输通道。
        offset: 断点续传起始字节（0=从头）。
        """
        return Protocol._pack_json(
            MessageType.SYNC_PULL_REQ,
            {'session_id': session_id, 'token': token, 'name': name,
             'offset': int(offset or 0)},
            filename=name,
        )

    @staticmethod
    def mesh_auth(room_code: str, password: str = '') -> str:
        """网状握手准入凭据：sha256(房间号:sha256(密码))，与 AUTH_REQ 同口径。

        无密码房间返回空串（准入退化为仅房间绑定）。静态凭据、明文上链，
        强度与既有 AUTH_REQ 一致（局域网威胁模型不变）。
        """
        if not password:
            return ''
        import hashlib
        pw = hashlib.sha256(password.encode()).hexdigest()
        return hashlib.sha256(f"{room_code}:{pw}".encode()).hexdigest()

    @staticmethod
    def create_end_info(end_id: str, name: str, mesh_port: int,
                        mgmt_port: int = 0, room_code: str = '',
                        auth: str = '') -> bytes:
        """创建端身份信息消息（0x24，握手后交换）

        mgmt_port: 本端管理监听端口（全网状管理平面：各端均监听管理端口，
        供管理连接故障时切换下一端点；旧版本缺省 0 = 不提供）
        room_code/auth: 网状准入字段，仅网状握手携带（管理链路留空），加法字段
        """
        return Protocol._pack_json(
            MessageType.END_INFO,
            {'end_id': end_id, 'name': name, 'mesh_port': mesh_port,
             'mgmt_port': mgmt_port, 'room_code': room_code, 'auth': auth},
            filename=end_id,
        )

    @staticmethod
    def create_host_info(host_id: str, host_ip: str, host_port: int) -> bytes:
        """创建主机信息指示消息（0x29，连接端复用管理监听 → 接入的新端）

        用于 H1"控制面只连主机"：接入端触达的是连接端的复用管理监听，而非真主机；
        已端据此告知真主机地址，令接入端把管理连接换接到真主机。host_id 可能为空
        （身份未识别），ip/port 必填。
        """
        return Protocol._pack_json(
            MessageType.HOST_INFO,
            {'host_id': host_id or '', 'ip': host_ip or '', 'port': int(host_port or 0)},
        )

    @staticmethod
    def create_mesh_peer_list(peers: list) -> bytes:
        """创建对端清单引导消息（0x25，主机→新加入端）

        peers: [{end_id, name, ip, mesh_port, mgmt_port}, ...]（不含接收端自身）
        """
        return Protocol._pack_json(MessageType.MESH_PEER_LIST, {'peers': peers})

    @staticmethod
    def create_mesh_peer_join(peer: dict) -> bytes:
        """创建新端加入通告消息（0x26，主机→各端）

        peer: {end_id, name, ip, mesh_port, mgmt_port}
        """
        return Protocol._pack_json(MessageType.MESH_PEER_JOIN, {'peer': peer})

    @staticmethod
    def create_mesh_peer_leave(end_id: str) -> bytes:
        """创建端离线通告消息（0x27，主机→各端）"""
        return Protocol._pack_json(MessageType.MESH_PEER_LEAVE, {'end_id': end_id})

    @staticmethod
    def create_clipboard_notify_signal(notify_dict: dict) -> bytes:
        """创建投递通知消息（0x28，端→网状各直连对端，阶段 5）

        与 CLIPBOARD_FILES_NOTIFY 同构（content=JSON 会话元数据），但走网状
        直连分发（不经主机转发）；文件字节仍由接收端端到端直连复制端拉取。
        """
        return Protocol._pack_json(MessageType.CLIPBOARD_NOTIFY_SIGNAL, notify_dict)

    @staticmethod
    def create_clipboard_text_signal(mime_type: str, data: bytes) -> bytes:
        """创建文本投递消息（0x2C，端→网状各直连对端）

        与 CLIPBOARD_DATA 同构（filename=mime_type、content=原始 utf-8 字节），
        但走网状直连分发（不经主机转发）；content 为原始字节，不做 JSON 解析。
        """
        return Protocol.pack_message(
            MessageType.CLIPBOARD_TEXT_SIGNAL,
            filename=mime_type,
            file_size=len(data),
            content=data,
        )

    @staticmethod
    def create_room_probe(sync_version: str, room_code: str) -> bytes:
        """创建房间探活请求（0x2A）：content="{sync_version}:{room_code}" """
        content = f"{sync_version}:{room_code}".encode('utf-8')
        return Protocol.pack_message(MessageType.ROOM_PROBE, '', len(content), False, content)

    @staticmethod
    def create_room_probe_resp(state: str, host_id: str = '') -> bytes:
        """创建房间探活响应（0x2B）：content=RoomProbeState 单字符状态码。

        宿主 end_id 走 filename 字段（旧端只读 content，混版安全）：探测端据此在
        连接前判定"宿主即本机"，避免主机以连接端身份连接自己。
        """
        content = str(state).encode('utf-8')
        return Protocol.pack_message(MessageType.ROOM_PROBE_RESP, host_id or '',
                                     len(content), False, content)

    @staticmethod
    def create_room_peer_query(sync_version: str, room_code: str) -> bytes:
        """创建对端查询请求（0x2D）：content="{sync_version}:{room_code}"（原始字节，非 JSON）"""
        content = f"{sync_version}:{room_code}".encode('utf-8')
        return Protocol.pack_message(MessageType.ROOM_PEER_QUERY, '', len(content), False, content)

    @staticmethod
    def create_room_peer_list(peers: list) -> bytes:
        """创建对端清单（0x2E）：content=JSON {"peers":[{end_id,name,ip,mesh_port,mgmt_port}]}"""
        return Protocol._pack_json(MessageType.ROOM_PEER_LIST, {'peers': peers or []})

    # ---- 私信（0x2F-0x31，content=JSON，走网状直连） ----

    @staticmethod
    def create_chat_text(from_id: str, msg_id: str, text: str, ts: float = 0.0) -> bytes:
        """创建私信文本消息（0x2F，端→端 mesh 直连）：content=JSON。

        文本按明文传输（局域网可信网络）；msg_id 供接收端去重与 UI 定位气泡。
        """
        import time as _t
        return Protocol._pack_json(MessageType.CHAT_TEXT, {
            'from_id': from_id, 'msg_id': msg_id, 'ts': float(ts or _t.time()),
            'text': text or '',
        })

    @staticmethod
    def create_chat_file_offer(from_id: str, msg_id: str, session_id: str,
                              token: str, files: list, host: str = '',
                              port: int = 0, ts: float = 0.0,
                              ttl: int = 0) -> bytes:
        """创建私信文件会话通知（0x30，端→端 mesh 直连）：content=JSON。

        files: [{name, size}]；文件字节不进本帧，由接收端点击"接收"后直连发送端
        FileProvider（host/port，0x23 通道）按 session_id/token 拉取。
        ttl 为会话有效期（秒）。
        """
        import time as _t
        return Protocol._pack_json(MessageType.CHAT_FILE_OFFER, {
            'from_id': from_id, 'msg_id': msg_id, 'session_id': session_id,
            'token': token, 'files': files or [], 'host': host or '',
            'port': int(port or 0), 'ts': float(ts or _t.time()),
            'ttl': int(ttl or 0),
        })

    @staticmethod
    def create_chat_session_close(from_id: str, session_id: str, reason: str = 'closed') -> bytes:
        """创建私信文件会话失效通知（0x31，端→端 mesh 直连）：content=JSON。

        发送端退出房间/主动撤销会话时下发，令接收端对应条目灰显"已过期"。
        """
        return Protocol._pack_json(MessageType.CHAT_SESSION_CLOSE, {
            'from_id': from_id, 'session_id': session_id, 'reason': reason or 'closed',
        })

    # 注：P2P_FILE_DATA(0x19) 为预留类型，投递已改走 FILE_BEGIN/FILE_DATA/FILE_END 端到端 TCP 流式传输，不再使用。


class ProtocolError(Exception):
    """协议解析错误：头长度非法 / 缓冲超限 / FILE_DATA 内容不足块索引。
    由接收循环抛出（fail-closed），对端发送垃圾时断连并复位，防止缓冲无界增长。"""


class MessageReceiver:
    """消息接收器 - 处理TCP流式数据的分包"""

    # 缓冲/头长度上限：对端发送无法组成合法头的字节流时，防止 buffer 无界增长（内存泄漏）。
    MAX_BUFFER_SIZE = 8 * 1024 * 1024            # 缓冲总上限（8MB）
    MAX_NAME_LEN = 64 * 1024                     # 文件名/路径长度上限（64KB）
    MAX_CONTENT_SIZE = 8 * 1024 * 1024           # 单条 content 长度上限（8MB，需能装进缓冲）

    # content 为 JSON 字典的消息类型（get_message 时自动 json.loads 为 dict）
    # 仅限去中心化新类型（0x20-0x28）；0x17/0x18 剪贴板会话类保持原始 bytes，
    # 由调用方自行 json.loads（file_provider._process_pull_message 与
    # client.files_notify_received 信号均依赖 bytes 语义）。
    JSON_CONTENT_TYPES = {
        MessageType.DISTRIBUTE_SIGNAL,
        MessageType.FILE_STATE_REQ,
        MessageType.FILE_STATE_RESP,
        MessageType.SYNC_PULL_REQ,
        MessageType.END_INFO,
        MessageType.MESH_PEER_LIST,
        MessageType.MESH_PEER_JOIN,
        MessageType.MESH_PEER_LEAVE,
        MessageType.CLIPBOARD_NOTIFY_SIGNAL,
        MessageType.HOST_INFO,
        MessageType.ROOM_PEER_LIST,
        MessageType.CHAT_TEXT,
        MessageType.CHAT_FILE_OFFER,
        MessageType.CHAT_SESSION_CLOSE,
    }

    def __init__(self):
        self.buffer = b''
    
    def feed(self, data: bytes):
        """添加接收到的数据。缓冲超限（对端灌入无法成帧的垃圾）时抛 ProtocolError，
        由接收循环断连并复位——防止 buffer 无界增长（内存泄漏）。"""
        self.buffer += data
        if len(self.buffer) > MessageReceiver.MAX_BUFFER_SIZE:
            raise ProtocolError(
                f"接收缓冲超过上限 {MessageReceiver.MAX_BUFFER_SIZE}，疑似协议垃圾")

    @staticmethod
    def _no_content_types() -> list:
        """无 content（即使 file_size 参数不为 0）的消息类型集合。"""
        return [MessageType.FILE_BEGIN, MessageType.FILE_END,
                MessageType.FILE_LIST_REQ, MessageType.FILE_REQUEST,
                MessageType.FILE_CANCEL, MessageType.FILE_NOTIFY,
                MessageType.FILE_REQUEST_FORWARD, MessageType.SYNC_REQUEST]

    def has_complete_message(self) -> bool:
        """检查是否有完整的消息。头长度非法（无法组成合法消息）抛 ProtocolError，
        交接收循环 fail-closed 断连，避免无谓地无限等待垃圾字节填满缓冲。"""
        if len(self.buffer) < Protocol.HEADER_SIZE:
            return False

        try:
            msg_type, filename_len, file_size, _, _ = Protocol.unpack_header(self.buffer)

            if msg_type in MessageReceiver._no_content_types():
                content_size = 0
            else:
                content_size = file_size

            # 头长度护防：非法文件名长 / 内容长 / FILE_DATA 块不足，判定为垃圾流。
            # 合法消息受 MAX_CONTENT_SIZE 约束（内容需能装入缓冲），不会误伤。
            if filename_len > MessageReceiver.MAX_NAME_LEN \
                    or content_size > MessageReceiver.MAX_CONTENT_SIZE \
                    or (msg_type == MessageType.FILE_DATA and content_size < 4):
                raise ProtocolError(
                    f"非法消息头: type={msg_type} name_len={filename_len} "
                    f"content_size={content_size}")

            required_size = Protocol.HEADER_SIZE + filename_len + content_size
            return len(self.buffer) >= required_size
        except ProtocolError:
            raise
        except Exception:
            return False

    def get_message(self) -> Optional[Tuple]:
        """获取一条完整消息"""
        if not self.has_complete_message():
            return None

        # 解析头部
        msg_type, filename_len, file_size, mtime, hide_flag = Protocol.unpack_header(self.buffer)

        if msg_type in MessageReceiver._no_content_types():
            content_size = 0
        else:
            content_size = file_size

        # 提取文件名和内容
        filename = self.buffer[Protocol.HEADER_SIZE:Protocol.HEADER_SIZE + filename_len].decode('utf-8')
        content_start = Protocol.HEADER_SIZE + filename_len
        content_end = content_start + content_size
        content = self.buffer[content_start:content_end]

        # 移除已处理的数据
        self.buffer = self.buffer[content_end:]

        # 对于FILE_DATA消息，解析块索引
        if msg_type == MessageType.FILE_DATA:
            chunk_index = struct.unpack('!I', content[:4])[0]
            chunk_data = content[4:]
            return msg_type, filename, file_size, mtime, hide_flag, (chunk_index, chunk_data)

        # 对于FILE_LIST_RESP消息，解析JSON
        if msg_type == MessageType.FILE_LIST_RESP:
            file_list = json.loads(content.decode('utf-8'))
            return msg_type, filename, file_size, mtime, hide_flag, file_list

        # 对于 JSON content 类型（0x20-0x27 及剪贴板会话类），解析为 dict
        if msg_type in MessageReceiver.JSON_CONTENT_TYPES:
            try:
                data = json.loads(content.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                data = {}
            return msg_type, filename, file_size, mtime, hide_flag, data

        return msg_type, filename, file_size, mtime, hide_flag, content
    
    def clear(self):
        """清空缓冲区"""
        self.buffer = b''
