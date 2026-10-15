"""Цэсний үйлдлийн бүртгэл: smoke нээсэн таб бүрийн товч, grid-ийг ЗӨВХӨН УНШИЖ цуглуулна.

Юу ч дарахгүй (READ-ONLY дүрэм хэвээр). Үр дүнгээр цэсийг ангилна:
  crud = "full"    — Нэмэх + Устгах товчтой → автомат CRUD хийж болно (тестийн өгөгдлийг цэвэрлэж чадна)
         "partial" — зөвхөн зарим нь (ж: Засах) → автомат CRUD хийхгүй
         "none"    — CRUD товчгүй (жагсаалт, тайлан …)
"""
from __future__ import annotations

import re

from app.models import MenuItem
from app.runner.detectors import DIALOGS_FN

SCAN_JS = r"""
([sel]) => {
  /*__DIALOGS__*/
  const vis = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const q = (root, s) => { try { return Array.from(root.querySelectorAll(s)); } catch (e) { return []; } };
  const excl = q(document, sel.exclude);
  const dialogs = __findDialogs(sel);
  const outside = (el) => !excl.some(e => e.contains(el)) && !dialogs.some(d => d.contains(el));
  const btnsIn = (root) => q(root, sel.button).filter(vis).filter(outside);
  // Идэвхтэй табын агуулга: харагдаж буй scope-уудаас хамгийн олон товчтой нь
  const scopes = q(document, sel.scope).filter(vis).filter(outside);
  let root = document;
  if (scopes.length) root = scopes.sort((a, b) => btnsIn(b).length - btnsIn(a).length)[0];
  const seen = new Map();
  for (const b of btnsIn(root)) {
    const t = (txt(b) || b.getAttribute('title') || b.getAttribute('aria-label') || '').split('\n')[0].trim().slice(0, 40);
    if (!t) continue;
    const dis = !!(b.disabled || b.getAttribute('aria-disabled') === 'true');
    seen.set(t, seen.has(t) ? (seen.get(t) && dis) : dis);
  }
  const buttons = Array.from(seen, ([text, disabled]) => ({text, disabled})).slice(0, 40);
  // DevExpress баганын толгойд шүүлтүүрийн aria текст ("No filter applied") наалддаг — хасна
  const colTxt = (el) => txt(el).replace(/\s*(No filter applied|Filter applied)\s*$/i, '').trim();
  const columns = [...new Set(q(root, sel.grid_header).filter(vis).map(colTxt).filter(Boolean))].slice(0, 25);
  // Цагаан дэлгэц: табын агуулгад текст ч, оролт/grid/зураг/график ч алга
  // (агуулгагүй табын хэсэг 0 өндөртэй байж болно — тиймээс display:none биш scope-ыг ч харна)
  const shown = (e) => getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden';
  const blankRoot = root !== document ? root
    : (q(document, sel.scope).filter(outside).filter(shown).filter(e => !q(e, sel.scope).length).pop() || null);
  const content = !blankRoot ? true
    : (txt(blankRoot).length > 0 || q(blankRoot, 'input, textarea, select, canvas, svg, img, iframe, table, .dxbl-grid').some(vis));
  const grids = q(root, sel.grid).filter(vis);
  const empty = grids.length > 0 && grids.every(g => /бичлэг олдсонгүй|no data/i.test(txt(g))
    && !q(g, 'tr[data-visible-index], [role=row][aria-rowindex]').some(r => !/бичлэг олдсонгүй|no data/i.test(txt(r))));
  return {buttons, columns, search: q(root, sel.grid_search).some(vis), grid: grids.length > 0,
          blank: !content, empty};
}
""".replace("/*__DIALOGS__*/", DIALOGS_FN)


def _norm(s: str) -> str:
    return re.sub(r"[\s:*]+$", "", re.sub(r"\s+", " ", s or "")).strip().lower()


def scan_payload(cfg: dict) -> dict:
    cs = cfg.get("crud_scan") or {}
    fs = (cfg.get("flow") or {}).get("selectors") or {}
    return {"scope": cs.get("scope", "[role=tabpanel]"), "exclude": cs.get("exclude", "nav"),
            "grid": cs.get("grid", "table"), "grid_header": cs.get("grid_header", "th"),
            "button": fs.get("button", "button"), "grid_search": fs.get("grid_search", "input[type=search]"),
            "dialog": cfg["selectors"]["dialog"]}


def classify(raw: dict, cfg: dict) -> dict:
    """Товчны текстийг flow.buttons (add/edit/delete)-тэй тулгаж цэсийг ангилна."""
    btns = (cfg.get("flow") or {}).get("buttons") or {}
    texts = {_norm(b["text"]) for b in raw.get("buttons") or []}
    has = {k: any(_norm(w) in texts for w in btns.get(k) or []) for k in ("add", "edit", "delete")}
    proc_words = [w.lower() for w in (cfg.get("crud_scan") or {}).get("process_words") or []]
    process = [b["text"] for b in raw.get("buttons") or [] if _norm(b["text"]) in proc_words]
    if has["add"] and has["delete"]:
        crud = "full"
    elif any(has.values()):
        crud = "partial"
    else:
        crud = "none"
    return {**raw, **has, "process": process, "crud": crud}


async def scan_actions(page, cfg: dict) -> dict:
    try:
        raw = await page.evaluate(SCAN_JS, [scan_payload(cfg)])
    except Exception:
        return {}
    return classify(raw or {}, cfg)


def menu_from_key(key: str) -> MenuItem:
    """MenuItem.key ('module|folder|name|href') → MenuItem."""
    parts = key.split("|")
    if len(parts) != 4:
        raise ValueError(f"Цэсний түлхүүр буруу: {key!r}")
    return MenuItem(module=parts[0], folder=parts[1], name=parts[2], href=parts[3])


def menu_path(item: MenuItem) -> str:
    return " / ".join(p for p in (item.module, item.folder, item.name) if p)


def form_candidates(results) -> list[dict]:
    """Форм шалгалт хийх цэсүүд: Нэмэх эсвэл Засах товчтой (таб нээгдсэн). Өөр модульд ижил хуудас (href) байвал
    нэг удаа (ж: Accounting «Жагсаалт» модуль — 92 цэс, 47 мин давхар шалгагдаж байсан)."""
    out = []
    seen: dict[str, dict] = {}
    for r in results:
        c = r.caps or {}
        if r.item.href and r.item.href in seen:
            seen[r.item.href].setdefault("also", []).append(menu_path(r.item))
            continue
        if r.tab_opened and (c.get("add") or c.get("edit")):
            out.append({"key": r.item.key, "module": r.item.module, "folder": r.item.folder,
                        "name": r.item.name, "path": menu_path(r.item), "status": r.status,
                        "buttons": [b["text"] for b in c.get("buttons") or []], "caps": c})
            if r.item.href:
                seen[r.item.href] = out[-1]
    return out


def crud_candidates(results) -> list[dict]:
    """Smoke-ийн үр дүнгээс автомат CRUD хийж болох (crud=full, таб нээгдсэн) цэсүүд."""
    out = []
    names = {r.item.name.strip().lower() for r in results}
    for r in results:
        c = r.caps or {}
        cols = {str(x).lower() for x in c.get("columns") or []}
        if "бараагаар" in r.item.name.lower() and {"барааны код", "барааны нэр"} & cols:
            continue      # мөрийн (бараагаар) жагсаалт — ижил баримтын толгойн жагсаалт тусдаа цэс
        m = re.match(r"^(.*?)\s*/[^/]+/\s*$", r.item.name.strip())
        if m and m.group(1).strip().lower() in names:
            # «… /ажилтнаар/», «/хөрөнгөөр/», «/дэвсгэртээр/» — мөр/задаргаагаар харуулдаг хувилбар: толгой
            # баримтыг хадгалахад тэнд харагдахгүй (HR, Accounting 2026-10-15); үндсэн жагсаалт нь тусдаа цэс
            continue
        if c.get("crud") == "full" and r.tab_opened:
            out.append({"key": r.item.key, "module": r.item.module, "folder": r.item.folder,
                        "name": r.item.name, "path": menu_path(r.item), "status": r.status,
                        "buttons": [b["text"] for b in c.get("buttons") or []],
                        "columns": c.get("columns") or [], "edit": bool(c.get("edit"))})
    return out
