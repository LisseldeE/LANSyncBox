"""
房间实例编号：读取/生成/继承与加入前缓存判定（阶段 D）。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import sqlite3
import time
import uuid

from config import Config
from sync.file_manager import safe_rmtree

JOIN_NO_DB_EMPTY = 'no_db_empty'
JOIN_NO_DB_NONEMPTY = 'no_db_nonempty'
JOIN_MATCH = 'match'
JOIN_MISMATCH = 'mismatch'

_META_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""


def room_db_path(room_code: str, base_dir: str = None) -> str:
    """房间 db 路径：<数据目录>/db/<房间号>.db。"""
    base = base_dir or str(Config.get_data_dir())
    return os.path.join(base, 'db', f'{room_code}.db')


def read_room_instance_id(db_path: str) -> str:
    """读取本地 db 中的房间实例编号；db 不存在或无记录返回空串。"""
    if not db_path or not os.path.isfile(db_path):
        return ''
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='room_instance_id'").fetchone()
        return str(row[0]) if row and row[0] else ''
    except sqlite3.Error:
        return ''
    finally:
        if conn is not None:
            conn.close()


def write_room_instance_id(db_path: str, room_instance_id: str):
    """把房间实例编号写入本地 db（仅建 meta 表，不引入状态持久化）。"""
    if not db_path or not room_instance_id:
        return
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(_META_SQL)
            conn.execute(
                'INSERT INTO meta(key, value) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('room_instance_id', str(room_instance_id)))
    finally:
        conn.close()


def resolve_host_instance_id(db_path: str, probe=None, timeout: float = 0.8) -> str:
    """主机编号：已有则复用；否则探活继承；再否则生成 UUID4 并落库。"""
    existing = read_room_instance_id(db_path)
    if existing:
        return existing
    inherited = ''
    if probe is not None:
        try:
            inherited = str(probe(timeout) or '')
        except Exception:
            inherited = ''
    rid = inherited or uuid.uuid4().hex
    write_room_instance_id(db_path, rid)
    return rid


def _has_entries(room_folder) -> bool:
    """目录内是否有用户缓存内容：程序自动补建的提示文件不算（首次加入不误报）。"""
    try:
        with os.scandir(str(room_folder)) as it:
            return any(not Config.is_sync_notice(entry.name) for entry in it)
    except OSError:
        return False


def inspect_join(db_path: str, room_folder, remote_id: str) -> str:
    """加入前判定：无 db 空目录/无 db 非空/同号/异号。"""
    remote_id = str(remote_id or '')
    if not os.path.isfile(db_path):
        return JOIN_NO_DB_EMPTY if not _has_entries(room_folder) \
            else JOIN_NO_DB_NONEMPTY
    local_id = read_room_instance_id(db_path)
    if not local_id:
        return JOIN_MATCH if not remote_id else JOIN_MISMATCH
    if not remote_id or local_id == remote_id:
        return JOIN_MATCH
    return JOIN_MISMATCH


def discard_room_db(db_path: str) -> bool:
    """删除房间 db（含 WAL/SHM 附属文件），不触碰房间文件。

    Windows 上句柄释放后仍可能被索引/杀毒软件短暂占用，删除失败时短暂
    重试；仍失败则返回 False 并打印具体文件，便于定位占用来源。
    """
    if not db_path:
        return True
    for suffix in ('', '-wal', '-shm', '-journal'):
        target = db_path + suffix
        for attempt in range(3):
            try:
                os.remove(target)
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                if attempt == 2:
                    print(f"删除房间库失败 {target}: {exc}")
                    return False
                time.sleep(0.1)
    return True


def clear_room_cache(room_folder, db_path: str) -> bool:
    """先清空房间目录，全部成功后才删除房间 db；失败保留 db。

    房间目录可能包含从只读源同步来的文件/目录，Windows 下直接
    shutil.rmtree 会因 ReadOnly 属性失败；改用 safe_rmtree 清属性后重试。
    """
    folder = str(room_folder)
    try:
        if os.path.isdir(folder):
            safe_rmtree(folder)
            os.makedirs(folder, exist_ok=True)
        elif os.path.exists(folder):
            os.remove(folder)
            os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        print(f"清理房间目录失败 {folder}: {exc}")
        return False
    return discard_room_db(db_path)
