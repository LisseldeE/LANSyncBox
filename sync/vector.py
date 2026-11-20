"""
去中心化同步：文件状态向量与三层冲突解决
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import json
from dataclasses import dataclass, field, asdict
from typing import Optional


# 变更状态
STATE_ADD = 1     # 新增
STATE_CHANGE = 2  # 变更（删除/重命名/移动后，最新变更状态）


@dataclass
class FileState:
    """单文件同步状态（端内文件列表条目）。

    name: 相对路径（'/' 分隔）
    op_no: 文件级操作编号（源端递增，用于分发去重）
    state: STATE_ADD / STATE_CHANGE
    exists: 本端该文件当前是否存在
    clock: 逻辑钟，本端每修改一次该文件 +1
    ts: 时间戳（mtime，兜底比较）
    vv: 版本向量 {src_id: 已应用的最大 op_no}（每文件）
    """
    name: str
    op_no: int = 0
    state: int = STATE_ADD
    exists: bool = False
    clock: int = 0
    ts: float = 0.0
    vv: dict = field(default_factory=dict)
    # 文件身份：路径从无到有时生成，内容更新/重命名保持；删除后重建换新。
    file_id: str = ''
    # 内容罗盘：MD5 hex + 文件大小；hash_state = pending / ok / none（目录）。
    hash: str = ''
    hash_state: str = 'none'
    size: int = 0
    # 目录条目（无身份，file_id 为空、hash_state='none'）。
    is_dir: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d['vv'] = dict(self.vv)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FileState":
        return cls(
            name=d.get('name', ''),
            op_no=int(d.get('op_no', 0) or 0),
            state=int(d.get('state', STATE_ADD) or STATE_ADD),
            exists=bool(d.get('exists', False)),
            clock=int(d.get('clock', 0) or 0),
            ts=float(d.get('ts', 0.0) or 0.0),
            vv=dict(d.get('vv', {}) or {}),
            file_id=d.get('file_id', '') or '',
            hash=d.get('hash', '') or '',
            hash_state=d.get('hash_state', 'none') or 'none',
            size=int(d.get('size', 0) or 0),
            is_dir=bool(d.get('is_dir', False)),
        )

    def to_wire_dict(self) -> dict:
        """对端间传输用结构：只承载活对象，带身份、目录标记、哈希与完整 vv。

        删除信息不经此通道（由 TOMBSTONE_RESP 承载）；exists 恒为 True，不再传输。
        """
        return {
            'name': self.name,
            'op_no': self.op_no,
            'state': self.state,
            'clock': self.clock,
            'ts': self.ts,
            'id': self.file_id,
            'is_dir': bool(self.is_dir),
            'hash': self.hash,
            'hash_state': self.hash_state,
            'size': int(self.size or 0),
            'vv': dict(self.vv),
        }

    @classmethod
    def from_wire_dict(cls, src_id: str, d: dict) -> "FileState":
        """由传输结构 + src_id 重建；vv 优先用完整字典，缺省回退单条目。"""
        st = cls.from_dict(d)
        st.exists = True
        st.file_id = d.get('id', '') or d.get('file_id', '') or ''
        raw_vv = d.get('vv')
        if isinstance(raw_vv, dict) and raw_vv:
            st.vv = {str(k): int(v or 0) for k, v in raw_vv.items()}
        else:
            st.vv = {src_id: st.op_no}
        return st


@dataclass
class Endpoint:
    """网状对端信息（对端表中的条目）。"""
    end_id: str
    name: str = ''
    ip: str = ''
    mesh_port: int = 0
    mgmt_port: int = 0    # 管理监听端口（供管理面故障切换；非建连必需）
    conn: object = None  # 网状直连连接对象（所有对端均为直连，无中心连接）

    def to_dict(self) -> dict:
        return {
            'end_id': self.end_id,
            'name': self.name,
            'ip': self.ip,
            'mesh_port': self.mesh_port,
            'mgmt_port': self.mgmt_port,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Endpoint":
        return cls(
            end_id=d.get('end_id', ''),
            name=d.get('name', ''),
            ip=d.get('ip', ''),
            mesh_port=int(d.get('mesh_port', 0) or 0),
            mgmt_port=int(d.get('mgmt_port', 0) or 0),
        )

    def is_valid(self) -> bool:
        """端信息完整可用（具备建连要素）"""
        return bool(self.end_id and self.ip and self.mesh_port > 0)


# ---- 版本向量 ----

def vv_covers(a: dict, b: dict) -> bool:
    """版本向量 a 是否覆盖 b：对 b 中每个源端，a 已应用的 op_no >= b。

    空向量覆盖空向量返回 True（视为已收敛）。
    """
    for src_id, op_no in b.items():
        if a.get(src_id, 0) < op_no:
            return False
    return True


def merge_vv(a: dict, b: dict) -> dict:
    """合并版本向量（逐源端取最大值），返回新字典不改动入参。"""
    merged = dict(a)
    for src_id, op_no in b.items():
        if op_no > merged.get(src_id, 0):
            merged[src_id] = op_no
    return merged


# ---- 三层冲突比较 ----

def compare_states(a: FileState, b: FileState,
                   a_end_id: str = '', b_end_id: str = '') -> int:
    """三层冲突比较：判定应应用哪一方的状态。

    Args:
        a / b: 待比较的两端同文件状态
        a_end_id / b_end_id: 各自所属端标识（第三层平局兜底，字典序小者胜）

    Returns:
        1  = a 胜（应应用 a，b 被覆盖）
        -1 = b 胜（应应用 b，a 被覆盖）
        0  = 相等/已收敛（无需动作）
    """
    # 第一层：版本向量（完全覆盖判定）
    a_covers_b = vv_covers(a.vv, b.vv)
    b_covers_a = vv_covers(b.vv, a.vv)
    if a_covers_b and not b_covers_a:
        return 1
    if b_covers_a and not a_covers_b:
        return -1
    if a_covers_b and b_covers_a:
        return 0  # 双向覆盖：已收敛，无需动作

    # 第二层：逻辑钟（并发修改时修改次数多者胜）
    if a.clock != b.clock:
        return 1 if a.clock > b.clock else -1

    # 第三层：时间戳（mtime 晚者胜）
    if a.ts != b.ts:
        return 1 if a.ts > b.ts else -1

    # 兜底：end_id 字典序（确定性收敛，防死循环）
    if a_end_id == b_end_id:
        return 0
    return 1 if a_end_id > b_end_id else -1


# ---- 序列化 ----

def file_state_store_to_json(states: dict) -> str:
    """FileStateStore 持久化：{name: FileState.to_dict()} → JSON 字符串"""
    return json.dumps(
        {name: st.to_dict() for name, st in states.items()},
        ensure_ascii=False,
    )


def file_state_store_from_json(text: str) -> dict:
    """JSON 字符串 → {name: FileState}"""
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {name: FileState.from_dict(d) for name, d in raw.items() if isinstance(d, dict)}
