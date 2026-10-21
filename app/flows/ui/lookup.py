"""Lookup ("...") цонхноос мөр хайж сонгох, validation мессежийн талбар."""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from app.flows.context import StepFailed
from app.flows.ui._js import MARK_LOOKUP_ROW_JS, _COMMON_JS
from app.flows.ui.base import buttons, fcfg, sel_payload
from app.flows.ui.wait import popup_problems, settle
from app.flows.ui.page import click_button, close_form, dialog_count

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine


def validation_targets(message: str, titles: list[str]) -> list[str]:
    """«Нийт дүн оруулна уу!», «Төрөл сонгоно уу» → ["Нийт дүн"], ["Төрөл"] (анхааруулгын гарчгийг хасна)."""
    out = []
    for line in (message or "").splitlines():
        line = line.strip()
        for t in sorted(titles, key=len, reverse=True):     # «АнхаарДанс оруулна уу» — гарчиг наалдсан
            if line.lower().startswith(t.lower()):
                line = line[len(t):].strip(" :!- ")
                break
        if not line:
            continue
        # «Бүлэг сонгоогүй байна», «Улс сонгогдоогүй байна!», «… хүний тоо 0 байж болохгүй!» (HR, 2026-10-14)
        m = re.match(r"^(.{2,60}?)\s+(?:0\s+)?(оруулна|сонгоно|сонгоогүй|сонгогдоогүй|татна|бөглөнө|хоосон байх|"
                     r"хоосон байна|заавал|байж болохгүй)", line, flags=re.I)
        if m:
            out.append(m.group(1).strip(" :*-!"))
    return out


async def _lookup_search(env: "FlowEngine", text: str) -> bool:
    """Lookup цонхны (дээд давхарга) хайлтын талбарт текст бичиж Enter — хайлтгүй бол False."""
    js = r"""([sel]) => { /*__COMMON__*/
      document.querySelectorAll('[data-ssg-lksearch]').forEach(e => e.removeAttribute('data-ssg-lksearch'));
      const root = __scope(sel, 'dialog'); if (!root) return false;
      const i = Array.from(root.querySelectorAll(sel.grid_search)).find(__vis);
      if (!i) return false; i.setAttribute('data-ssg-lksearch', '1'); return true; }""".replace("/*__COMMON__*/", _COMMON_JS)
    if not await env.page.evaluate(js, [sel_payload(env)]):
        return True
    loc = env.page.locator("[data-ssg-lksearch='1']")
    try:
        await loc.fill(text)
        await loc.press("Enter")
    except Exception:
        return True
    await asyncio.sleep(0.3)
    await settle(env, 10)
    return True


_LOOKUP_ROWS_JS = r"""([sel]) => { /*__COMMON__*/
  const root = __scope(sel, 'dialog'); if (!root) return [];
  return Array.from(root.querySelectorAll('tr[data-visible-index], ' + sel.grid_row)).filter(__vis)
    .filter(r => !r.closest('dxbl-tree-list, thead, tfoot'))
    .map(r => __txt(r).replace(/\s+/g, ' ').trim()).filter(t => t && !/бичлэг олдсонгүй/i.test(t))
    .filter((t, i, a) => a.indexOf(t) === i).slice(0, 15).map(t => t.slice(0, 60)); }""".replace("/*__COMMON__*/", _COMMON_JS)


async def _visible_lookup_rows(env: "FlowEngine", clear_search: bool) -> list[str]:
    try:
        if clear_search:
            await _lookup_search(env, "")
        return await env.page.evaluate(_LOOKUP_ROWS_JS, [sel_payload(env)])
    except Exception:
        return []


async def pick_from_lookup(env: "FlowEngine", what: str = "", pick: bool = True, want: str = "") -> str:
    """`[data-ssg-lookup='1']` ("...") товчоор lookup цонх нээж, өгөгдлийн grid-ийн фокуслагдсан (эсвэл эхний)
    мөрийг сонгоно: нүд дээр нэг дарж → «Сонгох». Сонгосон мөрийн текст, чадахгүй бол "" (цонхыг хаана).
    Давхар дарахыг зөвхөн «Сонгох» товчгүй үед хэрэглэнэ (SmartApp-д мөрийн сум нь бичлэг НЭЭДЭГ)."""
    page = env.page
    seq = getattr(env, "_lk_seq", 0) + 1
    env._lk_seq = seq
    trace: list[str] = []

    def log(msg: str) -> None:
        trace.append(msg)

    async def shot(stage: str) -> None:
        try:
            flow = re.sub(r"[^\w-]+", "_", getattr(env, "flow_name", ""), flags=re.U).strip("_")[:50]
            p = env.run_dir / "shots" / f"lookup-{flow}-{seq:02d}-{stage}.png"
            await page.screenshot(path=str(p), timeout=5000)
        except Exception:
            pass

    try:
        before = await dialog_count(env)
        try:
            await page.click("[data-ssg-lookup='1']", timeout=5000)
        except Exception as e:
            # Grid-ийн баруун захын жижиг «...» (ж: журналын «Мөнгөн гүйлгээний үзүүлэлт» багана, 2026-10-21) —
            # Playwright «дарагдахгүй» гэж үзсэн. DOM click() хангалтгүй (DevExpress pointerdown/mousedown-оор ажилладаг —
            # 2026-10-21 цонх нээгдээгүй) → харагдах хэсэгт гүйлгээд товчны төв рүү ЖИНХЭНЭ хулганаар дарна
            log(f"энгийн дарах амжилтгүй ({type(e).__name__}) — хулганаар координатаар дарав")
            box = await page.evaluate("() => { const b = document.querySelector('[data-ssg-lookup=\"1\"]'); if (!b) return null;"
                                      " b.scrollIntoView({block: 'center', inline: 'center'}); const r = b.getBoundingClientRect();"
                                      " return r.width > 0 ? {x: r.left + r.width / 2, y: r.top + r.height / 2} : null; }")
            if not box:
                raise
            await page.mouse.click(box["x"], box["y"])
            for _ in range(10):
                if await dialog_count(env) > before:
                    break
                await asyncio.sleep(0.2)
            else:                                   # сүүлийн арга — DOM-ийн click
                log("хулганаар нээгдээгүй — DOM-оор дарав")
                await page.evaluate("() => { const b = document.querySelector('[data-ssg-lookup=\"1\"]'); if (b) b.click(); }")
        for _ in range(25):
            if await dialog_count(env) > before:
                break
            await asyncio.sleep(0.2)
        else:
            log(f"цонх нээгдсэнгүй (dialogs={before})")
            await shot("no-dialog")
            return ""
        log(f"цонх нээгдлээ (dialogs {before}→{await dialog_count(env)})")
        row = None
        searched = False
        empty_since = None      # «Бичлэг олдсонгүй» анх харагдсан цаг
        deadline = time.monotonic() + fcfg(env).get("lookup_timeout_ms", 12000) / 1000
        while time.monotonic() < deadline:
            row = await page.evaluate(MARK_LOOKUP_ROW_JS, [sel_payload(env), want])
            if want and not row and not searched:
                # цонхны хайлтаар шүүгээд дахин үзнэ (нөхцөлт сонголт — эхний эерэг үгээр; эерэг үггүй бол хайхгүй)
                text = want if not ("&" in want or want.startswith("!")) else next(
                    (t.strip() for t in want.split("&") if t.strip() and not t.strip().startswith("!")), "")
                searched = await _lookup_search(env, text) if text else True
                continue
            if row:
                break
            # Lookup-ийн өгөгдөл ачаалахад системийн алдаа (ж: Invalid column name 'ActiveStatus') — аппын bug:
            # «утга олдсонгүй» гэж андуурахгүй, алдааг шууд мэдээлнэ
            errors, _ = await popup_problems(env)
            if errors:
                log(f"системийн алдаа: {errors[0].text[:150]}")
                await shot("error")
                await close_form(env, keep=before)
                raise StepFailed(f"{what or 'Сонголтын'} (lookup) цонх нээхэд: {errors[0].text}", errors)
            empty_since = (empty_since or time.monotonic()) if row == "" else None
            if row == "" and time.monotonic() - empty_since >= fcfg(env).get("empty_stable_ms", 1500) / 1000:
                break
            await asyncio.sleep(0.3)
        await shot("open")
        if not row:                               # хоосон lookup эсвэл ачаалагдсангүй
            errors, _ = await popup_problems(env)
            if errors:
                log(f"системийн алдаа: {errors[0].text[:150]}")
                await close_form(env, keep=before)
                raise StepFailed(f"{what or 'Сонголтын'} (lookup) цонх нээхэд: {errors[0].text}", errors)
            log(f"мөр олдсонгүй ({'хоосон' if row == '' else 'ачаалагдаагүй/таниагүй'})")
            if want:
                # Калибровк: хайлтыг цэвэрлээд lookup-д БОДИТ байгаа мөрүүдийг тайланд (ж: кассын орлогын «Данс»-д
                # авлагын данс огт гардаггүй — 2026-10-19)
                rows = await _visible_lookup_rows(env, searched)
                if rows and hasattr(env, "note"):
                    env.note(f"{what or 'Сонголт'}: «{want}» алга — lookup-ийн мөрүүд: {' | '.join(rows)}")
            await close_form(env, keep=before)
            return ""
        cell = await page.evaluate("() => { const c = document.querySelector('[data-ssg-lkcell=\"1\"]');"
                                   " return c ? (c.innerText || '').trim().slice(0, 60) : null; }")
        log(f"мөр: {row[:80]} | нүд: {cell}")
        if not pick:                              # форм шалгалт: ачаалагдсаныг харсан — сонгохгүй хаана
            await close_form(env, keep=before)
            log("сонгохгүй хаалаа")
            return row
        try:
            await page.click("[data-ssg-lkcell='1']", timeout=3000)       # мөрийг фокуслах
        except Exception as e:
            log(f"нүд дарж чадсангүй: {str(e)[:120]}")
        await asyncio.sleep(0.4)
        if await dialog_count(env) > before:      # мөр дарахад шууд сонгогдоогүй → «Сонгох»
            try:
                got = await click_button(env, buttons(env, "select") or ["Сонгох", "OK", "Select"],
                                         scope="dialog", wait_s=1.5)
                log(f"«{got}» дарлаа")
            except StepFailed:
                log("«Сонгох» товч олдсонгүй → давхар дарав")
                try:
                    await page.dblclick("[data-ssg-lkcell='1']", timeout=3000)
                except Exception:
                    pass
            for _ in range(15):
                if await dialog_count(env) <= before:
                    break
                await asyncio.sleep(0.2)
        else:
            log("мөр дарахад цонх шууд хаагдлаа")
        if await dialog_count(env) > before:
            log(f"сонгосны дараа цонх хаагдсангүй (dialogs={await dialog_count(env)})")
            await shot("not-closed")
            await close_form(env, keep=before)
            return ""
        log("сонгогдлоо")
        return row
    finally:
        env.runlog.log(f"[{getattr(env, 'flow_name', '')}] lookup #{seq}: " + " → ".join(trace), "info",
                       to_ui=False)
