"""
端昵称派生：由 end_id 稳定映射出可读昵称（语言中立取词 + 按语言渲染）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import hashlib

_ADJ = {
    "zh_CN": ["青", "白", "玄", "素", "赤", "朱", "绯", "橙", "金", "黄",
              "翠", "碧", "靛", "紫", "墨", "灰", "霜", "雪", "星", "夜",
              "风", "雾", "焰", "雷"],
    "en_US": ["Teal", "White", "Black", "Ivory", "Crimson", "Vermilion",
              "Scarlet", "Amber", "Gold", "Yellow", "Jade", "Azure",
              "Indigo", "Violet", "Ink", "Ash", "Frost", "Snow",
              "Star", "Night", "Wind", "Mist", "Flame", "Thunder"],
}
_NOUN = {
    "zh_CN": ["狐", "狼", "鹿", "鹤", "鲸", "枭", "鹰", "鸢", "豹", "虎",
              "熊", "兔", "猫", "蝶", "萤", "鲤", "汐", "川", "岳", "林",
              "渊", "穹", "痕", "铃"],
    "en_US": ["Fox", "Wolf", "Deer", "Crane", "Whale", "Owl", "Eagle",
              "Kite", "Leopard", "Tiger", "Bear", "Hare", "Cat",
              "Butterfly", "Firefly", "Koi", "Tide", "River", "Peak",
              "Forest", "Abyss", "Vault", "Trace", "Bell"],
}
_DEFAULT_LANG = "zh_CN"


def _pick(end_id: str, lang: str):
    """由 end_id 取（形容词, 名词）索引对；索引与语言无关，仅用于查表。"""
    digest = hashlib.sha256(("nlsb:" + end_id).encode("utf-8")).hexdigest()
    h = int(digest, 16)
    adjs = _ADJ.get(lang) or _ADJ[_DEFAULT_LANG]
    nouns = _NOUN.get(lang) or _NOUN[_DEFAULT_LANG]
    return adjs[h % len(adjs)], nouns[(h // len(adjs)) % len(nouns)]


def nickname(end_id: str, lang: str = None) -> str:
    """end_id → 可读昵称（如 青狐·3f2a / Teal Fox·3f2a）。

    lang 省略时取当前界面语言（I18n.get_language()）；end_id 为空返回 ''。
    """
    if not end_id:
        return ""
    if lang is None:
        try:
            from i18n import I18n
            lang = I18n.get_language()
        except Exception:
            lang = _DEFAULT_LANG
    adj, noun = _pick(end_id, lang)
    base = f"{adj} {noun}" if lang == "en_US" else f"{adj}{noun}"
    return f"{base}·{end_id[:4]}"


def short_code(end_id: str) -> str:
    """昵称尾缀（end_id 前 4 位）。"""
    return end_id[:4] if end_id else ""