"""Нэвтрэлт (ЗӨВХӨН гараар), хувилбар унших.

Нууц үг системд хэзээ ч ордоггүй: хэрэглэгч нээгдсэн (headed) хөтөч дээр өөрөө нэвтэрнэ,
энэ модуль зөвхөн `selectors.app_ready` гарч ирэхийг хүлээнэ.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Callable
from urllib.parse import urljoin, urlparse

from app.runner.detectors import snapshot


class LoginError(Exception):
    pass


class LoginCancelled(LoginError):
    """Хэрэглэгч нэвтрэлтийг хүлээж байх зуур Зогсоох дарсан."""


class LoginTimeout(LoginError):
    """Гар нэвтрэлтийг хүлээх хугацаа (manual_login_timeout_ms) дууссан."""


def base_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def login_url(url: str, cfg: dict) -> str:
    return urljoin(base_url(url), (cfg.get("session") or {}).get("login_path", "/auth/login").lstrip("/"))


def is_login_url(url: str, cfg: dict) -> bool:
    return (cfg.get("session") or {}).get("login_path", "/auth/login") in (urlparse(url).path or "")


async def read_version(page, cfg: dict) -> str:
    try:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""
    m = re.search(cfg["selectors"]["version_regex"], text or "")
    return m.group(0) if m else ""


async def wait_login_or_app(page, cfg: dict, timeout_ms: int,
                            cancel_event: "asyncio.Event | None" = None) -> str:
    """'login' | 'app' | 'timeout' | 'cancelled' — networkidle-д найдахгүй, DOM элемент хүлээнэ."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        st = await snapshot(page, cfg)
        if st:
            if st.appReady and not is_login_url(st.url, cfg):
                return "app"
            if st.hasPassword:
                return "login"
        await asyncio.sleep(0.3)
    return "timeout"


async def login(page, url: str, cfg: dict, navigate: bool = True,
                on_manual_wait: Callable[[float], None] | None = None,
                cancel_event: "asyncio.Event | None" = None, timeout_ms: int | None = None) -> str:
    """Хэрэглэгч хөтөч дээр ГАРААР нэвтрэхийг хүлээж, хувилбарыг буцаана.

    navigate=True бол `/auth/login` руу очно. Нэвтрэх хуудас гарвал `on_manual_wait(timeout_s)`
    дуудаж, `selectors.app_ready` гарч ирэх хүртэл (дээд тал нь manual_login_timeout_ms) хүлээнэ.
    """
    t = cfg["timing"]
    if navigate:
        await page.goto(login_url(url, cfg), wait_until="domcontentloaded", timeout=t["app_ready_timeout_ms"])
    state = await wait_login_or_app(page, cfg, t["app_ready_timeout_ms"], cancel_event)
    if state == "cancelled":
        raise LoginCancelled("Хэрэглэгч зогсоолоо")
    if state == "app":
        return await read_version(page, cfg)
    if state == "timeout":
        await page.goto(login_url(url, cfg), wait_until="domcontentloaded", timeout=t["app_ready_timeout_ms"])
        st2 = await wait_login_or_app(page, cfg, t["app_ready_timeout_ms"], cancel_event)
        if st2 == "cancelled":
            raise LoginCancelled("Хэрэглэгч зогсоолоо")
        if st2 == "app":
            return await read_version(page, cfg)
        if st2 != "login":
            raise LoginError("Нэвтрэх хуудас ачаалагдсангүй (timeout)")
    version = await read_version(page, cfg)

    timeout_s = (timeout_ms or t.get("manual_login_timeout_ms", 300000)) / 1000
    try:
        await page.bring_to_front()          # хэрэглэгч өөр цонхонд байсан ч хөтөч урд гарна
    except Exception:
        pass
    if on_manual_wait:
        on_manual_wait(timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise LoginCancelled("Хэрэглэгч зогсоолоо")
        st = await snapshot(page, cfg)
        if st and st.appReady and not is_login_url(st.url, cfg):
            return version or await read_version(page, cfg)
        await asyncio.sleep(0.3)
    raise LoginTimeout(f"Гар нэвтрэлтийг хүлээх хугацаа ({int(timeout_s)} сек) дууслаа")
