import openpyxl
import pytest
from docx import Document

from app import models as M
from app.models import Finding
from app.report.build import generate_reports
from app.runner.severity import XLSX_AUTOMATION, XLSX_PRIORITIES, XLSX_RESULTS, XLSX_TEST_TYPES
from tests.conftest import make_run, needs_soffice


@pytest.fixture
def report(tmp_path, cfg):
    run = make_run(30, {
        3: [Finding(M.SYSTEM_ERROR, "Системийн алдаа\nInvalid column name 'CostCenterId'.")],
        5: [Finding(M.TIMEOUT, "timeout")],
        7: [Finding(M.RUNTIME_CRASH, "Unhandled Exception:", detail="System.ObjectDisposedException: disposed",
                    after_close=True)],
    })
    (tmp_path / "shots").mkdir()
    summary = generate_reports(run, cfg, tmp_path, verify=False)
    return tmp_path, summary


def test_files_named_by_ssg_standard(report):
    d, s = report
    assert s["xlsx"] == "Accounting-Test-Cases-v1.0.xlsx"
    assert s["docx"] == "Accounting-Test-Report-v1.0.docx"
    assert (d / s["xlsx"]).exists() and (d / s["docx"]).exists()


def test_xlsx_sheets_formulas_and_validation_values(report):
    d, s = report
    wb = openpyxl.load_workbook(d / s["xlsx"])
    assert wb.sheetnames == ["📖 Заавар", "📋 Test Cases", "🐛 Bug Tracker", "📊 Dashboard"]
    ws = wb["📋 Test Cases"]
    # загварын томьёо хадгалагдсан
    assert str(ws["P4"].value).startswith("=COUNTA(A6:A")
    assert "GO" in str(ws["V4"].value)
    rows = list(ws.iter_rows(min_row=6, values_only=True))
    rows = [r for r in rows if r[0]]
    assert len(rows) == s["stats"]["total"]
    for r in rows:
        assert r[6] in XLSX_TEST_TYPES
        assert r[7] in XLSX_PRIORITIES
        assert r[8] in XLSX_AUTOMATION
        assert r[9] in XLSX_RESULTS
    # DV муж сүүлийн мөрийг хамарсан
    last = 5 + len(rows)
    for dv in ws.data_validations.dataValidation:
        top = max(int("".join(c for c in str(rng).split(":")[-1] if c.isdigit())) for rng in dv.sqref.ranges)
        assert top >= last
    bt = wb["🐛 Bug Tracker"]
    ids = [bt.cell(r, 1).value for r in range(6, 6 + len(s["bugs"]))]
    assert ids == [b["id"] for b in s["bugs"]]


def test_docx_no_placeholders_plain(report):
    from app.report.verify import verify_docx as vd
    from app.report import build
    d, s = report
    vd(d / s["docx"], build.DOCX_TEMPLATE, use_pdf=False)
    text = "\n".join(p.text for p in Document(d / s["docx"]).paragraphs)
    assert "BUG-001" in "\n".join(c.text for t in Document(d / s["docx"]).tables for row in t.rows for c in row.cells)
    assert "NO-GO" in text or "NO-GO" in "".join(
        c.text for t in Document(d / s["docx"]).tables for row in t.rows for c in row.cells)


@needs_soffice
def test_full_verification_with_libreoffice(tmp_path, cfg):
    run = make_run(25, {2: [Finding(M.NO_TAB, "таб нээгдсэнгүй")]})
    (tmp_path / "shots").mkdir()
    s = generate_reports(run, cfg, tmp_path, verify=True)
    assert s["verification"] == {"xlsx": "ok", "docx": "ok"}


def test_people_version_and_no_tool_name(tmp_path, cfg):
    """Хувилбар (UI-аас) файлын нэр ба баримт бүрт; Гүйцэтгэсэн хүн = QA Tester = Author = Тайлан бэлтгэсэн
    (нэг хүн); QA баг = QA Lead + гүйцэтгэгч; хэрэгслийн нэр («SSG Auto Test…», Playwright баг) орохгүй."""
    from docx import Document
    from openpyxl import load_workbook
    run = make_run(6, {1: [Finding(M.SYSTEM_ERROR, "Системийн алдаа")]})
    run.qa_lead, run.tester, run.version_label = "Б.Ганбат", "Э.Ренчин", "v26.2.10"
    s = generate_reports(run, cfg, tmp_path, verify=False)
    assert s["xlsx"] == "Accounting-Test-Cases-v26.2.10.xlsx" and s["docx"] == "Accounting-Test-Report-v26.2.10.docx"

    wb = load_workbook(tmp_path / s["xlsx"])
    cells = [str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value is not None]
    blob = "\n".join(cells)
    assert "SSG TEST CASE TRACKER — Accounting / Smoke тест — v26.2.10" in blob
    assert "Э.Ренчин" in blob and "SSG Auto Test" not in blob

    doc = Document(tmp_path / s["docx"])
    parts = [p.text for p in doc.paragraphs]
    parts += [c.text for t in doc.tables for r in t.rows for c in r.cells]
    for sec in doc.sections:
        parts += [p.text for p in sec.header.paragraphs] + [p.text for p in sec.footer.paragraphs]
    text = "\n".join(parts)
    assert "SSG Auto Test" not in text and "Playwright" not in text, [x for x in parts if "Auto Test" in x or "Playwright" in x][:3]
    assert "v26.2.10" in "\n".join(p.text for sec in doc.sections for p in sec.header.paragraphs)
    cover = {r.cells[0].text.strip(): r.cells[1].text.strip() for r in doc.tables[0].rows}
    assert cover.get("Бэлтгэсэн / Author") == "Э.Ренчин" and cover.get("Хувилбар / Version") == "v26.2.10", cover
    kv = {t.rows[0].cells[0].text.strip(): t.rows[0].cells[1].text.strip() for t in doc.tables[2:8]}
    assert kv.get("QA Lead") == "Б.Ганбат" and kv.get("QA баг") == "Б.Ганбат, Э.Ренчин", kv
    assert kv.get("Тайлан бэлтгэсэн") == "Э.Ренчин", kv


def test_qa_lead_never_used_as_tester(tmp_path, cfg):
    """QA Lead нь зөвхөн «QA Lead», «QA баг»-т; гүйцэтгэгч хоосон байсан ч түүний оронд тавигдахгүй."""
    from docx import Document
    from openpyxl import load_workbook
    run = make_run(4)
    run.qa_lead, run.tester, run.version_label = "Б.Ганбат", "", "v1.2.3"
    s = generate_reports(run, cfg, tmp_path, verify=False)
    wb = load_workbook(tmp_path / s["xlsx"])
    blob = "\n".join(str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value)
    assert "Б.Ганбат" not in blob                       # Гүйцэтгэсэн хүн / QA Tester-т ороогүй
    doc = Document(tmp_path / s["docx"])
    cover = {r.cells[0].text.strip(): r.cells[1].text.strip() for r in doc.tables[0].rows}
    assert cover.get("Бэлтгэсэн / Author") != "Б.Ганбат"
    kv = {t.rows[0].cells[0].text.strip(): t.rows[0].cells[1].text.strip() for t in doc.tables[2:8]}
    assert kv.get("QA Lead") == "Б.Ганбат" and kv.get("QA баг") == "Б.Ганбат" and kv.get("Тайлан бэлтгэсэн") != "Б.Ганбат"


def test_form_compare_with_previous_run(tmp_path, cfg):
    """Өмнөх хувилбартай харьцуулалт: шинээр эвдэрсэн / засагдсан / хэвээр эвдэрхий форм."""
    from docx import Document
    from app.report.build import form_compare

    def fl(menu, status):
        return {"flow": f"form: {menu}", "code": "F01", "status": status, "run_tag": "T",
                "meta": {"formcheck": True, "menu": menu, "menu_key": menu},
                "steps": [{"index": 1, "uses": "form.check_new", "name": "Шинэ форм", "status": status,
                           "message": "Invalid column name 'X'." if status == M.FAIL else "", "notes": [],
                           "findings": [{"kind": M.SYSTEM_ERROR, "text": "Invalid column name 'X'."}] if status == M.FAIL else []}]}
    run = make_run(2)
    run.flows = [fl("М / A", M.FAIL), fl("М / B", M.PASS), fl("М / C", M.FAIL)]
    prev = {"_version": "v26.2.9", "flows": [{"flow": "form: М / A", "status": "Pass"},
                                             {"flow": "form: М / B", "status": "Fail"},
                                             {"flow": "form: М / C", "status": "Fail"}]}
    c = form_compare(run, prev)
    assert c["new_fail"] == ["М / A"] and c["fixed"] == ["М / B"] and c["still_fail"] == ["М / C"]
    s = generate_reports(run, cfg, tmp_path, verify=False, prev=prev)
    assert s["form"]["compare"]["prev_version"] == "v26.2.9"
    text = "\n".join(p.text for p in Document(tmp_path / s["form"]["docx"]).paragraphs)
    assert "Өмнөх хувилбар (v26.2.9)-тай харьцуулахад" in text and "шинээр эвдэрсэн 1" in text


def test_compare_step_level_and_smoke_slowdown(tmp_path, cfg):
    """Шинэ алхам (шалгалт) нэмэгдээд Fail болсон нь регресс биш — «шинэ шалгалтаар илэрсэн»; smoke-ийн ачаалал
    өмнөх run-аас 2+ дахин удааширсан цэс тайланд гарна."""
    from docx import Document
    from app.report.build import form_compare, smoke_compare

    run = make_run(3)
    run.flows = [{"flow": "form: М / A", "code": "F01", "status": M.FAIL, "run_tag": "T",
                  "meta": {"formcheck": True, "menu": "М / A", "menu_key": "М / A"},
                  "steps": [{"index": 1, "uses": "form.check_new", "name": "Шинэ форм", "status": M.PASS,
                             "message": "", "notes": [], "findings": []},
                            {"index": 2, "uses": "form.close_tab", "name": "Цэсийн табыг хаах", "status": M.FAIL,
                             "message": "апп унав", "notes": [],
                             "findings": [{"kind": M.RUNTIME_CRASH, "text": "Unhandled Exception:"}]}]}]
    prev = {"_version": "v1", "flows": [{"flow": "form: М / A", "status": "Pass",
                                         "steps": [{"name": "Шинэ форм", "status": "Pass"}]}],
            "smoke_load": {run.results[0].item.key: 800, run.results[1].item.key: 900}}
    c = form_compare(run, prev)
    assert c["new_fail"] == [] and c["new_check_fail"] == ["М / A"], c
    run.results[0].load_ms = 4000          # 0.8 → 4.0 сек: удааширсан
    run.results[1].load_ms = 1000          # бага өөрчлөлт — тооцохгүй
    sc = smoke_compare(run, prev)
    assert [x["now_ms"] for x in sc["smoke_slower"]] == [4000], sc
    s = generate_reports(run, cfg, tmp_path, verify=False, prev=prev)
    assert s["smoke_load"][run.results[0].item.key] == 4000
    text = "\n".join(p.text for p in Document(tmp_path / s["docx"]).paragraphs)
    assert "өмнөх run-аас удааширсан 1 цэс" in text, text[:2000]


def test_rebuild_run_from_raw_json(tmp_path, cfg):
    """Сервер тайлан үүсгэж байх үед хаагдвал raw.json-оос RunData-г сэргээж тайланг дахин үүсгэнэ."""
    import json
    from app.report.rebuild import load_run
    run = make_run(6, {2: [Finding(M.SYSTEM_ERROR, "Системийн алдаа")]})
    (tmp_path / "raw.json").write_text(json.dumps(run.to_dict(), ensure_ascii=False), encoding="utf-8")
    back = load_run(tmp_path / "raw.json")
    assert back.results[2].findings[0].kind == M.SYSTEM_ERROR and back.results[0].item.name == "Цэс 0"
    s = generate_reports(back, cfg, tmp_path, verify=False)
    assert (tmp_path / s["xlsx"]).exists() and (tmp_path / "screenshots.zip").exists()
    assert not (tmp_path / "screenshots.zip.part").exists()



def test_same_crash_from_smoke_and_form_is_one_bug(cfg):
    """Smoke ба форм шалгалт нэг цэсийн таб хаасны дараах уналтыг илрүүлсэн бол 2 Critical биш — 1 bug (2 TC)."""
    from app.runner.classify import build_bugs, build_test_cases
    crash = Finding(M.RUNTIME_CRASH, "Unhandled Exception:", detail="System.ObjectDisposedException: X\n at A.Pages.RPARobotList.F()",
                    after_close=True)
    run = make_run(4, {1: [crash]})
    key = run.results[1].item.key
    run.flows = [{"flow": "form: Мастер дата / Тохиргоо / Цэс 1", "code": "F01", "status": M.FAIL, "run_tag": "T",
                  "meta": {"formcheck": True, "menu": "Мастер дата / Тохиргоо / Цэс 1", "menu_key": key},
                  "steps": [{"index": 1, "uses": "form.close_tab", "name": "Цэсийн табыг хаах", "status": M.FAIL,
                             "message": "Цэсийн табыг хаасны дараа апп унав", "notes": [],
                             "findings": [{"kind": M.RUNTIME_CRASH, "text": "Unhandled Exception:"}]}]}]
    tcs, _ = build_test_cases(run, cfg, "QA", "2026-10-09")
    bugs = build_bugs(run, tcs, cfg)
    crit = [b for b in bugs if b.severity.rank == 0]
    assert len(crit) == 1 and len(crit[0].tc_ids) == 2, [(b.title, b.tc_ids) for b in bugs]


def test_previous_summary_prefers_largest_overlap(monkeypatch):
    """Харьцуулах өмнөх run: хамгийн сүүлийн хэсэгчилсэн (үргэлжлүүлсэн) run биш, хамгийн олон цэс давхцсан нь;
    smoke-ийн ачааллыг smoke-той run-аас."""
    from app import api
    full = {"flows": [{"flow": f"form: М / {i}"} for i in range(10)], "smoke_load": {"k": 500}}
    part = {"flows": [{"flow": f"form: М / {i}"} for i in range(3)]}

    class FakeStore:
        def list(self, n, owner=None):
            return [{"id": "part", "host": "testhr.smartapp.mn", "status": "done"},
                    {"id": "full", "host": "testhr.smartapp.mn", "status": "done"}]

        def get(self, rid):
            return {"summary": part if rid == "part" else full, "version_label": rid}

    monkeypatch.setattr(api, "store", FakeStore())
    prev = api._previous_summary("now", "https://testhr.smartapp.mn/", [{"flow": f"form: М / {i}"} for i in range(10)])
    assert prev["_run_id"] == "full" and prev["smoke_load"] == {"k": 500}
