"""Flow алхмуудад зориулсан DevExpress Blazor UI туслахууд: цэс нээх, форм бөглөх, товч, grid.

Бүх selector/товчны нэр config-ийн `flow.selectors` / `flow.buttons`-оос ирнэ — сайт бүрт
config/sites/<host>.yaml-аар тааруулна.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from app import models as M
from app.flows.context import StepBlocked, StepFailed
from app.models import Finding, MenuItem
from app.runner import discovery
from app.runner.detectors import DIALOGS_FN, dialog_text, snapshot, tab_is

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine

_COMMON_JS = DIALOGS_FN + r"""
const __vis = (el) => {
  if (!el) return false;
  const s = getComputedStyle(el);
  if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
};
const __norm = (s) => (s || '').replace(/\s+/g, ' ').replace(/[\s:*]+$/, '').trim().toLowerCase();
const __txt = (el) => (el.innerText || el.textContent || '').trim();
// Хамгийн сүүлд нээгдсэн (дээд давхаргын) цонх, байхгүй бол бүх хуудас
const __scope = (sel, mode) => {
  const dl = __findDialogs(sel).filter(d => __txt(d));
  if (mode === 'page' || !dl.length) return mode === 'dialog' ? null : document;
  return dl[dl.length - 1];
};
// Формын талбар биш input (цэс, таб, grid-ийн шүүлтүүр, хайлт)
const __skipInput = (sel, i) => {
  try { if (sel.input_exclude && i.closest(sel.input_exclude)) return true; } catch (e) {}
  try { if (sel.grid_search && i.matches(sel.grid_search)) return true; } catch (e) {}
  return false;
};
// Lookup талбар: input-ийн дэргэдэх "..." (сонгох) товч — ≤3 өвгийн дотор, мөр бүхэлдээ биш
const __lookupBtn = (sel, i) => {
  let el = i.parentElement;
  for (let d = 0; d < 3 && el; d++, el = el.parentElement) {
    if (Array.from(el.querySelectorAll('input, textarea, select')).filter(__vis).length > 2) break;
    const b = Array.from(el.querySelectorAll('button, [role=button], .dxbl-btn')).filter(__vis).find(x => {
      const t = __txt(x), meta = ((x.getAttribute('title') || '') + ' ' + (x.getAttribute('aria-label') || '')).toLowerCase();
      return /^(\.\.\.|…)$/.test(t) || /сонго|lookup|browse|select|…|\.\.\./.test(meta);
    });
    if (b) return b;
  }
  return null;
};
// Огнооны талбар (DevExpress DateEdit / SmartApp slDate) — dropdown товчтой ч сонголтын талбар биш
const __isDate = (sel, i) => {
  if (['date', 'datetime-local'].includes((i.getAttribute('type') || '').toLowerCase())) return true;
  try { return !!i.closest(sel.date_edit || '.slDate, dxbl-date-edit, .dxbl-date-edit'); } catch (e) { return false; }
};
// FormLayout caption-гүй формд талбарын шошгыг таах: aria-label/labelledby, placeholder, label[for],
// эсвэл input-ийн (≤4 өвгийн) өмнөх ах дүү элементийн богино текст
const __fieldLabel = (i) => {
  const lb = i.getAttribute('aria-labelledby');
  if (lb) { const t = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(__txt).join(' ').trim();
            if (t) return t; }
  const a = i.getAttribute('aria-label') || i.getAttribute('placeholder');
  if (a && a.trim()) return a.trim();
  if (i.id) { try { const l = document.querySelector(`label[for="${CSS.escape(i.id)}"]`); if (l && __txt(l)) return __txt(l); } catch (e) {} }
  let el = i;
  for (let d = 0; d < 4 && el; d++) {
    let p = el.previousElementSibling;
    while (p) {
      const t = __txt(p);
      if (t) { if (t.length <= 40 && !p.querySelector('input, textarea, select')) return t.split('\n')[0].trim(); break; }
      p = p.previousElementSibling;
    }
    el = el.parentElement;
  }
  return '';
};
"""

FIND_FIELD_JS = r"""
([sel, label]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-field]').forEach(e => e.removeAttribute('data-ssg-field'));
  const root = __scope(sel, 'auto');
  const want = __norm(label);
  const caps = Array.from(root.querySelectorAll(sel.form_caption)).filter(__vis);
  const cap = caps.find(c => __norm(__txt(c)) === want) || caps.find(c => __norm(__txt(c)).startsWith(want));
  let input = null;
  if (cap) {
    const f = cap.getAttribute('for');
    if (f) input = document.getElementById(f);
    if (!__vis(input)) {
      const item = cap.closest(sel.form_item) || cap.parentElement;
      input = Array.from(item.querySelectorAll(sel.field_input)).find(__vis) || null;
    }
  }
  if (!input) {
    input = Array.from(root.querySelectorAll(sel.field_input)).filter(__vis).find(i =>
      ['placeholder', 'aria-label', 'name'].some(a => __norm(i.getAttribute(a)) === want)) || null;
  }
  if (!input) {   // caption-гүй форм: шошгыг input-ийн орчноос таана
    input = Array.from(root.querySelectorAll(sel.field_input)).filter(__vis)
      .filter(i => !__skipInput(sel, i)).find(i => __norm(__fieldLabel(i)) === want) || null;
  }
  if (!input) return null;
  input.setAttribute('data-ssg-field', '1');
  document.querySelectorAll('[data-ssg-lookup]').forEach(e => e.removeAttribute('data-ssg-lookup'));
  const lb = __lookupBtn(sel, input);
  if (lb) lb.setAttribute('data-ssg-lookup', '1');
  const date = __isDate(sel, input);
  const combo = !date && (input.getAttribute('role') === 'combobox' || !!input.closest(sel.combobox));
  return {tag: input.tagName.toLowerCase(), type: (input.getAttribute('type') || '').toLowerCase(),
          combo, date, lookup: !date && !!lb, value: input.value == null ? '' : String(input.value),
          readonly: !!(input.readOnly || input.disabled),
          options: input.tagName === 'SELECT' ? input.options.length : null};
}
""".replace("/*__COMMON__*/", _COMMON_JS)

FIND_BUTTON_JS = r"""
([sel, texts, mode]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-btn]').forEach(e => e.removeAttribute('data-ssg-btn'));
  const root = __scope(sel, mode);
  if (!root) return null;
  const want = texts.map(__norm);
  const dialogs = root === document ? __findDialogs(sel) : [];
  const btns = Array.from(root.querySelectorAll(sel.button)).filter(__vis)
      .filter(b => !dialogs.some(d => d.contains(b)))   // хуудсан дээр хайхад цонхны товч орохгүй
      .filter(b => !b.disabled && b.getAttribute('aria-disabled') !== 'true');
  for (const w of want) {
    const b = btns.find(x => [__txt(x), x.getAttribute('title'), x.getAttribute('aria-label')]
                              .some(t => __norm(t) === w));
    if (b) { b.setAttribute('data-ssg-btn', '1'); return __txt(b) || b.getAttribute('title') || w; }
  }
  return null;
}
""".replace("/*__COMMON__*/", _COMMON_JS)

FIND_ROWS_JS = r"""
([sel, text]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-row]').forEach(e => e.removeAttribute('data-ssg-row'));
  const want = __norm(text);
  const rows = Array.from(document.querySelectorAll(sel.grid_row)).filter(__vis)
      .filter(r => !__findDialogs(sel).some(d => d.contains(r)))
      .filter(r => __norm(__txt(r)).includes(want));
  rows.forEach((r, i) => r.setAttribute('data-ssg-row', String(i)));
  return rows.map(r => __txt(r).replace(/\s+/g, ' ').slice(0, 200));
}
""".replace("/*__COMMON__*/", _COMMON_JS)

MARK_SEARCH_JS = r"""
([sel]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-search]').forEach(e => e.removeAttribute('data-ssg-search'));
  const el = Array.from(document.querySelectorAll(sel.grid_search)).filter(__vis)
      .find(i => !__findDialogs(sel).some(d => d.contains(i)));
  if (!el) return false;
  el.setAttribute('data-ssg-search', '1');
  return true;
}
""".replace("/*__COMMON__*/", _COMMON_JS)

CLICK_OPTION_JS = r"""
([sel, text]) => {
  /*__COMMON__*/
  const want = __norm(text);
  const opts = Array.from(document.querySelectorAll(sel.list_option)).filter(__vis);
  const o = opts.find(x => __norm(__txt(x)) === want) || opts.find(x => __norm(__txt(x)).includes(want));
  if (!o) return false;
  o.click();
  return true;
}
""".replace("/*__COMMON__*/", _COMMON_JS)


SCAN_FORM_JS = r"""
([sel]) => {
  /*__COMMON__*/
  const root = __scope(sel, 'auto');
  const caps = Array.from(root.querySelectorAll(sel.form_caption)).filter(__vis);
  const out = []; const used = new Set();
  for (const cap of caps) {
    const raw = __txt(cap);
    const label = raw.replace(/\s+/g, ' ').replace(/[\s:*]+$/, '').trim();
    if (!label || out.some(o => o.label === label)) continue;
    let input = null;
    const f = cap.getAttribute('for');
    if (f) input = document.getElementById(f);
    const item = cap.closest(sel.form_item) || cap.parentElement;
    if (!__vis(input)) input = Array.from(item.querySelectorAll(sel.field_input)).find(__vis) || null;
    if (!input || used.has(input) || __skipInput(sel, input)) continue;
    used.add(input);
    const cls = ((item && item.className) || '') + ' ' + (cap.className || '');
    const date = __isDate(sel, input);
    out.push({label, tag: input.tagName.toLowerCase(), type: (input.getAttribute('type') || '').toLowerCase(),
              combo: !date && (input.getAttribute('role') === 'combobox' || !!input.closest(sel.combobox)),
              date, lookup: !date && !!__lookupBtn(sel, input),
              readonly: !!(input.readOnly || input.disabled),
              required: /\*\s*:?\s*$/.test(raw) || input.required || input.getAttribute('aria-required') === 'true'
                        || /required/i.test(cls),
              value: input.value == null ? '' : String(input.value),
              options: input.tagName === 'SELECT' ? input.options.length : null, source: 'caption', _el: input});
  }
  // Caption-д холбогдоогүй талбарууд (FormLayout-гүй форм): шошгыг орчноос таана
  for (const input of Array.from(root.querySelectorAll(sel.field_input)).filter(__vis)) {
    if (used.has(input) || __skipInput(sel, input)) continue;
    const t = (input.getAttribute('type') || '').toLowerCase();
    if (['button', 'submit', 'reset', 'image', 'file'].includes(t)) continue;
    const raw = __fieldLabel(input);
    const label = raw.replace(/\s+/g, ' ').replace(/[\s:*]+$/, '').trim();
    if (!label || out.some(o => o.label === label)) continue;
    used.add(input);
    const date = __isDate(sel, input);
    out.push({label, tag: input.tagName.toLowerCase(), type: t,
              combo: !date && (input.getAttribute('role') === 'combobox' || !!input.closest(sel.combobox)),
              date, lookup: !date && !!__lookupBtn(sel, input),
              readonly: !!(input.readOnly || input.disabled),
              required: /\*\s*:?\s*$/.test(raw) || input.required || input.getAttribute('aria-required') === 'true',
              value: input.value == null ? '' : String(input.value),
              options: input.tagName === 'SELECT' ? input.options.length : null, source: 'nearby', _el: input});
  }
  // Формд харагдах дарааллаар (хамааралтай lookup: ж. Дансны төрөл → Дансны бүлэг)
  out.sort((a, b) => (a._el.compareDocumentPosition(b._el) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  return out.map(({_el, ...rest}) => rest);
}
""".replace("/*__COMMON__*/", _COMMON_JS)

# Lookup цонхны (дээд давхарга) эхний өгөгдлийн мөр; хоосон ("Бичлэг олдсонгүй") бол '', ачаалж байвал null
MARK_LOOKUP_ROW_JS = r"""
([sel, want]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-lkrow], [data-ssg-lkcell]').forEach(e => {
    e.removeAttribute('data-ssg-lkrow'); e.removeAttribute('data-ssg-lkcell'); });
  const root = __scope(sel, 'dialog');
  if (!root) return null;
  const empty = /бичлэг олдсонгүй|no data|no records/i;
  const cls = (r) => (typeof r.className === 'string' ? r.className : '');
  // Ачаалж байх үед ("Түр хүлээнэ үү…") хоосон мэт харагдана — дуустал хүлээнэ (null → дахин оролдоно)
  if (/түр хүлээнэ үү|please wait|loading\.\.\./i.test(__txt(root))) return null;
  // SmartApp lookup: зүүн талд групп мод (dxbl-tree-list) + баруун талд өгөгдлийн dxbl-grid. Модыг алгасна.
  const isTree = (el) => !!el.closest('dxbl-tree-list');  // энгийн DxGrid ч role=treegrid тул үүгээр л
  const cand = new Set([...root.querySelectorAll(sel.grid_row), ...root.querySelectorAll('tr[data-visible-index]')]);
  const rows = Array.from(cand).filter(__vis).filter(r => !isTree(r))
    .filter(r => !r.closest('thead, tfoot') && !r.querySelector('th, [role=columnheader]'))
    .filter(r => !/(footer|group|filter|empty|header|summary|pager|edit)/i.test(cls(r)))
    .filter(r => !(r.hasAttribute('data-visible-index') && +r.getAttribute('data-visible-index') < 0))
    .filter(r => { const t = __txt(r).replace(/\s+/g, ' ').trim();
                   return t && !empty.test(t) && /[^\d\s.,\-]/.test(t); });
  let row = null;
  if (want) {
    // Бичсэн урсгал: тодорхой мөрийг (текстээр) сонгоно — олдохгүй бол '' (сонгохгүй)
    const w = __norm(want);
    const all = rows.concat(Array.from(cand).filter(__vis).filter(isTree));
    const rt = (r) => __norm(__txt(r)).replace(/^[^\p{L}\p{N}]+/u, '');
    row = all.find(r => rt(r) === w) || all.find(r => rt(r).startsWith(w + ' '))
       || all.find(r => rt(r).includes(w)) || null;
    if (!row) return '';
  } else if (rows.length) {
    // Хамгийн олон мөртэй grid → түүний фокуслагдсан (сонгогдсон) мөр, эсвэл эхний мөр
    const byGrid = new Map();
    for (const r of rows) { const g = r.closest('dxbl-grid, .dxbl-grid, table') || root;
                            if (!byGrid.has(g)) byGrid.set(g, []); byGrid.get(g).push(r); }
    const grid = [...byGrid.values()].sort((a, b) => b.length - a.length)[0];
    row = grid.find(r => /focused-row|selected-row/.test(cls(r)) || r.getAttribute('aria-selected') === 'true') || grid[0];
  } else {
    // Зөвхөн модтой lookup (Дансны бүлэг, Загварын бүлэг, Барааны бүлэг): «Бүх …» үндсэн зангилааг биш,
    // эхний НАВЧ зангилааг (expand товчгүй) сонгоно
    const tree = Array.from(cand).filter(__vis).filter(isTree)
      .filter(r => !r.closest('thead, tfoot') && !/(empty|footer|header|filter)/i.test(cls(r)))
      .filter(r => { const t = __txt(r).replace(/\s+/g, ' ').trim(); return t && !empty.test(t); })
      .filter(r => !(r.hasAttribute('data-visible-index') && +r.getAttribute('data-visible-index') < 0));
    const leaves = tree.filter(r => !r.querySelector('.dxbl-grid-tree-node-expand-button'));
    row = leaves[0] || tree[1] || null;
  }
  if (!row) return empty.test(__txt(root)) ? '' : null;
  row.setAttribute('data-ssg-lkrow', '1');
  // Дарах нүд: бичлэг НЭЭДЭГ сум/холбоосгүй (i.sllink, a) нүд
  const cells = Array.from(row.querySelectorAll('td')).filter(__vis);
  const cell = cells.find(td => __txt(td) && !td.querySelector('.sllink, a, [class*=fa-arrow], button')) || cells[cells.length - 1];
  if (cell) cell.setAttribute('data-ssg-lkcell', '1');
  return __txt(row).replace(/\s+/g, ' ').trim().slice(0, 120);
}
""".replace("/*__COMMON__*/", _COMMON_JS)

# ---- Баримтын мөрийн (inline edit) grid ------------------------------------------------------------
# Формын дотор "+" (fa-plus) командтай grid → мөр нэмэх товч, баганын толгойнууд
MARK_LINES_ADD_JS = r"""
([sel]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-lineadd], [data-ssg-linegrid]').forEach(e => {
    e.removeAttribute('data-ssg-lineadd'); e.removeAttribute('data-ssg-linegrid'); });
  const root = __scope(sel, 'dialog') || document;
  const grids = Array.from(root.querySelectorAll('dxbl-grid, .dxbl-grid')).filter(__vis)
    .filter(g => g.tagName !== 'DXBL-TREE-LIST' && !g.closest('dxbl-tree-list'));
  for (const g of grids) {
    const btns = Array.from(g.querySelectorAll('button, [role=button]')).filter(__vis);
    const btn = btns.find(b => b.querySelector('.fa-plus, [class*=fa-plus]'))
      || btns.find(b => /^\+$|^\+\s*шинэ$|^мөр нэмэх$/i.test(__txt(b).trim()))
      || btns.find(b => /^(add|new|нэмэх|мөр нэмэх)$/i.test((b.getAttribute('title') || b.getAttribute('aria-label') || '').trim()));
    if (!btn) continue;
    btn.setAttribute('data-ssg-lineadd', '1');
    g.setAttribute('data-ssg-linegrid', '1');
    const hdr = g.querySelector('tr.dxbl-grid-header-row') || g.querySelector('thead tr');
    const headers = hdr ? Array.from(hdr.children).map(th => (th.innerText || th.textContent || '')
        .replace(/No filter applied|Filter applied/gi, '').replace(/\s+/g, ' ').trim()) : [];
    return {headers, rows: g.querySelectorAll('tr[data-visible-index]').length};
  }
  return null;
}
""".replace("/*__COMMON__*/", _COMMON_JS)

# Засварлаж буй (шинэ) мөрийн нүднүүд — баганын индексээр (толгойтой тааруулна)
EDIT_ROW_JS = r"""
([sel, hint]) => {
  /*__COMMON__*/
  const g = document.querySelector('[data-ssg-linegrid="1"]');
  if (!g) return null;
  document.querySelectorAll('[data-ssg-lcell], [data-ssg-lrow]').forEach(e => {
    e.removeAttribute('data-ssg-lcell'); e.removeAttribute('data-ssg-lrow'); });
  // "+ Шинэ" placeholder мөр (data-visible-index=-1, 1–2 нүд, input-гүй) засварын мөр БИШ (бодит SmartApp хэмжилт)
  const placeholder = (r) => r.children.length <= 2 && !r.querySelector('input');
  const rows = Array.from(g.querySelectorAll('tr.dxbl-grid-edit-row, tr[data-visible-index="-1"]')).filter(__vis)
    .filter(r => !placeholder(r));
  const hasInput = (r) => Array.from(r.querySelectorAll('input')).some(__vis);
  // Данс сонгосны дараа мөр data-visible-index=0 болж засварын горимдоо үлддэг → фокустай / input-тэй мөр
  let row = rows.find(r => document.activeElement && r.contains(document.activeElement))
    || rows.find(r => /dxbl-grid-edit-row/.test(r.className || '') && hasInput(r))
    || rows.find(r => /new-item/.test(r.className || '')) || rows[rows.length - 1];
  if (!row) {
    // Lookup сонгосны дараа шинэ мөр commit болж засварын горимоос гарсан байж болно → саяхан сонгосон
    // утгыг (hint: ж. дансны код) агуулсан мөрүүдээс, тэр нүдийг тооцохгүйгээр тоон утгын нийлбэр хамгийн
    // бага (шинэ, бөглөгдөөгүй) мөр; тэнцвэл сүүлийнх
    let data = Array.from(g.querySelectorAll('tr[data-visible-index]')).filter(__vis)
      .filter(r => +r.getAttribute('data-visible-index') >= 0);
    if (hint) { const h = data.filter(r => (r.innerText || '').includes(hint)); if (h.length) data = h; }
    const score = (r) => Array.from(r.children).map(td => (td.innerText || '').trim())
      .filter(t => t && (!hint || !t.includes(hint)) && /^-?[\d\s.,]+$/.test(t))
      .reduce((s, t) => s + Math.abs(parseFloat(t.replace(/[\s,]/g, '')) || 0), 0);
    let best = null, bestScore = Infinity;
    for (const r of data) { const sc = score(r); if (sc <= bestScore) { best = r; bestScore = sc; } }
    row = best;
  }
  if (!row) return null;
  row.setAttribute('data-ssg-lrow', '1');
  return Array.from(row.children).map((td, i) => {
    td.setAttribute('data-ssg-lcell', String(i));
    const t = (td.innerText || td.textContent || '').replace(/\s+/g, ' ').trim();
    return {i, text: t,
            lookup: !!td.querySelector('.dxbl-text-edit-btn') || /(^|\s)(\.\.\.|…)$/.test(t),
            input: !!Array.from(td.querySelectorAll('input')).find(__vis),
            numeric: t !== '' && /^-?[\d\s.,]+$/.test(t),
            command: !!td.querySelector('[class*=fa-trash], [class*=fa-plus]') || /command|action/.test(td.className || '')};
  });
}
""".replace("/*__COMMON__*/", _COMMON_JS)

# Мөрийн нүдэн доторх lookup ("...") товчийг тэмдэглэнэ
MARK_CELL_LOOKUP_JS = r"""
(i) => {
  document.querySelectorAll('[data-ssg-lookup]').forEach(e => e.removeAttribute('data-ssg-lookup'));
  const td = document.querySelector(`[data-ssg-lrow="1"] [data-ssg-lcell="${i}"]`);
  if (!td) return false;
  const b = td.querySelector('.dxbl-text-edit-btn') || Array.from(td.querySelectorAll('button, [role=button]'))
    .find(x => /^(\.\.\.|…)$/.test((x.innerText || '').trim()));
  if (!b) return false;
  b.setAttribute('data-ssg-lookup', '1');
  return true;
}
"""

TOP_DIALOG_LEN_JS = r"""
([sel]) => {
  /*__COMMON__*/
  const root = __scope(sel, 'dialog');
  return root ? __txt(root).length : 99999;
}
""".replace("/*__COMMON__*/", _COMMON_JS)

# Дээд давхаргын цонхны толгойн ✕ (хадгалахгүй хаах)
MARK_HEADER_CLOSE_JS = r"""
([sel]) => {
  /*__COMMON__*/
  document.querySelectorAll('[data-ssg-hclose]').forEach(e => e.removeAttribute('data-ssg-hclose'));
  const root = __scope(sel, 'dialog');
  if (!root) return false;
  const b = Array.from(root.querySelectorAll('.dxbl-popup-header-button, .dxbl-modal-header button, ' +
      '.dxbl-popup-header button, [aria-label=Close], [aria-label=close], .dxbl-btn-close, .btn-close'))
      .filter(__vis).pop();
  if (!b) return false;
  b.setAttribute('data-ssg-hclose', '1');
  return true;
}
""".replace("/*__COMMON__*/", _COMMON_JS)

# «Нэмэх/Шинэ» дарсны дараа юу болсныг оношлох (бодит сайтын DOM зөрөхөд YAML/selector тааруулахад)
PROBE_JS = r"""
([sel, tabSel]) => {
  /*__COMMON__*/
  const q = (s) => { try { return Array.from(document.querySelectorAll(s)); } catch (e) { return []; } };
  const cls = (el) => (typeof el.className === 'string' ? el.className : '').slice(0, 80);
  const tabs = q(tabSel).filter(__vis).map(t => __txt(t).split('\n')[0].trim()).slice(0, 15);
  const dialogs = __findDialogs(sel).map(d => ({tag: d.tagName.toLowerCase(), cls: cls(d), text: __txt(d).slice(0, 150)}));
  const menus = q(sel.popup_menu).filter(__vis).map(m => __txt(m).replace(/\s+/g, ' ').slice(0, 60)).slice(0, 15);
  const root = __scope(sel, 'auto');
  const inputs = Array.from(root.querySelectorAll(sel.field_input)).filter(__vis).filter(i => !__skipInput(sel, i));
  const sample = inputs.slice(0, 15).map(i => ({ lookup: !!__lookupBtn(sel, i),
    tag: i.tagName.toLowerCase(), type: i.getAttribute('type') || '', name: i.getAttribute('name') || '',
    id: (i.id || '').slice(0, 40), cls: cls(i), placeholder: i.getAttribute('placeholder') || '',
    aria: i.getAttribute('aria-label') || '', skipped: __skipInput(sel, i), guess: __fieldLabel(i),
    parent: cls(i.parentElement || i), grand: cls((i.parentElement && i.parentElement.parentElement) || i)}));
  const caps = Array.from(root.querySelectorAll(sel.form_caption)).filter(__vis).map(c => __txt(c)).filter(Boolean).slice(0, 15);
  return {url: location.href, tabs, dialogs, menus, inputs: inputs.length, sample, captions: caps};
}
""".replace("/*__COMMON__*/", _COMMON_JS)


def fcfg(env: "FlowEngine") -> dict:
    return env.cfg.get("flow") or {}


def sel_payload(env: "FlowEngine") -> dict:
    return {**fcfg(env).get("selectors", {}), "dialog": env.cfg["selectors"]["dialog"]}


def buttons(env: "FlowEngine", kind: str, override=None) -> list[str]:
    if override:
        return [override] if isinstance(override, str) else list(override)
    return list((fcfg(env).get("buttons") or {}).get(kind) or [])


def menu_item(path: str) -> MenuItem:
    """'Модуль / Хавтас / Дэд хавтас / Цэс' эсвэл smoke-ийн цэсний түлхүүр 'module|folder|name|href' → MenuItem."""
    if path.count("|") == 3:
        m, folder, name, href = path.split("|")
        return MenuItem(module=m, folder=folder, name=name, href=href)
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if len(parts) < 2:
        raise StepFailed(f"Цэсний зам 'Модуль / ... / Цэс' хэлбэртэй байх ёстой: {path!r}")
    return MenuItem(module=parts[0], folder=" / ".join(parts[1:-1]), name=parts[-1], href="")


# ------------------------------------------------------------------ хүлээлт / алдаа
async def settle(env: "FlowEngine", max_s: float | None = None) -> None:
    """Ачаалал (loading индикатор) дуустал (дээд тал нь max_s) хүлээгээд, дараа нь сүлжээ тайван болохыг
    `flow.settle_cap_ms` хүртэл л хүлээнэ. SmartApp-д байнга нээлттэй (polling/long-poll) хүсэлт байдаг тул
    сүлжээ хэзээ ч «тайван» болохгүй байж болно — урьд нь энэ нь алхам бүрт 10–30 сек хиймэл хүлээлт үүсгэдэг
    байсан (HR: шинэ форм ~43 сек). Smoke ч мөн адил богино хязгаартай (timing.settle_ms)."""
    quiet = fcfg(env).get("settle_ms", 500) / 1000
    cap = fcfg(env).get("settle_cap_ms", 1500) / 1000
    deadline = time.monotonic() + (max_s if max_s is not None else env.cfg["timing"]["loading_blocked_ms"] / 1000)
    calm_since = None
    while time.monotonic() < deadline:
        st = await snapshot(env.page, env.cfg)
        if st and not st.loading:
            if env.col.quiet_for(quiet):
                return
            calm_since = calm_since or time.monotonic()
            if time.monotonic() - calm_since >= cap:
                return
        else:
            calm_since = None
        await asyncio.sleep(0.1)


def _is_error_text(env: "FlowEngine", text: str) -> bool:
    det = env.cfg.get("detect") or {}
    low = text.lower()
    pats = (det.get("system_error_title") or []) + (det.get("sql_error") or []) + (det.get("auth_error") or [])
    return any(p.lower() in low for p in pats) or any(w in low for w in ("exception", "error"))


async def popup_problems(env: "FlowEngine") -> tuple[list[Finding], list[str]]:
    """(алдаа, validation-ий анхааруулга) — цонх/toast-оос."""
    st = await snapshot(env.page, env.cfg)
    if not st:
        return [], []
    warn_pats = [p.lower() for p in ((env.cfg.get("detect") or {}).get("warning_toast") or [])
                 + (fcfg(env).get("validation_texts") or [])]
    titles = [t.lower() for t in fcfg(env).get("validation_titles") or []]

    def is_warning(text: str, dialog: bool = False) -> bool:
        low = text.lower()
        first = low.strip().split("\n")[0].strip()
        if any(first.startswith(t) for t in titles):
            return True
        # Форм өөрөө цонх — урт текстэд (шошгонууд) эдгээр үг таарч болох тул зөвхөн богино мессеж цонх
        return (not dialog or len(low) <= 300) and any(p in low for p in warn_pats)

    def is_error_dialog(text: str) -> bool:
        lines = [ln for ln in text.strip().split("\n") if ln.strip()]
        if len(text) <= 200 and len(lines) <= 4:
            return _is_error_text(env, text)          # богино мессеж цонх
        # Форм цонх (олон мөр/урт): шошго/багананд «Томьёо алдаатай», «error» мэт үг байж болно (ж: HR «Цалин бодох
        # загвар» — шошгоноос болж худал Fail болдог байсан) — зөвхөн гарчиг нь системийн алдаа, эсвэл SQL / exception
        if any(w in text for w in ("Exception:", "Object reference not set", "Unhandled exception")):
            return True
        # Урт текст = форм цонх: шошго/багананд «Томьёо алдаатай», «error» мэт үг байж болно — зөвхөн
        # SQL алдааны текст эсвэл системийн алдааны гарчгаар эхэлсэн үед алдаа гэж үзнэ
        det = env.cfg.get("detect") or {}
        low, first = text.lower(), text.strip().split("\n")[0].strip().lower()
        return (any(p.lower() in low for p in det.get("sql_error") or [])
                or any(first.startswith(t.lower()) and len(first) <= len(t) + 3
                       for t in det.get("system_error_title") or []))

    errors, warnings = [], []
    for d in st.dialogs:
        if is_error_dialog(d):
            errors.append(Finding(M.SYSTEM_ERROR, dialog_text(d)[:500]))
        elif is_warning(d, dialog=True):     # «Анхаар — Данс оруулна уу!» мэт validation цонх
            warnings.append(dialog_text(d))
    for t in st.toasts:
        if _is_error_text(env, t) or "алдаа" in t.lower():
            errors.append(Finding(M.SYSTEM_ERROR, dialog_text(t)[:500]))
        elif is_warning(t):
            warnings.append(dialog_text(t))
    return errors, warnings


async def raise_on_problems(env: "FlowEngine", what: str, warnings_too: bool = True) -> None:
    errors, warnings = await popup_problems(env)
    if errors:
        raise StepFailed(f"{what}: {errors[0].text}", errors)
    if warnings and warnings_too:
        raise StepFailed(f"{what}: баталгаажуулалтын анхааруулга — {warnings[0]}",
                         [Finding(M.WARNING_TOAST, w) for w in warnings])


# ------------------------------------------------------------------ цэс
async def reload_app(env: "FlowEngine", why: str) -> bool:
    """Апп-ыг дахин ачаална (smoke-ийн crash recovery-тэй ижил). Blazor компонент унасны (unhandled rendering
    exception) дараа WASM апп буруу төлөвт орж цэс/таб хэвийн ажиллахаа больдог. Нэвтрэлт хадгалагдана;
    login хуудас гарвал engine-ийн session guard дараагийн алхмын өмнө гар нэвтрэлт хүлээнэ."""
    from app.runner.session import wait_login_or_app
    t = env.cfg["timing"]
    env.log(f"Апп-ыг дахин ачаалж байна — {why}", "warn")
    try:
        await env.page.reload(wait_until="domcontentloaded", timeout=t["app_ready_timeout_ms"])
        state = await wait_login_or_app(env.page, env.cfg, t["app_ready_timeout_ms"], env.cancel_event)
    except Exception as e:
        env.log(f"Дахин ачаалах алдаа: {e!r}", "warn")
        return False
    if state == "app":
        await settle(env, 15)
        return True
    return state == "login"


async def open_menu(env: "FlowEngine", path: str, _retry: bool = True) -> str:
    """Цэсийг нээнэ. 'ok' | 'forbidden' | 'missing' буцаана. Таб нээгдэхгүй бол апп-ыг дахин ачаалж нэг удаа
    дахин оролдоно (өмнөх цэсийн Blazor уналтаас үлдсэн эвдэрсэн төлөв)."""
    item = menu_item(path)
    page, cfg = env.page, env.cfg
    aliases = env.ctx.__dict__.setdefault("_tab_alias", {})    # цэсийн нэр → бодит табын гарчиг
    st = await snapshot(page, cfg)
    if st and (tab_is(st.active_tab, item.name) or (aliases.get(item.name) and st.active_tab == aliases[item.name])):
        return "ok"
    tabs_before = st.tab_texts() if st else []
    if not await discovery.mark_leaf(page, cfg, item):
        st = await snapshot(page, cfg)
        if _retry and (st is None or st.dead) and await reload_app(
                env, f"'{item.name}' цэс олдсонгүй — апп (.NET runtime) унасан байна"):
            env.note("Апп унасан байсан тул дахин ачаалж цэсийг дахин хайв")
            return await open_menu(env, path, _retry=False)
        return "missing"
    try:
        await page.click("[data-ssg-target='1']", timeout=5000)
    except Exception as e:
        # Цэсний холбоосыг дарж чадсангүй (өөр элемент хааж байна, цэс хураагдсан г.м.) — хэрэгслийн
        # хязгаарлалт, аппын bug биш
        raise StepBlocked(f"'{item.name}' цэсийг дарж чадсангүй: {str(e).splitlines()[0][:150]}") from None
    forbidden_tab = cfg["selectors"].get("forbidden_tab_name", "_403")
    # Таб ихэвчлэн 1–3 сек-д нээгддэг; ачаалал (loading) үргэлжилж байвал loading_blocked_ms хүртэл хүлээнэ
    deadline = time.monotonic() + cfg["timing"]["loading_blocked_ms"] / 1000
    no_load_deadline = time.monotonic() + fcfg(env).get("tab_open_timeout_ms", 10000) / 1000
    while time.monotonic() < deadline:
        st = await snapshot(page, cfg)
        if st:
            if st.forbidden or st.active_tab == forbidden_tab:
                return "forbidden"
            if tab_is(st.active_tab, item.name) and not st.loading:
                break
            # Шинэ таб нээгдсэн ч гарчиг нь цэсийн нэрээс өөр (товчилсон/англи нэр) — нээгдсэн гэж үзэж санана
            if (st.active_tab and st.active_tab not in tabs_before and st.active_tab != forbidden_tab
                    and not st.loading and not st.dialogs):
                aliases[item.name] = st.active_tab
                env.note(f"Табын гарчиг «{st.active_tab}» цэсийн нэр «{item.name}»-ээс өөр")
                break
            await raise_on_problems(env, f"'{item.name}' цэсийг нээхэд", warnings_too=False)
            if st.dead or (not st.loading and time.monotonic() > no_load_deadline):
                deadline = 0          # ачааллагүйгээр таб нээгдэхгүй байна — эвдэрсэн төлөв
                break
        await asyncio.sleep(0.15)
    if deadline == 0 or time.monotonic() >= deadline:
        if _retry and await reload_app(env, f"'{item.name}' цэсний таб нээгдсэнгүй (идэвхтэй таб: "
                                            f"{st.active_tab if st else '—'})"):
            env.note("Цэсний таб нээгдээгүй тул апп-ыг дахин ачаалж дахин нээв")
            return await open_menu(env, path, _retry=False)
        raise StepFailed(f"'{item.name}' цэсний таб нээгдсэнгүй (идэвхтэй таб: {st.active_tab if st else '—'})")
    await settle(env)
    await raise_on_problems(env, f"'{item.name}' цэсийг нээхэд", warnings_too=False)
    return "ok"


async def open_menu_any(env: "FlowEngine", key: str) -> None:
    """Бичсэн урсгалын цэс: модуль мэдэгдэж байвал шууд, үгүй бол (бичихэд модуль сонгоогүй) модуль бүрээс хайна."""
    item = menu_item(key)
    if item.module:
        return await ensure_menu(env, key)
    outcome = await open_menu(env, key)
    if outcome == "missing":
        for m in await discovery.list_modules(env.page, env.cfg):
            outcome = await open_menu(env, f"{m}|{item.folder}|{item.name}|{item.href}")
            if outcome != "missing":
                break
    if outcome == "forbidden":
        raise StepFailed(f"'{item.name}' цэсэд хандах эрхгүй (403)", [Finding(M.FORBIDDEN, item.name)])
    if outcome == "missing":
        raise StepFailed(f"'{item.name}' цэс олдсонгүй")


async def ensure_menu(env: "FlowEngine", path: str | None) -> None:
    if not path:
        return
    outcome = await open_menu(env, path)
    if outcome == "forbidden":
        raise StepFailed(f"'{path}' цэсэд хандах эрхгүй (403)", [Finding(M.FORBIDDEN, path)])
    if outcome == "missing":
        raise StepFailed(f"'{path}' цэс олдсонгүй")


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
        m = re.match(r"^(.{2,40}?)\s+(оруулна|сонгоно|татна|бөглөнө|хоосон байх|хоосон байна|заавал)",
                     line, flags=re.I)
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
        await page.click("[data-ssg-lookup='1']", timeout=5000)
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
                searched = await _lookup_search(env, want)       # цонхны хайлтаар шүүгээд дахин үзнэ
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
                                                            if not any(w in headers[i] for w in COMPUTED_WORDS)), None))
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


async def pick_option(env: "FlowEngine", label: str) -> str:
    """Сонголтын талбарт (select / combobox / lookup "...") эхний боломжит утгыг сонгоно.
    Сонгосон утгын текстийг, чадахгүй бол "" буцаана. Сонгох нь өгөгдөл өөрчлөхгүй."""
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
        picked = await pick_from_lookup(env, f"'{label}' талбарын сонголтын")
        if not picked:
            return ""
    elif info["combo"]:
        js = r"""(s) => { const v = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
          const o = Array.from(document.querySelectorAll(s)).filter(v).find(x => (x.innerText || '').trim());
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
                picked = await page.evaluate(js, sel_payload(env)["list_option"])
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
FORM_TAB_JS = r"""
([sel, i]) => {
  /*__COMMON__*/
  const root = __scope(sel, 'dialog');
  if (!root) return 0;
  const tabs = Array.from(root.querySelectorAll('[role=tab], .dxbl-tabs-item')).filter(__vis)
      .filter((t, k, a) => !a.some(o => o !== t && o.contains(t)));
  if (i >= 0 && tabs[i]) tabs[i].click();
  return tabs.length;
}
""".replace("/*__COMMON__*/", _COMMON_JS)


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
    """Талбар харагдахгүй бол формын дотоод табуудыг ээлжлэн нээж хайна (таб солиход өгөгдөл хадгалагдана)."""
    try:
        return await find_field(env, label)
    except StepFailed:
        pass
    n = await env.page.evaluate(FORM_TAB_JS, [sel_payload(env), -1])
    for i in range(min(n, 12)):
        await env.page.evaluate(FORM_TAB_JS, [sel_payload(env), i])
        try:
            return await find_field(env, label, wait_s=1.0)
        except StepFailed:
            continue
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


async def fill_field(env: "FlowEngine", label: str, value) -> None:
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
    if info["combo"]:
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


async def dialog_count(env: "FlowEngine") -> int:
    st = await snapshot(env.page, env.cfg)
    return len(st.dialogs) if st else 0


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
