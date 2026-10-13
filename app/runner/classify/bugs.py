"""Илрэлүүдийг signature-аар нэгтгэж Bug болгоно (smoke, flow, форм шалгалт)."""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime

from app import models as M
from app.models import Bug, Finding, ItemResult, RunData, TestCase
from app.runner.severity import Severity
from app.runner.classify.signature import normalize_text, signature
from app.runner.classify.severity import (
    FLOW_CRITICAL_KINDS,
    FLOW_EXPECTED,
    FORM_STEPS,
    fix_for,
    security_note_for,
    severity_for,
    title_for,
)
from app.runner.classify.testcases import _fmt_dur, flow_step_key, is_form_flow, tested_flows


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
