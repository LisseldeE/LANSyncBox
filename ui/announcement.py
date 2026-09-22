"""
公告拉取模块
启动时后台拉取远程公告文件，解析批次号与版本定向正文，供主界面胶囊提示显示
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import re
import ssl
import urllib.request

from config import Config

# 公告批次号：日期 + 当日序号（如 26.9.15.2）
_VERSION_RE = re.compile(r'^(\d{2})\.(\d{1,2})\.(\d{1,2})\.(\d+)$')

# 版本定向指令行：普通公告正文也可能含冒号，仅锚定行首 "# 版本：正文" 形式
_TARGETED_RE = re.compile(r'^#\s*([^：:]+?)\s*[：:]\s*(.*)$')


def _parse_version(ver: str):
    """解析公告批次号 '26.9.15.2' → (26, 9, 15, 2)；非法格式返回 None"""
    m = _VERSION_RE.match(ver.strip())
    if m is None:
        return None
    return tuple(int(g) for g in m.groups())


def is_newer(remote: str, stored: str) -> bool:
    """remote 是否新于 stored（按日期+序号元组比较）。

    记录为空视为首次启动：拉取到合法批次即视为新公告返回 True。
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


def _select_for_version(body: str, local_version: str) -> str:
    """按本端程序版本号从公告正文中选取唯一显示文本。

    公告正文从第二行起，可含若干行版本的定向指令行，形如：
        # ALL：所有人可见的正式内容
        # R1.0.0.0：仅该版本可见的专属内容
    - 若存在与本端 APP_VERSION 完全一致的定向行，则只返回该专属正文；
    - 否则返回 ALL 行正文；
    - 两者绝不拼接；失效行（无匹配版本、无 ALL）直接丢弃。

    兼容无定向行的旧式正文：整体不是 "# 版本：" 形式时，原样返回全部正文。
    """
    if not body:
        return ""

    lines = body.strip().splitlines()
    targeted = []          # 已解析出的定向 (目标, 正文)
    raw_lines = []         # 非定向行原文
    for ln in lines:
        m = _TARGETED_RE.match(ln.strip())
        if m:
            targeted.append((m.group(1).strip(), m.group(2).strip()))
        else:
            raw_lines.append(ln)

    # 存在定向行视为 v2 定向格式：精确匹配本端版本，否则回退 ALL，绝不拼接
    if targeted:
        for target, text in targeted:
            if target == local_version:
                return text
        for target, text in targeted:
            if target == 'ALL':
                return text
        return ""  # 无匹配专属、也无 ALL：本端不该显示内容

    # 无定向行：兼容老格式，返回全部正文
    return '\n'.join(raw_lines).strip()


def fetch_announcement():
    """从远程拉取公告文件，并按本端版本选取应显示的唯一正文。

    文件格式（v2 定向投递）：
        第一行：公告批次号（如 26.9.15.2）
        其后若干行：版本定向指令行 "# 版本：正文"，其中 "# ALL：" 为通用正文
    返回: (批次号字符串, 本端应显示的正文) 元组
        成功: ("26.9.15.2", "正文（专属或 ALL）")
        失败: (None, "错误描述")；格式非法: (None, None)
        本端无匹配内容: ("26.9.15.2", "")——批次号已更新但本端无正文可显示
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
    text = _select_for_version('\n'.join(lines[1:]), Config.APP_VERSION)
    return version, text
