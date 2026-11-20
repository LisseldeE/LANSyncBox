"""
文件身份：file_id 生成/校验与同内容不同 ID 的合并裁决（阶段 A）。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import re
import uuid

_FILE_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def new_file_id() -> str:
    """生成文件身份：uuid4().hex（32 位小写十六进制）。"""
    return uuid.uuid4().hex


def is_valid_file_id(value) -> bool:
    """校验 file_id 是否为 uuid4().hex 形式。"""
    return isinstance(value, str) and bool(_FILE_ID_RE.match(value))


def pick_same_content_winner(a: dict, b: dict) -> dict:
    """同路径同内容的两个身份择一：clock 高 → ts 晚 → id 字典序大者胜。"""
    for key in ('clock', 'ts'):
        av = a.get(key, 0) or 0
        bv = b.get(key, 0) or 0
        if av != bv:
            return a if av > bv else b
    return a if str(a.get('id', '')) >= str(b.get('id', '')) else b
