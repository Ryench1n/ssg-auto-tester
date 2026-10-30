"""SCAN_FORM_JS бодит Chromium дээр: шошго нь input-ээс хол (шинэ wrapper) байсан ч байрлалаар танина."""
from __future__ import annotations

import os

import pytest

from app.config import load_config
from app.flows.ui._js import SCAN_FORM_JS

# testretail 2026-10-30: Код талбар хайлтын товчтой «slrow» wrapper дотор — шошго 4 өвгөөс хол
HTML = """
<div class="dxbl-modal" role="dialog" style="width:500px">
  <table><tr>
    <td style="width:150px"><div><span>Код *</span></div></td>
    <td><div class="slrow"><div><div><div class="dxbl-text-edit"><input type="text" style="width:120px"></div></div>
        <button>&#128269;</button></div></div></td>
  </tr><tr>
    <td><span>Нэр *</span></td>
    <td><div class="dxbl-text-edit"><input type="text" style="width:280px"></div></td>
  </tr></table>
</div>
"""


def test_scan_form_finds_label_by_row_position():
    pw_api = pytest.importorskip("playwright.sync_api")
    cfg = load_config("https://testretail.smartapp.mn")
    sel = {**((cfg.get("flow") or {}).get("selectors") or {}), "dialog": cfg["selectors"]["dialog"]}
    with pw_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=os.getenv("SSG_CHROMIUM_PATH") or pw.chromium.executable_path,
                                         args=["--headless=new"])
        except Exception as e:                      # хөтөч суугаагүй орчин
            pytest.skip(f"Chromium алга: {e}")
        page = browser.new_page()
        page.set_content(HTML)
        fields = page.evaluate(SCAN_FORM_JS, [sel])
        browser.close()
    by = {f["label"]: f for f in fields}
    assert "Код" in by and by["Код"]["required"], fields
    assert "Нэр" in by and by["Нэр"]["required"]
