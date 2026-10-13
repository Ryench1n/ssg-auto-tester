"""Урсгал бичигч (record → replay): хүн хөтөч дээр гараар хийсэн үйлдлийг бүртгэж YAML урсгал болгоно.

Бичих үед хөтөчид суулгасан скрипт click/change/dblclick үйл явдлыг (capture фаз) барьж, элементийг
DevExpress/SmartApp-ын утгаар нь (цэс, товчны текст, талбарын шошго, lookup-ийн мөр, grid-ийн мөр …)
тайлбарлан `window.__ssgRec`-ээр Python руу дамжуулна. Юу ч дардаггүй, өөрчилдөггүй — зөвхөн ажиглана.

`build_flow()` үйл явдлуудыг ерөнхий `ui.*` алхам болгоно (app/flows/steps/recorded.py). Хэрэглэгчийн
гараар бичсэн текст утгууд `{run_tag} <утга>` болж tag-тай болно — дахин тоглуулах бүрт давтагдашгүй,
засах/устгах алхам зөвхөн тэр (tag-тай) бичлэгт хүрнэ. Өнөөдрийн огноо `{today}` болно.
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Callable

from app.flows.ui import _COMMON_JS

RECORDER_JS = r"""
(() => {
  if (window.__ssgRecInstalled) return;
  window.__ssgRecInstalled = true;
  const sel = /*SEL*/;
  /*__COMMON__*/
  const send = (ev) => { if (window.__ssgRecOn === false) return; try { window.__ssgRec(ev); } catch (e) {} };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const inDialog = (el) => __findDialogs(sel).some(d => d.contains(el));
  const q1 = (el, s) => { try { return el.closest(s); } catch (e) { return null; } };
  // Талбарын шошго: FormLayout caption (for=id эсвэл ижил item) → орчны текст (FIND_FIELD_JS-тэй ижил дараалал)
  const label = (inp) => {
    const item = q1(inp, sel.form_item);
    if (inp.id) { try { const c = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`); if (c && __txt(c)) return clean(__txt(c)).replace(/[\s:*]+$/, ''); } catch (e) {} }
    if (item) { const c = item.querySelector(sel.form_caption); if (c && __txt(c)) return clean(__txt(c)).replace(/[\s:*]+$/, ''); }
    return clean(__fieldLabel(inp)).replace(/[\s:*]+$/, '');
  };
  const btnText = (b) => clean(__txt(b) || b.getAttribute('title') || b.getAttribute('aria-label') || '').split('\n')[0];
  const rowText = (tr) => Array.from(tr.querySelectorAll('td')).map(td => clean(td.innerText)).filter(Boolean).join(' | ');
  const header = (td) => {
    const tr = td.closest('tr'); const table = td.closest('table');
    if (!tr || !table) return '';
    const i = Array.from(tr.children).indexOf(td);
    const hr = table.querySelector('thead tr') || table.querySelector('tr.dxbl-grid-header-row');
    const th = hr ? hr.children[i] : null;
    return th ? clean(__txt(th)).replace(/\s*(No filter applied|Filter applied)\s*$/i, '') : '';
  };
  const isLookupBtn = (b) => {
    const t = __txt(b), meta = ((b.getAttribute('title') || '') + ' ' + (b.getAttribute('aria-label') || '')).toLowerCase();
    return /^(\.\.\.|…)$/.test(t) || /сонго|lookup|browse|…|\.\.\./.test(meta);
  };
  const fieldOfBtn = (b) => {             // "..." / ▾ товчны харьяалах input
    let el = b.parentElement;
    for (let d = 0; d < 4 && el; d++, el = el.parentElement) {
      const i = Array.from(el.querySelectorAll('input, textarea')).find(x => __vis(x) && x.type !== 'checkbox');
      if (i) return i;
    }
    return null;
  };
  const cellOf = (el) => { const td = q1(el, 'td'); return td && q1(td, '.dxbl-grid, [role=grid], [role=treegrid], table') ? td : null; };
  let lookup = null;        // {label, cell: bool, row: ''}
  let combo = '';
  let lastField = '';       // сүүлд идэвхжсэн талбар — lookup товчийг таньж чадаагүй үеийн нөөц
  // Lookup товч: текстээр нь биш БҮТЦЭЭР — дарсан элементийн ойролцоох input-ийн «сонгох» товч мөн эсэх
  // (DevExpress "..." нь дүрс/span байж болно; форм шалгалт ч яг энэ __lookupBtn-ээр таньдаг)
  const lookupInput = (t) => {
    let el = t;
    for (let d = 0; d < 5 && el; d++, el = el.parentElement) {
      const inputs = Array.from(el.querySelectorAll ? el.querySelectorAll('input, textarea') : []).filter(__vis);
      for (const i of inputs) {
        const lb = __lookupBtn(sel, i);
        if (lb && (lb === t || lb.contains(t) || t.contains(lb))) return i;
      }
      if (inputs.length > 2) break;
    }
    return null;
  };
  // Lookup цонхны сонгогдсон (фокуслагдсан) мөр — мөр дарсныг барьж чадаагүй үед DOM-оос уншина
  const selectedRow = (dlg) => {
    if (!dlg) return '';
    const r = Array.from(dlg.querySelectorAll('tr, [role=treeitem], [role=row], .dxbl-treeview-item')).filter(__vis)
      .find(x => /focused|selected|active/.test(typeof x.className === 'string' ? x.className : '')
                 || x.getAttribute('aria-selected') === 'true');
    return r ? (r.matches('tr') ? rowText(r) : clean(__txt(r)).split('\n')[0]) : '';
  };
  const topDialog = (el) => __findDialogs(sel).filter(d => d.contains(el)).pop() || null;
  // Календарь, dropdown, жагсаалтын popup — тэдгээрийн доторх td/tr дарах нь мөр сонголт БИШ
  const inPopup = (el) => !!q1(el, '.dxbl-calendar, [class*=calendar], .dxbl-dropdown, .dxbl-dropdown-body, '
    + '.dxbl-listbox-popup, .dxbl-list-box, .dxbl-popup:not(.dxbl-modal), .dxbl-date-time-edit-dropdown, [role=listbox]');
  // Жагсаалтын (цонхноос гадна) фокуслагдсан мөр — Засах/Устгах дарах үед аль бичлэг байсныг тэмдэглэнэ
  const focusedListRow = () => {
    const r = Array.from(document.querySelectorAll('tr')).filter(__vis).filter(x => !inDialog(x) && x.querySelector('td'))
      .find(x => /focused-row|selected-row/.test(typeof x.className === 'string' ? x.className : '')
                 || x.getAttribute('aria-selected') === 'true');
    return r ? rowText(r) : '';
  };
  const iconOf = (b) => {
    const el = [b, ...b.querySelectorAll('i, span, svg, img')].find(x => {
      const c = typeof x.className === 'string' ? x.className : (x.className && x.className.baseVal) || '';
      return /(?:^|\s)(fa-[\w-]+|dx-icon-[\w-]+|oi-[\w-]+|bi-[\w-]+|dxbl-image[\w-]*|icon-[\w-]+)/.test(c);
    });
    if (!el) return '';
    const c = typeof el.className === 'string' ? el.className : (el.className && el.className.baseVal) || '';
    const m = c.match(/(?:^|\s)(fa-(?!solid|regular|light|fw|lg|sm)[\w-]+|dx-icon-[\w-]+|oi-[\w-]+|bi-[\w-]+|icon-[\w-]+)/);
    return m ? m[1] : '';
  };

  // ---- Бөглөлт: change ирээгүй (Enter-ээр хадгалсан, blur хийгээгүй) утгыг дараагийн үйлдлийн өмнө бүртгэнэ
  const pend = new Map();          // input → сүүлийн утга
  const sent = new WeakMap();      // input → бүртгэсэн утга
  const fillEvent = (i) => {
    const type = (i.getAttribute('type') || '').toLowerCase();
    if (['password', 'file', 'hidden', 'checkbox', 'radio'].includes(type)) return null;   // нууц үг ХЭЗЭЭ Ч
    if (i.tagName === 'SELECT' || q1(i, sel.menu_root) || q1(i, sel.module_item)) return null;
    try { if (sel.grid_search && i.matches(sel.grid_search)) return null; } catch (e) {}
    if (q1(i, sel.combobox) && i.readOnly) return null;
    const td = cellOf(i);
    if (td && q1(td, '.dxbl-grid-filter-row, thead')) return null;
    if (td) return {t: 'cell', column: header(td), value: i.value};
    if (lookup && topDialog(i) && !q1(i, sel.form_item)) return null;      // lookup цонхны хайлт
    const date = /date/i.test(type) || !!q1(i, sel.date_edit || '.slDate, dxbl-date-edit, .dxbl-date-edit');
    return {t: 'fill', field: label(i), value: i.value, date,
            numeric: type === 'number' || /^-?[\d\s.,]+$/.test(i.value || ''), dialog: inDialog(i)};
  };
  const sendFill = (i) => {
    if (sent.get(i) === i.value) return;
    const ev = fillEvent(i);
    if (ev && ev.field !== '' || (ev && ev.t === 'cell')) { sent.set(i, i.value); send(ev); }
  };
  const flush = () => { for (const i of pend.keys()) { if (document.contains(i)) sendFill(i); } pend.clear(); };
  document.addEventListener('input', (e) => {
    const i = e.target;
    if (i && i.matches && i.matches('input, textarea')) pend.set(i, i.value);
  }, true);

  document.addEventListener('focusin', (e) => {
    const i = e.target;
    if (i && i.matches && i.matches('input, textarea') && !(lookup && topDialog(i) && !q1(i, sel.form_item))) {
      const l = label(i); if (l) lastField = l;
    }
  }, true);

  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    flush();
    const leaf = q1(t, sel.leaf);
    if (leaf && q1(leaf, sel.menu_root)) {
      const first = (el) => clean(el.getAttribute('aria-label') || el.getAttribute('title') || __txt(el)).split('\n')[0];
      const folders = []; let p = leaf.parentElement;
      while (p && !p.matches(sel.menu_root)) {
        if (p.getAttribute && p.getAttribute('role') === 'group' && p.previousElementSibling && p.previousElementSibling.matches(sel.folder))
          folders.unshift(first(p.previousElementSibling));
        else if (p.matches && p.matches(sel.folder)) folders.unshift(first(p));
        p = p.parentElement;
      }
      return send({t: 'menu', name: first(leaf), href: leaf.getAttribute('href') || '', folder: folders.join(' / ')});
    }
    if (q1(t, sel.menu_root)) return;                      // хавтас задлах — урсгалд хамаагүй
    const mod = q1(t, sel.module_item);
    if (mod) return send({t: 'module', name: clean(mod.getAttribute('title') || __txt(mod)).split('\n')[0]});
    const opt = q1(t, sel.list_option);
    if (opt && combo) { send({t: 'choose', field: combo, option: clean(__txt(opt))}); combo = ''; return; }
    if (opt) return;                                        // календарь / бусад жагсаалтын popup
    const tab = q1(t, sel.tab);
    if (tab && !q1(t, sel.tab_close)) return send({t: 'tab', name: clean(__txt(tab)).split('\n')[0], dialog: inDialog(tab)});
    if (tab) return;
    const mi = sel.popup_menu_item ? q1(t, sel.popup_menu_item) : null;
    if (mi) return send({t: 'click', text: clean(__txt(mi)), popup: true});      // «Шинэ» dropdown-ы төрөл
    const li = lookupInput(t);
    if (li) {
      const td = cellOf(li);
      lookup = {field: td ? header(td) : (label(li) || lastField), cell: !!td, row: ''};
      return;
    }
    if (inPopup(t)) return;                                 // календарийн өдөр г.м. — input-ийн change бүртгэнэ
    // Мөр доторх «→» сум / холбоос — бичлэгийг нээх
    const arrow = q1(t, '.sllink, [class*=fa-arrow], td a');
    const atr = arrow && q1(arrow, 'tr');
    if (atr && !lookup) return send({t: 'row', text: rowText(atr), open: true, dialog: inDialog(atr)});
    const b = q1(t, 'button, [role=button], .dxbl-btn');
    if (b) {
      if (isLookupBtn(b)) {
        const inp = fieldOfBtn(b); const td = cellOf(b);
        lookup = {field: td ? header(td) : (inp ? label(inp) : lastField), cell: !!td, row: ''};
        return;
      }
      if (q1(b, sel.combobox)) { const inp = fieldOfBtn(b); combo = inp ? label(inp) : ''; return; }
      if (b.matches('.sl-grid-command') || b.querySelector('.fa-plus')) {
        if (!btnText(b) || btnText(b) === '+') return send({t: 'line_add'});
      }
      if (q1(b, '.dxbl-grid-tree-node-expand-button, [class*=expand-button]')) return;   // мод задлах
      // Огнооны талбарын календарь (▾) — сонгосон огноо нь input-ийн change-ээр бүртгэгдэнэ
      if (q1(b, sel.date_edit || '.slDate, dxbl-date-edit, .dxbl-date-edit')) return;
      const text = btnText(b);
      const dlg = inDialog(b);
      if (dlg && /^(сонгох|select)$/i.test(text)) {
        // Цонхны «Сонгох» = lookup-ийн сонголт (форм дээр ийм товч байдаггүй)
        const lk = lookup || {field: lastField, cell: false, row: ''};
        if (!lk.row) lk.row = selectedRow(topDialog(b));
        send({t: 'pick', field: lk.field, cell: lk.cell, row: lk.row}); lookup = null; return;
      }
      if (lookup && dlg) {
        if (/^ok$/i.test(text) && lookup.row) { send({t: 'pick', field: lookup.field, cell: lookup.cell, row: lookup.row}); lookup = null; return; }
        if (/^(болих|хаах|cancel|close|×|✕)$/i.test(text) || q1(b, '.dxbl-popup-header-button')) { lookup = null; return; }
      }
      const icon = text ? '' : iconOf(b);
      if (!text && !icon) return;
      return send({t: 'click', text, icon, dialog: dlg, focus: dlg ? '' : focusedListRow()});
    }
    const inp = q1(t, 'input');
    if (inp && q1(inp, sel.combobox)) { combo = label(inp); return; }
    if (q1(t, 'input, textarea, select, label')) return;
    const node = q1(t, '[role=treeitem], .dxbl-treeview-item');
    if (lookup && node && inDialog(node) && !q1(t, 'tr')) { lookup.row = clean(__txt(node)).split('\n')[0]; return; }
    const tr = q1(t, 'tr');
    if (tr && tr.querySelector('td') && !q1(t, 'thead')) {
      const txt = rowText(tr);
      if (!txt) return;
      if (lookup && inDialog(tr)) { lookup.row = txt; return; }
      if (cellOf(t) && q1(tr, '.dxbl-grid-edit-row, [class*=edit-row], [class*=new-item]')) return;   // мөрийн grid-ийн засварлах мөр
      return send({t: 'row', text: txt, dialog: inDialog(tr)});
    }
    // Товч биш дарагддаг элемент (холбоос, toolbar/menu item)
    const g = q1(t, 'a[href], [role=menuitem], [role=link], .dxbl-toolbar-item, .dxbl-menu-item');
    if (g) { const text = clean(__txt(g)).split('\n')[0]; if (text) return send({t: 'click', text, any: true, dialog: inDialog(g)}); }
  }, true);
  document.addEventListener('dblclick', (e) => {
    const tr = e.target && e.target.closest && e.target.closest('tr');
    if (!tr || !tr.querySelector('td') || inPopup(tr)) return;
    if (lookup && inDialog(tr)) { send({t: 'pick', field: lookup.field, cell: lookup.cell, row: rowText(tr)}); lookup = null; return; }
    send({t: 'row', text: rowText(tr), open: true, dialog: inDialog(tr)});
  }, true);
  // Хайлтын талбарт Enter — жагсаалт дахин зурагдаж input солигдох тул change ирэхгүй байж болно
  document.addEventListener('keydown', (e) => {
    const i = e.target;
    if (e.key !== 'Enter' || !i || !i.matches) return;
    let search = false;
    try { search = !!(sel.grid_search && i.matches(sel.grid_search)); } catch (x) {}
    if (search) {
      if (lookup && inDialog(i)) return;            // lookup цонхны хайлт — сонголтод (pick) шингэнэ
      return send({t: 'search', text: i.value, dialog: inDialog(i)});
    }
    if (i.matches('input') && !cellOf(i)) {
      // Формын талбарт Enter — утгыг бүртгээд Enter-ийг өөрөө үйлдэл болгоно (ж: Enter-ээр хадгалдаг форм)
      pend.delete(i); sendFill(i);
      const l = label(i);
      if (l) send({t: 'key', key: 'Enter', field: l});
    }
  }, true);
  document.addEventListener('change', (e) => {
    const i = e.target;
    if (!i || !i.matches || !i.matches('input, textarea, select')) return;
    const type = (i.getAttribute('type') || '').toLowerCase();
    if (['password', 'file', 'hidden'].includes(type)) return;      // нууц үг ХЭЗЭЭ Ч бүртгэхгүй
    if (q1(i, sel.menu_root) || q1(i, sel.module_item)) return;
    try {
      if (sel.grid_search && i.matches(sel.grid_search)) {
        if (lookup && inDialog(i)) return;
        return send({t: 'search', text: i.value, dialog: inDialog(i)});
      }
    } catch (x) {}
    const td = cellOf(i);
    if ((type === 'checkbox' || type === 'radio') && td && !q1(td, 'thead')) {
      const tr = q1(td, 'tr');
      if (tr && q1(tr, '.dxbl-grid-edit-row, [class*=edit-row]')) return send({t: 'cell', column: header(td), value: i.checked ? 'true' : 'false'});
      return send({t: 'rowcheck', text: tr ? rowText(tr) : '', value: !!i.checked, dialog: inDialog(i)});
    }
    if (type === 'checkbox' || type === 'radio') return send({t: 'check', field: label(i), value: !!i.checked});
    if (i.tagName === 'SELECT') return send({t: 'choose', field: label(i), option: clean(i.options[i.selectedIndex] ? i.options[i.selectedIndex].text : '')});
    pend.delete(i);
    sendFill(i);
  }, true);
})();
"""


def recorder_script(cfg: dict) -> str:
    s = cfg["selectors"]
    fs = (cfg.get("flow") or {}).get("selectors") or {}
    sel = {**fs, "dialog": s["dialog"], "menu_root": s["menu_root"], "leaf": s["leaf"], "folder": s["folder"],
           "module_item": s["module_item"], "tab": s["tab"], "tab_close": s["tab_close"]}
    return RECORDER_JS.replace("/*SEL*/", json.dumps(sel, ensure_ascii=False)).replace("/*__COMMON__*/", _COMMON_JS)


def describe(ev: dict) -> str:
    """UI-ийн шууд жагсаалтад харагдах товч тайлбар."""
    t = ev.get("t")
    return {
        "menu": lambda: f"Цэс: {ev.get('folder') + ' / ' if ev.get('folder') else ''}{ev.get('name')}",
        "module": lambda: f"Модуль: {ev.get('name')}",
        "click": lambda: f"Товч: «{ev.get('text') or ev.get('icon')}»" + (" (цонхонд)" if ev.get("dialog") else ""),
        "key": lambda: f"Товчлуур: {ev.get('key')} («{ev.get('field')}»)",
        "rowcheck": lambda: f"Мөр тэмдэглэх: {str(ev.get('text'))[:60]}",
        "fill": lambda: f"Бөглөх: {ev.get('field')} = {ev.get('value')}",
        "check": lambda: f"Тэмдэглэх: {ev.get('field')} = {'✓' if ev.get('value') else '✗'}",
        "choose": lambda: f"Сонгох: {ev.get('field')} = {ev.get('option')}",
        "pick": lambda: f"Lookup: {ev.get('field')} ← {str(ev.get('row'))[:60]}",
        "row": lambda: f"Мөр{' нээх' if ev.get('open') else ''}: {str(ev.get('text'))[:60]}",
        "search": lambda: f"Хайх: {ev.get('text')}",
        "tab": lambda: f"Таб: {ev.get('name')}",
        "line_add": lambda: "Мөр нэмэх (+)",
        "cell": lambda: f"Мөрийн нүд: {ev.get('column')} = {ev.get('value')}",
    }.get(t, lambda: str(ev))()


class Recorder:
    """Нэг page дээрх бичлэг. start() → (хэрэглэгч үйлдэл хийнэ) → stop() → build_flow(events)."""

    def __init__(self, cfg: dict, on_event: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.events: list[dict] = []
        self.on_event = on_event
        self._installed = False

    def _handle(self, _source, ev) -> None:
        if not isinstance(ev, dict) or not ev.get("t"):
            return
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev)

    async def start(self, page) -> None:
        script = recorder_script(self.cfg)
        if not self._installed:
            await page.expose_binding("__ssgRec", self._handle)
            await page.context.add_init_script(script)       # reload/навигацийн дараа ч бичлэг үргэлжилнэ
            self._installed = True
        await page.evaluate("() => { window.__ssgRecOn = true; }")
        await page.evaluate(script)

    async def stop(self, page) -> None:
        try:
            await page.evaluate("() => { window.__ssgRecOn = false; }")
        except Exception:
            pass


# ------------------------------------------------------------------------------------------- YAML
_CANCEL = {"болих", "хаах", "cancel", "close", "×", "✕", "үгүй", "no"}


def _today_forms() -> set[str]:
    d = datetime.date.today()
    return {d.strftime(f) for f in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y")}


def _esc(s: str) -> str:
    return str(s).replace("{", "{{").replace("}", "}}")


def build_flow(events: list[dict], description: str = "") -> tuple[dict, list[str]]:
    """Бүртгэсэн үйл явдлуудаас урсгалын spec ({"steps": [...]}) ба анхааруулгууд."""
    warnings: list[str] = []
    today = _today_forms()
    typed: dict[str, str] = {}        # хэрэглэгчийн бичсэн утга → {run_tag} хэлбэр
    module = ""
    raw: list[dict] = []
    for ev in events:
        t = ev.get("t")
        if t == "module":
            module = ev.get("name") or module
            continue
        if t == "fill" and raw and raw[-1].get("t") == "fill" and raw[-1].get("field") == ev.get("field"):
            raw[-1] = ev                  # нэг талбарт дараалан бичсэн — сүүлчийн утга
            continue
        if t == "search" and raw and raw[-1].get("t") == "search" and raw[-1].get("text") == ev.get("text"):
            continue                      # Enter + change — нэг хайлт
        if t == "cell" and raw and raw[-1].get("t") == "cell" and raw[-1].get("column") == ev.get("column"):
            raw[-1] = ev
            continue
        raw.append({**ev, "_module": module})

    def tag_value(ev: dict, key: str) -> str:
        v = str(ev.get(key) or "")
        if not v.strip():
            return v
        if ev.get("date") or v.strip() in today:
            return "{today}" if v.strip() in today else _esc(v)
        if re.search(r"код|code|дугаар", str(ev.get("field") or ev.get("column") or ""), re.I)                 and not ev.get("date"):
            # Давтагдашгүй байх ёстой талбар (Код) — тоо байсан ч run бүрт шинэ: {run_tag}<утга>
            tagged = "{run_tag}" + _esc(v.strip())
            typed[v.strip()] = tagged
            return tagged
        if ev.get("numeric"):
            return _esc(v)
        tagged = "{run_tag} " + _esc(v.strip())
        typed[v.strip()] = tagged
        return tagged

    def cells_of(row: str) -> list[str]:
        # бичлэг нээх «→» сум, checkbox мэт тэмдэгтийг хасна
        return [c for c in (re.sub(r"^[^\w]+", "", c).strip() for c in row.split(" | ")) if c]

    def own(text: str) -> str | None:
        """Хэрэглэгчийн ЭНЭ урсгалд бичсэн утга (tag-тай хэлбэр). Богино утга («1») бодит өгөгдөлтэй
        санамсаргүй давхцдаг (ж: lookup-ын «1 | Бүх» мөр) тул ≥3 тэмдэгт ба нүдтэй ЯГ тэнцүү үед л."""
        return next((typed[o] for o in sorted(typed, key=len, reverse=True)
                     if len(o) >= 3 and o == text.strip()), None)

    def retag(text: str) -> str:
        return own(text) or _esc(text)

    def find_text(row: str) -> str:
        """Жагсаалтын мөрийг дахин олох текст: хэрэглэгчийн бичсэн утгатай нүд байвал түүнийг (tag-тай),
        үгүй бол эхний утгатай нүд (ихэвчлэн код)."""
        cells = cells_of(row)
        for c in cells:
            if own(c):
                return own(c)
        return _esc(cells[0] if cells else row)

    def lookup_text(row: str) -> str:
        """Lookup-ын мөр: бодит (байгаа) өгөгдөл — бүх нүдийг хоосон зайгаар (кодоор давхцахаас сэргийлнэ).
        Энэ урсгалд бүртгэсэн бичлэгийг сонгосон бол tag-тай хэлбэр."""
        cells = cells_of(row)
        for c in cells:
            if own(c):
                return own(c)
        return _esc(" ".join(cells))

    steps: list[dict] = []
    for ev in raw:
        t = ev["t"]
        if t == "menu":
            key = f"{ev.get('_module', '')}|{ev.get('folder', '')}|{ev.get('name', '')}|{ev.get('href', '')}"
            steps.append({"uses": "ui.menu", "name": f"«{ev.get('name')}» цэс нээх", "with": {"menu": key}})
        elif t == "click":
            text, icon = ev.get("text") or "", ev.get("icon") or ""
            scope = "popup" if ev.get("popup") else ("dialog" if ev.get("dialog") else "page")
            w = {"button": _esc(text), "scope": scope}
            if icon and not text:
                w = {"icon": icon, "scope": scope}
            if ev.get("any"):
                w["any"] = True
            kind = button_kind(text or icon)
            # Засах/Устгах нь ФОКУСЛАГДСАН мөр дээр ажилладаг: бичих үед мөр дараагүй (ж: хайсны дараа шууд
            # «Устгах») бол тэр үеийн фокуслагдсан мөрийг тоглуулахдаа яг тэр мөрийг сонгоно
            if kind in ("edit", "delete") and ev.get("focus") and not (steps and steps[-1]["uses"] == "ui.row"):
                steps.append({"uses": "ui.row", "name": "Мөр сонгох (бичих үеийн фокус)",
                              "with": {"text": find_text(str(ev["focus"])), "open": False}})
            steps.append({"uses": "ui.click", "name": f"«{text or icon}» дарах", "with": w})
        elif t == "fill":
            steps.append({"uses": "ui.fill", "name": f"«{ev.get('field')}» бөглөх",
                          "with": {"field": ev.get("field"), "value": tag_value(ev, "value")}})
        elif t == "check":
            steps.append({"uses": "ui.check", "name": f"«{ev.get('field')}» тэмдэглэх",
                          "with": {"field": ev.get("field"), "value": bool(ev.get("value"))}})
        elif t == "choose":
            steps.append({"uses": "ui.choose", "name": f"«{ev.get('field')}» сонгох",
                          "with": {"field": ev.get("field"), "option": _esc(ev.get("option") or "")}})
        elif t == "pick":
            row = str(ev.get("row") or "")
            w = {"field": ev.get("field"), "row": lookup_text(row) if row else ""}
            if not ev.get("field"):
                warnings.append("Lookup-ийн талбарыг тодорхойлж чадсангүй — YAML-д «field»-ийг гараар бичнэ үү")
            if ev.get("cell"):
                steps.append({"uses": "ui.cell_pick", "name": f"Мөрийн «{ev.get('field')}» сонгох", "with": w})
            else:
                steps.append({"uses": "ui.pick", "name": f"«{ev.get('field')}» lookup-оос сонгох", "with": w})
        elif t == "row":
            w = {"text": find_text(str(ev.get("text") or "")), "open": bool(ev.get("open"))}
            if ev.get("dialog"):
                w["dialog"] = True
            if steps and steps[-1]["uses"] == "ui.row" and steps[-1]["with"] == {**w, "open": False} and w["open"]:
                steps.pop()                    # дарж сонгоод давхар дарсан — нэг «нээх» алхам
            steps.append({"uses": "ui.row", "name": "Мөр " + ("нээх" if ev.get("open") else "сонгох"), "with": w})
        elif t == "rowcheck":
            w = {"text": find_text(str(ev.get("text") or "")), "value": bool(ev.get("value"))}
            if ev.get("dialog"):
                w["dialog"] = True
            steps.append({"uses": "ui.row_check", "name": "Мөрийг тэмдэглэх", "with": w})
        elif t == "search":
            w = {"text": retag(str(ev.get("text") or ""))}
            if ev.get("dialog"):
                w["dialog"] = True
            steps.append({"uses": "ui.search", "name": "Хайх", "with": w})
        elif t == "tab":
            steps.append({"uses": "ui.tab", "name": f"«{ev.get('name')}» таб", "with": {"name": ev.get("name")}})
        elif t == "key":
            steps.append({"uses": "ui.key", "name": f"«{ev.get('field')}» дээр {ev.get('key')}",
                          "with": {"field": ev.get("field"), "key": ev.get("key") or "Enter"}})
        elif t == "line_add":
            steps.append({"uses": "ui.line_add", "name": "Мөр нэмэх (+)", "with": {}})
        elif t == "cell":
            steps.append({"uses": "ui.cell", "name": f"Мөрийн «{ev.get('column')}» бөглөх",
                          "with": {"column": ev.get("column"), "value": tag_value(ev, "value")}})
    # Цонхыг хаах/болих (хадгалаагүй) дарсан нь урсгалд утгагүй биш — үлдээнэ. Хоосон урсгал:
    if not steps:
        warnings.append("Нэг ч үйлдэл бүртгэгдсэнгүй")
    if not any(s["uses"] == "ui.menu" for s in steps):
        warnings.append("Цэс нээх алхам алга — дахин тоглуулахад одоо нээлттэй таб дээрээс эхэлнэ")
    elif steps[0]["uses"] != "ui.menu":
        warnings.append("Эхний алхам цэс нээх биш — бичиж эхлэхээс өмнө нээлттэй байсан табаас хамаарна")
    kinds = [button_kind(s["with"].get("button") or s["with"].get("icon") or "")
             for s in steps if s["uses"] == "ui.click"]
    saves, deletes = [k for k in kinds if k == "save"], [k for k in kinds if k == "delete"]
    if saves and not deletes:
        warnings.append("Хадгалсан боловч устгах алхам алга — run бүрт {run_tag}-тай тестийн өгөгдөл үлдэнэ")
    if any("{run_tag}" not in str(s["with"].get("text", "{run_tag}")) for s in steps if s["uses"] == "ui.row"):
        warnings.append("Зарим мөрийн сонголт таны бичсэн утгыг агуулаагүй (бодит бичлэг) — тэр мөр дээр "
                        "Хадгалах/Засах/Устгах дарвал аюулгүй байдлын үүднээс зогсооно")
    spec = {"description": description or f"Бичсэн урсгал ({len(steps)} алхам)", "recorded": True,
            "steps": steps}
    return spec, warnings


_KIND_WORDS = {
    "save": ["хадгалах", "save", "батлах", "илгээх", "floppy", "fa-check"],
    "delete": ["устгах", "delete", "trash", "remove", "fa-times", "fa-xmark"],
    "edit": ["засах", "edit", "pencil", "pen-to-square"],
    "add": ["нэмэх", "шинэ", "add", "new", "fa-plus", "fa-file"],
    "confirm": ["тийм", "yes"],
}


def _is_kind(text: str, kind: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in _KIND_WORDS[kind])


def is_cancel(text: str) -> bool:
    return (text or "").strip().lower() in _CANCEL


def button_kind(text: str) -> str:
    for k in ("delete", "save", "edit", "confirm", "add"):
        if _is_kind(text, k):
            return k
    return ""


def slug(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()[:80]
