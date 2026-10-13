"""Товч, таб, toolbar, жагсаалтын эхний мөр, форм/таб хаах."""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from app import models as M
from app.flows.context import StepFailed
from app.models import Finding
from app.runner.detectors import snapshot
from app.flows.ui._js import (
    FIND_BUTTON_JS,
    MARK_HEADER_CLOSE_JS,
    MARK_SEARCH_JS,
    PROBE_JS,
    SCAN_FORM_JS,
    TOP_DIALOG_LEN_JS,
    _COMMON_JS,
)
from app.flows.ui.base import buttons, fcfg, sel_payload
from app.flows.ui.wait import popup_problems, settle

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine


# ------------------------------------------------------------------ товч / талбар
async def click_button(env: "FlowEngine", texts: list[str], scope: str = "auto",
                       wait_s: float = 5.0) -> str:
    deadline = time.monotonic() + wait_s
    while True:
        found = await env.page.evaluate(FIND_BUTTON_JS, [sel_payload(env), texts, scope])
        if found:
            try:
                await env.page.click("[data-ssg-btn='1']", timeout=5000)
            except Exception as e:
                # Товчийг өөр цонх (ж: алдааны цонх, удаан нээгдсэн форм) хааж байна — Timeout биш шалтгааныг мэдээлнэ
                errors, warnings = await popup_problems(env)
                if errors:
                    raise StepFailed(f"«{found}» дарах үед: {errors[0].text}", errors) from None
                if warnings:
                    raise StepFailed(f"«{found}» дарах үед: баталгаажуулалтын анхааруулга — {warnings[0]}",
                                     [Finding(M.WARNING_TOAST, w) for w in warnings]) from None
                raise StepFailed(f"«{found}» товчийг дарж чадсангүй (өөр цонх/элемент хааж байна): "
                                 f"{str(e).splitlines()[0][:150]}") from None
            return found
        if time.monotonic() >= deadline:
            raise StepFailed(f"Товч олдсонгүй: {' / '.join(texts)}")
        await asyncio.sleep(0.15)


BUTTON_EXISTS_JS = r"""
([sel, texts]) => {
  /*__COMMON__*/
  const want = texts.map(__norm);
  const dialogs = __findDialogs(sel);
  return Array.from(document.querySelectorAll(sel.button)).filter(__vis)
    .filter(b => !dialogs.some(d => d.contains(b)))
    .some(b => [__txt(b), b.getAttribute('title'), b.getAttribute('aria-label')].some(t => want.includes(__norm(t))));
}
""".replace("/*__COMMON__*/", _COMMON_JS)


# Жагсаалтын toolbar дахь dropdown товч (текст + ▾/chevron) — ж: HR «Үйл ажиллагааны загвар»: «Шинэ» нь дангаараа
# юу ч хийдэггүй, шинэ бичлэгийг «Тогтмол үйл ажиллагаа ▾»-аас төрөл сонгож үүсгэдэг. i ≥ 0 бол тэмдэглэнэ.
TOOLBAR_DROPDOWNS_JS = r"""
([sel, i]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-tbdd]').forEach(e => e.removeAttribute('data-ssg-tbdd'));
  const dl = __findDialogs(sel);
  const isChevron = (el) => /chevron|caret|angle-down|arrow-down|dropdown|spindown|expand-icon/i
      .test(typeof el.className === 'string' ? el.className : (el.className && el.className.baseVal) || '')
      || el.getAttribute('aria-haspopup') === 'true' || el.getAttribute('aria-haspopup') === 'menu';
  const btns = Array.from(document.querySelectorAll('button, [role=button], .dxbl-btn, .dxbl-dropdown-button'))
    .filter(__vis).filter(b => !dl.some(d => d.contains(b)) && !b.closest(sel.menu_root || '.mainnavbar'))
    .filter(b => !b.closest('table, .dxbl-grid, [role=grid]'))
    .filter(b => isChevron(b) || Array.from(b.querySelectorAll('*')).some(isChevron) || /▾|▼/.test(__txt(b)))
    .filter(b => __txt(b).replace(/[▾▼]/g, '').trim().length > 1);
  if (i >= 0 && btns[i]) btns[i].setAttribute('data-ssg-tbdd', '1');
  return btns.map(b => __txt(b).replace(/\s+/g, ' ').replace(/[▾▼]/g, '').trim().slice(0, 60));
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def mark_seen(env: "FlowEngine") -> None:
    """Одоо харагдаж буй бүх элементийг тэмдэглэнэ — дараа нь `new_dom_hint` шинээр гарсныг л харуулна."""
    try:
        await env.page.evaluate("() => document.querySelectorAll('body *').forEach(e => e.setAttribute('data-ssg-seen', '1'))")
    except Exception:
        pass


async def new_dom_hint(env: "FlowEngine") -> str:
    """`mark_seen`-ээс хойш ШИНЭЭР гарсан харагдах элементүүд (дээд түвшнийх) — dropdown/popup-ийн DOM оношилгоо."""
    js = r"""() => Array.from(document.querySelectorAll('body *:not([data-ssg-seen])'))
      .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
      .filter(e => !e.parentElement || e.parentElement.hasAttribute('data-ssg-seen'))
      .slice(0, 8).map(e => `<${e.tagName.toLowerCase()} class="${String(e.className).slice(0, 100)}" role="${e.getAttribute('role') || ''}">`
        + (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120)).join(' | ') || '(шинэ элемент алга)'"""
    try:
        return (await env.page.evaluate(js))[:1500]
    except Exception as e:
        return f"({e!r})"


async def popup_dom_hint(env: "FlowEngine") -> str:
    """Харагдаж буй dropdown/popup/menu элементүүдийн товч DOM (class + текст) — оношилгооны log-д."""
    js = r"""() => Array.from(document.querySelectorAll('[class*=dropdown], [class*=popup], [class*=menu], [role=menu], [role=listbox]'))
      .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
      .filter(e => !e.closest('.mainnavbar, nav'))
      .slice(0, 12).map(e => `<${e.tagName.toLowerCase()} class="${String(e.className).slice(0, 80)}" role="${e.getAttribute('role') || ''}">`
        + (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60)).join(' | ')"""
    try:
        return (await env.page.evaluate(js))[:1500]
    except Exception as e:
        return f"({e!r})"


# Жагсаалтын toolbar-ын УНШИЛТЫН товчнууд (Хэвлэх / Экспорт / PDF харах …): [safe, block] үгсээр шүүнэ.
# i ≥ 0 бол i-д товчийг [data-ssg-act='1'] гэж тэмдэглэнэ.
ACTION_BUTTONS_JS = r"""
([sel, safe, block, i]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-act]').forEach(e => e.removeAttribute('data-ssg-act'));
  const dl = __findDialogs(sel);
  const label = (b) => [__txt(b), b.getAttribute('title'), b.getAttribute('aria-label')]
      .map(t => (t || '').replace(/\s+/g, ' ').trim()).find(t => t) || '';
  const low = (t) => t.toLowerCase();
  const btns = Array.from(document.querySelectorAll(sel.button)).filter(__vis)
    .filter(b => !dl.some(d => d.contains(b)) && !b.closest(sel.menu_root || '.mainnavbar'))
    .filter(b => !b.closest('table, .dxbl-grid, [role=grid], [role=tablist]'))
    .filter(b => !b.disabled && b.getAttribute('aria-disabled') !== 'true')
    .filter(b => { const t = low(label(b)); return t && t.length <= 40
        && safe.some(w => t.includes(low(w))) && !block.some(w => t.includes(low(w))); });
  const seen = new Set();
  const uniq = btns.filter(b => { const t = label(b); if (seen.has(t)) return false; seen.add(t); return true; });
  if (i >= 0 && uniq[i]) uniq[i].setAttribute('data-ssg-act', '1');
  return uniq.map(label);
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def action_buttons(env: "FlowEngine", mark: int = -1) -> list[str]:
    fc = fcfg(env)
    safe = list(fc.get("readonly_actions") or [])
    block = list(fc.get("readonly_action_block") or []) + list(env.cfg.get("forbidden_button_words") or [])
    if not safe:
        return []
    try:
        return await env.page.evaluate(ACTION_BUTTONS_JS, [sel_payload(env), safe, block, mark])
    except Exception:
        return []


async def toolbar_dropdowns(env: "FlowEngine", mark: int = -1) -> list[str]:
    try:
        return await env.page.evaluate(TOOLBAR_DROPDOWNS_JS, [sel_payload(env), mark])
    except Exception:
        return []


async def is_form_page(env: "FlowEngine") -> bool:
    """Цэс нь жагсаалт биш, ШУУД баримтын форм (ж: «Гүйлгээ», «Нэхэмжлэх», «Эхний үлдэгдэл») эсэх:
    хуудсан дээр (цонхноос гадна) «Хадгалах» төрлийн товч байна."""
    if await has_button(env, buttons(env, "save")):
        return True
    prefixes = [w.lower() for w in fcfg(env).get("save_prefixes") or []]
    if not prefixes:
        return False
    js = r"""([sel, pre]) => {
      /*__COMMON__*/
      const dl = __findDialogs(sel);
      return Array.from(document.querySelectorAll(sel.button)).filter(__vis)
        .filter(b => !dl.some(d => d.contains(b)) && !b.closest(sel.menu_root || '.mainnavbar'))
        .some(b => { const t = __txt(b).trim().toLowerCase(); return pre.some(p => t.startsWith(p)); });
    }""".replace("/*__COMMON__*/", _COMMON_JS)
    try:
        return bool(await env.page.evaluate(js, [sel_payload(env), prefixes]))
    except Exception:
        return False


async def has_button(env: "FlowEngine", texts: list[str]) -> bool:
    """Хуудсан дээр (цонхноос гадна) товч байгаа эсэх — идэвхгүй (мөр сонгоогүй) байсан ч тооцно."""
    return bool(await env.page.evaluate(BUTTON_EXISTS_JS, [sel_payload(env), texts]))


async def form_has_inputs(env: "FlowEngine") -> bool:
    """Нээлттэй формын (сүүлийн цонх эсвэл идэвхтэй таб) идэвхтэй табд input/textarea/select байгаа эсэх
    (grid-ийн шүүлтүүрийн мөрөөс бусад)."""
    js = r"""() => Array.from(document.querySelectorAll('input:not([type=hidden]), textarea, select'))
      .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
      .some(e => !e.closest('.dxbl-grid thead, .dxbl-grid-filter-row, [class*=filter-row], .mainnavbar, nav, [class*=search]'))"""
    try:
        return bool(await env.page.evaluate(js))
    except Exception:
        return True


async def scan_form(env: "FlowEngine", wait_s: float = 10.0) -> list[dict]:
    """Нээгдсэн формын талбаруудыг (шошго, төрөл, заавал эсэх, утга) илрүүлнэ."""
    deadline = time.monotonic() + wait_s
    while True:
        fields = await env.page.evaluate(SCAN_FORM_JS, [sel_payload(env)])
        if fields or time.monotonic() >= deadline:
            return fields or []
        await asyncio.sleep(0.2)


async def probe(env: "FlowEngine") -> dict:
    """Оношлогоо: табууд, цонхнууд, popup цэс, харагдаж буй input-уудын дээж (шошго таамаг)."""
    try:
        return await env.page.evaluate(PROBE_JS, [sel_payload(env), env.cfg["selectors"]["tab"]])
    except Exception as e:
        return {"error": str(e)[:200]}


async def tab_names(env: "FlowEngine") -> list[str]:
    st = await snapshot(env.page, env.cfg)
    return st.tab_texts() if st else []


# Dropdown-ын зүйлс: эхлээд popup_menu_item selector; таарахгүй бол харагдаж буй popup контейнер (popup_menu)
# доторх хамгийн гүн текстэй дарагдах элементүүд (li / button / a / [role=menuitem] / *item*). DevExpress-ийн
# хувилбар бүрт dropdown-ын DOM өөр (HR: <dxbl-dropdown-dialog class="dxbl-toolbar-dropdown">).
POPUP_ITEMS_JS = r"""
([itemSel, boxSel, want]) => {
  const v = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const t = (x) => (x.innerText || x.textContent || '').replace(/\s+/g, ' ').trim();
  const q = (s, root) => { try { return Array.from((root || document).querySelectorAll(s)); } catch (e) { return []; } };
  let els = itemSel ? q(itemSel).filter(v).filter(e => t(e)) : [];
  if (!els.length && boxSel) {
    for (const box of q(boxSel).filter(v)) {
      const cand = q('li, button, a, [role=menuitem], [class*=item]', box).filter(v).filter(e => t(e));
      // хамгийн гүн (өөр нэр дэвшигчийг агуулаагүй) элементүүд
      els = els.concat(cand.filter(e => !cand.some(o => o !== e && e.contains(o) && t(o) === t(e))));
    }
  }
  const seen = new Set();
  els = els.filter(e => { const k = t(e); if (seen.has(k)) return false; seen.add(k); return true; });
  if (want !== null) { const o = els.find(e => t(e) === want); if (!o) return false; o.click(); return true; }
  return els.map(t).slice(0, 20);
}
"""


async def popup_menu_items(env: "FlowEngine") -> list[str]:
    """«Шинэ» дарахад гарсан dropdown цэсний (баримтын төрөл сонгох г.м.) зүйлс."""
    sels = fcfg(env).get("selectors", {})
    try:
        items = await env.page.evaluate(POPUP_ITEMS_JS, [sels.get("popup_menu_item"), sels.get("popup_menu"), None])
    except Exception:
        items = []
    return list(dict.fromkeys(items))          # нэг зүйл хоёр selector-оор (li + menu-item) давхардаж болно


def safe_type_items(env: "FlowEngine", items: list[str]) -> tuple[list[str], list[str]]:
    """Dropdown-ын зүйлсийг (шинэ бичлэгийн төрөл) аюулгүй / хориотой гэж хуваана — устгах, засах, батлах … хэзээ
    ч дарахгүй (READ-ONLY)."""
    block = [w.lower() for w in fcfg(env).get("type_item_block") or []]
    safe = [i for i in items if not any(w in i.lower() for w in block)]
    return safe, [i for i in items if i not in safe]


async def click_popup_item(env: "FlowEngine", text: str) -> bool:
    """«Шинэ» dropdown-ы `text` зүйлийг дарна (баримтын төрөл)."""
    sels = fcfg(env).get("selectors", {})
    try:
        return bool(await env.page.evaluate(POPUP_ITEMS_JS, [sels.get("popup_menu_item"), sels.get("popup_menu"), text]))
    except Exception:
        return False


# Хуудсан дээрх (цонхноос гадна) grid/модууд — хоёр ба түүнээс олон бол хамгийн том нь үндсэн жагсаалт
_MAIN_GRID_JS = r"""
  const __grids = (sel) => {
    const dl = __findDialogs(sel);
    return Array.from(document.querySelectorAll('.dxbl-grid, [role=grid], [role=treegrid], dxbl-tree-list'))
      .filter(__vis).filter(g => !dl.some(d => d.contains(g)))
      .filter(g => !(g.parentElement && g.parentElement.closest('.dxbl-grid, [role=grid], [role=treegrid], dxbl-tree-list')));
  };
  const __area = (g) => { const r = g.getBoundingClientRect(); return r.width * r.height; };
  const __mainGrid = (sel) => { const gs = __grids(sel); return gs.length > 1 ? gs.sort((a, b) => __area(b) - __area(a))[0] : null; };
"""


# Master-detail жагсаалтын хажуугийн (бүлгийн) панелийн мөр — «Бүх …» үндэс байвал түүнийг, үгүй бол эхнийхийг
# [data-ssg-side='1'] гэж тэмдэглэнэ. Мөр сонгох нь уншилт (өгөгдөл өөрчлөхгүй).
SIDE_GROUP_ROW_JS = r"""
([sel]) => {
  /*__COMMON__*/
  /*__MAIN__*/
  document.querySelectorAll('[data-ssg-side]').forEach(e => e.removeAttribute('data-ssg-side'));
  const main = __mainGrid(sel);
  if (!main) return null;
  const side = __grids(sel).filter(g => g !== main);
  const empty = /^(бичлэг олдсонгүй|no data|түр хүлээнэ үү)/i;
  const rows = side.flatMap(g => Array.from(g.querySelectorAll(sel.grid_row + ', [role=row], tr')))
    .filter(__vis).filter(r => !r.closest('thead'))
    .filter(r => !/header|footer|filter|edit-new/.test(typeof r.className === 'string' ? r.className : ''))
    .filter(r => { const t = __txt(r); return t && !empty.test(t); });
  const row = rows.find(r => /^(\S+\s+)?бүх(\s|$)/i.test(__txt(r))) || rows[0];
  if (!row) return null;
  (row.querySelector('td') || row).setAttribute('data-ssg-side', '1');
  return __txt(row).replace(/\s+/g, ' ').slice(0, 80);
}
""".replace("/*__COMMON__*/", _COMMON_JS).replace("/*__MAIN__*/", _MAIN_GRID_JS)


async def select_side_group(env: "FlowEngine") -> str:
    """Master-detail жагсаалт хоосон / «Шинэ» ажиллахгүй бол бүлгийн панелээс бүлэг сонгоно (уншилт). Бүлгийн нэр | ""."""
    try:
        name = await env.page.evaluate(SIDE_GROUP_ROW_JS, [sel_payload(env)])
        if not name:
            return ""
        await env.page.click("[data-ssg-side='1']", timeout=3000)
    except Exception:
        return ""
    await settle(env, 5)
    return name


# Жагсаалтын (цонхноос гадна) эхний өгөгдлийн мөрийг [data-ssg-row='0'] гэж тэмдэглэнэ; хоосон бол null
MARK_FIRST_ROW_JS = r"""
([sel, idx]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-row]').forEach(e => e.removeAttribute('data-ssg-row'));
  const empty = /^(бичлэг олдсонгүй|no data|түр хүлээнэ үү)/i;
  const rows = Array.from(document.querySelectorAll(sel.grid_row)).filter(__vis)
    .filter(r => !__findDialogs(sel).some(d => d.contains(r)))
    .filter(r => !/header|footer|group|filter|edit-new/.test(typeof r.className === 'string' ? r.className : ''))
    .filter(r => { const t = __txt(r); return t && !empty.test(t) && !/^[\d\s.,]+$/.test(t); });
  // Зүүн талын бүлгийн мод (dxbl-tree-list) биш өгөгдлийн grid-ийн мөр; жагсаалт өөрөө мод бол «Бүх …» үндсийг алгасна
  const inTree = (r) => !!r.closest('dxbl-tree-list');
  const isRoot = (r) => /^(\S+\s+)?бүх(\s|$)/i.test(__txt(r));
  // Master-detail (зүүн талд «Групп» панел + баруун талд үндсэн жагсаалт): үндсэн = хамгийн том grid. Үндсэн
  // жагсаалт хоосон бол бүлгийн мөрийг засварын бичлэг гэж андуурахгүй (HR «Ажилтны жагсаалт»: «SL групп» мөр)
  const main = __mainGrid(sel);
  const pool = main && main.tagName.toLowerCase() !== 'dxbl-tree-list' ? rows.filter(r => main.contains(r)) : rows;
  // idx — хэд дэх бичлэг (0 = эхний). «Бүх …» мэт систем/үндсэн мөрийг өөр мөр байвал алгасна
  let cands = pool.filter(r => !inTree(r) && r.querySelectorAll('td').length > 1);
  if (cands.length > 1) cands = cands.filter(r => !isRoot(r)).concat(cands.filter(isRoot));
  if (!cands.length && !(main && main.tagName.toLowerCase() !== 'dxbl-tree-list'))
    cands = pool.filter(r => inTree(r) && !isRoot(r));
  const row = cands[idx || 0] || null;
  if (!row) return null;
  row.setAttribute('data-ssg-row', '0');
  return __txt(row).replace(/\s+/g, ' ').slice(0, 200);
}
""".replace("/*__COMMON__*/", _COMMON_JS + _MAIN_GRID_JS)


# Жагсаалтын «Шүүлтийн нөхцөл» дэх огнооны хүрээний ЭХЛЭХ огноо (цонхноос гадна, grid-ээс гадна эхний огноо)
MARK_FILTER_DATE_JS = r"""
([sel]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-fdate]').forEach(e => e.removeAttribute('data-ssg-fdate'));
  const i = Array.from(document.querySelectorAll('input')).filter(__vis)
    .filter(x => !__findDialogs(sel).some(d => d.contains(x)))
    .filter(x => !x.closest('.dxbl-grid td, .dxbl-grid th, [role=gridcell], thead'))
    .find(x => __isDate(sel, x) && !x.readOnly && !x.disabled);
  if (!i) return null;
  i.setAttribute('data-ssg-fdate', '1');
  return i.value || '';
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def widen_date_filter(env: "FlowEngine", years: int = 5) -> str:
    """Жагсаалт огноогоор (ж: энэ улирал) шүүгдээд хоосон бол эхлэх огноог `years` жилээр ухрааж «Шүүх» дарна.
    Зөвхөн шүүлтүүр (уншилт) — өгөгдөл өөрчлөхгүй. Шинэ огноог, чадахгүй бол "" буцаана."""
    cur = await env.page.evaluate(MARK_FILTER_DATE_JS, [sel_payload(env)])
    if cur is None:
        return ""
    import datetime as _dt
    start = _dt.date(_dt.date.today().year - years, 1, 1)
    m = re.fullmatch(r"\d{4}([-./])\d{2}\1\d{2}", cur.strip())
    m2 = re.fullmatch(r"\d{2}([-./])\d{2}\1\d{4}", cur.strip())
    new = (start.strftime(f"%Y{m.group(1)}%m{m.group(1)}%d") if m
           else start.strftime(f"%d{m2.group(1)}%m{m2.group(1)}%Y") if m2 else start.isoformat())
    loc = env.page.locator("[data-ssg-fdate='1']")
    try:
        await loc.fill(new)
        await loc.press("Enter")
        await loc.press("Tab")
    except Exception:
        return ""
    try:
        await click_button(env, buttons(env, "filter") or ["Шүүх"], scope="page", wait_s=1.5)
    except StepFailed:
        pass                      # зарим жагсаалт огноо солимогц өөрөө шүүдэг
    await asyncio.sleep(0.3)
    await settle(env)
    return new


async def clear_grid_search(env: "FlowEngine") -> bool:
    """Жагсаалтын хайлтын талбарт текст байвал цэвэрлэнэ. Цэвэрлэсэн бол True."""
    if not await env.page.evaluate(MARK_SEARCH_JS, [sel_payload(env)]):
        return False
    loc = env.page.locator("[data-ssg-search='1']")
    try:
        if not (await loc.input_value()).strip():
            return False
        await loc.fill("")
        await loc.press("Enter")
        await settle(env, 5)
        return True
    except Exception:
        return False


async def refresh_list(env: "FlowEngine") -> bool:
    """Жагсаалтын «Сэргээх» товч (уншилт) — дарсан бол True."""
    try:
        await click_button(env, ["Сэргээх", "Refresh"], scope="page", wait_s=1.0)
    except StepFailed:
        return False
    await asyncio.sleep(0.3)
    await settle(env)
    return True


LIST_EMPTY_JS = r"""
([sel]) => {
  /*__COMMON__*/
  const dl = __findDialogs(sel);
  return Array.from(document.querySelectorAll('.dxbl-grid, [role=grid], [role=treegrid], table')).filter(__vis)
    .filter(g => !dl.some(d => d.contains(g)) && !g.closest('dxbl-tree-list'))
    .some(g => /бичлэг олдсонгүй|no data|no records/i.test(__txt(g)));
}
""".replace("/*__COMMON__*/", _COMMON_JS)


async def mark_first_row(env: "FlowEngine", wait_s: float = 5.0) -> str | None:
    """Жагсаалт async ачаалагддаг тул мөр гартал (дээд тал нь wait_s) хүлээнэ."""
    deadline = time.monotonic() + wait_s
    empty_since = None
    while True:
        row = await env.page.evaluate(MARK_FIRST_ROW_JS, [sel_payload(env)])
        if row or time.monotonic() >= deadline:
            return row
        # Grid «Бичлэг олдсонгүй» гэж тогтвортой харуулж, ачаалал явагдахгүй байвал дахин хүлээх утгагүй
        st = await snapshot(env.page, env.cfg)
        empty = bool(st and not st.loading) and await env.page.evaluate(LIST_EMPTY_JS, [sel_payload(env)])
        empty_since = (empty_since or time.monotonic()) if empty else None
        if empty and time.monotonic() - empty_since >= fcfg(env).get("empty_stable_ms", 1500) / 1000:
            return None
        await asyncio.sleep(0.3)


async def close_tabs(env: "FlowEngine", names: list[str]) -> None:
    """Автомат CRUD-ийн нээсэн (хадгалаагүй) табыг хаана; "Хадгалах уу?" асуувал Үгүй/Болих."""
    from app.runner.smoke import CLOSE_TAB_JS
    for _ in range(len(names) * 2):
        name = await env.page.evaluate(CLOSE_TAB_JS, [env.cfg["selectors"],
                                                      list(env.cfg.get("protected_tabs") or []), names])
        if not name:
            return
        try:
            await env.page.click("[data-ssg-close='1']", timeout=2000)
        except Exception:
            return
        await asyncio.sleep(0.3)
        await close_form(env)


async def close_form(env: "FlowEngine", keep: int = 0, max_rounds: int = 6) -> None:
    """Дээд давхаргын цонхнуудыг (dialog_count <= keep болтол) ХАДГАЛАХГҮЙ хаана:
    (богино алдаа/анхааруулгын цонх бол OK) → Болих/Үгүй/Хаах → толгойн ✕ → Escape.
    Цонх бүр хаагдмагц (тогтмол sleep-гүй) дараагийнх руу шилжинэ."""
    safe = list(env.cfg.get("safe_dialog_buttons") or ["OK"])
    for _ in range(max_rounds):
        n = await dialog_count(env)
        if n <= keep:
            return
        try:
            errors, warnings = await popup_problems(env)
            # Дээд давхарга нь богино мессеж цонх (алдаа / «Анхаар») — зөвхөн тэр үед OK мэт товч дарна
            # (урт формын цонхонд OK нь хадгалах утгатай байж болзошгүй)
            short = (errors or warnings) and await env.page.evaluate(TOP_DIALOG_LEN_JS, [sel_payload(env)]) <= 300
            done = False
            for texts in ([safe] if short else []) + [buttons(env, "cancel") or ["Болих", "Хаах"]]:
                try:
                    await click_button(env, texts, scope="dialog", wait_s=0.1)
                    done = True
                    break
                except StepFailed:
                    continue
            if not done:
                if await env.page.evaluate(MARK_HEADER_CLOSE_JS, [sel_payload(env)]):
                    await env.page.click("[data-ssg-hclose='1']", timeout=2000)
                else:
                    await env.page.keyboard.press("Escape")
        except Exception:
            try:
                await env.page.keyboard.press("Escape")
            except Exception:
                pass
        for _ in range(10):                      # цонх хаагдахыг хүлээнэ (≤1 сек), хаагдмагц үргэлжилнэ
            await asyncio.sleep(0.1)
            if await dialog_count(env) < n:
                break


async def clear_toasts(env: "FlowEngine") -> None:
    """Үлдсэн toast-уудыг хаах товчоор (эсвэл DOM-оос) авна — дараагийн алхамд хуучин анхааруулга саад болохгүй."""
    js = r"""([t, c]) => { let n = 0;
      document.querySelectorAll(t).forEach(x => { const b = x.querySelector(c);
        if (b) { try { b.click(); n++; } catch (e) {} } else { x.remove(); n++; } }); return n; }"""
    s = env.cfg["selectors"]
    try:
        await env.page.evaluate(js, [s["toast"], s["toast_close"]])
    except Exception:
        pass



async def dialog_count(env: "FlowEngine") -> int:
    st = await snapshot(env.page, env.cfg)
    return len(st.dialogs) if st else 0
