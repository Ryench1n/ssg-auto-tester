"""Эрэмбийн нэршлийг НЭГ газар тодорхойлно.

xlsx загвар (07) : 🔴 Critical / 🟡 Major / 🟢 Minor   (Data Validation-ийн утгатай яг таарна)
docx загвар (09) : Critical / P2 High / P3 Medium      (3.2, 4-р бүлгийн хүснэгт)
"""
from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"

    # ---- xlsx (07_Test-Cases) --------------------------------------------
    @property
    def xlsx(self) -> str:
        return _XLSX[self]

    # ---- docx (09_Test-Report) -------------------------------------------
    @property
    def docx(self) -> str:
        """4-р бүлгийн Bug хүснэгтэд бичих нэр."""
        return _DOCX[self]

    @property
    def docx_row_label(self) -> str:
        """3.2-р бүлгийн эрэмбийн задаргааны мөрийн нэр (загварт байгаагаар)."""
        return _DOCX_ROW[self]

    @property
    def p_level(self) -> str:
        return _P[self]

    @property
    def rank(self) -> int:
        return _RANK[self]

    @classmethod
    def from_any(cls, value: "str | Severity") -> "Severity":
        if isinstance(value, Severity):
            return value
        v = str(value).strip().lower()
        for sev in cls:
            if v in {sev.value, sev.xlsx.lower(), sev.docx.lower(), sev.p_level.lower(),
                     sev.docx_row_label.lower()}:
                return sev
        if "critical" in v or "p1" in v:
            return cls.CRITICAL
        if "major" in v or "high" in v or "p2" in v:
            return cls.MAJOR
        if "minor" in v or "medium" in v or "p3" in v:
            return cls.MINOR
        raise ValueError(f"Үл мэдэгдэх эрэмбэ: {value!r}")


_XLSX = {Severity.CRITICAL: "🔴 Critical", Severity.MAJOR: "🟡 Major", Severity.MINOR: "🟢 Minor"}
_DOCX = {Severity.CRITICAL: "P1 Critical", Severity.MAJOR: "P2 High", Severity.MINOR: "P3 Medium"}
_DOCX_ROW = {Severity.CRITICAL: "🔴 Critical", Severity.MAJOR: "🟡 P2 High", Severity.MINOR: "🟢 P3 Medium"}
_P = {Severity.CRITICAL: "P1", Severity.MAJOR: "P2", Severity.MINOR: "P3"}
_RANK = {Severity.CRITICAL: 0, Severity.MAJOR: 1, Severity.MINOR: 2}

# xlsx загварын Data Validation жагсаалтууд — writer, тест хоёулаа эндээс авна.
XLSX_TEST_TYPES = ("Manual", "Automated")
XLSX_PRIORITIES = tuple(_XLSX[s] for s in Severity)
XLSX_AUTOMATION = ("Playwright", "Postman", "Newman", "Cypress", "Үгүй")
XLSX_RESULTS = ("Pass", "Fail", "Blocked", "Pending")
XLSX_BUG_STATUSES = ("Open", "In Progress", "Fixed", "Closed", "Won't Fix")
