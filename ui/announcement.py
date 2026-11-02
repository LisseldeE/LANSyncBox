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

# 定向目标的语言后缀（与 I18n.set_language 的取值一致）
_LANG_SUFFIXES = ("zh_CN", "en_US")


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


def _split_target(target: str):
    """拆分定向目标为 (基础目标, 语言后缀)；无语言后缀时语言为 None。

    'R1.2.1.0_zh_CN' → ('R1.2.1.0', 'zh_CN')；'ALL' → ('ALL', None)
    """
    for lang in _LANG_SUFFIXES:
        if target.endswith("_" + lang):
            return target[:-(len(lang) + 1)], lang
    return target, None


def _select_for_version(body: str, local_version: str, lang: str = "") -> str:
    """按本端程序版本号与界面语言从公告正文中选取唯一显示文本。

    公告正文从第二行起，可含若干行版本的定向指令行，形如：
        # ALL_zh_CN：所有人可见的中文正式内容
        # ALL_en_US：英文正式内容
        # R1.0.0.0_zh_CN：仅该版本可见的中文专属内容
        # R1.0.0.0：不带语言后缀时对所有语言生效
    - 按以下优先级返回第一条命中：本端版本+当前语言 → 本端版本（无后缀）
      → ALL+当前语言 → ALL（无后缀）；
    - 命中的行只返回该行正文，多行绝不拼接；
    - 全部未命中则返回空串（本端不该显示内容）。

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

    # 存在定向行视为 v2 定向格式：按版本+语言优先级精确匹配，绝不拼接
    if targeted:
        wanted = [
            (local_version, lang or None),
            (local_version, None),
            ('ALL', lang or None),
            ('ALL', None),
        ]
        for want_base, want_lang in wanted:
            for target, text in targeted:
                base, target_lang = _split_target(target)
                if base == want_base and target_lang == want_lang:
                    return text
        return ""  # 无匹配专属、也无 ALL：本端不该显示内容

    # 无定向行：兼容老格式，返回全部正文
    return '\n'.join(raw_lines).strip()


def fetch_announcement():
    """从远程拉取公告文件，并按本端版本分别解析各语言的正文。

    文件格式（v2 定向投递）：
        第一行：公告批次号（如 26.9.15.2）
        其后若干行：版本定向指令行 "# 目标：正文"，目标可带语言后缀
                     （如 "# ALL_zh_CN：…"、"# R1.2.1.0_en_US：…"）
    返回: (批次号字符串, 各语言正文 dict) 元组
        成功: ("26.9.15.2", {"zh_CN": "中文正文", "en_US": "英文正文"})
        失败: (None, "错误描述")；格式非法: (None, None)
        本端该语言无内容: ("26.9.15.2", {"zh_CN": "", "en_US": ""})
                          —— 批次号已更新但本端无正文可显示
    """
    # 优先使用 certifi 自带 CA 根证书（macOS 及打包环境的系统证书库常缺失，
    # 会导致 CERTIFICATE_VERIFY_FAILED）；certifi 缺失时回退默认上下文。
    # 证书异常由下方 except 转为返回错误，调用方静默降级。
    try:
        import certifi
        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_context = ssl.create_default_context()

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
    body_text = '\n'.join(lines[1:])
    texts = {
        lang: _select_for_version(body_text, Config.APP_VERSION, lang)
        for lang in _LANG_SUFFIXES
    }
    return version, texts
