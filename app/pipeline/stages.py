"""Pipeline-ын алхмын төрлүүдийн registry.

Шинэ төрөл (ж: business, integration) нэмэхдээ `Stage`-ээс удамшуулж `@register`-ээр бүртгэнэ:
  - validate(): run эхлэхээс өмнө (API 400) тохиргоо/аюулгүй байдлыг шалгана
  - run(): нэвтэрсэн нэг page/session дээр алхмыг гүйцэтгэж (status, message) буцаана
  - skip(): алхам ажиллаж чадаагүй (нэвтрэлт амжилтгүй, session алдагдсан, зогсоосон) үед
            тайланд Blocked болж орох өгөгдлийг бүртгэнэ
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from app import models as M
from app.flows import ui
from app.flows.context import FlowContext, StepResult
from app.flows.engine import FlowConfigError, FlowEngine, host_allowed, host_reason, load_flow
from app.models import TimelineEvent
from app.runner.capabilities import crud_candidates, form_candidates, menu_from_key, menu_path
from app.runner.detectors import snapshot
from app.runner.runlog import now_iso
from app.runner.smoke import SmokeRunner

if TYPE_CHECKING:
    from app.pipeline.runner import PipelineRunner


class PipelineConfigError(ValueError):
    pass


_CRASH_KINDS = (M.RENDERER_EXCEPTION, M.RUNTIME_CRASH)


def crash_pages(run) -> set[str]:
    """Уналтын stack trace дахь Blazor хуудасны классууд (ж: «…UI.Pages.RPARobotList.…» → {"rparobotlist"})."""
    txt = " ".join(f"{f.text} {f.detail or ''}" for s in run.steps for f in s.findings)
    txt += " ".join(n for s in run.steps for n in s.notes)
    return {m.lower() for m in re.findall(r"\.Pages\.(\w+)", txt)}


# Апп/орчны тасалдлын шинж (аппын bug биш): сервертэй холбогдож чадаагүй, хуудас өөр тийш шилжсэн, цэсний мод алга
INFRA_PATTERNS = ("цэс олдсонгүй", "цэсийг дарж чадсангүй", "Failed to fetch", "Сервертэй холбогдож чадсангүй",
                  "Execution context was destroyed", "цэсний таб нээгдсэнгүй", "has been closed",
                  "net::ERR_", "Хөтөч хаагдсан")


def infra_broken(run) -> bool:
    """Урсгалын ажилласан бүх алхам орчны тасалдлын шинжтэй мессежээр бүтэлгүйтсэн эсэх."""
    # Өмнөх алхмаас хамаарч Blocked болсон алхмууд («Өмнөх алхам #1 … Fail», «Шаардлагатай өгөгдөл алга») өөрсдөө
    # шинж биш — тэдгээрийг тооцохгүй (автомат CRUD-ийн 8 алхамт урсгал)
    dependent = ("Өмнөх алхам", "Шаардлагатай өгөгдөл алга")
    steps = [s for s in run.steps if not (s.status == M.BLOCKED and s.category in ("nodata", "na"))
             and not (s.status == M.BLOCKED and (s.message or "").startswith(dependent))]
    bad = [s for s in steps if s.status in (M.FAIL, M.BLOCKED)]
    return bool(steps) and len(bad) == len(steps) and all(
        any(p in (s.message or "") for p in INFRA_PATTERNS) for s in bad)


def auto_infra_broken(run) -> bool:
    """Автомат CRUD: орчны тасалдал. Цэс бүрийг smoke «Нэмэх + Устгах»-тай гэж бүртгэсэн тул «Нэмэх товч олдсонгүй» нь
    апп эвдэрсний шинж (testhr 2026-10-22: «Хоолны меню»-ийн «Data is Null»-ийн дараа toolbar бүхэлдээ алга болж 68 цэс
    дараалан «Нэмэх товч олдсонгүй»)."""
    if infra_broken(run):
        return True
    first = next((s for s in run.steps if s.status in (M.FAIL, M.BLOCKED)), None)
    return bool(first and first.index == 1 and (first.message or "").startswith("Нэмэх товч ("))


def page_stem(name: str) -> str:
    """Хуудас/route-ийн үндэс: «OrganizationForm», «organizationlist» → «organization» (жагсаалт ба форм нэг цэс)."""
    return re.sub(r"(list|form|page|view|grid|detail|edit)+$", "", name.lower()) or name.lower()


def finding_pages(f) -> set[str]:
    return {page_stem(m) for m in re.findall(r"\.Pages\.(\w+)", f"{f.text}\n{f.detail or ''}")}


def menu_route(key: str) -> str:
    return key.rsplit("|", 1)[-1].rstrip("/").rsplit("/", 1)[-1].lower()


def blame_previous(prev, crash: list, where: str) -> None:
    """Өмнөх цэсийг хаасны дараа (ж: dispose хийгдээгүй timer) апп унасан — уналтыг тэр цэсийн bug болгоно."""
    if not crash or not prev.steps:
        return
    # Тусдаа алхам «Цэсийн табыг хаах» — шалгалтын бусад алхмын (Хэвлэх г.м.) үр дүнг өөрчлөхгүй
    st = next((s for s in prev.steps if s.uses == "form.close_tab"), None)
    if st is None:
        st = StepResult(index=len(prev.steps) + 1, uses="form.close_tab", name="Цэсийн табыг хаах",
                        status=M.PASS, always=True)
        prev.steps.append(st)
    known = {(f.kind, f.text) for f in st.findings}
    st.findings += [f for f in crash if (f.kind, f.text) not in known]
    blob = "\n".join(f"{f.text}\n{f.detail or ''}" for f in crash)
    m = re.search(r"(System\.[\w.]*Exception[^\n]*)", blob)
    exc = m.group(1).strip() if m else next((f.text for f in crash if f.kind in _CRASH_KINDS), "")
    pages = sorted(set(re.findall(r"\.Pages\.(\w+)", blob)))
    if pages:
        exc += f" ({', '.join(pages)})"
    if st.status != M.FAIL:
        st.status = M.FAIL
        st.category = ""
        st.message = f"Цэсийн табыг хаасны дараа апп унав (.NET runtime зогсов, {where}): {exc[:250]}"
    else:
        st.notes.append(f"Цэсийн табыг хаасны дараа апп унав ({where}): {exc[:250]}")


@dataclass
class StepSpec:
    kind: str
    flow: str | None = None
    modules: list[str] | None = None
    menus: list[str] | None = None     # autocrud/formcheck: цэсний түлхүүрүүд; None → smoke-ийн дараа сонгоно
    select: bool = True                # formcheck, menus=None: UI-аас сонгуулах (False → бүгд, ж: CLI)

    @classmethod
    def parse(cls, d: "StepSpec | dict") -> "StepSpec":
        if isinstance(d, StepSpec):
            return d
        return cls(kind=str(d.get("kind") or "").strip(), flow=d.get("flow"), modules=d.get("modules"),
                   menus=d.get("menus"), select=d.get("select", True) is not False)


class Stage:
    kind = ""
    mutates = False          # өгөгдөл өөрчилдөг эсэх (allowed_hosts шаардана)

    def name(self, spec: StepSpec) -> str:
        return self.kind

    def validate(self, url: str, cfg: dict, spec: StepSpec) -> None:
        if self.mutates and not host_allowed(url, cfg):
            raise PipelineConfigError(host_reason(url, cfg))

    async def run(self, pipe: "PipelineRunner", spec: StepSpec) -> tuple[str, str]:
        raise NotImplementedError

    def skip(self, pipe: "PipelineRunner", spec: StepSpec, reason: str) -> None:
        pass


STAGES: dict[str, Stage] = {}


def register(cls: type[Stage]) -> type[Stage]:
    STAGES[cls.kind] = cls()
    return cls


def validate_steps(url: str, cfg: dict, steps: list) -> list[StepSpec]:
    out: list[StepSpec] = []
    for i, raw in enumerate(steps or [], 1):
        spec = StepSpec.parse(raw)
        stage = STAGES.get(spec.kind)
        if stage is None:
            raise PipelineConfigError(f"#{i} алхам: '{spec.kind}' төрөл дэмжигдэхгүй. "
                                      f"Байгаа: {', '.join(sorted(STAGES))}")
        stage.validate(url, cfg, spec)
        if spec.kind == "record" and len(steps or []) > 1:
            raise PipelineConfigError("Урсгал бичих run-д өөр алхам нэмэхгүй (зөвхөн нэвтрэх + бичих)")
        if spec.kind in ("autocrud", "formcheck") and spec.menus is None and not any(s.kind == "smoke" for s in out):
            raise PipelineConfigError(f"#{i} алхам: {stage.name(spec)} — цэсийг smoke-ийн бүртгэлээс авах бол "
                                      "өмнө нь smoke алхам байх ёстой (эсвэл menus-ийг урьдчилан заана уу)")
        out.append(spec)
    return out


# ---------------------------------------------------------------------- smoke
@register
class SmokeStage(Stage):
    kind = "smoke"

    def name(self, spec: StepSpec) -> str:
        return "Smoke (READ-ONLY)"

    async def run(self, pipe: "PipelineRunner", spec: StepSpec) -> tuple[str, str]:
        opts = pipe.options
        if spec.modules:
            opts = replace(opts, modules=list(spec.modules), wait_selection=False)
        runner = SmokeRunner(pipe.url, pipe.cfg, opts, pipe.run_dir, data=pipe.data, runlog=pipe.runlog,
                             session=pipe.session, cancel_event=pipe.halt, emit=pipe.emit)
        pipe.smoke = runner
        pipe.data.smoke_ran = True
        try:
            await runner.execute(pipe.page, pipe.col)
        finally:
            pipe.smoke = None
        rs = pipe.data.results
        cnt = {k: sum(1 for r in rs if r.status == k) for k in (M.PASS, M.FAIL, M.BLOCKED)}
        msg = f"{len(rs)} цэс: Pass {cnt[M.PASS]} / Fail {cnt[M.FAIL]} / Blocked {cnt[M.BLOCKED]}"
        if pipe.session.lost:
            return M.BLOCKED, f"{msg} — {pipe.session.lost_reason}"
        if pipe.data.cancelled:
            return M.BLOCKED, f"{msg} — хэрэглэгч зогсоосон"
        if not rs:
            return M.BLOCKED, "Шалгах цэс олдсонгүй"
        if cnt[M.FAIL]:
            return M.FAIL, msg
        return (M.BLOCKED if cnt[M.BLOCKED] else M.PASS), msg


# ----------------------------------------------------------------------- crud
@register
class CrudStage(Stage):
    kind = "crud"
    mutates = True

    def name(self, spec: StepSpec) -> str:
        return f"CRUD: {spec.flow}"

    def validate(self, url: str, cfg: dict, spec: StepSpec) -> None:
        super().validate(url, cfg, spec)
        if not spec.flow:
            raise PipelineConfigError("crud алхамд 'flow' заавал")
        try:
            load_flow(cfg, spec.flow)
        except FlowConfigError as e:
            raise PipelineConfigError(str(e)) from e

    def _engine(self, pipe: "PipelineRunner", spec: StepSpec) -> FlowEngine:
        # flow-д `run_tag:` заасан бол (ж: өмнөх run-ы үлдсэн тестийн өгөгдлийг цэвэрлэх) тусдаа контекст —
        # crud.* алхмын run_tag хамгаалалт зөвхөн тэр tag-тай бичлэгт хүрэхийг зөвшөөрнө
        fspec = load_flow(pipe.cfg, spec.flow) or {}
        own_tag = fspec.get("run_tag")
        ctx = FlowContext(str(own_tag)) if own_tag else pipe.flow_ctx
        if own_tag:
            ctx.data.update(((pipe.cfg.get("e2e") or {}).get("vars") or {}))
        meta = {"e2e": True} if fspec.get("type") == "e2e" else None
        return FlowEngine(pipe.url, spec.flow, pipe.cfg, pipe.run_dir, ctx=ctx, runlog=pipe.runlog,
                          session=pipe.session, cancel_event=pipe.halt, emit=pipe.emit, run_id=pipe.run_id,
                          code=pipe.flow_codes[spec.flow], meta=meta)

    async def run(self, pipe: "PipelineRunner", spec: StepSpec) -> tuple[str, str]:
        run = await self._engine(pipe, spec).execute(pipe.page, pipe.col)
        pipe.data.flows.append(run.to_dict())
        kind = "Бизнес процесс" if run.meta.get("e2e") else "E2E урсгал"
        pipe.data.timeline.append(TimelineEvent(now_iso(), f"{kind} «{spec.flow}» ({len(run.steps)} алхам)",
                                                run.status, f"run_tag {run.run_tag}"))
        n_pass = sum(1 for s in run.steps if s.status == M.PASS)
        first_bad = next((s for s in run.steps if s.status != M.PASS), None)
        msg = f"{n_pass}/{len(run.steps)} алхам Pass"
        if first_bad:
            msg += f" — #{first_bad.index} {first_bad.name}: {first_bad.message}"
        return run.status, msg

    def skip(self, pipe: "PipelineRunner", spec: StepSpec, reason: str) -> None:
        pipe.data.flows.append(self._engine(pipe, spec).blocked(reason).to_dict())


# ------------------------------------------------------------------- autocrud
def auto_flow_spec(key: str, prefix: str, prefer: dict | None = None, columns: list[str] | None = None) -> dict:
    """Цэс бүрт үүсгэх автомат CRUD урсгал. Өгөгдлийн түлхүүр урсгал бүрт өөр (prefix) —
    нэг FlowContext хуваалцдаг тул өмнөх цэсийн бичлэгт хүрэхээс сэргийлнэ.
    prefer: {lookup шошго: мастер бичлэгийн утга} — тест орчинд хоосон lookup-д автоматаар үүсгэсэн бичлэг."""
    rec, fld = f"{prefix}_rec", f"{prefix}_field"
    create = {"menu": key, "save_as": rec, "field_as": fld, "try_as": f"{prefix}_try", "values_as": f"{prefix}_vals"}
    if prefer:
        create["prefer"] = dict(prefer)
    if columns:
        create["columns"] = list(columns)
    return {"steps": [
        {"uses": "crud.auto_create", "name": "Автомат бүртгэл (форм илрүүлж бөглөх)", "with": create},
        # menu: форм шинэ таб дээр нээгддэг сайтад алхам бүр жагсаалтын таб руу буцна (идэвхтэй бол юу ч хийхгүй)
        {"uses": "crud.search", "name": "Бүртгэснээ хайж олох", "with": {"menu": key, "text": "{%s}" % rec}},
        {"uses": "crud.auto_update", "name": "Автомат засвар",
         "with": {"menu": key, "find": "{%s}" % rec, "field": "{%s}" % fld, "save_as": rec}},
        # Засвар + бүртгэх үед бөглөсөн БҮХ талбар хадгалагдсаныг бичлэгийг дахин нээж шалгана
        {"uses": "crud.auto_verify", "name": "Засвар болон бүх талбар хадгалагдсаныг шалгах",
         "with": {"menu": key, "find": "{%s}" % rec, "field": "{%s}" % fld, "values": f"{prefix}_vals"}},
        {"uses": "crud.delete", "name": "Цэвэрлэх (устгах)", "always": True,
         "with": {"menu": key, "find": "{%s}" % rec}},
        {"uses": "crud.search", "name": "Устгагдсаныг шалгах",
         "with": {"menu": key, "text": "{%s}" % rec, "expect": "absent"}},
        # Өмнөх алхам Fail болсон ч (ж: «Хадгалах боломжгүй» гарсан ч бичлэг үүссэн) run_tag-тай өгөгдөл үлдээхгүй
        {"uses": "crud.auto_cleanup", "name": "Тестийн өгөгдөл үлдээгүйг шалгах", "always": True,
         "with": {"menu": key, "data": prefix}},
        {"uses": "crud.auto_empty_save", "name": "Хоосон форм хадгалахад анхааруулга гарах (сөрөг тест)",
         "always": True, "with": {"menu": key}},
    ]}


def _columns(pipe: "PipelineRunner", key: str) -> list[str] | None:
    """Smoke-ийн бүртгэсэн жагсаалтын баганууд (каталогоос сонгосон бол None)."""
    return next(((r.caps or {}).get("columns") for r in pipe.data.results or [] if r.item.key == key), None)


def master_setup_spec(key: str, prefix: str) -> dict:
    """Хамааралтай цэсийн lookup-д хэрэгтэй мастер бичлэгийг үүсгэж ҮЛДЭЭНЭ (master_teardown_spec устгана)."""
    rec = f"{prefix}_rec"
    return {"steps": [
        {"uses": "crud.auto_create", "name": "Мастер бичлэг үүсгэх (хамааралтай цэсүүдэд)",
         "with": {"menu": key, "save_as": rec, "field_as": f"{prefix}_field", "try_as": f"{prefix}_try"}},
        {"uses": "crud.search", "name": "Мастер бичлэгийг хайж олох", "with": {"menu": key, "text": "{%s}" % rec}},
    ]}


def master_teardown_spec(key: str, prefix: str) -> dict:
    rec = f"{prefix}_rec"
    return {"steps": [
        {"uses": "crud.delete", "name": "Мастер бичлэгийг устгах", "always": True,
         "with": {"menu": key, "find": "{%s}" % rec}},
        {"uses": "crud.auto_cleanup", "name": "Мастер өгөгдөл үлдээгүйг шалгах", "always": True,
         "with": {"menu": key, "data": prefix}},
    ]}


# Мастер цэсийн нэрэнд lookup-ийн шошгоос гадна байж болох ерөнхий үгс
_MASTER_EXTRA = {"жагсаалт", "бүртгэл", "лавлах"}


def master_for(label: str, keys: list[str]) -> str | None:
    """Хоосон lookup-ийн шошго («Ажилтан») → тэр өгөгдлийг үүсгэх цэс («Ажилтан», «Ажилтны жагсаалт»).
    Яг ижил нэр > шошгоор эхэлсэн > язгуураар эхэлсэн (монгол хэлний хувилал: Ажилтан → Ажилтны)."""
    lab = (label or "").strip().lower()
    if len(lab) < 3:
        return None
    lw = lab.split()
    best = None
    for k in keys:
        try:
            name = menu_from_key(k).name.strip().lower()
        except ValueError:
            continue
        nw = name.split()
        if len(nw) < len(lw):
            continue
        # Үг бүр язгуураараа таарна (Ажилтан ↔ Ажилтны, байгуулга ↔ байгууллага); илүү үг нь зөвхөн «жагсаалт»
        # мэт ерөнхий үг — «Ажилтны үзүүлэлт», «Харилцагчийн бүлэг» нь өөр өгөгдөл
        stems = [w[:max(4, len(w) - 2)] if len(w) >= 5 else w for w in lw]
        if not all(n.startswith(st) for n, st in zip(nw, stems)):
            continue
        if any(w not in _MASTER_EXTRA for w in nw[len(lw):]):
            continue
        score = 3 if name == lab else 2 if name.startswith(lab) else 1
        if best is None or (score, -len(name)) > best[0]:
            best = ((score, -len(name)), k)
    return best[1] if best else None


@register
class AutoCrudStage(Stage):
    """YAML-гүй автомат CRUD: smoke-ийн бүртгэсэн (эсвэл өмнөх run-ы каталогоос сонгосон) цэс бүрт."""
    kind = "autocrud"
    mutates = True

    def name(self, spec: StepSpec) -> str:
        if spec.menus is None:
            return "Автомат CRUD (smoke-ийн дараа цэс сонгоно)"
        return f"Автомат CRUD ({len(spec.menus)} цэс)"

    def validate(self, url: str, cfg: dict, spec: StepSpec) -> None:
        super().validate(url, cfg, spec)
        for k in spec.menus or []:
            try:
                menu_from_key(k)
            except ValueError as e:
                raise PipelineConfigError(str(e)) from e

    async def run(self, pipe: "PipelineRunner", spec: StepSpec) -> tuple[str, str]:
        keys = spec.menus
        if keys is None:
            cands = crud_candidates(pipe.data.results)
            if not cands:
                return M.BLOCKED, "Smoke-оос автомат CRUD хийх боломжтой (Нэмэх + Устгах) цэс олдсонгүй"
            keys = await pipe.wait_crud_selection(cands)
        if not keys:
            return M.BLOCKED, "Автомат CRUD хийх цэс сонгогдоогүй — алгасав"
        runs = []
        streak = 0
        for n, key in enumerate(keys):
            if pipe.halt.is_set():
                break
            code = pipe.next_auto_code()
            run = await self._flow(pipe, key, code, auto_flow_spec(key, code.lower(), columns=_columns(pipe, key)))
            runs.append(run)
            # Орчны тасалдал (апп/.NET runtime унасан, цэсний мод алга): бүх цэс дараалан «цэс олдсонгүй» гэж Fail
            # болдог байсан (testaccounting 2026-10-21: 118 цэс 32 секундэд) — 3 дараалвал зогсоож үлдсэнийг resume-д
            streak = streak + 1 if run.meta.get("infra") else 0
            if streak >= 3:
                rest = list(keys[n + 1:])
                pipe.data.resume.setdefault(self.kind, []).extend(rest)
                pipe.runlog.log(f"Автомат CRUD: тест орчин хариу өгөхгүй байна ({streak} цэс дараалан) — зогсоов; "
                                f"{len(rest)} цэсийг «Үлдсэн цэсүүдийг үргэлжлүүлэх»-ээр дахин ажиллуулна", "error")
                pipe.data.timeline.append(TimelineEvent(now_iso(), "Автомат CRUD: тест орчин хариу өгөөгүй",
                                                        M.BLOCKED, f"{len(rest)} цэс хойшлогдов"))
                break
        runs = await self._retry_with_masters(pipe, keys, runs)
        if not pipe.halt.is_set():
            await self._sweep_leftovers(pipe, keys)
        tested = [r for r in runs if not r.meta.get("skipped")]
        n_pass = sum(1 for r in tested if r.status == M.PASS)
        msg = (f"{len(keys)} цэс: шалгасан {len(tested)} (Pass {n_pass}), "
               f"автоматаар шалгаж чадаагүй {len(runs) - len(tested)}")
        if len(runs) < len(keys):
            msg += f", ажиллаагүй {len(keys) - len(runs)}"
        if not tested:
            return M.BLOCKED, msg
        if any(r.status == M.FAIL for r in tested):
            return M.FAIL, msg
        if any(r.status == M.BLOCKED for r in tested) or len(runs) < len(keys):
            return M.BLOCKED, msg
        return M.PASS, msg

    async def _flow(self, pipe: "PipelineRunner", key: str, code: str, spec: dict, title: str = "",
                    meta: dict | None = None, replace_at: int | None = None):
        """Нэг автомат урсгалыг ажиллуулж pipe.data.flows-д нэмнэ (replace_at — өмнөх оролдлогын оронд)."""
        item = menu_from_key(key)
        name = title or f"auto: {menu_path(item)}"
        cfg = {**pipe.cfg, "flows": {**(pipe.cfg.get("flows") or {}), name: spec}}
        eng = FlowEngine(pipe.url, name, cfg, pipe.run_dir, ctx=pipe.flow_ctx, runlog=pipe.runlog,
                         session=pipe.session, cancel_event=pipe.halt, emit=pipe.emit, run_id=pipe.run_id,
                         code=code, meta={"auto": True, "menu": menu_path(item), "menu_key": key, **(meta or {})})
        tabs0 = await _tabs(pipe)
        run = await eng.execute(pipe.page, pipe.col)
        if auto_infra_broken(run) and not pipe.halt.is_set():
            # Аппын bug биш — апп-ыг дахин ачаалж нэг удаа дахин шалгана
            first = next((x.message for x in run.steps if x.message), "")[:120]
            pipe.runlog.log(f"Автомат CRUD: «{menu_path(item)}» — орчны тасалдлын шинж ({first}) — дахин ачаалж "
                            "дахин шалгана", "warn")
            await ui.reload_app(eng, "орчны тасалдал (автомат CRUD)")
            eng = FlowEngine(pipe.url, name, cfg, pipe.run_dir, ctx=pipe.flow_ctx, runlog=pipe.runlog,
                             session=pipe.session, cancel_event=pipe.halt, emit=pipe.emit, run_id=pipe.run_id,
                             code=code, meta={"auto": True, "menu": menu_path(item), "menu_key": key, **(meta or {})})
            run = await eng.execute(pipe.page, pipe.col)
            run.meta["rechecked"] = True
            if auto_infra_broken(run):
                # Дахин ачаалсан ч мөн адил — цэсийг шалгаж чадаагүй (TC/bug үүсгэхгүй), Fail биш
                run.meta["infra"] = True
                run.meta["skipped"] = (f"Орчны тасалдал — апп хариу өгөхгүй/цэс олдохгүй байсан тул шалгаагүй "
                                       f"({next((x.message for x in run.steps if x.message), '')[:120]})")
        try:
            await ui.close_form(eng)          # дараагийн цэсэнд нээлттэй форм/цонх саад болохгүй
            await ui.clear_toasts(eng)        # хуучин анхааруулгын toast дараагийн цэсийг Fail болгохгүй
            # Форм шалгалт шиг цэсний табыг хаана — хуримтлагдвал апп удааширч, таб мөрөөс халин шинэ цэсний
            # товчнууд танигдахаа больдог (HR: A33–A50 «Нэмэх товч олдсонгүй»)
            extra = [t for t in await _tabs(pipe) if t not in tabs0]
            if extra:
                await ui.close_tabs(eng, extra)
        except Exception as e:
            pipe.runlog.log(f"Автомат CRUD: цэвэрлэгээний алдаа {e!r}", "warn", to_ui=False)
        if replace_at is None:
            pipe.data.flows.append(run.to_dict())
        else:
            pipe.data.flows[replace_at] = run.to_dict()
        label = f"алгассан: {run.meta['skipped']}" if run.meta.get("skipped") else run.status
        pipe.data.timeline.append(TimelineEvent(now_iso(), f"Автомат CRUD «{menu_path(item)}»"
                                                + (" (мастер)" if (meta or {}).get("setup") else ""), label,
                                                f"run_tag {run.run_tag}"))
        return run

    async def _sweep_leftovers(self, pipe: "PipelineRunner", keys: list[str]) -> None:
        """Хадгалсан ч өөрийн жагсаалтаас олдоогүй тестийн бичлэгийг ижил модулийн, нэр нь ижил үгээр эхэлсэн бусад
        жагсаалтаас (≤ 6) хайж устгана. testaccounting 2026-10-23: «ДҮ - ээс орлого» төрлийн баримт «ДҮ-ийн орлогын
        баримтын жагсаалт»-д харагдаагүй ч «ДҮ-ийн баримтын жагсаалт»-д харагддаг — run бүр 5–7 бичлэг үлддэг байсан.
        Зөвхөн энэ run-ы давтагдашгүй (run_tag-тай) утгатай яг тэнцүү мөрийг устгана — бодит өгөгдөлд хүрэхгүй."""
        left = [f for f in pipe.data.flows if (f.get("meta") or {}).get("auto") and (f.get("meta") or {}).get("leftover_values")]
        if not left:
            return
        pool = list(dict.fromkeys(list(keys) + [c["key"] for c in crud_candidates(pipe.data.results or [])]))
        first_word = lambda k: (menu_from_key(k).name.split() or [""])[0].lower()
        n = 0
        for f in left:
            own = f["meta"]["menu_key"]
            module = own.split("|")[0]
            others = [k for k in pool if k != own and k.split("|")[0] == module
                      and first_word(k) == first_word(own)][:6]
            for text in list(f["meta"]["leftover_values"]):
                for k in others:
                    if pipe.halt.is_set():
                        return
                    n += 1
                    data = f"sw{n}"
                    pipe.flow_ctx.data[f"{data}_try"] = text        # _rec биш — олдохгүй бол «үлдсэн» гэж тэмдэглэхгүй
                    spec = {"steps": [{"uses": "crud.auto_cleanup", "name": "Үлдсэн тестийн өгөгдлийг хайж устгах",
                                       "always": True, "with": {"menu": menu_path(menu_from_key(k)), "data": data}}]}
                    run = await self._flow(pipe, k, pipe.next_auto_code("S"), spec,
                                           f"cleanup: {menu_path(menu_from_key(k))}", {"setup": True, "sweep": True})
                    if run.meta.get("cleaned"):
                        tag = pipe.flow_ctx.run_tag
                        f["meta"]["leftover_values"].remove(text)
                        f["meta"]["leftover"] = [x for x in f["meta"].get("leftover") or []
                                                 if not x.startswith(text.replace(tag, "{run_tag}"))]
                        f["meta"].setdefault("swept", []).append(f"{text.replace(tag, '{run_tag}')} → "
                                                                 f"«{menu_path(menu_from_key(k))}»-ээс устгав")
                        pipe.runlog.log(f"Автомат CRUD: үлдсэн «{text}»-ийг «{menu_path(menu_from_key(k))}»-ээс олж устгав")
                        break
            if not f["meta"].get("leftover"):
                f["meta"].pop("leftover", None)
                if f["meta"].get("swept"):
                    # Цэвэрлэгээний алхам (Blocked «үлдсэн») — өөр жагсаалтаас олж устгасан тул Pass + тэмдэглэл
                    for st in f.get("steps") or []:
                        if st.get("uses") == "crud.auto_cleanup" and (st.get("message") or "").startswith("Тестийн өгөгдөл үлдсэн"):
                            st["status"], st["message"] = M.PASS, ""
                            st.setdefault("notes", []).extend(
                                ["Өөрийн жагсаалтаас олдоогүй — ижил модулийн өөр жагсаалтаас олж устгав: "
                                 + "; ".join(f["meta"]["swept"])])

    async def _retry_with_masters(self, pipe: "PipelineRunner", keys: list[str], runs: list) -> list:
        """Тест орчинд хоосон lookup («Ажилтан») улмаас алгассан цэсүүд: тохирох мастер цэсээр бичлэг үүсгэж
        (устгалгүй үлдээгээд) тэдгээрийг дахин ажиллуулна, эцэст нь мастер бичлэгийг устгана."""
        waiting: dict[str, list] = {}
        for r in runs:
            if r.meta.get("skipped") and r.meta.get("needs"):
                waiting.setdefault(r.meta["needs"], []).append(r)
        if not waiting:
            return runs
        pool = list(dict.fromkeys(list(keys) + [c["key"] for c in crud_candidates(pipe.data.results or [])]))
        out = list(runs)
        for label, deps in waiting.items():
            if pipe.halt.is_set():
                break
            dep_keys = {r.meta["menu_key"] for r in deps}
            mkey = master_for(label, [k for k in pool if k not in dep_keys])
            if not mkey:
                for r in deps:
                    self._annotate(pipe, r, f"«{label}» өгөгдлийг үүсгэх мастер цэс сонгогдоогүй — тэр цэсийг "
                                            "автомат CRUD-д сонговол түрүүлж үүсгээд дахин оролдоно")
                continue
            mcode = pipe.next_auto_code("M")
            mp = mcode.lower()
            mpath = menu_path(menu_from_key(mkey))
            setup = await self._flow(pipe, mkey, mcode, master_setup_spec(mkey, mp), f"master: {mpath}",
                                     {"setup": True, "for": label})
            value = pipe.flow_ctx.data.get(f"{mp}_rec")
            if setup.status != M.PASS or not value:
                for r in deps:
                    self._annotate(pipe, r, f"«{label}» мастер бичлэгийг «{mpath}» цэсээр үүсгэж чадсангүй "
                                            f"({setup.meta.get('skipped') or setup.status})")
            else:
                for r in deps:
                    if pipe.halt.is_set():
                        break
                    at = next(i for i, f in enumerate(pipe.data.flows)
                              if f.get("code") == r.code and f.get("flow") == r.flow)
                    again = await self._flow(pipe, r.meta["menu_key"], r.code,
                                             auto_flow_spec(r.meta["menu_key"], r.code.lower(), {label: value},
                                                            _columns(pipe, r.meta["menu_key"])),
                                             meta={"master": {"label": label, "menu": mpath}}, replace_at=at)
                    out[out.index(r)] = again
            await self._flow(pipe, mkey, mcode, master_teardown_spec(mkey, mp), f"master-cleanup: {mpath}",
                             {"setup": True, "for": label})
        return out

    @staticmethod
    def _annotate(pipe: "PipelineRunner", run, hint: str) -> None:
        run.meta["skipped"] = f"{run.meta['skipped']} [{hint}]"
        for f in pipe.data.flows:
            if f.get("code") == run.code and f.get("flow") == run.flow:
                f["meta"]["skipped"] = run.meta["skipped"]


# ------------------------------------------------------------------ formcheck
def form_flow_spec(key: str, caps: dict | None = None) -> dict:
    """Цэс бүрийн форм шалгалт (READ-ONLY). Алхмууд бие биеэсээ хамааралгүй (always)."""
    # Засварын шалгалт ЭХЭНД: цэвэр (дөнгөж нээсэн) жагсаалт дээр. Шинэ форм нээж хаасны дараа зарим
    # жагсаалт (HR: Компани, Салбар …) хоосон харагддаг байсан.
    empty = bool((caps or {}).get("empty"))      # smoke: жагсаалт «Бичлэг олдсонгүй» байсан
    steps = [{"uses": "form.check_edit", "name": "Бичлэгийг засварын горимд нээх", "always": True,
              "with": {"menu": key, "empty": empty}}]
    if caps is None or caps.get("add"):
        steps.append({"uses": "form.check_new", "name": "Шинэ форм + lookup-ууд нээх", "always": True,
                      "with": {"menu": key}})
    # Уншилтын товч (Хэвлэх / Экспорт) — товчгүй цэсэд N/A (тооцоонд орохгүй)
    steps.append({"uses": "form.check_actions", "name": "Хэвлэх / Экспорт товч шалгах", "always": True,
                  "with": {"menu": key, "empty": empty}})
    return {"steps": steps}


async def _tabs(pipe: "PipelineRunner") -> list[str]:
    st = await snapshot(pipe.page, pipe.cfg) if pipe.page else None
    return st.tab_texts() if st else []


@register
class FormCheckStage(Stage):
    """Форм шалгалт: цэс бүрийн Шинэ/засварын формыг нээж, lookup бүрийг ачаалж, алдааг илрүүлнэ — хадгалахгүй.

    Өгөгдөл өөрчлөхгүй ч зарим систем «Шинэ» дарахад баримтын дугаар нөөцөлдөг тул CRUD-тэй адил зөвхөн
    flow.allowed_hosts (тест орчин) дээр ажиллана."""
    kind = "formcheck"

    def name(self, spec: StepSpec) -> str:
        if spec.menus is None:
            return "Форм шалгалт (READ-ONLY, smoke-ийн дараа цэс сонгоно)"
        return f"Форм шалгалт (READ-ONLY, {len(spec.menus)} цэс)"

    def validate(self, url: str, cfg: dict, spec: StepSpec) -> None:
        if not host_allowed(url, cfg):
            raise PipelineConfigError(host_reason(url, cfg))
        for k in spec.menus or []:
            try:
                menu_from_key(k)
            except ValueError as e:
                raise PipelineConfigError(str(e)) from e

    async def run(self, pipe: "PipelineRunner", spec: StepSpec) -> tuple[str, str]:
        if spec.menus is None:
            cands = form_candidates(pipe.data.results)
            if not cands:
                return M.BLOCKED, "Smoke-оос Нэмэх/Засах товчтой цэс олдсонгүй"
            keys = await pipe.wait_form_selection(cands) if spec.select else [c["key"] for c in cands]
            caps = {c["key"]: c["caps"] for c in cands}
            targets = [(k, caps[k]) for k in keys]
            if not targets:
                return M.BLOCKED, "Форм шалгалт хийх цэс сонгогдоогүй — алгасав"
        else:
            targets = [(k, None) for k in spec.menus]
        runs = []
        t_clean = time.monotonic()
        targets = list(targets)
        deferred: set[str] = set()      # орчны тасалдлаас болж хойшлуулсан цэсүүд (төгсгөлд дахин нэг удаа)
        streak = 0                      # дараалсан тасалдал
        for n, (key, caps) in enumerate(targets):
            if pipe.halt.is_set():
                pipe.data.resume.setdefault(self.kind, []).extend(k for k, _ in targets[n:])
                break
            item = menu_from_key(key)
            code = pipe.next_auto_code("F")
            name = f"form: {menu_path(item)}"
            cfg = {**pipe.cfg, "flows": {**(pipe.cfg.get("flows") or {}), name: form_flow_spec(key, caps)}}
            prev = runs[-1] if runs else None

            def make_engine() -> FlowEngine:
                return FlowEngine(pipe.url, name, cfg, pipe.run_dir, ctx=pipe.flow_ctx, runlog=pipe.runlog,
                                  session=pipe.session, cancel_event=pipe.halt, emit=pipe.emit, run_id=pipe.run_id,
                                  code=code, meta={"formcheck": True, "menu": menu_path(item), "menu_key": key})

            async def attempt(eng: FlowEngine):
                tabs0 = await _tabs(pipe)
                run = await eng.execute(pipe.page, pipe.col)
                try:
                    await ui.close_form(eng)
                    await ui.clear_toasts(eng)
                    # Smoke шиг цэсний табыг хаана: нээлттэй таб хуримтлагдвал Blazor бүх табын компонентыг
                    # амьд байлгаж (polling, DOM) апп улам удааширдаг байсан (HR: 50+ таб)
                    extra = [t for t in await _tabs(pipe) if t not in tabs0]
                    if extra:
                        await ui.close_tabs(eng, extra)
                        left = [t for t in await _tabs(pipe) if t in extra]
                        if left:
                            pipe.runlog.log(f"Форм шалгалт: таб хаагдсангүй — {left}", "warn", to_ui=False)
                except Exception as e:
                    pipe.runlog.log(f"Форм шалгалт: цэвэрлэгээний алдаа {e!r}", "warn", to_ui=False)
                return run

            def update_prev() -> None:
                # өмнөх урсгал аль хэдийн хадгалагдсан — уналтыг нэмсэн хувилбараар солино
                if pipe.data.flows and pipe.data.flows[-1].get("code") == prev.code:
                    pipe.data.flows[-1] = prev.to_dict()

            t0 = time.monotonic()
            eng = make_engine()
            # Өмнөх цэсийг хаасны дараа апп (.NET runtime) унасан бол энэ цэсийг эвдэрсэн апп дээр шалгахгүй
            if pipe.col is not None and not pipe.halt.is_set():
                st = await snapshot(pipe.page, pipe.cfg)
                if st is not None and st.dead:
                    if prev is not None:
                        await pipe.col.drain()
                        crash = pipe.col.findings_from(pipe.col.since(t_clean))
                        blame_previous(prev, crash, "дараагийн цэс эхлэхээс өмнө")
                        update_prev()
                        await ui.reload_app(eng, f"«{prev.meta.get('menu')}» табыг хаасны дараа апп унасан")
                    else:
                        # өмнөх алхмын (smoke) сүүлийн цэсийн дараа унасан — smoke аль хэдийн бүртгэсэн
                        await ui.reload_app(eng, "форм шалгалт эхлэхэд апп (.NET runtime) унасан байсан")
            run = await attempt(eng)
            # Blazor компонент унасан бол (unhandled rendering exception) апп буруу төлөвт орсон — дараагийн цэс
            # эвдэрсэн апп дээр шалгагдаж худал «таб нээгдсэнгүй» болохоос сэргийлж дахин ачаална
            crashed = any(f.kind in _CRASH_KINDS for s in run.steps for f in s.findings)
            if crashed and not pipe.halt.is_set():
                await ui.reload_app(eng, f"«{menu_path(item)}» дээр Blazor компонент унасан")
                run.meta["reloaded"] = True
                pages = {page_stem(p) for p in crash_pages(run)}
                prev_stem = page_stem(menu_route(prev.meta.get("menu_key", ""))) if prev is not None else ""
                consequence = not pages and all(
                    "already exited" in f"{f.text} {f.detail or ''}"
                    for s in run.steps for f in s.findings if f.kind in _CRASH_KINDS)
                if consequence:
                    # Апп энэ цэсийг нээхээс өмнө унасан байсан (өөрийн exception алга) — энэ цэсийн bug биш
                    pipe.runlog.log(f"Форм шалгалт: «{menu_path(item)}» — апп өмнө нь унасан байсан "
                                    "(runtime already exited) — дахин шалгана", "warn")
                    run = await attempt(make_engine())
                    run.meta["rechecked"] = True
                elif prev is not None and prev_stem in pages:
                    # Уналтын stack trace өмнөх цэсийн хуудсыг заана (ж: RPARobotList-ийн timer таб хаагдсаны
                    # дараа ажиллаж runtime-ийг унагав) — энэ цэсийн алдаа биш: өмнөх цэс рүү шилжүүлж, энэ цэсийг
                    # дахин ачаалсан апп дээр шинээр шалгана
                    # зөвхөн өмнөх цэсийн хуудсыг заасан (эсвэл хуудасгүй ерөнхий runtime уналт) илрэл — энэ цэсийн
                    # өөрийн уналт (ж: OrganizationForm) энэ цэсэд үлдэнэ (дахин шалгахад дахин илэрнэ)
                    crash = [f for s in run.steps for f in s.findings
                             if (f.kind in _CRASH_KINDS or f.kind == M.CONSOLE_ERROR)
                             and (not finding_pages(f) or prev_stem in finding_pages(f))]
                    blame_previous(prev, crash, f"«{run.meta.get('menu')}» цэсийг нээх үед илэрсэн")
                    update_prev()
                    pipe.runlog.log(f"Форм шалгалт: уналт «{prev.meta.get('menu')}»-д хамаарна — "
                                    f"«{menu_path(item)}»-г дахин шалгана", "warn")
                    run = await attempt(make_engine())
                    run.meta["rechecked"] = True
                    if any(f.kind in _CRASH_KINDS for s in run.steps for f in s.findings) and not pipe.halt.is_set():
                        await ui.reload_app(eng, f"«{menu_path(item)}» дээр Blazor компонент унасан")
                        run.meta["reloaded"] = True
            # Орчны тасалдал (сервертэй холбогдож чадаагүй, хуудас шилжсэн, цэсний мод алга): аппын bug биш —
            # дахин ачаалж дахин шалгана; хэвээр бол хойшлуулж, дараалсан 3 бол зогсоож үлдсэнийг resume-д хадгална
            if infra_broken(run) and not pipe.halt.is_set():
                pipe.runlog.log(f"Форм шалгалт: «{menu_path(item)}» — орчны тасалдлын шинж "
                                f"({next((s.message for s in run.steps if s.message), '')[:120]}) — дахин ачаалж дахин шалгана", "warn")
                await ui.reload_app(eng, "орчны тасалдал")
                run = await attempt(make_engine())
                run.meta["rechecked"] = True
            if infra_broken(run) and not pipe.halt.is_set():
                if key not in deferred:
                    deferred.add(key)
                    streak += 1
                    if streak >= 3:
                        rest = [k for k, _ in targets[n + 1:]]
                        pend = [k for k in deferred if k not in rest]
                        pipe.data.resume.setdefault(self.kind, []).extend(pend + rest)
                        pipe.runlog.log(f"Форм шалгалт: тест орчин хариу өгөхгүй байна ({streak} цэс дараалан) — "
                                        f"зогсоов; {len(pend + rest)} цэсийг «Үлдсэн цэсүүдийг үргэлжлүүлэх»-ээр дахин "
                                        "ажиллуулна", "error")
                        pipe.data.timeline.append(TimelineEvent(now_iso(), "Форм шалгалт: тест орчин хариу өгөөгүй",
                                                                M.BLOCKED, f"{len(pend + rest)} цэс хойшлогдов"))
                        break
                    targets.append((key, caps))
                    pipe.runlog.log(f"Форм шалгалт: «{menu_path(item)}» хойшлогдов (орчны тасалдал) — төгсгөлд дахин "
                                    "шалгана", "warn")
                    t_clean = time.monotonic()
                    continue
                # хоёр дахь оролдлого ч тасалдсан — Fail биш, «шалгаж чадаагүй» (Blocked)
                for s in run.steps:
                    if s.status == M.FAIL:
                        s.status, s.category = M.BLOCKED, "tool"
                        s.message = f"Тест орчин хариу өгөөгүй: {s.message}"[:500]
            else:
                streak = 0
            t_clean = time.monotonic()
            run.meta["seconds"] = round(time.monotonic() - t0, 1)
            done = runs + [run]
            avg = sum(r.meta.get("seconds", 0) for r in done) / len(done)
            counts = {k: sum(1 for r in done if r.status == k) for k in (M.PASS, M.FAIL, M.BLOCKED)}
            last_fail = next((r for r in reversed(done) if r.status == M.FAIL), None)
            pipe.emit("progress", {
                "i": n + 1, "n": len(targets), "current": f"🧾 {menu_path(item)} · {run.meta['seconds']}с",
                "counts": counts, "logouts": len(pipe.data.logouts), "crashes": len(pipe.data.crashes),
                "eta_s": int(avg * (len(targets) - n - 1)), "status": run.status, "stage": "formcheck",
                "last_error": (f"{last_fail.meta.get('menu')}: " + next(
                    (s.message for s in last_fail.steps if s.status == M.FAIL), ""))[:200] if last_fail else ""})
            pipe.runlog.log(f"[{n + 1}/{len(targets)}] {menu_path(item)} → {run.status} · {run.meta['seconds']}с")
            if pipe.halt.is_set() and not any(s.status in (M.PASS, M.FAIL) for s in run.steps):
                # Session тасарч (эсвэл зогсоож) нэг ч алхам ажиллаагүй — Blocked биш «ажиллаагүй», дараа үргэлжлүүлнэ
                pipe.data.resume.setdefault(self.kind, []).extend(k for k, _ in targets[n:])
                break
            pipe.data.flows.append(run.to_dict())
            runs.append(run)
            pipe.data.timeline.append(TimelineEvent(now_iso(), f"Форм шалгалт «{menu_path(item)}»", run.status, ""))
        if pipe.data.resume.get(self.kind):
            pipe.data.resume[self.kind] = list(dict.fromkeys(pipe.data.resume[self.kind]))
        targets = list({k: (k, c) for k, c in targets}.values())
        cnt = {k: sum(1 for r in runs if r.status == k) for k in (M.PASS, M.FAIL, M.BLOCKED)}
        msg = f"{len(targets)} цэс: Pass {cnt[M.PASS]} / Fail {cnt[M.FAIL]} / Blocked {cnt[M.BLOCKED]}"
        na = sum(1 for r in runs if any(s.category in ("nodata", "na") and s.uses == "form.check_edit"
                                        for s in r.steps))
        if na:
            msg += f" · N/A (өгөгдөлгүй/хамаарахгүй алхамтай) {na}"
        if runs:
            secs = [r.meta.get("seconds", 0) for r in runs]
            msg += f" · дунджаар {sum(secs) / len(secs):.0f} сек/цэс"
            pipe.runlog.log(f"Форм шалгалт: {len(runs)} цэс, дунджаар {sum(secs) / len(secs):.1f} сек/цэс, "
                            f"хамгийн удаан: " + ", ".join(f"{r.meta.get('menu')} {r.meta.get('seconds')}с"
                                                        for r in sorted(runs, key=lambda r: -r.meta.get('seconds', 0))[:3]))
        if len(runs) < len(targets):
            msg += (f", ажиллаагүй {len(targets) - len(runs)} (session тасарсан/зогсоосон — "
                    "«Үлдсэн цэсүүдийг үргэлжлүүлэх»-ээр дахин ажиллуулна)")
        if cnt[M.FAIL]:
            return M.FAIL, msg
        return (M.BLOCKED if cnt[M.BLOCKED] or len(runs) < len(targets) else M.PASS), msg


# --------------------------------------------------------------------- record
@register
class RecordStage(Stage):
    """Урсгал бичих (record → replay): хэрэглэгч хөтөч дээр гараар үйлдэл хийж, «⏹ Дуусгах» дарна →
    YAML урсгал үүснэ (хадгалахаас өмнө UI-д урьдчилан харж засна). Бичих үед хэрэгсэл юу ч дарахгүй."""
    kind = "record"
    mutates = True              # бичсэн урсгалыг тоглуулахад өгөгдөл өөрчилнө — зөвхөн тест орчинд

    def name(self, spec: StepSpec) -> str:
        return f"Урсгал бичих: {spec.flow or '—'}"

    def validate(self, url: str, cfg: dict, spec: StepSpec) -> None:
        super().validate(url, cfg, spec)
        if not (spec.flow or "").strip():
            raise PipelineConfigError("Бичих урсгалын нэрийг оруулна уу")

    async def run(self, pipe: "PipelineRunner", spec: StepSpec) -> tuple[str, str]:
        import yaml

        from app.flows.recorder import Recorder, build_flow, describe

        def on_event(ev: dict) -> None:
            pipe.emit("rec_event", {"i": len(rec.events), "text": describe(ev)})
            pipe.runlog.log(f"🎥 {describe(ev)}", to_ui=False)

        rec = Recorder(pipe.cfg, on_event)
        await rec.start(pipe.page)
        pipe.record_done = asyncio.get_running_loop().create_future()
        pipe.emit("status", {"state": "recording"})
        pipe.runlog.log("🎥 Бичиж байна — хөтөч дээр урсгалаа гараар хийгээд UI-аас «⏹ Дуусгах» дарна уу")
        timeout = (pipe.cfg.get("timing") or {}).get("record_timeout_ms", 3600000) / 1000
        try:
            await asyncio.wait_for(pipe.record_done, timeout)
        except asyncio.TimeoutError:
            pipe.runlog.log("Бичлэгийн хугацаа дууслаа — бүртгэсэн үйлдлүүдээс урсгал үүсгэнэ", "warn")
        finally:
            pipe.record_done = None
            await rec.stop(pipe.page)
        flow, warnings = build_flow(rec.events)
        text = yaml.safe_dump(flow, allow_unicode=True, sort_keys=False, width=120)
        # Оношлогоо: хүний бодит үйлдлүүд (бичлэг) ↔ үүссэн алхмууд — тоглуулалт зөрвөл юуг яаж ойлгосныг харах
        import json as _json
        (pipe.run_dir / "recording.json").write_text(_json.dumps(
            {"name": spec.flow, "events": [{**ev, "text": describe(ev)} for ev in rec.events],
             "steps": flow["steps"], "warnings": warnings}, ensure_ascii=False, indent=1), encoding="utf-8")
        pipe.data.recording = {"name": spec.flow, "yaml": text, "warnings": warnings,
                               "events": len(rec.events), "steps": len(flow["steps"])}
        pipe.emit("rec_done", pipe.data.recording)
        msg = f"{len(rec.events)} үйлдэл → {len(flow['steps'])} алхам" + (f" · ⚠ {len(warnings)}" if warnings else "")
        return (M.PASS if flow["steps"] else M.BLOCKED), msg
