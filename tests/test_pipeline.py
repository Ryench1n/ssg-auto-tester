"""Тестийн урсгал (pipeline): гар нэвтрэлт → smoke → CRUD flow-ууд нэг session, нэг тайланд.

Mock CRUD сайт (tests/fixtures/mock_crud) дээр. Хүний гар нэвтрэлтийг HumanLogin hook дуурайна;
session тасрахыг зөвхөн энэ файлд бүртгэсэн тестийн алхам (test.*) үүсгэнэ.
"""
import asyncio
import os
import sqlite3
import zipfile

import pytest
from openpyxl import load_workbook

from app import models as M
from app.config import load_config
from app.flows.context import StepFailed
from app.flows.engine import step
from app.pipeline.runner import PipelineRunner
from app.report.build import generate_reports
from app.runner.smoke import RunOptions
from app.store import Store
from tests.conftest import login_client
from tests.fixtures.mock_crud.server import PASSWORD, CrudServer
from tests.human_login import HumanLogin

e2e = pytest.mark.skipif(os.environ.get("SSG_SKIP_E2E") == "1", reason="SSG_SKIP_E2E=1")

FAST = {"timing": {"app_ready_timeout_ms": 10000, "login_timeout_ms": 10000, "loading_blocked_ms": 6000,
                   "wait_ms": 1200, "post_close_wait_ms": 500, "item_timeout_s": 25},
        "flow": {"step_timeout_s": 40, "settle_ms": 250, "save_timeout_ms": 6000},
        "confirm_failures": False}
MENU = "Мастер дата / Бүртгэл / Харилцагч"

CUSTOMER_FLOW = {"code": "CUST", "steps": [
    {"uses": "auth.login", "name": "Нэвтрэх", "with": {"role": "admin"}},
    {"uses": "crud.create", "name": "Харилцагч бүртгэх", "with": {
        "menu": MENU, "fields": {"Нэр": "{run_tag} Pipeline харилцагч", "Утас": "99001122"},
        "save_as": "customer", "capture": {"customer_code": "Код"}}},
    {"uses": "crud.search", "name": "Хайх", "with": {"text": "{customer}", "contains": ["{customer_code}"]}},
    {"uses": "crud.delete", "name": "Цэвэрлэх", "always": True, "with": {"find": "{customer}"}},
]}
BROKEN_FLOW = {"steps": [
    {"uses": "crud.create", "name": "Алдаатай бүртгэл", "with": {
        "menu": MENU, "fields": {"Нэр": "{run_tag} Алдаатай", "Регистр": "ERR500"}, "save_as": "bad"}},
    {"uses": "crud.delete", "name": "Цэвэрлэх", "always": True, "with": {"find": "{bad}"}},
]}


# ---------------------------------------------------------------- тестийн алхмууд (зөвхөн энэ тестэд)
async def _kick(env) -> None:
    """Session-ийг хүчээр тасална (mock сайт role-оо localStorage-д хадгалдаг)."""
    await env.page.evaluate("() => localStorage.removeItem('mock_role')")
    await env.page.goto(env.url.rstrip("/") + "/auth/login", wait_until="domcontentloaded")


@step("test.kick_session")
async def kick_session(env, args: dict) -> dict:
    await _kick(env)
    return {}


@step("test.kick_mid_step")
async def kick_mid_step(env, args: dict) -> dict:
    """Алхмын ЯВЦАД session тасарна: эхний удаа унаж, дахин нэвтэрсний дараа амжилттай."""
    if not env.resumed:
        await _kick(env)
        raise StepFailed("Session тасарсан (тест)")
    return {"resumed_ok": "yes"}


class OneTimeHuman(HumanLogin):
    """Зөвхөн эхний удаа нэвтэрнэ — дараагийн (session тасарсны дараах) хүлээлтэд хариу өгөхгүй."""

    async def __call__(self, page) -> None:
        self.enabled = self.calls == 0
        await super().__call__(page)


@pytest.fixture(scope="module")
def srv():
    with CrudServer() as s:
        yield s


def make_pipe(srv, tmp_path, flows, steps, human=None, overrides=None, events=None):
    over = {**FAST, **(overrides or {})}
    cfg = load_config(srv.url, {**over, "flows": flows})
    pipe = PipelineRunner("p", srv.url, cfg, RunOptions(project="Pipe", version_label="v1.0"), tmp_path, steps,
                          emit=(lambda t, d: events.append((t, d))) if events is not None else None,
                          headless=True, on_waiting_login=human or HumanLogin("admin", PASSWORD))
    return pipe, cfg


# ---------------------------------------------------------------- smoke + crud нэг session
@pytest.fixture(scope="module")
def full(srv, tmp_path_factory):
    rd = tmp_path_factory.mktemp("pipe")
    events: list = []
    before = (srv.state.logins.get("admin", 0), srv.state.created, srv.state.deleted)
    pipe, cfg = make_pipe(srv, rd, {"customer_crud": CUSTOMER_FLOW, "broken_crud": BROKEN_FLOW},
                          [{"kind": "smoke"}, {"kind": "crud", "flow": "customer_crud"},
                           {"kind": "crud", "flow": "broken_crud"}], events=events)
    data = asyncio.run(pipe.run())
    summary = generate_reports(data, cfg, rd, verify=False)
    return {"data": data, "summary": summary, "events": events, "dir": rd, "before": before, "srv": srv}


@e2e
def test_smoke_and_crud_share_one_session(full):
    d, srv = full["data"], full["srv"]
    assert d.login_ok and d.smoke_ran
    assert [p["kind"] for p in d.pipeline] == ["auth", "smoke", "crud", "crud"]
    assert [p["status"] for p in d.pipeline] == [M.PASS, M.PASS, M.PASS, M.FAIL], d.pipeline
    # Нэг л гар нэвтрэлт: smoke болон 2 flow бүгд нэг session ашигласан
    assert srv.state.logins["admin"] - full["before"][0] == 1
    assert sum(1 for t, _ in full["events"] if t == "waiting_login") == 1
    assert {r.item.name for r in d.results} == {"Харилцагч", "Бараа", "Ангилал", "Бүлэг", "Гүйлгээ", "Журнал", "Данс"}
    stages = [e["stage"] for t, e in full["events"] if t == "stage" and e["state"] == "running"]
    assert stages == ["auth", "smoke", "crud:customer_crud", "crud:broken_crud"]


@e2e
def test_shared_context_and_data_handoff(full):
    d = full["data"]
    cust, broken = d.flows
    assert cust["run_tag"] == broken["run_tag"] == d.run_tag          # нэг FlowContext
    assert [s["status"] for s in cust["steps"]] == [M.PASS] * 4
    assert cust["data"]["customer"] == f"{d.run_tag} Pipeline харилцагч"
    assert cust["data"]["customer_code"].startswith("C-")
    # Нэг flow унасан ч дараагийн flow ажилласан; дотор нь Fail → дараагийн алхам Blocked
    assert [s["status"] for s in broken["steps"]] == [M.FAIL, M.BLOCKED]
    srv = full["srv"]
    assert srv.state.created - full["before"][1] == 1 and srv.state.deleted - full["before"][2] == 1
    assert not any(d.run_tag in c["name"] for c in srv.state.customers)


@e2e
def test_single_report_contains_flow_tcs(full):
    s, rd = full["summary"], full["dir"]
    ids = [t["id"] for t in s["tcs"]]
    assert "TC-LOGIN-001" in ids and any(i.startswith("TC-MD-") for i in ids)
    flow_tcs = [t for t in s["tcs"] if t["id"].startswith("TC-FLOW-")]
    assert [t["id"] for t in flow_tcs[:4]] == [f"TC-FLOW-CUST-{i:03d}" for i in range(1, 5)]
    assert all(t["module"] == "E2E" for t in flow_tcs)
    assert s["stats"]["total"] == len(s["tcs"]) and s["stats"]["fail"] >= 1      # flow TC GO/NO-GO-д орсон
    # Унасан алхмын дараах (хамааралтай) алхмууд тусдаа Blocked TC биш — эх TC-д «… алхам ажиллаагүй»
    assert any("алхам ажиллаагүй" in t["note"] for t in flow_tcs), [t["note"] for t in flow_tcs]
    assert s["decision"] == "NO-GO"
    fb = [b for b in s["bugs"] if b["signature"].startswith("flow|broken_crud|Алдаатай бүртгэл|")]
    assert len(fb) == 1 and fb[0]["severity"] == "🟡 Major"
    assert full["data"].run_tag not in fb[0]["signature"]            # regression-д тогтвортой
    # Нэг xlsx + нэг docx
    assert sorted(p.name for p in rd.glob("*.xlsx")) == ["Pipe-Test-Cases-v1.0.xlsx"]
    assert sorted(p.name for p in rd.glob("*.docx")) == ["Pipe-Test-Report-v1.0.docx"]
    ws = load_workbook(rd / "Pipe-Test-Cases-v1.0.xlsx")["📋 Test Cases"]
    col_a = {c.value for c in ws["A"]}
    assert {"TC-FLOW-CUST-001", "TC-FLOW-BC-001"} <= col_a
    with zipfile.ZipFile(rd / "Pipe-Test-Report-v1.0.docx") as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "E2E урсгал" in xml and full["data"].run_tag in xml and "Цэвэрлэгээ" in xml


@e2e
def test_password_never_written(full):
    for p in full["dir"].rglob("*"):
        if not p.is_file():
            continue
        if p.suffix in (".xlsx", ".docx", ".zip"):
            with zipfile.ZipFile(p) as z:
                blob = b"".join(z.read(n) for n in z.namelist())
        else:
            blob = p.read_bytes()
        assert PASSWORD.encode() not in blob, p


# ---------------------------------------------------------------- session тасрах
@e2e
def test_session_drop_pauses_and_resumes(srv, tmp_path):
    events: list = []
    human = HumanLogin("admin", PASSWORD)
    pipe, _ = make_pipe(srv, tmp_path, {"f": {"steps": [
        {"uses": "crud.open", "name": "Цэс нээх", "with": {"menu": MENU}},
        {"uses": "test.kick_session", "name": "Session тасрах (алхам хооронд)"},
        {"uses": "crud.open", "name": "Дахин нэвтэрсний дараа", "with": {"menu": MENU}},
        {"uses": "test.kick_mid_step", "name": "Алхмын явцад тасрах"},
        {"uses": "crud.search", "name": "Үргэлжлэл", "with": {"menu": MENU, "text": "Бодит"}},
    ]}}, [{"kind": "crud", "flow": "f"}], human=human, events=events)
    d = asyncio.run(pipe.run())
    steps = d.flows[0]["steps"]
    assert [s["status"] for s in steps] == [M.PASS] * 5, [(s["name"], s["message"]) for s in steps]
    assert steps[3]["retried"] and steps[3]["outputs"] == {"resumed_ok": "yes"}
    waits = [e for t, e in events if t == "waiting_login"]
    assert len(waits) == 3 and human.calls == 3
    assert all(w["stage"] == "crud:f" for w in waits[1:]) and "session тасарсан" in waits[1]["reason"]
    assert len(d.logouts) == 2 and len(d.login_times) == 3
    assert sum(1 for t, _ in events if t == "login_ok") == 3


@e2e
def test_session_lost_timeout_blocks_remaining(srv, tmp_path):
    events: list = []
    pipe, _ = make_pipe(srv, tmp_path, {
        "a": {"steps": [{"uses": "crud.open", "with": {"menu": MENU}},
                        {"uses": "test.kick_session"},
                        {"uses": "crud.open", "with": {"menu": MENU}}]},
        "b": {"steps": [{"uses": "crud.open", "with": {"menu": MENU}}]},
    }, [{"kind": "crud", "flow": "a"}, {"kind": "crud", "flow": "b"}],
        human=OneTimeHuman("admin", PASSWORD), events=events,
        overrides={"timing": {**FAST["timing"], "manual_login_timeout_ms": 2000, "relogin_timeout_ms": 2000}})
    d = asyncio.run(pipe.run())
    a, b = d.flows
    assert [s["status"] for s in a["steps"]] == [M.PASS, M.PASS, M.BLOCKED]
    assert [s["status"] for s in b["steps"]] == [M.BLOCKED]
    assert "дахин нэвтрээгүй" in a["steps"][2]["message"]
    assert [p["status"] for p in d.pipeline] == [M.PASS, M.BLOCKED, M.BLOCKED]
    assert not d.cancelled and any(t == "session_lost" for t, _ in events)


# ---------------------------------------------------------------- API (browser-гүй)
@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.api as api
    monkeypatch.setattr(api, "store", Store(tmp_path / "runs.db"))
    return login_client(api, api.store)


def test_api_flows_lists_site_flows(client):
    r = client.get("/api/flows", params={"url": "https://testaccounting.smartapp.mn/"})
    assert r.status_code == 200
    f = {x["name"]: x for x in r.json()}["customer_group_crud"]
    assert f["allowed"] and f["code"] == "CGC" and f["steps"] == 8 and "→" in f["description"]


def test_api_flows_not_allowed_on_non_test_host(client, monkeypatch):
    import app.api as api
    real = api.load_config
    monkeypatch.setattr(api, "load_config", lambda url, over=None: real(url, {**(over or {}), "flows": {
        "x": [{"uses": "crud.open", "with": {"menu": MENU}}]}}))
    r = client.get("/api/flows", params={"url": "https://accounting.smartapp.mn/"})
    [f] = r.json()
    assert not f["allowed"] and "allowed_hosts" in f["reason"]


def test_api_rejects_crud_on_non_allowed_host_before_start(client):
    import app.api as api
    n_live = len(api.LIVE)
    r = client.post("/api/runs", json={"url": "https://accounting.smartapp.mn/", "password": "x",
                                       "steps": [{"kind": "crud", "flow": "customer_group_crud"}]})
    assert r.status_code == 400 and "allowed_hosts" in r.json()["detail"]
    assert len(api.LIVE) == n_live and api.store.list() == []


def test_api_rejects_unknown_kind(client):
    r = client.post("/api/runs", json={"url": "https://testaccounting.smartapp.mn/",
                                       "steps": [{"kind": "business"}]})
    assert r.status_code == 400 and "business" in r.json()["detail"]


# ---------------------------------------------------------------- store migration
def test_store_migrates_old_db(tmp_path):
    db = tmp_path / "runs.db"
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE runs (id TEXT PRIMARY KEY, created_at TEXT, finished_at TEXT, url TEXT, host TEXT,
            project TEXT, qa_lead TEXT, version_label TEXT, app_version TEXT, status TEXT, decision TEXT,
            pass_rate REAL, total INTEGER, summary_json TEXT, error TEXT)""")
        c.execute("INSERT INTO runs (id, created_at, project, status) VALUES ('old', '2026-01-01', 'P', 'done')")
    s = Store(db)
    assert s.get("old")["pipeline"] == [] and s.list()[0]["id"] == "old"
    s.create("new", "u", "h", "P", "", "v1.1", [{"kind": "auth", "status": "Pending"}])
    s.update("new", pipeline=[{"kind": "auth", "status": "Pass"}])
    assert s.get("new")["pipeline"] == [{"kind": "auth", "status": "Pass"}]
    Store(db)   # дахин ачаалахад migration давхар ажиллахгүй
