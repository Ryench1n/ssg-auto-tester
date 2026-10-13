"""Илрэлийн төрөл → эрэмбэ (Critical/Major/Minor), bug-ийн гарчиг, засах санал, аюулгүй байдлын тэмдэглэл."""
from __future__ import annotations

import re

from app import models as M
from app.models import Finding
from app.runner.severity import Severity
from app.runner.classify.signature import _url_pattern, first_exception_line


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
