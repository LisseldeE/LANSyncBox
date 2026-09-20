"""
去中心化同步：自同步链路（端到端对比拉取，阶段 2）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.

职责（对应实施计划阶段 2）：
- 端内文件状态列表维护（名称/op_no/state/exists）——单一数据源为
  Distributor 的状态表（阶段 1 交付），本模块在其上做对比与收敛
- 收到 FILE_STATE_REQ → 回 FILE_STATE_RESP（entries + 自同步会话 session，
  拉取方据此直连本端 FileProvider 端到端拉取）
- 收到 FILE_STATE_RESP → 对比差异：
  - 本端缺失对端有的文件 → 加入拉取队列（state=ADD），版本感知槽复用
    （每文件只保留当前仲裁胜者版本，新版本就地覆盖旧槽），
    复用 FileProvider 会话 + pull_file 端到端拉取
  - 对端条目 exists=False 而本端存在 → 补收删除（走分发链路删除应用，
    信号排除上游向下传递）
  - 本地与对端均为 CHANGE 且 exists=False（已删）→ 删除条目，防列表无限变长
- 冲突收敛（阶段 3）：RESP 对比本端已有但三层裁决远端胜出 → 入拉取队列覆盖；
  分发链路冲突覆盖回调 request_conflict_pull → 向胜方请求状态取会话 → 拉取胜方字节
- 手动同步 / 断线重连自动补齐：向各对端请求 FILE_STATE_REQ 并对比补齐
  （替代 SYNC_REQUEST/SYNC_RESULT 主机差异同步）

传输约定与全库一致：线程 + socket 1s 超时 + SendLock.send_resumable 背压退避。
"""
import os
import threading
import time
import uuid
from typing import Optional

from PySide6.QtCore import QObject, Signal

from network.protocol import Protocol, MessageType
from network.file_provider import pull_file
from sync.vector import (FileState, STATE_ADD, STATE_CHANGE, compare_states,
                         vv_covers)


class FileStateStore(QObject):
    """自同步链路管理器：端内文件状态对比、拉取队列与删除收敛（每端一个）。"""

    log_message = Signal(str)
    # 一轮对比结束：True=有差异（正在补齐/拉取），False=列表一致
    sync_done = Signal(bool)
    # 自同步拉取完成落盘（相对路径；UI 刷新文件列表）
    file_added = Signal(str)
    # 自同步拉取进度（相对路径, 已收字节, 总字节）：接收端绿色进度条驱动。
    # 注意类型须与 server/client 转发信号一致（PySide6 信号对信号要求签名完全匹配）。
    pull_progress = Signal(str, 'qlonglong', 'qlonglong')
    # 自同步拉取结束（相对路径, 成功?）：接收端进度条收尾（转完成/失败记录）
    pull_done = Signal(str, bool)

    ROUND_TIMEOUT = 10.0   # 一轮对比超时兜底（对端不应答时也结束本轮，秒）
    RECONCILE_INTERVAL = 20.0  # 周期对账间隔（秒）：对在线对端周期 request_all，
                               # 作为分发信号 fire-and-forget 丢失/多端竞态时的收敛背stop
                               # （LAN 全量对比毫秒级开销；20s 把竞态最坏收敛延迟压到 ~20s）
    PENDING_CONFLICT_TTL = 30.0  # 冲突覆盖待命条目 TTL（秒）：对端不可达未应答时
                                 # 周期清理防残留增长
    PULL_OVERALL_TIMEOUT = 600.0  # 自同步拉取数据阶段整体超时（秒）：对端半开
                                  # （断电/拔线）时 recv 永不返回，单 worker 会被
                                  # 永久阻塞致后续拉取停摆；超时中止并继续处理后续槽
    PULL_CONCURRENCY = 5   # 自同步拉取并发 worker 数：一轮对比入队的多个差异
                           # 文件并发拉取（设计：发送/接收端均支持 5 并发）；
                           # 发送端 FileProvider 每条连接独立线程，天然并行服务

    def __init__(self, end_id: str, sync_folder: str, mesh=None, distributor=None,
                 provider=None, parent=None):
        super().__init__(parent)
        self.end_id = end_id
        self.sync_folder = os.path.abspath(sync_folder)
        self.mesh = mesh            # MeshManager
        self.distributor = distributor  # Distributor（状态表单一数据源）
        self._provider = provider    # 本端 FileProvider（会话服务；None 时可后注入）
        # 阶段 4：只读端不发起自同步推送（回空 entries，不供他端拉取），仅单向下拉
        self._readonly = False

        self._lock = threading.Lock()
        self._sync_tokens = {}   # src_id -> token（自同步会话令牌，每请求方固定）
        # 待拉取队列：name -> {name, end_id, op_no, clock, ts, session}
        # 每文件仅保留一个槽，槽内存当前仲裁胜者版本（版本化去重）
        self._pulls = {}
        # 在途拉取集合（name）：worker 已把槽弹出正在拉取中的文件。对账轮在文件
        # 大、拉取未结束期间，目标文件盘上只有 .part 临时文件、最终文件还不存在，
        # 现有「not local_exists」判定会把它当缺失每轮重复入队 → 同文件并发开两条
        # pull_file、两份临时文件同时写盘、字节翻倍（大文件重复接收撑爆磁盘的根因）。
        # 置入在途集合后对账轮 `_enqueue_pull` 命中即跳过，根除重复接收；拉取结束
        # （成败皆然）即移除，后续轮次可再按需补拉。__cond__ 保护。
        self._in_flight = set()
        # 冲突覆盖拉取（阶段 3）：name -> src_id（等待该对端 RESP 会话后入队）
        self._pending_conflicts = {}
        # 拉取冷却表（阶段 5，防反复回环）：name -> {src_id: {fails, cooldown_until,
        # last_op_no}}。当源端明确「没有此文件」（pull_file 返回「复制端未提供数据」）
        # 且对账轮反复重拉同一文件时，冷却后暂时停止对该 (name, src_id) 的重复拉取，
        # 避免空转打日志/刷 UI「正在补齐」。冷却到期或对端出现更高版本时自动放行，
        # 不永久丢弃真实文件。__lock__ 保护。
        self._pull_cooldowns = {}
        # 同名拉取串行化锁（name -> Lock）：worker 池跨文件 5 并发，但同一文件的
        # 多份在途拉取（不同 RESP 槽先后弹出）必须串行——后者在前者完成后重新做
        # 新鲜度裁决，防止低版本槽迟到落盘覆盖胜者字节（并发池化引入的拉取交错）
        self._file_locks = {}
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        # 轮次统计（request_all → 各 RESP 聚合 → sync_done）
        self._round_targets = None
        self._round_has_diff = False
        # 普通回调通道（与 Distributor 同款）：GUI 用 Qt 信号；无事件循环场景
        # （后台/单测）经此同步回调——Qt 信号跨线程 emit 到 Python 槽在无事件
        # 循环时会排队丢失，回调通道保证可靠投递。
        self._sync_done_handler = None
        self._file_added_handler = None
        # 跨通道互斥护栏（阶段 6）：同一文件通常可经「主机直推(A)」与「自同步拉取(B)」
        # 两条通道同时投递给同一接收端——重连后主机差异补发会 A 直推缺失文件，自同步
        # 对账又会 B 拉取同一文件，造成同文件两份接收、字节翻倍。本端（接收端）作为
        # 两条通道的共同汇聚点，通过 host_push_guard 探测「该文件是否正被主机直推通道
        # 接收」：为真则 B 不再自同步拉取、让 A 单一投递（跨通道互斥，防第二个传输）。
        # 由宿主（client.py）在 FILE_BEGIN..FILE_END 窗口内置真；抑制只发生在窗口内，
        # 结束后对账轮可照常补拉，不永久丢弃真实文件。
        self._host_push_guard = None  # callable(relpath)->bool；True=主机直推正在投递

        # 拉取 worker 池（PULL_CONCURRENCY 个）：每轮入队的多个差异文件并发拉取，
        # _pulls 槽在 _cond 锁内弹出，多 worker 并发取件/拉取互不干扰
        self._workers = []
        for _ in range(self.PULL_CONCURRENCY):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self._workers.append(t)
        # 周期对账线程（收敛背stop）：分发信号在稳定连接中丢失时，靠它周期触发
        # 端到端状态对比补拉，不依赖断线重连/手动同步。stop() 统一回收。
        threading.Thread(target=self._reconcile_loop, daemon=True).start()

    # ---- 注入 ----

    def set_readonly(self, readonly: bool):
        """阶段 4：设置只读标志——只读端不供他端拉取其文件（回空 entries），
        仅保持单向下拉（request_all 仍可用）。主机端永不设置。"""
        with self._lock:
            self._readonly = bool(readonly)

    def set_file_provider(self, provider):
        """注入本端 FileProvider（会话服务能力；启动时序晚于 store 创建时调用）。"""
        self._provider = provider

    def set_sync_done_handler(self, cb):
        """设置一轮对比结束回调 cb(has_diff)（在发起/响应线程调用）。"""
        self._sync_done_handler = cb

    def set_file_added_handler(self, cb):
        """设置拉取完成回调 cb(rel_path)（在拉取工作线程调用）。"""
        self._file_added_handler = cb

    def set_host_push_guard(self, cb):
        """注入「主机直推已在投递该文件」探测回调（由宿主 client.py 注入）。

        cb(relpath)->bool：True=该文件正经主机直推通道(A)被本端接收，自同步拉取(B)
        应让位，避免同文件两条通道重复投递（跨通道互斥）。
        """
        self._host_push_guard = cb

    def _notify_sync_done(self, has_diff: bool):
        try:
            self.sync_done.emit(has_diff)
        except Exception:
            pass
        if self._sync_done_handler:
            try:
                self._sync_done_handler(has_diff)
            except Exception:
                pass

    def _notify_file_added(self, name: str):
        try:
            self.file_added.emit(name)
        except Exception:
            pass
        if self._file_added_handler:
            try:
                self._file_added_handler(name)
            except Exception:
                pass

    # ---- 状态快照（响应 FILE_STATE_REQ） ----

    @staticmethod
    def _is_transient_temp(rel: str) -> bool:
        """传输临时文件判定：FileProvider 接收时 mkstemp(prefix='.tcp_',
        suffix='.part')，仅在传输期间存在于目标目录，成功原子改名成正式文件、
        失败/取消即删除。这类半成品一旦被补扫/快照纳入同步，会形成「接收端
        产生 .part → 补扫 emit add → 对端再收再产生 .part」的互相投递死循环，
        故两个入口均需过滤。"""
        b = os.path.basename(rel)
        return b.startswith('.tcp_') and b.endswith('.part')

    def snapshot(self) -> list:
        """本端文件状态（wire 格式）。

        - 已删除条目（exists=False）也导出：对端据此补收删除或收敛清理
          （计划：本地与对端均为 CHANGE 且已删 → 删除条目，防列表无限变长）
        - 存在且为文件的条目导出：拉取只对文件字节
        - 存在但为目录的条目不导出：避免对端尝试拉取目录而失败
        """
        if self.distributor is None:
            return []
        out = []
        for st in self.distributor.states().values():
            if not st.exists:
                out.append(st.to_wire_dict())
                continue
            if self._is_transient_temp(st.name):
                continue  # 传输临时文件不入快照（防对端当作缺失文件来回拉取）
            try:
                if not os.path.isfile(self._safe_join(st.name)):
                    continue
            except ValueError:
                continue
            out.append(st.to_wire_dict())
        return out

    # ---- 收到 FILE_STATE_REQ：回 RESP ----

    def handle_state_req(self, end_id: str):
        """收到对端文件状态请求：登记自同步会话并回 FILE_STATE_RESP。

        会话按请求方隔离（session_id=sync_{请求方}，token 固定），多端同时
        拉取互不干扰；会话登记不清空剪贴板既有会话（FileProvider 保留）。
        """
        if self.mesh is None:
            return
        # 阶段 4：只读端不供他端拉取——回空 entries 且不登记会话（请求方仍会
        # 收到 RESP，其 round 不悬挂；只读端仅单向下拉）
        if self._readonly:
            try:
                self.mesh.send_to_peer(
                    end_id, Protocol.create_file_state_resp(
                        self.end_id, [], session=None))
            except Exception as e:
                self.log_message.emit(f"文件状态响应失败: {e}")
            return
        session = None
        provider = self._provider
        if provider is not None and getattr(provider, 'port', None):
            token = self._sync_tokens.get(end_id)
            if not token:
                token = uuid.uuid4().hex
                with self._lock:
                    self._sync_tokens[end_id] = token
            files = {}
            for st in self.distributor.states().values():
                if not st.exists:
                    continue
                try:
                    path = self._safe_join(st.name)
                except ValueError:
                    continue
                if os.path.isfile(path):
                    files[st.name] = path
            try:
                provider.register_session(
                    f"sync_{end_id}", token, files, replace_all=False)
            except Exception as e:
                self.log_message.emit(f"登记自同步会话失败: {e}")
                session = None
            else:
                session = {
                    'session_id': f"sync_{end_id}",
                    'token': token,
                    'host': provider.host,
                    'port': provider.port,
                }
        try:
            self.mesh.send_to_peer(
                end_id, Protocol.create_file_state_resp(self.end_id, self.snapshot(),
                                                        session=session))
        except Exception as e:
            self.log_message.emit(f"文件状态响应失败: {e}")

    # ---- 收到 FILE_STATE_RESP：对比 + 拉取队列 + 补收删除 + 清理 ----

    def handle_state_resp(self, end_id: str, content: dict):
        """收到对端文件状态响应：对比本端列表并补齐差异。"""
        if not isinstance(content, dict) or self.distributor is None:
            self._finish_round(end_id, False)
            return
        entries = content.get('entries') or []
        src_id = content.get('src_id') or end_id
        session = content.get('session')
        has_diff = False
        for d in entries:
            if not isinstance(d, dict):
                continue
            name = d.get('name', '')
            if not name:
                continue
            remote = FileState.from_wire_dict(src_id, d)
            try:
                local_exists = os.path.exists(self._safe_join(name))
            except ValueError:
                continue
            # 冲突覆盖拉取（阶段 3）：信号路径裁决覆盖后，此 RESP 为取会话的应答
            with self._lock:
                pend = self._pending_conflicts.get(name)
                if isinstance(pend, dict) and pend.get('src') == src_id:
                    self._pending_conflicts.pop(name, None)
                else:
                    pend = None
            if pend and remote.exists and session is not None \
                    and self._source_byte_fresh(remote):
                # 冲突覆盖：仲裁已判 src_id 胜，force 覆盖版本槽（无视列表内旧槽）
                if not self._state_delete_wins(name, remote, src_id):
                    if self._enqueue_pull(name, src_id, remote, session, force=True):
                        has_diff = True
            if remote.exists:
                # 对端有该文件：本端缺失 → 入拉取队列（同文件排队去重）
                if not local_exists:
                    # 状态表已删除且删除版本严格胜出 → 该候选是过期 add，
                    # 不入队（防删除-拉取-再删震荡）
                    if self._state_delete_wins(name, remote, src_id):
                        self.log_message.emit(
                            f"忽略拉取 {name}: 本端已删除且版本更新")
                        continue
                    # 仅从持有胜者字节的可信源补拉（字节指纹 == 知识版本），
                    # 拒绝陈旧字节端（bclk=0/指纹失配）→ 防装陈旧字节来回拉取
                    if self._source_byte_fresh(remote) \
                            and self._enqueue_pull(name, src_id, remote, session):
                        has_diff = True
                # 本端已有：冲突收敛（阶段 3）——三层裁决远端胜出则拉取覆盖
                else:
                    local_st = self.distributor.get_state(name)
                    if (local_st is not None and session is not None
                            and self._source_byte_fresh(remote)
                            and compare_states(remote, local_st, src_id,
                                               self.end_id) == 1):
                        if self._enqueue_pull(name, src_id, remote, session):
                            has_diff = True
                    # C 兜底（竞态修正）：知识 vv 胜于 bytes_vv 且无在途槽 →
                    # 强制补拉胜者字节。覆盖两类残留：① 胜者信号被 pulled 回执
                    # 提前合并（vv 已覆盖 → 信号去重、永不触发冲突拉取）；
                    # ② 低版本槽迟到落盘回退覆盖胜者字节（_do_pull 落盘前裁决
                    # 之外的漏网）。仅当响应端条目与本地仲裁胜者指纹（clock/ts）
                    # 一致且本端尚无其字节时补拉，避免反复重拉败者版本。
                    elif (session is not None
                          and self._need_bytes_backfill(name, local_st,
                                                        remote)):
                        if self._enqueue_pull(name, src_id, remote, session,
                                              force=True):
                            has_diff = True
            else:
                # 对端条目已删除：本端存在 → 补收删除（断网期间删除指令未达）。
                # 仅当对端条目确为「变更（已删）」才补删——对端若是「add 信号
                # 已应用、字节未拉取」的待命态（ADD + exists=False），本端文件
                # 不应被误删，否则删除-拉取震荡。
                if local_exists and remote.state == STATE_CHANGE:
                    self._apply_remote_delete(name, src_id, remote)
                    has_diff = True
                # 清理收敛：本地与对端均为「变更」且已不存在 → 删除条目
                # （防列表无限变长）。同样排除待命态：对端 ADD + exists=False
                # 时保留本端删除知识，避免误清理后把已删文件反向拉回。
                local_st = self.distributor.get_state(name)
                if local_st is not None and not local_st.exists \
                        and local_st.state == STATE_CHANGE \
                        and remote.state == STATE_CHANGE:
                    self._cleanup_entry(name)
        self._finish_round(end_id, has_diff)

    def _enqueue_pull(self, name: str, src_id: str, remote: FileState, session,
                      force: bool = False) -> bool:
        """拉取排队（版本感知槽）：每文件只保留一个当前仲裁胜者版本槽。

        - 无槽 → 建槽。
        - 有槽 → 用三层裁决比较现有槽 vs 新候选：新版本胜则就地覆盖槽
          （保留更高版本，旧字节不入队、不占带宽）；现有槽不旧于新候选则
          抛弃新候选（当前槽已是更优版本）。force 亦同：候选胜才覆盖——
          冲突覆盖（仲裁已判 src_id 胜）的迟到败者版本不允许顶掉已入队的
          胜者版本（防覆盖拉取通道竞态）。

        Returns:
            True = 槽被新建或升级（调用方据此累计 has_diff）；False = 抛弃。
        """
        # 跨通道互斥（阶段 6）：该文件正被主机直推通道(A)接收时，本端不应同时自同步
        # 拉取(B)——否则同文件两条通道同时投递、两份字节写盘（大文件重复接收/磁盘膨胀
        # 的复现路径之一）。若宿主判定 A 在途，本轮直接放弃 B 拉取、把投递交给 A 单一
        # 完成；A 的 FILE_BEGIN..FILE_END 窗口结束后守卫放开，后续对账轮可照常补拉。
        if self._host_push_guard is not None:
            try:
                if self._host_push_guard(name):
                    self.log_message.emit(
                        f"跳过自同步拉取 {name}: 主机直推通道正在投递该文件")
                    return False
            except Exception:
                pass
        # 阶段 5 冷却门：源端明确「没有此文件」且处于冷却中、候选版本未更新 →
        # 拦截入队，打断对账轮反复重拉同一文件（防空转/刷 UI/刷日志）。冷却到期
        # 或候选为更高版本（源端真的重新产出）则自动放行，不永久丢弃真实文件。
        cooled, _left = self._pull_is_cooled(name, src_id, remote.op_no)
        if cooled:
            self.log_message.emit(
                f"冷却抑制重复拉取 {name}（源端未提供该文件），暂停本轮重试")
            return False
        with self._cond:
            if name in self._in_flight:
                # 该文件正被某 worker 拉取中（槽已弹出但在在途集合）：不再建第二个槽、
                # 不再开第二条并发接收（避免同一文件两份临时文件同时写盘、字节翻倍
                # 撑爆磁盘）。在途拉取落盘前自带新鲜度裁决，若期间出现更高版本，
                # 其落盘后 _pull_stale_by_state / backfill 仍会触发补拉收敛。
                return False
            prev = self._pulls.get(name)
            if prev is not None:
                prev_st = FileState(
                    name=name, op_no=int(prev['op_no'] or 0),
                    state=STATE_ADD, exists=True,
                    clock=int(prev.get('clock', 0) or 0),
                    ts=float(prev.get('ts', 0.0) or 0.0),
                    vv={prev['end_id']: int(prev['op_no'] or 0)})
                # compare_states(prev, cand)：1=现有槽胜 / -1=候选胜 / 0=收敛
                # force 亦同：候选胜才覆盖——防「迟到败者版本顶掉已入队的胜者
                # 版本」的覆盖拉取通道竞态（本环境 45s 超时复现过）
                if compare_states(prev_st, remote, prev['end_id'],
                                  src_id) >= 0:
                    return False  # 现有槽不旧于新候选 → 抛弃新候选
                prev['end_id'] = src_id
                prev['op_no'] = remote.op_no
                prev['clock'] = remote.clock
                prev['ts'] = remote.ts
                # 槽升级同步记录源端磁盘字节指纹（落盘内容版本）：_do_pull
                # 落盘后据此判别实际内容是否即仲裁胜者
                prev['bytes_clock'] = remote.bytes_clock
                prev['bytes_ts'] = remote.bytes_ts
                prev['session'] = dict(session) if isinstance(session,
                                                               dict) else None
                self._cond.notify_all()
                return True  # 槽被覆盖升级
            self._pulls[name] = {
                'name': name,
                'end_id': src_id,
                'op_no': remote.op_no,
                'clock': remote.clock,
                'ts': remote.ts,
                'bytes_clock': remote.bytes_clock,
                'bytes_ts': remote.bytes_ts,
                'session': dict(session) if isinstance(session, dict) else None,
            }
            # notify_all：一轮对比可能一次入队多个文件，多个等待 worker 可同时取件
            self._cond.notify_all()
        return True

    def _apply_remote_delete(self, name: str, src_id: str, remote: FileState):
        """补收删除：构造 delete 信号走分发链路应用（含防回声转发与状态合并）。"""
        signal = {
            'src_id': src_id,
            'op_no': remote.op_no,
            'op': 'delete',
            'file': name,
            'state': remote.state,
            'clock': remote.clock,
            'ts': remote.ts,
        }
        self.distributor.on_signal(signal)
        self.log_message.emit(f"补收删除: {name}")

    def _cleanup_entry(self, name: str):
        """双方均已删除：移除本端条目（防列表无限变长）。"""
        if self.distributor is not None:
            self.distributor.remove_state(name)
            self.log_message.emit(f"状态条目收敛移除: {name}")

    def _need_bytes_backfill(self, name: str, local_st, remote: FileState) -> bool:
        """C 兜底判定：知识 vv 严格胜于 bytes_vv 且无在途槽 → 补拉胜者字节。

        条件（防反复重拉败者版本/空转）：
        - 知识 vv 已覆盖 bytes_vv（存在已应用但字节未到位或被回退覆盖的版本）
         且 bytes_vv 未覆盖知识（确实有版本缺口）；
        - 该文件无在途槽（_pulls）且无待命冲突拉取（_pending_conflicts）；
        - 响应端条目与本地仲裁胜者指纹（clock/ts）一致——仅补拉真正胜者，
          不拉败者版本（避免回退）；
        - 字节指纹判定（防交叉反复重拉）：文件存在且本端字节指纹 == 状态表
          胜者指纹 → 本端字节已是仲裁胜者内容，无需补拉。各端 emit_pulled
          每次自增本端 op_no 并广播回执，对端快照版本号其他端永远「没见过」
          （bytes_vv 覆盖判定恒为缺口），若仅靠 bytes_vv 覆盖判定会对账轮
          反复交叉补拉同一内容；指纹一致即证明内容已对，与对端快照版本无关。
          指纹不一致（字节确非胜者内容）才回退到 bytes_vv 覆盖判定。
        """
        if local_st is None or not remote.exists:
            return False
        with self._cond:
            if name in self._pulls or name in self._pending_conflicts:
                return False
        if not vv_covers(local_st.vv, local_st.bytes_vv):
            return False
        if vv_covers(local_st.bytes_vv, local_st.vv):
            return False
        if remote.clock != local_st.clock \
                or abs(remote.ts - local_st.ts) > 1e-6:
            return False
        # 仅从「磁盘字节 == 其知识版本」的可信源补拉：知识指纹（clock/ts）全网
        # 收敛后每端都相同，即使字节各异（各自陈旧内容）——只看知识会把陈旧
        # 字节端也当作可信源，多端互相拉取陈旧字节、胜者内容被覆盖销毁 →
        # 来回拉取震荡。字节指纹失配 → 该端磁盘不是胜者内容，拒绝补拉。
        # 字节指纹必须严格匹配（不允许 bytes_clock=0 降级放行）：本版本所有
        # 写/拉路径都会设置字节指纹，0 只可能是「从未写过也从未成功拉取」的
        # 陈旧磁盘端——降级放行会把其陈旧字节当作可信源，覆盖销毁胜者内容。
        if remote.bytes_clock != remote.clock \
                or abs(remote.bytes_ts - remote.ts) > 1e-6:
            return False
        # 字节指纹一致 → 本端磁盘已是胜者内容，无需补拉（无论对端快照版本）
        if local_st.bytes_clock and local_st.bytes_clock == local_st.clock \
                and abs(local_st.bytes_ts - local_st.ts) <= 1e-6:
            try:
                if os.path.exists(self._safe_join(name)):
                    return False
            except ValueError:
                return False
        return not vv_covers(local_st.bytes_vv, remote.vv)

    @staticmethod
    def _source_byte_fresh(remote: FileState) -> bool:
        """拉取源是否持有仲裁胜者字节（字节可信源判定）。

        知识指纹（clock/ts）全网收敛后每端相同，即使字节各异（各自陈旧内容）——
        只看知识会把陈旧字节端也当作可信源，多端互相拉取陈旧字节、胜者内容被
        覆盖销毁 → 来回拉取震荡。仅当源端磁盘字节指纹 == 其知识版本（clock/ts）
        时，其字节才是仲裁胜者内容，可作拉取源。所有写/拉路径都会设置字节指纹
        （emit/emit_pulled），bytes_clock=0 只可能是「从未写过也从未成功拉取」
        的陈旧磁盘端；字节指纹失配则是「知识推进、字节未到位」的中间态。
        """
        return bool(remote.bytes_clock) and remote.bytes_clock == remote.clock \
            and abs(remote.bytes_ts - remote.ts) <= 1e-6

    def _state_delete_wins(self, name: str, cand: FileState, cand_end: str) -> bool:
        """状态表已删除且删除版本严格胜于候选 → 忽略该候选的拉取/落盘。

        防「删除 → 对账拉到过期 add → 复活 → 下轮再删」震荡：对端快照可能是
        删除信号到达前生成的过期数据，本端状态表若已记录更高版本的删除，则
        候选 add 不应再入队拉取或落盘。候选严格更新（对端确实重新添加）时
        compare_states 判候选胜，返回 False，正常拉取覆盖。

        Args:
            cand: 候选拉取条目（对端 add 版本）
            cand_end: 候选归属端（用于冲突仲裁末位裁决）
        """
        st = self.distributor.get_state(name) if self.distributor else None
        if st is None or st.exists:
            return False
        if st.state != STATE_CHANGE:
            # exists=False 但状态是 ADD：这是"add 信号已应用、字节尚未拉取"的
            # 拉取待命态，不是删除。其 vv 可能已合并其他端 emit_pulled 回执
            # 知识而"覆盖"候选，误判删除胜出会拦截正常缺失拉取。仅真正删除
            # （STATE_CHANGE + exists=False）才参与删除胜出裁决。
            return False
        # 本地删除条目的归属端：取 vv 中 op_no 最大的源端（删除发起端，
        # 仅用于末位裁决兜底；常规路径由 vv/clock/ts 层判定）
        st_end = self.end_id
        if st.vv:
            st_end = max(st.vv, key=lambda k: st.vv[k])
        return compare_states(st, cand, st_end, cand_end) == 1

    def emit_local_missing(self, exclude_dirs: Optional[set] = None) -> int:
        """本地补扫（建连时/切回同步时兜底）：扫描同步文件夹，对磁盘存在但
        状态表缺失的文件 emit add 信号，纳入同步体系。

        覆盖三类场景：① mesh 未就绪窗口内添加文件（信号未发出、状态未记录）
        ——建连后补扫兜底；② 启动前已放置于文件夹的文件（状态表无条目）；
        ③ 收集模式期间主机对根目录的本地增删被「收集不转发」跳过、切回同步
        后由 _complete_mode_switch 调用本函数补扫广播（此时连接端已在线，
        不会经过 _on_mesh_peer_connected）。状态表已有条目（含已删除条目）跳过，
        不复活已删文件。只读端不发起任何修改信号（返回 0，仅单向下拉）。

        Args:
            exclude_dirs: 需从扫描中剪枝的顶层目录名集合（收集模式 IP 文件夹）。
                切回同步时这些文件夹是已连接/历史连接端的私有目录，内部文件归
                属对应连接端、不应作为共享文件被扫描广播；不剪枝会把其内存误
                emit add 广播给所有对端。按目录名匹配（os.walk 的 `_dirs` 元素
                即目录 basename），命中即整棵子树剪掉。

        Returns:
            新 emit 的文件数
        """
        if self.distributor is None or self._readonly:
            return 0
        if exclude_dirs is None:
            exclude_dirs = set()
        count = 0
        try:
            for root, _dirs, files in os.walk(self.sync_folder):
                _dirs[:] = [d for d in _dirs
                            if not d.startswith('.') and d not in exclude_dirs]
                for fn in files:
                    if self._is_transient_temp(fn):
                        continue  # 传输临时文件不入同步（见 _is_transient_temp）
                    full = os.path.join(root, fn)
                    try:
                        rel = os.path.relpath(full, self.sync_folder).replace('\\', '/')
                        self._safe_join(rel)
                    except ValueError:
                        continue
                    if self.distributor.get_state(rel) is not None:
                        continue  # 状态表已有条目（存在/已删）→ 不重复 emit
                    self.distributor.emit('add', rel)
                    count += 1
        except OSError:
            pass
        if count:
            self.log_message.emit(f"本地补扫: 新发现 {count} 个文件入同步")
        return count

    # ---- 拉取执行（后台线程） ----

    def _worker_loop(self):
        while not self._stop.is_set():
            with self._cond:
                while not self._pulls and not self._stop.is_set():
                    self._cond.wait(timeout=0.5)
                if self._stop.is_set():
                    break
                item = next(iter(self._pulls.values()))
                self._pulls.pop(item['name'], None)
                # 标记在途：对账轮 `_enqueue_pull` 据此跳过该文件的重复入队（根除
                # 大文件拉取期间重复开第二条并发接收）。拉取结束（成败/未获锁放弃
                # 槽）在 finally 移除。
                self._in_flight.add(item['name'])
            try:
                # 同名拉取串行化：同文件多份在途槽按序执行（跨文件仍 5 并发）。
                # 等待期间 _stop 置位则放弃该槽（退出中）
                lk = self._get_file_lock(item['name'])
                acquired = False
                while not self._stop.is_set():
                    acquired = lk.acquire(timeout=0.5)
                    if acquired:
                        break
                if acquired:
                    try:
                        self._do_pull(item)
                    except Exception as e:
                        self.log_message.emit(f"拉取任务失败: {e}")
            finally:
                # 拉取结束（成功/失败/未获锁放弃）：解除在途标记，后续轮次可再补拉。
                # 用 _cond 与 _enqueue_pull 的读侧同步；acquired 在 try 内初始化，
                # 未进入循环（_stop 置位）时保持 False，finally 不重复释放锁。
                with self._cond:
                    self._in_flight.discard(item['name'])
                if acquired:
                    try:
                        lk.release()
                    except Exception:
                        pass

    def _get_file_lock(self, name: str) -> threading.Lock:
        """获取同名拉取串行化锁（按需创建；会话内常驻，数量 = 被拉取的不同文件数）。"""
        with self._lock:
            lk = self._file_locks.get(name)
            if lk is None:
                lk = threading.Lock()
                self._file_locks[name] = lk
            return lk

    def _is_pull_fresh(self, name: str, item: dict) -> bool:
        """本份在途拉取版本是否仍应由本份落盘。

        判定只对照列表（`_pulls`），不碰状态表——状态表 vv 是"已应用信号的分发
        知识"，不等同"字节已到位"；若以状态表判过时，会误丢弃分态仍需要的字节。

        item 已从列表弹出（在途）。若期间列表又入队了该文件**严格更高**的槽，
        则本份过时（返回 False）→ 抛弃，交由更高版本槽收敛，避免旧版本回退
        覆盖新版本。列表无更高槽（无槽 / 同版本）→ 本份新鲜，正常落盘。
        """
        with self._cond:
            other = self._pulls.get(name)
            if other is None:
                return True
            other_st = FileState(
                name=name, op_no=int(other['op_no'] or 0), state=STATE_ADD,
                exists=True, clock=int(other.get('clock', 0) or 0),
                ts=float(other.get('ts', 0.0) or 0.0),
                vv={other['end_id']: int(other['op_no'] or 0)})
            item_st = FileState(
                name=name, op_no=int(item['op_no'] or 0), state=STATE_ADD,
                exists=True, clock=int(item.get('clock', 0) or 0),
                ts=float(item.get('ts', 0.0) or 0.0),
                vv={item['end_id']: int(item['op_no'] or 0)})
            # compare_states(other, item)：1=列表槽严格更新 → 本份过时；否则应用
            return compare_states(other_st, item_st, other['end_id'],
                                  item['end_id']) != 1

    def _pull_stale_by_state(self, name: str, item: dict) -> bool:
        """状态表仲裁胜者严格胜于本份在途版本 → 本份过时，应丢弃。

        竞态修正（三端并发改同一文件时低版本槽迟到落盘回退覆盖胜者字节）：
        只比较第二/三层（逻辑钟、时间戳）——第一层 vv 会被 pulled 回执虚增
        知识，直接对照会误丢「唯一字节源」的合法拉取；而状态表的 clock/ts
        经 _store 取最大值合并，等于当前仲裁胜者的指纹。本份的 (clock, ts)
        低于胜者 → 已收敛到更新版本，旧字节落盘即回退，丢弃交由胜者版本收敛；
        与本份同指纹（同版本/待拉同版本）→ 不过时，正常落盘。
        """
        st = self.distributor.get_state(name) if self.distributor else None
        if st is None:
            return False
        cand_clock = int(item.get('clock', 0) or 0)
        cand_ts = float(item.get('ts', 0.0) or 0.0)
        if st.clock != cand_clock:
            return st.clock > cand_clock
        if st.ts != cand_ts:
            return st.ts > cand_ts
        return False

    def _mark_bytes_unknown(self, name: str):
        """把本端字节指纹清零（bytes_clock=0）：磁盘内容不可信时的兜底。

        并发拉取迟到败者落盘后，磁盘字节已非仲裁胜者内容——清零字节指纹使
        本端不再被 _source_byte_fresh 判定为可信拉取源（不会把败者字节散布
        全网），对账 backfill 据此从字节新鲜的端补拉当前胜者覆盖收敛。
        """
        if self.distributor is None:
            return
        st = self.distributor.get_state(name)
        if st is None:
            return
        with self.distributor._lock:
            st.bytes_clock = 0
            st.bytes_ts = 0.0

    def _do_pull(self, item: dict):
        name = item['name']
        session = item.get('session')
        if not session:
            self.log_message.emit(f"拉取 {name}: 对端未提供会话")
            return
        # 应用前裁决（IO 前）：仲裁已推进到更高版本 → 本份过时，直接抛弃该槽
        if not self._is_pull_fresh(name, item) \
                or self._pull_stale_by_state(name, item):
            self.log_message.emit(f"拉取 {name} 已过时，抛弃该槽")
            return
        host = session.get('host', '')
        port = int(session.get('port', 0) or 0)
        session_id = session.get('session_id', '')
        token = session.get('token', '')
        if not host or not port or not session_id or not token:
            self.log_message.emit(f"拉取 {name}: 会话信息不完整")
            return
        try:
            dest = self._safe_join(name)
        except ValueError as e:
            self.log_message.emit(f"拒绝非法路径: {e}")
            return
        os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
        ok, _received, err = pull_file(
            host, port, session_id, token, name, dest,
            msg_type=MessageType.SYNC_PULL_REQ,
            # 把本 store 的停止事件透传给拉取：退出房间/整体停止时设置 _stop，
            # pull_file 会在下一次 recv 超时(≤1s) 感知并取消，释放 .part 句柄并删除
            # 临时文件——否则工作线程会一直占着句柄（直到整体超时或对端关连接），
            # 导致传输中的临时文件死锁、退出房间无法删除。
            stop_event=self._stop,
            progress_cb=lambda recv, size: self.pull_progress.emit(name, recv, size),
            overall_timeout=self.PULL_OVERALL_TIMEOUT)
        if ok:
            # 应用前裁决（IO 后）：状态表已删除且删除版本严格胜出 → 本份为
            # 过期 add 复活，移除字节不落应用、不 emit_pulled（防删除-拉取震荡）
            cand = FileState(
                name=name, op_no=int(item['op_no'] or 0), state=STATE_ADD,
                exists=True, clock=int(item.get('clock', 0) or 0),
                ts=float(item.get('ts', 0.0) or 0.0),
                vv={item['end_id']: int(item['op_no'] or 0)})
            if self._state_delete_wins(name, cand, item['end_id']):
                self.log_message.emit(
                    f"拉取 {name} 落盘，但本地已删除该文件，移除复活字节")
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.pull_done.emit(name, False)
                return
            # 应用后裁决（并发池化修正）：拉取期间状态表推进到更高版本（更高版本
            # 信号应用/更高版本已落盘）→ 本份为迟到败者。磁盘已被本份覆盖为败者
            # 字节（可能覆盖了更早落盘的胜者字节），内容不可信：标记字节指纹未知
            # （bytes_clock=0，_source_byte_fresh 据此拒绝以本端为拉取源），不推进
            # bytes_vv；立即触发一轮对账，从字节新鲜的端补拉当前胜者覆盖收敛。
            if self._pull_stale_by_state(name, item):
                self._mark_bytes_unknown(name)
                self.pull_done.emit(name, False)
                self.log_message.emit(
                    f"拉取 {name} 落盘后已过时，标记字节未知待补拉")
                try:
                    self.request_all()
                except Exception:
                    pass
                return
            # 记录本端已持有（vv 推进到远端 op_no）+ 广播 add 状态；UI 刷新。
            # clock/ts 取远端条目仲裁知识版本（快照 clock/ts）——即当前仲裁胜者，
            # 状态表不回退、对账指纹比对不失配（用本端自增会污染仲裁指纹）。
            # 关键：bytes_clock/bytes_ts 用源端磁盘字节指纹——源端知识推进但磁盘
            # 字节未到位时两者失配（fresh=False），本端 bytes_vv 不推进，对账兜底
            # 仍会从可信源（字节指纹 == 胜者指纹的端）补拉收敛。
            src_clock = int(item.get('clock', 0) or 0)
            src_ts = float(item.get('ts', 0.0) or 0.0)
            bclock = int(item.get('bytes_clock', 0) or 0)
            bts = float(item.get('bytes_ts', 0.0) or 0.0)
            # 以「实际收到内容」判定新鲜度，不信源端声明：pull_file 已把源磁盘
            # mtime 复制到 dest，dest mtime 即源端磁盘真实内容版本的可靠标记
            # （emit 时 os.utime 校准）。源端磁盘若被其自身并发拉取临时覆盖为
            # 旧字节，其磁盘 mtime 与声明指纹不符 → 收到内容非仲裁胜者 →
            # fresh=False，bytes_vv 不推进、bytes_ts 如实记录收到内容，对账
            # backfill 从字节真实的端补拉收敛（杜绝「谎报 fresh」永久分叉）。
            received_ts = os.path.getmtime(dest)
            fresh = bool(bclock) and bclock == src_clock \
                and abs(received_ts - bts) <= 1e-6
            self.distributor.emit_pulled(name, item['end_id'], item['op_no'],
                                         src_clock, src_ts, fresh,
                                         bclock, received_ts)
            self._notify_file_added(name)
            self.pull_done.emit(name, True)
            self.log_message.emit(f"自同步拉取完成: {name}")
        else:
            self.pull_done.emit(name, False)
            # 阶段 5：源端明确「没有此文件」→ 记录冷却（连续多次后对账轮暂停重复拉取）
            if self._source_lacks_file(err):
                self._record_pull_cooldown(name, item['end_id'], item['op_no'])
            self.log_message.emit(f"自同步拉取失败 {name}: {err}")

    # ---- 拉取冷却（阶段 5：防「源端无此文件」反复回环） ----

    # 源端明确「没有此文件」所需连续失败次数；达到后进入冷却
    PULL_COOLDOWN_FAILS = 3
    # 冷却时长（秒）：期间停止对该 (name, src_id) 的重复拉取，到期自动放行重试
    PULL_COOLDOWN_SECONDS = 120.0

    @staticmethod
    def _source_lacks_file(err) -> bool:
        """拉取失败原因是否表示「源端明确没有此文件」。

        仅匹配 pull_file 返回的那条固定文案连线成「完整连接但源端从未发送文件
        数据」——即源端状态表有该条目但磁盘/会话已无字节，永不自我恢复，
        正是反复回环的元凶。其余错误（无法连接、拉取超时、拉取失败、文件不
        完整、已取消）都是瞬时/可在源端侧恢复的故障，不应进入冷却。
        """
        return bool(err) and "复制端未提供数据" in str(err)

    def _record_pull_cooldown(self, name: str, src_id: str, op_no):
        """记录一次「源端无此文件」拉取失败；连续多次后进入冷却。

        __lock__ 保护。冷却完成后（gate 侧触碰到期条目会移除）下次重试若仍失败
        再累计，故内存仅保留「正在冷却」的条目，不会无限增长。
        """
        with self._lock:
            recs = self._pull_cooldowns.setdefault(name, {})
            rec = recs.setdefault(src_id, {
                'fails': 0, 'cooldown_until': 0.0, 'last_op_no': 0})
            rec['last_op_no'] = max(rec.get('last_op_no', 0), int(op_no or 0))
            rec['fails'] = rec.get('fails', 0) + 1
            if rec['fails'] >= self.PULL_COOLDOWN_FAILS:
                rec['cooldown_until'] = time.time() + self.PULL_COOLDOWN_SECONDS
                rec['fails'] = 0

    def _pull_is_cooled(self, name: str, src_id: str, op_no) -> tuple:
        """判定 (name, src_id) 当前是否处于冷却态并清理到期条目。

        Returns:
            (是否冷却中, 冷却剩余秒数)。冷却中则 _enqueue_pull 应拦截。

        副作用：冷却已到期（0 < cooldown_until <= now）的条目在此移除，放行重试；
        对端出现更高版本（op_no > last_op_no，源端真的重新产出了该文件）则
        解除冷却并放行，避免误拦真实的新文件。仍在累计（cooldown_until == 0，
        失败次数未达阈值）时保留计数、不拦截。
        """
        with self._lock:
            recs = self._pull_cooldowns.get(name)
            if not recs:
                return False, 0.0
            rec = recs.get(src_id)
            if not rec:
                return False, 0.0
            now = time.time()
            until = rec.get('cooldown_until', 0.0)
            if until > now:
                if int(op_no or 0) > rec.get('last_op_no', 0):
                    del recs[src_id]  # 新版本 → 解除冷却
                    return False, 0.0
                return True, until - now
            if until > 0.0:
                # 冷却已到期：移除条目（含 last_op_no 记录），允许重试
                del recs[src_id]
            # until == 0：仍在累计（失败数未达阈值），保留计数，不拦截
            return False, 0.0

    # ---- 触发一轮对比 ----

    def request_conflict_pull(self, name: str, src_id: str):
        """冲突覆盖拉取（阶段 3）：远端信号胜出覆盖本端旧内容后，请求该对端
        状态以取得自同步会话，RESP 到达后入拉取队列端到端拉取胜方字节。

        待命条目带时间戳，事后由 _sweep_pending_conflicts 定期清理（对端不可达
        时避免残留无限增长）。
        """
        if self.mesh is None or not name or not src_id:
            return
        with self._lock:
            self._pending_conflicts[name] = {'src': src_id, 'ts': time.time()}
        try:
            self.mesh.send_to_peer(src_id, Protocol.create_file_state_req(self.end_id))
        except Exception as e:
            with self._lock:
                self._pending_conflicts.pop(name, None)
            self.log_message.emit(f"冲突拉取请求失败: {name} {e}")

    def _sweep_pending_conflicts(self, ttl: float = PENDING_CONFLICT_TTL):
        """清理过期冲突待命条目：对端长期不可达（未 RESP 应答）时移除，防内存增长。"""
        cutoff = time.time() - ttl
        stale = []
        with self._lock:
            for name, pend in list(self._pending_conflicts.items()):
                if isinstance(pend, dict) and pend.get('ts', 0) < cutoff:
                    stale.append(name)
            for name in stale:
                self._pending_conflicts.pop(name, None)
        if stale:
            self.log_message.emit(f"清理过期冲突待命: {len(stale)} 项")

    def _reconcile_loop(self):
        """周期对账（收敛背stop）：每 RECONCILE_INTERVAL 对在线对端 trigger
        request_all；一轮对比进行中（_round_targets 非 None）则跳过本拍，避免
        与在建轮次交叠互相覆盖 round 聚合；顺带清理过期冲突待命条目。stop() 后退出。"""
        while not self._stop.wait(self.RECONCILE_INTERVAL):
            try:
                self._sweep_pending_conflicts()
            except Exception:
                pass
            # 只读端同样周期对账（单向下拉补齐本端缺失文件；不供他端拉取由
            # handle_state_req 回空 entries 保证）
            if self.mesh is None:
                continue
            if not self.mesh.connected_end_ids():
                continue
            with self._lock:
                if self._round_targets is not None:
                    continue  # 上轮对比仍在聚合，跳过本拍防交叠
            try:
                self.request_all()
            except Exception:
                pass

    def request_all(self) -> int:
        """向全部直连对端请求文件状态（手动同步/断线重连/周期对账自动补齐）。

        Returns:
            成功下发请求的对端数
        """
        if self.mesh is None:
            return 0
        targets = self.mesh.connected_end_ids()
        if not targets:
            return 0
        with self._lock:
            self._round_targets = set(targets)
            self._round_has_diff = False
        for eid in targets:
            try:
                self.mesh.send_to_peer(eid, Protocol.create_file_state_req(self.end_id))
            except Exception:
                with self._lock:
                    self._round_targets.discard(eid)
        # 超时兜底：对端不应答（离线/未连）时也结束本轮，避免通知悬挂
        threading.Thread(target=self._round_timeout, args=(set(targets),), daemon=True).start()
        return len(targets)

    def _round_timeout(self, targets: set):
        time.sleep(self.ROUND_TIMEOUT)
        with self._lock:
            if self._round_targets is not None:
                had = self._round_has_diff
                self._round_targets = None
                self._round_has_diff = False
            else:
                had = False
        self._notify_sync_done(had)

    def _finish_round(self, end_id: str, has_diff: bool):
        """聚合一轮 RESP 结果；全部目标应答后发 sync_done。"""
        notify = False
        had = False
        with self._lock:
            if self._round_targets is not None:
                self._round_targets.discard(end_id)
                if has_diff:
                    self._round_has_diff = True
                if not self._round_targets:
                    had = self._round_has_diff
                    self._round_targets = None
                    self._round_has_diff = False
                    notify = True
        if notify:
            self._notify_sync_done(had)

    # ---- 工具 ----

    def _safe_join(self, name: str) -> str:
        """安全拼接同步文件夹路径（防路径穿越，与 Distributor 同款）。"""
        if not name:
            raise ValueError("文件名为空")
        norm = os.path.normpath(name.replace('\\', '/'))
        if norm in ('.', '') or norm.startswith('..') or os.path.isabs(norm):
            raise ValueError(f"非法路径: {name}")
        path = os.path.normpath(os.path.join(self.sync_folder, norm))
        if path != self.sync_folder and not path.startswith(self.sync_folder + os.sep):
            raise ValueError(f"非法路径: {name}")
        return path

    # ---- 生命周期 ----

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for w in getattr(self, '_workers', ()):
            if w.is_alive():
                try:
                    w.join(timeout=2.0)
                except Exception:
                    pass
