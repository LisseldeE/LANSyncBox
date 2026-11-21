"""
去中心化同步：自同步链路（端到端对比拉取，阶段 2）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import threading
import time
import uuid
from typing import Optional

from PySide6.QtCore import QObject, Signal

from network.protocol import Protocol, MessageType
from network.file_provider import pull_file, PULL_STALE_DISCARD_MSG, PULL_NO_SPACE_PREFIX
from sync.file_hasher import FileHasher
from sync.vector import (FileState, STATE_ADD, STATE_CHANGE, compare_states)


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
    ALIGN_HASH_RETRIES = 3  # 静默对齐残留哈希失败重试上限：对齐期文件被改写时
                            # 重算；超限放弃静默对齐，交由正常新增链路同步，
                            # 避免空 hash 残留让 _align_mode 永久不结束

    def __init__(self, end_id: str, sync_folder: str, mesh=None, distributor=None,
                 provider=None, parent=None, silent_align: bool = False):
        super().__init__(parent)
        self.end_id = end_id
        self.sync_folder = os.path.abspath(sync_folder)
        self.mesh = mesh            # MeshManager
        self.distributor = distributor  # Distributor（状态表单一数据源）
        self._provider = provider    # 本端 FileProvider（会话服务；None 时可后注入）
        # 阶段 4：只读端不发起自同步推送（回空 entries，不供他端拉取），仅单向下拉
        self._readonly = False

        # 阶段 D：清缓存回归静默对齐。db 已删、目录有残留时，残留只登记在
        # `_align_residues`，不进 Distributor、不进快照、不广播；与在线对端
        # 完成一轮对账后按 §8 裁决。
        self._align_mode = False
        self._align_residues = {}   # name -> {file_id, hash, size, mtime}
        self._align_remote = {}     # name -> 本轮对端 FileState
        self._align_pending = set()  # 对端仍 pending 的路径
        self._align_sessions = {}   # src_id -> session
        self._align_sources = {}    # name -> src_id
        self._align_got_response = False
        self._align_incomplete = False
        self._align_hasher = None
        self._align_timer = None

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
        # 墓碑对账在途端集合：建连后先分页拉墓碑，完成前不对该端发 FILE_STATE_REQ
        self._tombstone_pending = set()
        # 在途期间被缓冲的候选槽（name -> slot）：在途窗口内到达的候选不能丢弃——
        # 丢弃即遗忘，只能等下一拍对账（上限 RECONCILE_INTERVAL）才补发，收敛延迟
        # 被对账周期支配。worker 收尾（_in_flight.discard 之后）立即并入 _pulls 重判。
        # __cond__ 保护。
        self._pull_deferred = {}
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
        self._round_gen = 0  # 轮次代数：新 request_all 递增；旧轮超时据此自证过期，不误杀新轮
        # 在线闸门（删除收敛）：本轮任一响应端声明「该路径存在」的名字集合 +
        # 本轮已应答的直连端集合 + 本轮任一响应端「明确声明该路径已删除」的
        # 集合。墓碑仅在「全员应答、无人声明存在、且确有对端明确确认该路径
        # 也已删除、本端磁盘已删」时才移除；对端沉默（快照为空/目录不入快照）
        # 绝不等于对端已删，缺席端回归前一律保留墓碑压制其陈旧 add。
        self._round_exists_claim = set()
        self._round_deleted_claim = set()
        self._round_responded = set()
        self._round_remote_keys = set()  # 本轮对端声明的活对象 (path, id)
        self._align_remote_by_hash = {}
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

        # 启动清扫：进程重启后同步目录内遗留的传输临时文件（.tcp_*.part）已无
        # 对应传输，清除以防占盘/被误当正式文件。仅清 .tcp_*.part，不碰用户 .tmp。
        self.sweep_transient_temp()

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

        if silent_align:
            self.begin_silent_align()

    # ---- 阶段 D：静默对齐 ----

    @property
    def aligning(self) -> bool:
        with self._lock:
            return self._align_mode

    def begin_silent_align(self):
        """扫描清缓存后残留：只登记待对齐集合，算哈希，不广播不供拉取。"""
        if self.distributor is None:
            return
        residues = {}
        try:
            for root, dirs, files in os.walk(self.sync_folder):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fn in files:
                    if fn.startswith('.tcp_') and fn.endswith('.part'):
                        continue
                    rel = os.path.relpath(os.path.join(root, fn),
                                          self.sync_folder).replace('\\', '/')
                    try:
                        self._safe_join(rel)
                    except ValueError:
                        continue
                    try:
                        st = os.stat(self._safe_join(rel))
                    except OSError:
                        continue
                    residues[rel] = {'file_id': uuid.uuid4().hex,
                                     'hash': '', 'size': int(st.st_size),
                                     'mtime': float(st.st_mtime),
                                     'retries': 0, 'failed': False}
        except OSError:
            residues = {}
        with self._lock:
            if self._align_mode or not residues:
                return
            self._align_residues = residues
            self._align_remote = {}
            self._align_pending = set()
            self._align_sessions = {}
            self._align_sources = {}
            self._align_got_response = False
            self._align_incomplete = False
            self._align_mode = True
        hasher = FileHasher(callback=self._on_align_hash_done)
        self._align_hasher = hasher
        for rel, rec in residues.items():
            hasher.submit(self._safe_join(rel), rel, rec['file_id'],
                          kind='align')
        # 无任何在线对端时，不能无限等待：一轮超时后按本端新增发布。
        timer = threading.Timer(self.ROUND_TIMEOUT,
                                self.finalize_offline_alignment)
        timer.daemon = True
        self._align_timer = timer
        timer.start()

    def _on_align_hash_done(self, result: dict):
        rel = result.get('rel_path', '')
        resubmit = False
        file_id = ''
        hasher = None
        with self._lock:
            rec = self._align_residues.get(rel)
            if rec is None or not self._align_mode:
                return
            err = result.get('error') or ''
            if not err and not result.get('changed'):
                rec['hash'] = result.get('hash') or ''
                rec['size'] = int(result.get('size') or rec['size'])
                rec['mtime'] = float(result.get('mtime') or rec.get('mtime') or 0.0)
                return
            if err == 'missing':
                # 扫描后文件已被删除：重算不可能成功，直接落终态
                rec['failed'] = True
            else:
                rec['retries'] = int(rec.get('retries') or 0) + 1
                if rec['retries'] > self.ALIGN_HASH_RETRIES:
                    rec['failed'] = True
                    self.log_message.emit(
                        f"静默对齐放弃残留（哈希失败 {rec['retries']} 次）: {rel}")
                else:
                    resubmit = True
            if resubmit:
                file_id = rec['file_id']
                hasher = self._align_hasher
        if resubmit and hasher is not None:
            try:
                hasher.submit(self._safe_join(rel), rel, file_id, kind='align')
            except (OSError, ValueError):
                pass

    def wait_align_hashed(self, timeout: float = 5.0) -> bool:
        """等待残留哈希全部完成（测试/启动同步兜底）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._align_mode:
                    return True
                pending = [r for r in self._align_residues.values()
                           if not r.get('hash') and not r.get('failed')]
            if not pending:
                return True
            time.sleep(0.02)
        return False

    def finalize_offline_alignment(self):
        """无在线对端兜底：残留视为本端新增并发布。"""
        with self._lock:
            if not self._align_mode or self._align_got_response:
                return
        # 仍有直连对端（只是尚未应答）不算离线：续超时等待，避免提前发布残留
        try:
            connected = self.mesh.connected_end_ids() if self.mesh else set()
        except Exception:
            connected = set()
        if connected:
            timer = threading.Timer(self.ROUND_TIMEOUT,
                                    self.finalize_offline_alignment)
            timer.daemon = True
            with self._lock:
                if self._align_mode:
                    self._align_timer = timer
            timer.start()
            return
        self._finalize_alignment(offline=True)

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

    def sweep_transient_temp(self) -> int:
        """清扫同步目录内遗留的传输临时文件（.tcp_*.part）。

        传输中途崩溃/断电会在目标目录留下半成品；进程启动时已无对应传输，
        必须清除。仅清 .tcp_*.part（FileProvider 与主机直推统一命名），不碰
        用户自己的 .tmp 等其它文件。Returns: 删除个数。
        """
        import shutil
        removed = 0
        try:
            for root, dirs, files in os.walk(self.sync_folder):
                # 遗留临时目录（添加文件夹中途崩溃）整棵删除，否则会被下面的
                # dot 剪枝永久隐藏，既不清理也不会被同步
                for dn in list(dirs):
                    if self._is_transient_temp(dn):
                        shutil.rmtree(os.path.join(root, dn), ignore_errors=True)
                        dirs.remove(dn)
                        removed += 1
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fn in files:
                    if not self._is_transient_temp(fn):
                        continue
                    try:
                        os.remove(os.path.join(root, fn))
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        if removed:
            self.log_message.emit(f"清理遗留传输临时文件: {removed} 个")
        return removed

    def snapshot(self) -> list:
        """本端活对象快照（wire 格式）；删除信息只经 TOMBSTONE_RESP 下发。

        - 只导出 exists=True 的对象；显式目录（is_dir）同样进快照
        - 哈希未完成的文件暂不导出（不作为同步对象/拉取源）
        - 传输临时文件不入快照
        """
        if self.distributor is None:
            return []
        out = []
        for st in self.distributor.states().values():
            if not st.exists:
                continue
            if st.hash_state == 'pending' \
                    and self.distributor.hash_gate_active():
                continue  # 阶段 B：哈希未完成，不作为同步对象下发给对端
            if self._is_transient_temp(st.name):
                continue  # 传输临时文件不入快照（防对端当作缺失文件来回拉取）
            if not st.file_id:
                continue  # 无身份对象不参与同步
            if st.is_dir:
                # 显式目录身份进快照：空目录没有字节，只能靠状态对齐
                out.append(st.to_wire_dict())
                continue
            try:
                if not os.path.isfile(self._safe_join(st.name)):
                    continue
            except ValueError:
                continue
            out.append(st.to_wire_dict())
        return out

    def _pending_count(self) -> int:
        """仍待哈希的活对象数（文件）；非 0 时快照缺失不可作为删除确认。"""
        if self.distributor is None:
            return 0
        n = 0
        for st in self.distributor.states().values():
            if st.exists and st.hash_state == 'pending' \
                    and not self._is_transient_temp(st.name):
                n += 1
        return n

    # ---- 收到 FILE_STATE_REQ：回 RESP ----

    def handle_state_req(self, end_id: str, content: dict = None):
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
                        self.end_id, [], session=None, scan_complete=False))
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
            with self._lock:
                aligning = self._align_mode
            self.mesh.send_to_peer(
                end_id, Protocol.create_file_state_resp(
                    self.end_id, self.snapshot(), session=session,
                    scan_complete=not aligning,
                    pending_count=self._pending_count()))
        except Exception as e:
            self.log_message.emit(f"文件状态响应失败: {e}")

    # ---- 收到 FILE_STATE_RESP：对比 + 拉取队列 + 补收删除 + 清理 ----

    def handle_state_resp(self, end_id: str, content: dict):
        """收到对端文件状态响应：对比本端列表并补齐差异。"""
        with self._lock:
            if self._round_targets is not None:
                self._round_responded.add(end_id)
        if not isinstance(content, dict) or self.distributor is None:
            self._finish_round(end_id, False)
            return
        entries = content.get('entries') or []
        src_id = content.get('src_id') or end_id
        session = content.get('session')
        # 阶段 D：静默对齐期间只累计对端状态，轮到收口时统一裁决
        with self._lock:
            if self._align_mode:
                self._align_got_response = True
                if (not content.get('scan_complete', True)
                        or int(content.get('pending_count') or 0) > 0):
                    self._align_incomplete = True
                if session is not None:
                    self._align_sessions[src_id] = session
                for d in entries:
                    if not isinstance(d, dict):
                        continue
                    name = d.get('name', '')
                    if name not in self._align_residues:
                        continue
                    remote = FileState.from_wire_dict(src_id, d)
                    if (remote.hash_state == 'ok' and remote.hash
                            and not remote.is_dir):
                        self._align_remote_by_hash.setdefault(
                            remote.hash, []).append(remote)
                    self._align_remote[name] = remote
                    self._align_sources[name] = src_id
                    if remote.hash_state != 'ok' or not remote.hash:
                        self._align_pending.add(name)
                    else:
                        self._align_pending.discard(name)
        has_diff = False
        exists_claim = set()  # 本响应端声明「存在」的路径（在线闸门判据）
        remote_keys = set()   # 对端活对象 (path, id) 集合（删除确认判据）
        skip_names = set()    # 同 ID 已由本端裁决，旧路径不再按路径补拉
        # 同 ID 预扫描：同一身份在两端路径不同 = 改名。先按 ID 裁决路径归属，
        # 再按路径补缺，避免把对端旧路径当缺失文件重复拉取（文档 §5 阶段 3）。
        by_id = {}
        for d in entries:
            if not isinstance(d, dict):
                continue
            remote = FileState.from_wire_dict(src_id, d)
            if not remote.name or not remote.file_id:
                continue
            by_id.setdefault(remote.file_id, []).append(remote)
        for fid, group in by_id.items():
            local = self.distributor.get_state_by_id(fid)
            if local is None or not local.exists:
                continue
            rep = max(group, key=lambda r: (int(r.clock or 0),
                                            float(r.ts or 0.0)))
            if rep.name == local.name:
                continue
            action = self.distributor.reconcile_identity_path(rep, src_id)
            if action == 'adopt':
                has_diff = True
                # 改名发生在对端离线期间时不会再有分发信号到达，这里只能靠
                # 快照收敛改名；补一次「已改名」通知，否则列表仍显示旧名，
                # 直到手动刷新。
                self.distributor._notify_applied({
                    'src_id': src_id, 'op': self.distributor.OP_RENAME,
                    'file': rep.name, 'old': local.name,
                    'is_dir': bool(rep.is_dir)})
                moved = self.distributor.get_state_by_id(fid)
                if (moved is not None and session is not None
                        and rep.hash_state == 'ok' and rep.hash
                        and moved.hash_state == 'ok' and moved.hash
                        and moved.hash != rep.hash):
                    if self._enqueue_pull(moved.name, src_id, rep, session,
                                          force=True):
                        has_diff = True
            # 对端该身份的所有其它路径一律不按路径处理（旧名或新名皆然）
            skip_names |= {r.name for r in group if r.name != local.name}
        for d in entries:
            if not isinstance(d, dict):
                continue
            name = d.get('name', '')
            if not name:
                continue
            remote = FileState.from_wire_dict(src_id, d)
            # C3：仅用对端 vv 抬本端计数器地板，不并入状态表 vv
            raw_vv = d.get('vv')
            if isinstance(raw_vv, dict):
                try:
                    base = int(raw_vv.get(self.end_id, 0) or 0)
                except (TypeError, ValueError):
                    base = 0
                if base > 0:
                    self.distributor.recover_floor(
                        remote.file_id, base, remote.clock)
            if not remote.file_id:
                # 无 ID 的快照条目（旧版本端）无法映射到唯一身份，直接忽略
                continue
            if name in skip_names:
                continue
            remote_keys.add((name, remote.file_id or ''))
            if remote.is_dir:
                # 目录身份：没有字节可拉，只在缺失时建目录 + 采纳身份
                exists_claim.add(name)
                if self._adopt_dir_entry(name, src_id, remote):
                    has_diff = True
                continue
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
            if pend and session is not None and remote.hash_state == 'ok' \
                    and remote.hash:
                # 冲突覆盖：仲裁已判 src_id 胜，force 覆盖版本槽（无视列表内旧槽）
                if not self._tombstone_blocks(name, remote.file_id, remote) \
                        and self._enqueue_pull(name, src_id, remote, session,
                                               force=True):
                    has_diff = True
            exists_claim.add(name)  # 对端仍持有该路径 → 保留本端墓碑（在线闸门）
            # 对端有该文件：本端缺失 → 入拉取队列（同文件排队去重）
            if not local_exists:
                # 同身份已在本端另一路径存活：路径归属由 ID 裁决，不重复拉取
                fid = remote.file_id or ''
                other = (self.distributor.get_state_by_id(fid)
                         if fid else None)
                if other is not None and other.exists and other.name != name:
                    continue
                if self._tombstone_blocks(name, remote.file_id, remote):
                    self.log_message.emit(f"忽略拉取 {name}: 本端已有该 ID 墓碑")
                    continue
                # 仅从哈希已算好的源补拉（待哈希条目不提供可信内容）
                if remote.hash_state == 'ok' and remote.hash \
                        and self._enqueue_pull(name, src_id, remote, session):
                    has_diff = True
            # 本端已有：冲突收敛（阶段 3）——远端胜出或本端为未验证副本才拉取
            else:
                local_st = self.distributor.get_state(name)
                # 同路径不同 ID：clock/ts 高者占原路径，落败方加后缀保留
                if (local_st is not None and local_st.exists
                        and local_st.file_id and remote.file_id
                        and local_st.file_id != remote.file_id):
                    target = self.distributor._claim_path(
                        remote.file_id, name, remote.clock, remote.ts)
                    if target != name:
                        continue  # 本端占位者版本更高：等对端加后缀
                    # 本端占位者已让位 → 该路径缺失，按远端补拉
                    if (session is not None and remote.hash_state == 'ok'
                            and remote.hash
                            and not self._tombstone_blocks(
                                name, remote.file_id, remote)
                            and self._enqueue_pull(name, src_id, remote,
                                                   session)):
                        has_diff = True
                    continue
                if (local_st is not None and session is not None
                        and remote.hash_state == 'ok' and remote.hash):
                    # §5.2 同 path 同 hash 不同 ID：先合并身份（静默幂等），
                    # 否则 ADD 信号可能被 vv 去重跳过，两端 ID 长期分叉，
                    # 后续删除信号会被“精确 ID 匹配”误判为过期而无法收敛。
                    if (local_st.exists and local_st.hash_state == 'ok'
                            and local_st.hash
                            and local_st.hash == remote.hash
                            and local_st.file_id and remote.file_id
                            and local_st.file_id != remote.file_id
                            and not self.distributor.has_tombstone(
                                name, remote.file_id)):
                        if self.distributor.merge_remote_identity(name, remote):
                            has_diff = True
                        local_st = self.distributor.get_state(name)
                    cmp = compare_states(remote, local_st, src_id, self.end_id)
                    # 同版本但本端 hash 未验证（无内容证据）→ 以对端为准补拉
                    # （计划书 §6：未验证副本一律按对端内容校正，防元数据先到、
                    #  字节未到后信号被去重而永久分叉）
                    if (cmp == 1 or (cmp == 0
                                     and local_st.hash_state != 'ok')) \
                            and (not local_st.hash
                                 or local_st.hash != remote.hash) \
                            and not self._tombstone_blocks(
                                name, remote.file_id, remote) \
                            and self._enqueue_pull(name, src_id, remote, session):
                        has_diff = True
        # 删除确认：仅在快照完整（扫描完成且无待哈希）时，缺失的 id 才算已删
        if bool(content.get('scan_complete')) \
                and int(content.get('pending_count') or 0) == 0:
            for t in self.distributor.wire_tombstones():
                key = (t.get('rel_path', ''), t.get('file_id') or '')
                if key[0] and key not in remote_keys:
                    self.distributor.confirm_tombstone(key[0], key[1], src_id)
        with self._lock:
            if self._round_targets is not None:
                self._round_exists_claim |= exists_claim
                self._round_deleted_claim |= set()
                self._round_remote_keys |= remote_keys
        self._finish_round(end_id, has_diff)

    def _slot_from_remote(self, name: str, src_id: str, remote: FileState,
                          session) -> dict:
        """由远端状态构造拉取槽（目标身份 + 哈希 + 大小 + 自同步会话）。"""
        return {
            'name': name,
            'end_id': src_id,
            'op_no': remote.op_no,
            'clock': remote.clock,
            'ts': remote.ts,
            'id': remote.file_id,
            'hash': remote.hash,
            'size': int(remote.size or 0),
            'session': dict(session) if isinstance(session, dict) else None,
        }

    @staticmethod
    def _slot_key(slot: dict) -> tuple:
        """拉取目标身份：id + hash + size（C3 去重口径）。"""
        return (slot.get('id') or '', slot.get('hash') or '',
                int(slot.get('size') or 0))

    def _merge_slot_locked(self, cand: dict) -> bool:
        """候选槽并入拉取队列（调用方须持 _cond）：每文件仅一个槽。

        同一目标（id+hash+size 相同）不重复入队；不同目标按 (clock, ts)
        择新者，避免迟到败者顶掉已入队的胜者版本。
        """
        name = cand['name']
        prev = self._pulls.get(name)
        if prev is not None:
            if self._slot_key(prev) == self._slot_key(cand):
                return False  # 同一对象同一内容：已在队，去重
            if (int(prev.get('clock', 0) or 0), float(prev.get('ts', 0.0) or 0.0)) \
                    >= (int(cand.get('clock', 0) or 0),
                        float(cand.get('ts', 0.0) or 0.0)):
                return False
        self._pulls[name] = cand
        # notify_all：一轮对比可能一次入队多个文件，多个等待 worker 可同时取件
        self._cond.notify_all()
        return True

    def _enqueue_pull(self, name: str, src_id: str, remote: FileState, session,
                      force: bool = False) -> bool:
        """拉取排队（版本感知槽）：每文件只保留一个当前仲裁胜者版本槽。

        - 无槽 → 建槽。
        - 有槽 → 用三层裁决比较现有槽 vs 新候选：新版本胜则覆盖槽
          （保留更高版本，旧字节不入队、不占带宽）；现有槽不旧于新候选则
          抛弃新候选（当前槽已是更优版本）。force 亦同：候选胜才覆盖——
          冲突覆盖（仲裁已判 src_id 胜）的迟到败者版本不允许顶掉已入队的
          胜者版本（防覆盖拉取通道竞态）。
        - 在途（worker 正拉取该文件）→ 候选记入 _pull_deferred 待办，
          worker 收尾时并入队列重判（不遗忘）。

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
        # 阶段 B 门禁：本端该路径仍 pending（哈希未完成）→ 不参与同步，跳过拉取
        if self.distributor is not None \
                and self.distributor.is_hash_pending(name):
            self.log_message.emit(f"跳过拉取 {name}: 本地哈希未完成")
            return False
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
                # 该文件正被某 worker 拉取中：不能建第二个槽、不能开第二条并发接收
                # （避免同一文件两份临时文件同时写盘、字节翻倍撑爆磁盘），但候选也
                # 不能丢弃——丢弃即遗忘，只能等下一拍对账才补发（上限
                # RECONCILE_INTERVAL），收敛延迟被对账周期支配。改为记入待办，
                # worker 收尾时立即并入队列重判。多个候选只保留更优者。
                cand = self._slot_from_remote(name, src_id, remote, session)
                held = self._pull_deferred.get(name)
                if held is None or (
                        int(cand.get('clock', 0) or 0),
                        float(cand.get('ts', 0.0) or 0.0)) > (
                        int(held.get('clock', 0) or 0),
                        float(held.get('ts', 0.0) or 0.0)):
                    self._pull_deferred[name] = cand  # 候选更新 → 替换待办
                return False
            return self._merge_slot_locked(
                self._slot_from_remote(name, src_id, remote, session))

    def _adopt_dir_entry(self, name: str, src_id: str, remote) -> bool:
        """采纳对端快照里的目录身份：缺失则建目录 + 登记，冲突则合并/让位。

        目录没有字节可拉，因此这里只做「路径 + 身份」收敛；返回 True 表示本端
        因此发生了变更（供 sync_done 判断）。
        """
        if self.distributor is None or not remote.file_id:
            return False
        if self._tombstone_blocks(name, remote.file_id, remote):
            return False
        try:
            path = self._safe_join(name)
        except ValueError:
            return False
        local = self.distributor.get_state(name)
        if (local is not None and local.exists and local.file_id
                and local.file_id != remote.file_id):
            if local.is_dir:
                # 同路径两个目录身份：高版本存活，同版本按 ID 字典序决胜
                local_key = (local.clock, local.ts, local.file_id)
                remote_key = (remote.clock, remote.ts, remote.file_id)
                if local_key >= remote_key:
                    return False
                self.distributor.record_merge_tombstone(name, local.file_id)
                self.distributor._drop_identity(local.file_id)
            else:
                target = self.distributor._claim_path(
                    remote.file_id, name, remote.clock, remote.ts)
                if target == name:
                    return False  # 文件占位者版本更高：等信号侧改名
                remote.name = target
                name = target
                try:
                    path = self._safe_join(name)
                except ValueError:
                    return False
        # 已对齐（同名、同 ID、本端已有目录身份且磁盘目录在）：直接视为无差异。
        # 目录没有字节可拉，若每轮都重新 adopt 并返回 True，周期对账会永久
        # has_diff → 持续打印「正在补齐差异项」却无任何传输（空转刷日志/刷 UI）。
        if os.path.isdir(path):
            local = self.distributor.get_state(name)
            if (local is not None and local.exists and local.is_dir
                    and local.file_id and local.file_id == remote.file_id):
                return False
            self.distributor.adopt_remote_state(remote)
            return True
        if os.path.exists(path):
            return False
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            self.log_message.emit(f"创建目录失败: {name}: {e}")
            return False
        self.distributor.adopt_remote_state(remote)
        # 目录没有字节可拉，也就没有文件落盘回调；这里补一次「目录已创建」的
        # 信号应用通知，否则快照对账补齐的空目录不会刷新到文件列表（回归端
        # 补齐后看不到空目录）。
        self.distributor._notify_applied({
            'src_id': src_id, 'op': self.distributor.OP_DIR_CREATE,
            'file': name, 'is_dir': True})
        return True

    def _tombstone_blocks(self, name: str, file_id: str,
                          remote: FileState = None) -> bool:
        """仅「同路径 + 同 ID」的墓碑阻挡同步；更高版本可复活。

        目录墓碑与文件墓碑同构（带 file_id）：只为压制同一身份，不再按路径
        拦截同名重建。
        """
        if self.distributor is None or not file_id:
            return False
        tomb = self.distributor.get_tombstone(name, file_id)
        if tomb is None:
            return False
        if remote is not None and (int(remote.clock or 0),
                                   float(remote.ts or 0.0)) > (
                int(tomb.get('clock') or 0), float(tomb.get('ts') or 0.0)):
            return False  # 更高版本回到该路径 → 允许复活
        st = self.distributor.get_state_by_id(file_id)
        if st is not None and st.exists and st.name == name:
            return False  # 同 ID 已在该路径以活状态回来
        return True

    def emit_local_missing(self, exclude_dirs: Optional[set] = None) -> int:
        """本地补扫（建连时/切回同步时兜底）：扫描同步文件夹，对磁盘存在但
        状态表缺失的文件 emit add，纳入同步体系。

        只扫文件（目录随文件写入自动创建）与「磁盘空目录」（空目录只能靠显式
        目录身份同步）。覆盖三类场景：① mesh 未就绪窗口内添加文件（信号未发出、
        状态未记录）——建连后补扫兜底；② 启动前已放置于文件夹的文件（状态表
        无条目）；③ 收集模式期间主机对根目录的本地增删被「收集不转发」跳过、
        切回同步后由 _complete_mode_switch 调用本函数补扫广播（此时连接端已
        在线，不会经过 _on_mesh_peer_connected）。状态表已有条目（含已删除条目）
        跳过，不复活已删文件。只读端不发起任何修改信号（返回 0，仅单向下拉）。

        Args:
            exclude_dirs: 需从扫描中剪枝的顶层目录名集合（收集模式 IP 文件夹）。

        Returns:
            新 emit 的文件数
        """
        if self.distributor is None or self._readonly:
            return 0
        with self._lock:
            if self._align_mode:
                # 静默对齐期间残留不得当作新增广播（§8）
                return 0
        if exclude_dirs is None:
            exclude_dirs = set()
        count = 0
        try:
            for root, dirs, files in os.walk(self.sync_folder):
                dirs[:] = [d for d in dirs
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
                    if (self.distributor.get_state(rel) is not None
                            or self.distributor.tombstone_for_path(rel)):
                        # 状态表已有条目（存在/已删）或墓碑压制（删除残留）→ 不复活
                        continue
                    self.distributor.emit('add', rel)
                    count += 1
                for dn in dirs:
                    full_d = os.path.join(root, dn)
                    try:
                        if os.listdir(full_d):
                            continue  # 非空目录是隐式父目录，不建身份
                        rel_d = os.path.relpath(
                            full_d, self.sync_folder).replace('\\', '/')
                        self._safe_join(rel_d)
                    except (OSError, ValueError):
                        continue
                    if (self.distributor.get_state(rel_d) is not None
                            or self.distributor.tombstone_for_path(rel_d)):
                        continue
                    if self.distributor.emit('dir_create', rel_d) is not None:
                        count += 1
        except OSError:
            pass
        if count:
            self.log_message.emit(f"本地补扫: 新发现 {count} 个条目入同步")
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
                        # 异常退出未必已上报结束（如落盘后统计/回调抛错），补一次
                        # 失败结束，保证接收侧状态（_sync_active_recvs）不残留
                        self.pull_done.emit(item['name'], False)
            finally:
                # 拉取结束（成功/失败/未获锁放弃）：解除在途标记，后续轮次可再补拉。
                # 用 _cond 与 _enqueue_pull 的读侧同步；acquired 在 try 内初始化，
                # 未进入循环（_stop 置位）时保持 False，finally 不重复释放锁。
                with self._cond:
                    self._in_flight.discard(item['name'])
                    # 在途期间被缓冲的候选立即并入队列重判：不等下一拍对账，
                    # 收敛延迟不再被对账周期支配（并入不成立即抛弃，语义同前）。
                    deferred = self._pull_deferred.pop(item['name'], None)
                    if deferred is not None:
                        self._merge_slot_locked(deferred)
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

    def _slot_stale(self, name: str, item: dict) -> bool:
        """槽是否已过时：本端状态版本严格新于槽目标版本（计划书 §9.2）。

        内容证据（hash）无法识别「源端真实持有旧内容」的迟到槽——旧槽的
        hash 与源端字节相符，落盘会回退本端已推进的胜者字节，且会被
        emit_pulled 以胜者版本钳制，污染状态表 hash。故以 (clock, ts) 版本
        为准；同版本槽（含本端 hash 未定）不判过时，交由落盘后 hash 校验兜底。
        """
        st = None
        if self.distributor:
            fid = item.get('id') or ''
            st = self.distributor.get_state_by_id(fid) if fid else None
            if st is None:
                st = self.distributor.get_state(name)
        if st is None or not st.exists:
            return False
        return (int(st.clock or 0), float(st.ts or 0.0)) > (
            int(item.get('clock', 0) or 0), float(item.get('ts', 0.0) or 0.0))

    def _do_pull(self, item: dict):
        name = item['name']
        session = item.get('session')
        if not session:
            self.log_message.emit(f"拉取 {name}: 对端未提供会话")
            return
        # 删除优先（IO 前）：本端已有该 ID 墓碑、或其祖先目录已删除 → 不落盘
        cand = FileState(
            name=name, op_no=int(item['op_no'] or 0), state=STATE_ADD,
            exists=True, clock=int(item.get('clock', 0) or 0),
            ts=float(item.get('ts', 0.0) or 0.0),
            vv={item['end_id']: int(item['op_no'] or 0)},
            file_id=item.get('id') or '')
        other = (self.distributor.get_state_by_id(cand.file_id)
                 if cand.file_id else None)
        if other is not None and other.exists and other.name != name:
            # 同身份已在本端另一路径存活：路径归属由快照 ID 裁决，不重复拉取
            self.log_message.emit(
                f"拉取 {name} 跳过: 同身份已在本端 {other.name}")
            self.pull_done.emit(name, False)
            return
        if self._tombstone_blocks(name, cand.file_id, cand):
            self.log_message.emit(f"拉取 {name} 跳过: 本端已删除")
            self.pull_done.emit(name, False)
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
        # 去重（IO 前）：本端已持有该内容（id+hash+size 全部一致才跳过）
        st = self.distributor.get_state(name) if self.distributor else None
        if (st is not None and st.exists and st.hash_state == 'ok'
                and st.hash and st.hash == (item.get('hash') or '')
                and int(st.size or 0) == int(item.get('size') or 0)
                and os.path.exists(dest)):
            self.log_message.emit(f"拉取 {name} 跳过: 本端已持有该内容")
            return
        # 过时槽（IO 前）：胜者内容已定且与槽目标不同 → 该槽会回退胜者字节
        if self._slot_stale(name, item):
            self.log_message.emit(f"拉取 {name} 已过时，抛弃该槽")
            return
        os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
        ok, _received, err = pull_file(
            host, port, session_id, token, name, dest,
            msg_type=MessageType.SYNC_PULL_REQ,
            # 落盘前复核：拉取在途期间本端胜者内容若已确定且不同，放弃落盘
            pre_replace=lambda: self._slot_stale(name, item),
            # 把本 store 的停止事件透传给拉取：退出房间/整体停止时设置 _stop，
            # pull_file 会在下一次 recv 超时(≤1s) 感知并取消，释放 .part 句柄并删除
            # 临时文件——否则工作线程会一直占着句柄（直到整体超时或对端关连接），
            # 导致传输中的临时文件死锁、退出房间无法删除。
            stop_event=self._stop,
            progress_cb=lambda recv, size: self.pull_progress.emit(name, recv, size),
            overall_timeout=self.PULL_OVERALL_TIMEOUT)
        if ok:
            # 删除优先（IO 后）：拉取期间本端已删除 → 移除复活字节；
            # 胜者内容已定且与槽目标不同 → 移除刚落的过时字节
            if self._tombstone_blocks(name, cand.file_id, cand) \
                    or self._slot_stale(name, item):
                self.log_message.emit(
                    f"拉取 {name} 落盘，但本地已删除/已更新，移除该字节")
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.pull_done.emit(name, False)
                return
            # 同身份已改名到另一路径（传输期间收到改名信号）：落盘字节留在
            # 旧名下会成为无身份新文件，必须移除，由新路径拉取负责内容。
            other_now = (self.distributor.get_state_by_id(cand.file_id)
                         if cand.file_id else None)
            if other_now is not None and other_now.exists \
                    and other_now.name != name:
                self.log_message.emit(
                    f"拉取 {name} 落盘，但同身份已改名到 "
                    f"{other_now.name}，移除该字节")
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.pull_done.emit(name, False)
                return
            # 落盘校验：MD5+size 与目标身份一致才承认（不匹配则丢弃并重试）
            want_hash = item.get('hash') or ''
            want_size = int(item.get('size') or 0)
            try:
                got_hash, got_size = FileHasher.hash_file(dest)
            except OSError as e:
                self.log_message.emit(f"拉取 {name} 校验失败: {e}")
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.pull_done.emit(name, False)
                return
            if (want_hash and got_hash != want_hash) \
                    or (want_size and got_size != want_size):
                self.log_message.emit(
                    f"拉取 {name} 字节校验不符，丢弃并重试")
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.pull_done.emit(name, False)
                # §9：校验不符同样计入重试上限（默认 3 次），超过转冷却，
                # 防止源端持续提供错误字节时每轮对账无限重拉
                self._record_pull_cooldown(name, item['end_id'], item['op_no'])
                try:
                    self.request_all()
                except Exception:
                    pass
                return
            src_clock = int(item.get('clock', 0) or 0)
            src_ts = float(item.get('ts', 0.0) or 0.0)
            self.distributor.emit_pulled(name, item['end_id'], item['op_no'],
                                         src_clock, src_ts,
                                         on_done=lambda ok: self._finish_pull(name, ok),
                                         file_hash=want_hash,
                                         file_size=want_size,
                                         remote_id=item.get('id') or '')
        else:
            self.pull_done.emit(name, False)
            # 落盘前已判过时 → 本份未触碰目标文件，胜者字节完好，属正常放弃而非失败：
            # 不计冷却、不刷失败日志；胜者版本字节由既有冲突拉取链路与常态对账负责。
            if err == PULL_STALE_DISCARD_MSG:
                self.log_message.emit(f"拉取 {name} 落盘前已过时，放弃覆盖")
                return
            # 目标盘空间不足：立即进入冷却，避免「拉到一半失败→重拉」无限循环
            if str(err).startswith(PULL_NO_SPACE_PREFIX):
                self._record_pull_cooldown(name, item['end_id'], item['op_no'],
                                           immediate=True)
                self.log_message.emit(f"拉取 {name} 暂停: {err}；清理目标磁盘后自动恢复")
                return
            # 阶段 5：源端明确「没有此文件」→ 记录冷却（连续多次后对账轮暂停重复拉取）
            if self._source_lacks_file(err):
                self._record_pull_cooldown(name, item['end_id'], item['op_no'])
            self.log_message.emit(f"自同步拉取失败 {name}: {err}")

    def _finish_pull(self, name: str, ok: bool):
        """拉取落盘后的收尾：哈希完成才算完成（阶段 B）。"""
        if not ok:
            self.pull_done.emit(name, False)
            return
        self._notify_file_added(name)
        self.pull_done.emit(name, True)
        self.log_message.emit(f"自同步拉取完成: {name}")

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

    def _record_pull_cooldown(self, name: str, src_id: str, op_no, immediate: bool = False):
        """记录一次拉取失败；连续多次或 immediate 时进入冷却。

        __lock__ 保护。冷却完成后（gate 侧触碰到期条目会移除）下次重试若仍失败
        再累计，故内存仅保留「正在冷却」的条目，不会无限增长。
        """
        with self._lock:
            recs = self._pull_cooldowns.setdefault(name, {})
            rec = recs.setdefault(src_id, {
                'fails': 0, 'cooldown_until': 0.0, 'last_op_no': 0})
            rec['last_op_no'] = max(rec.get('last_op_no', 0), int(op_no or 0))
            rec['fails'] = rec.get('fails', 0) + 1
            if immediate or rec['fails'] >= self.PULL_COOLDOWN_FAILS:
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
            # 补删磁盘残留：墓碑已记但磁盘删除曾失败（在途句柄占用），每拍重试
            try:
                if self.distributor is not None:
                    self.distributor.retry_pending_deletes()
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

    # ---- 墓碑对账（C3：连接建立后先拉墓碑，再拉文件状态） ----

    TOMBSTONE_BATCH = 200  # 单次 TOMBSTONE_RESP 最大条目数（分页上限）

    def start_peer_sync(self, end_id: str):
        """对端建立连接后启动对账：先分页拉取墓碑，完成后再发 FILE_STATE_REQ。"""
        if self.mesh is None or not end_id:
            return
        with self._lock:
            self._tombstone_pending.add(end_id)
        try:
            self._send_tombstone_req(end_id, 0)
        except Exception as e:
            with self._lock:
                self._tombstone_pending.discard(end_id)
            self.log_message.emit(f"墓碑拉取失败: {e}")
            self.request_all()

    def _send_tombstone_req(self, end_id: str, offset: int):
        self.mesh.send_to_peer(
            end_id,
            Protocol.create_tombstone_req(self.end_id, '', offset))

    def handle_tombstone_req(self, end_id: str, content: dict):
        """应答对端墓碑拉取：只下发 origin=delete，按 (path, id) 排序分页。"""
        if self.mesh is None or self.distributor is None:
            return
        try:
            offset = int((content or {}).get('offset') or 0)
        except (TypeError, ValueError):
            offset = 0
        recs = sorted(self.distributor.wire_tombstones(),
                      key=lambda r: (r.get('rel_path', ''),
                                     r.get('file_id') or ''))
        page = recs[max(0, offset):max(0, offset) + self.TOMBSTONE_BATCH]
        entries = [{
            'path': r.get('rel_path', ''),
            'id': r.get('file_id') or '',
            'is_dir': bool(r.get('is_dir')),
            'op_no': int(r.get('op_no') or 0),
            'clock': int(r.get('clock') or 0),
            'ts': float(r.get('ts') or 0.0),
            'vv': dict(r.get('vv') or {}),
        } for r in page]
        more = max(0, offset) + len(page) < len(recs)
        try:
            self.mesh.send_to_peer(
                end_id,
                Protocol.create_tombstone_resp(
                    self.end_id, entries, more=more,
                    next_offset=max(0, offset) + len(page)))
        except Exception as e:
            self.log_message.emit(f"墓碑响应失败: {e}")

    def handle_tombstone_resp(self, end_id: str, content: dict):
        """应用对端墓碑并回写 confirmed_by；全部分页拉完后才发起文件状态对账。"""
        if not isinstance(content, dict) or self.distributor is None:
            with self._lock:
                self._tombstone_pending.discard(end_id)
            self.request_all()
            return
        for d in content.get('entries') or []:
            if not isinstance(d, dict):
                continue
            path = d.get('path', '')
            if not path:
                continue
            fid = d.get('id') or ''
            removed = self.distributor.apply_tombstone(
                path, fid, is_dir=bool(d.get('is_dir')), origin='delete',
                op_no=int(d.get('op_no') or 0),
                clock=int(d.get('clock') or 0),
                ts=float(d.get('ts') or 0.0),
                vv=dict(d.get('vv') or {}))
            if removed:
                # 离线期间发生的删除，回归/重连后只能靠墓碑补齐，不会再有分发
                # 信号到达；不通知则已删文件仍占着列表，需手动刷新。
                self.distributor._notify_applied({
                    'src_id': end_id, 'op': self.distributor.OP_DELETE,
                    'file': path, 'is_dir': bool(d.get('is_dir'))})
            # 对端持有该墓碑即证明其已完成删除 → 记入本端墓碑 confirmed_by
            self.distributor.confirm_tombstone(path, fid, end_id)
        if bool(content.get('more')):
            try:
                self._send_tombstone_req(
                    end_id, int(content.get('next_offset') or 0))
            except Exception as e:
                self.log_message.emit(f"墓碑续页失败: {e}")
                with self._lock:
                    self._tombstone_pending.discard(end_id)
                self.request_all()
            return
        with self._lock:
            self._tombstone_pending.discard(end_id)
        self.log_message.emit("墓碑对账完成，开始文件状态对账")
        self.request_all()

    def request_all(self) -> int:
        """向全部直连对端请求文件状态（手动同步/断线重连/周期对账自动补齐）。

        Returns:
            成功下发请求的对端数
        """
        if self.mesh is None:
            return 0
        targets = self.mesh.connected_end_ids()
        with self._lock:
            # 墓碑未拉完的端不参与文件状态对账（顺序固定：先墓碑后文件）
            targets = [e for e in targets
                       if e not in self._tombstone_pending]
        if not targets:
            return 0
        with self._lock:
            self._round_gen += 1
            gen = self._round_gen
            self._round_targets = set(targets)
            self._round_has_diff = False
            self._round_exists_claim = set()
            self._round_deleted_claim = set()
            self._round_responded = set()
            self._round_remote_keys = set()
        for eid in targets:
            try:
                self.mesh.send_to_peer(
                    eid, Protocol.create_file_state_req(self.end_id))
            except Exception:
                with self._lock:
                    self._round_targets.discard(eid)
        # 超时兜底：对端不应答（离线/未连）时也结束本轮，避免通知悬挂。
        # 携带本轮代数 gen：新轮已开启（_round_gen != gen）则旧轮让位，不误杀新轮
        threading.Thread(target=self._round_timeout, args=(gen,), daemon=True).start()
        return len(targets)

    def _round_timeout(self, gen: int):
        time.sleep(self.ROUND_TIMEOUT)
        with self._lock:
            # 已开启新轮（_round_gen != gen）：旧轮让位，不碰新轮聚合，避免误杀
            if self._round_gen != gen or self._round_targets is None:
                return
            had = self._round_has_diff
            self._round_targets = None
            self._round_has_diff = False
            self._round_exists_claim = set()
            self._round_deleted_claim = set()
            self._round_responded = set()
            self._round_remote_keys = set()
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
            # 本轮已收口（所有直连端应答）→ 执行在线闸门删除收敛
            try:
                self._cleanup_converged()
            except Exception:
                pass
            # 墓碑 GC：所有已知端均已确认且无同 id 活对象的删除墓碑
            try:
                self.distributor.gc_tombstones()
            except Exception:
                pass
            # 阶段 D：一轮应答收口后裁决静默对齐残留
            try:
                self._finalize_alignment()
            except Exception:
                pass
            self._notify_sync_done(had)

    def _finalize_alignment(self, offline: bool = False):
        """§8：按本轮对端状态处置残留（绑定/删除拉取/发布/墓碑压制）。"""
        with self._lock:
            if not self._align_mode:
                return
            residues = dict(self._align_residues)
            remote_map = dict(self._align_remote)
            remote_by_hash = {h: list(v)
                              for h, v in self._align_remote_by_hash.items()}
            pending_paths = set(self._align_pending)
            sessions = dict(self._align_sessions)
            sources = dict(self._align_sources)
            got_response = self._align_got_response
            incomplete = self._align_incomplete
        if offline and got_response:
            return
        keep = {}
        dropped = 0
        dropped_names = []
        for name, rec in residues.items():
            abs_path = self._safe_join(name)
            if not os.path.exists(abs_path):
                # 残留对象已不在盘上（对齐期被删/移走）：作废，落终态
                dropped += 1
                continue
            if not rec.get('hash'):
                if rec.get('failed'):
                    # 哈希重试耗尽：放弃静默对齐，交由结束后的正常新增链路
                    dropped += 1
                    dropped_names.append(name)
                    continue
                keep[name] = rec
                continue
            if name in pending_paths:
                keep[name] = rec
                continue
            remote = remote_map.get(name)
            if remote is not None:
                if remote.is_dir:
                    try:
                        os.remove(abs_path)
                    except OSError:
                        keep[name] = rec
                    continue
                if (int(remote.size or 0) == int(rec['size'])
                        and remote.hash == rec['hash']):
                    # 同 path 同 hash：丢弃本地临时 ID，绑定对端身份，静默收敛
                    try:
                        self.distributor.adopt_remote_state(remote)
                    except Exception:
                        keep[name] = rec
                    continue
                # 同 path 异 hash：以房间为准，删残留并从对端拉取
                try:
                    os.remove(abs_path)
                except OSError:
                    keep[name] = rec
                    continue
                session = sessions.get(sources.get(name, ''))
                if session is not None and remote.hash:
                    self._enqueue_pull(name, sources.get(name, ''),
                                       remote, session)
                continue
            # 对端无该 path：对端扫描未完整时不能据缺席判新，留待下一轮
            if incomplete:
                keep[name] = rec
                continue
            # 同内容异名 = 对端改名：hash 在远端唯一时本地静默改名并绑定身份
            cands = [r for r in remote_by_hash.get(rec['hash'], [])
                     if not r.is_dir and r.name != name]
            if len(cands) == 1:
                try:
                    dest = self._safe_join(cands[0].name)
                    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
                    os.replace(abs_path, dest)
                    self.distributor.adopt_remote_state(cands[0])
                    continue
                except (OSError, ValueError):
                    pass
            # 删除必须身份对应：只有墓碑 ID 与残留对象相同时才删。不同 ID 说明
            # 是另一个对象（例如用户选择保留带入的本地文件），按路径删会误删。
            if self.distributor.has_tombstone(name, rec['file_id']):
                try:
                    os.remove(abs_path)
                except OSError:
                    keep[name] = rec
                continue
            try:
                self.distributor.publish_residue(
                    name, rec['file_id'], rec['hash'], rec['size'])
            except Exception:
                keep[name] = rec
        with self._lock:
            if not self._align_mode:
                return
            self._align_residues = keep
            self._align_remote = {}
            self._align_remote_by_hash = {}
            self._align_sources = {}
            self._align_got_response = False
            self._align_incomplete = False
            self._align_pending = {p for p in pending_paths if p in keep}
            done = not keep
            if done:
                self._align_mode = False
                timer = self._align_timer
                self._align_timer = None
                hasher = self._align_hasher
                self._align_hasher = None
        if dropped:
            self.log_message.emit(
                f"静默对齐放弃 {dropped} 个残留（已删除或哈希失败）")
        # 哈希失败的残留立即按本端新增登记，交正常哈希链路接管：
        # emit_local_missing 只在建连/模式切换时触发，不保证下一拍会来。
        if self.distributor is not None:
            for name in dropped_names:
                try:
                    self.distributor._register_new_local(name)
                except Exception:
                    pass
        if done:
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
            if hasher is not None:
                try:
                    hasher.stop()
                except Exception:
                    pass
        elif offline and not got_response:
            # 离线兜底时仍有残留（如大文件哈希未完成）：续一个超时再判
            timer = threading.Timer(self.ROUND_TIMEOUT,
                                    self.finalize_offline_alignment)
            timer.daemon = True
            with self._lock:
                if self._align_mode:
                    self._align_timer = timer
            timer.start()

    def _cleanup_converged(self):
        """在线闸门：直连端全部应答后回收 rename 墓碑。

        rename 墓碑只做本地压制、不下发：仅当本轮所有直连端都已应答、没有
        端仍声明旧路径、且该身份确实已移到别的路径时才回收。delete 墓碑的
        回收仍由 storage.gc_tombstones 负责。
        """
        if self.distributor is None:
            return
        with self._lock:
            claim = set(self._round_exists_claim)
            remote_keys = set(self._round_remote_keys)
            responded = set(self._round_responded)
            self._round_exists_claim = set()
            self._round_deleted_claim = set()
            self._round_responded = set()
            self._round_remote_keys = set()
        if claim:
            return  # 仍有端声明路径存在，不能确认改名已完成
        live = set()
        if self.mesh is not None:
            try:
                live = set(self.mesh.connected_end_ids())
            except Exception:
                live = set()
            if not live.issubset(responded):
                return  # 有直连端未应答（含本轮中途新连入）→ 本轮不清理
        active = {st.file_id: st.name
                  for st in self.distributor.states().values()
                  if st.exists and st.file_id}
        for rec in self.distributor.rename_tombstones():
            path = rec.get('rel_path', '')
            fid = rec.get('file_id') or ''
            if not path or not fid:
                continue
            if (path, fid) in remote_keys:
                continue  # 仍有端持有旧路径
            if active.get(fid) in (None, path):
                continue  # 身份未移动，或又回到该路径
            self.distributor.forget_tombstone(path, fid)

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
        timer = self._align_timer
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        hasher = self._align_hasher
        if hasher is not None:
            try:
                hasher.stop()
            except Exception:
                pass
        with self._cond:
            self._cond.notify_all()
        for w in getattr(self, '_workers', ()):
            if w.is_alive():
                try:
                    w.join(timeout=2.0)
                except Exception:
                    pass
        # 退出清扫：本端已停止调度，残留临时文件不再有用（阶段 B）
        self.sweep_transient_temp()
