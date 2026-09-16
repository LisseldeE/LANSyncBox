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
  - 本端缺失对端有的文件 → 加入拉取队列（state=ADD），排队去重，
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
from sync.vector import FileState, STATE_CHANGE, compare_states


class FileStateStore(QObject):
    """自同步链路管理器：端内文件状态对比、拉取队列与删除收敛（每端一个）。"""

    log_message = Signal(str)
    # 一轮对比结束：True=有差异（正在补齐/拉取），False=列表一致
    sync_done = Signal(bool)
    # 自同步拉取完成落盘（相对路径；UI 刷新文件列表）
    file_added = Signal(str)

    ROUND_TIMEOUT = 10.0   # 一轮对比超时兜底（对端不应答时也结束本轮，秒）

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
        self._pulls = {}         # name -> {name, end_id, op_no, session} 待拉取队列
        # 冲突覆盖拉取（阶段 3）：name -> src_id（等待该对端 RESP 会话后入队）
        self._pending_conflicts = {}
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

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

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
                pend_src = self._pending_conflicts.get(name)
                if pend_src == src_id:
                    self._pending_conflicts.pop(name, None)
                else:
                    pend_src = None
            if pend_src and remote.exists and session is not None:
                if self._enqueue_pull(name, src_id, remote, session):
                    has_diff = True
            if remote.exists:
                # 对端有该文件：本端缺失 → 入拉取队列（同文件排队去重）
                if not local_exists:
                    if self._enqueue_pull(name, src_id, remote, session):
                        has_diff = True
                # 本端已有：冲突收敛（阶段 3）——三层裁决远端胜出则拉取覆盖
                else:
                    local_st = self.distributor.get_state(name)
                    if (local_st is not None and session is not None
                            and compare_states(remote, local_st, src_id,
                                               self.end_id) == 1):
                        if self._enqueue_pull(name, src_id, remote, session):
                            has_diff = True
            else:
                # 对端条目已删除：本端存在 → 补收删除（断网期间删除指令未达）
                if local_exists:
                    self._apply_remote_delete(name, src_id, remote)
                    has_diff = True
                # 清理收敛：本地与对端均为 CHANGE 且已不存在 → 删除条目
                local_st = self.distributor.get_state(name)
                if local_st is not None and not local_st.exists \
                        and local_st.state == STATE_CHANGE:
                    self._cleanup_entry(name)
        self._finish_round(end_id, has_diff)

    def _enqueue_pull(self, name: str, src_id: str, remote: FileState, session) -> bool:
        """拉取排队去重：同文件已有排队信息则跳过。返回是否新入队。"""
        with self._cond:
            if name in self._pulls:
                return False
            self._pulls[name] = {
                'name': name,
                'end_id': src_id,
                'op_no': remote.op_no,
                'session': dict(session) if isinstance(session, dict) else None,
            }
            self._cond.notify()
        return True

    def _apply_remote_delete(self, name: str, src_id: str, remote: FileState):
        """补收删除：构造 delete 信号走分发链路应用（含防回声转发与状态合并）。"""
        signal = {
            'src_id': src_id,
            'op_no': remote.op_no,
            'op': 'delete',
            'file': name,
            'state': STATE_CHANGE,
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
            try:
                self._do_pull(item)
            except Exception as e:
                self.log_message.emit(f"拉取任务失败: {e}")

    def _do_pull(self, item: dict):
        name = item['name']
        session = item.get('session')
        if not session:
            self.log_message.emit(f"拉取 {name}: 对端未提供会话")
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
        ok, _received, err = pull_file(host, port, session_id, token, name, dest,
                                       msg_type=MessageType.SYNC_PULL_REQ)
        if ok:
            # 记录本端已持有（vv 推进到远端 op_no）+ 广播 add 状态；UI 刷新
            self.distributor.emit_pulled(name, item['end_id'], item['op_no'])
            self._notify_file_added(name)
            self.log_message.emit(f"自同步拉取完成: {name}")
        else:
            self.log_message.emit(f"自同步拉取失败 {name}: {err}")

    # ---- 触发一轮对比 ----

    def request_conflict_pull(self, name: str, src_id: str):
        """冲突覆盖拉取（阶段 3）：远端信号胜出覆盖本端旧内容后，请求该对端
        状态以取得自同步会话，RESP 到达后入拉取队列端到端拉取胜方字节。"""
        if self.mesh is None or not name or not src_id:
            return
        with self._lock:
            self._pending_conflicts[name] = src_id
        try:
            self.mesh.send_to_peer(src_id, Protocol.create_file_state_req(self.end_id))
        except Exception as e:
            with self._lock:
                self._pending_conflicts.pop(name, None)
            self.log_message.emit(f"冲突拉取请求失败: {name} {e}")

    def request_all(self) -> int:
        """向全部直连对端请求文件状态（手动同步/断线重连自动补齐）。

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
        if self._worker and self._worker.is_alive():
            try:
                self._worker.join(timeout=2.0)
            except Exception:
                pass
