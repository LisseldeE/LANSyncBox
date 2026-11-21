"""
去中心化同步：分发链路引擎（阶段 1）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import stat
import threading
import time
import unicodedata
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

        self._lock = threading.RLock()
        # 身份为主键：版本与状态都跟着 file_id 走，改名不换身份
        self._op_counters = {}    # file_id -> 本端已发出 op_no（源端递增）
        self._clocks = {}         # file_id -> 本端逻辑钟（每修改一次 +1）
        self._states = {}         # file_id -> FileState（含已删身份的删态）
        self._by_path = {}        # rel_path -> file_id（当前路径索引，路径唯一）
        self._queue = []          # 待处理信号（远端，FIFO）
        self._queue_keys = set()  # 排队中 (src_id, file_id 或 path, op_no)
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
        # 已收到目录删除信号的根（rel -> 删除 clock）：子项删除信号可能后到，
        # 每处理一个子项就重试清理这批根下的空壳目录，直到整棵子树消失。
        self._deleted_dir_roots = {}
        self._rmdir_fail_logged = set()  # 诊断：已记录过删除失败的目录，防日志刷屏
        self._db_meta = {}      # file_id -> (size, mtime)：db 落库时的磁盘内容快照
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
            for fid, rec in storage.load_counters().items():
                self._op_counters[fid] = max(self._op_counters.get(fid, 0),
                                             int(rec.get('op_no') or 0))
                self._clocks[fid] = max(self._clocks.get(fid, 0),
                                        int(rec.get('clock') or 0))
            for path, rec in storage.load_files().items():
                fid = rec.get('file_id') or ''
                if not fid:
                    continue
                st = self._state_from_record(rec)
                prev = self._states.get(fid)
                if prev is None:
                    self._states[fid] = st
                else:
                    prev.vv = merge_vv(prev.vv, st.vv)
                self._by_path[path] = fid
                self._clocks[fid] = max(self._clocks.get(fid, 0), st.clock)
                self._op_counters[fid] = max(self._op_counters.get(fid, 0),
                                             st.op_no)
                self._db_meta[fid] = (rec.get('size'),
                                      float(rec.get('mtime') or 0.0))
            self._tombstones = storage.load_tombstones()
            # 墓碑同样抬升版本地板：计数器行被上限淘汰/缺失时，同一身份重建
            # 不得复用低 op_no/clock，否则会被墓碑去重丢弃（删除-重建震荡）。
            for (_path, fid), rec in self._tombstones.items():
                if not fid:
                    continue
                self._op_counters[fid] = max(
                    self._op_counters.get(fid, 0),
                    int(rec.get('op_no') or 0))
                self._clocks[fid] = max(
                    self._clocks.get(fid, 0), int(rec.get('clock') or 0))
            self._peers = set(storage.load_peers())
        self._cleanup_loaded_state()

    def _cleanup_loaded_state(self):
        """加载后清洗：缺 ID 的墓碑一律作废。

        文件 ID 是删除的唯一身份凭据。缺 ID 的删除指令不成立，留着只会在
        重放时按路径误删文件；目录墓碑在新模型中不再存在，一并清除。
        无 ID 的 files 行在落库层已被拒绝，加载时不会出现。
        """
        invalid = [key for key, rec in self._tombstones.items()
                   if not key[1]]
        for key in invalid:
            with self._lock:
                self._tombstones.pop(key, None)
            if self._storage is not None:
                self._storage.remove_tombstone(*key)
            self._notify_log(f"作废缺文件ID的墓碑: {key[0]}")

    @staticmethod
    def _state_from_record(rec: dict) -> FileState:
        return FileState(
            name=rec['rel_path'], op_no=int(rec.get('op_no') or 0),
            state=STATE_ADD, exists=True, clock=int(rec.get('clock') or 0),
            ts=float(rec.get('ts') or 0.0), vv=dict(rec.get('vv') or {}),
            file_id=rec.get('file_id') or '', hash=rec.get('hash') or '',
            hash_state=rec.get('hash_state') or 'none',
            size=int(rec.get('size') or 0), is_dir=bool(rec.get('is_dir')))

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
            fid = self._by_path.get(file) or ''
            prev = self._states.get(fid) if fid else None
            if not fid:
                fid = self._file_id_for_content(file)
            floor_op = self._op_counters.get(fid, 0)
            floor_ck = self._clocks.get(fid, 0)
            if prev is not None:
                floor_op = max(floor_op, prev.op_no, prev.vv.get(self.end_id, 0))
                floor_ck = max(floor_ck, prev.clock)
            op_no, clock = floor_op + 1, floor_ck + 1
            self._op_counters[fid] = op_no
            self._clocks[fid] = clock
            vv = dict(prev.vv) if prev else {}
        vv[self.end_id] = op_no
        return {'src_id': self.end_id, 'op_no': op_no, 'op': self.OP_ADD,
                'file': file, 'state': STATE_ADD, 'clock': clock,
                'ts': time.time(), 'vv': vv, 'id': fid}

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
            prev = self.get_state(rel)
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
        st = self.get_state(rel)
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
            states = [st for st in self._states.values() if st.exists]
            tomb_paths = {path for (path, _fid) in self._tombstones}
        if os.path.isdir(self.sync_folder):
            for st in states:
                kind = self._path_kind(st.name)
                if st.is_dir:
                    # 目录身份要求磁盘上仍是目录；被文件取代或消失 → 落删态
                    if kind == 'dir':
                        continue
                    self._mark_local_deleted(st)
                    if kind == 'file':
                        # 同路径已换成文件：摘掉旧目录的路径索引，让新文件在
                        # 本轮按文件身份登记（删态仍留在状态表里）。
                        self._free_path(st.name, st.file_id)
                    result['deleted'] += 1
                    continue
                if kind == 'missing':
                    self._mark_local_deleted(st)
                    result['deleted'] += 1
                elif kind == 'dir':
                    # 文件被同名目录取代：旧文件身份落墓碑；目录内的文件由
                    # 扫描按新身份登记。
                    self._mark_local_deleted(st)
                    result['deleted'] += 1
        for path in sorted(disk_files):
            with self._lock:
                known = path in self._by_path or path in tomb_paths
            if known:
                continue  # 已有条目或墓碑残留 → 不复活
            self._register_new_local(path)
            result['added'] += 1
        # 磁盘上的空目录：显式目录身份（无墓碑才登记）。非空目录通常是拉取
        # 时自动创建的隐式父目录，不建身份。
        for path in sorted(disk_dirs, key=lambda p: (p.count('/'), p)):
            with self._lock:
                known = path in self._by_path or path in tomb_paths
            if known:
                continue
            try:
                if os.listdir(self._safe_join(path)):
                    continue
            except (OSError, ValueError):
                continue
            self._register_new_dir(path)
            result['added'] += 1
        # db 有、盘在但 size/mtime 变化：视为同一 ID 的更新（§6），提版本重算哈希
        with self._lock:
            states = [st for st in self._states.values() if st.exists]
        for st in states:
            if (not st.file_id or st.hash_state != 'ok'
                    or st.name not in disk_files):
                continue
            with self._lock:
                tombstoned = (st.name, st.file_id) in self._tombstones
            if tombstoned or not self._disk_changed(st):
                continue
            self._refresh_local_content(st)
            result['updated'] += 1
        # db 残留 pending（上次进程退出前未算完哈希）：按原身份重算，
        # 否则 pending 不进快照也无人拉取，该文件永久不可见。
        with self._lock:
            states = [st for st in self._states.values() if st.exists]
        for st in states:
            if st.hash_state == 'pending' and st.name in disk_files:
                with self._lock:
                    tombstoned = (st.name, st.file_id) in self._tombstones
                if not tombstoned:
                    self._rehash_existing(st)
        return result

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
            meta = self._db_meta.get(st.file_id)
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
            prev = self._states.get(st.file_id)
        op_no, clock = self._bump_version(st.file_id, prev)
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

    def get_tombstone(self, path: str, file_id: str) -> Optional[dict]:
        """取指定 (路径, 身份) 的墓碑记录（无则 None）。"""
        with self._lock:
            rec = self._tombstones.get((path, file_id or ''))
            return dict(rec) if rec else None

    def rename_tombstones(self) -> list:
        """rename 墓碑（仅本地压制，不下发）：供快照轮次回收。"""
        with self._lock:
            return [dict(rec) for rec in self._tombstones.values()
                    if rec.get('origin') == 'rename']

    def forget_tombstone(self, path: str, file_id: str):
        """移除指定墓碑（对账轮确认旧路径已无人持有时调用）。"""
        self._forget_tombstone(path, file_id)

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
        if remote.file_id:
            self.recover_floor(remote.file_id, base, remote.clock)
        self._store(FileState(
            name=remote.name, op_no=remote.op_no, state=STATE_ADD,
            exists=True, clock=remote.clock, ts=remote.ts, vv=dict(remote.vv),
            file_id=remote.file_id,
            hash='' if remote.is_dir else remote.hash,
            hash_state='none' if remote.is_dir else 'ok',
            size=int(remote.size or 0), is_dir=bool(remote.is_dir)))

    def publish_residue(self, path: str, file_id: str, hash_value: str,
                        size: int) -> Optional[dict]:
        """静默对齐确认本端独有新增：写 hash=ok 状态并广播一次 add。"""
        fid = file_id or ''
        with self._lock:
            prev = self._states.get(fid) if fid else self.get_state(path)
        if not fid:
            fid = prev.file_id if prev else new_file_id()
        op_no, clock = self._bump_version(fid, prev)
        ts = time.time()
        vv = dict(prev.vv) if prev else {}
        vv[self.end_id] = op_no
        self._store(FileState(
            name=path, op_no=op_no, state=STATE_ADD, exists=True,
            clock=clock, ts=ts, vv=vv, file_id=fid,
            hash=hash_value or '', hash_state='ok', size=int(size or 0)))
        signal = {
            'src_id': self.end_id, 'op_no': op_no, 'op': self.OP_ADD,
            'file': path, 'state': STATE_ADD, 'clock': clock, 'ts': ts,
            'vv': vv, 'id': fid, 'hash': hash_value or '',
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
        local = self.get_state(path)
        if local is None or not local.exists or not local.file_id:
            return False
        if local.is_dir or remote.is_dir:
            return False  # 目录没有身份，不参与同内容合并
        if not remote.file_id or local.file_id == remote.file_id:
            return False
        self._merge_same_content(path, local, remote)
        cur = self.get_state(path)
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
        winner = pick_same_content_winner(
            {'id': local.file_id, 'clock': local.clock, 'ts': local.ts},
            {'id': incoming.file_id, 'clock': incoming.clock,
             'ts': incoming.ts})
        if winner.get('id') == local.file_id:
            self.record_merge_tombstone(file, incoming.file_id)
            return
        self.record_merge_tombstone(file, local.file_id)
        loser_id = local.file_id
        self._store(FileState(
            name=file, op_no=local.op_no, state=local.state, exists=True,
            clock=local.clock, ts=local.ts, vv=dict(local.vv),
            file_id=incoming.file_id, hash=local.hash, hash_state='ok',
            size=local.size))
        # 败者身份从状态表移除（一个路径只留一个活身份），墓碑作压制凭据
        with self._lock:
            self._states.pop(loser_id, None)
        if self._storage is not None:
            self._storage.remove_file(file_id=loser_id)
        self._notify_log(f"同内容身份合并: {file} "
                         f"{local.file_id[:8]}→{incoming.file_id[:8]}")

    def apply_tombstone(self, path: str, file_id: str, is_dir: bool = False,
                        origin: str = 'delete', op_no: int = 0,
                        clock: int = 0, ts: float = 0.0,
                        vv: dict = None) -> bool:
        """应用一条删除墓碑：文件必须「路径 + 文件ID」双对应才删盘。

        文件 ID 是对象的唯一身份。缺 ID、或该身份当前不在该路径时，既不能
        证明是同一对象，也不允许按路径删同名文件——只记录墓碑。
        """
        if origin != 'delete':
            self._record_tombstone(path, file_id, is_dir=is_dir,
                                   origin=origin, op_no=op_no, clock=clock,
                                   ts=ts, vv=vv)
            return False
        if not file_id:
            self._notify_log(f"拒绝无效删除墓碑（缺文件ID）: {path}")
            return False
        with self._lock:
            st = self._states.get(file_id)
        if st is None or not st.exists or st.name != path:
            # 身份不成立：可能是同一路径的新对象，也可能是本端从未持有
            # 该 ID 的对象。两种情况都不能删盘，只保留墓碑用于去重。
            self._record_tombstone(path, file_id, is_dir=is_dir,
                                   origin=origin, op_no=op_no, clock=clock,
                                   ts=ts, vv=vv)
            return False
        removed = False
        try:
            dir_path = self._safe_join(path)
        except ValueError:
            dir_path = None
        if st.is_dir or is_dir:
            # 目录身份删除：只在目录为空时 rmdir；非空只落墓碑，由子项各自的
            # 删除信号与空目录收敛完成物理删除（绝不整棵 rmtree）。
            if dir_path is not None:
                try:
                    os.rmdir(dir_path)
                    removed = True
                except OSError:
                    removed = False
        else:
            try:
                self._delete_local(path)
                removed = True
            except OSError as e:
                self._notify_log(f"本地删除失败，仍记录墓碑: {path}: {e}")
        op, ck = self._bump_version(file_id, st)
        t = float(ts or time.time())
        vv2 = dict(vv or {})
        vv2[self.end_id] = op
        self._store(FileState(name=path, op_no=op, state=STATE_CHANGE,
                              exists=False, clock=ck, ts=t, vv=vv2,
                              file_id=file_id, is_dir=st.is_dir or is_dir))
        self._record_tombstone(path, file_id, is_dir=(st.is_dir or is_dir),
                               origin=origin, op_no=op, clock=ck, ts=t, vv=vv2)
        if removed:
            # 目录不是实体：删掉最后一个文件后，空父目录（含各级祖先）自然消失
            self._prune_empty_dirs(os.path.dirname(path).replace('\\', '/'))
        return removed

    def _forget_tombstone(self, path: str, file_id: str):
        """同一身份回到该路径：撤销该路径上的旧墓碑，避免永久「忽略拉取」。"""
        key = (path, file_id or '')
        with self._lock:
            rec = self._tombstones.pop(key, None)
        if rec is not None and self._storage is not None:
            self._storage.remove_tombstone(*key)

    def _record_tombstone(self, path: str, file_id: str = '',
                          is_dir: bool = False, origin: str = 'delete',
                          op_no: int = 0, clock: int = 0, ts: float = 0.0,
                          vv: dict = None):
        if origin == 'delete' and not file_id:
            # 文件身份必然存在：缺 ID 的删除指令不成立，记录它只会在重放时
            # 按路径误删同名对象。
            self._notify_log(f"拒绝无效删除墓碑（缺文件ID）: {path}")
            return
        key = (path, file_id or '')
        with self._lock:
            prev = self._tombstones.get(key)
            rec = dict(prev) if prev else {
                'rel_path': key[0], 'file_id': key[1],
                'is_dir': 1 if is_dir else 0,
                'origin': origin, 'op_no': 0, 'clock': 0, 'ts': 0.0,
                'vv': {}, 'deleted_at': time.time(), 'confirmed_by': [],
            }
            if prev is not None:
                rec['origin'] = prev.get('origin') or 'delete'
            rec['is_dir'] = 1 if (is_dir or rec.get('is_dir')) else 0
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
        if storage is None or not st.file_id:
            return
        storage.put_counter(st.file_id, st.op_no, st.clock)
        if not st.exists:
            storage.remove_file(file_id=st.file_id)
            with self._lock:
                self._db_meta.pop(st.file_id, None)
            return
        size = int(st.size or 0)
        mtime = float(st.ts or 0.0)
        # mtime 取落库时的真实磁盘值：拉取文件的 st.ts 是对端 mtime，
        # 与本地落盘时间不同，直接沿用会让每次重启都误判为已变化。
        try:
            disk_stat = os.stat(self._safe_join(st.name))
            size, mtime = int(disk_stat.st_size), disk_stat.st_mtime
        except (OSError, ValueError):
            pass
        storage.put_file({
            'rel_path': st.name,
            'file_id': st.file_id,
            'op_no': st.op_no, 'clock': st.clock, 'ts': st.ts, 'vv': st.vv,
            'hash': st.hash, 'hash_state': st.hash_state,
            'size': size, 'mtime': mtime,
            'is_dir': 1 if st.is_dir else 0,
        })
        with self._lock:
            self._db_meta[st.file_id] = (size, mtime)

    def _bump_version(self, file_id: str, prev: Optional[FileState]):
        """身份版本地板 +1（不广播，供启动对账/墓碑应用使用）。"""
        with self._lock:
            floor_op = self._op_counters.get(file_id, 0)
            floor_ck = self._clocks.get(file_id, 0)
            if prev is not None:
                floor_op = max(floor_op, prev.op_no,
                               prev.vv.get(self.end_id, 0))
                floor_ck = max(floor_ck, prev.clock)
            op_no, clock = floor_op + 1, floor_ck + 1
            self._op_counters[file_id] = op_no
            self._clocks[file_id] = clock
        return op_no, clock

    def recover_floor(self, file_id: str, op_no: int, clock: int = 0):
        """用对端快照里的 vv[本端] 抬升计数器基线（只抬地板，不改状态表 vv）。

        接收端不把对端 vv 并入本地状态表，避免虚增知识造成字节分叉；仅保证
        本端后续操作的序号严格大于对端已知版本。
        """
        if not file_id:
            return
        op_no = int(op_no or 0)
        if op_no <= 0:
            return
        with self._lock:
            if self._op_counters.get(file_id, 0) < op_no:
                self._op_counters[file_id] = op_no
            if clock and self._clocks.get(file_id, 0) < int(clock):
                self._clocks[file_id] = int(clock)

    def _file_id_for_content(self, path: str) -> str:
        """新增/修改取身份：同一活对象沿用，删除后重建换新 ID。"""
        with self._lock:
            fid = self._by_path.get(path)
            prev = self._states.get(fid) if fid else None
        if prev is not None and prev.file_id and (prev.exists
                                                  or prev.state == STATE_ADD):
            return prev.file_id
        return new_file_id()

    def _mark_local_deleted(self, st: FileState):
        """启动对账确认磁盘缺失：按该身份记录删态与删除墓碑。"""
        if self._hasher is not None:
            self._hasher.cancel(st.name)
        with self._lock:
            self._hash_pending.pop(st.name, None)
        op_no, clock = self._bump_version(st.file_id, st)
        ts = time.time()
        vv = dict(st.vv) if st else {}
        vv[self.end_id] = op_no
        self._store(FileState(name=st.name, op_no=op_no, state=STATE_CHANGE,
                              exists=False, clock=clock, ts=ts, vv=vv,
                              file_id=st.file_id, is_dir=st.is_dir))
        self._record_tombstone(st.name, st.file_id, is_dir=st.is_dir,
                               origin='delete', op_no=op_no, clock=clock,
                               ts=ts, vv=vv)

    def _free_path(self, path: str, file_id: str):
        """把路径从旧身份的路径索引上摘除（旧身份的删态仍留在状态表里）。

        只在「同路径被另一种类型接管」时使用：新旧身份必须能在同一轮对账里
        各自登记；普通删除仍需保留路径索引，避免磁盘残留被误判成新对象复活。
        """
        with self._lock:
            if path and self._by_path.get(path) == file_id:
                self._by_path.pop(path, None)

    def _register_new_local(self, path: str):
        fid = new_file_id()
        op_no, clock = self._bump_version(fid, None)
        try:
            size = os.path.getsize(self._safe_join(path))
        except (OSError, ValueError):
            size = 0
        self._store(FileState(
            name=path, op_no=op_no, state=STATE_ADD, exists=True,
            clock=clock, ts=time.time(), vv={self.end_id: op_no},
            file_id=fid, hash='', hash_state='pending', size=size))
        # 阶段 B：补扫新增同样先算哈希，ok 后才发布 add
        if self._hasher is not None:
            st = self._states.get(fid)
            signal = {'src_id': self.end_id, 'op_no': op_no,
                      'op': self.OP_ADD, 'file': path, 'state': STATE_ADD,
                      'clock': clock, 'ts': st.ts if st else time.time(),
                      'vv': dict(st.vv) if st else {self.end_id: op_no},
                      'id': fid}
            self._enqueue_hash(st, 'local_add', {'signal': signal})

    def _register_new_dir(self, path: str):
        """启动对账发现磁盘空目录：登记显式目录身份（不发哈希、不拉取）。"""
        fid = new_file_id()
        op_no, clock = self._bump_version(fid, None)
        ts = time.time()
        self._store(FileState(
            name=path, op_no=op_no, state=STATE_ADD, exists=True,
            clock=clock, ts=ts, vv={self.end_id: op_no},
            file_id=fid, hash='', hash_state='none', is_dir=True))
        self._broadcast_signal({
            'src_id': self.end_id, 'op_no': op_no, 'op': self.OP_DIR_CREATE,
            'file': path, 'state': STATE_ADD, 'clock': clock, 'ts': ts,
            'vv': {self.end_id: op_no}, 'id': fid, 'is_dir': True})

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

    def _path_is_dir(self, name: str) -> bool:
        """磁盘上该路径当前是否为目录（仅用于本地删除/添加的目录展开）。"""
        try:
            return os.path.isdir(self._safe_join(name))
        except ValueError:
            return False

    # ---- 属性 ----

    def get_state(self, file: str) -> Optional[FileState]:
        with self._lock:
            fid = self._by_path.get(file)
            return self._states.get(fid) if fid else None

    def get_state_by_id(self, file_id: str) -> Optional[FileState]:
        if not file_id:
            return None
        with self._lock:
            return self._states.get(file_id)

    def states(self) -> dict:
        with self._lock:
            return {st.name: st for st in self._states.values() if st.name}

    @staticmethod
    def _identity_path_key(name: str) -> tuple:
        """同 ID 版本平局时的确定性路径序：NFC + 忽略大小写字典序。"""
        norm = unicodedata.normalize('NFC', name.replace('\\', '/'))
        return (norm.casefold(), norm)

    def reconcile_identity_path(self, remote: FileState, src_id: str) -> str:
        """快照对账：同 ID 不同路径视为改名，本地改名（不广播、不拉取）。

        Returns:
            'same' 路径一致或身份未知；'keep' 保留本端路径；
            'adopt' 已采用远端路径；'blocked' 目标被其它身份占用或改名失败。
        """
        if remote is None or not remote.file_id:
            return 'same'
        local = self.get_state_by_id(remote.file_id)
        if local is None or not local.exists or local.name == remote.name:
            return 'same'
        newer = (remote.clock, remote.ts) > (local.clock, local.ts)
        if (remote.clock, remote.ts) == (local.clock, local.ts):
            newer = (self._identity_path_key(remote.name)
                     < self._identity_path_key(local.name))
        if not newer:
            self._record_knowledge(remote.file_id, remote.vv)
            return 'keep'
        target = self._claim_path(remote.file_id, remote.name,
                                  remote.clock, remote.ts)
        if target != remote.name:
            self._notify_log(
                f"同 ID 换路径受阻：{remote.name} 被其它身份占用，等待对端仲裁")
            return 'blocked'
        if not self._rename_local(local.name, target):
            return 'blocked'
        op_no = max(local.op_no, remote.op_no)
        vv_merged = merge_vv(dict(local.vv), remote.vv)
        self._record_tombstone(local.name, remote.file_id, origin='rename',
                               op_no=op_no, clock=remote.clock, ts=remote.ts,
                               vv=vv_merged)
        if self._hasher is not None:
            self._hasher.cancel(local.name)
        hash_ok = local.hash_state == 'ok'
        self._store(FileState(
            name=target, op_no=op_no, state=local.state, exists=True,
            clock=remote.clock, ts=remote.ts, vv=vv_merged,
            file_id=remote.file_id,
            hash=local.hash if hash_ok else remote.hash,
            hash_state='ok' if hash_ok else remote.hash_state,
            size=local.size or remote.size, is_dir=local.is_dir))
        if local.is_dir or remote.is_dir:
            # 采纳的是目录改名：子树账本必须跟着走，否则本端会继续按旧前缀
            # 上报子项，对端再按旧路径把子树搬回去。
            self._move_descendant_states(local.name, target)
        old_parent = os.path.dirname(local.name).replace('\\', '/')
        if old_parent:
            # 旧父目录被搬空后立即收敛：否则会留下空壳目录，被补扫登记成
            # 新身份后反向广播，表现为「改名前的目录以空目录复活」。
            self._prune_empty_dirs(old_parent)
        base = int(remote.vv.get(self.end_id, 0) or 0)
        self.recover_floor(remote.file_id, base, remote.clock)
        self._persist_state(self._states[remote.file_id])
        return 'adopt'

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
        """本地文件/目录操作 → 生成 DISTRIBUTE_SIGNAL 沿网状传播并记录本端状态。

        本地 FS 已由调用方完成（本方法不重复执行 FS 操作，仅记录状态 + 传播）。
        显式创建的目录是实体：dir_create 携带身份（is_dir=True）参与同步；隐式
        目录（拉取时 makedirs 出来的父目录）不建身份。目录删除 = 子项各自删除
        + 一条目录删除信号。
        """
        if not file:
            return None
        # 复制/拖入文件夹时根目录走的是 file_added（add 信号）：早期这里直接
        # 丢弃，导致「添加端添加的文件夹」根目录从未登记身份，另一端删除它时
        # 本端无 ID 可用、空壳只能靠兜底清理。统一按目录创建登记身份。
        if op == self.OP_ADD and self._path_is_dir(file):
            op = self.OP_DIR_CREATE
        prev = None
        fid = ''
        is_dir = False
        if op == self.OP_DIR_CREATE:
            if not self._path_is_dir(file):
                return None  # 建后即删：无对象可登记
            existing = self.get_state(file)
            if existing is not None and existing.exists:
                return None  # 该路径已有活身份（重复事件）
            # 删除后再建：给新身份，旧墓碑只压制旧 ID，不会拦死同名重建
            fid = new_file_id()
            is_dir = True
        elif op == self.OP_DELETE:
            prev = self.get_state(file)
            if prev is not None and prev.is_dir:
                # 显式目录删除：子项各自删除（身份独立），目录自身只发一条信号
                self._delete_dir_children(file)
                fid = prev.file_id
                is_dir = True
                # 目录删掉后，其上的隐式父目录若已空也随之收敛
                self._prune_empty_dirs(
                    os.path.dirname(file).replace('\\', '/'))
            elif prev is None and (self._path_is_dir(file)
                                   or self._has_child_states(file)):
                self._delete_dir_local(file)
                return None
            elif prev is None:
                self._notify_log(
                    f"本地删除缺少文件ID（对象未登记），不广播: {file}")
                return None
            else:
                fid = prev.file_id
        elif op in (self.OP_RENAME, self.OP_MOVE):
            prev = self.get_state(old or file) or self.get_state(file)
            fid = prev.file_id if prev else ''
            if prev is not None and prev.is_dir:
                is_dir = True
            elif not prev and (self._has_child_states(old or file)
                               or self._path_is_dir(file)):
                # 目录此前没有身份（隐式目录或旧通道建出的目录）：用户显式改名
                # 即视为显式目录，补建身份并发出带 ID 的改名信号。否则对端拿不到
                # 身份，后续删除/再改名只能按路径猜，出现「目录没有ID」。
                fid = new_file_id()
                is_dir = True
            if not fid:
                fid = new_file_id()
        else:
            prev = self.get_state(file)
            fid = self._file_id_for_content(file)
        with self._lock:
            # 版本地板：op_no/clock 必须严格超过该身份已记录的版本；
            # prev 只有在同一身份时才算地板（删除后重建是新身份，从 1 开始）
            floor_op = self._op_counters.get(fid, 0)
            floor_ck = self._clocks.get(fid, 0)
            if prev is not None and prev.file_id == fid:
                floor_op = max(floor_op, prev.op_no,
                               prev.vv.get(self.end_id, 0))
                floor_ck = max(floor_ck, prev.clock)
            op_no, clock = floor_op + 1, floor_ck + 1
            self._op_counters[fid] = op_no
            self._clocks[fid] = clock
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
            'id': fid,
        }
        if old is not None:
            signal['old'] = old
        if is_dir:
            signal['is_dir'] = True
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
        st = self._states.get(fid)
        if st is not None:
            signal['hash'] = st.hash if st.hash_state == 'ok' else ''
        # 阶段 B 门禁：文件 add 先算哈希，pending 期间不广播
        if (self._hasher is not None and op == self.OP_ADD
                and st is not None and st.hash_state == 'pending'
                and self._enqueue_hash(st, 'local_add', {'signal': signal})):
            return signal
        self._broadcast_signal(signal)
        return signal

    def _delete_dir_children(self, folder: str):
        """显式目录删除前：逐个发送其下已登记子项（文件/子目录）的删除信号。

        不做空目录收敛——父目录身份还在，物理删除由调用方（UI）已完成。
        """
        pfx = folder.rstrip('/') + '/'
        with self._lock:
            names = sorted(st.name for st in self._states.values()
                           if st.exists and st.name.startswith(pfx))
        for name in names:
            self.emit(self.OP_DELETE, name)

    def _has_child_states(self, folder: str) -> bool:
        pfx = folder.rstrip('/') + '/'
        with self._lock:
            return any(st.exists and st.name.startswith(pfx)
                       for st in self._states.values())

    def _delete_dir_local(self, folder: str):
        """隐式目录删除：其下文件逐个删除 + 清理空目录。

        显式目录身份走 emit 的目录分支（一条目录信号），不经过这里。
        """
        pfx = folder.rstrip('/') + '/'
        with self._lock:
            alive = [st for st in self._states.values()
                     if st.exists and st.name.startswith(pfx)]
        names = sorted(st.name for st in alive)
        max_clock = max((st.clock for st in alive), default=0)
        for name in names:
            self.emit(self.OP_DELETE, name)
        # 目录自身无身份（隐式建出/旧版本数据）：仍要广播一条目录删除信号。
        # 否则对端同一目录若有身份，收不到删除授权，会作为「显式空目录」被
        # _prune_empty_dirs 永久保护。全新 ID 只承载删除知识，接收端仅在本地
        # 目录版本不更新时空壳清理（见 _prune_deleted_dir_tree 的 clock 保护）。
        if names:
            with self._lock:
                key = '@orphan_dir_delete'
                op_no = self._op_counters.get(key, 0) + 1
                self._op_counters[key] = op_no
            self._broadcast_signal({
                'src_id': self.end_id, 'op_no': op_no, 'op': self.OP_DELETE,
                'file': folder, 'state': STATE_CHANGE,
                'clock': max_clock + 1, 'ts': time.time(),
                'vv': {self.end_id: op_no}, 'id': new_file_id(),
                'is_dir': True, 'orphan': True,
            })
        self._prune_empty_dirs(folder)

    def _clear_readonly(self, path: str):
        """清除只读属性：Windows 下只读对象拒绝删除/移动，失败则忽略。"""
        from sync.file_manager import clear_readonly
        clear_readonly(path)

    def _rmdir_force(self, path: str) -> bool:
        """删除空目录；Windows 下只读目录（WinError 5）先清属性再重试。

        返回是否已删除。只对空目录操作，绝不递归删内容。
        """
        try:
            os.rmdir(path)
            return True
        except OSError:
            pass
        self._clear_readonly(path)  # 清除 ReadOnly 位
        try:
            os.rmdir(path)
            return True
        except OSError as e:
            self._log_rmdir_failure(path, e)
            return False

    def _log_rmdir_failure(self, path: str, err: OSError):
        """诊断：记录删除空目录失败的原因（错误码 / 剩余条目 / 文件属性）。"""
        if path in self._rmdir_fail_logged:
            return
        self._rmdir_fail_logged.add(path)
        try:
            names = os.listdir(path)
            rest = f'{len(names)}项 {names[:5]}'
        except OSError as e:
            rest = f'无法列举({e})'
        attr = ''
        try:
            attr = f'|属性={os.stat(path).st_file_attributes}'
        except Exception:
            pass
        self._notify_log(f'诊断: 空目录删除失败 {path} | 错误={err} | 剩余={rest}{attr}')

    def _prune_empty_dirs(self, folder: str):
        """自底向上移除空目录（含其祖先，直到同步根）；非空/受保护时保留。"""
        if not folder:
            return  # 同步根目录本身永不删除
        rel = folder.replace('\\', '/').strip('/')
        while rel:
            try:
                path = self._safe_join(rel)
            except ValueError:
                return
            if rel in self._protected_dirs:
                return
            with self._lock:
                _fid = self._by_path.get(rel)
                _st = self._states.get(_fid) if _fid else None
            if _st is not None and _st.exists and _st.is_dir:
                return  # 显式目录：文件删空后保留身份，不再向上收敛
            if os.path.isdir(path):
                for root, _dirs, _files in os.walk(path, topdown=False):
                    self._rmdir_force(root)
                if os.path.isdir(path):
                    return  # 本层仍非空（或被占用），祖先必然也非空
            # 本层已不在：继续向上，祖先可能同样已经空了
            rel = os.path.dirname(rel).replace('\\', '/')

    def _prune_deleted_dir_roots(self, path: str):
        """子项删除后重试清理已删目录留下的空壳子树。"""
        if not self._deleted_dir_roots:
            return
        norm = (path or '').replace('\\', '/')
        for root, info in list(self._deleted_dir_roots.items()):
            if norm == root or norm.startswith(root.rstrip('/') + '/'):
                self._prune_deleted_dir_tree(
                    root, int(info.get('clock', 0)),
                    ignore_clock=bool(info.get('orphan')))
        # 防御性上限：并发新增内容导致长期非空的删除根不能无限积累
        if len(self._deleted_dir_roots) > 256:
            for root in list(self._deleted_dir_roots)[:64]:
                self._deleted_dir_roots.pop(root, None)

    def _prune_deleted_dir_tree(self, root: str, clock: int,
                                ignore_clock: bool = False):
        """删除目录后后序清空其下空壳目录（只删空目录，绝不删文件）。

        子目录可能带过空目录身份（空目录补扫登记），文件删空后
        _prune_empty_dirs 会因「显式目录」停手，留下大量空壳。父目录删除
        意味着整棵子树都应消失：空目录删除，且其身份（不晚于删除版本）记
        墓碑摘除；非空目录（并发新增内容）保留，后续子项删除时重试。
        """
        rel = (root or '').strip('/')
        if not rel:
            return
        try:
            full = self._safe_join(rel)
        except ValueError:
            self._deleted_dir_roots.pop(rel, None)
            return
        if not os.path.isdir(full):
            self._deleted_dir_roots.pop(rel, None)
            return
        try:
            names = os.listdir(full)
        except OSError:
            return
        for name in names:
            child = f"{rel}/{name}"
            try:
                child_full = self._safe_join(child)
            except ValueError:
                continue
            if os.path.isdir(child_full):
                self._prune_deleted_dir_tree(child, clock, ignore_clock)
        with self._lock:
            fid = self._by_path.get(rel)
            st = self._states.get(fid) if fid else None
        if (not ignore_clock and st is not None and st.exists and st.is_dir
                and st.clock > clock):
            return  # 比删除更新：并发新建的显式目录，保留
        if not self._rmdir_force(full):
            return  # 仍有内容：保留，等后续子项删除再试
        if st is not None and st.exists:
            self._record_tombstone(rel, st.file_id, is_dir=bool(st.is_dir),
                                   origin='delete', op_no=st.op_no,
                                   clock=st.clock, ts=st.ts, vv=dict(st.vv))
            self._drop_identity(st.file_id)
        self._deleted_dir_roots.pop(rel, None)
        self._prune_empty_dirs(os.path.dirname(rel).replace('\\', '/'))

    def remove_state(self, file: str):
        """移除端内文件状态条目（自同步链路列表收敛：双方均已删除的文件）。

        保留 _op_counters/_clocks（身份版本单调）：对端墓碑的收敛清理各端
        独立、可能滞后，本地计数若归零，同一身份重建会复用低 op_no/clock，
        被对端墓碑去重丢弃或被其反向删除，形成删除-重建震荡。
        """
        with self._lock:
            fid = self._by_path.pop(file, None)
            if fid:
                self._states.pop(fid, None)
        if self._storage is not None and fid:
            self._storage.remove_file(file_id=fid)

    def retry_pending_deletes(self, limit: int = 1) -> int:
        """补删磁盘残留：只处理「墓碑 ID == 本地删态 ID」的同一对象。

        删除必须身份对应：拿不到本地同 ID 的删态就不补删。重启后 files 行
        随删除移除，无法再证明磁盘残留是当初那个对象（可能是之后新建的同名
        文件），因此不再按路径清理——残留由墓碑挡住复活，不参与同步。
        每拍至多处理 limit 个（限速，防长阻塞/日志刷屏）。Returns: 成功删除数。
        """
        done = 0
        with self._lock:
            candidates = [(path, fid, rec) for (path, fid), rec
                          in self._tombstones.items()
                          if rec.get('origin') == 'delete' and fid
                          and not rec.get('is_dir')]
        for file, fid, rec in candidates:
            if done >= limit:
                break
            if not file:
                continue
            with self._lock:
                st = self._states.get(fid)
                if (file in self._protected_dirs
                        or file in self._transferring):
                    continue  # 受保护目录 / 传输中：不碰
                if st is None or st.exists or st.name != file:
                    # 无同 ID 删态 = 身份不成立，不按路径删盘
                    continue
            try:
                path = self._safe_join(file)
            except ValueError:
                continue
            if not os.path.exists(path):
                continue
            try:
                self._delete_local(file)
                removed = not os.path.exists(path)
            except OSError:
                continue
            if not removed:
                continue  # 仍失败（句柄未释放）→ 下一拍再试
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
            prev = (self._states.get(remote_id) if remote_id
                    else self.get_state(file))
            if remote_id:
                fid = remote_id
            elif prev is not None and prev.file_id \
                    and (prev.exists or prev.state == STATE_ADD):
                fid = prev.file_id
            else:
                fid = new_file_id()
            op_no = self._op_counters.get(fid, 0) + 1
            self._op_counters[fid] = op_no
        if prev is not None and prev.exists and prev.name and prev.name != file:
            # 同身份已在另一路径存活（改名信号先于拉取回执应用）：回执只能
            # 记知识，不得把身份路径写回旧名——否则磁盘留下两份字节，重启后
            # 旧路径那份成为无身份新文件，永久重复。
            self._record_knowledge(fid, {remote_src_id: int(remote_op_no or 0)})
            self._notify_log(
                f"拉取回执忽略：同身份已在 {prev.name}，不回退路径 {file}")
            if on_done is not None:
                try:
                    on_done(False)
                except Exception:
                    pass
            return None
        ts = float(remote_ts or 0.0) or time.time()
        clock = int(remote_clock or 0)
        # 防胜者端被低版本拉取降级：仅当拉取版本严格强于本端状态时才采纳
        if prev is not None:
            if clock < prev.clock or (clock == prev.clock and ts < prev.ts):
                clock = prev.clock
                ts = prev.ts
        self._clocks[fid] = max(self._clocks.get(fid, 0), clock)
        st = FileState(name=file, op_no=op_no, state=STATE_ADD, exists=True,
                       clock=clock, ts=ts,
                       vv={self.end_id: op_no, remote_src_id: remote_op_no},
                       file_id=fid,
                       hash=file_hash or '',
                       hash_state=('ok' if file_hash else 'pending'),
                       size=int(file_size or 0))
        self._forget_tombstone(file, fid)
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
            'id': fid,
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
        fid = signal.get('id', '') or signal.get('file_id', '') or ''
        st = self._states.get(fid) if fid else self._states.get(
            self._by_path.get(file, ''))
        if st and st.vv.get(src_id, 0) >= op_no:
            # 例外：本端为未验证副本且该信号带内容证据 → 仍需处理，用于补拉校正
            if not (st.hash_state != 'ok' and signal.get('hash')):
                return  # 该源该编号（或更新）已应用过，丢弃
        key = (src_id, fid or file, op_no)
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
                fid = signal.get('id', '') or signal.get('file_id', '') or ''
                op_no = int(signal.get('op_no', 0) or 0)
                self._queue_keys.discard((src_id, fid or file, op_no))
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
        fid = signal.get('id', '') or signal.get('file_id', '') or ''
        with self._lock:
            st = self._states.get(fid) if fid else self._states.get(
                self._by_path.get(file, ''))
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
                             hash_state=('ok' if signal.get('hash') else 'pending'),
                             is_dir=bool(signal.get('is_dir')))
        # 全链路身份驱动：无 ID 的分发信号（只有旧版本端才会发）无法映射到
        # 唯一身份，接收端凭空补 ID 会与对端永久分叉，改名/删除再也对不上。
        # 版本闸门已拦旧端，这里整条拒收：不建状态、不转发、不动磁盘。
        if not incoming.file_id:
            self._notify_log(
                f"拒绝无文件ID的分发信号（对端版本不兼容）: {op} {file}")
            return
        if op == self.OP_DIR_CREATE or (op == self.OP_ADD
                                        and incoming.is_dir):
            # 目录创建：建目录 + 登记目录身份（不参与拉取/哈希）
            self._apply_dir_create(file, signal, src_id, op_no, clock, ts, vv)
            return
        # 身份优先仲裁：按 ID 取本地状态；同一身份换了路径（改名）也按身份取，
        # 避免拿旧名的新对象比对。未知 ID 不得借用同路径/旧路径上的其他身份。
        with self._lock:
            local = self._states.get(incoming.file_id)
        if local is None and op == self.OP_ADD:
            # 同路径同内容不同 ID：身份合并（不搬字节、不产生重复文件）。
            # 此处只做身份代次裁决，不借用占位者身份参与改名/仲裁。
            occupant = self.get_state(file)
            if (occupant is not None and occupant.exists
                    and occupant.hash_state == 'ok' and occupant.hash
                    and signal.get('hash') == occupant.hash):
                self._merge_same_content(file, occupant, incoming)
                self._record_knowledge(occupant.file_id, vv)
                self._forward(signal, src_id)
                return
        if local is not None and op != self.OP_DELETE:
            # 删除不参与版本仲裁：文件 ID 是唯一身份，ID 对应即删（由下方
            # 删除分支精确匹配）。若先走四层裁决，回归端拉取后继承的版本
            # 高于删除信号版本时会判「本地胜出」，删除永远传播不出去。
            if pulled:
                # 拉取回执：仅合并对端知识，不应用、不覆盖（防回声风暴）
                self._record_knowledge(local.file_id, vv)
                self._forward(signal, src_id)
                return
            # C3：ADD 携带 hash 且与本端已算好的内容一致 → 同内容，仅合并身份
            if (op == self.OP_ADD and local.exists
                    and local.hash_state == 'ok' and local.hash
                    and signal.get('hash') == local.hash):
                self._merge_same_content(file, local, incoming)
                self._record_knowledge(local.file_id, vv)
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
                    self._record_knowledge(incoming.file_id, vv)
                    self._notify_log(
                        f"本地版本胜出，忽略远端 {src_id} 信号: {file}")
                self._forward(signal, src_id)  # 已收敛也继续转发（其他端可能需应用）
                return
            # 远端胜出：覆盖本地内容
            if local.clock > 0:
                self._notify_log(f"本地修改被远端覆盖: {file}")
        # 目录不是实体：没有祖先目录状态需要拦截，文件各自按 ID 仲裁。
        want_pull = False
        try:
            if op == self.OP_ADD:
                exists = os.path.exists(self._safe_join(file))
                # 竞态复核：入参 local 可能在 FS 操作期间被本端 emit 更新（写盘
                # 与工作线程并发）；若当前状态已不弱于远端，退回「仅记知识」，
                # 否则会把刚记录的本端新版本覆盖掉（丢更新 + 哈希结果作废）
                cur = self.get_state(file)
                cur_id = cur.file_id if cur is not None else ''
                with self._lock:
                    tomb = (self._tombstones.get((file, incoming.file_id))
                            if incoming.file_id else None)
                if (tomb is not None and tomb.get('origin') == 'delete'
                        and int(tomb.get('clock') or 0) >= clock):
                    # 该身份在该路径已被删除，且没有更新的版本证据 → 不复活
                    self._record_knowledge(incoming.file_id, vv)
                    self._forward(signal, src_id)
                    return
                if cur is not None and cur.exists and cur_id \
                        and cur_id != incoming.file_id:
                    # 同路径不同身份：clock 高者占路径，落败方加后缀保留
                    if (clock, ts) <= (cur.clock, cur.ts):
                        self._notify_log(
                            f"同名冲突：本地版本更高，保留 {file}")
                        self._record_knowledge(cur_id, vv)
                        self._forward(signal, src_id)
                        return
                    loser = self._suffixed_path(file)
                    try:
                        self._rename_local(cur.name, loser)
                    except OSError as e:
                        self._notify_log(f"同名冲突改名失败: {file}: {e}")
                        return
                    self._store(FileState(
                        name=loser, op_no=cur.op_no, state=cur.state,
                        exists=True, clock=cur.clock, ts=cur.ts,
                        vv=dict(cur.vv), file_id=cur_id, hash=cur.hash,
                        hash_state=cur.hash_state, size=cur.size))
                    self._notify_log(
                        f"同名冲突：低版本改名保留 {file} → {loser}")
                    want_pull = True
                fid = incoming.file_id or cur_id or new_file_id()
                # 同一身份换了路径（改名以 add 到达）：先挪本地字节
                if local is not None and local.exists and local.name != file:
                    try:
                        self._rename_local(local.name, file)
                    except OSError as e:
                        self._notify_log(
                            f"同身份改名失败: {local.name}→{file}: {e}")
                        return
                elif local is not None and local.exists:
                    want_pull = True
                self._forget_tombstone(file, fid)
                self._store(FileState(
                    name=file, op_no=op_no, state=STATE_ADD,
                    exists=os.path.exists(self._safe_join(file)),
                    clock=clock, ts=ts, vv=vv, file_id=fid, hash='',
                    hash_state='pending'))
            elif op == self.OP_DELETE:
                # 删除留「删态」（exists=False）：源端延迟的 add 信号到达时
                # 本端 state.exists=False 判本地胜出，只记知识并转发、不再重建。
                sig_id = incoming.file_id
                st = self._states.get(sig_id)
                if st is None or not st.exists or st.name != file:
                    # 身份不对应：本地是另一个活对象（可能同路径重建），或本端
                    # 从未持有该 ID。按路径删盘会误删同名新文件，只记录删除
                    # 知识/墓碑并转发，保留本地内容。
                    if st is not None and st.exists:
                        self._notify_log(f"删除信号身份不匹配，保留本地对象: {file}")
                    self._record_tombstone(file, sig_id, origin='delete',
                                           op_no=op_no, clock=clock, ts=ts,
                                           vv=vv)
                    if incoming.is_dir:
                        # 目录删除信号身份不匹配（旧端无身份、或信号只承载删除
                        # 授权）：只要本端该路径目录版本不更新，就清空壳子树。
                        # 删除后并发新建的目录 clock 更新，由 clock 保护保留；
                        # orphan 信号明确表示「删除方已删整棵目录」，此时本端
                        # 对账抬高的 clock 不应阻止清理（只删空目录，绝不删文件）。
                        orphan = bool(signal.get('orphan'))
                        local = self.get_state(file)
                        if (orphan or local is None or not local.exists
                                or (local.is_dir
                                    and local.clock <= int(clock))):
                            self._deleted_dir_roots[file] = {
                                'clock': int(clock), 'orphan': orphan}
                            self._prune_deleted_dir_tree(
                                file, int(clock), ignore_clock=orphan)
                    # 属于已登记删除根时重试清空（文件/目录删空后触发）
                    self._prune_empty_dirs(
                        os.path.dirname(file).replace('\\', '/'))
                    self._prune_deleted_dir_roots(file)
                    self._forward(signal, src_id)
                    return
                is_dir_del = bool(incoming.is_dir or st.is_dir)
                if is_dir_del:
                    # 目录身份删除：先尝试直接 rmdir；大量子目录带过空目录
                    # 身份（补扫登记过），逐个文件删除后它们会变成「带活身份
                    # 的空壳」并阻断 _prune_empty_dirs。登记删除根并后序清空。
                    self._deleted_dir_roots[file] = {
                        'clock': int(clock), 'orphan': False}
                    self._prune_deleted_dir_tree(file, int(clock))
                else:
                    try:
                        self._delete_local(file)
                    except OSError as e:
                        # 在途拉取的 .part 被占用（WinError 32）等：磁盘删不掉也
                        # 必须记录墓碑并转发，否则删除知识丢失、删除永不收敛。
                        self._notify_log(f"本地删除失败，仍记录删除: {file}: {e}")
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=False, clock=clock, ts=ts,
                                      vv=vv, file_id=sig_id,
                                      is_dir=is_dir_del))
                self._record_tombstone(file, sig_id, is_dir=is_dir_del,
                                       origin='delete', op_no=op_no,
                                       clock=clock, ts=ts, vv=vv)
                # 最后一个文件删除后父目录自然消失（显式目录身份会阻断收敛）
                self._prune_empty_dirs(os.path.dirname(file).replace('\\', '/'))
                # 该文件属于某个已删目录：重试清空那一批根下的空壳子树
                self._prune_deleted_dir_roots(file)
            elif op in (self.OP_RENAME, self.OP_MOVE):
                old_name = old or file
                if local is None:
                    # 本端未登记该身份：只按 (clock, ts) 争目标路径，绝不搬
                    # 本端字节——旧路径上的字节属于另一个身份；缺的字节由
                    # 快照按 ID 补拉。
                    fid = incoming.file_id
                    target = self._claim_path(fid, file, clock, ts)
                    if target != file:
                        # 目标路径被本端更高版本身份占据，该改名不得抢路径
                        self._notify_log(
                            f"改名信号身份未知且版本较低，暂不应用: {file}")
                        self._forward(signal, src_id)
                        return
                    exists = os.path.exists(self._safe_join(target))
                    self._forget_tombstone(target, fid)
                    if incoming.is_dir and old_name != target:
                        # 对端把本端未登记的目录改名到 target：旧路径上的目录
                        # 通常就是同一次改名的物理对象（旧通道建出/两端身份
                        # 已分叉）。整体搬过去，避免只在新路径建空目录、把旧
                        # 目录留成空壳后被补扫成新身份反向广播。
                        self._adopt_unregistered_dir_rename(
                            old_name, target, fid)
                        exists = os.path.exists(self._safe_join(target))
                    if incoming.is_dir and not exists:
                        try:
                            os.makedirs(self._safe_join(target), exist_ok=True)
                            exists = True
                        except (OSError, ValueError) as e:
                            self._notify_log(f"创建目录失败: {target}: {e}")
                            return
                    self._store(FileState(
                        name=target, op_no=op_no, state=STATE_CHANGE,
                        exists=exists, clock=clock, ts=ts, vv=vv,
                        file_id=fid,
                        hash='' if incoming.is_dir else incoming.hash,
                        hash_state='none' if incoming.is_dir
                        else incoming.hash_state,
                        is_dir=incoming.is_dir))
                    self._forward(signal, src_id)
                    self._notify_applied(signal)
                    return
                fid = local.file_id
                target = self._claim_path(fid, file, clock, ts)
                src_name = old_name
                if not os.path.exists(self._safe_join(src_name)):
                    src_name = local.name
                if src_name != target and os.path.exists(
                        self._safe_join(src_name)):
                    try:
                        self._rename_local(src_name, target)
                    except OSError as e:
                        self._notify_log(
                            f"变更失败: {src_name}→{target}: {e}")
                        return
                vv_merged = merge_vv(dict(local.vv), vv)
                exists = os.path.exists(self._safe_join(target))
                self._forget_tombstone(target, fid)
                moved_dir = bool(incoming.is_dir or local.is_dir)
                self._store(FileState(
                    name=target, op_no=op_no, state=STATE_CHANGE,
                    exists=exists, clock=clock, ts=ts, vv=vv_merged,
                    file_id=fid,
                    hash='' if moved_dir else local.hash,
                    hash_state='none' if moved_dir else local.hash_state,
                    size=local.size, is_dir=moved_dir))
                if old_name != target:
                    # 旧路径墓碑只做本地压制：origin=rename 不下发，避免
                    # 对端先按「删除」删盘再收到改名时已无字节可移。
                    self._record_tombstone(old_name, fid, is_dir=moved_dir,
                                           origin='rename', op_no=op_no,
                                           clock=clock, ts=ts, vv=vv_merged)
                self._move_descendant_states(old_name, target)
            else:
                return  # 未知操作：不应用、不转发
        except ValueError as e:
            self._notify_log(f"拒绝非法路径: {e}")
            return
        # 拉取触发：add 信号应用后，本端无该文件（缺失拉取，阶段 4）或
        # 本端已有旧内容且远端胜出（冲突覆盖）→ 请求拉取源端字节
        if op == self.OP_ADD:
            try:
                final_exists = os.path.exists(self._safe_join(file))
            except ValueError:
                final_exists = False
            if not final_exists or want_pull:
                self._request_conflict_pull(file, src_id)
        self._forward(signal, src_id)
        self._notify_applied(signal)

    def _apply_dir_create(self, file: str, signal: dict, src_id: str,
                          op_no: int, clock: int, ts: float, vv: dict):
        """应用远端目录创建：建目录 + 登记目录身份。

        同路径若已有另一个目录身份（两端各自 mkdir），按 (clock, ts) 合并：
        高版本存活，败者落 merge 墓碑并移出状态表（磁盘上本来就是同一个目录）。
        同路径被文件占用时按 _claim_path 裁决，目录去带后缀的路径。
        """
        fid = signal.get('id', '') or signal.get('file_id', '') or ''
        with self._lock:
            local = self._states.get(fid)
        if local is not None and local.exists and local.is_dir \
                and local.name == file:
            self._record_knowledge(fid, vv)
            self._forward(signal, src_id)
            return
        target = file
        occupant = self.get_state(file)
        if occupant is not None and occupant.exists \
                and occupant.file_id != fid:
            if occupant.is_dir:
                # 同路径两个目录身份：高版本存活，同版本按 ID 字典序决胜，
                # 保证两端各自计算也收敛到同一个胜者。
                occupant_key = (occupant.clock, occupant.ts, occupant.file_id)
                remote_key = (clock, ts, fid)
                if occupant_key >= remote_key:
                    self.record_merge_tombstone(file, fid)
                    self._record_knowledge(occupant.file_id, vv)
                    self._forward(signal, src_id)
                    return
                self.record_merge_tombstone(file, occupant.file_id)
                self._drop_identity(occupant.file_id)
            else:
                target = self._claim_path(fid, file, clock, ts)
                if target != file:
                    self._notify_log(
                        f"同名冲突：目录改名为 {target}（原路径被文件占用）")
        try:
            os.makedirs(self._safe_join(target), exist_ok=True)
        except (OSError, ValueError) as e:
            self._notify_log(f"创建目录失败: {target}: {e}")
            return
        self._forget_tombstone(target, fid)
        self._store(FileState(
            name=target, op_no=op_no, state=STATE_CHANGE, exists=True,
            clock=clock, ts=ts, vv=vv, file_id=fid, hash='',
            hash_state='none', is_dir=True))
        self._forward(signal, src_id)
        self._notify_applied(signal)

    def _drop_identity(self, file_id: str):
        """身份合并败者：移出状态表与 db（磁盘对象保留，墓碑作压制凭据）。"""
        if not file_id:
            return
        with self._lock:
            st = self._states.pop(file_id, None)
            if st is not None and self._by_path.get(st.name) == file_id:
                self._by_path.pop(st.name, None)
        if self._storage is not None:
            self._storage.remove_file(file_id=file_id)

    def _forward(self, signal: dict, except_end_id: str):
        """防回声转发：向除 src_id 外所有直连对端（本地已收敛/忽略时同样转发）。"""
        if self.mesh is not None:
            try:
                self.mesh.send_to_all(Protocol.create_distribute_signal(signal),
                                      except_end_id=except_end_id)
            except Exception as e:
                self._notify_log(f"转发信号失败: {e}")

    def _record_knowledge(self, file_id: str, vv: dict):
        """仅合并对端 vv 知识（不改动本地 clock/ts/FS）：本地胜出或拉取回执时，
        记录对端已到达的版本，保证去重且不抬高本地时钟偏置后续裁决。"""
        with self._lock:
            st = self._states.get(file_id)
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
        fid = signal.get('id') or ''
        is_dir = bool(signal.get('is_dir'))
        try:
            if op == self.OP_DIR_CREATE:
                # 显式目录身份：不涉及哈希/字节，只登记路径与身份
                exists = self._path_is_dir(file)
                self._forget_tombstone(file, fid)
                self._store(FileState(
                    name=file, op_no=op_no, state=STATE_CHANGE,
                    exists=exists, clock=clock, ts=ts, vv=vv, file_id=fid,
                    hash='', hash_state='none', is_dir=True))
            elif op == self.OP_ADD:
                exists = os.path.exists(self._safe_join(file))
                # 本地 emit：磁盘 mtime 与声明 ts 对齐（拉取端据此识别陈旧源）
                try:
                    if touch:
                        os.utime(self._safe_join(file), (ts, ts))
                except OSError:
                    pass
                if not fid:
                    fid = self._file_id_for_content(file)
                self._forget_tombstone(file, fid)
                self._store(FileState(name=file, op_no=op_no, state=STATE_ADD,
                                      exists=exists, clock=clock, ts=ts,
                                      vv=vv, file_id=fid, hash='',
                                      hash_state='pending'))
            elif op == self.OP_DELETE:
                if not fid:
                    # 文件必然有身份；走到这里说明它从未在状态表登记
                    self._notify_log(f"本地删除缺少文件ID（对象未登记）: {file}")
                    return
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=False, clock=clock, ts=ts,
                                      vv=vv, file_id=fid, is_dir=is_dir))
                self._record_tombstone(file, fid, is_dir=is_dir,
                                       origin='delete', op_no=op_no,
                                       clock=clock, ts=ts, vv=vv)
            elif op in (self.OP_RENAME, self.OP_MOVE):
                old = signal.get('old', file)
                old_st = self.get_state(old) or self.get_state(file)
                if not fid:
                    fid = old_st.file_id if old_st else new_file_id()
                moved_dir = bool(is_dir or (old_st.is_dir if old_st else False))
                exists = os.path.exists(self._safe_join(file))
                self._forget_tombstone(file, fid)
                # 内容随改名移动（字节未变）：身份/哈希延续到新名
                self._store(FileState(name=file, op_no=op_no, state=STATE_CHANGE,
                                      exists=exists, clock=clock, ts=ts,
                                      vv=vv, file_id=fid,
                                      hash='' if moved_dir else
                                      (old_st.hash if old_st else ''),
                                      hash_state='none' if moved_dir else
                                      (old_st.hash_state if old_st
                                       else 'pending'),
                                      size=old_st.size if old_st else 0,
                                      is_dir=moved_dir))
                if old and old != file:
                    old_vv = merge_vv(dict(old_st.vv) if old_st else {}, vv)
                    self._record_tombstone(old, fid, is_dir=moved_dir,
                                           origin='rename', op_no=op_no,
                                           clock=clock, ts=ts, vv=old_vv)
                    self._move_descendant_states(old, file)
                    if moved_dir:
                        self._prune_empty_dirs(
                            os.path.dirname(old).replace('\\', '/'))
            else:
                return
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
        """写入状态表：按 file_id 唯一，合并 vv，维护路径索引与时钟步进。"""
        fid = new_state.file_id
        if not fid:
            fid = new_file_id()
            new_state.file_id = fid
        with self._lock:
            prev = self._states.get(fid)
            if prev is not None:
                new_state.vv = merge_vv(prev.vv, new_state.vv)
                # 同一身份的旧路径索引必须移除：一个 ID 只对应一条路径
                if prev.name and prev.name != new_state.name \
                        and self._by_path.get(prev.name) == fid:
                    self._by_path.pop(prev.name, None)
            self._states[fid] = new_state
            if new_state.name:
                other = self._by_path.get(new_state.name)
                if other and other != fid:
                    other_st = self._states.get(other)
                    if other_st is not None and not other_st.exists:
                        # 路径被新身份接管：旧删态不再保留（墓碑仍是去重凭据）
                        self._states.pop(other, None)
                self._by_path[new_state.name] = fid
            if remove and remove != new_state.name \
                    and self._by_path.get(remove) == fid:
                self._by_path.pop(remove, None)
            # 本地计数步进到已应用版本（Lamport 收信步进）：不步进则后续本地
            # 操作的 clock 可能低于状态表，被对端更新的墓碑判「本地胜出」丢弃。
            self._clocks[fid] = max(self._clocks.get(fid, 0), new_state.clock)
        self._persist_state(new_state)

    def _move_descendant_states(self, old: str, new: str):
        """目录改名/移动：把 old/ 下的活条目路径前缀平移到 new/。

        每个被平移的子项都按「自身也改了一次名」处理：路径变化必须伴随版本
        递增。否则离线端手里的旧路径与本端新路径版本完全相同，快照仲裁只能
        按字典序裁决，旧路径可能胜出并把整棵子树搬回去（目录改名回滚）。
        """
        if not old or not new or old == new:
            return
        pfx = old.rstrip('/') + '/'
        new_pfx = new.rstrip('/') + '/'
        with self._lock:
            children = [(fid, st) for fid, st in self._states.items()
                        if st.exists and st.name.startswith(pfx)]
        for fid, st in children:
            # 旧子路径留 rename 墓碑（旧版本）：压制延迟到达的旧路径 add
            self._record_tombstone(st.name, fid, is_dir=st.is_dir,
                                   origin='rename', op_no=st.op_no,
                                   clock=st.clock, ts=st.ts, vv=dict(st.vv))
            op_no, clock = self._bump_version(fid, st)
            ts = time.time()
            vv = dict(st.vv)
            vv[self.end_id] = op_no
            self._store(FileState(
                name=new_pfx + st.name[len(pfx):], op_no=op_no,
                state=st.state, exists=True, clock=clock, ts=ts,
                vv=vv, file_id=fid, hash=st.hash,
                hash_state=st.hash_state, size=st.size, is_dir=st.is_dir))

    def _adopt_unregistered_dir_rename(self, old: str, target: str, fid: str):
        """对端目录改名落地，而本端没有该身份：把旧路径上的目录整体搬过去。

        本端未登记该身份时，旧路径上的目录通常就是同一次改名的物理对象
        （旧通道建出、或两端身份已分叉）。只在新路径 makedirs 会把旧目录
        留成空壳，随后被补扫登记成新空目录身份并反向广播，表现为「改名后
        旧目录复活」。目标已存在时不动字节（同名交给身份对账），只摘除旧
        路径上失效的目录身份并收敛空壳。
        """
        try:
            old_path = self._safe_join(old)
            target_path = self._safe_join(target)
        except ValueError:
            return
        if not os.path.isdir(old_path):
            return
        stale = self.get_state(old)
        if not os.path.exists(target_path):
            try:
                self._rename_local(old, target)
            except OSError as e:
                self._notify_log(f"变更失败: {old}→{target}: {e}")
                return
            self._move_descendant_states(old, target)
        if stale is not None and stale.file_id != fid and stale.is_dir:
            # 旧路径上的另一份目录身份随物理目录一起失效：记合并墓碑并摘除，
            # 否则它会以「活身份」继续阻止空目录收敛。
            self._record_tombstone(stale.name, stale.file_id, is_dir=True,
                                   origin='merge', op_no=stale.op_no,
                                   clock=stale.clock, ts=stale.ts,
                                   vv=dict(stale.vv))
            self._drop_identity(stale.file_id)
        self._prune_empty_dirs(old)

    def _claim_path(self, fid: str, target: str, clock: int, ts: float) -> str:
        """同路径不同身份：clock 高者占路径，落败方加后缀保留。"""
        with self._lock:
            occ_id = self._by_path.get(target)
            occ = self._states.get(occ_id) if occ_id else None
        if occ is None or occ_id == fid or not occ.exists:
            return target
        if (clock, ts) > (occ.clock, occ.ts):
            loser = self._suffixed_path(target)
            try:
                self._rename_local(occ.name, loser)
            except OSError as e:
                self._notify_log(f"同名冲突：占位对象改名失败 {occ.name}: {e}")
                return target
            self._store(FileState(
                name=loser, op_no=occ.op_no, state=occ.state, exists=True,
                clock=occ.clock, ts=occ.ts, vv=dict(occ.vv), file_id=occ_id,
                hash=occ.hash, hash_state=occ.hash_state, size=occ.size))
            self._notify_log(f"同名冲突：高版本占 {target}，低版本改名 {loser}")
            return target
        loser = self._suffixed_path(target)
        self._notify_log(f"同名冲突：本端低版本，改名保留 {loser}")
        return loser

    def _suffixed_path(self, path: str) -> str:
        """生成不冲突的「name (n).ext」路径（内存与磁盘都未占用）。"""
        base, ext = os.path.splitext(path)
        for i in range(1, 10000):
            cand = f"{base} ({i}){ext}"
            with self._lock:
                taken = cand in self._by_path
            if taken:
                continue
            try:
                if os.path.exists(self._safe_join(cand)):
                    continue
            except ValueError:
                continue
            return cand
        return f"{base} ({new_file_id()[:8]}){ext}"

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
        # 仅大小写改名：大小写不敏感文件系统上二者是同一物理路径，先删目标
        # 会把源文件本身删掉（随后 rename 报源不存在），直接改名即可。
        same_physical = os.path.normcase(old_path) == os.path.normcase(new_path)
        if not same_physical and os.path.exists(new_path):
            if os.path.isdir(new_path) and not os.path.islink(new_path):
                from sync.file_manager import safe_rmtree
                safe_rmtree(new_path)
            else:
                self._clear_readonly(new_path)
                os.remove(new_path)
        try:
            os.rename(old_path, new_path)
        except OSError:
            # Windows 下只读目录/文件拒绝移动（WinError 5）：清属性后重试
            self._clear_readonly(old_path)
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
