"""Автомат CRUD-д зориулсан бодит сайтын DOM судалгаа (ХАДГАЛАХГҮЙ).

  python -m app.flows.explore --url https://testaccounting.smartapp.mn --menu "Ерөнхий дэвтэр||Данс|accountlist" [--menu ...]

Хөтөч харагдах горимд нээгдэж, хэрэглэгч гараар нэвтэрнэ. Цэс бүрт:
  жагсаалт → «Нэмэх/Шинэ» → формын HTML → lookup ("...") бүрийг нээж HTML → мөрийн grid-д мөр нэмэх → HTML
бүгдийг `runs/<id>/explore/<цэс>/`-д (html + png + fields.json) хадгалаад, формыг ХАДГАЛАХГҮЙ хаана.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path

from app.config import RUNS_DIR, load_config
from app.flows import ui
from app.flows.engine import step
from app.pipeline.runner import PipelineRunner
from app.pipeline.stages import PipelineConfigError
from app.runner.smoke import RunOptions

DUMP_JS = r"""
([sel, mode]) => {
  /*__COMMON__*/
  const src = mode === 'top' ? (__scope(sel, 'dialog') || document.body) : document.body;
  const c = src.cloneNode(true);
  // input-ийн одоогийн утга, checked-ийг attribute болгон хадгална (clone-д value алга болдог)
  const a = Array.from(src.querySelectorAll('input, textarea, select'));
  const b = Array.from(c.querySelectorAll('input, textarea, select'));
  a.forEach((el, i) => { if (b[i]) { b[i].setAttribute('data-ssg-value', el.value == null ? '' : String(el.value));
                                     if (el.checked) b[i].setAttribute('data-ssg-checked', '1'); } });
  c.querySelectorAll('script, style, svg').forEach(x => x.remove());
  return c.outerHTML;
}
""".replace("/*__COMMON__*/", ui._COMMON_JS)

# Формын дотор байгаа grid-ийн "мөр нэмэх" товч (+ / Шинэ / Мөр нэмэх) — формын толгойн ШИНЭ биш
MARK_GRID_ADD_JS = r"""
([sel, texts]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-gridadd]').forEach(e => e.removeAttribute('data-ssg-gridadd'));
  const root = __scope(sel, 'dialog') || document;
  const want = texts.map(__norm);
  const grids = Array.from(root.querySelectorAll('.dxbl-grid, [role=grid], table')).filter(__vis);
  for (const g of grids) {
    const box = g.closest('.dxbl-grid') || g;
    const btn = Array.from(box.querySelectorAll('button, [role=button], .dxbl-btn, a, span, div')).filter(__vis)
      .find(x => x.children.length <= 2 && want.includes(__norm(__txt(x) || x.getAttribute('title') || x.getAttribute('aria-label'))));
    if (btn) { btn.setAttribute('data-ssg-gridadd', '1'); return __txt(btn) || btn.getAttribute('title') || '+'; }
  }
  return null;
}
""".replace("/*__COMMON__*/", ui._COMMON_JS)


def _slug(s: str) -> str:
    return re.sub(r"[^\w-]+", "_", s, flags=re.U).strip("_")[:60] or "x"


@step("explore.form")
async def explore_form(env, args: dict) -> dict:
    menu = args["menu"]
    out = Path(env.run_dir) / "explore" / _slug(menu.split("|")[2] if menu.count("|") == 3 else menu)
    out.mkdir(parents=True, exist_ok=True)
    seq = [0]
    notes: dict = {"menu": menu, "steps": []}

    async def dump(name: str, mode: str = "top") -> None:
        seq[0] += 1
        base = out / f"{seq[0]:02d}-{_slug(name)}"
        try:
            html = await env.page.evaluate(DUMP_JS, [ui.sel_payload(env), mode])
            base.with_suffix(".html").write_text(html[:800_000], encoding="utf-8")
        except Exception as e:
            notes["steps"].append(f"{name}: dump алдаа {e!r}")
        try:
            await env.page.screenshot(path=str(base.with_suffix(".png")), timeout=8000)
        except Exception:
            pass
        notes["steps"].append(name)

    try:
        await ui.ensure_menu(env, menu)
        await asyncio.sleep(1.5)
        await dump("list", "page")
        before = await ui.dialog_count(env)
        tabs_before = await ui.tab_names(env)
        await ui.click_button(env, ui.buttons(env, "add"), scope="page")
        for _ in range(40):
            if await ui.dialog_count(env) > before or [t for t in await ui.tab_names(env) if t not in tabs_before]:
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(2.0)
        await ui.settle(env, 10)
        await dump("form")
        fields = await ui.scan_form(env, 5)
        notes["fields"] = fields
        notes["probe"] = await ui.probe(env)
        form_level = await ui.dialog_count(env)
        for f in fields:
            if not f.get("lookup") or f.get("readonly"):
                continue
            try:
                await ui.find_field(env, f["label"])
                await env.page.click("[data-ssg-lookup='1']", timeout=5000)
                for _ in range(30):
                    if await ui.dialog_count(env) > form_level:
                        break
                    await asyncio.sleep(0.2)
                await asyncio.sleep(4.0)                 # lookup-ийн өгөгдөл ачаалагдах
                await dump(f"lookup-{f['label']}")
            except Exception as e:
                notes["steps"].append(f"lookup {f['label']}: {e!r}")
            await ui.close_form(env, keep=form_level)
        # Баримтын мөрийн grid: мөр нэмэх товч
        try:
            added = await env.page.evaluate(MARK_GRID_ADD_JS, [ui.sel_payload(env), ["+", "Шинэ", "Мөр нэмэх", "Нэмэх", "New", "Add"]])
            notes["grid_add"] = added
            if added:
                await env.page.click("[data-ssg-gridadd='1']", timeout=5000)
                await asyncio.sleep(2.5)
                await dump("lines-after-add")
                await ui.close_form(env, keep=form_level)
        except Exception as e:
            notes["steps"].append(f"grid add: {e!r}")
        await ui.close_form(env, keep=before)
        extra = [t for t in await ui.tab_names(env) if t not in tabs_before]
        if extra:
            await ui.close_tabs(env, extra)
    finally:
        (out / "fields.json").write_text(json.dumps(notes, ensure_ascii=False, indent=2, default=str),
                                         encoding="utf-8")
    env.note(f"Судалгаа: {out}")
    return {}


ACTIVE_JS = r"""
() => { const a = document.activeElement; if (!a) return null;
  const td = a.closest('td'), tr = a.closest('tr');
  return {tag: a.tagName, cls: (typeof a.className === 'string' ? a.className : '').slice(0, 80),
          td: td ? td.cellIndex : null, tr_index: tr ? tr.getAttribute('data-visible-index') : null,
          tr_cls: tr ? (tr.className || '').slice(0, 120) : null, value: a.value == null ? null : String(a.value)}; }
"""
GRID_HTML_JS = r"""() => { const g = document.querySelector('[data-ssg-linegrid="1"]'); return g ? g.outerHTML : 'NO GRID'; }"""


@step("explore.lines")
async def explore_lines(env, args: dict) -> dict:
    """Баримтын мөрийн (inline) grid-ийн засварын зан төлөвийг алхам бүрээр бичнэ — ХАДГАЛАХГҮЙ:
    «+» → кодын lookup-оос сонгох → тоон нүд дээр дарах → давхар дарах → Tab ×3."""
    menu = args["menu"]
    out = Path(env.run_dir) / "explore" / ("lines-" + _slug(menu.split("|")[2] if menu.count("|") == 3 else menu))
    out.mkdir(parents=True, exist_ok=True)
    log: list = []
    seq = [0]

    async def dump(stage: str) -> None:
        seq[0] += 1
        base = out / f"{seq[0]:02d}-{_slug(stage)}"
        try:
            base.with_suffix(".html").write_text((await env.page.evaluate(GRID_HTML_JS))[:400_000], encoding="utf-8")
            await env.page.screenshot(path=str(base.with_suffix(".png")), timeout=8000)
        except Exception as e:
            log.append(f"{stage}: dump алдаа {e!r}")
        try:
            log.append({"stage": stage, "active": await env.page.evaluate(ACTIVE_JS),
                        "edit_row": await env.page.evaluate(ui.EDIT_ROW_JS, [ui.sel_payload(env), ""])})
        except Exception as e:
            log.append(f"{stage}: {e!r}")

    before = 0
    try:
        await ui.ensure_menu(env, menu)
        before = await ui.dialog_count(env)
        await ui.click_button(env, ui.buttons(env, "add"), scope="page")
        await asyncio.sleep(3)
        await ui.settle(env, 10)
        info = await env.page.evaluate(ui.MARK_LINES_ADD_JS, [ui.sel_payload(env)])
        log.append({"lines_grid": info})
        if not info:
            return {}
        headers = [h.lower() for h in info["headers"]]
        num = next((i for i, h in enumerate(headers) if "дебет" in h or "тоолсон" in h), None)
        await env.page.click("[data-ssg-lineadd='1']", timeout=5000)
        await asyncio.sleep(1.5)
        await dump("after-add")
        cells = await ui._edit_row(env) or []
        lk = next((c for c in cells if c["lookup"] and not c["command"]), None)
        if lk:
            await ui._activate_cell(env, lk["i"])
            if await env.page.evaluate(ui.MARK_CELL_LOOKUP_JS, lk["i"]):
                log.append({"picked": await ui.pick_from_lookup(env)})
            await asyncio.sleep(1.5)
            await dump("after-pick")
        if num is not None:
            await ui._edit_row(env, 1)
            try:
                await env.page.click(f"[data-ssg-lrow='1'] [data-ssg-lcell='{num}']", timeout=3000)
            except Exception as e:
                log.append(f"click num: {e!r}")
            await asyncio.sleep(1.5)
            await dump("after-click-num")
            await ui._edit_row(env, 1)
            try:
                await env.page.dblclick(f"[data-ssg-lrow='1'] [data-ssg-lcell='{num}']", timeout=3000)
            except Exception as e:
                log.append(f"dblclick num: {e!r}")
            await asyncio.sleep(1.5)
            await dump("after-dblclick-num")
        for k in range(3):
            await env.page.keyboard.press("Tab")
            await asyncio.sleep(1.0)
            await dump(f"after-tab-{k + 1}")
    finally:
        (out / "lines.json").write_text(json.dumps(log, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        await ui.close_form(env, keep=before)
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Автомат CRUD-д зориулсан DOM судалгаа (хадгалахгүй)")
    ap.add_argument("--url", required=True)
    ap.add_argument("--menu", action="append", default=[], help="цэсний түлхүүр (module|folder|name|href) эсвэл зам")
    ap.add_argument("--lines", action="append", default=[], help="мөрийн grid-ийн засварын зан төлөвийг судлах цэс")
    ap.add_argument("--login-timeout", type=int, default=900, help="гар нэвтрэлт хүлээх сек")
    a = ap.parse_args()
    url = a.url if a.url.startswith("http") else "https://" + a.url
    flows = {f"explore_{i}": {"steps": [{"uses": "explore.form", "name": f"Судлах: {m}", "with": {"menu": m}}]}
             for i, m in enumerate(a.menu, 1)}
    flows.update({f"lines_{i}": {"steps": [{"uses": "explore.lines", "name": f"Мөр судлах: {m}", "with": {"menu": m}}]}
                  for i, m in enumerate(a.lines, 1)})
    if not flows:
        ap.error("--menu эсвэл --lines дор хаяж нэгийг өгнө үү")
    cfg = load_config(url, {"flows": flows, "timing": {"manual_login_timeout_ms": a.login_timeout * 1000},
                            "flow": {"step_timeout_s": 300}})
    run_id = "explore-" + uuid.uuid4().hex[:8]

    def emit(t: str, d: dict) -> None:
        if t == "log":
            print(d["msg"], flush=True)
        elif t == "waiting_login":
            print(f">>> НЭЭГДСЭН ХӨТӨЧ ДЭЭР НЭВТЭРНЭ ҮҮ ({d['timeout_s']} сек) <<<", flush=True)

    try:
        pipe = PipelineRunner(run_id, url, cfg, RunOptions(project="Explore"), RUNS_DIR / run_id,
                              [{"kind": "crud", "flow": f} for f in flows], emit=emit, headless=False)
    except PipelineConfigError as e:
        print(f"Тохиргооны алдаа: {e}", file=sys.stderr)
        return 2
    data = asyncio.run(pipe.run())
    print(f"\nDONE {RUNS_DIR / run_id / 'explore'}")
    for fl in data.flows:
        s = fl["steps"][0]
        print(f"  {s['status']:<8} {s['name']} {s['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
