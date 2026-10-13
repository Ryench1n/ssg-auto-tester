"""Илрэлийн текстийг normalize хийж ижил bug-ийг нэгтгэх signature гаргана."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app import models as M
from app.models import Finding


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------
_GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def normalize_text(text: str, limit: int = 200) -> str:
    """Текстийн хэв: GUID, тоог арилгаж, хоосон зайг нэгтгэнэ."""
    t = _GUID.sub("<guid>", text or "")
    t = _HEX.sub("<hex>", t)
    t = _NUM.sub("#", t)
    t = _WS.sub(" ", t).strip()
    return t[:limit]


def first_exception_line(text: str) -> str:
    """Stack trace-ээс 'System.XxxException: message' мөрийг олно."""
    for line in (text or "").splitlines():
        line = line.strip()
        if re.search(r"[A-Za-z.]+Exception\b", line) and not line.startswith("at "):
            return line.replace("Unhandled Exception:", "").strip() or line
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[0] if lines else ""


def _url_pattern(url: str) -> str:
    p = urlparse(url or "")
    return normalize_text(p.path or url, 120)


def signature(f: Finding) -> str:
    k = f.kind
    if k in (M.PAGE_RELOAD, M.SESSION_LOGOUT, M.LOGIN_FAILED, M.NO_TAB, M.TAB_NAME_MISMATCH,
             M.LOADING_STUCK, M.TIMEOUT, M.FORBIDDEN, M.BLANK_PAGE, M.SLOW_LOAD, M.TAB_WRONG_PAGE):
        return k
    if k == M.RUNTIME_CRASH:
        return f"{k}|{normalize_text(first_exception_line(f.detail or f.text), 160)}"
    if k in (M.HTTP_5XX, M.HTTP_4XX):
        return f"{k}|{f.status}|{_url_pattern(f.url)}"
    return f"{k}|{normalize_text(f.text)}"
