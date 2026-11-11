"""Run-ын үр дүнгээс 2 тайлан (xlsx, docx) үүсгэж, шалгана."""
from __future__ import annotations

import json
import os
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from app import models as M
from app.config import TEMPLATES_DIR
from app.models import RunData
from app.report.docx_writer import write_report_docx
from app.report.verify import VerificationError, soffice_bin, verify_docx, verify_xlsx
from app.report.xlsx_writer import write_test_cases_xlsx
from app.runner.classify import build_bugs, build_test_cases, flow_step_key, go_no_go, is_form_flow
from app.runner.classify.testcases import not_tested_items

XLSX_TEMPLATE = TEMPLATES_DIR / "07_Test-Cases-Загвар.xlsx"
DOCX_TEMPLATE = TEMPLATES_DIR / "09_Test-Report-Загвар.docx"
DOCX_TPL = TEMPLATES_DIR / "report_tpl.docx"


def tester_of(run: RunData) -> str:
    """Тест гүйцэтгэсэн хүн (Гүйцэтгэсэн хүн / QA Tester / Author / Тайлан бэлтгэсэн — бүгд нэг хүн).
    QA Lead-ийг ХЭЗЭЭ Ч гүйцэтгэгчийн оронд тавихгүй — тэр зөвхөн «QA Lead», «QA баг»-т орно."""
    return run.tester or "QA Tester (тодорхойгүй)"


def safe_project(name: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in (name or "Project")).strip() or "Project"


def ensure_docx_template() -> Path:
    # Эх загвар эсвэл үүсгэгч скрипт (ж: «E2E урсгал» хэсэг нэмсэн) шинэчлэгдвэл дахин үүсгэнэ
    from scripts import make_report_template as mk
    newest = max(DOCX_TEMPLATE.stat().st_mtime, Path(mk.__file__).stat().st_mtime)
    if not DOCX_TPL.exists() or DOCX_TPL.stat().st_mtime < newest:
        from scripts.make_report_template import build
        build(DOCX_TEMPLATE, DOCX_TPL)
    return DOCX_TPL


def _move_shot(run_dir: Path, rel: str, name: str) -> str:
    src = run_dir / rel
    dst = run_dir / "shots" / f"{name}.png"
    if src.exists() and src != dst:
        os.replace(src, dst)
    # Windows дээр Path нь "\\" ашигладаг — URL/JSON-д үргэлж "/" байх ёстой.
    return dst.relative_to(run_dir).as_posix() if dst.exists() else ""


def _rename_shots(run_dir: Path, tcs) -> None:
    """item-0001.png → TC-xxx-001.png (TC ID ангиллын дараа тодорхой болдог)."""
    for tc in tcs:
        if tc.screenshot:
            tc.screenshot = _move_shot(run_dir, tc.screenshot, tc.tc_id)


def _rename_confirm_shots(run_dir: Path, run: RunData, tcs) -> None:
    """Давтан баталгаажуулалтын screenshot → TC-xxx-001-2.png (урьд нь хаана ч холбогдоогүй үлддэг байсан)."""
    tc_id = {tc.menu_key: tc.tc_id for tc in tcs}
    for r in run.results:
        if r.confirm_screenshot and r.item.key in tc_id:
            r.confirm_screenshot = _move_shot(run_dir, r.confirm_screenshot, f"{tc_id[r.item.key]}-2")


def smoke_compare(run: RunData, prev: dict | None) -> dict:
    """Smoke-ийн цэс бүрийн ачааллын хугацааг өмнөх run-тай (ижил систем) харьцуулна — гүйцэтгэлийн регресс."""
    before = (prev or {}).get("smoke_load") or {}
    if not before or not run.results:
        return {}
    slower = []
    for r in run.results:
        was, now = before.get(r.item.key), r.load_ms
        if was and now and now > 2000 and now >= max(2 * was, was + 1500):
            slower.append({"menu": f"{r.item.module} › {r.item.name}", "prev_ms": was, "now_ms": now})
    common = sum(1 for r in run.results if r.load_ms and before.get(r.item.key))
    return {"smoke_compared": common, "smoke_slower": sorted(slower, key=lambda x: -x["now_ms"])}


def form_compare(run: RunData, prev: dict | None) -> dict:
    """Өмнөх run-ы (ижил систем) summary-тай форм шалгалтыг харьцуулна: шинээр эвдэрсэн / засагдсан / хэвээр
    эвдэрхий цэсүүд (урсгалын нэрээр — цэс бүр нэг урсгал)."""
    if not prev:
        return {}
    before = {f["flow"]: f.get("status") for f in prev.get("flows") or [] if str(f.get("flow", "")).startswith("form: ")}
    if not before:
        return {}
    now = {f["flow"]: f.get("status") for f in run.flows if (f.get("meta") or {}).get("formcheck")}
    name = lambda k: k[len("form: "):]
    common = [k for k in now if k in before]
    # Алхмаар: өмнөх run-д БАЙСАН алхам одоо эвдэрсэн = шинээр эвдэрсэн; шинээр нэмэгдсэн шалгалт (алхам)
    # Fail бол «шинэ шалгалтаар илэрсэн» — регресс биш (хэрэгсэл шинэ зүйл шалгаж эхэлсэн)
    prev_steps = {f["flow"]: {s.get("name"): s.get("status") for s in f.get("steps") or []}
                  for f in prev.get("flows") or [] if str(f.get("flow", "")).startswith("form: ")}
    new_fail, new_check = [], []
    for fl in run.flows:
        k = fl["flow"]
        if k not in before or now.get(k) != "Fail" or before[k] == "Fail":
            continue
        failed = [s.get("name") for s in fl.get("steps") or [] if s.get("status") == "Fail"]
        old = prev_steps.get(k) or {}
        if not old or any(n in old for n in failed):        # алхмын мэдээлэлгүй хуучин summary → регресс
            new_fail.append(name(k))
        else:
            new_check.append(name(k))
    return {
        "prev_version": prev.get("_version", ""), "prev_run": prev.get("_run_id", ""),
        "compared": len(common),
        "new_fail": new_fail,
        "new_check_fail": new_check,
        "fixed": [name(k) for k in common if before[k] == "Fail" and now[k] != "Fail"],
        "still_fail": [name(k) for k in common if before[k] == "Fail" and now[k] == "Fail"],
    }


# Автомат CRUD-ийн «шалгаж чадаагүй» шалтгааны ангилал (алхмын StepBlocked.category) → хүнд ойлгомжтой нэр
SKIP_KINDS = {
    "nodata": "Тест орчинд лавлах (мастер) өгөгдөл алга",
    "workflow": "Форм тусгай үйлдэл шаарддаг (мэдээлэл татах, хуваарь …)",
    "validation": "Систем автоматаар бөглөсөн утгыг хүлээж аваагүй",
    # «tool» ангилалд: түлхүүр талбар жагсаалтад харагдахгүй (аюулгүй байдлын үүднээс үүсгээгүй), элемент дарагдаагүй,
    # тестийн бичлэг сонголтод олдоогүй, хэрэгслийн дотоод алдаа — «дотоод алдаа» гэж нэрлэх нь төөрөгдүүлдэг байсан
    "tool": "Хэрэгсэл гүйцэтгэж чадаагүй — түлхүүр жагсаалтад харагдахгүй, элемент дарагдаагүй г.м. (аппын bug биш)",
    "infra": "Орчны тасалдал — апп хариу өгөөгүй / цэс олдоогүй (дахин ачаалсан ч)",
    "": "Хэрэгсэл формыг таньж / бөглөж чадаагүй",
}


def auto_skip_kind(flow: dict) -> str:
    if (flow.get("meta") or {}).get("infra"):
        return "infra"
    st = next((s for s in flow.get("steps") or [] if s.get("status") in (M.BLOCKED, M.NOT_TESTED)), {})
    cat = st.get("category") or ""
    return cat if cat in SKIP_KINDS else ""


def auto_skip_counts(flows: list[dict]) -> list[dict]:
    """[{kind, label, count, menus}] — алгассан автомат CRUD цэсүүдийг шалтгаанаар нь бүлэглэнэ."""
    out: dict[str, dict] = {}
    for f in flows:
        meta = f.get("meta") or {}
        if not (meta.get("auto") and meta.get("skipped")) or meta.get("setup"):
            continue
        k = auto_skip_kind(f)
        g = out.setdefault(k, {"kind": k, "label": SKIP_KINDS[k], "count": 0, "menus": []})
        g["count"] += 1
        g["menus"].append(meta.get("menu") or f["flow"])
    return sorted(out.values(), key=lambda g: -g["count"])


def systemic_warnings(flows: list[dict], min_menus: int = 5) -> list[dict]:
    """Олон цэсийг (≥ min_menus) ИЖИЛ анхааруулгаар алгассан бол — цэс бүрийн асуудал биш, системийн/орчны нэг
    тохиргооны асуудал (testretail 2026-10-21: 60 цэс «Код тодорхойгүй байна.», формд «Код» талбаргүй).
    [{message, count, menus}]"""
    import re as _re
    groups: dict[str, list[str]] = {}
    for f in flows:
        meta = f.get("meta") or {}
        if not (meta.get("auto") and meta.get("skipped")) or meta.get("setup"):
            continue
        st = next((s for s in f.get("steps") or [] if s.get("status") in (M.BLOCKED, M.NOT_TESTED)), None)
        msg = (st or {}).get("message") or ""
        if "хүлээж авсангүй" not in msg:
            continue
        core = msg.split("—", 1)[-1].replace("\n", " ")
        core = _re.sub(r"\s*— форм тусгай.*", "", core)
        core = _re.sub(r"^\s*(Анхааруулга|Анхаар|Хадгалах боломжгүй байна)\s*", "", core.strip()).strip()
        if core:
            groups.setdefault(core, []).append(meta.get("menu") or f["flow"])
    return sorted(({"message": k, "count": len(v), "menus": v} for k, v in groups.items() if len(v) >= min_menus),
                  key=lambda g: -g["count"])


def leftovers(flows: list[dict]) -> list[dict]:
    """Устгаж чадаагүй тестийн өгөгдөл (crud.auto_cleanup → meta.leftover) — гараар цэвэрлэх жагсаалт."""
    return [{"menu": (f.get("meta") or {}).get("menu") or f["flow"], "items": f["meta"]["leftover"]}
            for f in flows if (f.get("meta") or {}).get("leftover")]


def generate_reports(run: RunData, cfg: dict, run_dir: Path, verify: bool = True,
                     prev: dict | None = None) -> dict:
    run_dir = Path(run_dir)
    executor = tester_of(run)
    date = (run.finished_at or datetime.now().isoformat())[:10]
    run.compare = {**form_compare(run, prev), **smoke_compare(run, prev)}
    tcs, _ = build_test_cases(run, cfg, executor, date)
    shot_by_key = {r.item.key: r.screenshot for r in run.results}
    for tc in tcs:
        tc.screenshot = shot_by_key.get(tc.menu_key, tc.screenshot)   # flow TC-ийнх аль хэдийн бий
    _rename_shots(run_dir, tcs)
    _rename_confirm_shots(run_dir, run, tcs)
    new_shot = {tc.menu_key: tc.screenshot for tc in tcs}
    for r in run.results:
        r.screenshot = new_shot.get(r.item.key, r.screenshot)
    for fl in run.flows:
        for s in fl.get("steps") or []:
            s["screenshot"] = new_shot.get(flow_step_key(fl["flow"], s["index"]), s.get("screenshot", ""))
    bugs = build_bugs(run, tcs, cfg)
    nt = not_tested_items(run)
    go = go_no_go(tcs, bugs, cancelled=run.cancelled, not_tested=nt,
                  min_coverage=float((cfg.get("verdict") or {}).get("min_coverage", 90)),
                  unfinished=[f["flow"] for f in run.flows if (f.get("meta") or {}).get("e2e")
                              and f.get("status") in (M.NOT_TESTED, M.BLOCKED)])

    proj = safe_project(run.project)
    xlsx = run_dir / f"{proj}-Test-Cases-{run.version_label}.xlsx"
    docx = run_dir / f"{proj}-Test-Report-{run.version_label}.docx"
    expected = write_test_cases_xlsx(XLSX_TEMPLATE, xlsx, run.project, run.version_label, tcs, bugs,
                                     executor, kind="Smoke тест")
    write_report_docx(ensure_docx_template(), docx, run, tcs, bugs, go, run_dir,
                      appendix=bool((cfg.get("reports") or {}).get("appendix", True)))

    verification = {"xlsx": "skipped", "docx": "skipped"}
    if verify and soffice_bin():
        verify_xlsx(xlsx, expected)          # зөрвөл VerificationError
        verification["xlsx"] = "ok"
        verify_docx(docx, DOCX_TEMPLATE)
        verification["docx"] = "ok"

    # Форм шалгалт (READ-ONLY) — тусдаа Test Cases / Test Report файл (зөвхөн форм шалгалтын TC, bug)
    form = _form_reports(run, cfg, run_dir, tcs, proj, verify and soffice_bin() is not None)

    # screenshot zip
    shots = sorted((run_dir / "shots").glob("*.png"))
    tmp_zip = run_dir / "screenshots.zip.part"
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in shots:
            z.write(p, p.name)
    os.replace(tmp_zip, run_dir / "screenshots.zip")
    (run_dir / "raw.json").write_text(json.dumps(run.to_dict(), ensure_ascii=False, indent=2),
                                      encoding="utf-8")

    summary = {
        "decision": go["decision"], "reasons": go["reasons"], "stats": go["stats"],
        "bug_counts": go["bug_counts"], "xlsx": xlsx.name, "docx": docx.name,
        "form": {**form, "compare": run.compare} if form else None,
        "resume": run.resume,
        "smoke_load": {r.item.key: r.load_ms for r in run.results if r.load_ms},
        "smoke_compare": {k: v for k, v in (run.compare or {}).items() if k.startswith("smoke_")},
        "verification": verification,
        "run_tag": run.run_tag,
        "draft": run.draft,
        "coverage": go["coverage"],
        # Шалгагдаагүй (TC биш) — шалтгаантай: tool | data | operator | app-ийн бус. Blocked-оос тусдаа
        "not_tested": {"by_cause": go["coverage"]["by_cause"], "items": nt[:400]},
        "auto_skips": auto_skip_counts(run.flows),
        "systemic": systemic_warnings(run.flows),
        "leftovers": leftovers(run.flows),
        "flows": [{"flow": f["flow"], "code": f.get("code"), "status": f.get("status"), "data": f.get("data"),
                   "auto": bool((f.get("meta") or {}).get("auto")),
                   "setup": bool((f.get("meta") or {}).get("setup")),
                   "skipped": (f.get("meta") or {}).get("skipped", ""),
                   "steps": [{"index": s["index"], "name": s["name"], "status": s["status"],
                              "cause": s.get("cause", ""), "message": s.get("message", "")}
                             for s in f.get("steps") or []]}
                  for f in run.flows],
        "bugs": [{"id": b.bug_id, "severity": b.severity.xlsx, "title": b.title, "tcs": b.tc_ids,
                  "modules": b.modules, "signature": b.signature, "fix": b.fix,
                  "confirmed": b.confirmed, "screenshot": b.screenshot,
                  "confirm_screenshot": b.confirm_screenshot, "actual": b.actual,
                  "stack": b.stack, "security": b.security_note} for b in bugs],
        "tcs": [{"id": t.tc_id, "module": t.module, "purpose": t.purpose, "priority": t.priority,
                 "result": t.result, "bugs": t.bug_ids, "note": t.note, "screenshot": t.screenshot}
                for t in tcs],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def test_data_needs(run: RunData) -> list[tuple[str, str, str, str]]:
    """Форм шалгалтыг бүрэн болгоход тест орчинд хэрэгтэй өгөгдөл: (модуль, цэс, шаардлага, тайлбар)."""
    from app.runner.classify import form_field_stats
    rows = []
    for fl in run.flows:
        if not is_form_flow(fl):
            continue
        menu = (fl.get("meta") or {}).get("menu") or fl["flow"]
        if any(s.get("category") == "nodata" for s in fl.get("steps") or []):
            rows.append((menu.split(" / ")[0], menu, "Жагсаалтад 1+ бичлэг (3 хүртэл)",
                         "Жагсаалт хоосон — засварын форм шалгагдаагүй (N/A)"))
    loaded, empty = form_field_stats(run)
    for field, menus in sorted(empty.items(), key=lambda kv: -len(kv[1])):
        if field in loaded:
            continue            # өөр формд өгөгдөлтэй — нөхцөлөөс хамаарсан, өгөгдөл дутуу биш
        ms = sorted(menus)
        rows.append((ms[0].split(" / ")[0], "; ".join(ms[:8]) + (f" … +{len(ms) - 8}" if len(ms) > 8 else ""),
                     f"«{field}» сонголтын лавлах өгөгдөл", f"{len(ms)} цэсний формд сонголтын жагсаалт хоосон"))
    return rows


def add_test_data_sheet(xlsx: Path, run: RunData) -> None:
    """Form Check Test Cases xlsx-д «Тест өгөгдөл» хуудас — QA багаас тест орчинд оруулахыг хүсэх өгөгдөл."""
    rows = test_data_needs(run)
    if not rows:
        return
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = load_workbook(xlsx)
    if "Тест өгөгдөл" in wb.sheetnames:
        del wb["Тест өгөгдөл"]
    ws = wb.create_sheet("Тест өгөгдөл")
    head = ["№", "Модуль", "Цэс", "Шаардлагатай өгөгдөл", "Тайлбар"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E79")
    for i, r in enumerate(rows, 1):
        ws.append([i, *r])
    for col, w in zip("ABCDE", (5, 18, 60, 34, 44)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    wb.save(xlsx)


def _form_reports(run: RunData, cfg: dict, run_dir: Path, tcs, proj: str, verify: bool) -> dict | None:
    """Форм шалгалтын урсгалууд байвал тусдаа xlsx/docx. Smoke-ийн цэсүүд, бусад урсгал орохгүй."""
    flows = [f for f in run.flows if is_form_flow(f)]
    if not flows:
        return None
    frun = replace(run, results=[], flows=flows, discovered={}, modules_selected=[], logouts=[], crashes=[],
                   pipeline=[p for p in run.pipeline if p.get("kind") in ("auth", "formcheck")],
                   timeline=[t for t in run.timeline if "Форм шалгалт" in t.activity or "Нэвтрэ" in t.activity])
    ftcs = [t for t in tcs if t.tc_id.startswith("TC-FORM-")]
    fbugs = build_bugs(frun, ftcs, cfg)
    fgo = go_no_go(ftcs, fbugs, cancelled=run.cancelled,
                   not_tested=[x for x in not_tested_items(run) if x["kind"] == "form"])
    xlsx = run_dir / f"{proj}-Form-Check-Test-Cases-{run.version_label}.xlsx"
    docx = run_dir / f"{proj}-Form-Check-Report-{run.version_label}.docx"
    expected = write_test_cases_xlsx(XLSX_TEMPLATE, xlsx, run.project, run.version_label, ftcs, fbugs,
                                     tester_of(run), kind="Форм шалгалт")
    write_report_docx(ensure_docx_template(), docx, frun, ftcs, fbugs, fgo, run_dir,
                      appendix=bool((cfg.get("reports") or {}).get("appendix", True)))
    if verify:
        verify_xlsx(xlsx, expected)
        verify_docx(docx, DOCX_TEMPLATE)
    add_test_data_sheet(xlsx, frun)
    st = fgo["stats"]
    return {"xlsx": xlsx.name, "docx": docx.name, "menus": len(flows), "decision": fgo["decision"],
            "tcs": st["total"], "pass": st["pass"], "fail": st["fail"], "blocked": st["blocked"],
            "bugs": len(fbugs)}
