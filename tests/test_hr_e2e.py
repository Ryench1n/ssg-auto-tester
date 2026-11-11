"""testhr «H1: Ажилтан авах → Гэрээ → Цалин» сценарийн бүтэц ба түүнд нэмсэн хэрэгслийн боломжууд.

Бодит testhr дээрх ажиллагааг шалгахгүй (гар нэвтрэлт шаардлагатай) — сценари зөв угсрагдсан эсэх, цэвэрлэгээ
бодит өгөгдөлд хүрэхгүйг шалгана."""
import asyncio
import datetime
import re
from types import SimpleNamespace

import pytest

from app import models as M
from app.config import load_config
from app.flows.context import FlowContext, placeholders
from app.flows.engine import STEPS
from app.flows.steps import auto_checks
from app.flows.steps.auto import field_aliases, without_key
from app.flows.steps.business import count_rows
from app.pipeline.runner import PipelineRunner
from app.runner.smoke import RunOptions
from tests.fixtures.mock_crud.server import PASSWORD, CrudServer
from tests.human_login import HumanLogin
from tests.test_flows import FAST, MENU, e2e

FLOW = "H1: Ажилтан авах → Гэрээ → Цалин"


@pytest.fixture(scope="module")
def steps():
    return load_config("https://testhr.smartapp.mn/")["flows"][FLOW]["steps"]


def test_scenario_is_e2e_and_uses_known_steps(steps):
    fl = load_config("https://testhr.smartapp.mn/")["flows"][FLOW]
    assert fl["type"] == "e2e" and fl["code"] == "H1"
    assert {s["uses"] for s in steps} <= set(STEPS)


def test_every_placeholder_comes_from_earlier_step(steps):
    have = {"today"}
    for s in steps:
        w = s["with"]
        missing = placeholders(w) - have
        assert not missing, (s["name"], missing)
        have |= {w[k] for k in ("save_as", "field_as", "try_as") if k in w}


def test_each_delta_reads_snapshots_taken_before(steps):
    seen: set[str] = set()
    for s in steps:
        w = s["with"]
        if s["uses"] == "assert.delta":
            assert {w["before"], w["after"]} <= seen, s["name"]
        elif s["uses"] == "report.read":
            seen.add(w["save_as"])


def test_cleanup_always_runs_in_reverse_order(steps):
    cleanup = [s for s in steps if s["uses"] == "crud.auto_cleanup"]
    assert [s["with"]["data"] for s in cleanup] == ["h05", "h04", "h03", "h02", "h01", "h06"]
    i = steps.index(cleanup[0])
    assert all(s.get("always") for s in steps[i:]), "цэвэрлэгээний дараах алхам бүр always байх ёстой"


def test_only_tagged_records_are_targeted(steps):
    """Устгах, сонгох утга бүр run_tag-тай — бодит ажилтан, хэлтэст хүрэхгүй."""
    for s in steps:
        w = s["with"]
        if "find" in w:
            assert "{run_tag}" in w["find"], s["name"]
        for v in (w.get("prefer") or {}).values():
            assert "{run_tag}" in v, s["name"]


def test_count_rows():
    rows = [["SSG103", "Бат"], ["SSG103", "Дорж"], ["OTHER", "Сүрэн"]]
    assert count_rows(rows, "SSG103") == 2
    assert count_rows(rows, "SSG999") == 0
    assert count_rows([], "SSG103") == 0


def test_without_key_writes_no_run_tag_to_form():
    """Систем олгосон дугаарт текст нэмэгдэж «ЦБ123002SSG…», дүнд текст бичигдэж байсан (testhr, 2026-11-05)."""
    values, key = without_key({"Үндсэн цалин": "TAG тест", "Код": "1"}, "Үндсэн цалин")
    assert key == "" and values == {"Код": "1"}


def test_system_numbered_documents_are_not_keyed(steps):
    for code in ("h04_rec", "h05_rec"):
        w = next(s for s in steps if s["with"].get("save_as") == code)["with"]
        assert w["record"] == "{run_tag}03" and "key" not in w and "Баримтын №" not in (w.get("values") or {})


def test_tax_number_alias_configured():
    cfg = load_config("https://testhr.smartapp.mn/")["flow"]["auto"]
    assert "Татвар төлөгчийн №" in field_aliases(cfg, "ТТД")
    assert field_aliases(cfg, "Банк") == []


def _cleanup_env(found: bool = True):
    calls = {"find": [], "delete": []}
    ctx = FlowContext(run_tag="SSG1")
    env = SimpleNamespace(ctx=ctx, run=SimpleNamespace(meta={}, steps=[]), note=lambda m: None)

    async def noop(*a, **k):
        return None

    async def grid_find(e, text, tries=2):
        calls["find"].append(text)
        return found

    ui = SimpleNamespace(close_form=noop, clear_toasts=noop, ensure_menu=noop, grid_find_settled=grid_find)
    return env, ui, calls


def test_cleanup_find_uses_given_column(monkeypatch):
    """Цалин, гэрээний жагсаалтад баримтын дугаарын багана алга — ажилтны кодоор олж устгана."""
    env, ui, calls = _cleanup_env()
    env.ctx.data.update(h05_rec="SSG1 H05 автомат тест", h05_try="SSG1 H05 автомат тест")

    async def delete(e, args):
        calls["delete"].append(args["find"])
        return {}

    monkeypatch.setattr(auto_checks, "ui", ui)
    monkeypatch.setattr("app.flows.steps.crud.crud_delete", delete)
    asyncio.run(auto_checks.auto_cleanup(env, {"menu": "m", "data": "h05", "find": "SSG103"}))
    assert calls["find"] == ["SSG103"] and calls["delete"] == ["SSG103"]


def test_cleanup_find_skips_when_step_never_reached(monkeypatch):
    """Бүртгэл явагдаагүй бол ажилтны кодоор хайж өөр бичлэг устгахгүй."""
    from app.flows.context import StepBlocked
    env, ui, calls = _cleanup_env()
    monkeypatch.setattr(auto_checks, "ui", ui)
    with pytest.raises(StepBlocked):
        asyncio.run(auto_checks.auto_cleanup(env, {"menu": "m", "data": "h05", "find": "SSG103"}))
    assert calls["find"] == []


@e2e
def test_row_count_delta_and_cleanup_by_find_in_browser(tmp_path):
    """Бодит хөтөч дээр: «agg: count» жагсаалтаас мөрийн тоог уншиж (0 мөртэй үед ч алдаагүй), Δ +1, `find`-аар
    цэвэрлэсний дараа Δ = 0."""
    def count(save_as, always=False):
        return {"uses": "report.read", "name": f"Тоо ({save_as})", "always": always,
                "with": {"menu": MENU, "search": True, "save_as": save_as,
                         "rows": {"n": {"match": "{run_tag} Тест", "column": "Нэр", "agg": "count"}}}}
    steps = [
        count("n0"),
        {"uses": "crud.create", "name": "Бүртгэх",
         "with": {"menu": MENU, "save_as": "c1_rec", "fields": {"Нэр": "{run_tag} Тест", "Регистр": "7654321"}}},
        count("n1"),
        {"uses": "assert.delta", "name": "Тоо +1", "with": {"before": "n0", "after": "n1", "expect": {"n": 1}}},
        {"uses": "crud.auto_cleanup", "name": "Цэвэрлэх", "always": True,
         "with": {"menu": MENU, "data": "c1", "find": "{run_tag} Тест"}},
        count("n2", always=True),
        {"uses": "assert.delta", "name": "Тоо Δ = 0", "always": True,
         "with": {"before": "n0", "after": "n2", "expect": {"n": 0}}},
    ]
    with CrudServer() as srv:
        cfg = load_config(srv.url, {**FAST, "flows": {"h": {"type": "e2e", "steps": steps}}})
        pipe = PipelineRunner("h", srv.url, cfg, RunOptions(project="T"), tmp_path,
                              steps=[{"kind": "crud", "flow": "h"}], headless=True,
                              on_waiting_login=HumanLogin("admin", PASSWORD))
        data = asyncio.run(pipe.run())
    fl = data.flows[0]
    assert fl["status"] == M.PASS, [(s["index"], s["name"], s["status"], s["message"][:160]) for s in fl["steps"]]
    assert (fl["data"]["n0"]["n"], fl["data"]["n1"]["n"], fl["data"]["n2"]["n"]) == (0.0, 1.0, 0.0)


def test_run_reg_encodes_valid_past_birth_date():
    """Эхний 6 цифрээс систем төрсөн огноог тооцдог — ирээдүйн/хүчингүй огноо гарвал ажилтан хадгалагдахгүй."""
    seen = set()
    for i in range(2000):
        tag = f"SSG1105{i:04X}"
        v = FlowContext(run_tag=tag).render("SS{run_reg}")
        assert re.fullmatch(r"SS\d{8}", v), v
        yy, mm, dd = int(v[2:4]), int(v[4:6]), int(v[6:8])
        assert datetime.date(1900 + yy, mm, dd) < datetime.date.today(), v
        seen.add(v)
    assert len(seen) > 1900                      # run бүрт давхцах магадлал бага
    assert not placeholders("SS{run_reg}")


def test_employee_register_is_unique_per_run(steps):
    emp = next(s for s in steps if s["with"].get("save_as") == "h03_rec")
    assert emp["with"]["values"]["Регистрийн №"] == "SS{run_reg}"


def test_contract_uses_test_contract_type(steps):
    i = next(n for n, s in enumerate(steps) if s["with"].get("save_as") == "h06_rec")
    j = next(n for n, s in enumerate(steps) if s["with"].get("save_as") == "h04_rec")
    assert i < j and steps[j]["with"]["prefer"]["Гэрээний төрөл"] == "{run_tag}06"


def test_cleanup_deltas_name_their_cleanup(steps):
    """Δ = 0 шалгалт бүр аль цэвэрлэгээнээс хамаарахаа заана — бүтэлгүйтсэн цэвэрлэгээ худал bug үүсгэхгүй."""
    deltas = [s["with"] for s in steps if s["uses"] == "assert.delta" and s.get("always")]
    assert [d["cleanup_of"] for d in deltas] == ["h05", "h04", "h03"]


def test_delta_skipped_after_failed_cleanup():
    from app.flows.context import StepBlocked
    from app.flows.steps.business import assert_delta
    ctx = FlowContext(run_tag="SSG1")
    ctx.data.update(s0={"salary": 0.0}, s2={"salary": 1500000.0}, h05_left=True)
    env = SimpleNamespace(ctx=ctx, note=lambda m: None)
    args = {"before": "s0", "after": "s2", "expect": {"salary": 0}, "cleanup_of": "h05"}
    with pytest.raises(StepBlocked, match="Цэвэрлэгээ бүтэлгүйтсэн"):
        asyncio.run(assert_delta(env, args))


@e2e
def test_grouped_grid_header_is_not_selected():
    """Бүлэглэсэн grid: хайлт толгой мөрийг бус өгөгдлийн мөрийг олно (testhr «Цалингийн жагсаалт» цэвэрлэгээ)."""
    from playwright.async_api import async_playwright

    from app.flows.ui._js import FIND_ROWS_JS
    from app.flows.ui.base import sel_payload
    from app.runner.browser import launch
    html = """<table class="dxbl-grid"><tbody>
      <tr role="row" class="dxbl-grid-group-row"><td colspan="3">Ажилтан: SSG103 - Тест ажилтан</td></tr>
      <tr role="row"><td>SSG10148683</td><td>SSG103</td><td>1,500,000</td></tr>
      <tr role="row"><td colspan="3">Бүлэг: OTHER</td></tr></tbody></table>"""
    env = SimpleNamespace(cfg=load_config("https://testhr.smartapp.mn/"))

    async def run():
        async with async_playwright() as pw:
            b = await launch(pw, True)
            page = await b.new_page()
            await page.set_content(html)
            rows = await page.evaluate(FIND_ROWS_JS, [sel_payload(env), "SSG103"])
            only = await page.evaluate(FIND_ROWS_JS, [sel_payload(env), "OTHER"])
            await b.close()
            return rows, only
    rows, only = asyncio.run(run())
    assert rows == ["SSG10148683 SSG103 1,500,000"]
    assert only == ["Бүлэг: OTHER"]                 # өгөгдлийн мөргүй бол толгой хэвээр (өмнөх зан төлөв)


def test_gateway_outage_is_not_an_app_bug():
    """Cloudflare 502 цэвэрлэгээний үеэр — Major bug биш, орчны саад (testhr H1, 2026-11-09). HTTP 500 bug хэвээр."""
    from app import models as Mo
    from app.flows.engine import split_outage
    from app.models import Finding
    f502 = Finding(Mo.HTTP_5XX, "HTTP 502", status=502, url="https://x/api/employee")
    f500 = Finding(Mo.HTTP_5XX, "HTTP 500", status=500, url="https://x/api/employee")
    crash = Finding(Mo.RUNTIME_CRASH, "Unhandled Exception:")
    assert split_outage([f502]) == ([], [f502])
    assert split_outage([f500, f502]) == ([f500], [f502])
    assert split_outage([crash]) == ([crash], [])


def test_cleanup_is_retried_after_transient_failure():
    from app.flows.engine import RETRYABLE_STEPS
    assert "crud.auto_cleanup" in RETRYABLE_STEPS
    assert "crud.auto_create" not in RETRYABLE_STEPS          # өгөгдөл үүсгэх алхмыг давтахгүй


def test_leftovers_accumulate_across_cleanups():
    """E2E-ийн олон цэвэрлэгээ: үлдэгдэл нэгтгэгдэнэ, дахин оролдлого амжилттай бол тухайн алхмынх арилна."""
    from app.flows.steps.auto_checks import _set_leftover
    env = SimpleNamespace(run=SimpleNamespace(meta={}), ctx=FlowContext(run_tag="SSG1"))
    _set_leftover(env, "h05", ["{run_tag}03: дуусаагүй"], ["SSG103"])
    _set_leftover(env, "h03", ["{run_tag} H03: FK"], ["SSG1 H03"])
    assert env.run.meta["leftover"] == ["{run_tag}03: дуусаагүй", "{run_tag} H03: FK"]
    assert env.run.meta["leftover_values"] == ["SSG103", "SSG1 H03"]
    assert env.ctx.data["h05_left"] and env.ctx.data["h03_left"]
    _set_leftover(env, "h05", [], [])                     # дахин оролдлого амжилттай
    assert env.run.meta["leftover"] == ["{run_tag} H03: FK"]
    assert "h05_left" not in env.ctx.data
    _set_leftover(env, "h03", [], [])
    assert "leftover" not in env.run.meta and "leftover_values" not in env.run.meta


def test_smartapp_wait_overlay_counts_as_loading():
    from app.config import load_config
    cfg = load_config("https://testhr.smartapp.mn/")
    assert "Түр хүлээнэ үү..." in cfg["selectors"]["loading_texts"]



@e2e
def test_wait_overlay_outside_dialog_is_loading():
    """«Түр хүлээнэ үү…» overlay lookup цонхны гадна — хуудас ачаалж байна гэж танина (testhr 2026-11-09)."""
    from playwright.async_api import async_playwright

    from app.flows.ui.lookup import _page_loading
    from app.runner.browser import launch
    cfg = load_config("https://testhr.smartapp.mn/")
    html = """<div role="dialog"><table><tr><td>Бичлэг олдсонгүй</td></tr></table></div>
      <div id="ov"><span>Түр хүлээнэ үү...</span></div>"""

    async def run():
        async with async_playwright() as pw:
            b = await launch(pw, True)
            page = await b.new_page()
            await page.set_content(html)
            env = SimpleNamespace(page=page, cfg=cfg)
            busy = await _page_loading(env)
            await page.evaluate("document.getElementById('ov').remove()")
            idle = await _page_loading(env)
            await b.close()
            return busy, idle
    assert asyncio.run(run()) == (True, False)


@e2e
def test_lookup_waits_for_page_overlay(tmp_path):
    """Lookup цонх «Бичлэг олдсонгүй» + хуудасны «Түр хүлээнэ үү…» — ачаалал дуустал хүлээж, мөрийг олно
    (testhr «Даатгуулагчийн төрөл», 2026-11-09; 2026-11-11 run-д энэ давталт TypeError өгсөн)."""
    from playwright.async_api import async_playwright

    from app.flows.ui.lookup import pick_from_lookup
    from app.runner.browser import launch
    cfg = load_config("https://testhr.smartapp.mn/")
    (tmp_path / "shots").mkdir()
    html = """<button data-ssg-lookup="1" onclick="openLk()">...</button>
      <script>
      function openLk() {
        const d = document.createElement('div'); d.setAttribute('role', 'dialog');
        d.innerHTML = '<table class="dxbl-grid"><tbody><tr role="row"><td>Бичлэг олдсонгүй</td></tr></tbody></table>';
        document.body.appendChild(d);
        const ov = document.createElement('div'); ov.innerHTML = '<span>Түр хүлээнэ үү...</span>';
        document.body.appendChild(ov);
        setTimeout(() => { ov.remove();
          d.innerHTML = '<table class="dxbl-grid"><tbody><tr role="row"><td>01001 Үндсэн ажилтан</td></tr></tbody></table>';
        }, 2500);
      }
      </script>"""

    trace = []

    async def run():
        async with async_playwright() as pw:
            b = await launch(pw, True)
            page = await b.new_page()
            await page.set_content(html)
            env = SimpleNamespace(page=page, cfg=cfg, run_dir=tmp_path, flow_name="H1", note=lambda m: None,
                                  runlog=SimpleNamespace(log=lambda *a, **k: trace.append(a[0])))
            got = await pick_from_lookup(env, "Даатгуулагчийн төрөл", pick=False)
            empty = env._lookup_empty
            await b.close()
            return got, empty
    got, empty = asyncio.run(run())
    assert "01001" in got and not empty, trace


def test_add_button_waits_for_late_toolbar(monkeypatch):
    """Хэрэгслийн самбар хожуу зурагдана — «Нэмэх товч олдсонгүй» гэж шууд дүгнэхгүй (testhr 2026-11-11)."""
    from app.flows import ui
    from app.flows.steps import auto
    calls = []

    async def late(env, texts):
        calls.append(1)
        return len(calls) >= 3
    monkeypatch.setattr(ui, "has_button", late)
    assert asyncio.run(auto.wait_button(None, ["Шинэ"], wait_s=5))
    calls.clear()

    async def never(env, texts):
        return False
    monkeypatch.setattr(ui, "has_button", never)
    assert not asyncio.run(auto.wait_button(None, ["Шинэ"], wait_s=0.6))
