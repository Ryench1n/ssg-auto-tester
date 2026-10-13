"""Smoke тестийн алгоритм: discovery → цэс бүр → recovery → давтан баталгаажуулалт.

Browser, page, Collector, нэвтрэлт (SessionGuard)-ийг pipeline эзэмшинэ — SmokeRunner нь
тэдгээрийг гаднаас хүлээн авч зөвхөн smoke алхмыг гүйцэтгэнэ.

READ-ONLY зарчим: зөвхөн модуль/хавтас/навч цэс, таб хаах товч, алдааны цонхны
"OK"/"Хаах" товчийг дарна. Бусад бүх товчийг `_safe_click` хамгаалалт татгалзана.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app import models as M
from app.models import Finding, ItemResult, MenuItem, RunData, TimelineEvent
from app.runner import discovery
from app.runner.capabilities import scan_actions
from app.runner.classify import bug_candidates, signature
from app.runner.detectors import tab_mismatch_finding, DIALOGS_FN, Collector, classify_popup, names_match, snapshot, strip_query
from app.runner.runlog import RunLog, now_iso
from app.runner.session import is_login_url, wait_login_or_app

if TYPE_CHECKING:
    from app.pipeline.session import SessionGuard

Emit = Callable[[str, dict], None]


@dataclass
class RunOptions:
    project: str
    qa_lead: str = ""
    tester: str = ""
    modules: list[str] | None = None     # None → бүгд
    wait_selection: bool = False         # discovery-ийн дараа UI-аас модуль сонголт хүлээх
    selection_timeout_s: int = 600
    check_risky: bool = True
    confirm_failures: bool = True
    version_label: str = "v1.0"


@dataclass
class _Flags:
    logout: bool = False
    logout_after_close: bool = False
    reload: bool = False
    crash: bool = False
    deploy: bool = False
    deploy_urls: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


CLOSE_TAB_JS = r"""
([sel, prot, only]) => {
  document.querySelectorAll('[data-ssg-close]').forEach(e => e.removeAttribute('data-ssg-close'));
  const name = t => (t.innerText || t.textContent || '').trim().split('\n')[0].trim();
  for (const t of document.querySelectorAll(sel.tab)) {
    if (prot.includes(name(t))) continue;
    if (only && !only.includes(name(t))) continue;   // зөвхөн шалгасан цэсний нээсэн таб
    let b = t.querySelector(sel.tab_close);
    if (!b && t.nextElementSibling && t.nextElementSibling.matches(sel.tab_close)) b = t.nextElementSibling;
    if (!b && t.parentElement) b = Array.from(t.parentElement.querySelectorAll(sel.tab_close)).find(x => x.closest('[role=tab]') === t) || null;
    if (b) { b.setAttribute('data-ssg-close', '1'); return name(t); }
  }
  return null;
}
"""

DIALOG_BTN_JS = r"""
([sel, safe]) => {
  /*__DIALOGS__*/
  document.querySelectorAll('[data-ssg-safe]').forEach(e => e.removeAttribute('data-ssg-safe'));
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const txt = el => (el.innerText || el.textContent || '').trim();
  for (const d of __findDialogs(sel)) {
    if (!txt(d)) continue;     // хоосон overlay (текстгүй) — алдааны цонх биш
    // Эхлээд баруун дээд ✕ (толгойн хаах товч) — байхгүй бол OK/Хаах мэт аюулгүй товч.
    let b = Array.from(d.querySelectorAll('.dxbl-popup-header-button, .dxbl-modal-header button, ' +
        '.dxbl-popup-header button, [aria-label=Close], [aria-label=close], .dxbl-btn-close, .btn-close'))
        .filter(vis)[0] || null;
    const btns = Array.from(d.querySelectorAll('button, [role=button], .dxbl-btn')).filter(vis);
    if (!b) {
      // Текстгүй (зөвхөн ✕ дүрс) товчнуудаас цонхны хамгийн баруун дээд талынх
      const dr = d.getBoundingClientRect();
      const icons = btns.filter(x => { const t = txt(x); return !t || t === '×' || t === '✕' || t === 'x'; })
        .filter(x => x.getBoundingClientRect().top < dr.top + Math.max(80, dr.height * 0.3));
      icons.sort((a, c) => c.getBoundingClientRect().right - a.getBoundingClientRect().right);
      b = icons[0] || null;
    }
    if (!b) b = btns.find(x => safe.includes(txt(x))) || null;
    if (b) { b.setAttribute('data-ssg-safe', '1'); return (txt(b) || b.getAttribute('aria-label') || '×'); }
    return '';
  }
  return null;
}
""".replace("/*__DIALOGS__*/", DIALOGS_FN)

TOAST_JS = r"""
([sel]) => {
  document.querySelectorAll('[data-ssg-toast]').forEach(e => e.removeAttribute('data-ssg-toast'));
  const toasts = Array.from(document.querySelectorAll(sel.toast));
  for (const t of toasts) {
    const b = t.querySelector(sel.toast_close);
    if (b) { b.setAttribute('data-ssg-toast', '1'); return 'button'; }
  }
  // Хаах товчгүй toast: зөвхөн клиент талын DOM-оос авна (серверийн өгөгдөлд нөлөөгүй)
  toasts.forEach(t => t.remove());
  return toasts.length ? 'removed' : null;
}
"""


def smoke_status(r: ItemResult, cfg: dict) -> str:
    """Цэсийн төлөв. «Зөөлөн» илрэл (Minor консол алдаа, тохиргооны анхааруулга, удаан ачаалал) нь Fail биш —
    цэс ажиллаж байгаа тул Pass + анхааруулга (bug-ийн жагсаалтад Minor болж үлдэнэ). HR: 25 Fail-ын 20 нь
    ийм байж, NO-GO шийдвэрийг дуу чимээ тодорхойлж байсан."""
    if r.blocked_reason:
        return M.BLOCKED
    soft = set((cfg.get("detect") or {}).get("soft_findings")
               or [M.CONSOLE_ERROR, M.WARNING_TOAST, M.SLOW_LOAD, M.TAB_NAME_MISMATCH])
    if any(f.kind not in soft for f in r.findings):
        return M.FAIL
    return M.PASS


class SmokeRunner:
    def __init__(self, url: str, cfg: dict, options: RunOptions, run_dir: Path, *,
                 data: RunData, runlog: RunLog, session: "SessionGuard", cancel_event: asyncio.Event,
                 emit: Emit | None = None):
        self.url = url
        self.cfg = cfg
        self.opt = options
        self.run_dir = Path(run_dir)
        (self.run_dir / "shots").mkdir(parents=True, exist_ok=True)
        self.emit_cb = emit or (lambda t, d: None)
        self.runlog = runlog
        self.sanitize = runlog.sanitize
        self.session = session
        # Хэрэглэгч зогсоосон ЭСВЭЛ session алдагдсан үед тавигдана (pipeline-ынх)
        self.cancel_event = cancel_event
        self.selection: asyncio.Future | None = None
        self.data = data
        self.page = None
        self.col: Collector | None = None
        self.current_module: str | None = None
        self.counts = {M.PASS: 0, M.FAIL: 0, M.BLOCKED: 0}
        self._seen_href: dict[str, ItemResult] = {}   # href → анх шалгасан үр дүн (давхардсан цэс)
        self.last_error = ""
        self._shot_seq = 0
        self.discovered_items: list[MenuItem] = []
        t = cfg["timing"]
        self.wait_s = t["wait_ms"] / 1000
        self.settle_s = t.get("settle_ms", 800) / 1000
        self.post_close_s = t["post_close_wait_ms"] / 1000
        self.loading_s = t["loading_blocked_ms"] / 1000
        self.sel = cfg["selectors"]
        self.protected = list(cfg.get("protected_tabs") or [])
        self._opened_tabs: set[str] = set()   # цэс шалгалтаар нээгдсэн (хаах ёстой) табууд

    # ------------------------------------------------------------------ util
    def log(self, msg: str, level: str = "info", to_ui: bool = True) -> None:
        self.runlog.log(msg, level, to_ui)

    def timeline(self, activity: str, result: str = "", note: str = "") -> None:
        self.data.timeline.append(TimelineEvent(now_iso(), activity, result, note))

    def cancel(self) -> None:
        """Pipeline-ын cancel() дуудна (cancel_event аль хэдийн тавигдсан) — сонголт хүлээлтийг чөлөөлнө."""
        if self.selection and not self.selection.done():
            self.selection.set_result(None)

    def select_modules(self, modules: list[str] | None) -> None:
        if self.selection and not self.selection.done():
            self.selection.set_result(modules)

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)

    # ----------------------------------------------------------------- main
    async def execute(self, page, col: Collector) -> None:
        """Нэвтэрсэн page дээр smoke алхмыг гүйцэтгэнэ (үр дүн self.data-д бичигдэнэ)."""
        self.page, self.col = page, col
        self.current_module = None
        await self._body()

    async def _body(self) -> None:
        # 4. Discovery
        self.emit_cb("status", {"state": "discovery"})
        # app_ready (.modulenavbar) илэрсэн даруйд модулиудын жагсаалт хараахан populate
        # болоогүй байж болно (WASM апп цэсний мэдээллийг async татдаг тул арай удаж болно) —
        # app_ready_timeout_ms-тэй ижил төрлийн хугацаагаар дахин дахин турших.
        modules: list[str] = []
        discovery_timeout_s = max(20.0, self.cfg["timing"]["app_ready_timeout_ms"] / 1000 / 2)
        discovery_deadline = time.monotonic() + discovery_timeout_s
        tries = 0
        while time.monotonic() < discovery_deadline and not self.cancel_event.is_set():
            tries += 1
            modules = await discovery.list_modules(self.page, self.cfg)
            if modules:
                break
            await self._sleep(0.5)
        if not modules and not self.cancel_event.is_set():
            url_now = ""
            try:
                url_now = await self.page.evaluate("() => location.href")
            except Exception:
                pass
            self.log(f"0 модуль олдлоо ({tries} удаа, {discovery_timeout_s:.0f} сек турш шалгасан) — "
                     f"одоогийн URL: {url_now or 'тодорхойгүй'}", "error")
            await self._screenshot("discovery-empty")
        else:
            self.log(f"{len(modules)} модуль олдлоо: {', '.join(modules)}")
        all_items: list[MenuItem] = []
        for mod in modules:
            if self.cancel_event.is_set():
                break
            await self._ensure_ready()
            try:
                items = await discovery.discover_module(self.page, self.cfg, mod)
            except Exception as e:
                self.log(f"Discovery алдаа ({mod}): {e}", "warn")
                items = []
            self.current_module = mod
            self.data.discovered[mod] = len(items)
            all_items.extend(items)
            self.log(f"  {mod}: {len(items)} цэс")
        self.discovered_items = all_items
        self.timeline("Discovery", f"{len(modules)} модуль, {len(all_items)} цэс",
                      ", ".join(f"{m} ({n})" for m, n in self.data.discovered.items()))
        self.emit_cb("discovery", {"modules": [{"name": m, "count": n} for m, n in self.data.discovered.items()],
                                   "total": len(all_items)})

        chosen = self.opt.modules
        if self.opt.wait_selection and not self.cancel_event.is_set():
            self.selection = asyncio.get_running_loop().create_future()
            self.emit_cb("status", {"state": "awaiting_selection"})
            self.log("Модулийн сонголтыг хүлээж байна…")
            try:
                sel = await asyncio.wait_for(self.selection, self.opt.selection_timeout_s)
                if sel:
                    chosen = sel
            except asyncio.TimeoutError:
                self.log("Сонголт ирээгүй — бүх модулийг шалгана", "warn")
        if chosen:
            all_items = [i for i in all_items if i.module in set(chosen)]
        self.data.modules_selected = list(dict.fromkeys(i.module for i in all_items))

        # Эрсдэлтэй / алгасах цэс
        risky = [r.lower() for r in self.cfg.get("risky_items") or []]
        skip = [s.lower() for s in self.cfg.get("skip_items") or []]
        for it in all_items:
            low = it.name.lower()
            it.risky = any(r in low for r in risky)
            it.skip = any(s == low or s in low for s in skip)
        normal = [i for i in all_items if not i.risky]
        riskies = [i for i in all_items if i.risky]
        ordered = normal + riskies        # эрсдэлтэйг хамгийн сүүлд

        # 5. Цэс бүр
        self.emit_cb("status", {"state": "running", "total": len(ordered)})
        t_start = time.monotonic()
        for i, item in enumerate(ordered, 1):
            if self.cancel_event.is_set():
                if self.session.lost:
                    # Session тасарч дахин нэвтрээгүй → үлдсэн цэсүүд Blocked (шалгагдаагүй)
                    for rest in ordered[i - 1:]:
                        self.data.results.append(ItemResult(rest, status=M.BLOCKED, checked_at=now_iso(),
                                                            blocked_reason=self.session.lost_reason[:150]))
                        self.counts[M.BLOCKED] += 1
                else:
                    self.data.cancelled = True
                    self.log("Хэрэглэгч зогсоолоо", "warn")
                break
            t_item = time.monotonic()
            orig = self._seen_href.get(item.href) if item.href else None
            if orig is not None:
                # Өөр модульд ижил хуудас (href) аль хэдийн шалгагдсан — дахин нээхгүй, үр дүнг хуулна (bug давхардахгүй:
                # bug-ийн давтамжийг хуудсаар тоолдог)
                r = replace(orig, item=item, checked_at=now_iso(), findings=list(orig.findings),
                            notes=[f"Давхардсан цэс — «{orig.item.module} › {orig.item.name}»-тэй ижил хуудас; "
                                   "дахин шалгаагүй"] + list(orig.notes))
            else:
                r = await self._check_item(item)
                if item.href and r.status != M.BLOCKED:
                    self._seen_href[item.href] = r
            item_s = time.monotonic() - t_item
            self.data.results.append(r)
            self.counts[r.status] = self.counts.get(r.status, 0) + 1
            if r.status != M.PASS:
                self.last_error = f"{item.name}: {(r.findings[0].text if r.findings else r.blocked_reason)[:160]}"
            elapsed = time.monotonic() - t_start
            eta = elapsed / i * (len(ordered) - i)
            self.log(f"[{i}/{len(ordered)}] {item.module} › {item.name} → {r.status}"
                     + (f" ({r.findings[0].kind})" if r.findings else "") + f" · {item_s:.1f}с")
            self.emit_cb("progress", {
                "i": i, "n": len(ordered), "current": f"{item.module} › {item.folder} › {item.name}",
                "counts": dict(self.counts), "logouts": len(self.data.logouts),
                "crashes": len(self.data.crashes), "eta_s": int(eta), "last_error": self.last_error,
                "status": r.status})

        # Модуль бүрийн хураангуй (тестийн явц)
        for mod in self.data.modules_selected:
            rs = [r for r in self.data.results if r.item.module == mod]
            if rs:
                p = sum(1 for r in rs if r.status == M.PASS)
                f = sum(1 for r in rs if r.status == M.FAIL)
                bl = sum(1 for r in rs if r.status == M.BLOCKED)
                self.data.timeline.append(TimelineEvent(
                    max(r.checked_at for r in rs), f"«{mod}» модулийн smoke тест ({len(rs)} цэс)",
                    f"Pass {p} / Fail {f}" + (f" / Blocked {bl}" if bl else "")))

        # 6. Давтан баталгаажуулалт
        if self.opt.confirm_failures and not self.cancel_event.is_set():
            fails = [r for r in self.data.results if r.status == M.FAIL
                     and not (r.notes and r.notes[0].startswith("Давхардсан цэс"))]
            if fails:
                self.emit_cb("status", {"state": "confirming", "total": len(fails)})
                self.log(f"Давтан баталгаажуулалт: {len(fails)} Fail цэс")
                confirmed = 0
                for r in fails:
                    if self.cancel_event.is_set():
                        break
                    r2 = await self._check_item(r.item, confirm=True)
                    s1 = {signature(f) for f in bug_candidates(r)}
                    s2 = {signature(f) for f in bug_candidates(r2)}
                    if s1 & s2:
                        r.confirmation = "Баталгаажсан (2/2)"
                        confirmed += 1
                    else:
                        r.confirmation = "1 удаа ажиглагдсан"
                self.timeline("Давтан баталгаажуулалт", f"{confirmed}/{len(fails)} баталгаажсан",
                              "Fail цэс бүрийг 1 удаа дахин шалгасан")
        self.data.timeline.sort(key=lambda e: e.at)

    # ------------------------------------------------------------- session
    async def _relogin(self, during: str, count: bool = True) -> None:
        """Logout → pipeline-ын session түр зогсоож гар нэвтрэлт хүлээнэ. Хугацаа хэтэрвэл
        cancel_event тавигдаж, үлдсэн цэсүүд Blocked болно."""
        await self.session.relogin(during, count)
        self.current_module = None

    async def _ensure_ready(self, during: str = "") -> None:
        st = await snapshot(self.page, self.cfg)
        if st is None:
            await wait_login_or_app(self.page, self.cfg, self.cfg["timing"]["app_ready_timeout_ms"],
                                    self.cancel_event)
            st = await snapshot(self.page, self.cfg)
        if st and is_login_url(st.url, self.cfg):
            await self._relogin(during or "цэсний хооронд")
            st = await snapshot(self.page, self.cfg)
        if st and st.dead and not self.cancel_event.is_set():
            await self._recover_crash(during or "өмнөх цэс")

    # ------------------------------------------------------------ recovery
    async def _recover_crash(self, item_name: str) -> None:
        self.log(f".NET runtime уналт ({item_name}) — reload хийж сэргээж байна", "warn")
        reloads, recovered = 0, False
        for _ in range(2):   # уналт давтагдвал 2 дахь удаа reload
            if self.cancel_event.is_set():
                break
            reloads += 1
            t0 = time.monotonic()
            try:
                await self.page.reload(wait_until="domcontentloaded",
                                       timeout=self.cfg["timing"]["app_ready_timeout_ms"])
            except Exception as e:
                self.log(f"Reload алдаа: {e}", "warn")
            state = await wait_login_or_app(self.page, self.cfg, self.cfg["timing"]["app_ready_timeout_ms"],
                                            self.cancel_event)
            if state == "login":
                await self._relogin(item_name, count=False)
            await self._sleep(self.cfg["timing"]["crash_settle_ms"] / 1000)
            st = await snapshot(self.page, self.cfg)
            again = self.col.has_crash(self.col.since(t0)) or (st is not None and st.dead)
            if st and not again:
                recovered = True
                break
            self.log("Reload-ын дараа уналт давтагдлаа", "warn")
        if not recovered and not self.cancel_event.is_set():
            try:
                await self.page.goto(self.url, wait_until="domcontentloaded")
                await wait_login_or_app(self.page, self.cfg, self.cfg["timing"]["app_ready_timeout_ms"],
                                        self.cancel_event)
                st = await snapshot(self.page, self.cfg)
                recovered = bool(st and not st.dead)
            except Exception:
                pass
        self.current_module = None
        self.data.crashes.append({"at": now_iso(), "item": item_name, "reloads": reloads,
                                  "recovered": recovered})
        self.timeline(".NET runtime уналт + recovery", "Сэргээсэн" if recovered else "Сэргээж чадсангүй",
                      f"Цэс: {item_name}; {reloads} удаа reload")

    # ------------------------------------------------------------ cleanup
    async def _safe_click(self, selector: str) -> bool:
        """READ-ONLY хамгаалалт: хориотой үгтэй товчийг хэзээ ч дарахгүй."""
        try:
            loc = self.page.locator(selector).first
            text = (await loc.inner_text(timeout=300) or "").strip()
            label = (await loc.get_attribute("aria-label") or "")
        except Exception:
            text, label = "", ""
        for w in self.cfg.get("forbidden_button_words") or []:
            if w.lower() in (text + " " + label).lower():
                self.log(f"READ-ONLY: «{text or label}» товчийг дарахаас татгалзав", "warn")
                return False
        try:
            await self.page.click(selector, timeout=700)
            return True
        except Exception:
            try:
                await self.page.evaluate("(s) => { const e = document.querySelector(s); if (e) e.click(); }", selector)
                return True
            except Exception:
                return False

    async def _click_target(self) -> None:
        """Цэсийг дарна. Үл мэдэгдэх overlay/цонх click-ийг хаавал ("intercepts pointer events")
        цонхыг ✕-ээр хааж, Escape дараад дахин оролдоно — 5 сек гацаж Blocked болохгүй."""
        for i in range(3):
            try:
                await self.page.click("[data-ssg-target='1']", timeout=2000)
                return
            except Exception:
                if i == 2:
                    raise
                self.log("Цэсийг дарахад өөр цонх саад болсон — хааж дахин дарна", "warn", to_ui=False)
                await self._close_dialogs()
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                await self._sleep(0.15)

    async def _close_dialogs(self) -> None:
        for _ in range(5):
            res = await self._eval(DIALOG_BTN_JS, [self.sel, self.cfg.get("safe_dialog_buttons") or []])
            if res is None:
                return
            if res == "":
                await self.page.keyboard.press("Escape")
                await self._sleep(0.3)
                continue
            await self._safe_click("[data-ssg-safe='1']")
            await self._sleep(0.12)

    async def _clear_toasts(self) -> None:
        for _ in range(10):
            res = await self._eval(TOAST_JS, [self.sel])
            if res != "button":
                return
            await self._safe_click("[data-ssg-toast='1']")
            await self._sleep(0.08)

    async def _count_tabs(self) -> int | None:
        return await self._eval("(s) => document.querySelectorAll(s).length", self.sel["tab"])

    async def _close_tabs(self, only: list[str] | None = None) -> None:
        """`only` — зөвхөн эдгээр нэртэй таб (цэсний нээсэн)-ыг хаана; анхнаасаа байсан
        (модулийн/нүүр) табуудыг хөндөхгүй."""
        if only is not None and not only:
            return
        for _ in range(15):
            name = await self._eval(CLOSE_TAB_JS, [self.sel, self.protected, only])
            if not name:
                return
            self._opened_tabs.discard(name)
            before = await self._count_tabs()
            if not await self._safe_click("[data-ssg-close='1']"):
                return
            # Тогтмол хүлээхийн оронд таб DOM-оос алга болмогц (дээд тал нь 0.6с) шууд үргэлжилнэ.
            end = time.monotonic() + 0.6
            while time.monotonic() < end:
                n = await self._count_tabs()
                if n is None or before is None or n < before:
                    break
                await self._sleep(0.03)

    async def _cleanup(self) -> None:
        # Өмнөх цэсийг хаасны дараа хоцорч гарсан алдааны цонх — тэр цэсийнх тул түүнийг Fail болгоно
        st = await snapshot(self.page, self.cfg)
        real = [x for x in self.data.results if not (x.notes and x.notes[0].startswith("Давхардсан цэс"))]
        if st and st.dialogs and real:
            prev = real[-1]
            prev.findings.extend(replace(classify_popup(d, self.cfg, is_toast=False), after_close=True)
                                 for d in st.dialogs)
            if not prev.screenshot:
                prev.screenshot = await self._screenshot(self._shot_name())
            if prev.status == M.PASS:
                self.counts[M.PASS] = self.counts.get(M.PASS, 1) - 1
                self.counts[M.FAIL] = self.counts.get(M.FAIL, 0) + 1
                prev.status = M.FAIL
            self.log(f"«{prev.item.name}»-ийн дараа алдааны цонх гарсан → {M.FAIL}: {st.dialogs[0][:120]}", "warn")
        await self._close_dialogs()
        await self._clear_toasts()
        # өмнөх цэсүүдийн хаагдаж амжаагүй табуудыг л хаана
        await self._close_tabs(list(self._opened_tabs))

    async def _eval(self, js: str, arg: Any) -> Any:
        try:
            return await self.page.evaluate(js, arg)
        except Exception:
            return None

    async def _screenshot(self, name: str) -> str:
        path = self.run_dir / "shots" / f"{name}.png"
        try:
            await self.page.screenshot(path=str(path), timeout=5000)
            # Windows дээр Path нь "\\" ашигладаг — URL/JSON-д үргэлж "/" байх ёстой.
            return path.relative_to(self.run_dir).as_posix()
        except Exception:
            return ""

    # ---------------------------------------------------------- item check
    async def _check_item(self, item: MenuItem, confirm: bool = False) -> ItemResult:
        if item.skip:
            return ItemResult(item, status=M.BLOCKED, blocked_reason="skip_items жагсаалтад орсон тул алгассан",
                              checked_at=now_iso())
        if item.risky and not self.opt.check_risky:
            return ItemResult(item, status=M.BLOCKED,
                              blocked_reason="Эрсдэлтэй цэс (risky_items) — тохиргоогоор шалгаагүй",
                              checked_at=now_iso())
        timeout = self.cfg["timing"]["item_timeout_s"]
        r: ItemResult | None = None
        flags = _Flags()
        deploy_retried = False
        attempt = 0
        while attempt < 3:
            attempt += 1
            try:
                await self._ensure_ready(item.name)
                r, flags = await asyncio.wait_for(self._check_once(item, attempt, deploy_retried), timeout)
            except asyncio.TimeoutError:
                r = ItemResult(item, status=M.BLOCKED, attempts=attempt, checked_at=now_iso(),
                               blocked_reason=f"Timeout ({timeout} сек)")
                r.findings.append(Finding(M.TIMEOUT, f"Цэсийн шалгалт {timeout} сек-д дууссангүй"))
                r.screenshot = await self._screenshot(self._shot_name())
                flags = _Flags()
                await self._recover_after_timeout(item)
                break
            except Exception as e:
                self.log(f"Шалгалтын алдаа ({item.name}): {e!r}", "warn")
                r = ItemResult(item, status=M.BLOCKED, attempts=attempt, checked_at=now_iso(),
                               blocked_reason=f"Шалгалтын алдаа: {str(e)[:150]}")
                flags = _Flags()
                await self._recover_after_timeout(item)
                break
            if flags.logout:
                await self._relogin(item.name)
                if attempt == 1:
                    continue      # энэ цэсийг нэг удаа дахин шалгана
                r.status, r.blocked_reason = M.BLOCKED, "Logout-ын улмаас шалгаж чадсангүй"
                break
            if flags.deploy and not deploy_retried:
                urls = ", ".join(flags.deploy_urls) or "тодорхойгүй URL"
                # Дахин оролдвол ихэнхдээ шууд шийдэгддэг тул live лог/дэлгэцэд шуугиулахгүй —
                # run.log-д бичигдэнэ, эцсийн TC-ийн тэмдэглэлд ч (r.notes) харагдана.
                self.log(f"Түр 503 ({urls}) — хүлээгээд дахин оролдоно", "warn", to_ui=False)
                deploy_retried = True
                await self._sleep(self.cfg["timing"]["deploy_retry_wait_ms"] / 1000)
                attempt -= 1      # энэ оролдлогыг тоолохгүй
                continue
            break
        assert r is not None
        if deploy_retried and not any(f.kind == M.HTTP_5XX for f in r.findings):
            r.notes.append("Түр: 503 ажиглагдсан ч дахин оролдлогоор давтагдаагүй")
            self.data.temp_5xx.append(item.name)
        if flags.logout_after_close:
            await self._relogin(item.name)
        if flags.crash:
            await self._recover_crash(item.name)
        elif flags.reload:
            await wait_login_or_app(self.page, self.cfg, self.cfg["timing"]["app_ready_timeout_ms"],
                                    self.cancel_event)
            self.current_module = None
        if confirm:
            r.screenshot = ""
        return r

    async def _recover_after_timeout(self, item: MenuItem) -> None:
        try:
            await self.page.reload(wait_until="domcontentloaded")
            await wait_login_or_app(self.page, self.cfg, self.cfg["timing"]["app_ready_timeout_ms"],
                                    self.cancel_event)
        except Exception:
            pass
        self.current_module = None

    def _shot_name(self) -> str:
        self._shot_seq += 1
        return f"item-{self._shot_seq:04d}"

    def _new_tabs(self, base: list[str], now: list[str]) -> list[str]:
        remaining = list(base)
        out = []
        for t in now:
            if t in remaining:
                remaining.remove(t)
            elif t not in self.protected:
                out.append(t)
        return out

    def _lap(self, name: str) -> None:
        now = time.monotonic()
        self._laps.append((name, now - self._lap_t))
        self._lap_t = now

    async def _check_once(self, item: MenuItem, attempt: int, deploy_retry: bool) -> tuple[ItemResult, _Flags]:
        """Цэс шалгах (хугацааны задаргааг run.log-д бичнэ: аль үе шат удаан байгааг харахад)."""
        self._laps: list[tuple[str, float]] = []
        t0 = self._lap_t = time.monotonic()
        try:
            return await self._check_once_body(item, attempt, deploy_retry)
        finally:
            parts = " ".join(f"{n}={d:.2f}" for n, d in self._laps if d >= 0.05)
            self.log(f"⏱ {item.name}: {time.monotonic() - t0:.1f}с | {parts}", to_ui=False)

    async def _check_once_body(self, item: MenuItem, attempt: int, deploy_retry: bool) -> tuple[ItemResult, _Flags]:
        page, cfg = self.page, self.cfg
        flags = _Flags()
        r = ItemResult(item, attempts=attempt, checked_at=now_iso())

        # a. цэвэрлэх
        await self._cleanup()
        self._lap("cleanup")
        # b. модуль → хавтас → навч (href/нэрээр)
        if self.current_module != item.module:
            await discovery.activate_module(page, cfg, item.module)
            self.current_module = item.module
            self._lap("module")
        found = await discovery.mark_leaf(page, cfg, item)
        self._lap("locate")
        if not found:
            st = await snapshot(page, cfg)
            if st and is_login_url(st.url, cfg):
                flags.logout = True
                return r, flags
            r.status, r.blocked_reason = M.BLOCKED, "Цэс олдсонгүй (href/нэрээр хайсан)"
            return r, flags
        base = await snapshot(page, cfg)
        if base is None:
            base = await snapshot(page, cfg)
        base_tabs = base.tab_texts() if base else []
        origin0 = base.timeOrigin if base else 0
        self.col.clear()

        # c. дарах → шинэ таб / модал / timeout
        self._lap("prep")
        t_click = time.monotonic()
        await self._click_target()
        self._lap("click")
        deadline = t_click + self.wait_s
        outcome = None
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                break
            st = await snapshot(page, cfg)
            if st is None:
                await self._sleep(0.1)
                continue
            if is_login_url(st.url, cfg):
                flags.logout = True
                return r, flags
            if st.timeOrigin != origin0:
                flags.reload = True
                break
            if st.dead:
                flags.crash = True
                break
            if self._new_tabs(base_tabs, st.tab_texts()):
                outcome = "tab"
                break
            if st.dialogs:
                outcome = "dialog"
                break
            await self._sleep(0.04)
        self._lap("tab_wait")

        # "Loading…" гацалт + ачаалах хугацаа
        if outcome == "tab":
            while True:
                if self.cancel_event.is_set():
                    break
                st = await snapshot(page, cfg)
                el = time.monotonic() - t_click
                if st and not st.loading:
                    r.load_ms = int(el * 1000)
                    break
                # Grid/өгөгдөл ачаалахад сервер алдаа шидвэл зарим DevExpress компонент өөрийн
                # loading spinner-оо арилгадаггүй ч алдааны цонх (dialog) дараа нь гарч ирдэг.
                # Dialog аль хэдийн харагдаж байгаа бол "Loading gацсан" гэж худал Blocked
                # болгохгүй — доорх d. хэсэгт бодит алдааны текстийг нь цуглуулна.
                if st and st.dialogs:
                    break
                if el > self.loading_s:
                    r.findings.append(Finding(M.LOADING_STUCK, f"\"Loading…\" {int(self.loading_s)} сек-ээс удаан"))
                    r.blocked_reason = f"\"Loading…\" {int(self.loading_s)} сек-ээс удаан гацсан"
                    break
                if st and (st.dead or is_login_url(st.url, cfg) or st.timeOrigin != origin0):
                    break
                await self._sleep(0.05)
        self._lap("load")

        # Хоцорсон алдаа/toast гарах хугацааг өгнө. Таб нээгдэж, ачаалал дууссан (outcome=="tab",
        # loading байхгүй) бол тогтмол хүлээхгүй — сүлжээ тайван болмогц (хүсэлт дууссан, 0.3с
        # шинэ идэвхгүй) шууд үргэлжилнэ; дээд хязгаар нь settle_ms. Юу ч нээгдээгүй (NO_TAB
        # гэх мэт) бол wait_ms бүтнээр хүлээж баттай дүгнэнэ.
        if not flags.crash and not flags.reload:
            loaded_ok = outcome == "tab" and not r.blocked_reason and not r.findings
            if loaded_ok:
                end = min(deadline, time.monotonic() + self.settle_s)
                while time.monotonic() < end and not self.col.quiet_for(0.3):
                    await self._sleep(0.04)
            else:
                await self._sleep(deadline - time.monotonic())
        self._lap("settle")
        st = await snapshot(page, cfg)
        if st is None or flags.reload:
            await wait_login_or_app(page, cfg, cfg["timing"]["app_ready_timeout_ms"], self.cancel_event)
            st = await snapshot(page, cfg)
        if st and is_login_url(st.url, cfg):
            flags.logout = True
            return r, flags
        if st and st.timeOrigin != origin0:
            flags.reload = True

        # d. DOM-оос цуглуулах
        new_names: list[str] = []
        if st:
            new = new_names = self._new_tabs(base_tabs, st.tab_texts())
            r.tab_opened = bool(new) and not flags.reload
            active = st.active_tab
            r.tab_name = active if active and active not in self.protected else (new[-1] if new else "")
            for d in st.dialogs:
                r.findings.append(classify_popup(d, cfg, is_toast=False))
            for t in st.toasts:
                r.findings.append(classify_popup(t, cfg, is_toast=True))
            forbidden = st.forbidden or r.tab_name == self.sel["forbidden_tab_name"]
            if forbidden:
                r.findings.append(Finding(M.FORBIDDEN, "Sorry, you are not authorized to access this page (403)"))
            if st.dead:
                flags.crash = True
            if (r.tab_opened and not forbidden and not names_match(r.tab_name, item.name)):
                r.findings.append(tab_mismatch_finding(r.tab_name, item.name))
            if not r.tab_opened and not st.dialogs and not flags.reload and not flags.crash and not forbidden:
                r.findings.append(Finding(M.NO_TAB, f"«{item.name}» дарахад таб нээгдсэнгүй"))
        if flags.reload:
            r.findings.append(Finding(M.PAGE_RELOAD, "Цэс дарахад SPA бүтэн дахин ачаалагдаж, табууд хаагдсан"))
        if self.col.has_crash(self.col.since(t_click)):
            flags.crash = True
        self._lap("collect")

        # d2. цэсний үйлдлийн бүртгэл (товч/grid-ийг зөвхөн уншина — юу ч дарахгүй)
        if ((cfg.get("crud_scan") or {}).get("enabled", True) and r.tab_opened and not flags.crash
                and not flags.reload and not (st and st.dialogs)
                and not any(f.kind == M.FORBIDDEN for f in r.findings)):
            r.caps = await scan_actions(page, cfg)
            if r.caps.get("blank"):
                # Агуулга хоцорч зурагдаж болно — нэг удаа хүлээгээд дахин шалгана
                await self._sleep(1.5)
                r.caps = await scan_actions(page, cfg)
                if r.caps.get("blank"):
                    r.findings.append(Finding(M.BLANK_PAGE, f"«{item.name}» таб нээгдсэн ч агуулга хоосон (цагаан дэлгэц)"))
            self._lap("scan")
        slow_ms = int((cfg.get("timing") or {}).get("slow_menu_ms", 5000))
        if r.load_ms is not None and r.load_ms > slow_ms:
            r.findings.append(Finding(M.SLOW_LOAD, f"«{item.name}» {r.load_ms / 1000:.1f} сек-д ачааллав "
                                                   f"(босго {slow_ms / 1000:.0f} сек)"))

        # e. screenshot (модал/таб хаагдахаас өмнө). Сүлжээ/консолын алдаа (HTTP 4xx/5xx, console error) нь
        # доор табыг хаасны ДАРАА цуглуулагддаг тул энд урьдчилан шалгана — эс бөгөөс зөвхөн ийм алдаатай цэс
        # (ж: тайлангийн CSS 404) screenshot-гүй үлддэг байсан
        net_err = bool(self.col.findings_from(self.col.since(t_click), include_deploy=deploy_retry))
        if r.findings or flags.crash or net_err:
            r.screenshot = await self._screenshot(self._shot_name())
            self._lap("shot")

        # f. хаах → хоцорсон уналт
        t_close = time.monotonic()
        if not flags.crash and not flags.reload:
            await self._close_dialogs()
            st_now = await snapshot(page, cfg)
            opened = self._new_tabs(base_tabs, st_now.tab_texts()) if st_now else list(new_names)
            self._opened_tabs.update(opened)
            await self._close_tabs(opened)
            await self._clear_toasts()
            self._lap("close")
            await self._sleep(self.post_close_s)
            self._lap("post_close")
            st2 = await snapshot(page, cfg)
            if st2 and st2.dead:
                flags.crash = True
            if st2 and st2.dialogs and not st2.dead:
                # таб хаасны дараа гарсан алдааны цонх → энэ цэс Fail; ✕-ээр хаагаад үргэлжилнэ
                for d in st2.dialogs:
                    r.findings.append(replace(classify_popup(d, cfg, is_toast=False), after_close=True))
                if not r.screenshot:
                    r.screenshot = await self._screenshot(self._shot_name())
                await self._close_dialogs()
            if st2 and is_login_url(st2.url, cfg):
                flags.logout_after_close = True
            elif st2 and st2.timeOrigin != origin0:
                flags.reload = True
                r.findings.append(Finding(M.PAGE_RELOAD, "Таб хаасны дараа SPA бүтэн дахин ачаалагдсан",
                                          after_close=True))
            if self.col.has_crash(self.col.since(t_close)):
                flags.crash = True
                if not r.screenshot:
                    r.screenshot = await self._screenshot(self._shot_name())

        self._lap("after_close")
        await self.col.drain()
        self._lap("drain")
        events = self.col.since(t_click)
        r.findings.extend(self.col.findings_from(events, after_close_t=t_close,
                                                 include_deploy=deploy_retry))
        flags.deploy = self.col.deploy_5xx(events)
        if flags.deploy:
            flags.deploy_urls = sorted({strip_query(e["url"]) for e in events
                                        if e["type"] == "http" and e.get("status") == 503})
        # давхардал арилгах
        uniq, seen = [], set()
        for f in r.findings:
            k = (f.kind, f.text[:120], f.url)
            if k not in seen:
                seen.add(k)
                uniq.append(f)
        r.findings = uniq

        r.status = smoke_status(r, cfg)
        return r, flags
