"""
去中心化同步：分发链路引擎（阶段 1）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import stat
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from network.protocol import Protocol, MessageType
from sync.file_identity import new_file_id, pick_same_content_winner
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
    # 文本投递：收到对端文本内容 (mime_type, data)，交 UI 写系统剪贴板
    clipboard_text_received = Signal(str, bytes)

    # 操作类型（DISTRIBUTE_SIGNAL 的 op 字段取值）
    OP_ADD = 'add'
    OP_DELETE = 'delete'
    OP_RENAME = 'rename'
    OP_MOVE = 'move'
    OP_DIR_CREATE = 'dir_create'

    # 内存模式墓碑上限（§4）：与状态库默认值一致
    TOMBSTONE_LIMIT = 10000

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
        self._clipboard_text_handler = None  # 文本投递回调 cb(mime_type, data)（测试/无事件循环场景）

        # 阶段 A：房间 db 持久化（None = 纯内存，单测/离线场景）
        self._storage = None
        self._tombstones = {}   # (path, file_id) -> 墓碑记录
        self._db_meta = {}      # path -> (size, mtime)：db 落库时的磁盘内容快照
        self._peers = set()     # 认证成功连接过的已知端（持久 end_id）
        # 阶段 B：哈希队列（None = 不启用 pending 门禁，同步广播语义不变）
        self._hasher = None
        self._hash_pending = {}  # 路径 -> 在途哈希任务数（拉取门禁依据）

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

    def set_clipboard_text_handler(self, cb: Callable[[str, bytes], None]):
        """设置文本投递回调 cb(mime_type, data)（在 mesh 连接线程调用；GUI 场景用
        Qt 信号 clipboard_text_received）。"""
        self._clipboard_text_handler = cb

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

    # ---- 持久化（阶段 A） ----

    def attach_storage(self, storage):
        """接入房间 db：加载计数器、文件清单、墓碑与已知端（内存为主）。"""
        with self._lock:
            self._storage = storage
            self._db_meta.clear()
            for path, rec in storage.load_counters().items():
                self._op_counters[path] = max(self._op_counters.get(path, 0),
                                              int(rec.get('op_no') or 0))
                self._clocks[path] = max(self._clocks.get(path, 0),
                                         int(rec.get('clock') or 0))
            for path, rec in storage.load_files().items():
                st = self._state_from_record(rec)
                prev = self._states.get(path)
                if prev is None:
                    self._states[path] = st
                else:
                    prev.vv = merge_vv(prev.vv, st.vv)
                self._clocks[path] = max(self._clocks.get(path, 0), st.clock)
                self._op_counters[path] = max(self._op_counters.get(path, 0),
                                              st.op_no)
                self._db_meta[path] = (rec.get('size'),
                                       float(rec.get('mtime') or 0.0))
            self._tombstones = storage.load_tombstones()
            # 墓碑同样抬升版本地板：计数器行被上限淘汰/缺失时，同名重建不得
            # 复用低 op_no/clock，否则会被墓碑去重丢弃（删除-重建震荡）。
            for (path, _fid), rec in self._tombstones.items():
                self._op_counters[path] = max(
                    self._op_counters.get(path, 0),
                    int(rec.get('op_no') or 0))
                self._clocks[path] = max(
                    self._clocks.get(path, 0), int(rec.get('clock') or 0))
            self._peers = set(storage.load_peers())
        self._cleanup_loaded_state()

    def _cleanup_loaded_state(self):
        """加载后修复身份：文件必须有 file_id，缺 ID 的删除墓碑一律作废。

        文件 ID 是删除的唯一身份凭据。旧版本/旧数据里可能存在无 ID 的文件
        状态与删除墓碑：前者补发 ID（保证后续删除可表达），后者直接删除
        （没有身份的删除指令不成立，留着只会在重放时按路径误删文件）。
        """
        invalid = [key for key, rec in self._tombstones.items()
                   if rec.get('origin') == 'delete'
                   and not rec.get('is_dir') and not key[1]]
        for key in invalid:
            with self._lock:
                self._tombstones.pop(key, None)
            if self._storage is not None:
                self._storage.remove_tombstone(*key)
            self._notify_log(f"作废缺文件ID的删除墓碑: {key[0]}")
        dirty_dirs = [key for key, rec in self._tombstones.items()
                      if rec.get('is_dir') and key[1]]
        for key in dirty_dirs:
            with self._lock:
                rec = self._tombstones.pop(key, None)
                if rec is None:
                    continue
                rec['file_id'] = ''
                existing = self._tombstones.get((key[0], ''))
                # 同路径已有空 ID 目录墓碑时取版本较高者并合并确认记录：
                # 直接用旧库数据覆盖会把版本地板拉低，同名重建会被墓碑去重丢弃。
                confirmed = []
                for src in (rec, existing):
                    for pid in (src or {}).get('confirmed_by') or []:
                        if pid not in confirmed:
                            confirmed.append(pid)
                if (existing is not None
                        and int(existing.get('op_no') or 0)
                        >= int(rec.get('op_no') or 0)):
                    rec = dict(existing)
                else:
                    rec = dict(rec)
                rec['file_id'] = ''
                rec['confirmed_by'] = confirmed
                self._tombstones[(key[0], '')] = rec
            if self._storage is not None:
                self._storage.remove_tombstone(*key)
                self._storage.put_tombstone(rec)
            self._notify_log(f"归一目录墓碑身份为空: {key[0]}")
        healed = []
        with self._lock:
            for st in self._states.values():
                if not st.is_dir and not st.file_id:
                    st.file_id = new_file_id()
                    healed.append(st)
        for st in healed:
            self._persist_state(st)
            self._notify_log(f"补发缺失的文件ID: {st.name}")

    @staticmethod
    def _state_from_record(rec: dict) -> FileState:
        return FileState(
            name=rec['rel_path'], op_no=int(rec.get('op_no') or 0),
            state=STATE_ADD, exists=True, clock=int(rec.get('clock') or 0),
            ts=float(rec.get('ts') or 0.0), vv=dict(rec.get('vv') or {}),
            file_id=rec.get('file_id') or '', hash=rec.get('hash') or '',
            hash_state=rec.get('hash_state') or 'none',
            size=int(rec.get('size') or 0),
            is_dir=bool(rec.get('is_dir')))

    def attach_hasher(self, hasher):
        """接入哈希队列：pending 条目算完 MD5+size 后才发布 add（阶段 B）。"""
        self._hasher = hasher
        if hasher is not None:
            hasher.set_callback(self._on_hash_done)

    def hash_gate_active(self) -> bool:
        """pending 门禁是否生效（未接入哈希队列时保持旧同步语义）。"""
        return self._hasher is not None

    def is_hash_pending(self, rel_path: str) -> bool:
        """该路径是否有哈希任务在途（在途期间忽略同步、避免重复拉取）。"""
        with self._lock:
            return self._hash_pending.get(rel_path, 0) > 0

    def _enqueue_hash(self, st: Optional[FileState], kind: str,
                      extra: dict = None) -> bool:
        """把 pending 条目送入哈希队列；文件不在盘上则不排队。"""
        if self._hasher is None or st is None:
            return False
        try:
            path = self._safe_join(st.name)
        except ValueError:
            return False
        if not os.path.isfile(path):
            return False
        self._hasher.submit(path, st.name, st.file_id, kind=kind, extra=extra)
        with self._lock:
            self._hash_pending[st.name] = self._hash_pending.get(st.name, 0) + 1
        return True

    def _bump_add_signal(self, file: str) -> dict:
        """按版本地板生成一条新的本地 add 信号（不落库、不广播）。"""
        with self._lock:
            prev = self._states.get(file)
            floor_op = self._op_counters.get(file, 0)
            floor_ck = self._clocks.get(file, 0)
            if prev is not None:
                floor_op = max(floor_op, prev.op_no, prev.vv.get(self.end_id, 0))
                floor_ck = max(floor_ck, prev.clock)
            op_no, clock = floor_op + 1, floor_ck + 1
            self._op_counters[file] = op_no
            self._clocks[file] = clock
            vv = dict(prev.vv) if prev else {}
        vv[self.end_id] = op_no
        return {'src_id': self.end_id, 'op_no': op_no, 'op': self.OP_ADD,
                'file': file, 'state': STATE_ADD, 'clock': clock,
                'ts': time.time(), 'vv': vv}

    def _broadcast_signal(self, signal: dict):
        if self.mesh is None:
            return
        try:
            self.mesh.send_to_all(Protocol.create_distribute_signal(signal),
                                  except_end_id=self.end_id)
        except Exception as e:
            self._notify_log(f"分发信号失败: {e}")

    def _hash_done(self, rel: str, ok: bool, extra: dict):
        """哈希流程收尾回调（拉取方据此发 file_added/pull_done）。"""
        cb = extra.get('on_done') if extra else None
        if cb is None:
            return
        try:
            cb(ok)
        except Exception:
            pass

    def _on_hash_done(self, payload: dict):
        """哈希结果回写：校验未变 → 置 ok 并发布；已删除/换 ID → 丢弃。"""
        rel = payload.get('rel_path') or ''
        fid = payload.get('file_id') or ''
        kind = payload.get('kind') or 'local_add'
        extra = payload.get('extra') or {}
        if not rel:
            return
        with self._lock:
            left = self._hash_pending.get(rel, 0) - 1
            if left > 0:
                self._hash_pending[rel] = left
            else:
                self._hash_pending.pop(rel, None)
        if payload.get('changed'):
            # 入队后内容变化：按同一 ID 更新提版本，重新入队
            with self._lock:
                prev = self._states.get(rel)
            if prev is None or not prev.exists:
                self._hash_done(rel, False, extra)
                return
            signal = self._bump_add_signal(rel)
            # 不改 mtime：否则会作废同批已入队任务，形成重算死循环
            self._record_local(signal, touch=False)
            st = self.get_state(rel)
            if not self._enqueue_hash(st, kind, dict(extra, signal=signal)):
                self._hash_done(rel, False, extra)
            return
        with self._lock:
            st = self._states.get(rel)
        if st is None or not st.exists or st.hash_state != 'pending' \
                or (fid and st.file_id != fid):
            # pending 期间被删除或换了身份：丢弃结果，不得发布 add
            self._hash_done(rel, False, extra)
            return
        sig = extra.get('signal') or {}
        if sig and (sig.get('op_no') != st.op_no
                    or int(sig.get('clock', 0) or 0) != st.clock
                    or float(sig.get('ts', 0.0) or 0.0) != st.ts):
            # 期间状态已被更高版本取代（本地新写/远端胜出）：旧任务结果作废，
            # 否则旧内容的哈希会贴到胜者版本上，让过时槽被误判为已过时
            self._hash_done(rel, False, extra)
            return
        if payload.get('error'):
            self._notify_log(f"哈希失败 {rel}: {payload.get('error')}")
            self._hash_done(rel, False, extra)
            return
        with self._lock:
            st.hash = payload.get('hash') or ''
            st.size = int(payload.get('size') or 0)
            st.hash_state = 'ok'
        self._persist_state(st)
        signal = extra.get('signal')
        if signal is not None:
            # 补上本次算出的内容证据：emit 时仍在 pending，广播前必须回填，
            # 否则接收端拿不到 hash，无法判定无需重复拉取（会永久分叉）
            signal['hash'] = st.hash
            signal['size'] = int(st.size or 0)
            if kind != 'local_add':
                signal['pulled'] = True
            self._broadcast_signal(signal)
            if kind != 'local_add':
                self._notify_applied(signal)
        self._hash_done(rel, True, extra)

    def register_received_file(self, rel_path: str):
        """主机直推落盘：登记 pending 并排队哈希，ok 后才发布（阶段 B）。"""
        if self._hasher is None or not rel_path:
            return
        signal = self._bump_add_signal(rel_path)
        self._record_local(signal)
        st = self.get_state(rel_path)
        self._enqueue_hash(st, 'received', {'signal': signal})

    def flush(self):
        """同步刷盘（退出/切房间/测试用）。"""
        if self._storage is not None:
            self._storage.flush()

    def reconcile_disk(self):
        """启动对账：db 有盘无 → 删除墓碑；盘有 db 无 → 新身份待哈希。

        存在性按条目逐路径核对，不依赖全树扫描：每条路径独立裁决，某条
        无法判定（权限/IO）只跳过它自己，其余照常。根目录整体不可达（未
        挂载/被移走）时本轮不做删除核对，否则所有条目会被误判为缺失。

        Returns: {'deleted': n, 'added': n, 'updated': n}
        """
        result = {'deleted': 0, 'added': 0, 'updated': 0}
        if self._storage is None:
            return result
        disk_files, disk_dirs, scan_complete = self._scan_disk()
        if not os.path.isdir(self.sync_folder):
            self._notify_log('同步目录不可达，本轮不核对删除')
        elif not scan_complete:
            self._notify_log('磁盘扫描不完整，新增发现可能不全')
        with self._lock:
            states = list(self._states.values())
            tomb_paths = {path for (path, _fid) in self._tombstones}
        if os.path.isdir(self.sync_folder):
            for st in states:
                if not st.exists:
                    continue
                kind = self._path_kind(st.name)
                if kind == 'missing':
                    self._mark_local_deleted(st.name, st.file_id,
                                             self._path_is_dir(st.name, st))
                    result['deleted'] += 1
                elif kind in ('file', 'dir'):
                    # 类型漂移：状态表的类型就是旧对象身份，按「删除旧对象 +
                    # 按真实类型新建」表达，由版本仲裁收敛，避免两端振荡。
                    if (kind == 'dir') != bool(st.is_dir):
                        self._realign_type(st.name, kind == 'dir')
                        result['deleted'] += 1
                        result['added'] += 1
        for path in sorted(disk_files | disk_dirs):
            with self._lock:
                known = path in self._states or path in tomb_paths
            if known:
                continue  # 已有条目或墓碑残留 → 不复活
            self._register_new_local(path, is_dir=path in disk_dirs)
            result['added'] += 1
        # db 有、盘在但 size/mtime 变化：视为同一 ID 的更新（§6），提版本重算哈希
        for st in states:
            if (not st.exists or st.is_dir or not st.file_id
                    or st.hash_state != 'ok' or st.name not in disk_files):
                continue
            with self._lock:
                tombstoned = (st.name, st.file_id) in self._tombstones
            if tombstoned or not self._disk_changed(st):
                continue
            self._refresh_local_content(st)
            result['updated'] += 1
        # db 残留 pending（上次进程退出前未算完哈希）：按原身份重算，
        # 否则 pending 不进快照也无人拉取，该文件永久不可见。
        for st in states:
            if (st.exists and not st.is_dir and st.hash_state == 'pending'
                    and st.name in disk_files):
                with self._lock:
                    tombstoned = (st.name, st.file_id) in self._tombstones
                if not tombstoned:
                    self._rehash_existing(st)
        return result

    def _realign_type(self, name: str, disk_is_dir: bool):
        """磁盘类型与状态表不符：先删旧身份，再按真实类型重建并广播。

        旧对象的类型以状态表为准（不看磁盘当前类型），这样文件换目录时
        生成的是文件墓碑而不是带 ID 的目录墓碑；新对象按磁盘类型重新登记。
        """
        prev = self.get_state(name)
        self._notify_log(
            f"磁盘类型变化，重建状态: {name} -> "
            f"{'目录' if disk_is_dir else '文件'}")
        if prev is not None and prev.exists:
            self.emit(self.OP_DELETE, name)
        if disk_is_dir:
            self.emit(self.OP_DIR_CREATE, name)
        else:
            self.emit(self.OP_ADD, name)

    def _rehash_existing(self, st: FileState):
        """重启对账：复用原 file_id/版本重算哈希，ok 后按原身份发布。"""
        if self._hasher is None:
            return
        signal = {'src_id': self.end_id, 'op_no': st.op_no, 'op': self.OP_ADD,
                  'file': st.name, 'state': STATE_ADD, 'clock': st.clock,
                  'ts': st.ts, 'vv': dict(st.vv)}
        if st.file_id:
            signal['id'] = st.file_id
        self._enqueue_hash(st, 'local_add', {'signal': signal})

    def _path_kind(self, name: str) -> Optional[str]:
        """按路径核对条目类型：'file' / 'dir' / 'missing'；None = 无法判定。

        任一异常都不得推断类型：读不到时返回 None，由调用方跳过本轮。
        """
        try:
            path = self._safe_join(name)
        except ValueError:
            return None
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return 'missing'
        except OSError:
            return None
        return 'dir' if stat.S_ISDIR(info.st_mode) else 'file'

    def _disk_changed(self, st: FileState) -> bool:
        """盘上 size/mtime 是否偏离 db 快照（§6 同 ID 更新判定）。"""
        try:
            stat = os.stat(self._safe_join(st.name))
        except (OSError, ValueError):
            return False
        with self._lock:
            meta = self._db_meta.get(st.name)
        if meta is None:
            return False
        db_size, db_mtime = meta
        if db_size is not None and int(db_size) != int(stat.st_size):
            return True
        return bool(db_mtime) and abs(float(db_mtime) - stat.st_mtime) > 1e-3

    def _refresh_local_content(self, st: FileState):
        """启动对账：同 ID 内容变化 → op_no+1、hash 置 pending 并重算发布。"""
        if self._hasher is not None:
            self._hasher.cancel(st.name)
        with self._lock:
            self._hash_pending.pop(st.name, None)
            prev = self._states.get(st.name)
        op_no, clock = self._bump_version(st.name, prev)
        try:
            disk_stat = os.stat(self._safe_join(st.name))
            ts, size = disk_stat.st_mtime, int(disk_stat.st_size)
        except (OSError, ValueError):
            ts, size = time.time(), int(st.size or 0)
        vv = dict(prev.vv) if prev else {}
        vv[self.end_id] = op_no
        new_st = FileState(name=st.name, op_no=op_no, state=STATE_CHANGE,
                           exists=True, clock=clock, ts=ts, vv=vv,
                           file_id=st.file_id, hash='', hash_state='pending',
                           size=size)
        self._store(new_st)
        if self._hasher is not None:
            signal = {'src_id': self.end_id, 'op_no': op_no, 'op': self.OP_ADD,
                      'file': st.name, 'state': STATE_ADD, 'clock': clock,
                      'ts': ts, 'vv': vv}
            if st.file_id:
                signal['id'] = st.file_id
            self._enqueue_hash(new_st, 'local_add', {'signal': signal})

    # ---- 墓碑与已知端 ----

    def tombstones(self) -> list:
        with self._lock:
            return [dict(rec) for rec in self._tombstones.values()]

    def wire_tombstones(self) -> list:
        """可下发墓碑：仅 origin=delete（merge 只做本地压制）。"""
        with self._lock:
            return [dict(rec) for rec in self._tombstones.values()
                    if rec.get('origin') == 'delete']

    def has_tombstone(self, path: str, file_id: str) -> bool:
        with self._lock:
            return (path, file_id or '') in self._tombstones

    def tombstone_for_path(self, path: str) -> bool:
        """该路径是否存在任一墓碑（文件按任意 ID / 目录 id=''）。"""
        with self._lock:
            return any(p == path for (p, _fid) in self._tombstones)

    def adopt_remote_state(self, remote: FileState):
        """静默采纳对端同内容对象：绑定对端 file_id/版本，不广播（阶段 D）。"""
        if remote is None:
            return
        try:
            base = int(remote.vv.get(self.end_id, 0) or 0)
        except (TypeError, ValueError):
            base = 0
        self.recover_floor(remote.name, base, remote.clock)
        self._store(FileState(
            name=remote.name, op_no=remote.op_no, state=STATE_ADD,
            exists=True, clock=remote.clock, ts=remote.ts, vv=dict(remote.vv),
            file_id=remote.file_id, hash=remote.hash, hash_state='ok',
            size=int(remote.size or 0), is_dir=bool(remote.is_dir)))

    def publish_residue(self, path: str, file_id: str, hash_value: str,
                        size: int) -> Optional[dict]:
        """静默对齐确认本端独有新增：写 hash=ok 状态并广播一次 add。"""
        with self._lock:
            prev = self._states.get(path)
        op_no, clock = self._bump_version(path, prev)
        ts = time.time()
        vv = dict(prev.vv) if prev else {}
        vv[self.end_id] = op_no
        self._store(FileState(
            name=path, op_no=op_no, state=STATE_ADD, exists=True,
            clock=clock, ts=ts, vv=vv, file_id=file_id or '',
            hash=hash_value or '', hash_state='ok', size=int(size or 0)))
        signal = {
            'src_id': self.end_id, 'op_no': op_no, 'op': self.OP_ADD,
            'file': path, 'state': STATE_ADD, 'clock': clock, 'ts': ts,
            'vv': vv, 'id': file_id or '', 'hash': hash_value or '',
        }
        self._broadcast_signal(signal)
        return signal

    def known_peers(self) -> set:
        with self._lock:
            return set(self._peers)

    def register_peer(self, peer_id: str):
        if not peer_id or peer_id == self.end_id:
            return
        with self._lock:
            self._peers.add(peer_id)
        if self._storage is not None:
            self._storage.add_peer(peer_id)

    def confirm_tombstone(self, path: str, file_id: str, peer_id: str) -> bool:
        """登记某已知端已确认该 (path, id) 删除；重复确认返回 False。"""
        key = (path, file_id or '')
        with self._lock:
            rec = self._tombstones.get(key)
            if rec is None:
                return False
            confirmed = rec.setdefault('confirmed_by', [])
            if peer_id in confirmed:
                return False
            confirmed.append(peer_id)
            rec = dict(rec)
            self._tombstones[key] = rec
        if self._storage is not None:
            self._storage.confirm_tombstone(path, key[1], peer_id)
        return True

    def gc_tombstones(self) -> int:
        """清理全体已知端已确认且无同 id 活对象的删除墓碑。"""
        if self._storage is not None:
            removed = self._storage.gc_tombstones()
            if removed:
                with self._lock:
                    self._tombstones = self._storage.load_tombstones()
            return removed
        # 内存模式（无 db 降级场景）同样执行 §4 回收，避免墓碑无界增长
        with self._lock:
            peers = set(self._peers)
            if not peers:
                return 0
            active = {st.file_id for st in self._states.values() if st.file_id}
            keys = [key for key, rec in self._tombstones.items()
                    if rec.get('origin') == 'delete'
                    and set(rec.get('confirmed_by') or ()) >= peers
                    and (rec.get('file_id') or '') not in active]
            for key in keys:
                self._tombstones.pop(key, None)
        return len(keys)

    def record_merge_tombstone(self, path: str, file_id: str):
        """身份合并败者的本地墓碑：压制旧 ID，不下发、不删磁盘。"""
        self._record_tombstone(path, file_id, origin='merge')

    def merge_remote_identity(self, path: str, remote: FileState) -> bool:
        """§5.2：同 path 同 hash 不同 ID → 按代次合并身份（不改内容、不广播）。

        由状态应答通道调用：两端各自与对端状态合并后收敛到同一胜者 ID，
        避免“同内容不同 ID”长期并存导致后续删除信号被 ID 精确匹配拦下。
        """
        with self._lock:
            local = self._states.get(path)
        if local is None or not local.exists or not local.file_id:
            return False
        if not remote.file_id or local.file_id == remote.file_id:
            return False
        self._merge_same_content(path, local, remote)
        with self._lock:
            cur = self._states.get(path)
        return cur is not None and cur.file_id != local.file_id

    def _merge_same_content(self, file: str, local: FileState,
                            incoming: FileState):
        """§5.2：同 path 同 hash 不同 ID → 代次高者成为唯一活对象。

        败者 ID 记 merge 墓碑（只做本地压制、不下发、不删文件）。本端胜出时
        不改状态；对端胜出时仅替换状态表身份，内容相同无需传输。
        """
        if (not local.file_id or not incoming.file_id
                or local.file_id == incoming.file_id):
            return
        if local.is_dir or incoming.is_dir or local.hash_state == 'none':
            # 目录不参与身份合并（旧版本可能给目录发过 add 留下 ID）
            return
        winner = pick_same_content_winner(
            {'id': local.file_id, 'clock': local.clock, 'ts': local.ts},
            {'id': incoming.file_id, 'clock': incoming.clock,
             'ts': incoming.ts})
        if winner.get('id') == local.file_id:
            self.record_merge_tombstone(file, incoming.file_id)
            return
        self.record_merge_tombstone(file, local.file_id)
        self._store(FileState(
            name=file, op_no=local.op_no, state=local.state, exists=True,
            clock=local.clock, ts=local.ts, vv=dict(local.vv),
            file_id=incoming.file_id, hash=local.hash, hash_state='ok',
            size=local.size, is_dir=local.is_dir))
        self._notify_log(f"同内容身份合并: {file} "
                         f"{local.file_id[:8]}→{incoming.file_id[:8]}")

    def apply_tombstone(self, path: str, file_id: str, is_dir: bool = False,
                        origin: str = 'delete', op_no: int = 0,
                        clock: int = 0, ts: float = 0.0,
                        vv: dict = None) -> bool:
        """应用一条删除墓碑：文件必须「路径 + 文件ID」双对应才删盘。

        文件 ID 是对象的唯一身份。缺 ID、或 ID 与本地活对象不一致时，既不能
        证明是同一对象，也不允许按路径删同名文件——只记录墓碑。目录没有身份，
        只清理由本端已知身份的子孙对象，并移除空目录，不整树删除。
        """
        with self._lock:
            st = self._states.get(path)
        removed = False
        if origin == 'delete' and is_dir:
            # 目录没有身份，file_id 判不出“已重建”。只要目录下仍有活子条目，
            # 说明它已被子文件重建：重放旧删除墓碑时不得整树删掉（否则与
            # 对端拉取形成“删除-重建-再删除”循环）。子墓碑会各自删子条目，
            # 目录本身留待补删在无活子条目后清理。
            with self._lock:
                rebuilt = any(name.startswith(path + '/') and child.exists
                              for name, child in self._states.items())
            if rebuilt:
                self._record_tombstone(path, file_id, is_dir=True,
                                       origin=origin, op_no=op_no, clock=clock,
                                       ts=ts, vv=vv)
                return False
        if origin == 'delete' and not is_dir:
            if not file_id:
                self._notify_log(f"拒绝无效删除墓碑（缺文件ID）: {path}")
                return False
            if st is None or not st.file_id or st.file_id != file_id:
                # 身份不成立：可能是同一路径的新对象，也可能是本端从未持有
                # 该 ID 的对象。两种情况都不能删盘，只保留墓碑用于去重。
                self._record_tombstone(path, file_id, is_dir=False,
                                       origin=origin, op_no=op_no, clock=clock,
                                       ts=ts, vv=vv)
                return False
        if origin == 'delete':
            if is_dir:
                removed = self._delete_known_subtree(path)
            else:
                try:
                    self._delete_local(path)
                    removed = True
                except OSError as e:
                    self._notify_log(f"本地删除失败，仍记录墓碑: {path}: {e}")
            if st is not None and st.exists:
                op, ck = self._bump_version(path, st)
                t = float(ts or time.time())
                vv2 = dict(vv or {})
                vv2[self.end_id] = op
                self._store(FileState(name=path, op_no=op,
                                      state=STATE_CHANGE, exists=False,
                                      clock=ck, ts=t, vv=vv2,
                                      file_id=st.file_id, is_dir=is_dir))
                if is_dir:
                    self._tombstone_children(path, op, ck, t, vv2)
        self._record_tombstone(path, file_id, is_dir=is_dir, origin=origin,
                               op_no=op_no, clock=clock, ts=ts, vv=vv)
        return removed

    def _record_tombstone(self, path: str, file_id: str = '',
                          is_dir: bool = False, origin: str = 'delete',
                          op_no: int = 0, clock: int = 0, ts: float = 0.0,
                          vv: dict = None):
        if is_dir:
            # 目录没有身份：墓碑统一用空 ID。读取侧（祖先拦截、收敛清理、
            # 对端去重）都只按 (path, '') 查找，带 ID 会让它们全部失效。
            file_id = ''
        if origin == 'delete' and not is_dir and not file_id:
            # 文件身份必然存在：缺 ID 的删除指令不成立，记录它只会在重放时
            # 按路径误删同名对象。
            self._notify_log(f"拒绝无效删除墓碑（缺文件ID）: {path}")
            return
        key = (path, file_id or '')
        with self._lock:
            prev = self._tombstones.get(key)
            rec = dict(prev) if prev else {
                'rel_path': key[0], 'file_id': key[1], 'is_dir': 1,
                'origin': origin, 'op_no': 0, 'clock': 0, 'ts': 0.0,
                'vv': {}, 'deleted_at': time.time(), 'confirmed_by': [],
            }
            if prev is not None:
                rec['origin'] = prev.get('origin') or 'delete'
            rec['is_dir'] = 1 if is_dir else 0
            if op_no:
                rec['op_no'] = int(op_no)
                rec['clock'] = int(clock or 0)
                rec['ts'] = float(ts or 0.0)
                rec['vv'] = dict(vv or {})
                rec['deleted_at'] = time.time()
            self._tombstones[key] = rec
            total = len(self._tombstones)
        if self._storage is None:
            if total > self.TOMBSTONE_LIMIT:
                with self._lock:
                    self._evict_tombstones(self.TOMBSTONE_LIMIT)
            return
        self._storage.put_tombstone(rec)
        if total > self._storage.TOMBSTONE_LIMIT:
            self._storage.enforce_tombstone_limit()
            with self._lock:
                self._tombstones = self._storage.load_tombstones()

    def _evict_tombstones(self, limit: int) -> int:
        """内存模式超限淘汰（调用方需持有锁）：优先 merge，再淘汰最旧删除墓碑。"""
        excess = len(self._tombstones) - int(limit)
        if excess <= 0:
            return 0
        rows = sorted(self._tombstones.items(),
                      key=lambda kv: (0 if kv[1].get('origin') == 'merge' else 1,
                                      kv[1].get('deleted_at') or 0.0))
        evicted = rows[:excess]
        for key, _rec in evicted:
            self._tombstones.pop(key, None)
        self._notify_log(f"墓碑超限淘汰 {excess} 条: {[k[0] for k, _ in evicted]}")
        return excess

    def _persist_state(self, st: FileState):
        """把一条活/删状态写回 db（删态只移除 files 行，墓碑另行记录）。"""
        storage = self._storage
        if storage is None:
            return
        storage.put_counter(st.name, st.op_no, st.clock)
        if not st.exists:
            storage.remove_file(st.name)
            with self._lock:
                self._db_meta.pop(st.name, None)
            return
        is_dir = bool(st.is_dir or (st.hash_state == 'none' and not st.file_id))
        size = None if is_dir else int(st.size or 0)
        mtime = float(st.ts or 0.0)
        if not is_dir:
            # mtime 取落库时的真实磁盘值：拉取文件的 st.ts 是对端 mtime，
            # 与本地落盘时间不同，直接沿用会让每次重启都误判为已变化。
            try:
                disk_stat = os.stat(self._safe_join(st.name))
                size, mtime = int(disk_stat.st_size), disk_stat.st_mtime
            except (OSError, ValueError):
                pass
        storage.put_file({
            'rel_path': st.name,
            'file_id': st.file_id or None,
            'is_dir': 1 if is_dir else 0,
            'op_no': st.op_no, 'clock': st.clock, 'ts': st.ts, 'vv': st.vv,
            'hash': st.hash, 'hash_state': st.hash_state,
            'size': size, 'mtime': mtime,
        })
        with self._lock:
            self._db_meta[st.name] = (size, mtime)

    def _bump_version(self, path: str, prev: Optional[FileState]):
        """路径版本地板 +1（不广播，供启动对账/墓碑应用使用）。"""
        with self._lock:
            floor_op = self._op_counters.get(path, 0)
            floor_ck = self._clocks.get(path, 0)
            if prev is not None:
                floor_op = max(floor_op, prev.op_no,
                               prev.vv.get(self.end_id, 0))
                floor_ck = max(floor_ck, prev.clock)
            op_no, clock = floor_op + 1, floor_ck + 1
            self._op_counters[path] = op_no
            self._clocks[path] = clock
        return op_no, clock

    def recover_floor(self, path: str, op_no: int, clock: int = 0):
        """用对端快照里的 vv[本端] 抬升计数器基线（只抬地板，不改状态表 vv）。

        接收端不把对端 vv 并入本地状态表，避免虚增知识造成字节分叉；仅保证
        本端后续操作的序号严格大于对端已知版本。
        """
        op_no = int(op_no or 0)
        if op_no <= 0:
            return
        with self._lock:
            if self._op_counters.get(path, 0) < op_no:
                self._op_counters[path] = op_no
            if clock and self._clocks.get(path, 0) < int(clock):
                self._clocks[path] = int(clock)

    def _file_id_for_content(self, path: str) -> str:
        """新增/修改取身份：同一活对象沿用，删除后重建换新 ID。"""
        with self._lock:
            prev = self._states.get(path)
        if prev is not None and prev.file_id and (prev.exists
                                                  or prev.state == STATE_ADD):
            return prev.file_id
        return new_file_id()

    def _mark_local_deleted(self, path: str, file_id: str, is_dir: bool):
        if self._hasher is not None:
            self._hasher.cancel(path)
        with self._lock:
            self._hash_pending.pop(path, None)
            prev = self._states.get(path)
        op_no, clock = self._bump_version(path, prev)
        ts = time.time()
        vv = dict(prev.vv) if prev else {}
        vv[self.end_id] = op_no
        fid = file_id or (prev.file_id if prev else '')
        if not is_dir and not fid:
            self._notify_log(f"启动对账删除缺少文件ID（对象未登记）: {path}")
        self._store(FileState(name=path, op_no=op_no, state=STATE_CHANGE,
                              exists=False, clock=clock, ts=ts, vv=vv,
                              file_id=fid))
        self._record_tombstone(path, fid, is_dir=is_dir, origin='delete',
                               op_no=op_no, clock=clock, ts=ts, vv=vv)

    def _register_new_local(self, path: str, is_dir: bool):
        with self._lock:
            prev = self._states.get(path)
        op_no, clock = self._bump_version(path, prev)
        size = 0
        if not is_dir:
            try:
                size = os.path.getsize(self._safe_join(path))
            except (OSError, ValueError):
                size = 0
        self._store(FileState(
            name=path, op_no=op_no, state=STATE_ADD, exists=True,
            clock=clock, ts=time.time(), vv={self.end_id: op_no},
            file_id='' if is_dir else new_file_id(), hash='',
            hash_state='none' if is_dir else 'pending', size=size,
            is_dir=is_dir))
        # 阶段 B：补扫新增同样先算哈希，ok 后才发布 add
        if not is_dir and self._hasher is not None:
            st = self.get_state(path)
            signal = {'src_id': self.end_id, 'op_no': op_no,
                      'op': self.OP_ADD, 'file': path, 'state': STATE_ADD,
                      'clock': clock, 'ts': st.ts if st else time.time(),
                      'vv': dict(st.vv) if st else {self.end_id: op_no}}
            self._enqueue_hash(st, 'local_add', {'signal': signal})

    def _scan_disk(self):
        """扫描同步目录，返回 (files, dirs, complete)。

        complete=False 表示扫描结果不可信（根目录缺失/不可读、子目录枚举
        失败）：调用方不得把「未扫到」当作删除依据，否则挂载点临时消失、
        权限抖动都会被误判为文件已删除并广播删除墓碑。
        """
        files, dirs = set(), set()
        if not os.path.isdir(self.sync_folder):
            return files, dirs, False
        failed = []

        def _on_error(err):
            failed.append(err)

        for root, subdirs, names in os.walk(self.sync_folder, onerror=_on_error):
            keep = []
            for d in subdirs:
                if d.startswith('.') or self._is_temp_name(d):
                    continue
                keep.append(d)
                full = os.path.join(root, d)
                dirs.add(os.path.relpath(full, self.sync_folder)
                         .replace('\\', '/'))
            subdirs[:] = keep
            for fn in names:
                if self._is_temp_name(fn):
                    continue
                full = os.path.join(root, fn)
                files.add(os.path.relpath(full, self.sync_folder)
                          .replace('\\', '/'))
        return files, dirs, not failed

    @staticmethod
    def _is_temp_name(name: str) -> bool:
        return name.startswith('.tcp_') and name.endswith('.part')

    def _path_is_dir(self, name: str, prev: Optional[FileState] = None) -> bool:
        # 状态表是类型真相：有活条目就按它判断。磁盘类型只用于无状态场景，
        # 不一致时由启动对账的 _realign_type 修正，不在这里按磁盘反推。
        if prev is not None:
            if prev.exists:
                return bool(prev.is_dir)
            if prev.is_dir:
                return True
        try:
            if os.path.isdir(self._safe_join(name)):
                return True
        except ValueError:
            return False
        if prev is not None and prev.file_id:
            return False
        pfx = name.rstrip('/') + '/'
        with self._lock:
            if any(n.startswith(pfx) for n in self._states):
                return True
        return bool(prev is not None and prev.hash_state == 'none')

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
        if op == self.OP_ADD and self._path_is_dir(file):
            # 目录没有身份：界面把文件夹当“添加”上报时归一到 dir_create。
            # 否则目录会拿到 file_id，删除/重建后与墓碑身份错位，且对账时
            # 会被当成“同内容不同 ID”反复合并身份。
            op = self.OP_DIR_CREATE
        with self._lock:
            prev = self._states.get(file)
            # 版本地板：op_no/clock 必须严格超过本端该路径已记录的版本。目录删除时
            # _tombstone_children 会把父目录版本复制给每个子路径，子路径自身计数器
            # 低于该版本；不取地板则重建子路径会发出与墓碑相等/更低的 op_no，被对端
            # 按重复丢弃——删除收敛前整棵子树的重建都不同步。
            floor_op = self._op_counters.get(file, 0)
            floor_ck = self._clocks.get(file, 0)
            if prev is not None:
                floor_op = max(floor_op, prev.op_no, prev.vv.get(self.end_id, 0))
                floor_ck = max(floor_ck, prev.clock)
            op_no = floor_op + 1
            clock = floor_ck + 1
            self._op_counters[file] = op_no
            self._clocks[file] = clock
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
        # 阶段 B：删除/改名先撤销在途哈希，防止旧路径结果迟到发布
        if self._hasher is not None and op in (self.OP_DELETE, self.OP_RENAME,
                                               self.OP_MOVE):
            self._hasher.cancel(file)
            if old is not None:
                self._hasher.cancel(old)
            with self._lock:
                self._hash_pending.pop(file, None)
                if old is not None:
                    self._hash_pending.pop(old, None)
        self._record_local(signal)
        st = self.get_state(file)
        if st is not None:
            signal['id'] = st.file_id
            signal['hash'] = st.hash if st.hash_state == 'ok' else ''
        if (op == self.OP_DELETE and st is not None and not st.is_dir
                and not st.file_id):
            # 删除必须携带文件 ID；没有身份就不构成删除指令，不广播
            self._notify_log(f"本地删除缺少文件ID，拒绝广播: {file}")
            return signal
        # 阶段 B 门禁：文件 add 先算哈希，pending 期间不广播
        if self._hasher is not None and op == self.OP_ADD:
            if st is not None and st.hash_state == 'pending' \
                    and self._enqueue_hash(st, 'local_add', {'signal': signal}):
                return signal
        self._broadcast_signal(signal)
        return signal

    def remove_state(self, file: str):
        """移除端内文件状态条目（自同步链路列表收敛：双方均已删除的文件）。

        保留 _op_counters/_clocks（会话内按路径单调）：对端墓碑的收敛清理各端
        独立、可能滞后，本地计数若归零，删除后同名重建会复用低 op_no/clock，
        被对端墓碑去重丢弃或被其反向删除，形成删除-重建震荡。
        """
        with self._lock:
            self._states.pop(file, None)
        if self._storage is not None:
            self._storage.remove_file(file)

    def retry_pending_deletes(self, limit: int = 1) -> int:
        """补删磁盘残留：只处理「墓碑 ID == 本地删态 ID」的同一对象。

        删除必须身份对应：拿不到本地同 ID 的删态就不补删。重启后 files 行
        随删除移除，无法再证明磁盘残留是当初那个对象（可能是之后新建的同名
        文件），因此不再按路径清理——残留由墓碑挡住复活，不参与同步。
        每拍至多处理 limit 个（限速，防长阻塞/日志刷屏）。Returns: 成功删除数。
        """
        done = 0
        with self._lock:
            candidates = [(path, rec) for (path, _fid), rec
                          in self._tombstones.items()
                          if rec.get('origin') == 'delete']
        for file, rec in candidates:
            if done >= limit:
                break
            if not file:
                continue
            with self._lock:
                st = self._states.get(file)
                if (file in self._protected_dirs
                        or file in self._transferring):
                    continue  # 受保护目录 / 传输中：不碰
                if rec.get('is_dir'):
                    if st is not None and st.exists:
                        continue
                    if any(name.startswith(file + '/') and child.exists
                           for name, child in self._states.items()):
                        # 目录已被子文件重建：墓碑已过期，不能把活内容删掉
                        # （否则补删-重拉无限循环）。
                        continue
                else:
                    fid = rec.get('file_id') or ''
                    if st is None or st.exists or not fid or st.file_id != fid:
                        # 无同 ID 删态 = 身份不成立，不按路径删盘
                        continue
            try:
                path = self._safe_join(file)
            except ValueError:
                continue
            if not os.path.exists(path):
                continue
            try:
                if rec.get('is_dir'):
                    removed = self._remove_empty_dir(file)
                else:
                    self._delete_local(file)
                    removed = not os.path.exists(path)
            except OSError:
                continue
            if not removed:
                continue  # 仍失败（句柄未释放/目录非空）→ 下一拍再试
            done += 1
            self._notify_log(f"补删磁盘残留: {file}")
        return done

    def emit_pulled(self, file: str, remote_src_id: str, remote_op_no: int,
                    remote_clock: int = 0, remote_ts: float = 0.0,
                    on_done=None, file_hash: str = '', file_size: int = 0,
                    remote_id: str = ''):
        """自同步拉取完成：记录本端已持有该文件并广播 add 状态（阶段 2）。

        本地 FS 已由 pull_file 落盘并校验 MD5+size。状态表 vv 推进到远端 op_no，
        clock/ts 取拉取版本的仲裁知识指纹——即当前仲裁胜者，状态表不回退。

        广播回执 vv 仅含本端自 op（不携带远端 src 版本）：回执携带 src 版本
        会虚增他端知识——他端据此把胜者真实信号误判为「已应用」而去重，
        永不触发冲突拉取 → 字节分叉永久定格（本环境复现过的核心竞态）。
        """
        with self._lock:
            op_no = self._op_counters.get(file, 0) + 1
            self._op_counters[file] = op_no
            prev = self._states.get(file)
        ts = float(remote_ts or 0.0) or time.time()
        clock = int(remote_clock or 0)
        # 防胜者端被低版本拉取降级：仅当拉取版本严格强于本端状态时才采纳
        if prev is not None:
            if clock < prev.clock or (clock == prev.clock and ts < prev.ts):
                clock = prev.clock
                ts = prev.ts
        self._clocks[file] = max(self._clocks.get(file, 0), clock)
        # 拉取落盘后身份以目标对象为准：远端 id 优先（删除后重建、id 变更仲裁
        # 胜出都靠它收敛）；老协议无 id 时才沿用本端活对象身份。
        if remote_id:
            fid = remote_id
        elif prev is not None and prev.file_id \
                and (prev.exists or prev.state == STATE_ADD):
            fid = prev.file_id
        else:
            fid = new_file_id()
        st = FileState(name=file, op_no=op_no, state=STATE_ADD, exists=True,
                       clock=clock, ts=ts,
                       vv={self.end_id: op_no, remote_src_id: remote_op_no},
                       file_id=fid,
                       hash=file_hash or '',
                       hash_state=('ok' if file_hash else 'pending'),
                       size=int(file_size or 0))
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
        # 阶段 B：拉取落盘同样先算哈希，pending 期间不发 add/不发 pull_done
        if self._hasher is not None and st.hash_state == 'pending' \
                and self._enqueue_hash(st, 'pulled',
                                       {'signal': signal, 'on_done': on_done}):
            return signal
        self._broadcast_signal(signal)
        self._notify_applied(signal)
        if on_done is not None:
            try:
                on_done(True)
            except Exception:
                pass
        return signal

    # ---- 投递（阶段 5 起）：复制信号改走分发链路，不经主机转发 ----

    def emit_files_notify(self, notify_dict: dict) -> bool:
        """本地复制文件 → 投递通知沿网状直连广播（纯 mesh，不经主机）。

        文件字节仍由接收端端到端直连复制端 FileProvider 拉取（不动）。
        返回 True 表示已发出；mesh 未就绪 / 无直连对端 / 全部发送失败均返回
        False，调用方据 False 记日志（不回退主机）。
        """
        if self.mesh is None:
            return False
        if not self.mesh.connected_end_ids():
            return False
        try:
            sent = self.mesh.send_to_all(
                Protocol.create_clipboard_notify_signal(notify_dict),
                except_end_id=self.end_id)
        except Exception as e:
            self._notify_log(f"投递通知广播失败: {e}")
            return False
        if not sent:
            self._notify_log("投递通知广播失败: 无可达对端")
            return False
        return True

    def emit_clipboard_text(self, mime_type: str, data: bytes) -> bool:
        """本地复制文本 → 文本投递沿网状直连广播（纯 mesh，不经主机）。

        返回 True 表示已发出；mesh 未就绪 / 无直连对端 / 全部发送失败均返回
        False，调用方据 False 记日志（不回退主机）。
        """
        if self.mesh is None or not data:
            return False
        if not self.mesh.connected_end_ids():
            return False
        try:
            sent = self.mesh.send_to_all(
                Protocol.create_clipboard_text_signal(mime_type, data),
                except_end_id=self.end_id)
        except Exception as e:
            self._notify_log(f"文本投递广播失败: {e}")
            return False
        if not sent:
            self._notify_log("文本投递广播失败: 无可达对端")
            return False
        return True

    def on_clipboard_text(self, mime_type: str, data):
        """收到对端文本投递（网状直连）：转交 UI 写系统剪贴板。

        双通道：回调（mesh 连接线程直接调用，测试/无事件循环场景）+ Qt 信号
        （GUI 主线程）。文本投递是一次性广播（接收端不转发），不进文件状态/
        冲突裁决，与同步信号（需洪泛去重）语义不同。
        """
        if mime_type != 'text' or not isinstance(data, (bytes, bytearray)) or not data:
            return
        payload = bytes(data)
        if self._clipboard_text_handler:
            try:
                self._clipboard_text_handler(mime_type, payload)
            except Exception:
                pass
        try:
            self.clipboard_text_received.emit(mime_type, payload)
        except Exception:
            pass

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
            # 例外：本端为未验证副本且该信号带内容证据 → 仍需处理，用于补拉校正
            if not (st.hash_state != 'ok' and signal.get('hash')):
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
                # 未验证副本 + 信号带内容证据：放行以触发补拉校正
                if not (st.hash_state != 'ok' and signal.get('hash')):
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
                             clock=clock, ts=ts, vv=vv,
                             file_id=signal.get('id', '')
                             or signal.get('file_id', '') or '',
                             hash=signal.get('hash', '') or '',
                             hash_state=('ok' if signal.get('hash') else 'pending'))
        # 三层冲突裁决
        with self._lock:
            local = self._states.get(file)
        if local is not None and op != self.OP_DELETE:
            # 删除不参与版本仲裁：文件 ID 是唯一身份，ID 对应即删（由下方
            # 删除分支精确匹配）。若先走四层裁决，回归端拉取后继承的版本
            # 高于删除信号版本时会判「本地胜出」，删除永远传播不出去。
            if pulled:
                # 拉取回执：仅合并对端知识，不应用、不覆盖（防回声风暴）
                self._record_knowledge(file, vv)
                self._forward(signal, src_id)
                return
            # C3：ADD 携带 hash 且与本端已算好的内容一致 → 同内容，仅合并身份
            if (op == self.OP_ADD and local.exists
                    and local.hash_state == 'ok' and local.hash
                    and signal.get('hash') == local.hash):
                self._merge_same_content(file, local, incoming)
                self._record_knowledge(file, vv)
                self._forward(signal, src_id)
                return
            result = compare_states(incoming, local, src_id, self.end_id)
            if (result == 0 and local.exists and local.hash_state != 'ok'
                    and incoming.hash_state == 'ok'
                    and incoming.hash != local.hash):
                # 同版本但本端是无内容证据的副本（元数据先到、字节未到）→
                # 借源端迟到的哈希信号补拉校正，避免永久分叉
                self._request_conflict_pull(file, src_id)
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
        # 祖先目录已删（墓碑）→ 非删除信号不得重建其内部路径，改记同版本墓碑
        # 顶回（防残缺视图下 dir_create 的 os.makedirs 复活幽灵目录）。
        if op != self.OP_DELETE:
            anc = self._deleted_ancestor(file)
            if anc is not None:
                self._store(FileState(name=file, op_no=anc.op_no,
                                      state=STATE_CHANGE, exists=False,
                                      clock=anc.clock, ts=anc.ts, vv=dict(anc.vv)))
                self._forward(signal, src_id)
                return
        try:
            if op == self.OP_ADD:
                exists = os.path.exists(self._safe_join(file))
                # 竞态复核：入参 local 可能在 FS 操作期间被本端 emit 更新（写盘
                # 与工作线程并发）；若当前状态已不弱于远端，退回「仅记知识」，
                # 否则会把刚记录的本端新版本覆盖掉（丢更新 + 哈希结果作废）
                cur = self.get_state(file)
                if cur is not None and cur.exists \
                        and compare_states(incoming, cur, src_id,
                                           self.end_id) <= 0:
                    self._record_knowledge(file, vv)
                    self._forward(signal, src_id)
                    return
                cur_id = cur.file_id if cur is not None else ''
                if not cur_id and local is not None and local.exists:
                    cur_id = local.file_id
                # §5.2 胜者 ID 成为该路径唯一活对象：远端内容胜出时采纳其身份，
                # 本端被覆盖的旧 ID 记 merge 墓碑（只做本地压制，不下发）。
                if cur_id and incoming.file_id and cur_id != incoming.file_id:
                    self.record_merge_tombstone(file, cur_id)
                    self._notify_log(
                        f"本地身份被远端覆盖: {file} {cur_id[:8]}→{incoming.file_id[:8]}")
                fid = incoming.file_id or cur_id or new_file_id()
                self._store(FileState(name=file, op_no=op_no, state=STATE_ADD,
                                      exists=exists, clock=clock, ts=ts,
                                      vv=vv, file_id=fid, hash='',
                                      hash_state='pending'))
            elif op == self.OP_DELETE:
                # 删除留「墓碑」（exists=False）：源端延迟的 dir_create/add 信号到达时
                # 本端 state.exists=False 判本地胜出，只记知识并转发、不再重建；他端据
                # 转发收敛。否则会把已删目录/文件重建出幽灵条目。
                sig_id = incoming.file_id
                is_dir = self._path_is_dir(file, local)
                if is_dir:
                    sig_id = ''  # 目录无身份，不做 ID 精确匹配
                elif not sig_id:
                    # 缺 ID 的删除指令不成立：不删本地、不建墓碑、不再转发
                    self._notify_log(f"删除信号缺少文件ID，已忽略: {file}")
                    return
                elif not (local is not None and local.exists
                          and local.file_id and local.file_id == sig_id):
                    # 身份不对应：本地是另一个活对象（可能同路径重建），或本端
                    # 从未持有该 ID。按路径删盘会误删同名新文件，因此只记录
                    # 删除知识/墓碑并转发，保留本地内容。
                    if local is not None and local.exists:
                        self._notify_log(f"删除信号身份不匹配，保留本地对象: {file}")
                    else:
                        self._store(FileState(name=file, op_no=op_no,
                                              state=STATE_CHANGE, exists=False,
                                              clock=clock, ts=ts, vv=vv,
                                              file_id=sig_id, is_dir=False))
                    self._record_tombstone(file, sig_id, is_dir=False,
                                           origin='delete', op_no=op_no,
                                           clock=clock, ts=ts, vv=vv)
                    self._forward(signal, src_id)
                    return
                fid = sig_id or (local.file_id if local is not None else '')
                try:
                    if is_dir:
                        # 目录删除只清本端已知身份的子孙，再移除空目录；
                        # 未知身份的同名文件不得按路径整树删除。
                        self._delete_known_subtree(file)
                    else:
                        self._delete_local(file)
                except OSError as e:
                    # 目录内有在途拉取的 .part 被占用（WinError 32）等：磁盘删不掉也
                    # 必须记录墓碑并转发，否则删除知识丢失、对端持续供该条目，删除
                    # 永不收敛（幽灵条目反复复活）。
                    self._notify_log(f"本地删除失败，仍记录删除: {file}: {e}")
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=False, clock=clock, ts=ts,
                                      vv=vv, file_id=fid, is_dir=is_dir))
                self._tombstone_children(file, op_no, clock, ts, vv)
                self._record_tombstone(file, fid, is_dir=is_dir, origin='delete',
                                       op_no=op_no, clock=clock, ts=ts, vv=vv)
            elif op in (self.OP_RENAME, self.OP_MOVE):
                old_name = old or file
                if not self._rename_local(old_name, file):
                    return  # 源不存在（可能排在 delete 后）→ 跳过
                prev = self._states.get(old_name)
                vv_merged = dict(prev.vv) if prev else {}
                vv_merged = merge_vv(vv_merged, vv)
                is_dir_old = (bool(prev.is_dir) if prev
                              else self._path_is_dir(file))
                # 内容随重命名移动（字节未变）：身份/哈希延续到新名；
                # 旧名未登记（无身份可延续）时给新名补发 ID，保证之后可删除
                fid = prev.file_id if prev else (
                    '' if is_dir_old else new_file_id())
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=True, clock=clock, ts=ts,
                                      vv=vv_merged, file_id=fid,
                                      hash=prev.hash if prev else '',
                                      hash_state=(prev.hash_state
                                                  if prev else 'pending'),
                                      size=prev.size if prev else 0,
                                      is_dir=is_dir_old))
                if old_name != file:
                    # 旧名留墓碑（变更 + 不存在）：替代直接移除旧条目。对端未收到
                    # rename 时其快照仍含旧名存在条目，本端若删掉旧名状态，会把
                    # 旧文件反向拉回（删除-拉取震荡）；留墓碑使对端据快照补删旧名，
                    # 双方均为「变更 + 不存在」后由对账清理条目收敛。
                    self._store(FileState(name=old_name, op_no=op_no,
                                          state=STATE_CHANGE, exists=False,
                                          clock=clock, ts=ts, vv=vv_merged,
                                          file_id=fid, is_dir=is_dir_old))
                    self._record_tombstone(old_name, fid, is_dir=is_dir_old,
                                           origin='delete', op_no=op_no,
                                           clock=clock, ts=ts, vv=vv_merged)
                    if is_dir_old:
                        # 目录移动：子树状态随目录迁移，身份/哈希/大小不变
                        self._move_descendants(old_name, file, op_no, clock,
                                               ts, vv_merged)
            elif op == self.OP_DIR_CREATE:
                os.makedirs(self._safe_join(file), exist_ok=True)
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=True, clock=clock, ts=ts,
                                      vv=vv, hash_state='none', is_dir=True))
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
                # 本端有状态：远端已在仲裁中胜出 → 请求胜方字节覆盖本地旧内容
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

    def _record_local(self, signal: dict, touch: bool = True):
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
        try:
            if op == self.OP_ADD:
                exists = os.path.exists(self._safe_join(file))
                # 本地 emit：磁盘 mtime 与声明 ts 对齐（拉取端据此识别陈旧源）
                try:
                    if touch:
                        os.utime(self._safe_join(file), (ts, ts))
                except OSError:
                    pass
                self._store(FileState(name=file, op_no=op_no, state=STATE_ADD,
                                      exists=exists, clock=clock, ts=ts,
                                      vv=vv,
                                      file_id=self._file_id_for_content(file),
                                      hash='', hash_state='pending'))
            elif op == self.OP_DELETE:
                is_dir = self._path_is_dir(file, prev)
                fid = prev.file_id if prev else ''
                if not is_dir and not fid:
                    # 文件必然有身份；走到这里说明它从未在状态表登记（bug）
                    self._notify_log(f"本地删除缺少文件ID（对象未登记）: {file}")
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=False, clock=clock, ts=ts,
                                      vv=vv, file_id=fid, is_dir=is_dir))
                self._tombstone_children(file, op_no, clock, ts, vv)
                self._record_tombstone(file, fid, is_dir=is_dir, origin='delete',
                                       op_no=op_no, clock=clock, ts=ts, vv=vv)
            elif op in (self.OP_RENAME, self.OP_MOVE):
                old = signal.get('old', file)
                old_st = self._states.get(old)
                is_dir_old = (bool(old_st.is_dir) if old_st
                              else self._path_is_dir(file))
                # 旧名有身份则原样沿用；旧名未登记时给新名补发 ID，
                # 否则新路径会留下无身份状态，后续删除无法表达。
                fid = old_st.file_id if old_st else (
                    '' if is_dir_old else new_file_id())
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=os.path.exists(self._safe_join(file)),
                                      clock=clock, ts=ts, vv=vv,
                                      file_id=fid,
                                      hash=old_st.hash if old_st else '',
                                      hash_state=(old_st.hash_state
                                                  if old_st else 'pending'),
                                      size=old_st.size if old_st else 0,
                                      is_dir=is_dir_old))
                if old and old != file:
                    # 旧名留墓碑（变更 + 不存在）：对端未收到 rename 时据快照补删
                    # 旧名，避免旧条目残留/反向拉回；双方均为「变更 + 不存在」
                    # 后由对账清理条目。
                    prev_old = self._states.get(old)
                    old_vv = dict(prev_old.vv) if prev_old else {}
                    old_vv = merge_vv(old_vv, vv)
                    old_id = prev_old.file_id if prev_old else ''
                    self._store(FileState(name=old, op_no=op_no,
                                          state=STATE_CHANGE, exists=False,
                                          clock=clock, ts=ts, vv=old_vv,
                                          file_id=old_id, is_dir=is_dir_old))
                    self._record_tombstone(old, old_id, is_dir=is_dir_old,
                                           origin='delete', op_no=op_no,
                                           clock=clock, ts=ts, vv=old_vv)
                    if is_dir_old:
                        # 目录移动：子树状态随目录迁移，身份/哈希/大小不变
                        self._move_descendants(old, file, op_no, clock, ts, vv)
            elif op == self.OP_DIR_CREATE:
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=True, clock=clock, ts=ts,
                                      vv=vv, hash_state='none', is_dir=True))
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
        """写入状态表：与既有条目合并 vv，保留本地计数单调。"""
        with self._lock:
            prev = self._states.get(new_state.name)
            if prev:
                new_state.vv = merge_vv(prev.vv, new_state.vv)
            self._states[new_state.name] = new_state
            # 本地计数步进到已应用版本（Lamport 收信步进）：不步进则后续本地操作的
            # clock 可能低于状态表，被对端更新的墓碑判「本地胜出」丢弃（他端删除后
            # 本端同名重建不同步）。
            self._clocks[new_state.name] = max(
                self._clocks.get(new_state.name, 0), new_state.clock)
            # 同一对象内容更新时身份不变（路径已删除后重建由调用方显式换新 ID）
            if not new_state.file_id and prev is not None and prev.exists:
                new_state.file_id = prev.file_id
            if remove:
                self._states.pop(remove, None)
        self._persist_state(new_state)

    def _tombstone_children(self, folder: str, op_no: int, clock: int, ts: float,
                            vv: dict):
        """删除目录时给其下所有既有条目留同版本墓碑（文件按 ID、子目录 id=''）。"""
        pfx = folder + '/'
        with self._lock:
            names = [n for n in self._states if n.startswith(pfx)]
        for n in names:
            with self._lock:
                child = self._states.get(n)
            fid = child.file_id if child else ''
            is_dir = bool(child and (child.is_dir or (not child.file_id
                                                      and child.hash_state == 'none')))
            self._store(FileState(name=n, op_no=op_no, state=STATE_CHANGE,
                                  exists=False, clock=clock, ts=ts,
                                  vv=dict(vv), file_id=fid))
            self._record_tombstone(n, fid, is_dir=is_dir, origin='delete',
                                   op_no=op_no, clock=clock, ts=ts, vv=vv)

    def _move_descendants(self, old: str, new: str, op_no: int, clock: int,
                          ts: float, vv: dict):
        """目录改名/移动：把 old/ 下的活条目迁到 new/，身份/哈希原样保留。

        目录本身由调用方记录；这里只迁子树。已删条目留在旧路径不动——旧目录
        墓碑（is_dir=True）会让离线端整树清理，无需逐条写子树墓碑。
        """
        pfx = old + '/'
        with self._lock:
            names = [n for n in self._states if n.startswith(pfx)]
        for name in names:
            with self._lock:
                child = self._states.get(name)
            if child is None or not child.exists:
                continue
            target = new + '/' + name[len(pfx):]
            moved = FileState(name=target, op_no=op_no, state=STATE_CHANGE,
                              exists=True, clock=clock, ts=ts,
                              vv=merge_vv(dict(child.vv), vv),
                              file_id=child.file_id, hash=child.hash,
                              hash_state=child.hash_state, size=child.size,
                              is_dir=child.is_dir)
            if self._storage is not None:
                # 先删旧行再写新行：files.file_id 唯一索引，同 ID 不能换路径并存
                self._storage.remove_file(name)
                with self._lock:
                    self._db_meta.pop(name, None)
            self._store(moved, remove=name)
            with self._lock:
                self._op_counters[target] = max(
                    self._op_counters.get(target, 0), op_no)
                self._clocks[target] = max(self._clocks.get(target, 0), clock)
            if child.hash_state == 'pending':
                # 在途哈希随旧路径作废：按新路径重排一次（不广播，算完靠快照收敛）
                self._enqueue_hash(moved, 'local_add', {})

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

    def _remove_empty_dir(self, file: str) -> bool:
        """只移除空目录；目录非空（含未知身份文件）时保留。"""
        if file in self._protected_dirs:
            return False
        try:
            path = self._safe_join(file)
        except ValueError:
            return False
        if not os.path.isdir(path):
            return False
        try:
            os.rmdir(path)
            return True
        except OSError:
            return False

    def _delete_known_subtree(self, folder: str) -> bool:
        """目录删除只清本端已知身份（有状态行）的子孙，再移除空目录。

        没有状态行的文件身份未知，不能按路径删除——那正是「同名文件被
        旧墓碑误删」的入口。它们由各自的 ID 墓碑处理，或原样保留。
        """
        pfx = folder + '/'
        with self._lock:
            names = [(n, bool(s.is_dir))
                     for n, s in self._states.items()
                     if n.startswith(pfx) and s.exists]
        removed = False
        for name, is_dir in sorted(names, key=lambda kv: len(kv[0]),
                                   reverse=True):
            if is_dir:
                removed = self._remove_empty_dir(name) or removed
                continue
            try:
                self._delete_local(name)
                removed = True
            except OSError as e:
                self._notify_log(f"目录删除失败，保留子文件: {name}: {e}")
        removed = self._remove_empty_dir(folder) or removed
        return removed

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

    def _deleted_ancestor(self, name: str) -> Optional[FileState]:
        """最近的已删祖先目录（墓碑）状态副本；无则 None。"""
        if '/' not in name:
            return None
        parts = name.split('/')
        for i in range(1, len(parts)):
            anc = '/'.join(parts[:i])
            with self._lock:
                st = self._states.get(anc)
                dir_tomb = self._tombstones.get((anc, ''))
            if st is not None and not st.exists and st.state == STATE_CHANGE:
                return FileState(name=st.name, op_no=st.op_no, state=st.state,
                                 exists=st.exists, clock=st.clock, ts=st.ts,
                                 vv=dict(st.vv))
            if dir_tomb is not None and dir_tomb.get('origin') == 'delete' \
                    and not (st is not None and st.exists):
                return FileState(name=anc, op_no=dir_tomb.get('op_no', 0),
                                 state=STATE_CHANGE, exists=False,
                                 clock=dir_tomb.get('clock', 0),
                                 ts=dir_tomb.get('ts', 0.0),
                                 vv=dict(dir_tomb.get('vv') or {}),
                                 is_dir=True)
        return None

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
        if self._hasher is not None:
            self._hasher.stop()
        if self._worker and self._worker.is_alive():
            try:
                self._worker.join(timeout=2.0)
            except Exception:
                pass
        self.flush()
