"""BLOCKED-ийн шалтгаан тогтоолт: зөвхөн апп талын НОТОЛГООТОЙ саад Blocked, бусад нь «Шалгагдаагүй»."""
from __future__ import annotations

import json
import threading

import pytest

from app import models as M
from app.flows.context import FlowRun, StepResult, make_run_tag
from app.runner.classify.cause import cause_of, status_for
from app.runner.classify.testcases import item_effective_status, step_effective_status
from app.runner.classify.verdict import coverage, go_no_go


@pytest.mark.parametrize("category,message,cause", [
    # апп талын нотолгоо
    ("", "Сервертэй холбогдож чадсангүй (HTTP 503)", "app"),
    ("", "TypeError: Failed to fetch", "app"),
    ("", "page.goto: net::ERR_CONNECTION_REFUSED", "app"),
    ("infra", "Нэмэх товч олдсонгүй", "app"),
    ("", "Тест орчин хариу өгөөгүй (дахин ачаалсан ч): цэс олдсонгүй", "app"),
    # хэрэгсэл — нотолгоогүй бүх тохиолдол
    ("", "Timeout 30000ms exceeded", "tool"),
    ("tool", "locator.click: element is not visible", "tool"),
    ("", "Хэрэгслийн шалгалтын хугацаа хэтэрсэн (90 сек)", "tool"),
    ("", "KeyError: 'menu'", "tool"),
    ("", "", "tool"),
    # өгөгдөл / хамаарал / хэрэглэгч / хамаарахгүй
    ("nodata", "Жагсаалтад бичлэг алга", "data"),
    ("", "Өмнөх алхам #1 (Бүртгэх) Fail", "dependent"),
    ("", "Шаардлагатай өгөгдөл алга: record", "dependent"),
    ("", "Хэрэглэгч зогсоосон", "operator"),
    ("", "Session алдагдсан — дахин нэвтрээгүй", "operator"),
    ("", "Хөтөч хаагдсан", "operator"),
    ("na", "Заавал талбаргүй", "na"),
])
def test_cause_of(category, message, cause):
    assert cause_of(category, message) == cause


def test_status_for_only_app_is_blocked():
    assert status_for("", "HTTP 502 Bad Gateway")[0] == M.BLOCKED
    for msg in ("Timeout 30000ms exceeded", "selector олдсонгүй", "Өмнөх алхам #2 Fail", "Хэрэглэгч зогсоосон"):
        assert status_for("", msg)[0] == M.NOT_TESTED, msg


def test_legacy_blocked_reclassified():
    # хуучин raw.json — шалтгаангүй Blocked
    assert step_effective_status({"status": M.BLOCKED, "category": "tool", "message": "Timeout"}) == M.NOT_TESTED
    assert step_effective_status({"status": M.BLOCKED, "message": "Failed to fetch"}) == M.BLOCKED
    assert step_effective_status({"status": M.BLOCKED, "cause": "app"}) == M.BLOCKED
    assert step_effective_status({"status": M.PASS}) == M.PASS
    r = M.ItemResult(item=M.MenuItem(module="A", folder="", name="B", href="/b"), status=M.BLOCKED,
                     blocked_reason="Хэрэгслийн шалгалтын алдаа: boom")
    assert item_effective_status(r) == M.NOT_TESTED


def test_flowrun_status_not_tested_vs_blocked():
    def run(*steps):
        fr = FlowRun(run_id="r", url="u", flow="f", run_tag="T")
        fr.steps = list(steps)
        return fr
    ok = StepResult(index=1, name="a", uses="x", status=M.PASS)
    nt = StepResult(index=2, name="b", uses="x", status=M.NOT_TESTED, cause="tool")
    data = StepResult(index=2, name="b", uses="x", status=M.NOT_TESTED, category="nodata", cause="data")
    blk = StepResult(index=2, name="b", uses="x", status=M.BLOCKED, cause="app")
    fail = StepResult(index=3, name="c", uses="x", status=M.FAIL)
    assert run(ok, nt).status == M.NOT_TESTED
    assert run(ok, data).status == M.PASS          # тест орчны өгөгдөлгүй алхам урсгалыг унагахгүй
    assert run(ok, blk, nt).status == M.BLOCKED
    assert run(ok, blk, fail).status == M.FAIL


def _tc(result):
    return M.TestCase(tc_id="T", req_ref="", purpose="", precondition="", steps="", expected="", test_type="",
                      priority="", automation="", result=result, executor="", date="")


def test_coverage_and_incomplete_verdict():
    tcs = [_tc(M.PASS)] * 90
    nt = [{"cause": "tool"}] * 20 + [{"cause": "data"}] * 50
    cov = coverage(tcs, nt)
    assert cov["not_tested"] == 70 and cov["by_cause"] == {"tool": 20, "data": 50}
    assert cov["pct"] == pytest.approx(81.8, abs=0.1)       # өгөгдөлгүй нь хамрах хүрээг бууруулахгүй
    g = go_no_go(tcs, [], not_tested=nt)
    assert g["decision"] == "INCOMPLETE" and "Хамрах хүрээ" in " ".join(g["reasons"])
    assert go_no_go(tcs, [], not_tested=[{"cause": "data"}] * 50)["decision"] == "GO"


def test_run_tag_unique_under_threads():
    out: list[str] = []
    lock = threading.Lock()

    def work():
        tags = [make_run_tag() for _ in range(300)]
        with lock:
            out.extend(tags)
    ths = [threading.Thread(target=work) for _ in range(20)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert len(out) == len(set(out)) == 6000


def test_auto_tool_transient_retry_predicate():
    from app.pipeline.stages import auto_tool_transient
    fr = FlowRun(run_id="r", url="u", flow="f", run_tag="T")
    fr.steps = [StepResult(index=1, name="a", uses="crud.auto_create", status=M.NOT_TESTED, cause="tool",
                           message="locator.click: Timeout 30000ms exceeded")]
    assert auto_tool_transient(fr)
    fr.steps[0].message = "Хөтөч хаагдсан"                 # хөтөч хаагдсан → давтахгүй
    assert not auto_tool_transient(fr)
    fr.steps[0] = StepResult(index=1, name="a", uses="crud.auto_create", status=M.FAIL, message="Timeout")
    assert not auto_tool_transient(fr)                     # Fail (апп-ын хариу) — давтахгүй
    fr.steps = [StepResult(index=1, name="a", uses="x", status=M.PASS),
                StepResult(index=2, name="b", uses="x", status=M.NOT_TESTED, cause="tool", message="Timeout")]
    assert not auto_tool_transient(fr)                     # өгөгдөл үүссэний дараа — давтахгүй


def test_rebuild_reclassifies_legacy_blocked(tmp_path):
    from app.report.rebuild import load_run
    raw = {
        "run_id": "r1", "project": "P", "url": "https://testaccounting.smartapp.mn", "results": [
            {"item": {"module": "A", "folder": "", "name": "B", "href": "/b"}, "status": M.BLOCKED,
             "blocked_reason": "Хэрэгслийн шалгалтын хугацаа хэтэрсэн (90 сек)"},
            {"item": {"module": "A", "folder": "", "name": "C", "href": "/c"}, "status": M.BLOCKED,
             "blocked_reason": "Сервертэй холбогдож чадсангүй"}],
        "flows": [{"flow": "auto: X", "status": M.BLOCKED, "meta": {"auto": True}, "steps": [
            {"index": 1, "name": "a", "uses": "crud.auto_create", "status": M.BLOCKED, "category": "tool",
             "message": "Timeout 30000ms exceeded"}]}],
    }
    p = tmp_path / "raw.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    run = load_run(p)
    assert [r.status for r in run.results] == [M.NOT_TESTED, M.BLOCKED]
    assert run.flows[0]["status"] == M.NOT_TESTED and run.flows[0]["steps"][0]["cause"] == "tool"


def test_saved_but_invisible_record_is_not_a_bug():
    """testaccounting 2026-10-28: ДҮ-ийн баримт алдаагүй хадгалагдаж өөр жагсаалтаас олдсон ч хайлт + устгал хоёр
    Major bug болж байсан — нотолгоогүй тул Шалгагдаагүй (tool), устгал нь түүний үр дагавар."""
    from app.pipeline.stages import reclassify_invisible_record
    steps = [
        {"index": 1, "uses": "crud.auto_create", "status": M.PASS, "message": ""},
        {"index": 2, "uses": "crud.search", "status": M.FAIL, "message": "'T A1 автомат тест' хайлтаар мөр олдсонгүй"},
        {"index": 3, "uses": "crud.auto_update", "status": M.NOT_TESTED,
         "message": "Өмнөх алхам #2 (Бүртгэснээ хайж олох) Fail"},
        {"index": 5, "uses": "crud.delete", "status": M.FAIL, "message": "'T A1 автомат тест' агуулсан мөр олдсонгүй"},
    ]
    assert reclassify_invisible_record(steps)
    assert [s["status"] for s in steps] == [M.PASS, M.NOT_TESTED, M.NOT_TESTED, M.NOT_TESTED]
    assert steps[1]["cause"] == "tool" and steps[3]["cause"] == "dependent"
    assert cause_of("", steps[3]["message"]) == "dependent" and steps[2]["message"].endswith("Шалгагдаагүй")
    # Хадгалахад алдаа гарсан (#1 Fail) бол хөндөхгүй — тэр нь апп-ын хариу
    other = [{"index": 1, "uses": "crud.auto_create", "status": M.FAIL, "message": "Системийн алдаа"},
             {"index": 2, "uses": "crud.search", "status": M.FAIL, "message": "мөр олдсонгүй"}]
    assert not reclassify_invisible_record(other) and other[1]["status"] == M.FAIL


def test_update_outcomes_reclassified():
    """testhr 2026-10-28: засварын анхааруулга (A165) Major bug болж, засвар хадгалахад алдаа гарсны дараах
    устгалын «мөр олдсонгүй» (A20) давхар Major bug болж байсан."""
    from app.pipeline.stages import reclassify_update_outcomes
    warn = [
        {"index": 1, "uses": "crud.auto_create", "status": M.PASS, "message": ""},
        {"index": 3, "uses": "crud.auto_update", "status": M.FAIL,
         "message": "Хадгалахад: баталгаажуулалтын анхааруулга — Анхаар\nМэдээлэл хоосон байна!"},
        {"index": 4, "uses": "crud.auto_verify", "status": M.NOT_TESTED, "message": "Өмнөх алхам #3 (Автомат засвар) Fail"},
    ]
    assert reclassify_update_outcomes(warn)
    assert warn[1]["status"] == M.NOT_TESTED and warn[1]["cause"] == "tool"
    assert warn[2]["message"].endswith("Шалгагдаагүй")
    err = [
        {"index": 3, "uses": "crud.auto_update", "status": M.FAIL,
         "message": "Хадгалахад: Cистемийн алдаа\nInvalid column name 'TemplateType'."},
        {"index": 5, "uses": "crud.delete", "status": M.FAIL, "message": "'T A20 автомат тест' агуулсан мөр олдсонгүй"},
    ]
    assert reclassify_update_outcomes(err)
    assert err[0]["status"] == M.FAIL                      # серверийн алдаа — бодит bug хэвээр
    assert err[1]["status"] == M.NOT_TESTED and cause_of("", err[1]["message"]) == "dependent"
    # Засварыг НЭЭХЭД алдаа (хадгалаагүй) — устгал «олдсонгүй» бол бие даасан Fail хэвээр
    opened = [{"index": 3, "uses": "crud.auto_update", "status": M.FAIL, "message": "Бичлэгийг засахаар нээхэд: x"},
              {"index": 5, "uses": "crud.delete", "status": M.FAIL, "message": "мөр олдсонгүй"}]
    assert not reclassify_update_outcomes(opened) and opened[1]["status"] == M.FAIL


def test_click_intercepted_without_app_message_is_not_a_bug(tmp_path):
    """testaccounting 2026-10-29: «Шинэ» товчийг өөр элемент хааж Page.click Timeout — алдааны цонхгүй тул Major bug
    биш, Шалгагдаагүй (tool)."""
    from app.report.rebuild import load_run
    msg = "«Шинэ» товчийг дарж чадсангүй (өөр цонх/элемент хааж байна): Page.click: Timeout 5000ms exceeded."
    raw = {"run_id": "r1", "project": "P", "url": "https://testaccounting.smartapp.mn", "results": [],
           "flows": [{"flow": "form: A / B", "status": M.FAIL, "meta": {}, "steps": [
               {"index": 2, "name": "Шинэ форм", "uses": "form.check_new", "status": M.FAIL, "message": msg}]}]}
    p = tmp_path / "raw.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    run = load_run(p)
    assert run.flows[0]["steps"][0]["status"] == M.NOT_TESTED and run.flows[0]["steps"][0]["cause"] == "tool"
    assert run.flows[0]["status"] == M.NOT_TESTED
    assert status_for("tool", msg)[0] == M.NOT_TESTED
