"""Component-based E2E flow engine — mock CRUD сайт (tests/fixtures/mock_crud) дээр.

Flow нь PipelineRunner-оор (нэвтрэх → crud) ажиллана; хүний гар нэвтрэлтийг HumanLogin hook дуурайна.
"""
import asyncio
import os

import pytest

from app import models as M
from app.config import load_config
from app.flows.context import FlowContext, MissingData, placeholders
from app.flows.engine import flow_code
from app.pipeline.runner import PipelineRunner
from app.pipeline.stages import PipelineConfigError
from app.runner.smoke import RunOptions
from tests.fixtures.mock_crud.server import PASSWORD, CrudServer
from tests.human_login import HumanLogin

e2e = pytest.mark.skipif(os.environ.get("SSG_SKIP_E2E") == "1", reason="SSG_SKIP_E2E=1")

FAST = {"timing": {"app_ready_timeout_ms": 10000, "login_timeout_ms": 10000, "loading_blocked_ms": 6000},
        "flow": {"step_timeout_s": 40, "settle_ms": 250, "save_timeout_ms": 6000}}
MENU = "Мастер дата / Бүртгэл / Харилцагч"


@pytest.fixture(scope="module")
def srv():
    with CrudServer() as s:
        yield s


def run_flow(srv, tmp_path, steps, user="admin", name="t", human=None, overrides=None):
    cfg = load_config(srv.url, {**FAST, "flows": {name: steps}, **(overrides or {})})
    pipe = PipelineRunner(name, srv.url, cfg, RunOptions(project="T"), tmp_path,
                          steps=[{"kind": "crud", "flow": name}], headless=True,
                          on_waiting_login=human or HumanLogin(user, PASSWORD))
    data = asyncio.run(pipe.run())
    return data, data.flows[0]


def statuses(flow):
    return [s["status"] for s in flow["steps"]]


def kinds(step):
    return {f["kind"] for f in step["findings"]}


# ---------------------------------------------------------------- context (browser-гүй)
def test_placeholders_and_render():
    assert placeholders({"a": "{run_tag} {customer}", "b": ["{code}"]}) == {"customer", "code"}
    ctx = FlowContext("SSG0928AB12", {"customer": "X"})
    assert ctx.render({"n": "{run_tag}-{customer}"}) == {"n": "SSG0928AB12-X"}
    with pytest.raises(MissingData):
        ctx.render("{nope}")


def test_host_guard_blocks_non_test_host(tmp_path):
    cfg = load_config("https://accounting.smartapp.mn/", {"flows": {"f": [{"uses": "auth.login"}]}})
    with pytest.raises(PipelineConfigError, match="allowed_hosts"):
        PipelineRunner("x", "https://accounting.smartapp.mn/", cfg, RunOptions(project="X"), tmp_path,
                       steps=[{"kind": "crud", "flow": "f"}])


def test_unknown_step_rejected(tmp_path):
    cfg = load_config("http://127.0.0.1:1/", {"flows": {"f": [{"uses": "crud.nope"}]}})
    with pytest.raises(PipelineConfigError, match="crud.nope"):
        PipelineRunner("x", "http://127.0.0.1:1/", cfg, RunOptions(project="X"), tmp_path,
                       steps=[{"kind": "crud", "flow": "f"}])


def test_flow_code():
    assert flow_code("customer_group_crud") == "CGC"
    assert flow_code("customers") == "CUSTOM"
    assert flow_code("x", {"code": "cust-1"}) == "CUST1"


# ---------------------------------------------------------------- E2E
@e2e
def test_full_lifecycle_passes_and_hands_off_data(srv, tmp_path):
    steps = [
        {"uses": "auth.login", "name": "Admin нэвтрэх", "with": {"role": "admin"}},
        {"uses": "auth.expect_access", "with": {"menu": MENU, "allowed": True}},
        {"uses": "crud.create", "name": "Харилцагч бүртгэх", "with": {
            "menu": MENU, "fields": {"Нэр": "{run_tag} Тест харилцагч", "Регистр": "7654321", "Утас": "99112233"},
            "save_as": "customer", "capture": {"customer_code": "Код"}}},
        {"uses": "crud.search", "with": {"text": "{customer}", "contains": ["{customer_code}", "99112233"]}},
        {"uses": "crud.update", "with": {"find": "{customer}", "key": "Нэр", "save_as": "customer",
                                          "fields": {"Нэр": "{run_tag} Засварласан", "Утас": "99887766"}}},
        {"uses": "crud.search", "with": {"text": "{customer}", "contains": ["99887766"]}},
        # Тусдаа тест модуль: идэвхтэй session-ийн storage_state-ээр шинэ context — дахин нэвтрэхгүй
        {"uses": "auth.login", "name": "Session дамжуулах", "with": {"role": "admin", "new_context": True}},
        {"uses": "crud.search", "with": {"menu": MENU, "text": "{customer}"}},
        {"uses": "crud.delete", "always": True, "with": {"find": "{customer}"}},
        {"uses": "crud.search", "with": {"text": "{customer}", "expect": "absent"}},
    ]
    before_logins = srv.state.logins.get("admin", 0)
    data, flow = run_flow(srv, tmp_path, steps)
    assert statuses(flow) == [M.PASS] * len(steps), [(s["name"], s["status"], s["message"]) for s in flow["steps"]]
    assert flow["status"] == M.PASS and data.app_version == "v3.0.0.1"
    assert flow["data"]["customer"] == f"{flow['run_tag']} Засварласан"
    assert flow["data"]["customer_code"].startswith("C-")
    assert srv.state.logins["admin"] - before_logins == 1          # нэг гар нэвтрэлт, session дахин ашиглагдсан
    assert any("storage_state" in n for n in flow["steps"][6]["notes"])
    assert not any(flow["run_tag"] in c["name"] for c in srv.state.customers)   # цэвэрлэгдсэн
    assert any(c["name"] == "Бодит харилцагч ХХК" for c in srv.state.customers)
    assert (tmp_path / "raw.json").exists()


@e2e
def test_viewer_has_no_access(srv, tmp_path):
    """Хэрэглэгч viewer-ээр гараар нэвтэрсэн — YAML-ын role (admin) нь зөвхөн анхааруулга."""
    data, flow = run_flow(srv, tmp_path, [
        {"uses": "auth.login", "with": {"role": "admin"}},
        {"uses": "auth.expect_access", "with": {"menu": MENU, "allowed": False}},
    ], user="viewer")
    assert statuses(flow) == [M.PASS, M.PASS], [(s["name"], s["message"]) for s in flow["steps"]]
    assert any("role 'admin'" in n for n in flow["steps"][0]["notes"])
    assert "олон role дэмжигдэхгүй" in (tmp_path / "run.log").read_text(encoding="utf-8")


@e2e
def test_failure_blocks_dependents_and_cleanup_needs_data(srv, tmp_path):
    created = srv.state.created
    data, flow = run_flow(srv, tmp_path, [
        {"uses": "auth.login", "with": {"role": "admin"}},
        {"uses": "crud.create", "with": {"menu": MENU, "save_as": "customer",
                                          "fields": {"Нэр": "{run_tag} Алдаатай", "Регистр": "ERR500"}}},
        {"uses": "crud.search", "with": {"text": "{customer}"}},
        {"uses": "crud.delete", "always": True, "with": {"find": "{customer}"}},
    ])
    steps = flow["steps"]
    assert statuses(flow) == [M.PASS, M.FAIL, M.NOT_TESTED, M.NOT_TESTED]
    assert {M.SYSTEM_ERROR, M.HTTP_5XX} <= kinds(steps[1]), steps[1]["findings"]
    assert "RegNo" in steps[1]["message"] and steps[1]["screenshot"]
    assert "#2" in steps[2]["message"]                  # өмнөх алхмаас болж Blocked
    assert "customer" in steps[3]["message"]            # цэвэрлэх өгөгдөлгүй
    assert srv.state.created == created


@e2e
def test_validation_warning_is_fail(srv, tmp_path):
    data, flow = run_flow(srv, tmp_path, [
        {"uses": "auth.login", "with": {"role": "admin"}},
        {"uses": "crud.create", "with": {"menu": MENU, "key": "Регистр", "fields": {"Регистр": "{run_tag}"}}},
    ])
    assert statuses(flow) == [M.PASS, M.FAIL]
    assert "Нэр оруулна уу" in flow["steps"][1]["message"]
    assert M.WARNING_TOAST in kinds(flow["steps"][1])


@e2e
def test_never_touches_untagged_data(srv, tmp_path):
    created, deleted = srv.state.created, srv.state.deleted
    data, flow = run_flow(srv, tmp_path, [
        {"uses": "auth.login", "with": {"role": "admin"}},
        {"uses": "crud.create", "with": {"menu": MENU, "fields": {"Нэр": "Шошгогүй харилцагч"}}},
        {"uses": "crud.delete", "always": True, "with": {"menu": MENU, "find": "Бодит харилцагч ХХК"}},
    ])
    assert statuses(flow) == [M.PASS, M.FAIL, M.FAIL]
    assert all("run_tag" in s["message"] for s in flow["steps"][1:])
    assert (srv.state.created, srv.state.deleted) == (created, deleted)


@e2e
def test_manual_login_timeout_blocks_all_steps(srv, tmp_path):
    """Хэрэглэгч хугацаандаа гараар нэвтрээгүй → нэвтрэх болон flow-ын бүх алхам Blocked (аппын bug биш)."""
    human = HumanLogin("admin", PASSWORD, enabled=False)
    data, flow = run_flow(srv, tmp_path, [
        {"uses": "auth.login", "with": {"role": "admin"}},
        {"uses": "crud.open", "with": {"menu": MENU}},
    ], human=human, overrides={"timing": {**FAST["timing"], "manual_login_timeout_ms": 1500, "relogin_timeout_ms": 1500}})
    assert human.calls == 1
    assert not data.login_ok and data.login_timeout and "хугацаа" in data.login_error
    assert data.pipeline[0]["status"] == M.NOT_TESTED and data.pipeline[1]["status"] == M.NOT_TESTED
    assert statuses(flow) == [M.NOT_TESTED, M.NOT_TESTED]
    assert all("Нэвтрэлт амжилтгүй" in s["message"] for s in flow["steps"])


@e2e
def test_tool_error_is_not_tested_not_app_bug(srv, tmp_path):
    """Хэрэгслийн кодын алдаа (TypeError г.м.) аппын bug болохгүй — Blocked «tool». 2026-10-13 HR run-д хуучин
    сервер + шинэ кодын холилдлоос 103 урсгал TypeError-оор Fail болж худал bug үүсгэсэн."""
    from app.flows.engine import step

    @step("test.tool_boom")
    async def boom(env, args):
        raise TypeError("fill_field() got an unexpected keyword argument 'as_date'")

    _, fl = run_flow(srv, tmp_path, [{"uses": "crud.open", "with": {"menu": MENU}},
                                     {"uses": "test.tool_boom"}], name="boom")
    st = fl["steps"][1]
    assert st["status"] == M.NOT_TESTED and st["category"] == "tool", st
    assert "Хэрэгслийн дотоод алдаа" in st["message"] and fl["status"] == M.NOT_TESTED



@e2e
def test_playwright_locator_timeout_is_tool_error(srv, tmp_path):
    """Accounting A123 «Улс»: `TimeoutError: Locator.press: Timeout 30000ms` — хэрэгслийн харилцааны алдаа, bug биш."""
    from app.flows.engine import step

    class TimeoutError(Exception):          # playwright._impl._errors.TimeoutError-тэй ижил нэр
        pass

    @step("test.locator_timeout")
    async def boom(env, args):
        raise TimeoutError('Locator.press: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator("[data-ssg-field=\'1\']")')

    _, fl = run_flow(srv, tmp_path, [{"uses": "test.locator_timeout"}], name="lt")
    st = fl["steps"][0]
    assert st["status"] == M.NOT_TESTED and st["category"] == "tool", st
