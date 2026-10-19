"""Формын талбар: олох, бөглөх, унших, ComboBox, формын таб, хадгалах."""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from app.flows.context import StepFailed
from app.runner.detectors import snapshot
from app.flows.ui._js import (
    FORM_TAB_JS,
    CLICK_OPTION_JS,
    FIND_FIELD_JS,
    MARK_CELL_LOOKUP_JS,
    MARK_LINES_ADD_JS,
    _COMMON_JS,
)
from app.flows.ui.base import fcfg, sel_payload
from app.flows.ui.wait import popup_problems, raise_on_problems, settle
from app.flows.ui.page import click_button, dialog_count
from app.flows.ui.lookup import pick_from_lookup
from app.flows.ui.lines import _activate_cell, _edit_row

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine


MARK_DROPDOWN_BTN_JS = r"""
([sel]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-ddbtn]').forEach(e => e.removeAttribute('data-ssg-ddbtn'));
  const i = document.querySelector('[data-ssg-field="1"]');
  if (!i) return false;
  const box = i.closest(sel.combobox) || i.parentElement;
  const b = box && Array.from(box.querySelectorAll('button, [role=button], .dxbl-edit-btn-dropdown, .dxbl-btn'))
      .filter(__vis).pop();
  if (!b) return false;
  b.setAttribute('data-ssg-ddbtn', '1');
  return true;
}
""".replace("/*__COMMON__*/", _COMMON_JS)


LIST_OPTIONS_JS = r"""(s) => { const v = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return Array.from(document.querySelectorAll(s)).filter(v).map(x => (x.innerText || '').trim()).filter(Boolean).slice(0, 30); }"""


async def peek_combo(env: "FlowEngine", label: str) -> list[str] | None:
    """Форм шалгалт: ComboBox-ийн жагсаалтыг нээж сонголтуудыг уншаад СОНГОЛГҮЙ хаана.
    Сонголтын текстүүд ([] = хоосон жагсаалт), нээж чадаагүй бол None."""
    await find_field(env, label)
    page, opt_sel = env.page, sel_payload(env)["list_option"]
    level = await dialog_count(env)
    opts: list[str] = []
    opened_by = ""
    for opener in ("input", "button"):
        try:
            if opener == "input":
                await page.locator("[data-ssg-field='1']").click(timeout=3000)
            else:
                if not await page.evaluate(MARK_DROPDOWN_BTN_JS, [sel_payload(env)]):
                    break
                await page.click("[data-ssg-ddbtn='1']", timeout=3000)
        except Exception:
            continue
        for _ in range(10):
            await asyncio.sleep(0.25)
            opts = await page.evaluate(LIST_OPTIONS_JS, opt_sel)
            if opts:
                break
        if opts:
            opened_by = opener
            break
    # Хаах: нээсэн аргаараа дахин дарж (toggle) — Escape нь формын цонхыг хааж болзошгүй тул сүүлд
    try:
        if opened_by == "button" and await page.evaluate(MARK_DROPDOWN_BTN_JS, [sel_payload(env)]):
            await page.click("[data-ssg-ddbtn='1']", timeout=2000)
        await asyncio.sleep(0.2)
        if await page.evaluate(LIST_OPTIONS_JS, opt_sel) and await dialog_count(env) >= level:
            await page.keyboard.press("Escape")
    except Exception:
        pass
    return opts if opened_by else None


async def peek_line_lookups(env: "FlowEngine", what: str, limit: int = 3) -> dict | None:
    """Форм шалгалт: мөрийн grid-д «+»-ээр (хадгалахгүй) мөр нэмж, мөрийн lookup баганыг нээгээд СОНГОЛГҮЙ хаана.
    Grid байхгүй бол None. Буцаах: {"ok": [...], "empty": [...], "note": ""}; системийн алдаа → StepFailed."""
    info = await env.page.evaluate(MARK_LINES_ADD_JS, [sel_payload(env)])
    if not info:
        return None
    env._line_hint = ""
    await env.page.click("[data-ssg-lineadd='1']", timeout=5000)
    await asyncio.sleep(0.5)
    errors, _ = await popup_problems(env)
    if errors:
        raise StepFailed(f"{what}: мөр нэмэхэд: {errors[0].text}", errors)
    cells = await _edit_row(env)
    out = {"ok": [], "empty": [], "note": ""}
    if not cells:
        out["note"] = "«+» дарахад засварлах мөр гарсангүй"
        return out
    name = lambda i: (info["headers"][i] if i < len(info["headers"]) else f"#{i}") or f"#{i}"
    for c in [c for c in cells if c["lookup"] and not c["command"]][:limit]:
        col = name(c["i"])
        await _activate_cell(env, c["i"])
        if not await env.page.evaluate(MARK_CELL_LOOKUP_JS, c["i"]):
            continue
        row = await pick_from_lookup(env, f"{what}: мөрийн '{col}' баганын сонголтын", pick=False)
        out["ok" if row else "empty"].append(col)
    return out


async def pick_option(env: "FlowEngine", label: str, want: str = "") -> str:
    """Сонголтын талбарт (select / combobox / lookup "...") эхний боломжит утгыг сонгоно.
    want: энэ текст агуулсан мөр/утгыг сонгох (ж: автомат CRUD-ийн үүсгэсэн мастер бичлэг) — олдохгүй бол
    эхний утга. Сонгосон утгын текстийг, чадахгүй бол "" буцаана. Сонгох нь өгөгдөл өөрчлөхгүй."""
    info = await find_field(env, label)
    page = env.page
    loc = page.locator("[data-ssg-field='1']")
    picked = ""
    if info["tag"] == "select":
        n = info.get("options") or 0
        if n < 1:
            return ""
        await loc.select_option(index=1 if n > 1 else 0)
        picked = await loc.evaluate("e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : ''")
    elif info.get("lookup"):
        picked = await pick_from_lookup(env, f"'{label}' талбарын сонголтын", want=want) if want else ""
        if not picked and not want.startswith("!"):        # «!…» (хасах) нөхцөлгүйгээр сонгохгүй
            picked = await pick_from_lookup(env, f"'{label}' талбарын сонголтын")
        if not picked:
            return ""
    elif info["combo"]:
        js = r"""([s, want]) => { const v = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
          const all = Array.from(document.querySelectorAll(s)).filter(v).filter(x => (x.innerText || '').trim());
          const o = (want && all.find(x => x.innerText.includes(want))) || all[0];
          if (!o) return ''; const t = o.innerText.trim(); o.click(); return t; }"""
        # 1) input дээр дарах  2) нээгдэхгүй бол dropdown (▾) товч — readonly ComboBox-д input дарахад нээгддэггүй
        for opener in ("input", "button"):
            try:
                if opener == "input":
                    await loc.click(timeout=3000)
                else:
                    btn = await page.evaluate(MARK_DROPDOWN_BTN_JS, [sel_payload(env)])
                    if not btn:
                        break
                    await page.click("[data-ssg-ddbtn='1']", timeout=3000)
            except Exception:
                continue
            for _ in range(8 if opener == "input" else 16):
                picked = await page.evaluate(js, [sel_payload(env)["list_option"], want])
                if picked:
                    break
                await asyncio.sleep(0.25)
            if picked:
                break
    else:
        return ""
    await asyncio.sleep(0.3)
    now = await find_field(env, label, wait_s=2)
    if info["tag"] == "select":
        return picked
    after = (now.get("value") or "").strip()
    env.runlog.log(f"[{getattr(env, 'flow_name', '')}] сонголт '{label}': «{picked[:60]}» → талбарын утга "
                   f"«{(info.get('value') or '')[:30]}» → «{after[:30]}»", "info", to_ui=False)
    # Утга үнэхээр тогтсон (хоосон биш, өмнөхөөс өөр) бол л амжилттай
    return picked if after and after != (info.get("value") or "").strip() else ""


# Дээд цонхны (формын) доторх табууд (Анкетийн мэдээлэл / Цалингийн мэдээлэл …): i-г дарна, i=-1 бол тоог буцаана
# Нээлттэй формын ДОТООД табууд (Анкет / Цалин / Үзүүлэлт …): форм цонх бол түүний дотор, шинэ таб дээрх форм
# бол идэвхтэй апп-табын агуулга дотор (апп-ын үндсэн таб мөрийг оруулахгүй). i ≥ 0 бол дарна; нэрсийг буцаана.
FORM_TABS_JS = r"""
([sel, tabsel, i]) => {
  /*__COMMON__*/
  let root = __scope(sel, 'dialog');
  if (!root) {
    const panels = Array.from(document.querySelectorAll('[role=tabpanel]')).filter(__vis)
      .filter(p => !__findDialogs(sel).some(d => d.contains(p)));
    root = panels.sort((a, b) => b.querySelectorAll('input').length - a.querySelectorAll('input').length)[0] || null;
  }
  if (!root) return [];
  const appTabs = new Set(Array.from(document.querySelectorAll(tabsel)).filter(t => !root.contains(t)));
  const tabs = Array.from(root.querySelectorAll('[role=tab], .dxbl-tabs-item')).filter(__vis)
    .filter(t => !appTabs.has(t))
    .filter((t, k, a) => !a.some(o => o !== t && o.contains(t)));
  if (i >= 0 && tabs[i]) tabs[i].click();
  return tabs.map(t => __txt(t).split(/\n/)[0].trim());
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def form_tabs(env: "FlowEngine", click: int = -1) -> list[str]:
    try:
        return await env.page.evaluate(FORM_TABS_JS, [sel_payload(env), env.cfg["selectors"]["tab"], click])
    except Exception:
        return []


async def reveal_field(env: "FlowEngine", label: str) -> dict:
    """Талбар харагдахгүй бол формын дотоод табуудыг ээлжлэн нээж хайна (таб солиход өгөгдөл хадгалагдана).
    Олдохгүй бол АНХНЫ табыг буцааж нээнэ — «Данс оруулна уу» (мөрийн grid «Данс» таб дээр) мэт талбар биш
    мессежийн дараа «Бусад» таб нээлттэй үлдэж мөрийн grid нуугдаж байсан (Accounting журнал, 2026-10-15)."""
    try:
        return await find_field(env, label)
    except StepFailed:
        pass
    active = await env.page.evaluate(FORM_TAB_JS, [sel_payload(env), -2])
    n = await env.page.evaluate(FORM_TAB_JS, [sel_payload(env), -1])
    for i in range(min(n, 12)):
        await env.page.evaluate(FORM_TAB_JS, [sel_payload(env), i])
        try:
            return await find_field(env, label, wait_s=1.0)
        except StepFailed:
            continue
    if n and active is not None and active >= 0:
        await env.page.evaluate(FORM_TAB_JS, [sel_payload(env), active])
        await asyncio.sleep(0.3)
    raise StepFailed(f"'{label}' талбар олдсонгүй")


async def choose_combo(env: "FlowEngine", label: str, option: str) -> bool:
    """ComboBox-ийг нээж яг `option` сонголтыг дарна (find_field-ээр талбар тэмдэглэгдсэн байх)."""
    await find_field(env, label)
    page = env.page
    for opener in ("input", "button"):
        try:
            if opener == "input":
                await page.locator("[data-ssg-field='1']").click(timeout=3000)
            else:
                if not await page.evaluate(MARK_DROPDOWN_BTN_JS, [sel_payload(env)]):
                    break
                await page.click("[data-ssg-ddbtn='1']", timeout=3000)
        except Exception:
            continue
        for _ in range(12):
            await asyncio.sleep(0.25)
            if await page.evaluate(CLICK_OPTION_JS, [sel_payload(env), option]):
                await asyncio.sleep(0.3)
                return True
    return False


CLICK_TAB_JS = r"""
([sel, name]) => {
  /*__COMMON__*/
  const want = __norm(name);
  const roots = [__scope(sel, 'dialog'), document].filter(Boolean);
  for (const r of roots) {
    const t = Array.from(r.querySelectorAll('[role=tab], .dxbl-tabs-item')).filter(__vis)
      .find(x => __norm(__txt(x).split(/\n/)[0]) === want);
    if (t) { t.click(); return true; }
  }
  return false;
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def click_tab(env: "FlowEngine", name: str) -> bool:
    for _ in range(20):
        if await env.page.evaluate(CLICK_TAB_JS, [sel_payload(env), name]):
            return True
        await asyncio.sleep(0.25)
    return False


async def line_column(env: "FlowEngine", column: str) -> int:
    """Мөрийн grid-ийн `column` баганын индекс (засварлах мөр байх ёстой)."""
    info = await env.page.evaluate(MARK_LINES_ADD_JS, [sel_payload(env)])
    if not info:
        raise StepFailed("Мөрийн grid олдсонгүй")
    want = re.sub(r"\s+", " ", column).strip().lower()
    heads = [re.sub(r"\s+", " ", h or "").strip().lower() for h in info["headers"]]
    if want not in heads:
        raise StepFailed(f"Мөрийн grid-д «{column}» багана алга (байгаа: {', '.join(h for h in info['headers'] if h)})")
    if not await _edit_row(env):
        raise StepFailed("Засварлах мөр алга — эхлээд «+» мөр нэмнэ")
    return heads.index(want)


async def find_field(env: "FlowEngine", label: str, wait_s: float = 0.0) -> dict:
    deadline = time.monotonic() + wait_s
    while True:
        info = await env.page.evaluate(FIND_FIELD_JS, [sel_payload(env), label])
        if info:
            return info
        if time.monotonic() >= deadline:
            raise StepFailed(f"'{label}' талбар олдсонгүй")
        await asyncio.sleep(0.15)


async def fill_field(env: "FlowEngine", label: str, value, as_date: bool = False) -> None:
    """as_date: DateEdit (▾ товчтой тул combo мэт) — жагсаалтаас сонгохгүй, огноог бичээд Tab."""
    info = await find_field(env, label)
    if info["readonly"]:
        raise StepFailed(f"'{label}' талбар засах боломжгүй (readonly)")
    loc = env.page.locator("[data-ssg-field='1']")
    if isinstance(value, bool) or info["type"] in ("checkbox", "radio"):
        await loc.set_checked(bool(value))
        return
    text = "" if value is None else str(value)
    if info["tag"] == "select":
        await loc.select_option(label=text)
        return
    if info["combo"] and not as_date:
        await loc.fill("")
        await loc.press_sequentially(text, delay=20)
        await settle(env, 5)
        if not await env.page.evaluate(CLICK_OPTION_JS, [sel_payload(env), text]):
            await loc.press("Enter")
        return
    await loc.fill(text)
    # DevExpress-ийн input ихэвчлэн change/blur дээр утгаа bind хийдэг
    await loc.press("Tab")


async def read_field(env: "FlowEngine", label: str) -> str:
    return (await find_field(env, label))["value"]



async def save_form(env: "FlowEngine", save_texts: list[str]) -> None:
    """Хадгалах дарж, алдаагүй хадгалагдсаныг (форм хаагдсан эсвэл тайван болсон) баталгаажуулна."""
    before = await dialog_count(env)
    await click_button(env, save_texts)
    timeout = fcfg(env).get("save_timeout_ms", 15000) / 1000
    deadline = time.monotonic() + timeout
    await asyncio.sleep(0.2)
    cap = fcfg(env).get("settle_cap_ms", 1500) / 1000
    calm_since = None
    while time.monotonic() < deadline:
        await raise_on_problems(env, "Хадгалахад")
        st = await snapshot(env.page, env.cfg)
        ok = bool(st and not st.loading)
        calm_since = (calm_since or time.monotonic()) if ok else None
        if ok and (env.col.quiet_for(fcfg(env).get("settle_ms", 500) / 1000)
                   or time.monotonic() - calm_since >= cap):
            if before == 0 or len(st.dialogs) < before:
                await asyncio.sleep(0.2)
                await raise_on_problems(env, "Хадгалахад")
                return
        await asyncio.sleep(0.15)
    raise StepFailed(f"Хадгалсны дараа форм {timeout:.0f} сек-д хаагдсангүй")
