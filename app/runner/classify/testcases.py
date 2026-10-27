"""Цэс/flow-ын үр дүн → Test Case мөрүүд."""
from __future__ import annotations

import json
import re

from app import models as M
from app.models import ItemResult, RunData, TestCase
from app.runner.severity import Severity
from app.runner.classify.signature import _url_pattern, first_exception_line
from app.runner.classify.severity import FLOW_EXPECTED, FORM_STEPS
from app.runner.classify.modules import ModuleCoder
from app.runner.classify.cause import cause_of, status_for


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
        # Хэрэглэгч хугацаандаа гараар нэвтрээгүй — апп талын саад БИШ (Blocked биш): шалгагдаагүй, TC үүсгэхгүй
        login_result, login_note = M.NOT_TESTED, f"Шалгагдаагүй: {run.login_error}"
    else:
        login_result, login_note = M.FAIL, f"Нэвтэрч чадсангүй: {run.login_error}"
    if run.app_version:
        login_note += f" (хувилбар {run.app_version})"
    if login_result != M.NOT_TESTED:
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
        s_res, s_note = M.NOT_TESTED, "Нэвтэрч чадаагүй тул шалгах боломжгүй"
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
    # Smoke-гүй богино run (ж: 2 минутын бизнес процессын E2E): {min_min} минутын session шалгах боломжгүй —
    # «Pending» нь Pass Rate-ийг хиймлээр унагаж NO-GO болгодог байсан (testaccounting P1, 2026-10-19) → хамаарахгүй
    skip_session = (s_res == M.PENDING and not getattr(run, "smoke_ran", bool(run.results))) or s_res == M.NOT_TESTED
    if not skip_session:
        tcs.append(TestCase(
            "TC-LOGIN-002", "SMOKE-LOGIN", f"Session {min_min}+ минут тасрахгүй байх",
            "Нэвтэрсэн", f"1. Нэвтрэх\n2. {min_min} минут турш цэсүүдийг ашиглах\n3. /auth/login руу шилжсэн эсэхийг ажиглах",
            "Session тасрахгүй, /auth/login руу шилжихгүй", "Automated", Severity.CRITICAL.xlsx, auto,
            s_res, executor, date, note=s_note, module="LOGIN", menu_key="__session__"))

    # --- Цэс бүр = 1 TC ---------------------------------------------------
    counters: dict[str, int] = {}
    for r in run.results:
        it = r.item
        if item_effective_status(r) == M.NOT_TESTED:
            continue            # хэрэгсэл/тохиргоо/хэрэглэгчийн шалтгаантай шалгагдаагүй — TC биш (not_tested_items-д)
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
        # Өмнөх алхам амжилтгүй болсны улмаас ажиллаагүй алхмууд («Өмнөх алхам #1 … Fail», «Шаардлагатай өгөгдөл
        # алга») — тусдаа Blocked TC биш: нэг шалтгааныг 6–7 дахин тоолж Blocked/Pass Rate-ийг хиймлээр гажуудуулдаг
        # байсан (2026-10-21: 631 Blocked-ийн 623). Эх алхмын TC-д «дараагийн N алхам ажиллаагүй» гэж тэмдэглэнэ.
        not_run: dict[int, int] = {}
        for s in fl.get("steps") or []:
            root = dependent_root(s, fl.get("steps") or [])
            if root is not None:
                not_run[root] = not_run.get(root, 0) + 1
        for s in fl.get("steps") or []:
            if dependent_root(s, fl.get("steps") or []) is not None:
                continue        # ажиллаагүй (өмнөх алхмаас хамаарсан) — эх алхмын TC-д тоологдоно
            if step_effective_status(s) == M.NOT_TESTED:
                continue        # апп талын саад биш (хэрэгсэл, өгөгдөл, хэрэглэгч, N/A) — TC биш, not_tested_items-д
            args = json.dumps(s.get("args") or {}, ensure_ascii=False)
            pre = f"Нэвтэрсэн (гараар); run_tag={fl.get('run_tag', '')}"
            if s.get("requires"):
                pre += f"; өмнөх алхмын өгөгдөл: {', '.join(s['requires'])}"
            note = [s.get("message") or ""]
            if s.get("retried"):
                note.append("Session тасарсны дараа дахин ажилласан")
            note += s.get("notes") or []
            if not_run.get(s["index"]):
                note.append(f"Энэ алхмын улмаас дараагийн {not_run[s['index']]} алхам ажиллаагүй")
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
        if s.get("category") in ("nodata", "na") or step_effective_status(s) == M.NOT_TESTED:
            continue            # шалгагдаагүй (өгөгдөлгүй / хэрэгсэл таньсангүй / N/A) — TC биш, not_tested_items-д
        tool = ""
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


def step_effective_status(s: dict) -> str:
    """Алхмын TC-ийн төлөв: Blocked нь зөвхөн апп талын нотолгоотой саад; хуучин (шалтгаангүй) raw.json-ийн Blocked-ийг
    ч мөн шалтгаанаар нь дахин ангилна."""
    st = s.get("status")
    if st == M.NOT_TESTED:
        return M.NOT_TESTED
    if st != M.BLOCKED:
        return st
    if s.get("cause"):
        return M.BLOCKED if s["cause"] == "app" else M.NOT_TESTED
    return status_for(s.get("category") or "", s.get("message") or "")[0]


def item_effective_status(r) -> str:
    """Smoke цэсийн TC-ийн төлөв (хуучин Blocked-ийг шалтгаанаар нь)."""
    if r.status == M.NOT_TESTED:
        return M.NOT_TESTED
    if r.status != M.BLOCKED:
        return r.status
    if getattr(r, "cause", ""):
        return M.BLOCKED if r.cause == "app" else M.NOT_TESTED
    return status_for("", r.blocked_reason or "")[0]


def not_tested_items(run: RunData) -> list[dict]:
    """Шалгагдаагүй зүйлс (TC болоогүй) шалтгаантай нь — тайлангийн «Шалгагдаагүй» хэсэг, хамрах хүрээний тооцоонд.
    [{kind: smoke|flow|form|auto, name, step, cause, message, screenshot}] — хамааралтай (dependent) алхмуудыг оруулахгүй."""
    out: list[dict] = []
    for r in run.results:
        if item_effective_status(r) == M.NOT_TESTED:
            out.append({"kind": "smoke", "name": f"{r.item.module} › {r.item.name}", "step": "",
                        "cause": r.cause or cause_of("", r.blocked_reason or ""), "message": r.blocked_reason,
                        "screenshot": r.screenshot})
    for fl in run.flows:
        meta = fl.get("meta") or {}
        if meta.get("setup"):
            continue
        kind = "form" if meta.get("formcheck") else "auto" if meta.get("auto") else "flow"
        steps = fl.get("steps") or []
        for s in steps:
            if dependent_root(s, steps) is not None or step_effective_status(s) != M.NOT_TESTED:
                continue
            cause = s.get("cause") or cause_of(s.get("category") or "", s.get("message") or "")
            if cause in ("dependent", "na"):
                continue
            out.append({"kind": kind, "name": meta.get("menu") or fl["flow"], "step": s.get("name", ""),
                        "cause": cause, "message": s.get("message") or "", "screenshot": s.get("screenshot") or ""})
            if meta.get("skipped"):
                break           # автоматаар шалгаж чадаагүй урсгал — эхний шалтгаан л
    return out


def dependent_root(step: dict, steps: list[dict]) -> int | None:
    """Алхам өмнөх алхмаас хамаарч ажиллаагүй бол эх (анх амжилтгүй болсон) алхмын дугаар, үгүй бол None."""
    if step.get("status") not in (M.BLOCKED, M.NOT_TESTED):
        return None
    msg = step.get("message") or ""
    m = re.match(r"Өмнөх алхам #(\d+)", msg)
    if m:
        idx = int(m.group(1))
        prev = next((s for s in steps if s.get("index") == idx), None)
        # Эх алхам мөн хамааралтай бол түүний эх рүү (гинжин)
        return (dependent_root(prev, steps) or idx) if prev is not None else idx
    if msg.startswith("Шаардлагатай өгөгдөл алга"):
        first = next((s for s in steps if s.get("status") in (M.FAIL, M.BLOCKED, M.NOT_TESTED)
                      and not (s.get("message") or "").startswith(("Өмнөх алхам", "Шаардлагатай өгөгдөл алга"))), None)
        return first.get("index") if first is not None else None
    return None


def is_form_flow(fl: dict) -> bool:
    return bool((fl.get("meta") or {}).get("formcheck"))


def flow_step_key(flow: str, index: int) -> str:
    return f"flow|{flow}|{index}"


def tested_flows(run: RunData) -> list[dict]:
    """TC/bug болох урсгалууд — автомат CRUD «шалгаж чадаагүй» (meta.skipped) урсгалыг хасна
    (формыг автоматаар бөглөж чадаагүй нь аппын алдаа биш, GO/NO-GO-г хиймлээр унагахгүй) болон мастер
    өгөгдөл бэлтгэх/цэвэрлэх туслах урсгалыг (meta.setup) хасна."""
    return [f for f in run.flows if not (f.get("meta") or {}).get("skipped") and not (f.get("meta") or {}).get("setup")]


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60} мин {s % 60} сек"
