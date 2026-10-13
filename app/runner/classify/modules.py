"""Модулийн нэр → латин товчлол (TC-[MODULE]-[№])."""
from __future__ import annotations

import re


# --------------------------------------------------------------------------
# Модулийн товчлол
# --------------------------------------------------------------------------
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "ө": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ү": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in (text or "").lower())


def auto_module_code(name: str) -> str:
    words = [w for w in re.split(r"[^\wА-Яа-яӨөҮүЁё]+", transliterate(name)) if w]
    if not words:
        return "MOD"
    if len(words) == 1:
        return re.sub(r"[^a-z0-9]", "", words[0])[:4].upper() or "MOD"
    return "".join(w[0] for w in words)[:5].upper()


class ModuleCoder:
    def __init__(self, cfg: dict):
        self.map = dict((cfg or {}).get("module_codes") or {})
        self.used: dict[str, str] = {}

    def code(self, module: str) -> str:
        """YAML mapping-тай бол түүнийг (хоёр модуль нэг код хуваалцаж болно — TC дугаар
        нэг тоолуураар үргэлжилдэг тул ID давхцахгүй). Үгүй бол транслитерац хийж, өөр
        модулийн кодтой давхцвал тоо залгана."""
        if module in self.used:
            return self.used[module]
        mapped = self.map.get(module) or self.map.get(module.strip())
        if mapped:
            code = str(mapped)
        else:
            base = code = auto_module_code(module)
            taken = set(self.used.values()) | {str(v) for v in self.map.values()}
            n = 2
            while code in taken:
                code, n = f"{base}{n}", n + 1
        self.used[module] = code
        return code
