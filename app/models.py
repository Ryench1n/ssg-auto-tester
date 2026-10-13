"""Өгөгдлийн бүтэц (runner → classify → report)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ---- Илрэлийн төрөл -------------------------------------------------------
RUNTIME_CRASH = "runtime_crash"        # .NET runtime уналт (апп зогссон)
AUTH_ERROR = "auth_error"              # UserRefreshToken г.м.
SESSION_LOGOUT = "session_logout"      # session тогтмол тасрах (run түвшин)
LOGIN_FAILED = "login_failed"
PAGE_RELOAD = "page_reload"            # SPA бүтэн reload
SYSTEM_ERROR = "system_error"          # модал "Системийн алдаа" / SQL / алдааны toast
HTTP_5XX = "http_5xx"
LOADING_STUCK = "loading_stuck"
TIMEOUT = "timeout"
FORBIDDEN = "forbidden"                # 403
RENDERER_EXCEPTION = "renderer_exception"
CONSOLE_ERROR = "console_error"
TAB_NAME_MISMATCH = "tab_name_mismatch"    # бичгийн/орчуулгын ялгаа (ижил хуудас) — Minor, анхааруулга
TAB_WRONG_PAGE = "tab_wrong_page"          # табын нэр огт өөр — буруу хуудас нээгдэж болзошгүй (Major)
NO_TAB = "no_tab"
WARNING_TOAST = "warning_toast"
HTTP_4XX = "http_4xx"
FLOW_STEP_FAIL = "flow_step_fail"      # E2E (crud) урсгалын алхам амжилтгүй
NA = "N/A"                             # урсгалын бүх алхам хамаарахгүй / өгөгдөлгүй (тооцоонд орохгүй)
BLANK_PAGE = "blank_page"              # таб нээгдсэн ч агуулга хоосон (цагаан дэлгэц)
SLOW_LOAD = "slow_load"                # цэс босгоос удаан ачааллав (анхааруулга)
FORM_WARNING = "form_warning"          # форм шалгалт: хоосон сонголтын жагсаалт, удаан форм г.м. (Minor, алхам Pass)

# Нэг цэсэд "үндсэн" илрэл байвал хоёрдогч (консол, HTTP, табын нэр) илрэлүүд
# тусдаа bug болохгүй, үндсэн bug-ийн нотолгоо болж хавсрагдана.
PRIMARY_KINDS = {RUNTIME_CRASH, AUTH_ERROR, PAGE_RELOAD, SYSTEM_ERROR, LOADING_STUCK,
                 TIMEOUT, FORBIDDEN, WARNING_TOAST, BLANK_PAGE}

PASS, FAIL, BLOCKED, PENDING = "Pass", "Fail", "Blocked", "Pending"


@dataclass
class MenuItem:
    module: str
    folder: str
    name: str
    href: str
    index: int = 0
    risky: bool = False
    skip: bool = False

    @property
    def key(self) -> str:
        # Нэг модульд ижил нэртэй цэс байж болно → module+folder+name+href
        return f"{self.module}|{self.folder}|{self.name}|{self.href}"


@dataclass
class Finding:
    kind: str
    text: str = ""
    detail: str = ""              # stack trace, URL, г.м.
    after_close: bool = False     # таб хаасны дараах хоцорсон уналт
    status: int | None = None
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ItemResult:
    item: MenuItem
    status: str = PENDING
    findings: list[Finding] = field(default_factory=list)
    blocked_reason: str = ""
    tab_opened: bool = False
    tab_name: str = ""
    load_ms: int | None = None
    screenshot: str = ""
    attempts: int = 0
    confirmation: str = ""        # "Баталгаажсан (2/2)" / "1 удаа ажиглагдсан"
    notes: list[str] = field(default_factory=list)
    checked_at: str = ""
    # Цэсний үйлдлийн бүртгэл (READ-ONLY scan): {buttons, columns, search, grid, add, edit, delete, crud}
    caps: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["item"]["key"] = self.item.key
        return d


@dataclass
class TimelineEvent:
    at: str                        # ISO цаг
    activity: str
    result: str = ""
    note: str = ""


@dataclass
class RunData:
    run_id: str
    url: str
    project: str
    qa_lead: str = ""
    tester: str = ""                   # тест гүйцэтгэсэн QA (гүйцэтгэгч = QA Tester = Author = тайлан бэлтгэсэн)
    version_label: str = "v1.0"
    app_version: str = ""
    started_at: str = ""
    finished_at: str = ""
    login_ok: bool = False
    login_error: str = ""
    login_timeout: bool = False        # хэрэглэгч хугацаандаа гараар нэвтрээгүй (аппын алдаа биш)
    login_times: list[str] = field(default_factory=list)
    logouts: list[dict[str, Any]] = field(default_factory=list)   # {at, after_s, during}
    crashes: list[dict[str, Any]] = field(default_factory=list)   # {at, item, reloads, recovered}
    results: list[ItemResult] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    cancelled: bool = False
    modules_selected: list[str] = field(default_factory=list)
    discovered: dict[str, int] = field(default_factory=dict)      # module -> цэсийн тоо
    session_observed_s: float = 0.0    # хамгийн урт тасралтгүй session (сек)
    temp_5xx: list[str] = field(default_factory=list)
    # Pipeline: алхам бүрийн товч төлөв {kind, name, flow?, status, message, started_at, finished_at}
    pipeline: list[dict[str, Any]] = field(default_factory=list)
    smoke_ran: bool = False
    run_tag: str = ""
    flows: list[dict[str, Any]] = field(default_factory=list)      # FlowRun.to_dict() жагсаалт
    draft: str = ""                    # автомат CRUD-оос үүсгэсэн flows YAML ноорогийн файлын нэр
    # Session тасарсан / зогсоосны улмаас шалгагдаагүй цэсүүд {"formcheck": [key…]} — дараагийн run-д үргэлжлүүлнэ
    resume: dict[str, list[str]] = field(default_factory=dict)
    # «Урсгал бичих» run-ы үр дүн {name, yaml, warnings, events} — UI урьдчилан харж хадгална
    recording: dict[str, Any] = field(default_factory=dict)
    # Форм шалгалт: өмнөх (ижил систем) run-тай харьцуулалт {prev_version, new_fail, fixed, still_fail}
    compare: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            **{k: v for k, v in asdict(self).items() if k not in ("results", "timeline")},
            "results": [r.to_dict() for r in self.results],
            "timeline": [asdict(t) for t in self.timeline],
        }


@dataclass
class TestCase:
    tc_id: str
    req_ref: str
    purpose: str
    precondition: str
    steps: str
    expected: str
    test_type: str
    priority: str          # xlsx утга (🔴 Critical / 🟡 Major / 🟢 Minor)
    automation: str
    result: str
    executor: str
    date: str
    bug_ids: list[str] = field(default_factory=list)
    note: str = ""
    module: str = ""
    menu_key: str = ""
    screenshot: str = ""


@dataclass
class Bug:
    bug_id: str
    signature: str
    kind: str
    severity: Any          # Severity
    title: str
    modules: list[str] = field(default_factory=list)
    tc_ids: list[str] = field(default_factory=list)
    steps: str = ""
    expected: str = ""
    actual: str = ""
    stack: str = ""
    screenshot: str = ""
    confirmed: str = ""
    fix: str = ""
    security_note: str = ""
    status: str = "Open"
    found_date: str = ""
    order: int = 0
