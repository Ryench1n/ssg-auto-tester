"""Жагсаалтын grid: мөр хайх, focus шалгах, мөр сонгох/чагтлах."""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from app.flows.context import StepFailed
from app.models import Finding
from app.flows.ui._js import FIND_ROWS_JS, MARK_SEARCH_JS, _COMMON_JS
from app.flows.ui.base import sel_payload
from app.flows.ui.wait import popup_problems, raise_on_problems, settle
from app.flows.ui.page import dialog_count, tab_names

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine


# ------------------------------------------------------------------ grid
async def grid_find(env: "FlowEngine", text: str) -> list[str]:
    """Grid-ийн хайлтаар `text`-ийг хайж, таарсан мөрүүдийг буцаана."""
    if await env.page.evaluate(MARK_SEARCH_JS, [sel_payload(env)]):
        loc = env.page.locator("[data-ssg-search='1']")
        await loc.fill(text)
        await loc.press("Enter")
        await asyncio.sleep(0.2)
        await settle(env)
    await raise_on_problems(env, "Хайхад")
    return await env.page.evaluate(FIND_ROWS_JS, [sel_payload(env), text])


async def grid_find_settled(env: "FlowEngine", text: str, expect: str = "found", tries: int = 3,
                            wait_s: float = 1.5) -> list[str]:
    """grid_find-ийг хүлээгдэж буй үр дүнд (found | absent) хүрэх хүртэл хэд дахин оролдоно: хадгалсан/устгасан
    даруйд жагсаалт хараахан шинэчлэгдээгүй байж болно (HR 2026-10-14: «мөр олдсонгүй» гэсэн ч дараагийн алхам
    тэр бичлэгийг олсон). Сүүлийн оролдлогын мөрүүдийг буцаана."""
    rows: list[str] = []
    for i in range(tries):
        rows = await grid_find(env, text)
        if bool(rows) == (expect == "found"):
            return rows
        if i < tries - 1:
            await asyncio.sleep(wait_s)
    return rows


# Мөрийг сонгох аюулгүй цэг: сонголтын checkbox, эс бөгөөс бичлэг НЭЭДЭГ сум/холбоосгүй нүд
# Мөрийг ФОКУСЛАХ цэг: бичлэг НЭЭДЭГ сум/холбоосгүй текст нүд. (Checkbox нь зөвхөн сонгодог — SmartApp-ын
# «Засах/Устгах» ФОКУСЛАГДСАН мөр дээр ажилладаг тул checkbox дармагц өөр бичлэг засагдах эрсдэлтэй — 2026-09-29.)
MARK_ROW_TARGET_JS = r"""
() => {
  document.querySelectorAll('[data-ssg-rowsel]').forEach(e => e.removeAttribute('data-ssg-rowsel'));
  const row = document.querySelector("[data-ssg-row='0']");
  if (!row) return null;
  const cells = Array.from(row.querySelectorAll('td'));
  const cell = cells.find(td => (td.innerText || '').trim()
      && !td.querySelector('.sllink, a, [class*=fa-arrow], button, input, .dxbl-checkbox'));
  if (cell) { cell.setAttribute('data-ssg-rowsel', '1'); return 'cell'; }
  return null;
}
"""

# Grid-ийн фокуслагдсан мөр манай (`text` агуулсан) мөр мөн эсэх: 'ok' | 'mismatch' | 'unknown' (фокусын класс алга)
FOCUS_CHECK_JS = r"""
([sel, text]) => {
  /*__COMMON__*/
  const want = __norm(text);
  const focused = Array.from(document.querySelectorAll(sel.grid_row + ', tr[data-visible-index]')).filter(__vis)
    .filter(r => !__findDialogs(sel).some(d => d.contains(r)))
    .filter(r => /focused-row/.test(typeof r.className === 'string' ? r.className : ''));
  if (!focused.length) return 'unknown';
  return focused.some(r => __norm(__txt(r)).includes(want)) ? 'ok' : 'mismatch';
}
""".replace("/*__COMMON__*/", _COMMON_JS)

# Нээгдсэн форм (дээд цонх эсвэл хуудас) манай бичлэгийн утгыг агуулж буй эсэх
FORM_HAS_TEXT_JS = r"""
([sel, text]) => {
  /*__COMMON__*/
  const want = __norm(text);
  const root = __scope(sel, 'auto');
  // жагсаалтын хайлтын талбар (манай текст бичигдсэн) / grid-ийн input-ыг тооцохгүй — худал "таарсан" болохгүй
  const vals = Array.from(root.querySelectorAll('input, textarea')).filter(__vis).filter(i => !__skipInput(sel, i))
    .map(i => __norm(i.value || ''));
  return vals.some(v => v && (v.includes(want) || want.includes(v) && v.length >= 6));
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def focus_check(env: "FlowEngine", text: str) -> str:
    try:
        return await env.page.evaluate(FOCUS_CHECK_JS, [sel_payload(env), text])
    except Exception:
        return "unknown"


FORM_VALUES_JS = r"""
([sel]) => {
  /*__COMMON__*/
  const root = __scope(sel, 'auto');
  return Array.from(root.querySelectorAll('input, textarea')).filter(__vis).filter(i => !__skipInput(sel, i))
    .map(i => (i.value || '').trim()).filter(Boolean).slice(0, 10);
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def form_has_text(env: "FlowEngine", text: str, wait_s: float = 10.0) -> bool:
    """Засварын форм өгөгдлөө хожуу (async) ачаалдаг — утга гарч иртэл `wait_s` хүртэл шалгана."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            if await env.page.evaluate(FORM_HAS_TEXT_JS, [sel_payload(env), text]):
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.4)


async def form_values(env: "FlowEngine") -> list[str]:
    try:
        return await env.page.evaluate(FORM_VALUES_JS, [sel_payload(env)])
    except Exception:
        return []


class RowSelection:
    """select_row-ийн үр дүн: мөр дарахад бичлэг (засварын форм) нээгдсэн эсэх, гарсан системийн алдаа."""

    def __init__(self, text: str, opened: bool = False, errors: list[Finding] | None = None, level: int = 0):
        self.text = text
        self.opened = opened        # цонх (засварын форм) нээгдсэн
        self.errors = errors or []
        self.level = level          # сонгохоос өмнөх цонхны тоо
        self.new_tabs: list[str] = []   # мөр дарахад нээгдсэн таб (баримтын форм)
        self.focus = "unknown"          # фокуслагдсан мөр манайх эсэх: ok | mismatch | unknown


_SCOPE_JS = r"""
  const scopeOf = (sel, scope) => scope === 'dialog' ? __scope(sel, 'dialog')
    : scope === 'page' ? document : (__scope(sel, 'auto') || document);
  const outsideDialogs = (sel, scope, el) => scope !== 'page' || !__findDialogs(sel).some(d => d.contains(el));
"""

CLICK_ICON_JS = r"""
([sel, icon, scope]) => {
  /*__COMMON__*/
  /*__SCOPE__*/
  const root = scopeOf(sel, scope); if (!root) return false;
  const b = Array.from(root.querySelectorAll('button, [role=button], .dxbl-btn, a')).filter(__vis)
    .filter(x => outsideDialogs(sel, scope, x))
    .filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true')
    .find(x => x.querySelector('.' + CSS.escape(icon)) || (typeof x.className === 'string' && x.classList.contains(icon)));
  if (!b) return false;
  b.click(); return true;
}
""".replace("/*__COMMON__*/", _COMMON_JS).replace("/*__SCOPE__*/", _SCOPE_JS)

CLICK_TEXT_ANY_JS = r"""
([sel, text, scope]) => {
  /*__COMMON__*/
  /*__SCOPE__*/
  const root = scopeOf(sel, scope); if (!root) return false;
  const want = __norm(text);
  const el = Array.from(root.querySelectorAll('a, [role=menuitem], [role=link], [role=button], button, .dxbl-toolbar-item, .dxbl-menu-item, span, div'))
    .filter(__vis).filter(x => outsideDialogs(sel, scope, x))
    .filter(x => __norm(__txt(x).split(/\n/)[0]) === want)
    .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length)[0];
  if (!el) return false;
  el.click(); return true;
}
""".replace("/*__COMMON__*/", _COMMON_JS).replace("/*__SCOPE__*/", _SCOPE_JS)

# Дээд цонхны (эсвэл хуудасны) grid-ээс `text` агуулсан мөр: эхлээд яг тэнцүү нүдтэй, дараа нь агуулсан
MARK_ANY_ROW_JS = r"""
([sel, text, dialog]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-anyrow], [data-ssg-anycell]').forEach(e => {
    e.removeAttribute('data-ssg-anyrow'); e.removeAttribute('data-ssg-anycell'); });
  const root = dialog ? __scope(sel, 'dialog') : document;
  if (!root) return null;
  const want = __norm(text);
  const rows = Array.from(root.querySelectorAll('tr')).filter(__vis).filter(r => r.querySelector('td') && !r.closest('thead'))
    .filter(r => dialog || !__findDialogs(sel).some(d => d.contains(r)));
  const cellsOf = (r) => Array.from(r.querySelectorAll('td')).map(td => __norm(__txt(td)).replace(/^[^\p{L}\p{N}]+/u, ''));
  const row = rows.find(r => cellsOf(r).includes(want)) || rows.find(r => __norm(__txt(r)).includes(want));
  if (!row) return null;
  row.setAttribute('data-ssg-anyrow', '1');
  const cell = Array.from(row.querySelectorAll('td')).filter(__vis)
    .find(td => __txt(td) && !td.querySelector('.sllink, a, [class*=fa-arrow], button, input, .dxbl-checkbox'));
  (cell || row).setAttribute('data-ssg-anycell', '1');
  return __txt(row).replace(/\s+/g, ' ').slice(0, 200);
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def click_icon(env: "FlowEngine", icon: str, scope: str) -> bool:
    for _ in range(40):
        if await env.page.evaluate(CLICK_ICON_JS, [sel_payload(env), icon, scope]):
            return True
        await asyncio.sleep(0.25)
    return False


async def click_text_any(env: "FlowEngine", text: str, scope: str) -> bool:
    for _ in range(20):
        if await env.page.evaluate(CLICK_TEXT_ANY_JS, [sel_payload(env), text, scope]):
            return True
        await asyncio.sleep(0.25)
    return False


async def dialog_row(env: "FlowEngine", text: str, open_: bool = False) -> bool:
    for _ in range(40):
        if await env.page.evaluate(MARK_ANY_ROW_JS, [sel_payload(env), text, True]):
            loc = env.page.locator("[data-ssg-anycell='1']")
            await (loc.dblclick if open_ else loc.click)(timeout=5000)
            return True
        await asyncio.sleep(0.25)
    return False


async def row_check(env: "FlowEngine", text: str, value: bool, dialog: bool) -> bool:
    for _ in range(40):
        if await env.page.evaluate(MARK_ANY_ROW_JS, [sel_payload(env), text, dialog]):
            cb = env.page.locator("[data-ssg-anyrow='1'] input[type=checkbox]").first
            if not await cb.count():
                return False
            if await cb.is_checked() != value:
                try:
                    await cb.click(timeout=3000)
                except Exception:
                    await env.page.locator("[data-ssg-anyrow='1'] .dxbl-checkbox").first.click(timeout=3000)
            return True
        await asyncio.sleep(0.25)
    return False


async def only_tagged_focus(env: "FlowEngine") -> bool:
    """Жагсаалтын фокуслагдсан мөр run_tag-тай эсэх. Фокусын класс алга бол харагдаж буй БҮХ мөр
    run_tag-тай (хайлтын үр дүн зөвхөн манай бичлэг) үед л True."""
    tag = env.ctx.run_tag
    state = await focus_check(env, tag)
    if state != "unknown":
        return state == "ok"
    all_rows = await env.page.evaluate(FIND_ROWS_JS, [sel_payload(env), ""])
    return bool(all_rows) and all(tag in r for r in all_rows)


async def select_row(env: "FlowEngine", text: str) -> RowSelection:
    """`text` агуулсан ганц мөрийг сонгоно. SmartApp-д мөр дарахад бичлэг шууд засварын горимд нээгдэж
    болдог тул checkbox (эсвэл сумгүй нүд) дарж, нээгдсэн эсэх / системийн алдааг буцаана."""
    rows = await grid_find(env, text)
    if not rows:
        raise StepFailed(f"'{text}' агуулсан мөр олдсонгүй")
    # Олон мөр: журнал шиг жагсаалт баримтын мөр (дебет/кредит, харьцсан данс) бүрийг тусад нь харуулдаг.
    # Хайлтын текст run_tag агуулж байвал бүх мөр манай (урсгалаар давтагдашгүй) тестийн өгөгдөл → эхнийг сонгоход
    # аюулгүй. run_tag-гүй текстээр олон ялгаатай мөр олдвол сонгохгүй.
    tag = getattr(getattr(env, "ctx", None), "run_tag", "")
    if len(rows) > 1 and not (tag and tag in text) and len({r.split(" ")[0] for r in rows}) > 1:
        raise StepFailed(f"'{text}' агуулсан {len(rows)} мөр олдлоо — нэгийг сонгох боломжгүй")
    before = await dialog_count(env)
    tabs_before = await tab_names(env)
    target = await env.page.evaluate(MARK_ROW_TARGET_JS)
    await env.page.click("[data-ssg-rowsel='1']" if target else "[data-ssg-row='0']", timeout=5000)
    sel = RowSelection(rows[0], level=before)
    # Мөр дарахад бичлэг (засварын форм) удаан нээгдэж болно — 3 сек хүртэл ажиглана
    for _ in range(12):
        await asyncio.sleep(0.25)
        if await dialog_count(env) > before or [t for t in await tab_names(env) if t not in tabs_before]:
            break
    sel.new_tabs = [t for t in await tab_names(env) if t not in tabs_before]
    sel.focus = await focus_check(env, text)
    if await dialog_count(env) > before or sel.new_tabs:
        await settle(env, 10)
        sel.opened = await dialog_count(env) > before
        sel.errors, _ = await popup_problems(env)
    return sel
