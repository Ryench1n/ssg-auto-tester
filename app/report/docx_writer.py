"""09_Test-Report-Загвар.docx → бөглөх (docxtpl, templates/report_tpl.docx ашиглана)."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from docxtpl import DocxTemplate, InlineImage, Listing
from docx.shared import Mm

from app import models as M
from app.models import Bug, RunData, TestCase
from app.runner.classify import is_form_flow, tested_flows
from app.runner.classify.cause import CAUSE_LABEL
from app.runner.classify.testcases import item_effective_status, step_effective_status
from app.runner.severity import Severity


def _pct(n: int, total: int) -> str:
    return f"{(n / total * 100):.1f}" if total else "0.0"


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%m/%d %H:%M")
    except Exception:
        return iso or "—"


def _tcs_short(ids: list[str], limit: int = 8) -> str:
    if len(ids) <= limit:
        return ", ".join(ids)
    return ", ".join(ids[:limit]) + f" … (+{len(ids) - limit})"


def _tc_prefix(fl: dict) -> str:
    return f"TC-{'FORM' if is_form_flow(fl) else 'FLOW'}-{fl.get('code')}"


def _one_line(text: str) -> str:
    """«Cистемийн алдаа
Invalid column …» — docx-д мөр алга болж наалдахаас сэргийлнэ."""
    return " — ".join(p.strip() for p in (text or "").splitlines() if p.strip())


def _menu(fl: dict) -> str:
    return (fl.get("meta") or {}).get("menu") or fl["flow"]


def _short(text: str | None, n: int = 160) -> str:
    t = _one_line(text or "")
    return t if len(t) <= n else t[:n - 1] + "…"


def _cleanup(fl: dict) -> str:
    steps = fl.get("steps") or []
    cleanup = [s for s in steps if s.get("always") or s.get("uses") == "crud.delete"]
    if not cleanup:
        return "Цэвэрлэх алхам тодорхойлоогүй"
    if all(s["status"] == M.PASS for s in cleanup):
        return "Амжилттай — run_tag-тай тестийн өгөгдөл устгагдсан"
    return (f"Амжилтгүй — «{fl.get('run_tag')}» агуулсан тестийн өгөгдөл системд үлдсэн байж болзошгүй, "
            "гараар шалгаж устгана уу")


def build_context(tpl: DocxTemplate | None, run: RunData, tcs: list[TestCase], bugs: list[Bug],
                  go: dict, file_name: str, run_dir: Path, appendix: bool) -> dict:
    s = go["stats"]
    total = s["total"]
    start_d = (run.started_at or "")[:10]
    end_d = (run.finished_at or run.started_at or "")[:10]
    date = end_d or datetime.now().strftime("%Y-%m-%d")
    qa_lead = run.qa_lead or "QA Lead (тодорхойгүй)"
    # QA Lead — зөвхөн «QA Lead» ба «QA баг»; гүйцэтгэгч (Author / Тайлан бэлтгэсэн) нь тусдаа хүн
    author = run.tester or "QA Tester (тодорхойгүй)"
    team = ", ".join(dict.fromkeys(x for x in (run.qa_lead, run.tester) if x)) or "—"
    ok_pr = s["pass_rate_exact"] >= 95 and total > 0

    # Форм шалгалтын тусдаа тайлан: зөвхөн форм шалгалтын урсгалтай, smoke-ийн цэсгүй
    form_only = bool(run.flows) and not run.results and all(is_form_flow(f) for f in run.flows)

    # 2.1 / 2.2 — ХАМРАХ ХҮРЭЭ: юуг шалгасан / шалгаагүй (товч). Нарийн үр дүн 3-р бүлэг ба xlsx-д.
    tested, untested = [], []
    tested.append("✅ Нэвтрэлт (гараар, TC-LOGIN-001), session тогтвортой байдал (TC-LOGIN-002)")
    mods = run.modules_selected or list(dict.fromkeys(r.item.module for r in run.results))
    smoke_mods = [(m, [r for r in run.results if r.item.module == m]) for m in mods]
    smoke_mods = [(m, rs) for m, rs in smoke_mods if rs]
    if smoke_mods:
        n = sum(len(rs) for _, rs in smoke_mods)
        tested.append(f"✅ Smoke (READ-ONLY, бүх цэсийг нээж хаах) — {len(smoke_mods)} модуль, {n} цэс: "
                      + ", ".join(f"{m} ({len(rs)})" for m, rs in smoke_mods))
        allr = [r for _, rs in smoke_mods for r in rs]
        soft = [r for r in allr if r.status == M.PASS and r.findings]
        loads = sorted(r.load_ms for r in allr if r.load_ms)
        perf = (f"Гүйцэтгэл: цэсийн ачаалал медиан {loads[len(loads) // 2] / 1000:.1f} сек, хамгийн удаан "
                f"{loads[-1] / 1000:.1f} сек" if loads else "")
        slow = (run.compare or {}).get("smoke_slower") or []
        if slow:
            perf += (f"; өмнөх run-аас удааширсан {len(slow)} цэс: "
                     + ", ".join(f"{x['menu']} ({x['prev_ms'] / 1000:.1f}→{x['now_ms'] / 1000:.1f} сек)" for x in slow[:6])
                     + (" …" if len(slow) > 6 else ""))
        if perf:
            tested.append("   " + perf)
        if soft:
            tested.append(f"   Анхааруулгатай Pass {len(soft)} цэс (Minor консол алдаа / тохиргооны анхааруулга / "
                          "удаан ачаалал — цэс ажиллаж байгаа тул Fail биш, bug-ийн жагсаалтад 🟢 Minor)")
    flows_t = tested_flows(run)
    form_fl = [f for f in flows_t if is_form_flow(f)]
    if form_fl:
        by_mod = Counter(_menu(f).split(" / ")[0] for f in form_fl)
        tested.append(f"✅ Форм шалгалт (READ-ONLY: Шинэ/засварын форм, lookup, Хэвлэх/Экспорт) — {len(form_fl)} цэс: "
                      + ", ".join(f"{m} ({c})" for m, c in by_mod.items()) + " (TC-FORM-…). "
                      "Шалгалтын түвшин: форм алдаагүй нээгдэж, lookup / ComboBox / мөрийн grid ачаалагдах, бүртгэлтэй "
                      "бичлэг засварын горимд өгөгдөлтэйгээ нээгдэх, уншилтын товч алдаагүй ажиллах. «Pass» нь "
                      "хадгалалт, тооцоолол, validation зөв ажиллаж байгааг батлахгүй.")
    auto_fl = [f for f in flows_t if (f.get("meta") or {}).get("auto")]
    if auto_fl:
        by_mod = Counter(_menu(f).split(" / ")[0] for f in auto_fl)
        tested.append(f"✅ Автомат CRUD (бүртгэх → хайх → засах → устгах, run_tag-тай) — {len(auto_fl)} цэс: "
                      + ", ".join(f"{m} ({c})" for m, c in by_mod.items()))
    for fl in flows_t:
        if not is_form_flow(fl) and not (fl.get("meta") or {}).get("auto"):
            if (fl.get("meta") or {}).get("e2e"):
                n_delta = sum(1 for s in fl.get("steps") or [] if s.get("uses") == "assert.delta")
                tested.append(f"✅ Бизнес процессын E2E «{fl['flow']}» ({_tc_prefix(fl)}-xxx) — "
                              f"тайлангаар баталгаажуулсан {n_delta} шалгалт (Δ)")
            else:
                tested.append(f"✅ E2E урсгал «{fl['flow']}» ({_tc_prefix(fl)}-xxx)")

    if form_only:
        untested.append("Хадгалах, засах, устгах (өгөгдөл өөрчлөх) — форм шалгалт READ-ONLY тул хамрагдаагүй; "
                        "автомат CRUD-оор шалгана")
        untested.append("Формын validation (заавал бөглөх талбар), талбар хоорондын хамаарал, тооцоолол, хэвлэх "
                        "загварын агуулга — форм шалгалтаар шалгагдаагүй (CRUD / гар тест)")
    elif not run.smoke_ran and run.login_ok:
        untested.append("Smoke (бүх цэс) — энэ run-д сонгогдоогүй")
    skipped_mods = [m for m in run.discovered if m not in (run.modules_selected or run.discovered)]
    if skipped_mods:
        n = sum(run.discovered[m] for m in skipped_mods)
        untested.append(f"Хэрэглэгч сонгоогүй {len(skipped_mods)} модуль ({n} цэс): "
                        + ", ".join(skipped_mods))
    for st, label in ((M.BLOCKED, "Smoke — шалгаж буй систем саад болсон (Blocked)"),
                      (M.NOT_TESTED, "Smoke — шалгагдаагүй (Blocked биш)")):
        rows = [r for r in run.results if item_effective_status(r) == st]
        if rows:
            names = ", ".join(f"{r.item.module} › {r.item.name}" for r in rows[:8])
            untested.append(f"{label} {len(rows)} цэс: {names}"
                            + (f" … (+{len(rows) - 8}, Test Cases xlsx-ээс)" if len(rows) > 8 else ""))
    by_cause = (go.get("coverage") or {}).get("by_cause") or {}
    for cause, n in sorted(by_cause.items(), key=lambda x: -x[1]):
        if cause not in ("dependent", "na"):
            untested.append(f"Шалгагдаагүй — {CAUSE_LABEL.get(cause, cause)}: {n}")
    if run.cancelled:
        untested.append("Run хэрэглэгчийн хүсэлтээр зогссон — үлдсэн цэсүүд шалгагдаагүй")
    for kind, keys in (run.resume or {}).items():
        if keys:
            label = {"formcheck": "Форм шалгалт", "autocrud": "Автомат CRUD"}.get(kind, kind)
            untested.append(f"{label}: {len(keys)} цэс ажиллаагүй (session тасарсан / зогсоосон) — "
                            "UI-ийн «Үлдсэн цэсүүдийг үргэлжлүүлэх»-ээр дахин ажиллуулна")
    skipped = [f for f in run.flows if (f.get("meta") or {}).get("skipped") and not (f.get("meta") or {}).get("setup")]
    if skipped:
        from app.report.build import auto_skip_counts
        groups = "; ".join(f"{g['label']} — {g['count']}" for g in auto_skip_counts(run.flows))
        untested.append(f"Автомат CRUD-оор шалгаж чадаагүй {len(skipped)} цэс ({groups}): "
                        + ", ".join(_menu(f).split(" / ")[-1] for f in skipped[:10])
                        + (" …" if len(skipped) > 10 else "")
                        + (f" — YAML ноорог: {run.draft}" if run.draft else ""))
        from app.report.build import systemic_warnings
        for g in systemic_warnings(run.flows):
            untested.append(f"⚠ {g['count']} цэс ИЖИЛ анхааруулгаар хадгалагдсангүй: «{g['message']}» — цэс бүрийн биш, "
                            "системийн/орчны нэг тохиргооны асуудал байх магадлалтай (хөгжүүлэгчээр шалгуулна уу)")
    left = [f for f in run.flows if (f.get("meta") or {}).get("leftover")]
    if left:
        untested.append(f"⚠ Цэвэрлэгдээгүй тестийн өгөгдөл ({len(left)} цэс) — гараар устгана уу: "
                        + "; ".join(f"{_menu(f).split(' / ')[-1]}: {', '.join(x.split(':')[0] for x in f['meta']['leftover'])}"
                                    for f in left[:10]))
    if not form_only and not auto_fl and not any(not is_form_flow(f) for f in run.flows):
        untested.append("Өгөгдөл өөрчлөх (CRUD) урсгал — энэ run-д сонгогдоогүй")
    na = [_menu(f).split(" / ")[-1] for f in run.flows
          if any(s.get("category") == "nodata" for s in f.get("steps") or [])]
    if na:
        untested.append(f"Засварын форм — тест орчинд өгөгдөлгүй (N/A) {len(na)} цэс: {', '.join(na[:10])}"
                        + (f" … +{len(na) - 10}" if len(na) > 10 else "") + "; тест өгөгдөл оруулбал шалгагдана")
    if not any((f.get("meta") or {}).get("e2e") for f in flows_t):
        untested.append("Батлах, бизнес процесс (тайлангаар баталгаажуулах E2E) — энэ run-д хамрагдаагүй")
    untested += ["Интеграцийн урсгал (И-баримт, банк) — хамрагдаагүй",
                 "Mobile, бусад хөтөч (Firefox, Safari) — хамрагдаагүй (зөвхөн Chromium)"]

    # 2.3 — урсгалын ТОВЧ: форм шалгалт / автомат CRUD нь зөвхөн Fail/Blocked цэсүүд (нэг мөр),
    # YAML-ийн E2E урсгал бүрэн. Бүх TC-ийн дэлгэрэнгүй — Test Cases (.xlsx).
    flows, summary = [], []
    for label, group in (("Форм шалгалт", form_fl), ("Автомат CRUD", auto_fl)):
        if not group:
            continue
        cnt = Counter(f.get("status") for f in group)
        summary.append(f"{label}: {len(group)} цэс — Pass {cnt.get(M.PASS, 0)}, Fail {cnt.get(M.FAIL, 0)}, "
                       f"Blocked {cnt.get(M.BLOCKED, 0)}"
                       + (f", Шалгагдаагүй {cnt.get(M.NOT_TESTED, 0)}" if cnt.get(M.NOT_TESTED) else "")
                       + (f", N/A {cnt.get(M.NA, 0)}" if cnt.get(M.NA) else "") + "."
                       + (" Доор зөвхөн Fail цэсүүд." if cnt.get(M.FAIL) else ""))
        # Blocked (шалгаж чадаагүй — bug биш) цэсүүдийг шалтгаанаар нь нэг мөрөнд нэгтгэнэ
        reasons: dict[str, list[str]] = {}
        for fl in group:
            if fl.get("status") not in (M.BLOCKED, M.NOT_TESTED):
                continue
            bad = next((s for s in fl.get("steps") or [] if step_effective_status(s) in (M.BLOCKED, M.NOT_TESTED)
                        and s.get("category") not in ("nodata", "na")), None)
            why = re.split(r"\s[—(]", _one_line((bad or {}).get("message") or "шалгаж чадаагүй"))[0].strip()
            why = ("Blocked" if fl.get("status") == M.BLOCKED else "Шалгагдаагүй") + " — " + why
            reasons.setdefault(why, []).append(_menu(fl).split(" / ")[-1])
        na = [_menu(fl).split(" / ")[-1] for fl in group
              if any(s.get("category") == "nodata" for s in fl.get("steps") or [])]
        if label == "Форм шалгалт":
            edit_ok = sum(1 for fl in group for s in fl.get("steps") or []
                          if s.get("uses") == "form.check_edit" and s["status"] in (M.PASS, M.FAIL))
            act = sum(1 for fl in group for s in fl.get("steps") or []
                      if s.get("uses") == "form.check_actions" and s["status"] in (M.PASS, M.FAIL))
            summary.append(f"Хамрах хүрээ: засварын форм {edit_ok}/{len(group)} цэсэд шалгагдсан"
                           + (f", Хэвлэх/Экспорт товч {act} цэсэд" if act else "") + ".")
        if na:
            summary.append(f"N/A — тест орчинд өгөгдөлгүй тул засварын форм шалгаагүй (тооцоонд ороогүй): {len(na)} цэс "
                           f"({', '.join(na[:12])}" + (f" … +{len(na) - 12}" if len(na) > 12 else "") + "). "
                           "Шаардлагатай тест өгөгдлийн жагсаалт — Test Cases xlsx-ийн «Тест өгөгдөл» хуудас.")
        cmp = run.compare or {}
        if label == "Форм шалгалт" and cmp.get("compared"):
            short = lambda xs: ", ".join(x.split(" / ")[-1] for x in xs[:10]) + (f" … +{len(xs) - 10}" if len(xs) > 10 else "")
            summary.append(f"Өмнөх хувилбар ({cmp.get('prev_version') or cmp.get('prev_run')})-тай харьцуулахад "
                           f"({cmp['compared']} цэс): шинээр эвдэрсэн {len(cmp['new_fail'])}"
                           + (f" ({short(cmp['new_fail'])})" if cmp["new_fail"] else "")
                           + (f"; шинэ шалгалтаар илэрсэн {len(cmp.get('new_check_fail') or [])} "
                              f"({short(cmp['new_check_fail'])})" if cmp.get("new_check_fail") else "")
                           + f"; засагдсан {len(cmp['fixed'])}" + (f" ({short(cmp['fixed'])})" if cmp["fixed"] else "")
                           + f"; эвдэрхий хэвээр {len(cmp['still_fail'])}")
        direct = [_menu(fl).split(" / ")[-1] for fl in group
                  if any(s.get("category") == "na" and s.get("uses") == "form.check_edit"
                         for s in fl.get("steps") or [])]
        if direct:
            summary.append(f"Шууд форм цэс (жагсаалтгүй — зөвхөн формыг шалгав, засварын шалгалт хамаарахгүй): "
                           f"{len(direct)} цэс ({', '.join(direct[:12])}" + (f" … +{len(direct) - 12}" if len(direct) > 12 else "") + ")")
        for why, names in reasons.items():
            summary.append(f"{why}: {len(names)} цэс ({', '.join(names[:12])}"
                           + (f" … +{len(names) - 12}" if len(names) > 12 else "") + ")")
        for fl in group:
            if fl.get("status") != M.FAIL:
                continue
            bad = [s for s in fl.get("steps") or [] if s["status"] != M.PASS]
            first = bad[0] if bad else None
            flows.append({"name": _menu(fl), "tc": _tc_prefix(fl), "status": fl.get("status") or "",
                          "info": "", "cleanup": "" if is_form_flow(fl) else _cleanup(fl),
                          "steps": [f"{_tc_prefix(fl)}-{s['index']:03d} {s['name']} — {s['status']}: "
                                    f"{_short(s.get('message'))}" for s in bad[:2]] if first else []})
    for fl in flows_t:
        if is_form_flow(fl) or (fl.get("meta") or {}).get("auto"):
            continue
        data = fl.get("data") or {}
        e2e = (fl.get("meta") or {}).get("e2e")
        deltas = [n for s in fl.get("steps") or [] if s.get("uses") == "assert.delta" for n in s.get("notes") or []]
        flows.append({
            "name": ("Бизнес процесс: " if e2e else "") + fl["flow"], "tc": _tc_prefix(fl),
            "status": fl.get("status") or "",
            "info": f"run_tag: {fl.get('run_tag') or ''}  |  Дамжсан өгөгдөл: "
                    + (", ".join(f"{k} = {v}" for k, v in data.items() if not isinstance(v, dict)) or "—")
                    + (f"  |  Тайлангийн шалгалт (Δ): {' / '.join(deltas)}" if deltas else ""),
            "cleanup": _cleanup(fl),
            "steps": [f"{_tc_prefix(fl)}-{s['index']:03d} {s['name']} — {s['status']}"
                      + (f": {_short(s.get('message'))}" if s.get("message") else "") for s in fl.get("steps") or []],
        })

    # 3.2
    pr = {}
    for sev in Severity:
        d = s["by_priority"][sev.value]
        pr[sev.value] = {"total": d["total"], "pass": d["pass"], "fail": d["fail"], "blocked": d["blocked"]}

    # 4
    bug_rows = []
    for b in bugs:
        desc = b.title
        if b.confirmed:
            desc += f" ({b.confirmed})"
        if b.security_note:
            desc += " ⚠ " + b.security_note
        bug_rows.append({"id": b.bug_id, "desc": desc, "sev": b.severity.docx, "status": b.status,
                         "tcs": _tcs_short(b.tc_ids)})
    if not bug_rows:
        bug_rows = [{"id": "—", "desc": "Bug илрээгүй", "sev": "—", "status": "—", "tcs": "—"}]
    open_ids = [b.bug_id for b in bugs if b.status == "Open"]
    st_open = f"{len(open_ids)} — {', '.join(open_ids) if open_ids else 'байхгүй'} — засвар хүлээгдэж байна"

    # 5
    progress = [{"date": _fmt_dt(e.at), "activity": e.activity, "result": e.result, "note": e.note}
                for e in run.timeline]
    if len(progress) > 60:
        progress = progress[:59] + [{"date": "…", "activity": f"бусад {len(progress) - 59} үйл явдал",
                                     "result": "", "note": "run.log-оос харна уу"}]
    if not progress:
        progress = [{"date": _fmt_dt(run.started_at), "activity": "Тест", "result": "—", "note": ""}]

    # 7
    bc = go["bug_counts"]
    conds = {c[0]: c for c in go["conditions"]}
    ok = lambda b: "Хангасан" if b else "Хангаагүй"
    all_exec = s["pending"] == 0 and s["blocked"] == 0 and not (go.get("coverage") or {}).get("not_tested")
    go_rows = {
        "pass_rate": f"{s['pass_rate']}% — {ok(conds['Pass Rate'][2])}",
        "critical": f"{bc['critical']} — {ok(conds['Critical Bug'][2])}",
        "major": f"{bc['major']} — {ok(conds['P2 High Bug'][2])}",
        "blocked": f"{s['blocked']} — {ok(conds['Blocked TC'][2])}",
        "report": "Хүлээгдэж байна",
        "all_executed": f"{'Тийм' if all_exec else 'Үгүй'} — {ok(all_exec)}",
        "regression": "Үгүй — smoke тест (regression хийгдээгүй)",
    }
    decision = go["decision"]
    if decision == "INCOMPLETE":
        reason = "ХЭСЭГЧИЛСЭН: " + "; ".join(go["reasons"]) + "."
    elif decision == "GO":
        reason = "Бүх нөхцөлийг хангасан."
    else:
        reason = "NO-GO: " + "; ".join(go["reasons"]) + "."
    rerun = "форм шалгалтыг" if form_only else "smoke тестийг"
    next_steps = [f"{b.bug_id} засварлаж {rerun} дахин ажиллуулна"
                  for b in bugs if b.severity in (Severity.CRITICAL, Severity.MAJOR)]
    if decision == "INCOMPLETE":
        next_steps = ["Run-ыг зогсоолгүй бүрэн ажиллуулж, эцсийн GO/NO-GO дүгнэлт гаргана"] + next_steps
    elif decision == "GO":
        next_steps = ["Production deploy огноог тогтоож PM-д мэдэгдэнэ"] + next_steps
    if s["blocked"]:
        next_steps.append(f"Шалгаж буй систем саад болсон (Blocked) {s['blocked']} TC — сервер/апп талын саадыг "
                          "хөгжүүлэгчээр арилгуулж дахин шалгана")
    cov = go.get("coverage") or {}
    nt_tool = sum(n for c, n in (cov.get("by_cause") or {}).items() if c in ("tool", "operator"))
    if nt_tool:
        next_steps.append(f"Хэрэгсэл/тохиргооны шалтгаантай шалгагдаагүй {nt_tool} зүйлийг (хамрах хүрээ "
                          f"{cov.get('pct', 0)}%) дахин ажиллуулж нөхнө — Blocked биш, апп-ын алдаа биш")
    if not next_steps:
        next_steps = ["Minor bug-уудыг дараагийн sprint-д засварлана"]

    # 8
    tech = list(dict.fromkeys(b.fix for b in bugs if b.fix)) or ["Илэрсэн bug байхгүй — нэмэлт техникийн зөвлөмж алга"]
    process = ["Deploy хийхийн өмнө DB migration бүрэн ажилласан эсэхийг шалгах",
               "Smoke тестийг deploy бүрийн дараа CI-д автоматаар ажиллуулах",
               "Critical/Major bug-ийг өдөр бүрийн bug triage-аар хянах"]
    sprint = ["Нээлттэй bug-уудыг засварлаж smoke тестээр баталгаажуулах",
              "Гол урсгалуудад (CRUD, батлах) функциональ автомат тест нэмэх"]
    if any(b.kind in (M.SESSION_LOGOUT, M.AUTH_ERROR) for b in bugs):
        sprint.insert(0, "Token сунгалт (refresh) засварыг тест орчинд баталгаажуулах")

    if decision == "INCOMPLETE":
        final = f"ХЭСЭГЧИЛСЭН — {', '.join(go['reasons'])}"
    elif decision == "GO":
        final = "GO — Production-д гаргаж болно"
    else:
        final = f"NO-GO — {', '.join(go['reasons'])} засах хүртэл хойшлуулах"

    # Хавсралт
    details = []
    if appendix:
        for b in bugs:
            img = img2 = ""
            if tpl is not None and b.screenshot and (run_dir / b.screenshot).exists():
                img = InlineImage(tpl, str(run_dir / b.screenshot), width=Mm(150))
            if tpl is not None and b.confirm_screenshot and (run_dir / b.confirm_screenshot).exists():
                img2 = InlineImage(tpl, str(run_dir / b.confirm_screenshot), width=Mm(150))
            details.append({
                "id": b.bug_id, "title": b.title, "sev": b.severity.docx, "status": b.status,
                "modules": ", ".join(b.modules) or "—", "confirmed": b.confirmed or "—",
                "tcs": _tcs_short(b.tc_ids, 30),
                "steps": Listing(b.steps), "expected": b.expected, "actual": Listing(b.actual),
                "stack": Listing(b.stack) if b.stack else "—", "fix": b.fix,
                "security": b.security_note, "image": img, "image2": img2,
            })

    url_ver = f"{run.url}" + (f", хувилбар {run.app_version}" if run.app_version else "")
    return {
        "test_kind": "форм шалгалт (READ-ONLY)" if form_only else "smoke тест",
        "test_kind_title": "Форм шалгалт" if form_only else "Smoke тест",
        "flow_title": "Форм шалгалт (READ-ONLY)" if form_only else "Урсгалын товч (E2E / форм шалгалт / CRUD)",
        "flow_summary": summary,
        "project": run.project, "version": run.version_label, "file_name": file_name, "date": date,
        "qa_lead": qa_lead, "author": author,
        "period": f"{start_d} ~ {end_d}" if start_d else "—",
        "decision": decision,
        "project_full": f"{run.project} ({url_ver})",
        "qa_team": team,
        "tested": tested, "untested": untested, "flows": flows,
        "s": {**s, "pass_pct": _pct(s["pass"], total), "fail_pct": _pct(s["fail"], total),
              "blocked_pct": _pct(s["blocked"], total), "pending_pct": _pct(s["pending"], total)},
        "pass_rate_verdict": ok(ok_pr),
        "pass_rate_verdict_long": f"SSG стандарт ≥ 95% — {ok(ok_pr)}",
        "pr": pr, "bugs": bug_rows,
        "st_fixed": "0 — байхгүй — дахин тест хийгдээгүй",
        "st_open": st_open,
        "st_closed": "0 — байхгүй",
        "progress": progress, "go": go_rows, "decision_reason": reason, "next_steps": next_steps,
        "next_meeting": "Bug засварын дараа товлоно" if decision != "GO" else "Шаардлагагүй (GO)",
        "tech_recs": tech, "process_recs": process, "sprint_recs": sprint, "final_decision": final,
        "appendix": bool(appendix and details), "bugs_detail": details,
    }


def write_report_docx(tpl_path: Path, out: Path, run: RunData, tcs: list[TestCase], bugs: list[Bug],
                      go: dict, run_dir: Path, appendix: bool = True) -> Path:
    tpl = DocxTemplate(str(tpl_path))
    ctx = build_context(tpl, run, tcs, bugs, go, out.name, run_dir, appendix)
    tpl.render(ctx, autoescape=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    tpl.save(str(out))
    return out
