"""Шалгагдаагүй (Pass/Fail биш) үр дүнгийн ШАЛТГААНЫГ тогтооно — BLOCKED-ийг зөвхөн шалгаж буй апп талаас үүссэн,
тестийг үргэлжлүүлэх боломжгүй бодит саадад үлдээнэ.

  app        — шалгаж буй test*.smartapp.mn систем: сервер хариу өгөхгүй (5xx/холболт), апп эвдэрч дахин ачаалсан ч
               сэргээгүй (цэс/toolbar алга) — НОТОЛГООТОЙ үед л → BLOCKED
  data       — тест орчинд лавлах/мастер өгөгдөл алга (сонголтын жагсаалт хоосон) → NOT TESTED
  dependent  — өмнөх алхам амжилтгүй болсон тул ажиллаагүй → NOT TESTED (эх алхмын TC-д тоологдоно)
  operator   — хэрэглэгч зогсоосон, гар нэвтрэлт хийгдээгүй, тохиргоогоор алгассан (skip/risky), цэс сонгоогүй
  na         — хамаарахгүй (ж: заавал талбаргүй формын сөрөг тест)
  tool       — SSG Auto Tester өөрөө: timeout, selector, элемент дарагдаагүй, тест өгөгдөл/сценари дутуу, дотоод
               алдаа … → NOT TESTED (Blocked БИШ; тайланд «Хэрэгслийн шалтгаантай шалгагдаагүй» гэж тусад нь)

Нотолгоогүйгээр апп-д хамааруулахгүй: тодорхой бус бүх тохиолдол «tool».
"""
from __future__ import annotations

from app import models as M

# Апп/сервер талын саадын нотолгоо (мессеж, ангилалд)
APP_EVIDENCE = (
    "Сервертэй холбогдож чадсангүй", "ServiceUnavailable", "StatusCode: 50", "HTTP 50", "Failed to fetch",
    "net::ERR_", "Орчны тасалдал", "апп хариу өгөхгүй", ".NET runtime уналт", "runtime already exited",
    "Тест орчин хариу өгөөгүй",
    "сек-ээс удаан гацсан",          # апп-ын өөрийн «Loading…» spinner арилаагүй (smoke)
)
DEPENDENT_PREFIX = ("Өмнөх алхам", "Шаардлагатай өгөгдөл алга", "Харьцуулах тайлангийн агшин алга",
                    "Бүртгэл хадгалах хүртэл явагдаагүй")
OPERATOR_TEXT = ("Хэрэглэгч зогсоосон", "хэрэглэгч зогсоосон", "Нэвтэрсэн session алга", "Session алдагдсан",
                 "нэвтрэлт", "Нэвтрэлт амжилтгүй", "Нэвтэрч чадаагүй", "дахин нэвтрээгүй", "skip_items", "risky_items", "сонгогдоогүй — алгасав",
                 "Хөтөч хаагдсан", "Logout-ын улмаас")

CAUSE_LABEL = {
    "app": "Шалгаж буй систем (сервер/апп) тестийг үргэлжлүүлэх боломжгүй болгосон",
    "tool": "SSG Auto Tester-ийн шалтгаан (timeout, элемент, тест өгөгдөл/сценари дутуу г.м.)",
    "data": "Тест орчинд шаардлагатай лавлах/мастер өгөгдөл алга",
    "dependent": "Өмнөх алхам амжилтгүй болсон тул ажиллаагүй",
    "operator": "Хэрэглэгч / тохиргоо (зогсоосон, нэвтрээгүй, алгасахаар тохируулсан)",
    "na": "Хамаарахгүй",
}


def cause_of(category: str = "", message: str = "") -> str:
    """Pass/Fail биш үр дүнгийн шалтгаан. category: алхмын ангилал (nodata | na | tool | infra | validation | workflow)."""
    category, message = category or "", message or ""
    if category == "na":
        return "na"
    if message.startswith(DEPENDENT_PREFIX):
        return "dependent"
    if category == "nodata":
        return "data"
    if category == "infra" or any(p in message for p in APP_EVIDENCE):
        return "app"
    if any(p in message for p in OPERATOR_TEXT):
        return "operator"
    return "tool"


def status_for(category: str = "", message: str = "") -> tuple[str, str]:
    """(статус, шалтгаан): апп талын саад → BLOCKED, бусад → NOT_TESTED."""
    cause = cause_of(category, message)
    return (M.BLOCKED if cause == "app" else M.NOT_TESTED), cause


def not_executed(status: str) -> bool:
    return status in (M.BLOCKED, M.NOT_TESTED)
