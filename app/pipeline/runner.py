"""PipelineRunner: нэг run = нэг browser session = нэг тайлан.

  Нэвтрэх (гараар, заавал) → Smoke (сонголттой) → CRUD flow-ууд (0..N) → …

Browser, page, Collector, SessionGuard, FlowContext (нэг run_tag)-ийг эзэмшиж, алхам бүрийг
`STAGES` registry-гээс дуудна. Алхам хоорондын дүрэм:
  - smoke NO-GO байсан ч crud үргэлжилнэ; нэг flow унасан ч дараагийн flow ажиллана
  - нэвтрэлт амжилтгүй / session алдагдсан / хэрэглэгч зогсоосон → үлдсэн алхмууд Blocked
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app import models as M
from app.flows.context import FlowContext, make_run_tag
from app.flows.draft import write_draft
from app.flows.engine import flow_code, load_flow
from app.models import RunData, TimelineEvent
from app.pipeline.session import LoginObserver, SessionGuard
from app.pipeline.stages import STAGES, StepSpec, validate_steps
from app.runner import browser as browser_mod
from app.runner.detectors import Collector
from app.runner.runlog import Emit, RunLog, now_iso
from app.runner.session import LoginCancelled, LoginError, LoginTimeout
from app.runner.smoke import RunOptions, SmokeRunner

DEFAULT_STEPS = [{"kind": "smoke"}]


class PipelineRunner:
    def __init__(self, run_id: str, url: str, cfg: dict, options: RunOptions, run_dir: Path,
                 steps: list | None = None, emit: Emit | None = None, *, headless: bool = False,
                 on_waiting_login: LoginObserver | None = None, run_tag: str | None = None,
                 cdp: str | None = None):
        """headless: гар нэвтрэлтэд хөтөч харагдах ёстой тул production-д ҮРГЭЛЖ False (API өөрчлөх
        боломж өгөхгүй); зөвхөн автомат тест True дамжуулна.
        on_waiting_login: гар нэвтрэлт хүлээж эхлэхэд page-ийг авах hook — зөвхөн тестэд."""
        self.steps: list[StepSpec] = validate_steps(url, cfg, DEFAULT_STEPS if steps is None else steps)
        self.run_id = run_id
        self.url = url
        self.cfg = cfg
        self.options = options
        self.run_dir = Path(run_dir)
        (self.run_dir / "shots").mkdir(parents=True, exist_ok=True)
        self.emit = emit or (lambda t, d: None)
        self.headless = headless
        self.cdp = cdp                       # өөрийн Chrome-д холбогдох (captcha-тай сайт) — CDP URL
        self.halt = asyncio.Event()          # хэрэглэгч зогсоосон ЭСВЭЛ session алдагдсан
        self.flow_ctx = FlowContext(run_tag or make_run_tag())
        # E2E орчны утгууд (тест компани, тайлант үе, мастер өгөгдлийн код …) — сценарид {нэр}-ээр
        self.flow_ctx.data.update({k: v for k, v in ((cfg.get("e2e") or {}).get("vars") or {}).items()})
        self.data = RunData(run_id=run_id, url=url, project=options.project, qa_lead=options.qa_lead,
                            tester=options.tester,
                            version_label=options.version_label, run_tag=self.flow_ctx.run_tag)
        self.flow_codes = self._assign_flow_codes()
        self.data.pipeline = [{"kind": "auth", "name": "Нэвтрэх (гараар)", "status": M.PENDING, "message": ""}]
        for spec in self.steps:
            self.data.pipeline.append({"kind": spec.kind, "flow": spec.flow, "name": STAGES[spec.kind].name(spec),
                                       "status": M.PENDING, "message": ""})
        self.runlog = RunLog(self.run_dir, self.emit)
        self.session = SessionGuard(url, cfg, self.data, self.runlog, self.emit, self.halt, on_waiting_login)
        self.page = None
        self.col: Collector | None = None
        self.smoke: SmokeRunner | None = None
        self.crud_selection: asyncio.Future | None = None
        self.form_selection: asyncio.Future | None = None
        self.record_done: asyncio.Future | None = None
        self._auto_seq: dict[str, int] = {}

    def _assign_flow_codes(self) -> dict[str, str]:
        codes: dict[str, str] = {}
        for spec in self.steps:
            if spec.kind != "crud" or spec.flow in codes:
                continue
            base = code = flow_code(spec.flow, load_flow(self.cfg, spec.flow))
            n = 2
            while code in codes.values():
                code, n = f"{base}{n}", n + 1
            codes[spec.flow] = code
        return codes

    # --------------------------------------------------------------- control
    def cancel(self) -> None:
        already = self.halt.is_set() and self.data.cancelled
        self.data.cancelled = True
        self.halt.set()
        if not already:
            self.runlog.log("Хэрэглэгч Зогсоох дарлаа — run-ыг зогсоож байна…", "warn")
        if self.smoke:
            self.smoke.cancel()
        self.select_crud([])
        self.select_forms([])
        self.stop_recording()

    def select_modules(self, modules: list[str] | None) -> None:
        if self.smoke:
            self.smoke.select_modules(modules)

    def select_crud(self, keys: list[str] | None) -> bool:
        """UI-аас автомат CRUD хийх цэсүүд ирлээ. Хүлээж байгаа сонголт байвал True."""
        if self.crud_selection and not self.crud_selection.done():
            self.crud_selection.set_result(list(keys or []))
            return True
        return False

    async def wait_crud_selection(self, cands: list[dict]) -> list[str]:
        """Smoke-ийн дараа CRUD боломжтой цэсүүдийг UI-д харуулж сонголт хүлээнэ. Өгөгдөл өөрчилдөг тул
        хугацаа хэтэрвэл (эсвэл зогсоовол) ЮУ Ч сонгохгүй. Зөвхөн нэр дэвшигчдийн түлхүүрийг хүлээн авна."""
        self.crud_selection = asyncio.get_running_loop().create_future()
        self.emit("crud_candidates", {"menus": cands})
        self.emit("status", {"state": "awaiting_crud_selection"})
        self.runlog.log(f"Автомат CRUD: {len(cands)} боломжтой цэс — сонголт хүлээж байна…")
        try:
            keys = await asyncio.wait_for(self.crud_selection, self.options.selection_timeout_s)
        except asyncio.TimeoutError:
            self.runlog.log("Автомат CRUD-ийн сонголт ирээгүй — алгасав (өгөгдөл өөрчлөхгүй)", "warn")
            keys = []
        finally:
            self.crud_selection = None
        allowed = {c["key"] for c in cands}
        chosen = [k for k in keys or [] if k in allowed]
        self.runlog.log(f"Автомат CRUD: {len(chosen)} цэс сонгогдлоо")
        return chosen

    def stop_recording(self) -> bool:
        """UI «⏹ Дуусгах» — бичлэгийг дуусгаж YAML үүсгэнэ. Бичиж байгаа бол True."""
        if self.record_done and not self.record_done.done():
            self.record_done.set_result(True)
            return True
        return False

    def select_forms(self, keys: list[str] | None) -> bool:
        """UI-аас форм шалгалт хийх цэсүүд ирлээ. Хүлээж байгаа сонголт байвал True."""
        if self.form_selection and not self.form_selection.done():
            self.form_selection.set_result(list(keys or []))
            return True
        return False

    async def wait_form_selection(self, cands: list[dict]) -> list[str]:
        """Smoke-ийн дараа форм шалгалт хийх цэсийг UI-аас сонгуулна (smoke-ийн модуль сонголт шиг).
        READ-ONLY тул хугацаа хэтэрвэл БҮХ нэр дэвшигчийг шалгана; зогсоовол юу ч шалгахгүй."""
        self.form_selection = asyncio.get_running_loop().create_future()
        self.emit("form_candidates", {"menus": [{k: v for k, v in c.items() if k != "caps"} for c in cands]})
        self.emit("status", {"state": "awaiting_form_selection"})
        self.runlog.log(f"Форм шалгалт: {len(cands)} цэс — сонголт хүлээж байна…")
        try:
            keys = await asyncio.wait_for(self.form_selection, self.options.selection_timeout_s)
        except asyncio.TimeoutError:
            self.runlog.log("Форм шалгалтын сонголт ирээгүй — бүх цэсийг шалгана (READ-ONLY)", "warn")
            keys = [c["key"] for c in cands]
        finally:
            self.form_selection = None
        allowed = {c["key"] for c in cands}
        chosen = [k for k in keys or [] if k in allowed]
        self.runlog.log(f"Форм шалгалт: {len(chosen)} цэс сонгогдлоо")
        return chosen

    def next_auto_code(self, prefix: str = "A") -> str:
        """Автомат урсгалын код: A01… (автомат CRUD), F01… (форм шалгалт)."""
        while True:
            self._auto_seq[prefix] = self._auto_seq.get(prefix, 0) + 1
            code = f"{prefix}{self._auto_seq[prefix]:02d}"
            if code not in self.flow_codes.values():
                return code

    def _stage(self, idx: int, status: str, message: str = "") -> None:
        e = self.data.pipeline[idx]
        if status == "running":
            e["started_at"] = now_iso()
        else:
            e["finished_at"] = now_iso()
        e["status"], e["message"] = status, self.runlog.sanitize(message)[:300]
        stage = "auth" if e["kind"] == "auth" else (f"crud:{e['flow']}" if e["kind"] == "crud" else e["kind"])
        self.emit("stage", {"index": idx, "kind": e["kind"], "flow": e.get("flow"), "name": e["name"],
                            "stage": stage, "state": status, "message": e["message"]})

    async def _screenshot(self, name: str) -> None:
        try:
            await self.page.screenshot(path=str(self.run_dir / "shots" / f"{name}.png"), timeout=5000)
        except Exception:
            pass

    # ------------------------------------------------------------------ main
    async def run(self) -> RunData:
        from playwright.async_api import async_playwright

        self.data.started_at = now_iso()
        self.emit("status", {"state": "starting", "run_tag": self.flow_ctx.run_tag})
        self.runlog.log(f"Pipeline: {' → '.join(e['name'] for e in self.data.pipeline)} (run_tag={self.flow_ctx.run_tag})")
        try:
            async with async_playwright() as pw:
                if self.cdp:
                    self.runlog.log(f"Өөрийн Chrome-д холбогдож байна ({self.cdp}) — шинэ таб нээнэ")
                    browser, bctx, self.page = await browser_mod.attach(pw, self.cdp, self.cfg)
                else:
                    browser = await browser_mod.launch(pw, self.headless)
                try:
                    if not self.cdp:
                        bctx = await browser_mod.new_context(browser, self.cfg)
                        self.page = await bctx.new_page()
                    self.col = Collector(self.page, self.cfg, self.runlog.sanitize, self.runlog.raw)
                    self.session.page = self.page
                    await self._run_stages()
                finally:
                    try:
                        if self.cdp:
                            await self.page.close()      # зөвхөн өөрийн табыг — хэрэглэгчийн Chrome нээлттэй үлдэнэ
                        await browser.close()            # CDP: зөвхөн холболтыг салгана
                    except Exception:
                        pass
        except Exception as e:  # хөтөч эхлүүлэх г.м. ерөнхий алдаа
            self.runlog.log(f"Run алдаа: {e!r}", "error")
            if not self.data.login_ok and not self.data.login_error:
                self.data.login_error = self.runlog.sanitize(str(e))[:300]
            self.emit("error", {"msg": self.runlog.sanitize(str(e))})
            self._skip_rest(f"Run алдаа: {str(e)[:150]}")
        finally:
            self.session.close_segment()
            self.data.session_observed_s = max(self.session.segments or [0.0])
            self.data.finished_at = now_iso()
            self.data.timeline.sort(key=lambda e: e.at)
            try:
                self.data.draft = write_draft(self.run_dir, self.run_id, self.data.flows)
            except Exception as e:
                self.runlog.log(f"YAML ноорог үүсгэж чадсангүй: {e!r}", "warn")
            self.runlog.close()
            (self.run_dir / "raw.json").write_text(
                json.dumps(self.data.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return self.data

    def _skip_rest(self, reason: str) -> None:
        for idx, e in enumerate(self.data.pipeline):
            if e["status"] in (M.PENDING, "running"):
                if idx > 0:
                    spec = self.steps[idx - 1]
                    STAGES[spec.kind].skip(self, spec, reason)
                # Ажиллаагүй үе шат — апп-ын саадын нотолгоо биш (зогсоосон / нэвтрээгүй / session) → Шалгагдаагүй
                self._stage(idx, M.NOT_TESTED, reason)

    async def _run_stages(self) -> None:
        # 1. Нэвтрэх (гараар, заавал)
        self.emit("status", {"state": "login"})
        self._stage(0, "running")
        self.runlog.log(f"Хөтөч нээгдлээ — {self.url} дээр гараар нэвтэрнэ үү")
        try:
            self.data.app_version = await self.session.first_login()
        except LoginCancelled:
            self.data.cancelled = True
            self.runlog.log("Хэрэглэгч нэвтрэлтийг хүлээж байх зуур зогсоолоо", "warn")
            self.data.timeline.append(TimelineEvent(now_iso(), "Нэвтрэлт", "Зогсоосон", "Хэрэглэгч Зогсоох дарсан"))
            self._skip_rest("Хэрэглэгч зогсоосон")
            return
        except LoginError as e:
            self.data.login_error = self.runlog.sanitize(str(e))[:300]
            self.data.login_timeout = isinstance(e, LoginTimeout)
            self.runlog.log(f"Нэвтрэлт амжилтгүй: {self.data.login_error}", "error")
            self.data.timeline.append(TimelineEvent(now_iso(), "Нэвтрэлт", "Fail", self.data.login_error))
            await self._screenshot("login-failed")
            self._stage(0, M.NOT_TESTED if self.data.login_timeout else M.FAIL, self.data.login_error)
            self._skip_rest(f"Нэвтрэлт амжилтгүй: {self.data.login_error}")
            return
        self.data.login_ok = True
        self.runlog.log(f"Нэвтэрлээ. Хувилбар: {self.data.app_version or 'тодорхойгүй'}")
        self.data.timeline.append(TimelineEvent(now_iso(), "Нэвтрэлт (гараар)", "Pass",
                                                f"Хувилбар {self.data.app_version or '—'}"))
        self._stage(0, M.PASS, self.data.app_version)

        # 2. Сонгосон алхмууд
        for idx, spec in enumerate(self.steps, 1):
            if self.halt.is_set():
                break
            stage = STAGES[spec.kind]
            self.session.stage = f"crud:{spec.flow}" if spec.kind == "crud" else spec.kind
            self._stage(idx, "running")
            try:
                status, msg = await stage.run(self, spec)
            except Exception as e:
                self.runlog.log(f"'{stage.name(spec)}' алхмын алдаа: {e!r}", "error")
                status, msg = M.FAIL, f"{type(e).__name__}: {e}"
            self._stage(idx, status, msg)
        if self.halt.is_set():
            self._skip_rest(self.session.lost_reason if self.session.lost else "Хэрэглэгч зогсоосон")
