"""
在线更新模块
解析官网更新清单、下载安装包并清理更新缓存。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.

清单格式（官网纯文本；第一行必须是版本号，保证旧客户端仍能按原逻辑识别）：
R1.2.2.0
# win_sha256: <64 位十六进制>
# win_url_zh_CN: `https://.../LANSyncBox_Setup.exe`
# win_url_en_US: `https://.../LANSyncBox_Setup.exe`
# log
- 条目一
- 条目二

解析阶段只负责尽力提取字段：sha256 / 下载地址缺失或非法时不抛错，
由真正下载的 download_package 校验。这样远程版本不比本地新时，
调用方只看到"已是最新"，不会因清单字段问题报"检查更新失败"。
"""
import hashlib
import os
import re
import shutil
import ssl
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

from config import Config

CHUNK_SIZE = 1024 * 1024  # 下载分块 1MB


class UpdateError(Exception):
    """更新清单非法、下载失败或校验不通过。"""


class UpdateInfo:
    """一次更新检查结果。"""

    __slots__ = ('version', 'sha256', 'url_zh_cn', 'url_en_us', 'changelog', 'size')

    def __init__(self, version: str, sha256: str, url_zh_cn: str = '',
                 url_en_us: str = '', changelog: Optional[List[str]] = None,
                 size: Optional[int] = None):
        self.version = version
        self.sha256 = sha256
        self.url_zh_cn = url_zh_cn
        self.url_en_us = url_en_us
        self.changelog = list(changelog or [])
        self.size = size

    def url_for(self, language: str) -> str:
        """按界面语言选下载地址（中文优先中文地址，其余走英文地址）。"""
        if str(language).lower().startswith('zh'):
            return self.url_zh_cn or self.url_en_us
        return self.url_en_us or self.url_zh_cn

    def __repr__(self):
        return f"UpdateInfo({self.version!r})"


_VERSION_RE = re.compile(r'R\d+\.\d+\.\d+\.\d+')
_SHA256_RE = re.compile(r'[0-9a-fA-F]{64}')


def version_tuple(version: str):
    """提取四段版本号元组；无法解析返回 None。"""
    match = re.search(r'R(\d+)\.(\d+)\.(\d+)\.(\d+)', version or '')
    return tuple(map(int, match.groups())) if match else None


def is_newer(latest: str, current: str) -> bool:
    """latest 是否比 current 新（四段逐段比较）。"""
    a, b = version_tuple(latest), version_tuple(current)
    return bool(a and b and a > b)


def _clean(value: str) -> str:
    """去掉首尾空白与反引号（清单里的 URL 可能被反引号包裹）。"""
    return value.strip().strip('`').strip()


def parse_manifest(text: str) -> UpdateInfo:
    """解析官网更新清单。非法内容抛 UpdateError。"""
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if not lines:
        raise UpdateError('更新清单为空')

    # 第一行是版本号；容忍旧写法带 "# " 前缀
    first = _clean(lines[0].lstrip('#'))
    if not _VERSION_RE.fullmatch(first):
        raise UpdateError('无法解析更新清单中的版本号')
    version = first

    meta = {}
    changelog = []
    in_log = False
    for line in lines[1:]:
        if line.startswith('#'):
            body = line[1:].strip()
            if body.lower() == 'log':
                in_log = True
                continue
            if in_log:
                continue
            key, sep, value = body.partition(':')
            if sep:
                meta[key.strip().lower()] = _clean(value)
            continue
        if in_log and line.startswith('-'):
            item = line[1:].strip()
            if item:
                changelog.append(item)

    # win_sha256 为唯一字段；非法值按缺失处理
    sha256 = meta.get('win_sha256') or ''
    if not _SHA256_RE.fullmatch(sha256):
        sha256 = ''

    url_zh = meta.get('win_url_zh_cn', '')
    url_en = meta.get('win_url_en_us', '')

    size = meta.get('size', '')
    try:
        size_value = int(size) if size else None
    except ValueError:
        size_value = None

    return UpdateInfo(version, sha256, url_zh, url_en, changelog, size_value)


def fetch_manifest(url: str = None, timeout: float = 8.0) -> UpdateInfo:
    """下载并解析更新清单（网络/解析异常统一抛 UpdateError）。"""
    target = url or Config.UPDATE_URL
    # 优先用 certifi 根证书（打包环境/部分系统证书库缺失时会 CERTIFICATE_VERIFY_FAILED）
    try:
        import certifi
        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_context = ssl.create_default_context()

    request = urllib.request.Request(target)
    request.add_header('User-Agent', Config.APP_NAME)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            body = response.read().decode('utf-8', errors='replace')
    except Exception as e:
        raise UpdateError(f'无法获取更新清单: {e}') from e
    return parse_manifest(body)


def package_path(info: UpdateInfo) -> Path:
    """安装包在本机缓存路径（配置目录 updates/ 下，按版本命名）。"""
    safe = re.sub(r'[^0-9A-Za-z._-]', '_', info.version) or 'latest'
    return Config.get_updates_dir() / f'LANSyncBox_Setup_{safe}.exe'


def download_package(info: UpdateInfo, language: str,
                     progress: Optional[Callable[[int, int], None]] = None,
                     cancel=None,
                     on_verify: Optional[Callable[[], None]] = None) -> Path:
    """下载安装包到本机缓存并校验 sha256，返回落地路径。

    progress(done, total)：total 为 0 表示长度未知；cancel 为 threading.Event，
    置位后抛 UpdateError（已下载部分保留为 .part，供下次续传）。
    on_verify()：字节下载完成、开始 sha256 校验前回调，供界面切换提示。
    """
    url = info.url_for(language)
    if not url:
        raise UpdateError('更新清单未提供当前语言的下载地址')
    if not _SHA256_RE.fullmatch(info.sha256):
        raise UpdateError('更新清单缺少合法的 win_sha256（需 64 位十六进制）')

    target = package_path(info)
    part = target.with_name(target.name + '.part')
    target.unlink(missing_ok=True)

    existing = part.stat().st_size if part.exists() else 0
    headers = {'User-Agent': Config.APP_NAME}
    if existing:
        headers['Range'] = f'bytes={existing}-'

    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=20)
    except Exception as e:
        raise UpdateError(f'无法下载安装包: {e}') from e

    with response:
        status = getattr(response, 'status', 200)
        if existing and status != 206:
            existing = 0  # 服务端不支持断点续传，从头写入
        content_length = int(response.headers.get('Content-Length') or 0)
        total = (existing + content_length) if existing else (content_length or (info.size or 0))
        done = existing
        if progress:
            progress(done, total)

        mode = 'ab' if existing else 'wb'
        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateError('已取消下载')
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    if info.size and done != info.size:
        part.unlink(missing_ok=True)
        raise UpdateError(f'安装包大小不符: 期望 {info.size}，实际 {done}')

    if on_verify:
        on_verify()

    digest = hashlib.sha256()
    with open(part, 'rb') as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b''):
            digest.update(chunk)
    if digest.hexdigest().lower() != info.sha256.lower():
        part.unlink(missing_ok=True)
        raise UpdateError('安装包校验失败（sha256 不匹配）')

    os.replace(part, target)
    return target


def clear_cache():
    """清空更新缓存目录（主程序启动时调用）；失败静默忽略。"""
    directory = Config.get_updates_dir()
    for entry in directory.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            pass
