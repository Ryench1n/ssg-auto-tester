"""Flow engine: YAML-д тодорхойлсон алхмуудыг дарааллаар ажиллуулж, өгөгдлийг дамжуулна.

Алхам бүр `@step("module.action")`-аар бүртгэгдэнэ. Алхам нь `async fn(env, args) -> dict`:
буцаасан dict нь FlowContext.data-д нэмэгдэж, дараагийн алхмууд `{key}` хэлбэрээр ашиглана.

Browser/page/Collector/нэвтрэлтийг pipeline эзэмшинэ — FlowEngine тэдгээрийг гаднаас хүлээн авна.
Нэг pipeline-ын бүх flow нэг FlowContext (нэг run_tag) хуваалцана.

Дүрэм:
  - Алхмын аргументэд байгаа `{key}` бүр тухайн алхмын `requires` — байхгүй бол Blocked.
  - Нэг алхам Fail/Blocked болбол дараагийнх нь Blocked (`always: true` алхмаас бусад —
    цэвэрлэх алхам шаардлагатай өгөгдөл нь байвал ажиллана).
  - Алхмын явцад session тасарвал (logout) pipeline түр зогсож гар нэвтрэлт хүлээнэ, дараа нь
    тасарсан алхмыг 1 удаа дахин ажиллуулна. Хугацаа хэтэрвэл үлдсэн алхмууд Blocked.
  - Өгөгдөл өөрчилдөг тул зөвхөн `flow.allowed_hosts`-д таарах тест орчинд ажиллана.
"""
from __future__ import annotations

import asyncio
import fnmatch
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlparse

from app import models as M
from app.flows.context import (FlowContext, FlowRun, MissingData, StepBlocked, StepFailed, StepResult,
                               placeholders)
from app.runner import browser as browser_mod
from app.runner.detectors import Collector
from app.runner.runlog import RunLog, now_iso


def hard_message(f) -> str:
    """Ноцтой алдааны ойлгомжтой мессеж: Blazor «crit: …WebAssemblyRenderer[100]» гэхийн оронд жинхэнэ шалтгаан."""
    m = re.search(r"Unhandled exception rendering component:\s*(.+)", f.detail or "")
    if m:
        return f"Blazor компонент унав: {m.group(1).strip()[:300]}"
    if f.kind == M.RUNTIME_CRASH:
        return f".NET runtime уналт (апп зогссон): {f.text[:250]}"
    return f"{f.kind}: {f.text}"

if TYPE_CHECKING:
    from app.pipeline.session import SessionGuard

Emit = Callable[[str, dict], None]
StepFn = Callable[["FlowEngine", dict], Awaitable[dict | None]]

# Алхмын явцад гарвал шууд Fail болгох илрэл (консолын жижиг алдаа, 4xx нь тэмдэглэл л болно)
HARD_KINDS = {M.RUNTIME_CRASH, M.RENDERER_EXCEPTION, M.HTTP_5XX, M.AUTH_ERROR}


@dataclass
class StepDef:
    fn: StepFn
    needs_session: bool = True


STEPS: dict[str, StepDef] = {}


def step(name: str, needs_session: bool = True):
    def deco(fn: StepFn) -> StepFn:
        STEPS[name] = StepDef(fn, needs_session)
        return fn
    return deco


class FlowConfigError(ValueError):
    pass


def _register_steps() -> None:
    from app.flows import steps as _steps  # noqa: F401  (import-ын гаж нөлөө: алхмууд бүртгэгдэнэ)


def host_allowed(url: str, cfg: dict) -> bool:
    host = (urlparse(url).hostname or "").lower()
    pats = ((cfg.get("flow") or {}).get("allowed_hosts")) or []
    return any(fnmatch.fnmatch(host, p) for p in pats)


def host_reason(url: str, cfg: dict) -> str:
    """allowed бол "" — эс бөгөөс UI/400-д харуулах шалтгаан."""
    if host_allowed(url, cfg):
        return ""
    pats = ((cfg.get("flow") or {}).get("allowed_hosts")) or []
    return (f"{urlparse(url).hostname or url}: CRUD урсгал өгөгдөл өөрчилдөг тул зөвхөн тест орчинд "
            f"(flow.allowed_hosts: {', '.join(pats) or '—'}) ажиллана")


def load_flow(cfg: dict, name: str) -> dict:
    _register_steps()
    flows = cfg.get("flows") or {}
    if name not in flows:
        raise FlowConfigError(f"'{name}' flow тохиргоонд алга. Байгаа: {', '.join(flows) or '—'}")
    spec = flows[name]
    if isinstance(spec, list):
        spec = {"steps": spec}
    for i, s in enumerate(spec.get("steps") or [], 1):
        if s.get("uses") not in STEPS:
            raise FlowConfigError(f"#{i} алхам: '{s.get('uses')}' гэсэн алхам бүртгэгдээгүй. "
                                  f"Байгаа: {', '.join(sorted(STEPS))}")
    return spec


def flow_code(name: str, spec: dict | None = None) -> str:
    """TC-FLOW-<code>: YAML-ын `code:` эсвэл нэрийн үгсийн эхний үсэг (customer_group_crud → CGC)."""
    code = str((spec or {}).get("code") or "").strip()
    if not code:
        words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
        code = "".join(w[0] for w in words) if len(words) > 1 else (words[0][:6] if words else "FLOW")
    return re.sub(r"[^A-Za-z0-9]", "", code).upper() or "FLOW"


def flow_description(spec: dict) -> str:
    if spec.get("description"):
        return str(spec["description"])
    names = [s.get("name") or s.get("uses") for s in spec.get("steps") or []]
    text = " → ".join(names)
    return text if len(text) <= 160 else text[:157] + "…"


class FlowEngine:
    def __init__(self, url: str, flow_name: str, cfg: dict, run_dir: Path, *, ctx: FlowContext,
                 runlog: RunLog, session: "SessionGuard", cancel_event: asyncio.Event,
                 emit: Emit | None = None, run_id: str = "", code: str = "", meta: dict | None = None):
        if not host_allowed(url, cfg):
            raise FlowConfigError(host_reason(url, cfg))       # backend-ийн 2 дахь хамгаалалт
        self.url = url
        self.cfg = cfg
        self.flow_name = flow_name
        self.spec = load_flow(cfg, flow_name)
        self.run_dir = Path(run_dir)
        (self.run_dir / "shots").mkdir(parents=True, exist_ok=True)
        self.runlog = runlog
        self.sanitize = runlog.sanitize
        self.session = session
        self.cancel_event = cancel_event
        self.emit_cb = emit or (lambda t, d: None)
        self.ctx = ctx
        self.run = FlowRun(run_id, url, flow_name, ctx.run_tag, code=code or flow_code(flow_name, self.spec),
                           description=flow_description(self.spec), meta=dict(meta or {}))
        self.page = None
        self.col: Collector | None = None
        self.role = ""
        self._main: tuple[Any, Collector] | None = None     # pipeline-ын page, col
        self._extra_ctx = None                              # auth.login new_context-ын context
        self._shot_seq = 0
        self.current: StepResult | None = None
        self.resumed = False          # session тасарсны дараа алхмыг дахин ажиллуулж байгаа эсэх

    # ------------------------------------------------------------------ util
    def log(self, msg: str, level: str = "info") -> None:
        self.runlog.log(msg, level)

    def warn(self, text: str) -> None:
        """Алхмыг унагахгүй анхааруулга (ж: сонголтын жагсаалт хоосон) — тайланд Minor bug болж гарна."""
        if self.current is not None:
            self.current.findings.append(M.Finding(M.FORM_WARNING, text))
            self.current.notes.append(f"⚠ {text}")

    def note(self, msg: str) -> None:
        if self.current is not None:
            self.current.notes.append(self.sanitize(msg))

    async def screenshot(self, name: str) -> str:
        if not self.page:
            return ""
        self._shot_seq += 1
        safe = re.sub(r"[^\w-]", "_", self.flow_name)
        path = self.run_dir / "shots" / f"flow-{safe}-{self._shot_seq:02d}-{name}.png"
        try:
            await self.page.screenshot(path=str(path), timeout=5000)
            return path.relative_to(self.run_dir).as_posix()
        except Exception:
            return ""

    async def open_context(self, storage_state: dict) -> None:
        """Тусдаа тест модуль session-ийг хүлээн авч буйг шалгах: storage_state-ээр шинэ context."""
        await self.close_context()
        browser = self._main[0].context.browser
        bctx = await browser_mod.new_context(browser, self.cfg, storage_state)
        page = await bctx.new_page()
        self._extra_ctx = bctx
        self.page, self.col = page, Collector(page, self.cfg, self.sanitize, self.runlog.raw)

    async def close_context(self) -> None:
        if self._extra_ctx is not None:
            try:
                await self._extra_ctx.close()
            except Exception:
                pass
            self._extra_ctx = None
        if self._main:
            self.page, self.col = self._main

    # ----------------------------------------------------------------- main
    async def execute(self, page, col: Collector) -> FlowRun:
        """Нэвтэрсэн pipeline-ын page дээр flow-ын бүх алхмыг ажиллуулна."""
        self._main = (page, col)
        self.page, self.col = page, col
        self.run.started_at = now_iso()
        self.log(f"Flow '{self.flow_name}' эхэллээ — {self.url} (run_tag={self.ctx.run_tag})")
        try:
            await self._steps()
        except Exception as e:
            self.run.error = self.sanitize(str(e))[:500]
            self.log(f"Flow алдаа: {e!r}", "error")
        finally:
            await self.close_context()
            try:        # дараагийн урсгалд үлдсэн форм/алдааны цонх, toast саад болохгүй
                from app.flows import ui
                await ui.close_form(self)
                await ui.clear_toasts(self)
            except Exception:
                pass
            self._fill_blocked(self.run.error and f"Flow алдаа: {self.run.error}" or "Ажиллаагүй")
            self.run.finished_at = now_iso()
            self.run.data = {k: v for s in self.run.steps for k, v in s.outputs.items()}
        return self.run

    def blocked(self, reason: str) -> FlowRun:
        """Flow эхлээгүй (нэвтрэлт амжилтгүй, session алдагдсан, зогсоосон) — бүх алхам Blocked."""
        self.run.started_at = self.run.finished_at = now_iso()
        self._fill_blocked(reason)
        return self.run

    def _fill_blocked(self, reason: str) -> None:
        steps = self.spec.get("steps") or []
        for i, s in enumerate(steps[len(self.run.steps):], len(self.run.steps) + 1):
            args = s.get("with") or {}
            self.run.steps.append(StepResult(
                index=i, uses=s["uses"], name=s.get("name") or s["uses"], status=M.BLOCKED, message=reason,
                requires=sorted(placeholders(args) | set(s.get("requires") or [])),
                always=bool(s.get("always")), args=dict(args)))

    def _emit_step(self, res: StepResult, state: str) -> None:
        # auto: явцын UI-д run-ы дундуур үүссэн урсгал (автомат CRUD / форм шалгалт) — идэвхтэй алхамд нэмнэ
        self.emit_cb("flow_step", {"flow": self.flow_name,
                                   "auto": bool(self.run.meta.get("auto") or self.run.meta.get("formcheck")),
                                   "index": res.index, "name": res.name,
                                   "state": state, "message": res.message,
                                   "total": len(self.spec.get("steps") or [])})

    async def _steps(self) -> None:
        steps = self.spec.get("steps") or []
        timeout_s = (self.cfg.get("flow") or {}).get("step_timeout_s", 120)
        broken: StepResult | None = None      # эхний Fail/Blocked алхам
        for i, s in enumerate(steps, 1):
            uses = s["uses"]
            args = s.get("with") or {}
            res = StepResult(index=i, uses=uses, name=s.get("name") or uses,
                             requires=sorted(placeholders(args) | set(s.get("requires") or [])),
                             always=bool(s.get("always")), args=dict(args))
            self.run.steps.append(res)
            self._emit_step(res, "running")
            if self.cancel_event.is_set():
                res.status = M.BLOCKED
                res.message = self.session.lost_reason if self.session.lost else "Хэрэглэгч зогсоосон"
            elif broken and not s.get("always"):
                res.status = M.BLOCKED
                res.message = f"Өмнөх алхам #{broken.index} ({broken.name}) {broken.status}"
            else:
                await self._run_with_session(res, STEPS[uses], args, timeout_s)
            if res.status != M.PASS and broken is None:
                broken = res
            self.log(f"[{self.flow_name}] #{i} {res.name}: {res.status}{' — ' + res.message if res.message else ''}",
                     "info" if res.status == M.PASS else "warn")
            self._emit_step(res, res.status)

    async def _run_with_session(self, res: StepResult, sd: StepDef, raw_args: dict, timeout_s: float) -> None:
        from app.pipeline.session import SessionLost

        self.session.stage = f"crud:{self.flow_name}"
        try:
            if sd.needs_session:
                await self.session.ensure(f"{self.flow_name} › {res.name}")
        except SessionLost as e:
            res.status, res.message = M.BLOCKED, str(e) or "Session алдагдсан"
            return
        self.resumed = False
        await self._run_step(res, sd, raw_args, timeout_s)
        # Алхмын явцад logout болсон бол: түр зогсоож дахин нэвтрэлт хүлээгээд алхмыг 1 удаа дахин ажиллуулна
        if res.status != M.PASS and self._extra_ctx is None and await self.session.is_logged_out():
            self.note(f"Алхмын явцад session тасарсан ({res.status}: {res.message})")
            if not await self.session.relogin(f"{self.flow_name} › {res.name}"):
                res.status = M.BLOCKED
                res.message = self.session.lost_reason or "Хэрэглэгч зогсоосон"
                return
            notes = list(res.notes)
            res.status, res.message, res.findings, res.screenshot, res.outputs = M.PENDING, "", [], "", {}
            res.notes, res.retried = notes, True
            self.resumed = True
            await self._run_step(res, sd, raw_args, timeout_s)
            self.resumed = False

    async def _run_step(self, res: StepResult, sd: StepDef, raw_args: dict, timeout_s: float) -> None:
        missing = [k for k in res.requires if k not in self.ctx.data]
        if missing:
            res.status = M.BLOCKED
            res.message = f"Шаардлагатай өгөгдөл алга: {', '.join(missing)}"
            return
        if sd.needs_session and self.page is None:
            res.status, res.message = M.BLOCKED, "Нэвтэрсэн session алга"
            return
        try:
            args = self.ctx.render(raw_args)
        except MissingData as e:
            res.status, res.message = M.BLOCKED, f"Шаардлагатай өгөгдөл алга: {e.args[0]}"
            return
        res.started_at = now_iso()
        self.current = res
        t0 = time.monotonic()
        try:
            out = await asyncio.wait_for(sd.fn(self, args), timeout_s)
            res.outputs = dict(out or {})
            res.status = M.PASS
        except StepBlocked as e:
            # алхмын явцад бүртгэсэн анхааруулгыг (env.warn) алдагдуулахгүй
            res.status, res.message = M.BLOCKED, self.sanitize(str(e))
            res.findings = list(e.findings) + [f for f in res.findings if f not in e.findings]
            res.category = e.category
            if e.skip_flow:
                self.run.meta["skipped"] = res.message
        except StepFailed as e:
            res.status, res.message = M.FAIL, self.sanitize(str(e))
            res.findings = list(e.findings) + [f for f in res.findings if f not in e.findings]
        except asyncio.TimeoutError:
            res.status, res.message = M.BLOCKED, f"Алхам {timeout_s} сек-д дууссангүй (timeout)"
        except Exception as e:
            if "has been closed" in str(e) or type(e).__name__ == "TargetClosedError":
                # Хөтөч/таб хаагдсан (хэрэглэгч хаасан эсвэл унасан) — аппын bug биш; үлдсэн бүх алхам ажиллах боломжгүй
                res.status, res.message = M.BLOCKED, "Хөтөч хаагдсан — алхам ажиллаж чадсангүй"
                if self.cancel_event is not None:
                    self.cancel_event.set()
            elif type(e).__name__ == "TimeoutError" and "Page." in str(e):
                # Playwright: элемент дарагдсангүй / хүлээлт хэтэрсэн — аппын алдаа биш, шалгаж чадаагүй
                res.status, res.category = M.BLOCKED, "tool"
                res.message = self.sanitize(f"Элементтэй харилцаж чадсангүй: {str(e).splitlines()[0]}")[:500]
            else:
                res.status, res.message = M.FAIL, self.sanitize(f"{type(e).__name__}: {e}")[:500]
        res.role = self.role
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        # Алхмын явц дахь сүлжээ/консолын ноцтой алдаа — UI дээр амжилттай мэт харагдсан ч Fail
        if self.col is not None:
            await self.col.drain()
            found = self.col.findings_from(self.col.since(t0), include_deploy=True)
            hard = [f for f in found if f.kind in HARD_KINDS]
            known = {(f.kind, f.text) for f in res.findings}
            res.findings += [f for f in found if (f.kind, f.text) not in known]
            if hard and res.status in (M.PASS, M.BLOCKED) and "Session" not in (res.message or ""):
                # Апп уналт / 5xx нь bug — алхам «шалгаж чадаагүй» (Blocked) болсон ч шалтгаан нь апп өөрөө
                if res.status == M.BLOCKED and res.message:
                    res.notes.append(f"Алхам дуусаагүй: {res.message}")
                res.status = M.FAIL
                res.message = hard_message(hard[0])
            res.notes += [f"{f.kind}: {f.text}" for f in found if f.kind not in HARD_KINDS]
        if res.status == M.PASS:
            self.ctx.data.update(res.outputs)
        else:
            res.outputs = {}
            if not res.screenshot:        # алхам өөрөө (формыг хаахаас өмнө) авсан бол хадгална
                res.screenshot = await self.screenshot(res.uses.replace(".", "-"))
