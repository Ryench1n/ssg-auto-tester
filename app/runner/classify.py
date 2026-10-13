"""Илрэл → signature → Bug, эрэмбэ, засах санал; цэс → Test Case; GO/NO-GO."""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import datetime
from urllib.parse import urlparse

from app import models as M
from app.models import Bug, Finding, ItemResult, RunData, TestCase
from app.runner.severity import Severity

# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------
_GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def normalize_text(text: str, limit: int = 200) -> str:
    """Текстийн хэв: GUID, тоог арилгаж, хоосон зайг нэгтгэнэ."""
    t = _GUID.sub("<guid>", text or "")
    t = _HEX.sub("<hex>", t)
    t = _NUM.sub("#", t)
    t = _WS.sub(" ", t).strip()
    return t[:limit]


def first_exception_line(text: str) -> str:
    """Stack trace-ээс 'System.XxxException: message' мөрийг олно."""
    for line in (text or "").splitlines():
        line = line.strip()
        if re.search(r"[A-Za-z.]+Exception\b", line) and not line.startswith("at "):
            return line.replace("Unhandled Exception:", "").strip() or line
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[0] if lines else ""


def _url_pattern(url: str) -> str:
    p = urlparse(url or "")
    return normalize_text(p.path or url, 120)


def signature(f: Finding) -> str:
    k = f.kind
    if k in (M.PAGE_RELOAD, M.SESSION_LOGOUT, M.LOGIN_FAILED, M.NO_TAB, M.TAB_NAME_MISMATCH,
             M.LOADING_STUCK, M.TIMEOUT, M.FORBIDDEN, M.BLANK_PAGE, M.SLOW_LOAD, M.TAB_WRONG_PAGE):
        return k
    if k == M.RUNTIME_CRASH:
        return f"{k}|{normalize_text(first_exception_line(f.detail or f.text), 160)}"
    if k in (M.HTTP_5XX, M.HTTP_4XX):
        return f"{k}|{f.status}|{_url_pattern(f.url)}"
    return f"{k}|{normalize_text(f.text)}"


# --------------------------------------------------------------------------
# Эрэмбэ, гарчиг, засах санал
# --------------------------------------------------------------------------
BASE_SEVERITY = {
    M.RUNTIME_CRASH: Severity.CRITICAL,
    M.AUTH_ERROR: Severity.CRITICAL,
    M.SESSION_LOGOUT: Severity.CRITICAL,
    M.LOGIN_FAILED: Severity.CRITICAL,
    M.PAGE_RELOAD: Severity.MAJOR,
    M.SYSTEM_ERROR: Severity.MAJOR,
    M.HTTP_5XX: Severity.MAJOR,
    M.LOADING_STUCK: Severity.MAJOR,
    M.TIMEOUT: Severity.MAJOR,
    M.FORBIDDEN: Severity.MINOR,
    M.FORM_WARNING: Severity.MINOR,
    M.RENDERER_EXCEPTION: Severity.MINOR,
    M.CONSOLE_ERROR: Severity.MINOR,
    M.TAB_NAME_MISMATCH: Severity.MINOR,
    M.TAB_WRONG_PAGE: Severity.MAJOR,
    M.NO_TAB: Severity.MINOR,
    M.WARNING_TOAST: Severity.MINOR,
    M.HTTP_4XX: Severity.MINOR,
    M.FLOW_STEP_FAIL: Severity.MAJOR,
    M.BLANK_PAGE: Severity.MAJOR,
    M.SLOW_LOAD: Severity.MINOR,
}

# Flow-ын алхмын илрэлд эдгээр байвал bug 🔴 Critical (апп уналт, token алдаа)
FLOW_CRITICAL_KINDS = {k for k, v in BASE_SEVERITY.items() if v == Severity.CRITICAL}

FLOW_EXPECTED = {
    "auth.login": "Гараар нэвтэрсэн идэвхтэй session-ээр апп нээлттэй байх",
    "auth.expect_access": "Цэсэд хандах эрх тохиргоонд заасантай таарах",
    "crud.open": "Цэс алдаагүй нээгдэх",
    "crud.create": "Бичлэг алдаагүй хадгалагдаж, форм хаагдах",
    "crud.search": "Хайлтын үр дүн хүлээгдэж буйтай таарах",
    "crud.update": "Засвар алдаагүй хадгалагдах",
    "crud.delete": "Бичлэг устаж, дахин хайхад олдохгүй байх",
    "form.check_new": ("Шинэ форм алдаагүй нээгдэж, формын lookup цонх, ComboBox жагсаалт, мөрийн grid-ийн "
                       "lookup бүр алдаагүй ачаалагдах; юу ч хадгалахгүй"),
    "form.check_edit": ("Бүртгэлтэй бичлэгүүд (эхний 3 хүртэл) засварын горимд алдаагүй нээгдэж, өгөгдөл нь "
                        "форм дээр ачаалагдах; юу ч хадгалахгүй"),
    "form.close_tab": "Цэсийн табыг хаахад апп (.NET runtime) хэвийн ажилласаар байх",
    "form.check_actions": ("Уншилтын товч (Хэвлэх / Экспорт / PDF харах) дарахад системийн алдаа гарахгүй; "
                           "өгөгдөл өөрчлөгдөхгүй"),
}

# Форм шалгалтын (READ-ONLY) алхмын хүнд ойлгомжтой тайлбар (TC-ийн «Алхмууд»)
FORM_STEPS = {
    "form.check_new": "\n".join([
        "1. {path} цэсийг нээх",
        "2. «Нэмэх/Шинэ» дарах (төрөл сонгох цэстэй бол төрөл бүрийг)",
        "3. Формын lookup («...») бүрийг нээж ачаалагдахыг хүлээх — сонгохгүй хаах",
        "4. ComboBox бүрийн жагсаалтыг нээж хаах (сонгохгүй)",
        "5. Мөрийн grid байвал «+» мөр нэмж мөрийн lookup-ийг нээх (хадгалахгүй)",
        "6. Формыг хадгалалгүй хаах"]),
    "form.check_edit": "\n".join([
        "1. {path} цэсийг нээх (жагсаалт хоосон бол «Сэргээх», огнооны шүүлтүүрийг сунгах)",
        "2. Эхний бичлэгийг засварын горимд нээх (мөр дарах / «Засах»), lookup / ComboBox / дотоод табыг шалгах",
        "3. Дараагийн 2 бичлэгийг засварын горимд нээж алдаа, өгөгдөл ачаалагдсан эсэхийг шалгах",
        "4. Хадгалалгүй хаах"]),
    "form.close_tab": "\n".join([
        "1. {path} цэсийг нээж ашиглах",
        "2. Цэсийн табыг хаах",
        "3. Дараагийн цэсийг нээх — апп унахгүй (.NET runtime зогсохгүй) байх"]),
    "form.check_actions": "\n".join([
        "1. {path} цэсийг нээх",
        "2. Уншилтын товч (Хэвлэх / Экспорт / PDF харах) бүрийг дарах",
        "3. Алдааны цонх / 5xx гарах эсэхийг шалгаж, нээгдсэн цонхыг хаах (файл татахыг цуцлах)"]),
}


def severity_for(kind: str, occurrences: int = 1, cfg: dict | None = None) -> Severity:
    sev = BASE_SEVERITY.get(kind, Severity.MINOR)
    if kind == M.FORBIDDEN:
        threshold = int(((cfg or {}).get("detect") or {}).get("forbidden_major_threshold", 5))
        if occurrences >= threshold:
            sev = Severity.MAJOR
    return sev


def title_for(f: Finding) -> str:
    k = f.kind
    short = (f.text or "").strip().replace("\n", " ")[:160]
    if k == M.RUNTIME_CRASH:
        exc = first_exception_line(f.detail or f.text)[:140]
        where = " (таб хаасны дараа)" if f.after_close else ""
        return f".NET runtime уналт{where} — апп бүхэлдээ зогссон: {exc}"
    if k == M.AUTH_ERROR:
        return f"Нэвтрэлтийн (token) алдаа: {short}"
    if k == M.SESSION_LOGOUT:
        return "Session тогтмол тасарч /auth/login руу шилжүүлдэг (token сунгалт ажиллахгүй)"
    if k == M.LOGIN_FAILED:
        return f"Системд нэвтэрч чадахгүй: {short}"
    if k == M.PAGE_RELOAD:
        return "Цэс нээхэд SPA бүтэн дахин ачаалагдаж, нээлттэй табууд алдагдана"
    if k == M.SYSTEM_ERROR:
        return f"Системийн алдааны цонх гарна: {short}"
    if k == M.HTTP_5XX:
        return f"Сервер HTTP {f.status} алдаа буцаана: {_url_pattern(f.url)}"
    if k == M.LOADING_STUCK:
        return "Дэлгэц \"Loading…\" төлөвт гацаж контент гарахгүй"
    if k == M.TIMEOUT:
        return "Цэсийн шалгалт хугацаа хэтэрсэн (timeout)"
    if k == M.FORBIDDEN:
        return "Эрхгүй (403) цэс хэрэглэгчид харагдаж байна"
    if k == M.RENDERER_EXCEPTION:
        return f"Компонент рендерлэх үеийн exception (апп амьд): {first_exception_line(f.detail or f.text)[:140]}"
    if k == M.CONSOLE_ERROR:
        return f"Консолын алдаа: {short}"
    if k == M.TAB_WRONG_PAGE:
        return "Цэс өөр нэртэй хуудас нээж байна (буруу хуудас нээгдэж болзошгүй)"
    if k == M.TAB_NAME_MISMATCH:
        return "Нээгдсэн табын нэр цэсний нэртэй зөрнө"
    if k == M.NO_TAB:
        return "Цэс дарахад таб нээгдэхгүй"
    if k == M.WARNING_TOAST:
        return f"Цэс нээгдэхэд шууд анхааруулга гарна: {short}"
    if k == M.HTTP_4XX:
        return f"HTTP {f.status} хариу: {_url_pattern(f.url)}"
    if k == M.FLOW_STEP_FAIL:
        return f"E2E урсгалын алхам амжилтгүй: {short}"
    if k == M.BLANK_PAGE:
        return "Цэсийн таб нээгдсэн ч агуулга хоосон (цагаан дэлгэц)"
    if k == M.SLOW_LOAD:
        return "Цэс удаан ачааллаж байна (гүйцэтгэлийн анхааруулга)"
    return short or k


def fix_for(f: Finding) -> str:
    text = f"{f.text}\n{f.detail}"
    if f.kind == M.RUNTIME_CRASH:
        if "ObjectDisposedException" in text and ("Timer" in text or f.after_close):
            return "Компонентын Dispose үед timer/background task-ийг зогсоох (IDisposable/IAsyncDisposable)"
        if "NullReferenceException" in text:
            return "Null шалгалт нэмж, компонентын параметр/өгөгдөл ачаалагдахаас өмнө рендерлэхгүй байх"
        return "Unhandled exception-ийг барих (ErrorBoundary), stack trace-ийн эх үүсвэрийг засах"
    if f.kind in (M.AUTH_ERROR, M.SESSION_LOGOUT):
        return "Refresh token сунгалтын логикийг (UserRefreshToken) шалгаж засах, token-ийн хугацааг тохируулах"
    if f.kind == M.LOGIN_FAILED:
        return "Нэвтрэх үйлчилгээ, хэрэглэгчийн эрх, тест орчны тохиргоог шалгах"
    if "Invalid column name" in text or "Invalid object name" in text:
        return "Тест DB-д migration ажиллуулах (схем кодтой нийцэхгүй байна)"
    if f.kind == M.PAGE_RELOAD:
        return "Route бүртгэл болон NavigationManager.NavigateTo(forceLoad) дуудлагыг шалгах"
    if f.kind == M.HTTP_5XX:
        return "API-ийн серверийн лог шалгаж, exception-ийг засах"
    if f.kind in (M.LOADING_STUCK, M.TIMEOUT):
        return "Ачааллын дуудлагын алдааг барьж, loading төлөвийг заавал дуусгах; удаан query-г оновчлох"
    if f.kind == M.FORBIDDEN:
        return "Хэрэглэгчийн эрхгүй цэсийг цэсний модноос нуух (эрхийн шүүлтүүр)"
    if f.kind == M.RENDERER_EXCEPTION:
        return "Рендерийн exception-ийг засах, ErrorBoundary ашиглах"
    if f.kind == M.WARNING_TOAST:
        return "Анхдагч утга (default) өгөх эсвэл анхааруулгыг хэрэглэгч үйлдэл хийх үед л харуулах"
    if f.kind == M.TAB_WRONG_PAGE:
        return "Цэсний route/компонентыг шалгах — цэс зөв хуудсыг нээж байгаа эсэх; зөв бол табын гарчгийг цэсийн нэртэй ижил болгох"
    if f.kind == M.TAB_NAME_MISMATCH:
        return "Табын гарчгийг цэсний нэртэй ижил болгох"
    if f.kind == M.NO_TAB:
        return "Цэсний route/компонентыг шалгах"
    if f.kind == M.SYSTEM_ERROR:
        return "Серверийн exception-ийг засаж, хэрэглэгчид ойлгомжтой мессеж харуулах"
    if f.kind == M.BLANK_PAGE:
        return "Компонентын рендер / өгөгдөл ачааллын алдааг (консол, сүлжээ) шалгах; ErrorBoundary-ээр алдааг харуулах"
    if f.kind == M.SLOW_LOAD:
        return "Цэсийн анхны ачааллын хүсэлтүүдийг (query, lookup) оновчлох, шаардлагагүй ачааллыг хойшлуулах"
    if f.kind == M.FLOW_STEP_FAIL:
        return ("Алхмын мессеж, screenshot-оор бизнес логик/формын шалгалтыг засах; тестийн тохиргоо "
                "(талбарын нэр, товч) зөрсөн бол config/sites/<host>.yaml-ын flow-ыг шинэчлэх")
    return "Консол/сүлжээний алдааны эх үүсвэрийг шалгаж засах"


_RAW_PATTERNS = [
    (re.compile(r"^\s*[\{\[]\s*\"", re.M), "JSON"),
    (re.compile(r"Invalid (column|object) name|SELECT\s|INSERT\s|SqlException|FROM\s+\[", re.I), "SQL"),
    (re.compile(r"\bat [A-Z][\w.]+\(|System\.[A-Z]\w+Exception"), ".NET stack trace"),
    (re.compile(r"\b[A-Z][a-zA-Z]+(?:\.[A-Z][a-zA-Z]+){2,}\b"), ".NET resource key"),
]


def security_note_for(text: str) -> str:
    kinds = [name for rx, name in _RAW_PATTERNS if rx.search(text or "")]
    if not kinds:
        return ""
    return ("Аюулгүй байдлын тэмдэглэл: хэрэглэгчид түүхий техникийн мессеж харагдаж байна ("
            + ", ".join(dict.fromkeys(kinds)) + ") — дотоод бүтэц ил гарна.")


# --------------------------------------------------------------------------
# Модулийн товчлол
# --------------------------------------------------------------------------
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "ө": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ү": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in (text or "").lower())


def auto_module_code(name: str) -> str:
    words = [w for w in re.split(r"[^\wА-Яа-яӨөҮүЁё]+", transliterate(name)) if w]
    if not words:
        return "MOD"
    if len(words) == 1:
        return re.sub(r"[^a-z0-9]", "", words[0])[:4].upper() or "MOD"
    return "".join(w[0] for w in words)[:5].upper()


class ModuleCoder:
    def __init__(self, cfg: dict):
        self.map = dict((cfg or {}).get("module_codes") or {})
        self.used: dict[str, str] = {}

    def code(self, module: str) -> str:
        """YAML mapping-тай бол түүнийг (хоёр модуль нэг код хуваалцаж болно — TC дугаар
        нэг тоолуураар үргэлжилдэг тул ID давхцахгүй). Үгүй бол транслитерац хийж, өөр
        модулийн кодтой давхцвал тоо залгана."""
        if module in self.used:
            return self.used[module]
        mapped = self.map.get(module) or self.map.get(module.strip())
        if mapped:
            code = str(mapped)
        else:
            base = code = auto_module_code(module)
            taken = set(self.used.values()) | {str(v) for v in self.map.values()}
            n = 2
            while code in taken:
                code, n = f"{base}{n}", n + 1
        self.used[module] = code
        return code


# --------------------------------------------------------------------------
# Test Case
# --------------------------------------------------------------------------
def _is_critical_item(item_name: str, cfg: dict) -> bool:
    names = [n.lower() for n in ((cfg.get("priority") or {}).get("critical_items") or [])]
    return item_name.strip().lower() in names


def _default_priority(cfg: dict) -> Severity:
    return Severity.from_any((cfg.get("priority") or {}).get("default", "major"))


def _short_note(r: ItemResult) -> str:
    parts: list[str] = []
    if r.status == M.BLOCKED and r.blocked_reason:
        parts.append(r.blocked_reason)
    for f in r.findings:
        t = (f.text or f.kind).replace("\n", " ").strip()
        if f.kind in (M.HTTP_5XX, M.HTTP_4XX):
            t = f"HTTP {f.status} {_url_pattern(f.url)}"
        elif f.kind == M.RUNTIME_CRASH:
            t = "Runtime уналт: " + first_exception_line(f.detail or f.text)
        parts.append(t[:150])
    if r.status == M.PASS and r.load_ms is not None:
        parts.append(f"OK — {r.load_ms} мс-д нээгдсэн")
    if r.confirmation:
        parts.append(r.confirmation)
    parts.extend(r.notes)
    seen = list(dict.fromkeys(p for p in parts if p))
    return "; ".join(seen)[:600]


def build_test_cases(run: RunData, cfg: dict, executor: str, date: str) -> tuple[list[TestCase], ModuleCoder]:
    coder = ModuleCoder(cfg)
    tcs: list[TestCase] = []
    auto = "Playwright"

    # --- Нэвтрэлтийн TC ---------------------------------------------------
    login_cancelled = run.cancelled and not run.login_ok
    if login_cancelled:
        login_result, login_note = M.PENDING, "Хэрэглэгч нэвтрэлтийг хүлээж байх зуур Зогсоох дарсан"
    elif run.login_ok:
        login_result, login_note = M.PASS, "Амжилттай нэвтэрсэн (гараар)"
    elif run.login_timeout:
        # Хэрэглэгч хугацаандаа гараар нэвтрээгүй — аппын алдаа биш тул bug үүсгэхгүй
        login_result, login_note = M.BLOCKED, f"Шалгагдаагүй: {run.login_error}"
    else:
        login_result, login_note = M.FAIL, f"Нэвтэрч чадсангүй: {run.login_error}"
    if run.app_version:
        login_note += f" (хувилбар {run.app_version})"
    tcs.append(TestCase(
        "TC-LOGIN-001", "SMOKE-LOGIN", "Хэрэглэгч гараар нэвтэрч апп нээгдэх",
        "Хэрэглэгч бүртгэлтэй; нэвтрэх хуудас нээлттэй",
        "1. /auth/login очих\n2. Хэрэглэгч нээгдсэн хөтөч дээр нэвтрэх нэр, нууц үгээ өөрөө оруулах\n"
        "3. Нэвтрэх дарах",
        "/greeting руу шилжиж, модулийн цэс харагдах", "Automated", Severity.CRITICAL.xlsx, auto,
        login_result, executor, date, note=login_note, module="LOGIN",
        menu_key="__login__"))

    min_min = int((cfg.get("session") or {}).get("min_session_minutes", 20))
    if not run.login_ok:
        s_res, s_note = M.BLOCKED, "Нэвтэрч чадаагүй тул шалгах боломжгүй"
    elif run.logouts:
        first = run.logouts[0]
        s_res = M.FAIL
        s_note = (f"Logout {len(run.logouts)} удаа; эхнийх нь нэвтэрснээс "
                  f"{_fmt_dur(first['after_s'])}-ын дараа")
    elif run.session_observed_s >= min_min * 60:
        s_res, s_note = M.PASS, f"{_fmt_dur(run.session_observed_s)} тасралтгүй, logout гараагүй"
    else:
        s_res = M.PENDING
        s_note = (f"Run {_fmt_dur(run.session_observed_s)} үргэлжилсэн (< {min_min} мин) — "
                  "logout ажиглагдаагүй ч бүрэн баталгаажаагүй")
    tcs.append(TestCase(
        "TC-LOGIN-002", "SMOKE-LOGIN", f"Session {min_min}+ минут тасрахгүй байх",
        "Нэвтэрсэн", f"1. Нэвтрэх\n2. {min_min} минут турш цэсүүдийг ашиглах\n3. /auth/login руу шилжсэн эсэхийг ажиглах",
        "Session тасрахгүй, /auth/login руу шилжихгүй", "Automated", Severity.CRITICAL.xlsx, auto,
        s_res, executor, date, note=s_note, module="LOGIN", menu_key="__session__"))

    # --- Цэс бүр = 1 TC ---------------------------------------------------
    counters: dict[str, int] = {}
    for r in run.results:
        it = r.item
        code = coder.code(it.module)
        counters[code] = counters.get(code, 0) + 1
        tc_id = f"TC-{code}-{counters[code]:03d}"
        folder = it.folder or "—"
        prio = Severity.CRITICAL if _is_critical_item(it.name, cfg) else _default_priority(cfg)
        wait_s = round(int((cfg.get("timing") or {}).get("wait_ms", 3000)) / 1000, 1)
        wait_txt = f"{wait_s:g}"
        tcs.append(TestCase(
            tc_id=tc_id,
            req_ref=f"SMOKE-{code}",
            purpose=f"«{it.name}» цэс алдаагүй нээгдэх",
            precondition=f"Нэвтэрсэн; {it.module} → {folder}",
            steps=(f"1. {it.module} модулийг нээх\n2. {folder} → «{it.name}» дарах\n"
                   f"3. {wait_txt} сек хүлээх\n4. Табыг хаах"),
            expected=(f"«{it.name}» нэртэй таб нээгдэх, алдааны цонх/консол алдаа гарахгүй, "
                      "хуудас дахин ачаалагдахгүй"),
            test_type="Automated",
            priority=prio.xlsx,
            automation=auto,
            result=r.status,
            executor=executor,
            date=(r.checked_at or date)[:10],
            note=_short_note(r),
            module=it.module,
            menu_key=it.key,
            screenshot=r.screenshot,
        ))

    # --- E2E урсгалын алхам бүр = 1 TC (TC-FLOW-<CODE>-<№>) ------------------
    for fl in tested_flows(run):
        code = fl.get("code") or "FLOW"
        meta = fl.get("meta") or {}
        if meta.get("formcheck"):
            tcs.extend(_form_tcs(fl, executor, date, auto))
            continue
        for s in fl.get("steps") or []:
            args = json.dumps(s.get("args") or {}, ensure_ascii=False)
            pre = f"Нэвтэрсэн (гараар); run_tag={fl.get('run_tag', '')}"
            if s.get("requires"):
                pre += f"; өмнөх алхмын өгөгдөл: {', '.join(s['requires'])}"
            note = [s.get("message") or ""]
            if s.get("retried"):
                note.append("Session тасарсны дараа дахин ажилласан")
            note += s.get("notes") or []
            tcs.append(TestCase(
                tc_id=f"TC-FLOW-{code}-{s['index']:03d}",
                req_ref=f"E2E-{code}",
                purpose=f"«{fl['flow']}» урсгал: {s['name']}",
                precondition=pre,
                steps=f"{s['uses']}" + (f"\n{args[:400]}" if args != "{}" else ""),
                expected=FLOW_EXPECTED.get(s["uses"], "Алхам алдаагүй дуусах"),
                test_type="Automated",
                priority=Severity.MAJOR.xlsx,
                automation=auto,
                result=s["status"] if s["status"] in (M.PASS, M.FAIL, M.BLOCKED) else M.PENDING,
                executor=executor,
                date=(s.get("started_at") or fl.get("started_at") or date)[:10],
                note="; ".join(dict.fromkeys(n for n in note if n))[:600],
                module="E2E",
                menu_key=flow_step_key(fl["flow"], s["index"]),
                screenshot=s.get("screenshot") or "",
            ))
    return tcs, coder


def _form_tcs(fl: dict, executor: str, date: str, auto: str) -> list[TestCase]:
    """Форм шалгалтын алхам бүр = 1 TC (TC-FORM-<F01>-<№>), модуль нь цэсний модуль."""
    meta = fl.get("meta") or {}
    path = meta.get("menu") or fl["flow"]
    parts = path.split(" / ")
    out = []
    for s in fl.get("steps") or []:
        if s.get("category") in ("nodata", "na"):
            continue            # тест орчинд өгөгдөлгүй — гүйцэтгээгүй (N/A), тооцоонд орохгүй («Тестлээгүй хэсгүүд»-д)
        tool = "[Хэрэгсэл таньсангүй] " if s.get("category") == "tool" else ""
        out.append(TestCase(
            tc_id=f"TC-FORM-{fl.get('code') or 'F'}-{s['index']:03d}",
            req_ref=f"FORM-{fl.get('code') or 'F'}",
            purpose=f"«{parts[-1]}» цэс: {s['name']}",
            precondition=f"Нэвтэрсэн (гараар); {' → '.join(parts[:-1]) or path}; READ-ONLY (хадгалахгүй)",
            steps=FORM_STEPS.get(s["uses"], s["uses"]).format(path=path),
            expected=FLOW_EXPECTED.get(s["uses"], "Алхам алдаагүй дуусах"),
            test_type="Automated",
            priority=Severity.MAJOR.xlsx,
            automation=auto,
            result=s["status"] if s["status"] in (M.PASS, M.FAIL, M.BLOCKED) else M.PENDING,
            executor=executor,
            date=(s.get("started_at") or fl.get("started_at") or date)[:10],
            note=tool + "; ".join(dict.fromkeys(n for n in [s.get("message") or ""] + (s.get("notes") or []) if n))[:600],
            module=parts[0] if parts else "Форм",
            menu_key=flow_step_key(fl["flow"], s["index"]),
            screenshot=s.get("screenshot") or "",
        ))
    return out


def is_form_flow(fl: dict) -> bool:
    return bool((fl.get("meta") or {}).get("formcheck"))


def flow_step_key(flow: str, index: int) -> str:
    return f"flow|{flow}|{index}"


def tested_flows(run: RunData) -> list[dict]:
    """TC/bug болох урсгалууд — автомат CRUD «шалгаж чадаагүй» (meta.skipped) урсгалыг хасна
    (формыг автоматаар бөглөж чадаагүй нь аппын алдаа биш, GO/NO-GO-г хиймлээр унагахгүй)."""
    return [f for f in run.flows if not (f.get("meta") or {}).get("skipped")]


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60} мин {s % 60} сек"


# --------------------------------------------------------------------------
# Bug
# --------------------------------------------------------------------------
def bug_candidates(r: ItemResult) -> list[Finding]:
    """Нэг цэсийн илрэлүүдээс bug болох илрэлийг сонгоно (ижил шалтгааныг нэгтгэнэ)."""
    primary = [f for f in r.findings if f.kind in M.PRIMARY_KINDS]
    return primary if primary else list(r.findings)


def build_bugs(run: RunData, tcs: list[TestCase], cfg: dict) -> list[Bug]:
    by_key = {tc.menu_key: tc for tc in tcs}
    groups: "OrderedDict[str, dict]" = OrderedDict()

    def add(sig: str, f: Finding, tc: TestCase | None, r: ItemResult | None, evidence: list[Finding]):
        g = groups.get(sig)
        if g is None:
            g = groups[sig] = {"finding": f, "tcs": [], "modules": [], "results": [], "evidence": [],
                               "order": len(groups)}
        if tc and tc.tc_id not in g["tcs"]:
            g["tcs"].append(tc.tc_id)
        if r and r.item.module not in g["modules"]:
            g["modules"].append(r.item.module)
        if r:
            g["results"].append(r)
        g["evidence"].extend(evidence)

    # Нэвтрэлт / session (хэрэглэгч зогсоосон бол жинхэнэ bug биш тул алгасна)
    if not run.login_ok and not run.cancelled and not run.login_timeout:
        add(M.LOGIN_FAILED, Finding(M.LOGIN_FAILED, run.login_error or "Нэвтрэлт амжилтгүй"),
            by_key.get("__login__"), None, [])
    if run.logouts:
        times = ", ".join(_fmt_dur(l["after_s"]) for l in run.logouts[:5])
        add(M.SESSION_LOGOUT, Finding(M.SESSION_LOGOUT,
                                      f"{len(run.logouts)} удаа logout; нэвтэрснээс хойш: {times}"),
            by_key.get("__session__"), None, [])

    for r in run.results:
        if not r.findings:
            continue
        tc = by_key.get(r.item.key)
        chosen = bug_candidates(r)
        evidence = [f for f in r.findings if f not in chosen]
        for f in chosen:
            add(signature(f), f, tc, r, evidence)

    bugs: list[Bug] = []
    for sig, g in groups.items():
        f: Finding = g["finding"]
        # Нэг хуудас хоёр модульд цэс болж давхардсан бол (HR «Цалин бодолтын процесс») нэг л удаа тоолно
        n_pages = len({r.item.href or r.item.key for r in g["results"]}) if g["results"] else len(g["tcs"])
        sev = severity_for(f.kind, n_pages, cfg)
        first_r: ItemResult | None = g["results"][0] if g["results"] else None
        stack_src = f.detail or next((e.detail for e in g["evidence"] if e.detail), "")
        stack = "\n".join([l for l in (stack_src or "").splitlines() if l.strip()][:5])
        confirmations = {r.confirmation for r in g["results"] if r.confirmation}
        confirmed = ("Баталгаажсан (2/2)" if any("2/2" in c for c in confirmations)
                     else ("1 удаа ажиглагдсан" if confirmations else ""))
        sec_texts = " ".join([f.text] + [r_f.text for r in g["results"] for r_f in r.findings
                                         if r_f.kind in (M.SYSTEM_ERROR, M.WARNING_TOAST)])
        sec = security_note_for(sec_texts) if f.kind in (M.SYSTEM_ERROR, M.WARNING_TOAST, M.AUTH_ERROR) else ""
        if first_r:
            it = first_r.item
            steps = (f"1. Нэвтрэх\n2. {it.module} → {it.folder or '—'} → «{it.name}» цэсийг дарах\n"
                     f"3. 3 сек хүлээх" + ("\n4. Табыг хаагаад 1.5 сек хүлээх" if f.after_close else ""))
            expected = f"«{it.name}» таб алдаагүй нээгдэх"
        elif f.kind == M.SESSION_LOGOUT:
            steps = "1. Нэвтрэх\n2. Цэсүүдийг 20+ минут ашиглах"
            expected = "Session тасрахгүй"
        else:
            steps = "1. /auth/login очих\n2. Нэвтрэх нэр, нууц үгээ гараар оруулж нэвтрэх"
            expected = "Амжилттай нэвтэрч /greeting руу шилжих"
        actual = (f.text or title_for(f)).strip()
        if len(g["tcs"]) > 1:
            dup = len(g["tcs"]) - n_pages
            actual += (f" (нийт {n_pages} хуудсанд давтагдсан" if n_pages > 1 else " (нэг хуудас")
            actual += (f"; {dup} нь өөр модульд давхардсан ижил цэс)" if dup > 0 else ")")
        shot = next((r.screenshot for r in g["results"] if r.screenshot), "")
        bugs.append(Bug(
            bug_id="", signature=sig, kind=f.kind, severity=sev, title=title_for(f),
            modules=g["modules"], tc_ids=g["tcs"], steps=steps, expected=expected,
            actual=actual[:800], stack=stack, screenshot=shot, confirmed=confirmed,
            fix=fix_for(f), security_note=sec, status="Open",
            found_date=(first_r.checked_at[:10] if first_r and first_r.checked_at
                        else (run.started_at or datetime.now().isoformat())[:10]),
            order=g["order"],
        ))

    crash_by_menu = {r.item.key: b for b in bugs if b.kind == M.RUNTIME_CRASH
                     for r in groups[b.signature]["results"]}
    fbugs = _flow_bugs(run, by_key, len(groups))
    for fl in tested_flows(run):
        key = (fl.get("meta") or {}).get("menu_key")
        if not is_form_flow(fl) or key not in crash_by_menu:
            continue
        for s in fl.get("steps") or []:
            tc = by_key.get(flow_step_key(fl["flow"], s["index"]))
            if s.get("uses") != "form.close_tab" or s.get("status") != M.FAIL or not tc:
                continue
            target = crash_by_menu[key]           # smoke-ийн ижил уналтын bug-д TC-г нэмнэ
            if tc.tc_id not in target.tc_ids:
                target.tc_ids.append(tc.tc_id)
            for fb in fbugs:
                if tc.tc_id in fb.tc_ids:
                    fb.tc_ids.remove(tc.tc_id)
    bugs.extend(fb for fb in fbugs if fb.tc_ids)
    bugs.sort(key=lambda b: (b.severity.rank, b.order))
    for i, b in enumerate(bugs, 1):
        b.bug_id = f"BUG-{i:03d}"
    by_tc = {tc.tc_id: tc for tc in tcs}
    for b in bugs:
        for tid in b.tc_ids:
            if tid in by_tc and b.bug_id not in by_tc[tid].bug_ids:
                by_tc[tid].bug_ids.append(b.bug_id)
    return bugs


def flow_signature(flow: str, step_name: str, message: str, run_tag: str = "") -> str:
    """flow|<flow>|<step>|<мессеж> — run_tag (run бүрт өөр)-ийг арилгаж regression-д тогтвортой болгоно."""
    msg = (message or "").replace(run_tag, "<run_tag>") if run_tag else (message or "")
    return f"flow|{flow}|{step_name}|{normalize_text(msg, 160)}"


def _flow_bugs(run: RunData, by_key: dict[str, TestCase], order0: int) -> list[Bug]:
    """E2E урсгалын Fail алхам бүр → bug (🟡 Major; апп уналт/token алдаа илэрсэн бол 🔴 Critical)."""
    out: dict[str, Bug] = {}
    form_bugs = _form_bugs(run, by_key, order0)
    for fl in tested_flows(run):
        if is_form_flow(fl):
            continue
        tag = fl.get("run_tag", "")
        steps = fl.get("steps") or []
        for s in steps:
            if s.get("status") != M.FAIL:
                continue
            sig = flow_signature(fl["flow"], s["name"], s.get("message", ""), tag)
            tc = by_key.get(flow_step_key(fl["flow"], s["index"]))
            if sig in out:
                if tc and tc.tc_id not in out[sig].tc_ids:
                    out[sig].tc_ids.append(tc.tc_id)
                continue
            kinds = {f.get("kind") for f in s.get("findings") or []}
            critical = bool(kinds & FLOW_CRITICAL_KINDS)
            menu = (fl.get("meta") or {}).get("menu")
            form = is_form_flow(fl)
            f = Finding(M.FLOW_STEP_FAIL, f"«{menu if form else fl['flow']}» › {s['name']}: {s.get('message', '')}")
            stack_src = next((x.get("detail") for x in s.get("findings") or [] if x.get("detail")), "")
            prev = [f"{p['index']}. {p['name']} ({p['uses']})" for p in steps if p["index"] <= s["index"]]
            sec_texts = " ".join(x.get("text", "") for x in s.get("findings") or [])
            out[sig] = Bug(
                bug_id="", signature=sig, kind=M.FLOW_STEP_FAIL,
                severity=Severity.CRITICAL if critical else Severity.MAJOR,
                title=(title_for(f).replace("E2E урсгалын алхам амжилтгүй", "Форм шалгалт: нээхэд алдаа") if form
                       else title_for(f))[:200], modules=[menu.split(" / ")[0] if menu else "E2E"],
                tc_ids=[tc.tc_id] if tc else [],
                steps=("Гараар нэвтэрсний дараа (READ-ONLY):\n" + FORM_STEPS.get(s["uses"], s["uses"]).format(path=menu)
                       if form else "1. Гараар нэвтрэх\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(prev, 2))),
                expected=FLOW_EXPECTED.get(s["uses"], "Алхам алдаагүй дуусах"),
                actual=(s.get("message") or "")[:800],
                stack="\n".join([l for l in (stack_src or "").splitlines() if l.strip()][:5]),
                screenshot=s.get("screenshot") or "", confirmed="", fix=fix_for(f),
                security_note=security_note_for(sec_texts), status="Open",
                found_date=(s.get("started_at") or run.started_at or datetime.now().isoformat())[:10],
                order=order0 + len(form_bugs) + len(out),
            )
    return form_bugs + list(out.values())


_ROOT_PATTERNS = [
    (r"Invalid column name '([^']+)'", "Invalid column name '{}'"),
    (r"Invalid object name '([^']+)'", "Invalid object name '{}'"),
    (r"Cannot create a DbSet for '([^']+)'", "Cannot create a DbSet for '{}'"),
    (r"Blazor компонент унав:\s*([^\n;]{5,120})", "Blazor компонент унав: {}"),
    (r"(Object reference not set to an instance of an object)", "{}"),
]


def root_causes(text: str) -> list[str]:
    """Алдааны үндсэн шалтгаан(ууд) — ижил шалтгаантай олон цэсийн алдааг НЭГ bug болгоход.
    ж: 6 цэсийн «Invalid column name 'ActiveStatus'» → нэг migration-ы асуудал."""
    out: list[str] = []
    for pat, fmt in _ROOT_PATTERNS:
        for m in re.finditer(pat, text or ""):
            cause = fmt.format(re.sub(r"\s+", " ", m.group(1)).strip())
            if cause not in out:
                out.append(cause)
    return out


_LOADED_RE = re.compile(r"(?:ачаалагдсан|нээгдсэн): ([^;—]+)")
_EMPTY_WARN_RE = re.compile(r"(?:Мөрийн )?«([^»]+)».*хоосон")


def form_field_stats(run: RunData) -> tuple[set[str], dict[str, set[str]]]:
    """Форм шалгалтын сонголтын талбарууд: (аль нэг формд өгөгдөлтэй ачаалагдсан шошгууд,
    хоосон байсан шошго → цэсүүд). Нэг шошго зарим формд дүүрэн, заримд хоосон бол хоосон байдал нь тест
    орчны өгөгдөл дутуугаас биш формын нөхцөлөөс (ж: «Компани» сонгоогүй тул «Салбар» хоосон) хамаарна."""
    loaded: set[str] = set()
    empty: dict[str, set[str]] = {}
    for fl in tested_flows(run):
        if not is_form_flow(fl):
            continue
        menu = (fl.get("meta") or {}).get("menu") or fl["flow"]
        for s in fl.get("steps") or []:
            for n in s.get("notes") or []:
                for m in _LOADED_RE.finditer(n):
                    loaded.update(x.strip() for x in m.group(1).split(",") if x.strip())
            for w in s.get("findings") or []:
                if w.get("kind") == M.FORM_WARNING:
                    m = _EMPTY_WARN_RE.match(re.sub(r"\s*\([^)]*\)\s*$", "", w.get("text", "")))
                    if m:
                        empty.setdefault(m.group(1), set()).add(menu)
    return loaded, empty


def _form_bugs(run: RunData, by_key: dict[str, TestCase], order0: int) -> list[Bug]:
    """Форм шалгалт: Fail алхмыг ҮНДСЭН ШАЛТГААНААР нэгтгэнэ (нэг шалтгаан = нэг bug, хамаарах бүх цэс/TC);
    шалтгаан танигдаагүй бол цэс бүрт тусад нь. Анхааруулга (хоосон жагсаалт, удаан форм) → 🟢 Minor bug."""
    out: dict[str, Bug] = {}
    menus: dict[str, list[str]] = {}
    empty_fields: dict[str, set[str]] = {}

    def add(sig: str, title: str, sev: Severity, tc: TestCase | None, menu: str, s: dict, actual: str,
            fix: str, finding_texts: str, steps_txt: str, expected: str) -> None:
        if sig in out:
            b = out[sig]
            if tc and tc.tc_id not in b.tc_ids:
                b.tc_ids.append(tc.tc_id)
            mod = menu.split(" / ")[0]
            if mod not in b.modules:
                b.modules.append(mod)
            if menu not in menus[sig]:
                menus[sig].append(menu)
            if sev.rank < b.severity.rank:
                b.severity = sev
            return
        menus[sig] = [menu]
        stack_src = next((x.get("detail") for x in s.get("findings") or [] if x.get("detail")), "")
        out[sig] = Bug(
            bug_id="", signature=sig, kind=M.FLOW_STEP_FAIL, severity=sev, title=title[:200],
            modules=[menu.split(" / ")[0]], tc_ids=[tc.tc_id] if tc else [], steps=steps_txt, expected=expected,
            actual=actual[:800], stack="\n".join([l for l in (stack_src or "").splitlines() if l.strip()][:5]),
            screenshot=s.get("screenshot") or "", confirmed="", fix=fix,
            security_note=security_note_for(finding_texts), status="Open",
            found_date=(s.get("started_at") or run.started_at or datetime.now().isoformat())[:10],
            order=order0 + len(out),
        )

    for fl in tested_flows(run):
        if not is_form_flow(fl):
            continue
        menu = (fl.get("meta") or {}).get("menu") or fl["flow"]
        for s in fl.get("steps") or []:
            tc = by_key.get(flow_step_key(fl["flow"], s["index"]))
            steps_txt = "Гараар нэвтэрсний дараа (READ-ONLY):\n" + FORM_STEPS.get(s["uses"], s["uses"]).format(path=menu)
            expected = FLOW_EXPECTED.get(s["uses"], "Алхам алдаагүй дуусах")
            texts = " ".join(x.get("text", "") for x in s.get("findings") or [])
            if s.get("status") == M.FAIL:
                kinds = {f.get("kind") for f in s.get("findings") or []}
                sev = Severity.CRITICAL if kinds & FLOW_CRITICAL_KINDS else Severity.MAJOR
                msg = s.get("message") or ""
                causes = root_causes(msg + "\n" + texts)
                fix = fix_for(Finding(M.FLOW_STEP_FAIL, msg + "\n" + texts))
                if causes:
                    for c in causes:
                        add(f"formroot|{normalize_text(c, 160)}", f"Форм шалгалт: {c}", sev, tc, menu, s,
                            f"«{menu}» › {s['name']}: {msg}", fix, texts, steps_txt, expected)
                else:
                    sig = flow_signature(fl["flow"], s["name"], msg, fl.get("run_tag", ""))
                    f = Finding(M.FLOW_STEP_FAIL, f"«{menu}» › {s['name']}: {msg}")
                    add(sig, title_for(f).replace("E2E урсгалын алхам амжилтгүй", "Форм шалгалт: нээхэд алдаа"),
                        sev, tc, menu, s, msg, fix, texts, steps_txt, expected)
            for w in (x for x in s.get("findings") or [] if x.get("kind") == M.FORM_WARNING):
                text = w.get("text", "")
                core = re.sub(r"\s*\([^)]*\)\s*$", "", text)
                m = re.match(r"(?:Мөрийн )?«([^»]+)».*хоосон", core)
                if m:
                    # Хоосон сонголтын жагсаалт = ихэвчлэн тест орчны лавлах өгөгдөл дутуу (апп-ын bug биш) — бүгдийг
                    # НЭГ ажиглалт болгон нэгтгэнэ (HR: 27 тусдаа Minor bug болж тайланг дүүргэж байсан)
                    empty_fields.setdefault(m.group(1), set()).add(menu)
                    add("formwarn|empty-lists", "", Severity.MINOR, tc, menu, s, text,
                        "Тест орчинд лавлах (master) өгөгдлийг оруулах; бодит орчинд хоосон байвал тохиргоог шалгах",
                        text, steps_txt, expected)
                elif "удаан нээгдэв" in core:
                    add("formwarn|slow", "", Severity.MINOR, tc, menu, s, text,
                        "Формын ачааллын хүсэлтүүдийг (lookup, лавлах өгөгдөл) оновчлох, шаардлагагүй ачааллыг хойшлуулах",
                        text, steps_txt, expected)
                else:
                    add(f"formwarn|{normalize_text(core, 160)}", f"Форм шалгалт (анхааруулга): {core}", Severity.MINOR,
                        tc, menu, s, text, "Формын тохиргоог шалгах", text, steps_txt, expected)
    if "formwarn|empty-lists" in out:
        b = out["formwarn|empty-lists"]
        loaded, _ = form_field_stats(run)
        order = sorted(empty_fields.items(), key=lambda kv: -len(kv[1]))
        always = [(f, ms) for f, ms in order if f not in loaded]
        context = [(f, ms) for f, ms in order if f in loaded]
        n_menus = len(menus["formwarn|empty-lists"])
        if always:
            b.title = (f"Форм шалгалт (анхааруулга): тест орчны лавлах өгөгдөл дутуу — {len(always)} сонголтын "
                       f"жагсаалт бүх формд хоосон, {n_menus} цэс")
        else:
            b.title = (f"Форм шалгалт (анхааруулга): {len(context)} сонголтын жагсаалт зарим формд хоосон "
                       f"(формын нөхцөлөөс хамаарсан байж болзошгүй), {n_menus} цэс")
        parts = []
        if always:
            parts.append("Бүх формд хоосон (тест орчинд өгөгдөл алга): "
                         + "; ".join(f"«{f}» ({len(ms)} цэс)" for f, ms in always))
        if context:
            parts.append("Зарим формд хоосон, бусад формд өгөгдөлтэй (өмнөх талбар — ж: компани/салбар — сонгоогүйгээс "
                         "шүүгдсэн байж болзошгүй; өгөгдөл дутуу биш): "
                         + "; ".join(f"«{f}» ({len(ms)} цэс)" for f, ms in context))
        b.actual = "\n".join(parts)[:800]
        menus["formwarn|empty-lists"] = []          # гарчиг/дэлгэрэнгүйг дээр бүрдүүлсэн
    if "formwarn|slow" in out:
        b = out["formwarn|slow"]
        b.title = f"Форм шалгалт (анхааруулга): удаан нээгдсэн форм — {len(menus['formwarn|slow'])} цэс"
        b.actual = ("Удаан нээгдсэн: " + "; ".join(menus["formwarn|slow"][:20]))[:800]
        menus["formwarn|slow"] = []
    for sig, b in out.items():
        ms = menus[sig]
        if len(ms) > 1:
            b.title = f"{b.title} — {len(ms)} цэс"[:200]
            b.actual = (f"{len(ms)} цэсэд давтагдсан: " + "; ".join(ms[:15]) + (" …" if len(ms) > 15 else "")
                        + "\n\nЖишээ: " + b.actual)[:800]
    return list(out.values())


# --------------------------------------------------------------------------
# Статистик, GO / NO-GO
# --------------------------------------------------------------------------
def stats(tcs: list[TestCase]) -> dict:
    total = len(tcs)
    cnt = {k: sum(1 for t in tcs if t.result == k) for k in (M.PASS, M.FAIL, M.BLOCKED, M.PENDING)}
    pr = (cnt[M.PASS] / total * 100) if total else 0.0
    by_prio = {}
    for sev in Severity:
        rows = [t for t in tcs if t.priority == sev.xlsx]
        by_prio[sev.value] = {"total": len(rows), **{k.lower(): sum(1 for t in rows if t.result == k)
                                                    for k in (M.PASS, M.FAIL, M.BLOCKED, M.PENDING)}}
    return {"total": total, "pass": cnt[M.PASS], "fail": cnt[M.FAIL], "blocked": cnt[M.BLOCKED],
            "pending": cnt[M.PENDING], "pass_rate": round(pr, 1), "pass_rate_exact": pr,
            "by_priority": by_prio}


def go_no_go(tcs: list[TestCase], bugs: list[Bug], cancelled: bool = False) -> dict:
    s = stats(tcs)
    crit = sum(1 for b in bugs if b.severity == Severity.CRITICAL)
    major = sum(1 for b in bugs if b.severity == Severity.MAJOR)
    minor = sum(1 for b in bugs if b.severity == Severity.MINOR)
    conds = [
        ("Pass Rate", "≥ 95%", s["pass_rate_exact"] >= 95 and s["total"] > 0, f"{s['pass_rate']}%"),
        ("Critical Bug", "= 0", crit == 0, str(crit)),
        ("P2 High Bug", "≤ 2", major <= 2, str(major)),
        ("Blocked TC", "= 0", s["blocked"] == 0, str(s["blocked"])),
    ]
    reasons = []
    if not conds[0][2]:
        reasons.append(f"Pass Rate {s['pass_rate']}% < 95%")
    if not conds[1][2]:
        reasons.append(f"Critical bug {crit} ширхэг (= 0 байх ёстой)")
    if not conds[2][2]:
        reasons.append(f"Major (P2 High) bug {major} ширхэг (≤ 2 байх ёстой)")
    if not conds[3][2]:
        reasons.append(f"Blocked TC {s['blocked']} ширхэг (= 0 байх ёстой)")
    # Хэрэглэгч Зогсоох дарсан run бүрэн шалгагдаагүй тул "NO-GO" (жинхэнэ чанарын
    # алдаа) гэж биш, харин "дүгнэлт гаргах боломжгүй" гэж тусад нь ялгана — Pending
    # (ж: Session 20+ мин шалгалт) TC-ууд Pass Rate-ийг хиймлээр бууруулж, бүрэн
    # дууссан боловч муу run шигээ ижилхэн улаан NO-GO харуулдаг байсныг засав.
    if cancelled:
        decision = "INCOMPLETE"
        reasons = ["Run хэрэглэгчийн хүсэлтээр зогссон — бүх цэс шалгагдаагүй тул эцсийн дүгнэлт гаргах боломжгүй"]
    else:
        decision = "GO" if not reasons else "NO-GO"
    return {"decision": decision, "reasons": reasons, "conditions": conds, "stats": s,
            "bug_counts": {"critical": crit, "major": major, "minor": minor, "total": len(bugs)}}


def template_v4_expected(tcs: list[TestCase]) -> str:
    """xlsx загварын V4 томьёог (өөрчлөхгүй) Python-оор давтан тооцоолно — тулгалтад."""
    s = stats(tcs)
    crit_fail = sum(1 for t in tcs if "Critical" in t.priority and t.result == M.FAIL)
    ok = s["total"] > 0 and s["pass"] / s["total"] >= 0.95 and crit_fail == 0 and s["blocked"] == 0
    return "✅ GO" if ok else "❌ NO-GO"
