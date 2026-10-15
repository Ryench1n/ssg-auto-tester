"""Баримтын мөрийн (inline edit) grid: нүд идэвхжүүлэх, бөглөх."""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from app.flows.ui._js import EDIT_ROW_JS, MARK_CELL_LOOKUP_JS, MARK_LINES_ADD_JS
from app.flows.ui.base import sel_payload
from app.flows.ui.page import close_form, dialog_count
from app.flows.ui.lookup import pick_from_lookup

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine


async def _edit_row(env: "FlowEngine", wait_s: float = 5.0) -> list[dict] | None:
    deadline = time.monotonic() + wait_s
    while True:
        cells = await env.page.evaluate(EDIT_ROW_JS, [sel_payload(env), getattr(env, "_line_hint", "")])
        if cells or time.monotonic() >= deadline:
            return cells
        await asyncio.sleep(0.25)


# Нүдэнд editor (input) идэвхтэй эсэх: input харагдаж байгаа ЭСВЭЛ фокус тэр нүдэнд байгаа
CELL_EDITOR_JS = r"""
(i) => {
  const td = document.querySelector(`[data-ssg-lrow="1"] [data-ssg-lcell="${i}"]`);
  if (!td) return false;
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const inp = Array.from(td.querySelectorAll('input')).find(vis);
  return !!inp || (document.activeElement && td.contains(document.activeElement) && document.activeElement.tagName === 'INPUT');
}
"""


async def _cell_has_editor(env: "FlowEngine", i: int) -> bool:
    await _edit_row(env, 1)
    try:
        return bool(await env.page.evaluate(CELL_EDITOR_JS, i))
    except Exception:
        return False


async def _activate_cell(env: "FlowEngine", i: int) -> bool:
    """Мөрийн i-р нүдийг засварын горимд оруулна. DevExpress inline (EditCell) grid-д:
    1) нүд дээр дарах  2) давхар дарах  3) идэвхтэй editor-оос Tab-аар тэр нүд хүртэл шилжих."""
    await _edit_row(env, 2)
    sel = f"[data-ssg-lrow='1'] [data-ssg-lcell='{i}']"
    if await _cell_has_editor(env, i):
        return True                               # editor аль хэдийн нээлттэй (дарвал "..." товч дээр буух эрсдэлтэй)
    for attempt in ("click", "dblclick"):
        try:
            await _edit_row(env, 1)
            await (env.page.click if attempt == "click" else env.page.dblclick)(sel, timeout=3000)
        except Exception:
            continue
        for _ in range(8):
            if await _cell_has_editor(env, i):
                return True
            await asyncio.sleep(0.2)
    # Tab навигаци: мөрийн аль нэг editor-т фокуслаад Tab-аар урагшилна (хамгийн ихдээ 8 алхам)
    try:
        await env.page.evaluate("() => { const r = document.querySelector('[data-ssg-lrow=\"1\"]');"
                                " const inp = r && Array.from(r.querySelectorAll('input')).find(x => x.offsetWidth > 0);"
                                " if (inp) inp.focus(); }")
        for _ in range(8):
            await env.page.keyboard.press("Tab")
            await asyncio.sleep(0.35)
            if await _cell_has_editor(env, i):
                return True
            # Фокус засварлаж буй мөрөөс гарвал (дараагийн/шинэ мөр рүү) зогсоно — хоосон мөр үүсгэхгүй
            if not await env.page.evaluate("() => { const r = document.querySelector('[data-ssg-lrow=\"1\"]');"
                                           " return !!(r && document.activeElement && r.contains(document.activeElement)); }"):
                break
    except Exception:
        pass
    return False


async def _dump_line_row(env: "FlowEngine", name: str) -> None:
    """Мөрийн нүд засварлаж чадаагүй үед оношлогоо: засварлаж буй мөр/grid-ийн HTML + screenshot."""
    try:
        html = await env.page.evaluate("() => { const r = document.querySelector('[data-ssg-lrow=\"1\"]') ||"
                                       " document.querySelector('[data-ssg-linegrid=\"1\"] tbody');"
                                       " return r ? r.outerHTML : 'NO ROW'; }")
        flow = re.sub(r"[^\w-]+", "_", getattr(env, "flow_name", ""), flags=re.U).strip("_")[:40]
        base = env.run_dir / "shots" / f"lines-{flow}-{name}"
        base.with_suffix(".html").write_text(html[:200_000], encoding="utf-8")
        await env.page.screenshot(path=str(base.with_suffix(".png")), timeout=5000)
    except Exception:
        pass


async def _fill_cell(env: "FlowEngine", i: int, value: str) -> bool:
    if not await _activate_cell(env, i):
        await _dump_line_row(env, f"cell{i}")
        return False
    loc = env.page.locator(f"[data-ssg-lrow='1'] [data-ssg-lcell='{i}'] input").first
    # Tab ДАРАХГҮЙ: SmartApp-д сүүлийн засварлах нүднээс Tab дарахад шинэ (хоосон) мөр үүсдэг →
    # «Данс сонгогдоогүй байна». Утгыг blur-аар (change) баталгаажуулна.
    if not await loc.count():                     # input-гүй ч фокус нүдэнд — идэвхтэй элемент рүү бичнэ
        await env.page.keyboard.press("Control+A")
        await env.page.keyboard.type(str(value))
        await env.page.evaluate("() => document.activeElement && document.activeElement.blur()")
    else:
        await loc.fill(str(value))
        await loc.blur()
    await asyncio.sleep(0.5)
    return True


DEBIT_WORDS = ["дебет", "debit"]
CREDIT_WORDS = ["кредит", "credit"]
QTY_WORDS = ["тоолсон", "тоо ширхэг", "тоо", "хэмжээ", "дүн", "үнэ", "qty", "quantity", "amount"]
COMPUTED_WORDS = ["үлдэгдэл", "зөрүү", "нийт", "balance", "total"]


async def fill_lines(env: "FlowEngine", amount: str = "1") -> dict | None:
    """Формын мөрийн grid-д мөр нэмж бөглөнө. Grid байхгүй бол None.

    - Эхний lookup багана (Дансны код / Барааны код) — lookup-оос эхний утгыг сонгоно.
    - Дебет + Кредит баганатай (журнал) → 2 мөр: дебет=amount, кредит=amount (тэнцвэртэй).
    - Эс бөгөөс тоо хэмжээ/дүнгийн баганад amount (тооцоолдог үлдэгдэл/зөрүү баганыг хөндөхгүй).
    Буцаах: {"lines": [...тайлбар], "error": ""}"""
    info = await env.page.evaluate(MARK_LINES_ADD_JS, [sel_payload(env)])
    if not info:
        return None
    headers = [h.lower() for h in info["headers"]]

    def col(words: list[str], allowed: set[int] | None = None) -> int | None:
        for i, h in enumerate(headers):
            if (allowed is None or i in allowed) and any(w in h for w in words) \
                    and not any(c in h for c in COMPUTED_WORDS):
                return i
        return None

    dcol, ccol = col(DEBIT_WORDS), col(CREDIT_WORDS)
    targets = [dcol, ccol] if dcol is not None and ccol is not None else [None]
    lines: list[str] = []
    for target in targets:
        if not await env.page.evaluate(MARK_LINES_ADD_JS, [sel_payload(env)]):
            return {"lines": lines, "error": "мөр нэмэх товч дахин олдсонгүй"}
        env._line_hint = ""
        await env.page.click("[data-ssg-lineadd='1']", timeout=5000)
        await asyncio.sleep(0.5)
        cells = await _edit_row(env)
        if not cells:
            return {"lines": lines, "error": "«+» дарахад засварлах мөр гарсангүй"}
        name = lambda i: (info["headers"][i] if i < len(info["headers"]) else f"#{i}") or f"#{i}"
        part = []
        lk = next((c for c in cells if c["lookup"] and not c["command"]), None)
        if lk is not None:
            await _activate_cell(env, lk["i"])
            if not await env.page.evaluate(MARK_CELL_LOOKUP_JS, lk["i"]):
                return {"lines": lines, "error": f"'{name(lk['i'])}' нүдний «...» товч олдсонгүй"}
            picked = await pick_from_lookup(env, f"Мөрийн '{name(lk['i'])}' баганын сонголтын")
            if not picked:
                return {"lines": lines, "error": f"'{name(lk['i'])}' мөрийн lookup-оос сонгож чадсангүй"}
            part.append(f"{name(lk['i'])}={picked[:40]}")
            # Сонгосны дараа мөр commit болж болно — шинэ мөрийг сонгосон кодоор (эхний үг) дахин олно
            env._line_hint = picked.split()[0] if picked.split() else ""
            await asyncio.sleep(0.4)
            cells = await _edit_row(env) or cells
        # Lookup (код) багана тоон утгатай (1001) ч тоо хэмжээ биш — бичвэл хайлтын цонх нээгдэнэ
        numeric = {c["i"] for c in cells if c["numeric"] and not c["command"] and not c["lookup"]
                   and (lk is None or c["i"] != lk["i"])}
        num = target if target is not None else (col(QTY_WORDS, numeric)
                                                 if col(QTY_WORDS, numeric) is not None
                                                 else next((i for i in sorted(numeric)
                                                            if not any(w in (headers[i] if i < len(headers) else "")
                                                                       for w in COMPUTED_WORDS)), None))
        if num is not None:
            level = await dialog_count(env)
            if not await _fill_cell(env, num, amount):
                return {"lines": lines, "error": f"'{name(num)}' нүдэнд утга оруулж чадсангүй"}
            if await dialog_count(env) > level:        # бичихэд сонголтын цонх нээгдлээ — тоон багана биш
                await close_form(env, keep=level)
                return {"lines": lines, "error": f"'{name(num)}' нүдэнд бичихэд сонголтын цонх нээгдлээ"}
            part.append(f"{name(num)}={amount}")
        lines.append(", ".join(part))
    return {"lines": lines, "error": ""}


# ComboBox-ийн dropdown (▾) товч — `[data-ssg-field]` input-ийн combobox эх элемент доторх товч
