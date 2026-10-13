"""Модуль, цэсний модыг илрүүлэх (зөвхөн навигаци — өгөгдөл өөрчлөхгүй)."""
from __future__ import annotations

import asyncio
import time

from app.models import MenuItem

MODULES_JS = r"""
(sel) => {
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return Array.from(document.querySelectorAll(sel.module_item)).filter(vis).map((el, i) => {
    const name = (el.getAttribute('title') || el.innerText || el.textContent || '').trim().split('\n')[0].trim();
    return {name, index: i};
  }).filter(m => m.name);
}
"""

MARK_MODULE_JS = r"""
([sel, name]) => {
  document.querySelectorAll('[data-ssg-mod]').forEach(e => e.removeAttribute('data-ssg-mod'));
  const norm = s => (s || '').trim().split('\n')[0].trim();
  const el = Array.from(document.querySelectorAll(sel.module_item))
      .find(e => norm(e.getAttribute('title') || e.innerText || e.textContent) === name);
  if (!el) return false;
  el.setAttribute('data-ssg-mod', '1');
  return true;
}
"""

# Хавтасны зам: parent-id attribute → хавтасны id (id / data-id / item-id / node-id);
# олдохгүй бол DOM-ын өвөг хавтсаар.
LEAVES_JS = r"""
(sel) => {
  const root = document.querySelector(sel.menu_root) || document;
  const first = (el) => (el.getAttribute('aria-label') || el.getAttribute('title') ||
                         (el.innerText || el.textContent || '')).trim().split('\n')[0].trim();
  const folders = Array.from(root.querySelectorAll(sel.folder));
  const byId = new Map();
  for (const f of folders) {
    for (const a of ['id', 'data-id', 'item-id', 'node-id']) {
      const v = f.getAttribute(a); if (v) byId.set(v, f);
    }
  }
  const parentOf = (el) => {
    const pid = el.getAttribute(sel.parent_attr);
    if (pid && byId.has(pid)) return byId.get(pid);
    let p = el.parentElement;
    while (p && p !== root) {
      if (p.matches && p.matches(sel.folder)) return p;
      // <div folder/> <div role=group>...</div> хэлбэр
      if (p.getAttribute && p.getAttribute('role') === 'group') {
        const prev = p.previousElementSibling;
        if (prev && prev.matches(sel.folder)) return prev;
      }
      p = p.parentElement;
    }
    return null;
  };
  const path = (el) => {
    const names = []; let f = parentOf(el); let guard = 0;
    while (f && guard++ < 10) { names.unshift(first(f)); f = parentOf(f); }
    return names.join(' / ');
  };
  return Array.from(root.querySelectorAll(sel.leaf)).map((a, i) => ({
    name: first(a), href: a.getAttribute('href') || '', folder: path(a), index: i
  })).filter(x => x.name);
}
"""

EXPAND_JS = r"""
(sel) => {
  const root = document.querySelector(sel.menu_root) || document;
  const els = Array.from(root.querySelectorAll(sel.folder_collapsed));
  let n = 0;
  for (const el of els) {
    const target = el.querySelector('.dxbl-treeview-item-content, .expand-btn, [class*=toggle]') || el;
    try { target.click(); n++; } catch (e) {}
  }
  return n;
}
"""

MARK_LEAF_JS = r"""
([sel, item]) => {
  const root = document.querySelector(sel.menu_root) || document;
  document.querySelectorAll('[data-ssg-target]').forEach(e => e.removeAttribute('data-ssg-target'));
  const first = (el) => (el.getAttribute('aria-label') || el.getAttribute('title') ||
                         (el.innerText || el.textContent || '')).trim().split('\n')[0].trim();
  const all = Array.from(root.querySelectorAll(sel.leaf));
  let c = all.filter(a => (a.getAttribute('href') || '') === item.href && first(a) === item.name);
  if (!c.length) c = all.filter(a => first(a) === item.name);
  if (!c.length) return 0;
  let el = c[0];
  if (c.length > 1 && item.folder) {
    const leaf = item.folder.split(' / ').pop();
    const byFolder = c.filter(a => {
      const pid = a.getAttribute(sel.parent_attr);
      const f = pid ? root.querySelector(`[id="${CSS.escape(pid)}"],[data-id="${CSS.escape(pid)}"],[item-id="${CSS.escape(pid)}"]`) : null;
      return f && first(f) === leaf;
    });
    if (byFolder.length) el = byFolder[0];
  }
  el.setAttribute('data-ssg-target', '1');
  el.scrollIntoView({block: 'center'});
  return c.length;
}
"""


def sel_payload(cfg: dict) -> dict:
    s = cfg["selectors"]
    return {k: s[k] for k in ("module_item", "menu_root", "folder", "folder_collapsed", "leaf",
                              "parent_attr")}


async def list_modules(page, cfg: dict) -> list[str]:
    mods = await page.evaluate(MODULES_JS, sel_payload(cfg))
    seen, out = set(), []
    for m in mods:
        if m["name"] not in seen:
            seen.add(m["name"])
            out.append(m["name"])
    return out


async def activate_module(page, cfg: dict, module: str) -> bool:
    if not await page.evaluate(MARK_MODULE_JS, [sel_payload(cfg), module]):
        return False
    await page.click("[data-ssg-mod='1']", timeout=5000)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if await page.locator(cfg["selectors"]["menu_root"]).count():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.12)
    return True


async def expand_all(page, cfg: dict, max_rounds: int = 6) -> None:
    """aria-expanded=false хавтас үлдэхгүй болтол (≤6 давталт) задална."""
    for _ in range(max_rounds):
        n = await page.evaluate(EXPAND_JS, sel_payload(cfg))
        if not n:
            return
        await asyncio.sleep(0.2)


async def discover_module(page, cfg: dict, module: str) -> list[MenuItem]:
    if not await activate_module(page, cfg, module):
        return []
    await expand_all(page, cfg)
    raw = await page.evaluate(LEAVES_JS, sel_payload(cfg))
    items: list[MenuItem] = []
    seen: set[str] = set()
    for r in raw:
        it = MenuItem(module=module, folder=r["folder"], name=r["name"], href=r["href"], index=r["index"])
        if it.key in seen:
            continue
        seen.add(it.key)
        items.append(it)
    return items


async def _marked_visible(page) -> bool:
    try:
        return await page.locator("[data-ssg-target='1']").first.is_visible()
    except Exception:
        return False


async def mark_leaf(page, cfg: dict, item: MenuItem) -> bool:
    """Навч цэсийг индексээр биш href/нэрээр нь олно (reload-ын дараа дараалал өөрчлөгдөж болно).

    Элемент DOM-д байгаа ч хаагдсан хавтас дотор (харагдахгүй) байж болох тул
    харагдахгүй бол модулийг идэвхжүүлж, хавтсуудыг задлаад дахин тэмдэглэнэ.
    """
    payload = [sel_payload(cfg), {"href": item.href, "name": item.name, "folder": item.folder}]
    if await page.evaluate(MARK_LEAF_JS, payload) and await _marked_visible(page):
        return True
    await expand_all(page, cfg)
    if await page.evaluate(MARK_LEAF_JS, payload) and await _marked_visible(page):
        return True
    await activate_module(page, cfg, item.module)
    await expand_all(page, cfg)
    return bool(await page.evaluate(MARK_LEAF_JS, payload)) and await _marked_visible(page)
