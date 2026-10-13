"""Chromium эхлүүлэх, context үүсгэх (smoke болон flow хоёулаа ашиглана)."""
from __future__ import annotations

import os
from typing import Any

from app.runner.detectors import init_script


async def launch(pw, headless: bool):
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        # headed цонх OS-ийн фокусоос гарахад Chrome сайтын JS timer-үүдийг
        # хааж (throttle) эхэлдэг тул "Loading…" хиймлээр гацаж, item бүр
        # timeout хүрдэг байсан — эдгээр флаг үүнийг хаана.
        "args": ["--disable-background-timer-throttling",
                 "--disable-backgrounding-occluded-windows",
                 "--disable-renderer-backgrounding"],
    }
    chromium_path = os.getenv("SSG_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path
    return await pw.chromium.launch(**launch_kwargs)


async def new_context(browser, cfg: dict, storage_state: dict | None = None):
    b = cfg["browser"]
    ctx = await browser.new_context(viewport=b["viewport"], locale=b["locale"],
                                    timezone_id=b["timezone"], storage_state=storage_state)
    await ctx.add_init_script(init_script(cfg))
    return ctx


# ---- Өөрийн Chrome-д холбогдох (CDP) -------------------------------------------------------------
# Cloudflare Turnstile мэт captcha нь автоматжуулалтын хэрэгслээр нээсэн хөтчийг таньж нэвтрэлтийг
# хориглодог. Хэрэгсэл captcha-г тойрч/хуурахгүй: хэрэглэгч ЖИНХЭНЭ Chrome-оо (тусдаа профайл) нээж
# captcha-г өөрөө давж нэвтэрнэ, дараа нь хэрэгсэл тэр Chrome-д (remote debugging, зөвхөн 127.0.0.1) холбогдож
# шинэ таб дээр тестээ хийнэ. Дуусахад зөвхөн өөрийн табаа хаана — Chrome хэвээр нээлттэй.
import json as _json
import subprocess as _sp
import urllib.request as _ur
from pathlib import Path as _Path

DEFAULT_CDP = "http://127.0.0.1:9222"
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def cdp_url(cfg: dict) -> str:
    return os.getenv("SSG_CDP_URL") or (cfg.get("browser") or {}).get("cdp_url") or DEFAULT_CDP


def cdp_alive(url: str) -> dict | None:
    """Chrome remote debugging хариулж байвал хувилбарын мэдээлэл, үгүй бол None."""
    try:
        with _ur.urlopen(url.rstrip("/") + "/json/version", timeout=2) as r:
            return _json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def find_chrome() -> str | None:
    p = os.getenv("SSG_CHROME_PATH")
    if p and _Path(p).exists():
        return p
    return next((c for c in CHROME_PATHS if c and _Path(c).exists()), None)


def start_chrome(open_url: str, port: int = 9222) -> str:
    """Тусдаа профайлтай Chrome-ыг remote debugging-тэй нээнэ (хэрэглэгчийн хувийн профайлд хүрэхгүй)."""
    exe = find_chrome()
    if not exe:
        raise FileNotFoundError("Google Chrome олдсонгүй — суулгах эсвэл SSG_CHROME_PATH-аар замыг заана уу")
    profile = _Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "SSGAutoTester" / "chrome-profile" \
        if os.name == "nt" else _Path.home() / ".ssg-auto-tester" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    _sp.Popen([exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
               f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", open_url],
              stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, close_fds=True)
    return str(profile)


async def attach(pw, url: str, cfg: dict):
    """Нээлттэй Chrome-д холбогдож (browser, context, шинэ page) буцаана."""
    browser = await pw.chromium.connect_over_cdp(url)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    await ctx.add_init_script(init_script(cfg))
    page = await ctx.new_page()
    return browser, ctx, page
