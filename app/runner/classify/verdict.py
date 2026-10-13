"""Статистик, GO / NO-GO шийдвэр."""
from __future__ import annotations

from app import models as M
from app.models import Bug, TestCase
from app.runner.severity import Severity


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
