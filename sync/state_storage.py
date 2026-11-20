"""
房间状态持久化：SQLite 文件清单 / 墓碑 / 计数器 / 已知端（阶段 A）。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import json
import os
import sqlite3
import threading
import time
from typing import Callable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    rel_path     TEXT PRIMARY KEY,
    file_id      TEXT,
    is_dir       INTEGER NOT NULL DEFAULT 0,
    op_no        INTEGER NOT NULL,
    clock        INTEGER NOT NULL,
    ts           REAL,
    vv           TEXT,
    hash         TEXT,
    hash_state   TEXT,
    size         INTEGER,
    mtime        REAL,
    updated_at   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_id
    ON files(file_id) WHERE file_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tombstones (
    rel_path     TEXT NOT NULL,
    file_id      TEXT NOT NULL DEFAULT '',
    is_dir       INTEGER NOT NULL DEFAULT 0,
    origin       TEXT NOT NULL DEFAULT 'delete',
    op_no        INTEGER NOT NULL,
    clock        INTEGER NOT NULL,
    ts           REAL,
    vv           TEXT,
    deleted_at   REAL,
    confirmed_by TEXT,
    PRIMARY KEY (rel_path, file_id)
);

CREATE TABLE IF NOT EXISTS counters (
    rel_path   TEXT PRIMARY KEY,
    op_no      INTEGER NOT NULL,
    clock      INTEGER NOT NULL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS peers (
    peer_id    TEXT PRIMARY KEY,
    first_seen REAL,
    last_seen  REAL
);
"""

_UPSERT_FILE = """
INSERT INTO files (rel_path, file_id, is_dir, op_no, clock, ts, vv, hash,
                   hash_state, size, mtime, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(rel_path) DO UPDATE SET
    file_id=excluded.file_id, is_dir=excluded.is_dir, op_no=excluded.op_no,
    clock=excluded.clock, ts=excluded.ts, vv=excluded.vv, hash=excluded.hash,
    hash_state=excluded.hash_state, size=excluded.size, mtime=excluded.mtime,
    updated_at=excluded.updated_at
"""

_UPSERT_TOMBSTONE = """
INSERT INTO tombstones (rel_path, file_id, is_dir, origin, op_no, clock, ts,
                        vv, deleted_at, confirmed_by)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(rel_path, file_id) DO UPDATE SET
    is_dir=excluded.is_dir, origin=excluded.origin, op_no=excluded.op_no,
    clock=excluded.clock, ts=excluded.ts, vv=excluded.vv,
    deleted_at=excluded.deleted_at, confirmed_by=excluded.confirmed_by
"""

_UPSERT_COUNTER = """
INSERT INTO counters (rel_path, op_no, clock, updated_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(rel_path) DO UPDATE SET
    op_no=MAX(counters.op_no, excluded.op_no),
    clock=MAX(counters.clock, excluded.clock),
    updated_at=excluded.updated_at
"""

_UPSERT_PEER = """
INSERT INTO peers (peer_id, first_seen, last_seen)
VALUES (?, ?, ?)
ON CONFLICT(peer_id) DO UPDATE SET last_seen=excluded.last_seen
"""


def _loads(text, default):
    try:
        value = json.loads(text) if text else default
    except (ValueError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


class StateStorage:
    """房间 db：内存脏集合 + 定时批量提交，读取前先刷盘保证一致。"""

    SCHEMA_VERSION = 1
    TOMBSTONE_LIMIT = 10000
    COUNTER_LIMIT = 100000       # 计数器行上限（§12.9）：超出后淘汰无引用最旧行
    COUNTER_TRIM_EVERY = 2000    # 累计写入多少计数器行后检查一次上限（限频）

    def __init__(self, db_path: str, flush_interval: float = 0.2,
                 log: Optional[Callable[[str], None]] = None):
        self.db_path = os.path.abspath(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._log = log
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA synchronous=NORMAL')
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            'INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)',
            ('schema_version', str(self.SCHEMA_VERSION)))
        self._conn.commit()

        self._dirty_files = {}     # rel_path -> record / None(删除)
        self._dirty_tomb = {}      # (rel_path, file_id) -> record
        self._dirty_counters = {}  # rel_path -> {op_no, clock}
        self._dirty_peers = {}     # peer_id -> (first_seen, last_seen)
        self._dirty_meta = {}      # key -> value

        self._flush_interval = float(flush_interval or 0)
        self._counter_writes = 0
        self._stop = threading.Event()
        self._thread = None
        if self._flush_interval > 0:
            self._thread = threading.Thread(target=self._flush_loop,
                                            daemon=True)
            self._thread.start()

    # ---- 生命周期 ----

    def _flush_loop(self):
        while not self._stop.wait(self._flush_interval):
            try:
                self.flush()
            except Exception as e:
                self._log_msg(f"状态刷盘失败: {e}")

    def flush(self):
        """把脏集合一次性写入 db。"""
        with self._lock:
            if self._closed:
                return
            files = self._dirty_files
            tombs = self._dirty_tomb
            counters = self._dirty_counters
            peers = self._dirty_peers
            meta = self._dirty_meta
            if not (files or tombs or counters or peers or meta):
                return
            self._dirty_files, self._dirty_tomb = {}, {}
            self._dirty_counters, self._dirty_peers, self._dirty_meta = {}, {}, {}
            try:
                with self._conn:
                    for path, rec in files.items():
                        if rec is None:
                            self._conn.execute(
                                'DELETE FROM files WHERE rel_path=?', (path,))
                            continue
                        self._conn.execute(_UPSERT_FILE, (
                            path, rec.get('file_id'), int(rec.get('is_dir', 0)),
                            int(rec.get('op_no', 0)), int(rec.get('clock', 0)),
                            float(rec.get('ts') or 0.0),
                            json.dumps(rec.get('vv') or {}, ensure_ascii=False),
                            rec.get('hash') or '', rec.get('hash_state') or 'none',
                            rec.get('size'), float(rec.get('mtime') or 0.0),
                            float(rec.get('updated_at') or time.time())))
                    for (path, fid), rec in tombs.items():
                        self._conn.execute(_UPSERT_TOMBSTONE, (
                            path, fid, int(rec.get('is_dir', 0)),
                            rec.get('origin') or 'delete',
                            int(rec.get('op_no', 0)), int(rec.get('clock', 0)),
                            float(rec.get('ts') or 0.0),
                            json.dumps(rec.get('vv') or {}, ensure_ascii=False),
                            float(rec.get('deleted_at') or 0.0),
                            json.dumps(list(rec.get('confirmed_by') or []),
                                       ensure_ascii=False)))
                    for path, rec in counters.items():
                        self._conn.execute(_UPSERT_COUNTER, (
                            path, int(rec.get('op_no', 0)),
                            int(rec.get('clock', 0)), time.time()))
                    for peer_id, seen in peers.items():
                        self._conn.execute(_UPSERT_PEER, (peer_id, seen[0], seen[1]))
                    for key, value in meta.items():
                        self._conn.execute(
                            'INSERT INTO meta(key, value) VALUES(?, ?) '
                            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (key, value))
            except Exception:
                # 写失败回填脏集合，避免状态丢失
                self._dirty_files.update(files)
                self._dirty_tomb.update(tombs)
                self._dirty_counters.update(counters)
                self._dirty_peers.update(peers)
                self._dirty_meta.update(meta)
                raise
            if counters:
                self._counter_writes += len(counters)
                if self._counter_writes >= self.COUNTER_TRIM_EVERY:
                    self._counter_writes = 0
                    self._trim_counters(self.COUNTER_LIMIT)

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        with self._lock:
            if self._closed:
                return
            self.flush()
            self._closed = True
            try:
                self._conn.close()
            except Exception:
                pass

    def _log_msg(self, msg: str):
        if self._log:
            try:
                self._log(msg)
            except Exception:
                pass

    # ---- meta ----

    def set_meta(self, key: str, value):
        with self._lock:
            self._dirty_meta[key] = None if value is None else str(value)

    def get_meta(self, key: str, default=None):
        self.flush()
        with self._lock:
            row = self._conn.execute(
                'SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    # ---- files ----

    def put_file(self, record: dict):
        rec = {
            'file_id': record.get('file_id') or None,
            'is_dir': 1 if record.get('is_dir') else 0,
            'op_no': int(record.get('op_no') or 0),
            'clock': int(record.get('clock') or 0),
            'ts': float(record.get('ts') or 0.0),
            'vv': dict(record.get('vv') or {}),
            'hash': record.get('hash') or '',
            'hash_state': record.get('hash_state') or 'none',
            'size': record.get('size'),
            'mtime': float(record.get('mtime') or 0.0),
            'updated_at': time.time(),
        }
        with self._lock:
            self._dirty_files[record['rel_path']] = rec

    def remove_file(self, rel_path: str):
        with self._lock:
            self._dirty_files[rel_path] = None

    def load_files(self) -> dict:
        self.flush()
        with self._lock:
            rows = self._conn.execute(
                'SELECT rel_path, file_id, is_dir, op_no, clock, ts, vv, hash, '
                'hash_state, size, mtime FROM files').fetchall()
        out = {}
        for (path, fid, is_dir, op_no, clock, ts, vv, hash_, hash_state,
             size, mtime) in rows:
            out[path] = {
                'rel_path': path, 'file_id': fid or '', 'is_dir': int(is_dir),
                'op_no': int(op_no), 'clock': int(clock), 'ts': float(ts or 0.0),
                'vv': _loads(vv, {}), 'hash': hash_ or '',
                'hash_state': hash_state or 'none', 'size': size,
                'mtime': float(mtime or 0.0),
            }
        return out

    # ---- counters ----

    def put_counter(self, rel_path: str, op_no: int, clock: int):
        with self._lock:
            prev = self._dirty_counters.get(rel_path)
            op_no = max(int(op_no), int(prev['op_no']) if prev else 0)
            clock = max(int(clock), int(prev['clock']) if prev else 0)
            self._dirty_counters[rel_path] = {'op_no': op_no, 'clock': clock}

    def load_counters(self) -> dict:
        self.flush()
        with self._lock:
            rows = self._conn.execute(
                'SELECT rel_path, op_no, clock FROM counters').fetchall()
        return {path: {'op_no': int(op_no), 'clock': int(clock)}
                for path, op_no, clock in rows}

    def enforce_counter_limit(self, limit: Optional[int] = None) -> int:
        """计数器超限淘汰：只淘汰既无活对象也无墓碑引用的最旧行。"""
        limit = self.COUNTER_LIMIT if limit is None else int(limit)
        self.flush()
        with self._lock:
            return self._trim_counters(limit)

    def _trim_counters(self, limit: int) -> int:
        """调用方需持有 _lock：删除无 files/tombstones 引用的最旧计数器。"""
        try:
            total = int(self._conn.execute(
                'SELECT COUNT(*) FROM counters').fetchone()[0] or 0)
            excess = total - limit
            if excess <= 0:
                return 0
            cur = self._conn.execute(
                'DELETE FROM counters WHERE rel_path IN ('
                'SELECT c.rel_path FROM counters c '
                'WHERE NOT EXISTS (SELECT 1 FROM files f '
                '                  WHERE f.rel_path=c.rel_path) '
                '  AND NOT EXISTS (SELECT 1 FROM tombstones t '
                '                  WHERE t.rel_path=c.rel_path) '
                'ORDER BY c.updated_at ASC, c.rel_path ASC LIMIT ?)',
                (excess,))
            self._conn.commit()
            removed = max(int(cur.rowcount or 0), 0)
        except sqlite3.Error as e:
            self._log_msg(f"计数器淘汰失败: {e}")
            return 0
        if removed:
            self._log_msg(f"计数器超限淘汰 {removed} 条")
        return removed

    # ---- tombstones ----

    def put_tombstone(self, record: dict):
        key = (record['rel_path'], record.get('file_id') or '')
        rec = {
            'rel_path': key[0], 'file_id': key[1],
            'is_dir': 1 if record.get('is_dir') else 0,
            'origin': record.get('origin') or 'delete',
            'op_no': int(record.get('op_no') or 0),
            'clock': int(record.get('clock') or 0),
            'ts': float(record.get('ts') or 0.0),
            'vv': dict(record.get('vv') or {}),
            'deleted_at': float(record.get('deleted_at') or time.time()),
            'confirmed_by': list(record.get('confirmed_by') or ()),
        }
        with self._lock:
            self._dirty_tomb[key] = rec

    def load_tombstones(self, origin: Optional[str] = None) -> dict:
        self.flush()
        with self._lock:
            rows = self._conn.execute(
                'SELECT rel_path, file_id, is_dir, origin, op_no, clock, ts, '
                'vv, deleted_at, confirmed_by FROM tombstones').fetchall()
        out = {}
        for (path, fid, is_dir, org, op_no, clock, ts, vv, deleted_at,
             confirmed) in rows:
            if origin is not None and org != origin:
                continue
            out[(path, fid)] = {
                'rel_path': path, 'file_id': fid, 'is_dir': int(is_dir),
                'origin': org, 'op_no': int(op_no), 'clock': int(clock),
                'ts': float(ts or 0.0), 'vv': _loads(vv, {}),
                'deleted_at': float(deleted_at or 0.0),
                'confirmed_by': _loads(confirmed, []),
            }
        return out

    def confirm_tombstone(self, rel_path: str, file_id: str,
                          peer_id: str) -> bool:
        """记录某端已确认该 (path, id) 删除；已确认过返回 False。"""
        if not peer_id:
            return False
        key = (rel_path, file_id or '')
        with self._lock:
            rec = self._dirty_tomb.get(key)
        if rec is None:
            rec = self.load_tombstones().get(key)
        if rec is None:
            return False
        confirmed = list(rec.get('confirmed_by') or ())
        if peer_id in confirmed:
            return False
        confirmed.append(peer_id)
        rec = dict(rec, confirmed_by=confirmed)
        with self._lock:
            self._dirty_tomb[key] = rec
        return True

    def remove_tombstone(self, rel_path: str, file_id: str = '') -> bool:
        """删除指定墓碑行（加载时清理缺文件ID的无效删除记录）。"""
        key = (rel_path, file_id or '')
        with self._lock:
            self._dirty_tomb.pop(key, None)
            with self._conn:
                cur = self._conn.execute(
                    'DELETE FROM tombstones WHERE rel_path=? AND file_id=?', key)
            return bool(cur.rowcount)

    def gc_tombstones(self) -> int:
        """清理全体已知端已确认、且本地无同 id 活对象的删除墓碑。"""
        self.flush()
        peers = self.load_peers()
        if not peers:
            return 0
        tombs = self.load_tombstones(origin='delete')
        active = {rec['file_id'] for rec in self.load_files().values()
                  if rec.get('file_id')}
        keys = [key for key, rec in tombs.items()
                if set(rec.get('confirmed_by') or ()) >= peers
                and rec['file_id'] not in active]
        if not keys:
            return 0
        with self._lock:
            with self._conn:
                self._conn.executemany(
                    'DELETE FROM tombstones WHERE rel_path=? AND file_id=?', keys)
            for key in keys:
                self._dirty_tomb.pop(key, None)
        return len(keys)

    def enforce_tombstone_limit(self, limit: Optional[int] = None) -> int:
        """超限淘汰：优先 origin=merge，再淘汰最旧的删除墓碑。"""
        limit = self.TOMBSTONE_LIMIT if limit is None else int(limit)
        self.flush()
        rows = list(self.load_tombstones().items())
        excess = len(rows) - limit
        if excess <= 0:
            return 0
        rows.sort(key=lambda kv: (0 if kv[1]['origin'] == 'merge' else 1,
                                  kv[1]['deleted_at'] or 0.0))
        evicted = rows[:excess]
        with self._lock:
            with self._conn:
                self._conn.executemany(
                    'DELETE FROM tombstones WHERE rel_path=? AND file_id=?',
                    [key for key, _ in evicted])
            for key, _ in evicted:
                self._dirty_tomb.pop(key, None)
        self._log_msg(f"墓碑超限淘汰 {excess} 条: {[k[0] for k, _ in evicted]}")
        return excess

    # ---- peers ----

    def add_peer(self, peer_id: str):
        if not peer_id:
            return
        now = time.time()
        with self._lock:
            first = self._dirty_peers.get(peer_id, (now, now))[0]
            self._dirty_peers[peer_id] = (first, now)

    def load_peers(self) -> set:
        self.flush()
        with self._lock:
            rows = self._conn.execute('SELECT peer_id FROM peers').fetchall()
        return {row[0] for row in rows}


def open_room_state_storage(distributor, db_path: str, log=None,
                            reconcile: bool = True):
    """生产接线：打开房间 db 并挂到分发引擎；reconcile=启动磁盘对账。

    调用方负责异常降级（内存模式），返回的 storage 由调用方断开时 close。
    """
    storage = StateStorage(db_path, log=log)
    try:
        distributor.attach_storage(storage)
        if reconcile:
            distributor.reconcile_disk()
    except Exception:
        storage.close()
        raise
    return storage
