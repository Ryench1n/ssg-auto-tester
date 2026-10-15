"""Браузерт (page.evaluate) ажиллах нийтлэг JS хэсгүүд: талбар/товч/мөр олох, форм скан."""
from __future__ import annotations

from app.runner.detectors import DIALOGS_FN


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
    const inputs = Array.from(el.querySelectorAll('input, textarea, select')).filter(__vis);
    if (inputs.length > 2) break;
    // Товч энэ талбарынх: DOM дарааллаар товчны өмнөх хамгийн ойрын талбар нь i (2 талбартай жижиг формд
    // хөрш lookup-ийн "..." товчийг текст талбарынх гэж андуурахаас сэргийлнэ)
    const owner = (b) => inputs.filter(x => x.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).pop();
    const b = Array.from(el.querySelectorAll('button, [role=button], .dxbl-btn')).filter(__vis).find(x => {
      const t = __txt(x), meta = ((x.getAttribute('title') || '') + ' ' + (x.getAttribute('aria-label') || '')).toLowerCase();
      return (/^(\.\.\.|…)$/.test(t) || /сонго|lookup|browse|select|…|\.\.\./.test(meta)) && owner(x) === i;
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
    let grid = [...byGrid.values()].sort((a, b) => b.length - a.length)[0];
    // «999 Бүх улс», «Бүх Бүх» — нэгтгэл (бүгд) мөр: систем ихэвчлэн хүлээж авдаггүй (HR «Хот, аймаг»: «Улс
    // сонгогдоогүй байна!») — өөр мөр байвал тэрийг сонгоно
    const allRow = (r) => /^(\d+\s+)?бүх(\s|$)/i.test(__txt(r).replace(/^[^\p{L}\p{N}]+/u, '').trim());
    if (grid.some(r => !allRow(r))) grid = grid.filter(r => !allRow(r));
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


# Формын (дээд цонхны) дотоод табууд: i ≥ 0 — дарна, буцаах нь табын тоо; i = -2 — идэвхтэй табын индекс (-1 = алга)
FORM_TAB_JS = r"""
([sel, i]) => {
  /*__COMMON__*/
  const root = __scope(sel, 'dialog');
  if (!root) return i === -2 ? -1 : 0;
  const tabs = Array.from(root.querySelectorAll('[role=tab], .dxbl-tabs-item')).filter(__vis)
      .filter((t, k, a) => !a.some(o => o !== t && o.contains(t)));
  if (i === -2) return tabs.findIndex(t => t.getAttribute('aria-selected') === 'true'
      || /(^|\s)dxbl-active(\s|$)/.test(typeof t.className === 'string' ? t.className : '')
      || !!t.querySelector('.dxbl-active'));
  if (i >= 0 && tabs[i]) tabs[i].click();
  return tabs.length;
}
""".replace("/*__COMMON__*/", _COMMON_JS)
