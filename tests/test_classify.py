from app import models as M
from app.models import Finding
from app.runner.classify import build_bugs, build_test_cases, go_no_go, signature, stats
from app.runner.severity import Severity
from tests.conftest import make_run


def test_signature_normalizes_guid_and_numbers():
    a = Finding(M.SYSTEM_ERROR, "Record 3f2504e0-4f89-11d3-9a0c-0305e82c3301 not found (id=15)")
    b = Finding(M.SYSTEM_ERROR, "Record 9a1b2c3d-1111-2222-3333-444455556666 not found (id=982)")
    assert signature(a) == signature(b)


def test_runtime_crash_signature_uses_exception_line():
    a = Finding(M.RUNTIME_CRASH, "Unhandled Exception:", detail="Unhandled Exception:\nSystem.ObjectDisposedException: "
                "Cannot access a disposed object.\n   at A.B()")
    b = Finding(M.RUNTIME_CRASH, "Unhandled Exception:", detail="Unhandled Exception:\nSystem.ObjectDisposedException: "
                "Cannot access a disposed object.\n   at C.D()")
    c = Finding(M.RUNTIME_CRASH, "x", detail="System.NullReferenceException: Object reference not set")
    assert signature(a) == signature(b) != signature(c)


def test_same_error_on_many_menus_is_one_bug(cfg):
    sql = Finding(M.SYSTEM_ERROR, "Системийн алдаа\nInvalid column name 'CostCenterId'.")
    run = make_run(20, {i: [sql] for i in (1, 3, 5, 7)})
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-09-22")
    bugs = build_bugs(run, tcs, cfg)
    assert len(bugs) == 1
    assert len(bugs[0].tc_ids) == 4
    assert bugs[0].severity == Severity.MAJOR
    # bug ID TC-ийн J/M баганад холбогдсон
    linked = [t for t in tcs if bugs[0].bug_id in t.bug_ids]
    assert len(linked) == 4 and all(t.result == M.FAIL for t in linked)


def test_bugs_sorted_by_severity(cfg):
    run = make_run(10, {
        1: [Finding(M.NO_TAB, "таб нээгдсэнгүй")],
        2: [Finding(M.SYSTEM_ERROR, "Системийн алдаа")],
        3: [Finding(M.RUNTIME_CRASH, "Unhandled Exception:", detail="System.InvalidOperationException: boom")],
    })
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-09-22")
    bugs = build_bugs(run, tcs, cfg)
    assert [b.severity for b in bugs] == [Severity.CRITICAL, Severity.MAJOR, Severity.MINOR]
    assert [b.bug_id for b in bugs] == ["BUG-001", "BUG-002", "BUG-003"]


def test_forbidden_escalates_to_major_when_frequent(cfg):
    fb = Finding(M.FORBIDDEN, "403")
    run = make_run(20, {i: [fb] for i in range(6)})
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-09-22")
    bugs = build_bugs(run, tcs, cfg)
    assert len(bugs) == 1 and bugs[0].severity == Severity.MAJOR


def test_tc_ids_and_login_cases(cfg):
    run = make_run(6)
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-09-22")
    ids = [t.tc_id for t in tcs]
    assert ids[:2] == ["TC-LOGIN-001", "TC-LOGIN-002"]
    assert any(i.startswith("TC-SYS-") for i in ids) and any(i.startswith("TC-MD-") for i in ids)
    assert len(ids) == len(set(ids))


def test_go_when_all_pass(cfg):
    run = make_run(40)
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-09-22")
    g = go_no_go(tcs, build_bugs(run, tcs, cfg))
    assert g["decision"] == "GO", g["reasons"]


def test_no_go_on_critical(cfg):
    run = make_run(40, {2: [Finding(M.RUNTIME_CRASH, "x", detail="System.Exception: x")]})
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-09-22")
    g = go_no_go(tcs, build_bugs(run, tcs, cfg))
    assert g["decision"] == "NO-GO"
    assert any("Critical" in r for r in g["reasons"])


def test_no_go_on_blocked_and_major_count(cfg):
    run = make_run(100, {
        1: [Finding(M.TIMEOUT, "timeout")],
        2: [Finding(M.SYSTEM_ERROR, "A алдаа")], 3: [Finding(M.SYSTEM_ERROR, "B алдаа")],
        4: [Finding(M.SYSTEM_ERROR, "C алдаа")],
    })
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-09-22")
    g = go_no_go(tcs, build_bugs(run, tcs, cfg))
    s = stats(tcs)
    assert s["blocked"] == 1
    assert g["decision"] == "NO-GO"
    assert any("Blocked" in r for r in g["reasons"]) and any("Major" in r for r in g["reasons"])


def test_dialog_config_warning_is_not_system_error():
    """«Анхаар … тохиргоо хийгдээгүй байна!» цонх (dialog) нь toast-той адил тохиргооны анхааруулга (Minor) —
    алдааны үг (SQL, exception, Failed to fetch) агуулсан бол системийн алдаа хэвээр."""
    from app.config import load_config
    from app.runner.detectors import classify_popup
    cfg = load_config(None)
    assert classify_popup("Анхаар\nКассирын агуулахын тохиргоо хийгдээгүй байна!\nOK", cfg, False).kind == M.WARNING_TOAST
    assert classify_popup("Анхааруулга\nТокен авах мэдээлэл буруу байна", cfg, False).kind == M.WARNING_TOAST
    assert classify_popup("Анхааруулга\nСервертэй холбогдож чадсангүй. TypeError: Failed to fetch", cfg, False).kind == M.SYSTEM_ERROR
    assert classify_popup("Анхаар\nInvalid column name 'X'.", cfg, False).kind == M.SYSTEM_ERROR
    assert classify_popup("Мэдээлэл татах боломжгүй байна.\nCannot create a DbSet for 'X'", cfg, False).kind == M.SYSTEM_ERROR


def test_form_candidates_dedupe_same_page():
    """Өөр модульд ижил хуудас (href) — форм шалгалтад нэг удаа (Accounting «Жагсаалт» модуль)."""
    from app.models import ItemResult, MenuItem
    from app.runner.capabilities import form_candidates
    caps = {"add": True, "edit": True, "buttons": []}
    rs = [ItemResult(MenuItem("Худалдан авалт", "Жагсаалт", "Татан авалт", "/po/list"), status=M.PASS, tab_opened=True, caps=caps),
          ItemResult(MenuItem("Жагсаалт", "Худалдан авалт", "Татан авалт", "/po/list"), status=M.PASS, tab_opened=True, caps=caps),
          ItemResult(MenuItem("Жагсаалт", "Бусад", "Өөр", "/x/list"), status=M.PASS, tab_opened=True, caps=caps)]
    c = form_candidates(rs)
    assert [x["path"] for x in c] == ["Худалдан авалт / Жагсаалт / Татан авалт", "Жагсаалт / Бусад / Өөр"]
    assert c[0]["also"] == ["Жагсаалт / Худалдан авалт / Татан авалт"]



def test_tab_name_mismatch_two_classes():
    """Табын нэр зөрөх: бичгийн/орчуулгын ялгаа (Minor, цэс Pass) ↔ өөр хуудас байж болзошгүй (Major, Fail)."""
    from app.config import load_config
    from app.models import ItemResult, MenuItem
    from app.runner.detectors import tab_mismatch_finding as t
    from app.runner.smoke import smoke_status
    assert t("SmartJobQueueList", "Ажлын дараалал").kind == M.TAB_NAME_MISMATCH          # орчуулагдаагүй
    assert t("Кассын нярвын баримтын жагсаалт", "Кассын няравын баримтын жагсаалт").kind == M.TAB_NAME_MISMATCH
    assert t("Гүйлгээний жагсаалт", "Баримтын жагсаалт").kind == M.TAB_WRONG_PAGE
    assert t("Борлуулалтын тайлан /байршил, бараагаар/", "Борлуулалтын тайлан /бараагаар/").kind == M.TAB_WRONG_PAGE
    cfg = load_config(None)
    r = ItemResult(MenuItem("М", "Ф", "Нэр", "/x"), findings=[t("Шаардах хуудасны жагсаалт", "Шаардах хуудсын жагсаалт")])
    assert smoke_status(r, cfg) == M.PASS
    r.findings = [t("Өрөлтийн төрөл", "Мерчендайзийн төрөл")]
    assert smoke_status(r, cfg) == M.FAIL


def test_short_e2e_only_run_has_no_pending_session_tc(cfg):
    """Smoke-гүй 2 минутын бизнес процессын run: 20 минутын session шалгах боломжгүй — «Pending» TC үүсгэж Pass Rate-ийг
    93.3% болгож NO-GO гаргадаг байсан (testaccounting P1 13/13 Pass, 2026-10-19)."""
    run = make_run(0)
    run.session_observed_s = 79
    run.smoke_ran = False
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-10-19")
    assert "TC-LOGIN-002" not in [t.tc_id for t in tcs]
    run.smoke_ran = True                                  # smoke-той богино run — Pending хэвээр (бүрэн бус)
    tcs, _ = build_test_cases(run, cfg, "SSG", "2026-10-19")
    assert [t.result for t in tcs if t.tc_id == "TC-LOGIN-002"] == ["Pending"]


def test_dependent_steps_are_not_separate_blocked_tcs():
    """«Өмнөх алхам #1 … Fail» / «Шаардлагатай өгөгдөл алга» — тусдаа Blocked TC биш; эх алхмын TC-д тоологдоно."""
    from app.runner.classify.testcases import dependent_root
    st = lambda i, status, msg="": {"index": i, "status": status, "message": msg}
    steps = [st(1, M.FAIL, "Хадгалахад: Системийн алдаа"), st(2, M.BLOCKED, "Өмнөх алхам #1 (Бүртгэх) Fail"),
             st(3, M.BLOCKED, "Өмнөх алхам #2 (Хайх) Blocked"), st(5, M.BLOCKED, "Шаардлагатай өгөгдөл алга: a01_rec"),
             st(7, M.BLOCKED, "Тестийн өгөгдөл үлдсэн (гараар устгана уу)"), st(8, M.PASS)]
    assert [dependent_root(s, steps) for s in steps] == [None, 1, 1, 1, None, None]


def test_static_asset_detection():
    from app.runner.detectors import is_asset
    assert is_asset("stylesheet", "https://x/_content/DevExpress.Blazor.Reporting.Viewer/css/viewer.bs5.css")
    assert is_asset("", "https://x/reportviewer/_content/DevExpress.Blazor.Resources/dx-blazor.svg")
    assert is_asset("", "https://x/css/democenter-ui.css?v=2")
    assert not is_asset("fetch", "https://x/api/menus")
    assert not is_asset("script", "https://x/_framework/blazor.webassembly.js")


def test_missing_asset_does_not_fail_menu():
    from app.models import ItemResult, MenuItem
    from app.runner.smoke import smoke_status
    r = ItemResult(MenuItem("Тайлан", "", "Бэлнээр цалин олгох тайлан", "/r"),
                   findings=[Finding(M.ASSET_4XX, "HTTP 404 (статик файл)", status=404, url="https://x/a.css")])
    assert smoke_status(r, {}) == M.PASS
    r.findings.append(Finding(M.HTTP_4XX, "HTTP 404", status=404, url="https://x/api/report"))
    assert smoke_status(r, {}) == M.FAIL


def test_rebuild_reclassifies_legacy_static_404(tmp_path):
    """Хуучин raw.json: статик CSS 404-өөр Fail болсон цэс дахин тооцоход Pass + анхааруулга (testbalancelite)."""
    import json
    from app.report.rebuild import load_run
    raw = {"run_id": "r1", "project": "P", "url": "https://testbalancelite.smartapp.mn", "results": [
        {"item": {"module": "Тайлан", "folder": "", "name": "НД7", "href": "/r"}, "status": M.FAIL,
         "findings": [{"kind": M.HTTP_4XX, "text": "HTTP 404", "status": 404,
                       "url": "https://testbalancelite.smartapp.mn/_content/X.Viewer/css/viewer.bs5.css"}]},
        {"item": {"module": "Тайлан", "folder": "", "name": "API", "href": "/a"}, "status": M.FAIL,
         "findings": [{"kind": M.HTTP_4XX, "text": "HTTP 404", "status": 404,
                       "url": "https://testbalancelite.smartapp.mn/api/report"}]}]}
    p = tmp_path / "raw.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    a, b = load_run(p).results
    assert a.status == M.PASS and a.findings[0].kind == M.ASSET_4XX
    assert b.status == M.FAIL and b.findings[0].kind == M.HTTP_4XX


def test_in_tab_error_page_title():
    from app.runner.classify.severity import title_for
    from app.runner.detectors import ERROR_PAGE_DETAIL
    f = Finding(M.SYSTEM_ERROR, "Системийн алдаа\nObject of type 'SLRangePicker' does not have a property 'Enabled'.",
                detail=ERROR_PAGE_DETAIL)
    assert title_for(f).startswith("Цэсийн таб дотор системийн алдааны хуудас гарна")
    assert title_for(Finding(M.SYSTEM_ERROR, "Системийн алдаа\nX")).startswith("Системийн алдааны цонх гарна")


def test_adopt_legacy_confirm_shots(tmp_path):
    """Хуучин run: давтан шалгалтын item-*.png-ийг run.log-ийн «⏱ <цэс>» цагаар Fail цэст нь хавсаргана."""
    import os
    from datetime import datetime
    from app.models import ItemResult, MenuItem
    from app.report.rebuild import adopt_confirm_shots
    (tmp_path / "shots").mkdir()
    (tmp_path / "run.log").write_text("\n".join([
        "2026-11-03T16:31:40 [INFO] ⏱ А: 1.0с | click=0.1",                       # үндсэн давталт — тооцохгүй
        "2026-11-03T16:31:40 [INFO] Давтан баталгаажуулалт: 3 Fail цэс",
        "2026-11-03T16:31:42 [INFO] ⏱ А: 2.2с | click=0.3",                       # А — screenshot-гүй
        "2026-11-03T16:31:43 [INFO] ⏱ Б: 1.5с | shot=0.05",
        "2026-11-03T16:31:47 [INFO] ⏱ В: 3.0с | click=0.2",                       # В — 2 оролдлого
        "2026-11-03T16:31:49 [INFO] ⏱ В: 2.0с | shot=0.2"]), encoding="utf-8")
    ts = lambda s: datetime.fromisoformat(f"2026-11-03T{s}").timestamp()
    for name, t in (("item-0016", "16:31:43.15"), ("item-0017", "16:31:48.90")):
        p = tmp_path / "shots" / f"{name}.png"
        p.write_bytes(b"png")
        os.utime(p, (ts(t), ts(t)))
    rs = [ItemResult(MenuItem("M", "", n, f"/{n}"), status=M.FAIL, confirmation="Баталгаажсан (2/2)") for n in "АБВ"]
    assert adopt_confirm_shots(tmp_path, rs) == 2
    assert [r.confirm_screenshot for r in rs] == ["", "shots/item-0016.png", "shots/item-0017.png"]
