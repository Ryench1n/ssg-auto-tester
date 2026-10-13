"""Илрүүлэгчид: console / pageerror / HTTP / navigation listener + DOM төлөвийн snapshot."""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from app import models as M
from app.models import Finding

# --------------------------------------------------------------------------
# Хөтөчид суулгах скрипт: runtime уналтыг __dead тугаар тэмдэглэнэ.
# --------------------------------------------------------------------------
INIT_SCRIPT = r"""
(() => {
  if (window.__ssgInstalled) return;
  window.__ssgInstalled = true;
  window.__dead = false;
  const pats = __PATTERNS__;
  const hit = (args) => {
    try {
      const t = Array.from(args).map(a => (a && a.stack) ? (a.message + '\n' + a.stack) : String(a)).join(' ');
      if (pats.some(p => t.includes(p))) window.__dead = true;
    } catch (e) {}
  };
  for (const m of ['error', 'warn', 'log', 'info']) {
    const orig = console[m] && console[m].bind(console);
    if (!orig) continue;
    console[m] = function (...a) { hit(a); return orig(...a); };
  }
  window.addEventListener('error', e => hit([e.message || '', (e.error && e.error.stack) || '']));
  window.addEventListener('unhandledrejection', e => hit([String(e.reason && (e.reason.stack || e.reason))]));
})();
"""


def init_script(cfg: dict) -> str:
    pats = (cfg.get("detect") or {}).get("runtime_crash") or []
    return INIT_SCRIPT.replace("__PATTERNS__", json.dumps(pats, ensure_ascii=False))


# --------------------------------------------------------------------------
# DOM snapshot
# --------------------------------------------------------------------------
# Алдааны цонх (модал) илрүүлэх — STATE_JS болон smoke.DIALOG_BTN_JS хоёулаа ашиглана.
# DevExpress-ийн DxPopup нь `role=dialog`/`.dxbl-modal`-гүй байж болно: зөвхөн
# <dxbl-popup-root> дотор <dxbl-modal-root> (дэлгэц бүрхэх overlay) үүсгэдэг. Үүнийг
# илрүүлэхгүй бол цонх нээлттэй үлдэж, дараагийн цэсний click-ийг "intercepts pointer
# events"-ээр хаадаг байсан. Давхар бүртгэхгүйн тулд хамгийн гадна талын элементийг л авна.
DIALOGS_FN = r"""
const __findDialogs = (sel) => {
  const v = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  // <dxbl-modal-root> өөрөө display:contents (хэмжээгүй) байж болно — хүүхэд нь харагдаж байвал болно
  const shown = (el) => v(el) || Array.from(el.children).some(v);
  let els = [];
  try { els = Array.from(document.querySelectorAll(sel.dialog)).filter(shown); } catch (e) {}
  return els.filter(e => !els.some(o => o !== e && o.contains(e)));
};
"""

STATE_JS = r"""
(sel) => {
  /*__DIALOGS__*/
  const vis = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const txt = (el) => (el.innerText || el.textContent || '').replace(/[ \t\u00a0]+/g, ' ')
      .replace(/\s*\n\s*/g, '\n').trim();
  const q = (s) => { try { return Array.from(document.querySelectorAll(s)); } catch (e) { return []; } };
  const tabs = q(sel.tab).filter(vis).map(t => ({
    text: txt(t).split('\n')[0].trim(),
    active: t.matches(sel.tab_active) || t.getAttribute('aria-selected') === 'true'
  }));
  // Цонхны текстээс товчны шошгыг (OK / Хадгалах ...) хасна — bug гарчиг цэвэр байх.
  // cloneNode ХЭРЭГЛЭХГҮЙ: DevExpress custom element-ийг хуулахад attributeChangedCallback-аас
  // «Error: readonly» консол алдаа үүсч (хэрэгслийн өөрийн үүсгэсэн хуурамч алдаа) тайланд ордог байсан.
  const BLOCK = /^(DIV|P|TR|LI|UL|OL|TABLE|TBODY|SECTION|HEADER|FOOTER|H[1-6]|BR|LABEL|FORM)$/;
  const dtxt = (d) => {
    const parts = []; let n = 0;
    const walk = (el) => {
      if (n > 6000) return;
      if (el.nodeType === 3) { parts.push(el.nodeValue); n += el.nodeValue.length; return; }
      if (el.nodeType !== 1 || el.matches('button, [role=button], .dxbl-btn, script, style, template')) return;
      if (el.hidden || (el.style && el.style.display === 'none')) return;
      const block = BLOCK.test(el.tagName);
      if (block) parts.push('\n');
      for (const c of el.childNodes) walk(c);
      if (block) parts.push('\n');
    };
    walk(d);
    return parts.join('').replace(/[ \t ]+/g, ' ').replace(/\s*\n\s*/g, '\n').trim() || txt(d);
  };
  const dialogs = __findDialogs(sel).map(dtxt).filter(Boolean);
  const toasts = q(sel.toast).filter(vis).map(txt).filter(Boolean);
  // <dxbl-loading-panel> нь агуулгыг ороосон wrapper тул үргэлж DOM-д, бүтэн хэмжээгээрээ
  // байдаг (ачаалал дуусах, дуусахаас үл хамааран) — зөвхөн vis()-ээр шалгавал үргэлж
  // "loading" гэж буруу дүгнэнэ. Харин ачаалал дуусмагц DevExpress түүн дээр нь
  // `dxbl-loaded` attribute-ийг (утгагүй, зөвхөн оршихуйгаараа) нэмдэг тул үүнийг
  // тэргүүлж шалгана: attribute байвал (ачаалал дууссан бол) loading гэж тооцохгүй.
  let loading = q(sel.loading).some((el) => {
    if (!vis(el)) return false;
    if (el.tagName === 'DXBL-LOADING-PANEL' && el.hasAttribute('dxbl-loaded')) return false;
    return true;
  });
  if (!loading && sel.loading_texts && sel.loading_texts.length) {
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    let n; let k = 0;
    while ((n = walker.nextNode()) && k < 20000) {
      k++;
      const v = (n.nodeValue || '').trim();
      if (v && sel.loading_texts.includes(v) && vis(n.parentElement)) { loading = true; break; }
    }
  }
  const bodyText = document.body ? document.body.innerText : '';
  const forbidden = (sel.forbidden_texts || []).some(t => bodyText.includes(t));
  return {
    url: location.href,
    path: location.pathname,
    timeOrigin: performance.timeOrigin,
    dead: !!window.__dead,
    tabs, dialogs, toasts, loading, forbidden,
    appReady: !!document.querySelector(sel.app_ready),
    hasPassword: !!document.querySelector(sel.password),
  };
}
""".replace("/*__DIALOGS__*/", DIALOGS_FN)


@dataclass
class PageState:
    url: str = ""
    path: str = ""
    timeOrigin: float = 0.0
    dead: bool = False
    tabs: list[dict] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    toasts: list[str] = field(default_factory=list)
    loading: bool = False
    forbidden: bool = False
    appReady: bool = False
    hasPassword: bool = False

    @property
    def active_tab(self) -> str:
        for t in self.tabs:
            if t.get("active"):
                return t.get("text", "")
        return ""

    def tab_texts(self) -> list[str]:
        return [t.get("text", "") for t in self.tabs]


def selector_payload(cfg: dict) -> dict:
    s = cfg["selectors"]
    return {k: s[k] for k in ("tab", "tab_active", "dialog", "toast", "loading", "loading_texts",
                              "forbidden_texts", "app_ready", "password")}


async def snapshot(page, cfg: dict) -> PageState | None:
    try:
        data = await page.evaluate(STATE_JS, selector_payload(cfg))
        return PageState(**data)
    except Exception:
        # Navigation/reload явагдаж байх үед execution context устдаг.
        return None


# --------------------------------------------------------------------------
# Нууц мэдээлэл шүүх
# --------------------------------------------------------------------------
_JWT = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[\w.\-~+/=]+")
_SENSITIVE_Q = re.compile(r"(?i)(token|access_token|refresh_token|password|pwd|code|auth)=[^&\s]+")


def make_sanitizer(secrets: list[str]) -> Callable[[str], str]:
    secrets = [s for s in secrets if s and len(s) >= 3]

    def clean(text: Any) -> str:
        t = "" if text is None else str(text)
        for s in secrets:
            t = t.replace(s, "***")
        t = _JWT.sub("<jwt>", t)
        t = _BEARER.sub(r"\1<redacted>", t)
        t = _SENSITIVE_Q.sub(r"\1=<redacted>", t)
        return t

    return clean


def strip_query(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception:
        return url


def _retryable(e: dict) -> bool:
    """Жинхэнэ (хэрэглэгчийн хүсэлтийн) 503 нь HTTP стандартаараа "түр зуур" гэсэн утгатай —
    deploy (msdeploy) тэмдэгтэй эсэхээс үл хамааран нэг удаа дахин оролдоно. Хөтчийн
    speculative prefetch-ийн 503 (Cloudflare Speed Brain) үүнд ОРОХГҮЙ — тэр алдаа биш."""
    if e.get("speculative"):
        return False
    return bool(e.get("deploy")) or e.get("status") == 503


# --------------------------------------------------------------------------
# Listener-үүд
# --------------------------------------------------------------------------
class Collector:
    """page-ийн бүх event-ийг цуглуулна. Цэс бүрийн өмнө clear() → зөвхөн тэр цэсийн event."""

    def __init__(self, page, cfg: dict, sanitize: Callable[[str], str],
                 raw_sink: Callable[[dict], None] | None = None):
        self.page = page
        self.cfg = cfg
        self.det = cfg.get("detect") or {}
        self.sanitize = sanitize
        self.raw_sink = raw_sink
        self.events: list[dict] = []
        self.main_navigations = 0
        self._tasks: set[asyncio.Task] = set()
        self._open_reqs: dict = {}            # дуусаагүй хүсэлт → эхэлсэн цаг (сүлжээ тайван эсэхийг мэдэхэд)
        self.last_net = time.monotonic()
        # Үүнээс удаан нээлттэй хүсэлт = арын (long-poll / polling) холболт — «ачаалал» гэж тооцохгүй.
        # SmartApp-д байнга ийм хүсэлт байдаг тул тооцвол сүлжээ хэзээ ч тайван болохгүй.
        self.background_s = float((cfg.get("timing") or {}).get("background_request_ms", 4000)) / 1000
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        page.on("response", self._on_response)
        page.on("framenavigated", self._on_nav)
        page.on("request", self._on_req_start)
        page.on("requestfinished", self._on_req_end)
        page.on("requestfailed", self._on_req_end)

    # -- сүлжээний идэвх: тогтмол sleep-ийн оронд "тайван болмогц" үргэлжлүүлэхэд ---------
    def _on_req_start(self, req) -> None:
        try:
            if req.resource_type in ("websocket", "eventsource"):
                return          # урт настай холболт хэзээ ч "дуусдаггүй"
            self._open_reqs[req] = time.monotonic()
            self.last_net = time.monotonic()
        except Exception:
            pass

    def _on_req_end(self, req) -> None:
        started = self._open_reqs.pop(req, None)
        now = time.monotonic()
        if started is None or now - started < self.background_s:
            self.last_net = now          # арын long-poll дуусах нь «идэвх» биш

    def quiet_for(self, seconds: float) -> bool:
        """Дуусаагүй (арын long-poll-оос бусад) хүсэлтгүй бөгөөд сүүлийн сүлжээний идэвхээс `seconds` өнгөрсөн эсэх."""
        now = time.monotonic()
        if any(now - t < self.background_s for t in self._open_reqs.values()):
            return False
        return (now - self.last_net) >= seconds

    # -- helpers ------------------------------------------------------------
    def _push(self, ev: dict) -> None:
        ev["t"] = time.monotonic()
        ev["text"] = self.sanitize(ev.get("text", ""))[:4000]
        if "url" in ev:
            ev["url"] = self.sanitize(strip_query(ev["url"]))
        self.events.append(ev)
        if self.raw_sink:
            self.raw_sink({k: v for k, v in ev.items() if k != "t"})

    def clear(self) -> None:
        self.events = []

    def since(self, t0: float) -> list[dict]:
        return [e for e in self.events if e["t"] >= t0]

    def _ignored(self, text: str) -> bool:
        return (any(p in text for p in (self.det.get("ignore_console") or []))
                or text.strip() in (self.det.get("ignore_console_exact") or []))

    # -- handlers -----------------------------------------------------------
    def _on_console(self, msg) -> None:
        try:
            text = msg.text
            mtype = msg.type
        except Exception:
            return
        crash = any(p in text for p in (self.det.get("runtime_crash") or []))
        if mtype != "error" and not crash:
            return
        if not crash and (self._ignored(text) or text.startswith("Failed to load resource")):
            return
        self._push({"type": "console", "level": mtype, "text": text})

    def _on_pageerror(self, err) -> None:
        text = f"{getattr(err, 'message', err)}\n{getattr(err, 'stack', '') or ''}"
        if self._ignored(text):
            return
        self._push({"type": "pageerror", "text": text})

    def _on_response(self, resp) -> None:
        try:
            status = resp.status
        except Exception:
            return
        if status < 400:
            return
        url = resp.url
        if self._ignored(url):
            return
        ev = {"type": "http", "status": status, "url": url, "text": f"HTTP {status}", "deploy": False,
              "speculative": False}
        self._push(ev)
        task = asyncio.ensure_future(self._inspect(resp, ev))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _inspect(self, resp, ev: dict) -> None:
        """≥400 хариуг ангилна: (1) хөтчийн speculative prefetch/prerender мөн эсэх,
        (2) 5xx бол msdeploy тэмдэгтэй эсэх. drain() эдгээрийг findings-ээс өмнө хүлээнэ."""
        # Cloudflare Speed Brain (`Speculation-Rules: /cdn-cgi/speculation`) нь Chrome-д цэсний
        # линк бүрийг дарах (pointerdown) агшинд `Sec-Purpose: prefetch`-ээр урьдчилан татуулдаг ба
        # Cloudflare тэр хүсэлтэд 503 буцаадаг. Хэрэглэгчид огт харагддаггүй, апп-ын алдаа биш.
        try:
            hdrs = await asyncio.wait_for(resp.request.all_headers(), 0.8)
            purpose = f"{hdrs.get('sec-purpose', '')} {hdrs.get('purpose', '')}".lower()
            if "prefetch" in purpose or "prerender" in purpose:
                ev["speculative"] = True
                if self.raw_sink:
                    self.raw_sink({"type": "note", "url": ev["url"],
                                   "text": f"HTTP {ev['status']} — speculative prefetch (үл тооно)"})
                return
        except Exception:
            pass
        if ev["status"] >= 500:
            try:
                body = (await asyncio.wait_for(resp.text(), 1.5))[:5000]
                if (self.det.get("deploy_marker") or "msdeploy") in body.lower():
                    ev["deploy"] = True
            except Exception:
                pass

    def _on_nav(self, frame) -> None:
        try:
            if frame == self.page.main_frame:
                self.main_navigations += 1
                self._push({"type": "nav", "url": frame.url, "text": "navigate"})
        except Exception:
            pass

    # -- ангилал ------------------------------------------------------------
    def has_crash(self, events: list[dict]) -> bool:
        pats = self.det.get("runtime_crash") or []
        return any(e["type"] in ("console", "pageerror") and any(p in e["text"] for p in pats)
                   for e in events)

    def findings_from(self, events: list[dict], after_close_t: float | None = None,
                      include_deploy: bool = False) -> list[Finding]:
        det = self.det
        out: list[Finding] = []
        seen: set[tuple] = set()
        for e in events:
            after = after_close_t is not None and e["t"] >= after_close_t
            f: Finding | None = None
            if e["type"] in ("console", "pageerror"):
                text = e["text"]
                if any(p in text for p in det.get("runtime_crash") or []):
                    f = Finding(M.RUNTIME_CRASH, text.splitlines()[0][:300], detail=text, after_close=after)
                elif any(p in text for p in det.get("renderer_exception") or []):
                    f = Finding(M.RENDERER_EXCEPTION, text.splitlines()[0][:300], detail=text, after_close=after)
                elif any(p in text for p in det.get("auth_error") or []):
                    f = Finding(M.AUTH_ERROR, text.splitlines()[0][:300], detail=text, after_close=after)
                else:
                    f = Finding(M.CONSOLE_ERROR, text.splitlines()[0][:300] if text else "",
                                detail=text, after_close=after)
            elif e["type"] == "http":
                if e.get("speculative"):
                    continue    # хөтчийн prefetch — хэрэглэгчид нөлөөгүй, алдаа биш
                st = int(e["status"])
                if st >= 500:
                    if _retryable(e) and not include_deploy:
                        continue  # түр 5xx (503 эсвэл msdeploy) — smoke.py хүлээгээд дахин оролдоно
                    f = Finding(M.HTTP_5XX, f"HTTP {st}", status=st, url=e["url"], after_close=after)
                elif st in (401,) and any(p in e["url"] for p in det.get("auth_error") or []):
                    f = Finding(M.AUTH_ERROR, f"HTTP {st} {e['url']}", status=st, url=e["url"])
                elif st == 403:
                    f = Finding(M.FORBIDDEN, f"HTTP 403 {strip_query(e['url'])}", status=st, url=e["url"])
                else:
                    f = Finding(M.HTTP_4XX, f"HTTP {st}", status=st, url=e["url"], after_close=after)
            if f is None:
                continue
            key = (f.kind, f.text[:120], f.url, f.status)
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out

    async def drain(self, timeout: float = 1.5) -> None:
        """5xx хариуны body уншилт (msdeploy тэмдэглэгээ) дуусахыг хүлээнэ."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    def deploy_5xx(self, events: list[dict]) -> bool:
        return any(e["type"] == "http" and _retryable(e) for e in events)


# --------------------------------------------------------------------------
# DOM-оос илрэл
# --------------------------------------------------------------------------
_BTN_TAIL = re.compile(r"(\n\s*(OK|Ok|Хаах|Close|×|Болих)\s*)+$")


def dialog_text(t: str) -> str:
    return _BTN_TAIL.sub("", (t or "").strip()).strip()


def classify_popup(text: str, cfg: dict, is_toast: bool) -> Finding:
    det = cfg.get("detect") or {}
    t = dialog_text(text)
    low = t.lower()
    if any(p.lower() in low for p in det.get("auth_error") or []):
        return Finding(M.AUTH_ERROR, t[:500])
    error_words = ["алдаа", "error", "exception", "failed"] + [p.lower() for p in det.get("sql_error") or []]
    if not is_toast:
        # «Анхаар / Анхааруулга …» гарчигтай, алдааны үггүй цонх = тохиргоо/сонголтын анхааруулга (ж: «Кассирын
        # агуулахын тохиргоо хийгдээгүй байна!») — ижил текст toast-оор гарвал Minor байсан тул нэгтгэнэ
        titles = [x.lower() for x in ((cfg.get("flow") or {}).get("validation_titles") or [])]
        first = low.split("\n")[0].strip()
        if titles and any(first.startswith(x) for x in titles) and not any(w in low for w in error_words):
            return Finding(M.WARNING_TOAST, t[:500])
        return Finding(M.SYSTEM_ERROR, t[:500])
    if any(w in low for w in error_words):
        return Finding(M.SYSTEM_ERROR, t[:500])
    return Finding(M.WARNING_TOAST, t[:500])


def tab_mismatch_finding(tab: str, menu: str) -> Finding:
    """Табын гарчиг цэсийн нэрээс зөрөх 2 ангилал:
      - бичгийн/орчуулгын ялгаа (TAB_NAME_MISMATCH, Minor): гарчиг нь орчуулагдаагүй латин нэр (SmartJobQueueList),
        эсвэл үгийн тоо ижил, үг бүр бараг ижил (нярвын ↔ няравын, хуудасны ↔ хуудсын);
      - өөр хуудас байж болзошгүй (TAB_WRONG_PAGE, Major): өөр үгтэй («Гүйлгээний жагсаалт» ↔ «Баримтын жагсаалт»,
        «/байршил, бараагаар/» ↔ «/бараагаар/»)."""
    import difflib
    text = f"Таб: «{tab}» ≠ цэс: «{menu}»"
    words = lambda s: re.sub(r"[^\w]+", " ", (s or "").lower()).split()
    a, b = words(tab), words(menu)
    latin_id = bool(re.fullmatch(r"[A-Za-z0-9_]+", (tab or "").strip())) and bool(re.search(r"[А-Яа-яӨөҮүЁё]", menu or ""))
    same = len(a) == len(b) and a and all(difflib.SequenceMatcher(None, x, y).ratio() >= 0.7 for x, y in zip(a, b))
    if latin_id:
        return Finding(M.TAB_NAME_MISMATCH, text + " — гарчиг орчуулагдаагүй")
    if same:
        return Finding(M.TAB_NAME_MISMATCH, text + " — бичгийн ялгаа")
    return Finding(M.TAB_WRONG_PAGE, text + " — өөр нэртэй хуудас (буруу хуудас нээгдэж болзошгүй)")


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def names_match(tab: str, menu: str) -> bool:
    a, b = norm_name(tab), norm_name(menu)
    return bool(a and b and (a == b or a in b or b in a))


def tab_is(tab: str, menu: str) -> bool:
    """Идэвхтэй таб яг энэ цэсийнх эсэх (хатуу). names_match нь «Түлхүүр» ↔ «Түлхүүрийн төрөл», «Меню» ↔
    «Менюны загвар»-ыг ижил гэж үздэг тул цэс нээгдсэнийг шалгахад ашиглахгүй. Таслагдсан («…») табыг зөвшөөрнө."""
    a, b = norm_name(tab), norm_name(menu)
    if not a or not b:
        return False
    if a == b:
        return True
    cut = re.sub(r"(\.\.\.|…)$", "", a).strip()
    return cut != a and len(cut) >= 3 and b.startswith(cut)
