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
    # 字节版本向量：真实落盘字节对应的版本。仅本地 emit / 自同步拉取落盘时推进，
    # 回执 merge（pulled / 知识合并）不动它。用于修正「知识已覆盖但字节被旧槽
    # 回退覆盖」的三端并发竞态：_do_pull 落盘前对照裁决，对账时作为
    # 「知识胜于字节 → 强制补拉」兜底的判定依据。旧端（无字段）置空、兜底不生效。
    bytes_vv: dict = field(default_factory=dict)
    # 字节指纹：磁盘真实字节对应的 (clock, ts)（落盘内容的版本指纹）。
    # 本地 emit / 自同步拉取落盘时设置；rename/move 延续旧名；delete 清零。
    # 用于对账补拉兜底：文件存在且字节指纹 == 状态表胜者指纹 → 本端字节已
    # 是仲裁胜者内容，无需再向任何对端补拉（各端 emit_pulled 自增 op_no 导致
    # 对端快照版本号其他端永远「没见过」，bytes_vv 覆盖判定会误判缺口而
    # 交叉反复重拉同一内容——指纹一致即证明内容已对）。旧端（无字段）默认 0，
    # 一次补拉后收敛。
    bytes_clock: int = 0
    bytes_ts: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d['vv'] = dict(self.vv)
        d['bytes_vv'] = dict(self.bytes_vv)
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
            bytes_vv=dict(d.get('bytes_vv', {}) or {}),
            bytes_clock=int(d.get('bytes_clock', 0) or 0),
            bytes_ts=float(d.get('bytes_ts', 0.0) or 0.0),
        )

    def to_wire_dict(self) -> dict:
        """对端间传输用精简结构（vv 由对端 src_id 隐式关联，不随条目传输；
        bytes_vv 供新端参考，旧端无字段 → 对端置空、兜底降级）。

        bytes_clock/bytes_ts 为磁盘真实字节指纹（落盘内容版本）：供拉取端在
        落盘后判别「实际拉到的内容是否即仲裁胜者」——知识推进但磁盘字节未
        到位时指纹失配，据此不推进 bytes_vv 并交由对账补拉可信源收敛。"""
        return {
            'name': self.name,
            'op_no': self.op_no,
            'state': self.state,
            'exists': self.exists,
            'clock': self.clock,
            'ts': self.ts,
            'bytes_vv': dict(self.bytes_vv),
            'bytes_clock': self.bytes_clock,
            'bytes_ts': self.bytes_ts,
        }

    @classmethod
    def from_wire_dict(cls, src_id: str, d: dict) -> "FileState":
        """由精简结构 + src_id 重建（vv 单条目 = {src_id: op_no}）。"""
        st = cls.from_dict(d)
        st.vv = {src_id: st.op_no}
        bv = d.get('bytes_vv')
        if isinstance(bv, dict):
            st.bytes_vv = {str(k): int(v or 0) for k, v in bv.items()}
        return st


@dataclass
class Endpoint:
    """网状对端信息（对端表中的条目）。"""
    end_id: str
    name: str = ''
    ip: str = ''
    mesh_port: int = 0
    conn: object = None  # 网状直连连接对象（所有对端均为直连，无中心连接）

    def to_dict(self) -> dict:
        return {
            'end_id': self.end_id,
            'name': self.name,
            'ip': self.ip,
            'mesh_port': self.mesh_port,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Endpoint":
        return cls(
            end_id=d.get('end_id', ''),
            name=d.get('name', ''),
            ip=d.get('ip', ''),
            mesh_port=int(d.get('mesh_port', 0) or 0),
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
