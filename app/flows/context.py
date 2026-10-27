"""Component-based E2E: алхам хоорондын өгөгдөл (FlowContext) ба үр дүнгийн бүтэц."""
from __future__ import annotations

import secrets
import string
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.models import BLOCKED, FAIL, NOT_TESTED, PASS, PENDING, Finding


class StepFailed(Exception):
    """Алхам бизнесийн утгаараа амжилтгүй (ж: хадгалахад алдааны цонх гарсан)."""

    def __init__(self, msg: str, findings: list[Finding] | None = None):
        super().__init__(msg)
        self.findings = findings or []


class StepBlocked(StepFailed):
    """Алхмыг гүйцэтгэх боломжгүй (аппын bug биш) → Blocked.

    skip_flow=True бол (ж: автомат CRUD формыг бөглөж чадсангүй, Устгах товчгүй) урсгал бүхэлдээ
    «автоматаар шалгаж чадаагүй» гэж тэмдэглэгдэж TC/bug үүсгэхгүй."""

    def __init__(self, msg: str, findings: list[Finding] | None = None, skip_flow: bool = False,
                 category: str = ""):
        super().__init__(msg, findings)
        self.skip_flow = skip_flow
        # Яагаад шалгаж чадаагүй: "nodata" — тест орчинд өгөгдөл алга (N/A, тооцоонд орохгүй);
        # "tool" — хэрэгсэл UI-г таньж чадсангүй (сайжруулах); "" — бусад (Blocked)
        self.category = category


class MissingData(KeyError):
    """Алхмын аргументэд {key} байгаа ч өмнөх алхам тэр өгөгдлийг гаргаагүй."""


_TAGS_USED: set[str] = set()
_TAGS_LOCK = threading.Lock()


def make_run_tag() -> str:
    # Тестийн өгөгдөл бүрд орох давтагдашгүй угтвар — цэвэрлэх, мөргөлдөхөөс сэргийлэх. 20+ систем зэрэг
    # ажиллахад нэг процесс доторх run бүр ӨӨР tag авна (4 hex санамсаргүй давхцлыг бүртгэлээр хаана).
    with _TAGS_LOCK:
        while True:
            tag = f"SSG{datetime.now():%m%d}{secrets.token_hex(2).upper()}"
            if tag not in _TAGS_USED:
                _TAGS_USED.add(tag)
                return tag


_FMT = string.Formatter()


def placeholders(value: Any) -> set[str]:
    """Аргумент доторх {key} нэрсийг (run_tag-аас бусад) буцаана — алхмын `requires`."""
    out: set[str] = set()
    if isinstance(value, str):
        for _, name, _, _ in _FMT.parse(value):
            if name:
                out.add(name.split(".")[0].split("[")[0])
    elif isinstance(value, dict):
        for v in value.values():
            out |= placeholders(v)
    elif isinstance(value, list):
        for v in value:
            out |= placeholders(v)
    out.discard("run_tag")
    out.discard("today")
    return out


@dataclass
class FlowContext:
    run_tag: str
    data: dict[str, Any] = field(default_factory=dict)       # алхмуудын гаргасан ID/утга

    def render(self, value: Any) -> Any:
        if isinstance(value, str):
            # {today} — бичсэн урсгалын «өнөөдрийн огноо» (YYYY-MM-DD)
            names = {"run_tag": self.run_tag, "today": datetime.now().strftime("%Y-%m-%d"), **self.data}
            missing = placeholders(value) - set(names)
            if missing:
                raise MissingData(", ".join(sorted(missing)))
            return value.format_map(names)
        if isinstance(value, dict):
            return {k: self.render(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render(v) for v in value]
        return value


@dataclass
class StepResult:
    index: int
    uses: str
    name: str
    status: str = PENDING
    requires: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    findings: list[Finding] = field(default_factory=list)
    screenshot: str = ""
    started_at: str = ""
    duration_ms: int | None = None
    role: str = ""
    notes: list[str] = field(default_factory=list)
    always: bool = False           # цэвэрлэх (always: true) алхам
    args: dict[str, Any] = field(default_factory=dict)   # YAML-ын (render хийгээгүй) аргумент — тайланд
    retried: bool = False          # session тасарсны дараа дахин ажилласан
    category: str = ""             # Blocked-ийн ангилал: nodata (N/A) | tool | ""
    cause: str = ""                # Pass/Fail биш бол шалтгаан: app | tool | data | dependent | operator | na


@dataclass
class FlowRun:
    run_id: str
    url: str
    flow: str
    run_tag: str
    code: str = ""                 # TC-FLOW-<code>-<№>
    description: str = ""
    started_at: str = ""
    finished_at: str = ""
    app_version: str = ""
    steps: list[StepResult] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    # Автомат CRUD: {auto: True, menu, menu_key, fields: {шошго: утгын загвар}, key, edit_fields, skipped}
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        # «nodata» (тест орчинд өгөгдөлгүй — N/A) алхмууд урсгалын төлөвт нөлөөлөхгүй
        sts = {s.status for s in self.steps
               if not (s.status in (BLOCKED, NOT_TESTED) and (s.category in ("nodata", "na") or s.cause in ("data", "na")))}
        if self.error or FAIL in sts:
            return FAIL
        if BLOCKED in sts:
            return BLOCKED          # апп талын нотолгоотой саад
        if NOT_TESTED in sts or not self.steps:
            return NOT_TESTED       # хэрэгсэл/хэрэглэгч/өмнөх алхмын улмаас бүрэн шалгагдаагүй
        if not sts:     # бүх алхам N/A (өгөгдөлгүй / хамаарахгүй) — шалгах зүйлгүй, Blocked биш
            return "N/A"
        return PASS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status
        return d
