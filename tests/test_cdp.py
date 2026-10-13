"""Өөрийн Chrome-д холбогдох (CDP) горим — Cloudflare/captcha-тай сайтад хэрэглэгч жинхэнэ Chrome дээр
өөрөө нэвтэрч, хэрэгсэл тэр хөтөчид холбогдоно. Тест: тусдаа Chromium-ыг remote debugging-тэй асааж
pipeline-ийг түүнд холбоно; дуусахад хөтөч ХААГДАХГҮЙ (зөвхөн хэрэгслийн таб хаагдана)."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import tempfile
import time

import pytest

from app import models as M
from app.runner import browser as browser_mod
from tests.fixtures.mock_crud.server import CrudServer
from tests.test_autocrud import CUSTOMERS, e2e, make_pipe


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def chromium_cdp():
    from playwright.sync_api import sync_playwright
    exe = os.getenv("SSG_CHROMIUM_PATH")
    if not exe:
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
    port = _free_port()
    prof = tempfile.mkdtemp(prefix="ssg-cdp-")
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
                             "--no-first-run", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        if browser_mod.cdp_alive(url):
            break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("Chromium remote debugging эхэлсэнгүй")
    yield url
    proc.kill()


def test_cdp_alive_false_when_nothing_listens():
    assert browser_mod.cdp_alive(f"http://127.0.0.1:{_free_port()}") is None


@e2e
def test_pipeline_attaches_to_own_chrome(chromium_cdp, tmp_path):
    with CrudServer() as srv:
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [CUSTOMERS]}], [])
        pipe.cdp = chromium_cdp
        d = asyncio.run(pipe.run())
        assert d.login_ok and d.flows and d.flows[0]["status"] == M.PASS, d.pipeline
    # Хөтөч нээлттэй хэвээр (хэрэглэгчийн Chrome-ыг хаахгүй)
    assert browser_mod.cdp_alive(chromium_cdp)
