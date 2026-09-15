"""
公告拉取模块
启动时后台拉取远程公告文件，解析版本号并比对，供主界面胶囊提示显示
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import re
import ssl
import urllib.request

from config import Config

# 公告版本号：日期 + 当日序号（如 26.9.15.1）
_VERSION_RE = re.compile(r'^(\d{2})\.(\d{1,2})\.(\d{1,2})\.(\d+)$')


def _parse_version(ver: str):
    """解析公告版本号 '26.9.15.1' → (26, 9, 15, 1)；非法格式返回 None"""
    m = _VERSION_RE.match(ver.strip())
    if m is None:
        return None
    return tuple(int(g) for g in m.groups())


def is_newer(remote: str, stored: str) -> bool:
    """remote 是否新于 stored（按日期+序号元组比较）。

    记录为空视为首次启动：拉取到合法版本即视为新公告返回 True。
    remote 非法返回 False（不显示也不更新记录，下次启动重试）。
    """
    r = _parse_version(remote)
    if r is None:
        return False
    if not stored:
        return True
    s = _parse_version(stored)
    if s is None:
        return True
    return r > s


def fetch_announcement():
    """从远程拉取公告文件。

    文件格式：第一行版本号（如 26.9.15.1），其后为公告正文。
    返回: (版本号字符串, 公告正文) 元组
        成功: ("26.9.15.1", "公告：XXX")
        失败: (None, "错误描述")；格式非法: (None, None)
    """
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(Config.ANNOUNCEMENT_URL)
    req.add_header('User-Agent', Config.APP_NAME)

    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl_context) as response:
            body = response.read().decode('utf-8').strip()
    except Exception as e:
        return None, str(e)

    lines = body.splitlines()
    if not lines or _parse_version(lines[0].strip()) is None:
        return None, None
    version = lines[0].strip()
    text = '\n'.join(lines[1:]).strip()
    return version, text
