"""
去中心化同步：分发链路引擎（阶段 1）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.

职责（对应实施计划阶段 1）：
- 本地操作 → DISTRIBUTE_SIGNAL 沿网状直连传播（不再依赖主机转发）
- 收到信号 → 去重（同文件同 src_id 同 op_no 已应用则丢弃）→ 本地应用 →
  向除 src_id 外所有直连对端转发（防回声）
- 排队队列：信号串行处理，同 (src_id, file, op_no) 排队去重（冗余设计，防积压）
- 传输互操作：传输中收到 delete → 取消传输 + 状态置 CHANGE；
  传输中收到 rename/move → 进旁队列等传输完成再执行，执行时文件不存在则跳过
  （防止排在 delete 后）
- 端内文件状态表（内存，FileState）：记录每个文件最新 op_no/state/clock/ts/vv，
  供阶段 2 自同步链路复用
- 三层冲突解决（阶段 3）：信号携带 vv；收到远端信号按三层规则（版本向量 →
  逻辑钟 → 时间戳 → end_id 兜底）决定应用/忽略/覆盖；被覆盖端日志提示
  「本地修改被远端覆盖」并请求自同步链路拉取胜方内容；拉取回执（pulled）
  仅记知识防回声风暴

传输约定与全库一致：线程 + socket 1s 超时 + SendLock.send_resumable 背压退避。
"""
import os
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from network.protocol import Protocol, MessageType
from sync.vector import (FileState, STATE_ADD, STATE_CHANGE, merge_vv,
                         compare_states)


class Distributor(QObject):
    """分发链路引擎：信号生成、去重、转发与本地应用（每端一个）。

    阶段 3：收到远端信号按三层冲突规则（版本向量 → 逻辑钟 → 时间戳 → end_id
    兜底）决定应用/忽略/覆盖；被覆盖端日志提示「本地修改被远端覆盖」；覆盖时
    经冲突拉取回调请求对端内容（自同步链路端到端拉取）。
    """

    log_message = Signal(str)
    # 一条信号已在本地应用（含本端 emit 与远端信号）；dict = 信号
    signal_applied = Signal(dict)
    # 传输中收到 delete：请求调用方取消该文件传输
    transfer_cancel_requested = Signal(str)
    # 冲突覆盖（远端胜出且本端已有旧内容）：请求拉取胜方内容 file, src_id
    conflict_pull_requested = Signal(str, str)
    # 投递通知（阶段 5）：收到对端复制文件会话通知（content=JSON 字节），交 UI 展示远程文件胶囊
    files_notify_received = Signal(bytes)

    # 操作类型（DISTRIBUTE_SIGNAL 的 op 字段取值）
    OP_ADD = 'add'
    OP_DELETE = 'delete'
    OP_RENAME = 'rename'
    OP_MOVE = 'move'
    OP_DIR_CREATE = 'dir_create'

    def __init__(self, end_id: str, sync_folder: str, mesh=None, parent=None):
        super().__init__(parent)
        self.end_id = end_id
        self.sync_folder = os.path.abspath(sync_folder)
        self.mesh = mesh  # MeshManager；None 时仅本地（单测/离线场景）

        self._lock = threading.Lock()
        self._op_counters = {}    # file -> 本端已发出 op_no（源端递增）
        self._clocks = {}         # file -> 本端逻辑钟（每修改一次 +1）
        self._states = {}         # file -> FileState（端内文件状态表）
        self._queue = []          # 待处理信号（远端，FIFO）
        self._queue_keys = set()  # 排队中 (src_id, file, op_no)
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._transferring = set()    # 传输中的文件（接收/发送中）
        self._side_queue = {}         # file -> [rename/move 信号...]（旁队列）
        self._cancel_handler = None   # 取消传输回调 cb(file)
        self._applied_handler = None  # 本地应用回调（无事件循环场景/单测）
        self._conflict_pull_handler = None  # 冲突覆盖回调 cb(file, src_id)
        self._protected_dirs = set()  # 受保护目录(相对路径,如收集模式 IP 文件夹名):本体不可删,内部可删
        self._log_handler = None      # 日志回调（无事件循环场景/单测）
        self._files_notify_handler = None  # 投递通知回调 cb(content_bytes)（阶段 5，测试/无事件循环场景）

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    # ---- 回调注册 ----

    def set_applied_handler(self, cb: Callable[[dict], None]):
        """设置信号应用回调（在分发工作线程调用；GUI 场景用 Qt 信号）。"""
        self._applied_handler = cb

    def set_cancel_handler(self, cb: Callable[[str], None]):
        """设置传输取消回调：传输中收到 delete 时调用 cb(file)。"""
        self._cancel_handler = cb

    def set_conflict_pull_handler(self, cb: Callable[[str, str], None]):
        """设置冲突覆盖回调 cb(file, src_id)：远端胜出且本端已有旧内容时，
        请求自同步链路拉取胜方文件内容（阶段 3）。"""
        self._conflict_pull_handler = cb

    def set_log_handler(self, cb: Callable[[str], None]):
        """设置日志回调（在分发工作线程调用；GUI 场景用 Qt 信号 log_message）。"""
        self._log_handler = cb

    def set_files_notify_handler(self, cb: Callable[[bytes], None]):
        """设置投递通知回调 cb(content_bytes)（在 mesh 连接线程调用；GUI 场景用
        Qt 信号 files_notify_received）。阶段 5。"""
        self._files_notify_handler = cb

    def _notify_log(self, msg: str):
        # 修正：原为 self._notify_log(msg) 自递归（永不 emit），导致分发层日志在
        # GUI（log_message Qt 信号）中全部静默丢失。现改为 emit 到 Qt 信号。
        try:
            self.log_message.emit(msg)
        except Exception:
            pass
        if self._log_handler:
            try:
                self._log_handler(msg)
            except Exception:
                pass

    # ---- 属性 ----

    def get_state(self, file: str) -> Optional[FileState]:
        with self._lock:
            return self._states.get(file)

    def states(self) -> dict:
        with self._lock:
            return dict(self._states)

    def mark_transfer(self, file: str):
        """标记某文件传输中（接收/发送开始）。"""
        with self._lock:
            self._transferring.add(file)

    def unmark_transfer(self, file: str):
        """传输结束：解除标记并冲刷该文件的旁队列（按序重新入队应用）。"""
        with self._lock:
            self._transferring.discard(file)
            side = self._side_queue.pop(file, [])
        for signal in side:
            self._enqueue(signal)

    # ---- 本地操作入口 ----

    def emit(self, op: str, file: str, old: str = None) -> Optional[dict]:
        """本地文件操作 → 生成 DISTRIBUTE_SIGNAL 沿网状传播并记录本端状态。

        本地 FS 已由调用方完成（本方法不重复执行 FS 操作，仅记录状态 + 传播）。
        返回生成的信号 dict（无法传播时仍返回，供上层记录/测试）。
        """
        if not file:
            return None
        with self._lock:
            op_no = self._op_counters.get(file, 0) + 1
            self._op_counters[file] = op_no
            clock = self._clocks.get(file, 0) + 1
            self._clocks[file] = clock
            prev = self._states.get(file)
            vv = dict(prev.vv) if prev else {}
        vv[self.end_id] = op_no
        signal = {
            'src_id': self.end_id,
            'op_no': op_no,
            'op': op,
            'file': file,
            'state': STATE_ADD if op == self.OP_ADD else STATE_CHANGE,
            'clock': clock,
            'ts': time.time(),
            'vv': vv,
        }
        if old is not None:
            signal['old'] = old
        self._record_local(signal)
        if self.mesh is not None:
            try:
                self.mesh.send_to_all(Protocol.create_distribute_signal(signal),
                                      except_end_id=self.end_id)
            except Exception as e:
                self._notify_log(f"分发信号失败: {e}")
        return signal

    def remove_state(self, file: str):
        """移除端内文件状态条目（自同步链路列表收敛：双方均已删除的文件）。"""
        with self._lock:
            self._states.pop(file, None)
            self._op_counters.pop(file, None)
            self._clocks.pop(file, None)

    def emit_pulled(self, file: str, remote_src_id: str, remote_op_no: int,
                    remote_clock: int = 0, remote_ts: float = 0.0,
                    content_fresh: bool = True,
                    bytes_clock: int = 0, bytes_ts: float = 0.0):
        """自同步拉取完成：记录本端已持有该文件并广播 add 状态（阶段 2）。

        本地 FS 已由 pull_file 落盘完成。状态表 vv 推进到远端 op_no（本端已
        应用该版本），并计入本端源计数，避免后续对比重复拉取。clock/ts 取
        拉取版本的仲裁知识指纹（快照 clock/ts）——即当前仲裁胜者，状态表不
        回退、对账指纹比对不失配；用本端自增 clock/now-ts 会污染仲裁指纹。

        bytes_clock/bytes_ts 为源端磁盘字节指纹（落盘内容的真实版本）：与知识
        指纹不一致说明源端知识推进但磁盘字节未到位，本端拉到的可能是过时字节。
        bytes_vv 推进到落盘内容版本 {remote_src_id: remote_op_no}，但仅当
        content_fresh=True（源端字节指纹 == 快照知识版本，实际拉到的内容即
        仲裁胜者）。fresh=False 说明源端知识推进但磁盘字节未到位，本端拉到的
        是过时字节——不推进 bytes_vv，使对账兜底（知识 vv 胜 bytes_vv）继续
        从可信源补拉收敛，杜绝「期望指纹标记陈旧字节」的永久分叉。

        广播回执 vv 仅含本端自 op（不携带远端 src 版本）：回执携带 src 版本
        会虚增他端知识——他端据此把胜者真实信号误判为「已应用」而去重，
        永不触发冲突拉取 → 字节分叉永久定格（本环境复现过的核心竞态）。
        """
        with self._lock:
            op_no = self._op_counters.get(file, 0) + 1
            self._op_counters[file] = op_no
            prev = self._states.get(file)
            prev_bytes = dict(prev.bytes_vv) if prev else {}
        ts = float(remote_ts or 0.0) or time.time()
        clock = int(remote_clock or 0)
        # 防胜者端被低版本拉取降级（关键）：本端状态表 (clock, ts) 是当前仲裁
        # 胜者指纹。拉取完成若直接用拉取版本指纹覆盖，胜者端拉到败者字节后其
        # 状态表会被降级为败者指纹——该指纹与磁盘字节一致（bytes_clock==clock、
        # bytes_ts==ts），会以「可信源」姿态把败者内容散布全网，胜者内容永久
        # 销毁 → 字节分叉。仅当拉取版本严格强于本端状态（clock/ts 更高）时才
        # 采纳拉取指纹；否则保留本端（胜者）指纹。字节缺口由对账 backfill 从
        # 字节新鲜的端补拉收敛（_need_bytes_backfill 的字节指纹门控）。
        if prev is not None:
            if clock < prev.clock or (clock == prev.clock and ts < prev.ts):
                clock = prev.clock
                ts = prev.ts
        self._clocks[file] = max(self._clocks.get(file, 0), clock)
        st = FileState(name=file, op_no=op_no, state=STATE_ADD, exists=True,
                       clock=clock, ts=ts,
                       vv={self.end_id: op_no, remote_src_id: remote_op_no},
                       bytes_vv=(merge_vv(prev_bytes,
                                          {remote_src_id: remote_op_no})
                                 if content_fresh else prev_bytes),
                       bytes_clock=int(bytes_clock or 0),
                       bytes_ts=float(bytes_ts or 0.0) or ts)
        self._store(st)
        signal = {
            'src_id': self.end_id,
            'op_no': op_no,
            'op': self.OP_ADD,
            'file': file,
            'state': STATE_ADD,
            'clock': clock,
            'ts': ts,
            'vv': {self.end_id: op_no},
            'pulled': True,  # 拉取完成回执：对端仅记知识，不再触发冲突覆盖拉取
        }
        if self.mesh is not None:
            try:
                self.mesh.send_to_all(Protocol.create_distribute_signal(signal),
                                      except_end_id=self.end_id)
            except Exception as e:
                self._notify_log(f"分发信号失败: {e}")
        self._notify_applied(signal)
        return signal

    # ---- 投递通知（阶段 5）：复制文件信号改走分发链路，替换主机转发 ----

    def emit_files_notify(self, notify_dict: dict) -> bool:
        """本地复制文件 → 投递通知沿网状直连广播（不经主机转发）。

        文件字节仍由接收端端到端直连复制端 FileProvider 拉取（不动）。
        返回 True 表示已沿网状发出；分发链路未就绪或无直连对端返回 False，
        调用方应回退旧路径（经主机转发，阶段 5 迁移兜底）。
        """
        if self.mesh is None:
            return False
        if not self.mesh.connected_end_ids():
            return False
        try:
            self.mesh.send_to_all(
                Protocol.create_clipboard_notify_signal(notify_dict),
                except_end_id=self.end_id)
        except Exception as e:
            self._notify_log(f"投递通知广播失败: {e}")
            return False
        return True

    def on_files_notify(self, content):
        """收到对端投递通知（网状直连，阶段 5）：转 bytes 交 UI 展示远程文件胶囊。

        双通道：回调（mesh 连接线程直接调用，测试/无事件循环场景）+ Qt 信号
        （GUI 主线程）。投递通知是一次性广播（接收端不转发），不进入文件状态/
        冲突裁决，与同步信号（需洪泛去重）语义不同。
        """
        if isinstance(content, dict):
            import json
            content = json.dumps(content, ensure_ascii=False).encode('utf-8')
        if not isinstance(content, (bytes, bytearray)):
            return
        payload = bytes(content)
        if self._files_notify_handler:
            try:
                self._files_notify_handler(payload)
            except Exception:
                pass
        try:
            self.files_notify_received.emit(payload)
        except Exception:
            pass

    # ---- 远端信号入口 ----

    def on_signal(self, signal: dict):
        """收到对端 DISTRIBUTE_SIGNAL：去重后入队，由工作线程应用并转发。"""
        if not isinstance(signal, dict):
            return
        with self._lock:
            self._enqueue_locked(signal)

    # ---- 内部：排队 ----

    def _enqueue(self, signal: dict):
        with self._lock:
            self._enqueue_locked(signal)

    def _enqueue_locked(self, signal: dict):
        """锁内入队：非法/自身信号（回声）丢弃；已应用/已在队列则去重。"""
        src_id = signal.get('src_id', '')
        op_no = int(signal.get('op_no', 0) or 0)
        file = signal.get('file', '')
        if not src_id or src_id == self.end_id or not file or op_no <= 0:
            return  # 非法信号 / 自身信号回声
        st = self._states.get(file)
        if st and st.vv.get(src_id, 0) >= op_no:
            return  # 该源该编号（或更新）已应用过，丢弃
        key = (src_id, file, op_no)
        if key in self._queue_keys:
            return  # 排队去重：同文件同编号已在队列
        self._queue_keys.add(key)
        self._queue.append(signal)
        self._cond.notify()

    def _worker_loop(self):
        while not self._stop.is_set():
            with self._cond:
                while not self._queue and not self._stop.is_set():
                    self._cond.wait(timeout=0.5)
                if self._stop.is_set():
                    break
                signal = self._queue.pop(0)
                src_id = signal.get('src_id', '')
                file = signal.get('file', '')
                op_no = int(signal.get('op_no', 0) or 0)
                self._queue_keys.discard((src_id, file, op_no))
            try:
                self._process_signal(signal)
            except Exception as e:
                self._notify_log(f"处理信号失败: {e}")

    # ---- 信号处理 ----

    def _process_signal(self, signal: dict):
        src_id = signal.get('src_id', '')
        op_no = int(signal.get('op_no', 0) or 0)
        file = signal.get('file', '')
        op = signal.get('op', '')
        # 冗余校验：可能已被其他路径（旁队列/并发）应用
        with self._lock:
            st = self._states.get(file)
            if st and st.vv.get(src_id, 0) >= op_no:
                return
        if op in (self.OP_RENAME, self.OP_MOVE):
            # 传输中 rename/move：进旁队列等传输完成再执行（同源同编号去重）。
            # 传输以「当前名」标记，rename/move 的源名（old）即当前名。
            transfer_key = signal.get('old') or file
            if self._is_transferring(transfer_key):
                with self._lock:
                    side = self._side_queue.setdefault(transfer_key, [])
                    if any(s.get('src_id') == src_id
                           and int(s.get('op_no', 0) or 0) == op_no for s in side):
                        return
                    side.append(signal)
                return
        if op == self.OP_DELETE and self._is_transferring(file):
            # 传输中 delete：取消传输 + 状态置 CHANGE（随后正常应用删除）
            self._request_cancel(file)
        self._apply(file, op, signal, src_id, op_no)

    def _request_cancel(self, file: str):
        if self._cancel_handler:
            try:
                self._cancel_handler(file)
            except Exception:
                pass
        try:
            self.transfer_cancel_requested.emit(file)
        except Exception:
            pass

    def _is_transferring(self, file: str) -> bool:
        with self._lock:
            return file in self._transferring

    # ---- 应用 ----

    def _apply(self, file: str, op: str, signal: dict, src_id: str, op_no: int):
        """应用一条远端信号：三层冲突裁决 + FS 操作 + 状态表更新 + 防回声转发。

        阶段 3 裁决（按实施计划第 4 节四层口径）：
        - 本端无状态 / 远端胜出 → 应用（覆盖本地内容，被覆盖端日志提示，add 时
          经冲突拉取回调请求对端字节）
        - 本地胜出 / 已收敛 → 不执行 FS 操作，仅记录对端 vv 知识（防重放），仍转发
        - 拉取回执（pulled=True）→ 仅记录知识，不触发覆盖拉取（防回声风暴）
        """
        clock = int(signal.get('clock', 0) or 0)
        ts = float(signal.get('ts', 0.0) or 0.0)
        old = signal.get('old')
        vv_raw = signal.get('vv')
        vv = dict(vv_raw) if isinstance(vv_raw, dict) else {src_id: op_no}
        pulled = bool(signal.get('pulled'))
        incoming = FileState(name=file, op_no=op_no,
                             state=STATE_ADD if op == self.OP_ADD else STATE_CHANGE,
                             exists=op != self.OP_DELETE,
                             clock=clock, ts=ts, vv=vv)
        # 三层冲突裁决
        with self._lock:
            local = self._states.get(file)
        if local is not None:
            if pulled:
                # 拉取回执：仅合并对端知识，不应用、不覆盖（防回声风暴）
                self._record_knowledge(file, vv)
                self._forward(signal, src_id)
                return
            result = compare_states(incoming, local, src_id, self.end_id)
            if result <= 0:
                if result == -1:
                    # 本地胜出：忽略远端信号，仅记对端 vv 知识
                    self._record_knowledge(file, vv)
                    self._notify_log(
                        f"本地版本胜出，忽略远端 {src_id} 信号: {file}")
                self._forward(signal, src_id)  # 已收敛也继续转发（其他端可能需应用）
                return
            # 远端胜出：覆盖本地内容
            if local.clock > 0:
                self._notify_log(f"本地修改被远端覆盖: {file}")
        try:
            if op == self.OP_ADD:
                exists = os.path.exists(self._safe_join(file))
                self._store(FileState(name=file, op_no=op_no, state=STATE_ADD,
                                      exists=exists, clock=clock, ts=ts,
                                      vv=vv))
            elif op == self.OP_DELETE:
                self._delete_local(file)
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=False, clock=clock, ts=ts,
                                      vv=vv))
            elif op in (self.OP_RENAME, self.OP_MOVE):
                old_name = old or file
                if not self._rename_local(old_name, file):
                    return  # 源不存在（可能排在 delete 后）→ 跳过
                prev = self._states.get(old_name)
                vv_merged = dict(prev.vv) if prev else {}
                vv_merged = merge_vv(vv_merged, vv)
                # 内容随重命名移动（字节未变）：bytes_vv / 字节指纹从旧名延续到新名
                prev_bytes = dict(prev.bytes_vv) if prev else {}
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=True, clock=clock, ts=ts,
                                      vv=vv_merged, bytes_vv=prev_bytes,
                                      bytes_clock=prev.bytes_clock if prev else 0,
                                      bytes_ts=prev.bytes_ts if prev else 0.0))
                if old_name != file:
                    # 旧名留墓碑（变更 + 不存在）：替代直接移除旧条目。对端未收到
                    # rename 时其快照仍含旧名存在条目，本端若删掉旧名状态，会把
                    # 旧文件反向拉回（删除-拉取震荡）；留墓碑使对端据快照补删旧名，
                    # 双方均为「变更 + 不存在」后由对账清理条目收敛。
                    self._store(FileState(name=old_name, op_no=op_no,
                                          state=STATE_CHANGE, exists=False,
                                          clock=clock, ts=ts, vv=vv_merged))
            elif op == self.OP_DIR_CREATE:
                os.makedirs(self._safe_join(file), exist_ok=True)
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=True, clock=clock, ts=ts,
                                      vv=vv))
            else:
                return  # 未知操作：不应用、不转发
        except ValueError as e:
            self._notify_log(f"拒绝非法路径: {e}")
            return
        # 拉取触发：add 信号应用后，本端无该文件（缺失拉取，阶段 4）或
        # 本端已有旧内容且远端胜出（冲突覆盖）→ 请求拉取源端字节
        if op == self.OP_ADD:
            exists = False
            try:
                exists = os.path.exists(self._safe_join(file))
            except ValueError:
                pass
            if not exists:
                # 阶段 4：本端无该文件（缺失）→ 直接向源端请求状态并拉取字节，
                # 不再依赖主机转发（主机断线后其余端仍能互相同步）
                self._request_conflict_pull(file, src_id)
            elif local is not None:
                # 防覆盖竞态：本端磁盘内容更新于远端信号（mtime > 信号 ts）→
                # 本端是尚未 emit 的新内容（本地写入先于 watcher 上报），跳过
                # 冲突拉取，交由本端 watcher emit 后仲裁——否则胜者本端自己的
                # 新字节会被旧版本拉取覆盖，胜者内容永久丢失 → 永久分叉。
                # 兜底：即便误判（本地实为旧内容），对账 backfill 仍会补拉收敛。
                try:
                    if os.path.getmtime(self._safe_join(file)) > ts + 1e-2:
                        self._notify_log(f"本端内容更新，跳过覆盖拉取: {file}")
                    else:
                        self._request_conflict_pull(file, src_id)
                except OSError:
                    self._request_conflict_pull(file, src_id)
        self._forward(signal, src_id)
        self._notify_applied(signal)

    def _forward(self, signal: dict, except_end_id: str):
        """防回声转发：向除 src_id 外所有直连对端（本地已收敛/忽略时同样转发）。"""
        if self.mesh is not None:
            try:
                self.mesh.send_to_all(Protocol.create_distribute_signal(signal),
                                      except_end_id=except_end_id)
            except Exception as e:
                self._notify_log(f"转发信号失败: {e}")

    def _record_knowledge(self, file: str, vv: dict):
        """仅合并对端 vv 知识（不改动本地 clock/ts/FS）：本地胜出或拉取回执时，
        记录对端已到达的版本，保证去重且不抬高本地时钟偏置后续裁决。"""
        with self._lock:
            st = self._states.get(file)
            if st is None:
                return
            st.vv = merge_vv(st.vv, vv)

    def _request_conflict_pull(self, file: str, src_id: str):
        """请求自同步链路拉取对端（胜方）文件内容覆盖本地旧版本。"""
        try:
            self.conflict_pull_requested.emit(file, src_id)
        except Exception:
            pass
        if self._conflict_pull_handler:
            try:
                self._conflict_pull_handler(file, src_id)
            except Exception:
                pass

    def _record_local(self, signal: dict):
        """记录本端 emit 的状态（本地 FS 已由调用方完成，不重复执行）。"""
        file = signal['file']
        op = signal['op']
        src_id = signal['src_id']
        op_no = signal['op_no']
        clock = signal['clock']
        ts = signal['ts']
        vv = signal.get('vv') or {src_id: op_no}
        with self._lock:
            prev = self._states.get(file)
            prev_bytes = dict(prev.bytes_vv) if prev else {}
        try:
            if op == self.OP_ADD:
                exists = os.path.exists(self._safe_join(file))
                # 本地 emit：字节真实为本端版本 → bytes_vv 推进到 {本端: op_no}，
                # 字节指纹 = 本端 (clock, ts)。对齐磁盘 mtime = 声明 ts：拉取端
                # 落盘后以实际 dest mtime 判别「实际收到内容是否即声明指纹」——
                # 磁盘 mtime 与声明不符即证明该端字节已被其自身并发拉取覆盖
                # （谎报源），拒绝采纳。OSError（文件被锁/瞬时不可达）忽略：
                # 最坏退化为无 mtime 校准，接收端判 fresh=False 走对账补拉。
                try:
                    os.utime(self._safe_join(file), (ts, ts))
                except OSError:
                    pass
                self._store(FileState(name=file, op_no=op_no, state=STATE_ADD,
                                      exists=exists, clock=clock, ts=ts,
                                      vv=vv,
                                      bytes_vv=merge_vv(prev_bytes,
                                                        {src_id: op_no}),
                                      bytes_clock=clock, bytes_ts=ts))
            elif op == self.OP_DELETE:
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=False, clock=clock, ts=ts,
                                      vv=vv))
            elif op in (self.OP_RENAME, self.OP_MOVE):
                old = signal.get('old', file)
                old_st = self._states.get(old)
                old_bytes = dict(old_st.bytes_vv) if old_st else {}
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=os.path.exists(self._safe_join(file)),
                                      clock=clock, ts=ts, vv=vv,
                                      bytes_vv=old_bytes,
                                      bytes_clock=old_st.bytes_clock if old_st else 0,
                                      bytes_ts=old_st.bytes_ts if old_st else 0.0))
                if old and old != file:
                    # 旧名留墓碑（变更 + 不存在）：对端未收到 rename 时据快照补删
                    # 旧名，避免旧条目残留/反向拉回；双方均为「变更 + 不存在」
                    # 后由对账清理条目。
                    prev_old = self._states.get(old)
                    old_vv = dict(prev_old.vv) if prev_old else {}
                    old_vv = merge_vv(old_vv, vv)
                    self._store(FileState(name=old, op_no=op_no,
                                          state=STATE_CHANGE, exists=False,
                                          clock=clock, ts=ts, vv=old_vv))
            elif op == self.OP_DIR_CREATE:
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=True, clock=clock, ts=ts,
                                      vv=vv))
        except ValueError as e:
            self._notify_log(f"拒绝非法路径: {e}")
            return
        self._notify_applied(signal)

    def _notify_applied(self, signal: dict):
        try:
            self.signal_applied.emit(signal)
        except Exception:
            pass
        if self._applied_handler:
            try:
                self._applied_handler(signal)
            except Exception:
                pass

    def _store(self, new_state: FileState, remove: str = None):
        """写入状态表：与既有条目合并 vv/clock/ts（保留历史推进方向）。

        bytes_vv 仅当条目存在（exists=True 或显式给出）时合并——删除/拉取待命态
        （add 已应用、字节未到位）不延续旧字节版本，字节已不存在。
        字节指纹（bytes_clock/bytes_ts）合并规则：新状态显式给出（非零）采纳；
        否则 exists=True 延续 prev（磁盘字节未改写）、exists=False 清零（删除）。
        """
        with self._lock:
            prev = self._states.get(new_state.name)
            if prev:
                new_state.vv = merge_vv(prev.vv, new_state.vv)
                # 状态表 clock/ts 采用仲裁胜者自身指纹（不再 max 合并）：max 合并
                # 会把「低时钟高时间戳」的败者 ts 混入胜者指纹，形成无人持有的
                # 混合指纹——对账指纹比对（remote.ts==local_st.ts）永远失配、
                # 胜者字节永不补拉 → 永久分叉。每个 _store 的新状态都已在调用方
                # 赢下仲裁（或为本地最新操作/已通过过时判定的拉取版本），其
                # (clock, ts) 即当前仲裁胜者指纹，直接采纳。
                if new_state.bytes_vv or new_state.exists:
                    new_state.bytes_vv = merge_vv(prev.bytes_vv,
                                                  new_state.bytes_vv)
                else:
                    new_state.bytes_vv = {}
                if new_state.bytes_clock or new_state.bytes_ts:
                    pass  # 显式给出（非零）→ 采纳
                elif new_state.exists:
                    new_state.bytes_clock = prev.bytes_clock
                    new_state.bytes_ts = prev.bytes_ts
                else:
                    new_state.bytes_clock = 0
                    new_state.bytes_ts = 0.0
            self._states[new_state.name] = new_state
            if remove:
                self._states.pop(remove, None)

    # ---- 本地 FS 操作 ----

    def _safe_join(self, name: str) -> str:
        """安全拼接同步文件夹路径（防路径穿越）。"""
        if not name:
            raise ValueError("文件名为空")
        norm = os.path.normpath(name.replace('\\', '/'))
        if norm in ('.', '') or norm.startswith('..') or os.path.isabs(norm):
            raise ValueError(f"非法路径: {name}")
        path = os.path.normpath(os.path.join(self.sync_folder, norm))
        if path != self.sync_folder and not path.startswith(self.sync_folder + os.sep):
            raise ValueError(f"非法路径: {name}")
        return path

    def _delete_local(self, file: str):
        # 受保护目录（收集/同步模式下连接端 IP 文件夹本体）不可被删除；
        # 其内部文件/子目录（rel 路径不等于保护名）照常可删。
        if file in self._protected_dirs:
            self.log_message.emit(f"拒绝删除受保护目录: {file}")
            return
        path = self._safe_join(file)
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
        elif os.path.isdir(path):
            from sync.file_manager import safe_rmtree
            safe_rmtree(path)

    def set_protected_dirs(self, dirs):
        """设置不可被删除的目录集合（相对路径，如收集模式 IP 文件夹名）。

        仅拦截「目标 == 某个保护目录本身」的删除；其内部文件/子目录不受限。
        """
        with self._lock:
            self._protected_dirs = set(dirs)

    def _rename_local(self, old: str, new: str) -> bool:
        """重命名/移动：源不存在返回 False（跳过）；目标存在先删除。"""
        old_path = self._safe_join(old)
        new_path = self._safe_join(new)
        if not os.path.exists(old_path):
            return False
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        if os.path.exists(new_path):
            if os.path.isdir(new_path) and not os.path.islink(new_path):
                from sync.file_manager import safe_rmtree
                safe_rmtree(new_path)
            else:
                os.remove(new_path)
        os.rename(old_path, new_path)
        return True

    # ---- 生命周期 ----

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._worker and self._worker.is_alive():
            try:
                self._worker.join(timeout=2.0)
            except Exception:
                pass
