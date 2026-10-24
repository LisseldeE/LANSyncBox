"""
文件搜索后台扫描
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal


def _is_temp_name(name: str) -> bool:
    """传输临时文件（与 FileManager.get_file_list_for_sync 的跳过规则一致）。"""
    return name.endswith('.tmp') or (name.startswith('.tcp_')
                                     and name.endswith('.part'))


class FileSearchWorker(QThread):
    """在后台递归扫描目录并按文件名匹配。

    结果通过 results_ready(generation, results, scanned, truncated) 回传；
    results 为 dict 列表，字段：path / name / is_dir / size / mtime / parent。
    扫描带条目数、时间、结果数三重预算，任何一项触顶即停止并标记 truncated。
    """

    results_ready = Signal(int, object, int, bool)

    def __init__(self, root, keyword: str, generation: int, parent=None,
                 max_results: int = 200, max_entries: int = 20000,
                 max_seconds: float = 5.0):
        super().__init__(parent)
        self._root = Path(root)
        self._keyword = (keyword or '').strip().lower()
        self._generation = generation
        self._max_results = max_results
        self._max_entries = max_entries
        self._max_seconds = max_seconds
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        if not self._keyword or self._cancel.is_set():
            return

        root = self._root
        results = []
        scanned = 0
        truncated = False
        start = time.monotonic()
        last_check = start
        stack = [root]

        try:
            while stack:
                if self._cancel.is_set():
                    return
                directory = stack.pop()
                try:
                    iterator = os.scandir(directory)
                except OSError:
                    continue

                try:
                    for entry in iterator:
                        if self._cancel.is_set():
                            return
                        scanned += 1
                        if scanned > self._max_entries:
                            truncated = True
                            break

                        now = time.monotonic()
                        if now - last_check >= 0.2:
                            last_check = now
                            if now - start > self._max_seconds:
                                truncated = True
                                break

                        name = entry.name
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            is_file = entry.is_file(follow_symlinks=False)
                        except OSError:
                            continue
                        if not (is_dir or is_file):
                            continue

                        # 先入栈，保证命中文件夹时其子项也会被扫描
                        if is_dir:
                            stack.append(Path(entry.path))

                        if _is_temp_name(name):
                            continue
                        if self._keyword not in name.lower():
                            continue

                        size = 0
                        mtime = 0.0
                        if is_file:
                            try:
                                st = entry.stat(follow_symlinks=False)
                                size = st.st_size
                                mtime = st.st_mtime
                            except OSError:
                                pass

                        try:
                            parent = str(
                                Path(entry.path).parent.relative_to(root))
                        except ValueError:
                            parent = '.'
                        parent = parent.replace('\\', '/')
                        if not parent:
                            parent = '.'

                        results.append({
                            'path': entry.path,
                            'name': name,
                            'is_dir': is_dir,
                            'size': size,
                            'mtime': mtime,
                            'parent': parent,
                        })
                        if len(results) >= self._max_results:
                            truncated = True
                            break
                finally:
                    try:
                        iterator.close()
                    except Exception:
                        pass

                if truncated:
                    break
        finally:
            if self._cancel.is_set():
                return
            results.sort(key=lambda r: (r['parent'].lower(),
                                        not r['is_dir'],
                                        r['name'].lower()))
            self.results_ready.emit(
                self._generation, results, scanned, truncated)
