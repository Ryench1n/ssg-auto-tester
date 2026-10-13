"""Урсгал бичих (record → replay) — mock CRUD сайт дээр.

1. «Хүн» (Playwright) хөтөч дээр Харилцагч бүртгэж → хайж → сонгож → устгана; хэрэгсэл үйлдлийг бичнэ.
2. Бичлэгээс YAML үүснэ: хэрэглэгчийн бичсэн утгууд {run_tag}-тай, мөр сонголт тэр утгаар.
3. Хадгалаад дахин тоглуулахад (crud алхам) шинэ run_tag-тай бичлэг бүртгэгдэж устгагдана — бодит бичлэгт хүрэхгүй.
4. Аюулгүй байдал: run_tag-гүй (бодит) мөр сонгоод «Устгах» гэвэл зогсооно.
"""
from __future__ import annotations

import asyncio

import pytest
import yaml

from app import config as appcfg
from app import models as M
from app.flows.recorder import build_flow
from app.runner import discovery
from tests.fixtures.mock_crud.server import CrudServer
from tests.test_autocrud import CUSTOMERS, e2e, make_pipe

TYPED = "Бичлэгийн туршилт ХХК"


def test_build_flow_tags_typed_values_and_rows():
    events = [
        {"t": "module", "name": "Мастер дата"},
        {"t": "menu", "name": "Харилцагч", "href": "/md/customers", "folder": "Бүртгэл"},
        {"t": "click", "text": "Нэмэх", "dialog": False},
        {"t": "fill", "field": "Нэр", "value": "Шин"},
        {"t": "fill", "field": "Нэр", "value": TYPED},               # нэг талбар — сүүлчийн утга
        {"t": "fill", "field": "Утас", "value": "99112233", "numeric": True},
        {"t": "fill", "field": "Огноо", "value": "2020-01-01", "date": True},
        {"t": "click", "text": "Хадгалах", "dialog": True},
        {"t": "search", "text": TYPED}, {"t": "search", "text": TYPED},
        {"t": "row", "text": f"C-0002 | {TYPED} | 99112233"},
        {"t": "click", "text": "Устгах", "dialog": False},
        {"t": "click", "text": "Тийм", "dialog": True},
    ]
    flow, warnings = build_flow(events)
    steps = flow["steps"]
    assert [s["uses"] for s in steps] == ["ui.menu", "ui.click", "ui.fill", "ui.fill", "ui.fill", "ui.click",
                                          "ui.search", "ui.row", "ui.click", "ui.click"]
    assert steps[0]["with"]["menu"] == "Мастер дата|Бүртгэл|Харилцагч|/md/customers"
    assert steps[2]["with"] == {"field": "Нэр", "value": "{run_tag} " + TYPED}
    assert steps[3]["with"]["value"] == "99112233"                  # тоо — tag-гүй
    assert steps[4]["with"]["value"] == "2020-01-01"
    assert steps[6]["with"]["text"] == "{run_tag} " + TYPED
    assert steps[7]["with"]["text"] == "{run_tag} " + TYPED        # мөрийг бичсэн утгаар нь олно
    assert not warnings


def test_build_flow_warns_on_untagged_row_and_no_cleanup():
    _, w = build_flow([{"t": "menu", "name": "Х", "href": "/x", "folder": ""},
                       {"t": "row", "text": "C-0001 | Бодит харилцагч ХХК"},
                       {"t": "click", "text": "Хадгалах", "dialog": True}])
    assert any("устгах алхам алга" in x for x in w) and any("бодит бичлэг" in x for x in w)


@pytest.fixture
def recdir(tmp_path, monkeypatch):
    monkeypatch.setattr(appcfg, "RECORDED_DIR", tmp_path / "recorded")
    return tmp_path


async def _human(pipe):
    """Хүний гар үйлдэл (жинхэнэ DOM click/fill — бичигч үүнийг л харна)."""
    page = pipe.page
    from app.runner.capabilities import menu_from_key
    item = menu_from_key(CUSTOMERS)
    assert await discovery.mark_leaf(page, pipe.cfg, item)
    await page.click("[data-ssg-target='1']")
    await page.wait_for_selector("button.add")
    await page.click("button.add")
    await page.fill(".dxbl-modal input[name=name]", TYPED)
    await page.press(".dxbl-modal input[name=name]", "Tab")
    await page.fill(".dxbl-modal input[name=phone]", "99112233")
    await page.press(".dxbl-modal input[name=phone]", "Tab")
    await page.click(".dxbl-modal button.save")
    await page.wait_for_selector(".dxbl-modal", state="detached")
    await page.fill(".dxbl-grid-search-box input", TYPED)
    await page.press(".dxbl-grid-search-box input", "Enter")
    await page.wait_for_selector(f"tr:has-text('{TYPED}')")
    await page.click(f"tr:has-text('{TYPED}') td:nth-child(2)")
    await page.click("button.del")
    await page.click(".dxbl-modal button.yes")
    await asyncio.sleep(0.5)
    pipe.stop_recording()


@e2e
def test_record_save_and_replay(recdir, tmp_path):
    from fastapi.testclient import TestClient

    import app.api as api
    with CrudServer() as srv:
        events: list = []

        def on_event(t, d):
            events.append((t, d))
            if t == "status" and d.get("state") == "recording":
                asyncio.get_running_loop().create_task(_human(holder["pipe"]))

        holder: dict = {}
        pipe, _ = make_pipe(srv, tmp_path / "rec", [{"kind": "record", "flow": "Харилцагч бүртгэх"}], events, on_event)
        holder["pipe"] = pipe
        d = asyncio.run(pipe.run())
        rec = d.recording
        assert rec and rec["steps"] >= 8, rec
        assert any(t == "rec_event" for t, _ in events) and any(t == "rec_done" for t, _ in events)
        spec = yaml.safe_load(rec["yaml"])
        uses = [s["uses"] for s in spec["steps"]]
        assert uses[0] == "ui.menu" and "ui.fill" in uses and "ui.row" in uses
        assert any(s["uses"] == "ui.fill" and s["with"]["value"] == "{run_tag} " + TYPED for s in spec["steps"])
        assert not any("password" in str(s).lower() for s in spec["steps"])
        assert srv.state.created == 1 and srv.state.deleted == 1        # хүний үйлдэл

        # API-аар хадгална (UI-ийн «💾 Хадгалах»)
        c = TestClient(api.app)
        r = c.post("/api/recorded", json={"url": srv.url, "name": "Харилцагч бүртгэх", "yaml": rec["yaml"]})
        assert r.status_code == 400 and "allowed_hosts" in r.text or r.status_code == 200, r.text
        # 127.0.0.1 нь allowed_hosts-д байдаг
        assert r.status_code == 200, r.text
        name = r.json()["name"]
        assert name == "rec: Харилцагч бүртгэх" and r.json()["code"] == "R01"
        flows = c.get("/api/flows", params={"url": srv.url}).json()
        assert any(f["name"] == name and f["recorded"] and f["allowed"] for f in flows)

        # Дахин тоглуулах — шинэ run_tag-тай бичлэг бүртгэгдэж, устгагдана
        before = (srv.state.created, srv.state.deleted)
        pipe2, cfg2 = make_pipe(srv, tmp_path / "play", [{"kind": "crud", "flow": name}], [])
        d2 = asyncio.run(pipe2.run())
        fl = d2.flows[0]
        assert fl["status"] == M.PASS, [(s["name"], s["status"], s["message"]) for s in fl["steps"]]
        assert (srv.state.created - before[0], srv.state.deleted - before[1]) == (1, 1)
        assert srv.state.untagged_touched == 0
        assert any(c["name"] == "Бодит харилцагч ХХК" for c in srv.state.customers)
        assert fl["code"] == "R01"

        # Аюулгүй байдал: бодит (tag-гүй) мөр сонгоод «Устгах» → зогсооно
        bad = spec["steps"][:1] + [{"uses": "ui.search", "with": {"text": "Бодит"}},
                                   {"uses": "ui.row", "with": {"text": "Бодит харилцагч"}},
                                   {"uses": "ui.click", "with": {"button": "Устгах", "scope": "page"}}]
        c.post("/api/recorded", json={"url": srv.url, "name": "муу", "yaml": yaml.safe_dump({"steps": bad})})
        pipe3, _ = make_pipe(srv, tmp_path / "bad", [{"kind": "crud", "flow": "rec: муу"}], [])
        d3 = asyncio.run(pipe3.run())
        st = d3.flows[0]["steps"]
        assert st[-1]["status"] == M.BLOCKED and "бодит" in st[-1]["message"], st[-1]
        assert any(c["name"] == "Бодит харилцагч ХХК" for c in srv.state.customers)
        assert c.delete("/api/recorded", params={"url": srv.url, "name": "rec: муу"}).status_code == 200


def test_record_must_be_only_step(tmp_path):
    from app.config import load_config
    from app.pipeline.runner import PipelineRunner
    from app.pipeline.stages import PipelineConfigError
    from app.runner.smoke import RunOptions
    cfg = load_config("https://testhr.smartapp.mn/")
    with pytest.raises(PipelineConfigError):
        PipelineRunner("x", "https://testhr.smartapp.mn/", cfg, RunOptions(project="X"), tmp_path,
                       [{"kind": "smoke"}, {"kind": "record", "flow": "a"}])
    with pytest.raises(PipelineConfigError, match="allowed_hosts"):
        PipelineRunner("x", "https://hr.smartapp.mn/", load_config("https://hr.smartapp.mn/"),
                       RunOptions(project="X"), tmp_path, [{"kind": "record", "flow": "a"}])


GROUPS = "Мастер дата|Бүртгэл|Бүлэг|/md/groups"


async def _human_lookup(pipe):
    """HR «Тогтмол ажил» шиг: lookup нээгээд мөр ДАРАЛГҮЙ (фокуслагдсан мөрөөр) шууд «Сонгох»; модтой lookup-д
    зангилаа дарж «Сонгох»."""
    page = pipe.page
    from app.runner.capabilities import menu_from_key
    assert await discovery.mark_leaf(page, pipe.cfg, menu_from_key(GROUPS))
    await page.click("[data-ssg-target='1']")
    await page.click("button.add")
    await page.wait_for_selector("input[name=gname]")
    await page.fill("input[name=gname]", "Бичсэн бүлэг")
    await page.press("input[name=gname]", "Tab")
    await page.click("button.lkbtn")
    await page.wait_for_selector(".dxbl-modal .lk-select")
    await asyncio.sleep(1.2)                                    # lookup ачаалагдана (фокуслагдсан мөртэй)
    await page.click(".dxbl-modal .lk-select")                 # мөр дараагүй
    await page.wait_for_selector(".dxbl-modal", state="detached")
    await page.click("button.lkbtn2")
    await page.click(".dxbl-modal tr:has-text('Бараа')")
    await page.click(".dxbl-modal .lk-select")
    await page.wait_for_selector(".dxbl-modal", state="detached")
    await page.click("button.gsave")
    await asyncio.sleep(0.6)
    pipe.stop_recording()


@e2e
def test_record_lookup_without_row_click_and_tree(recdir, tmp_path):
    with CrudServer() as srv:
        holder: dict = {}

        def on_event(t, d):
            if t == "status" and d.get("state") == "recording":
                asyncio.get_running_loop().create_task(_human_lookup(holder["pipe"]))

        pipe, _ = make_pipe(srv, tmp_path / "rec", [{"kind": "record", "flow": "Бүлэг"}], [], on_event)
        holder["pipe"] = pipe
        d = asyncio.run(pipe.run())
        spec = yaml.safe_load(d.recording["yaml"])
        picks = [s["with"] for s in spec["steps"] if s["uses"] == "ui.pick"]
        assert [p["field"] for p in picks] == ["Эцэг бүлэг", "Бүлгийн төрөл"], spec["steps"]
        assert picks[0]["row"] == "G01 Үндсэн бүлэг" and "Бараа" in picks[1]["row"], picks
        assert not any(s["uses"] == "ui.click" and s["with"]["button"] == "Сонгох" for s in spec["steps"])
        appcfg.save_recorded("127.0.0.1", {"rec: Бүлэг": {**spec, "code": "R01"}})
        pipe2, _ = make_pipe(srv, tmp_path / "play", [{"kind": "crud", "flow": "rec: Бүлэг"}], [])
        d2 = asyncio.run(pipe2.run())
        fl = d2.flows[0]
        assert fl["status"] == M.PASS, [(s["name"], s["status"], s["message"]) for s in fl["steps"]]



def test_short_typed_code_does_not_tag_lookup_row():
    """HR «Тогтмол ажил»: Код=1 бичээд lookup-оос «1 | Бүх» сонгосон → lookup-ын мөр `{run_tag}1` болж
    тоглуулахад олдохгүй байсан. Богино утга бодит өгөгдөлтэй давхцахгүй, lookup-ын мөр бүх нүдээр хайгдана."""
    flow, _ = build_flow([
        {"t": "menu", "name": "Тогтмол ажил", "href": "/x", "folder": ""},
        {"t": "click", "text": "Шинэ"},
        {"t": "fill", "field": "Код", "value": "1", "numeric": True},
        {"t": "fill", "field": "Нэр", "value": "one"},
        {"t": "pick", "field": "Харъяалагдах бүлэг", "row": "1 | Бүх"},
    ])
    st = {s["uses"] + ":" + str(s["with"].get("field")): s["with"] for s in flow["steps"]}
    assert st["ui.fill:Код"]["value"] == "{run_tag}1"
    assert st["ui.pick:Харъяалагдах бүлэг"]["row"] == "1 Бүх"


JOBS = "Мастер дата|Бүртгэл|Ажил|/md/jobs"


async def _human_jobs(pipe):
    """Хүнд тохиолдлууд: icon-оос өөр текстгүй товч, календарь, цонхны жагсаалтын мөр, Enter-ээр хадгалах,
    хайсны дараа мөр дарахгүй устгах (фокус), мөрийн checkbox, toolbar холбоос."""
    page = pipe.page
    from app.runner.capabilities import menu_from_key
    assert await discovery.mark_leaf(page, pipe.cfg, menu_from_key(JOBS))
    await page.click("[data-ssg-target='1']")
    await page.click("button.jadd")
    await page.click(".dxbl-modal button.cal")
    await page.click(".dxbl-calendar td[data-d='15']")
    await page.click(".dxbl-modal tbody tr:has-text('Дорж')")
    await page.click(".dxbl-modal input[name=jname]")
    await page.keyboard.type("Шинэ ажил")
    await page.keyboard.press("Enter")                       # blur-гүй, Enter-ээр хадгална
    await page.wait_for_selector(".dxbl-modal", state="detached")
    await page.fill(".dxbl-grid-search-box input", "Шинэ ажил")
    await page.press(".dxbl-grid-search-box input", "Enter")
    await page.wait_for_selector("tr.dxbl-grid-focused-row:has-text('Шинэ ажил')")
    await page.click("tr:has-text('Шинэ ажил') input[type=checkbox]")
    await page.click("button.jdel")                          # мөр дараагүй — фокус дээр
    await page.click(".dxbl-modal button.yes")
    await page.click("a.jref")
    await asyncio.sleep(0.5)
    pipe.stop_recording()


@e2e
def test_record_replay_rich_cases(recdir, tmp_path):
    with CrudServer(rich=True) as srv:
        holder: dict = {}

        def on_event(t, d):
            if t == "status" and d.get("state") == "recording":
                asyncio.get_running_loop().create_task(_human_jobs(holder["pipe"]))

        pipe, _ = make_pipe(srv, tmp_path / "rec", [{"kind": "record", "flow": "Ажил"}], [], on_event)
        holder["pipe"] = pipe
        d = asyncio.run(pipe.run())
        assert (srv.state.jobs_created, srv.state.jobs_deleted) == (1, 1)
        spec = yaml.safe_load(d.recording["yaml"])
        uses = [(s["uses"], s["with"]) for s in spec["steps"]]
        print(*uses, sep="\n")
        assert ("ui.click", {"icon": "fa-plus-circle", "scope": "page"}) in uses
        assert ("ui.fill", {"field": "Огноо", "value": "2026-10-15"}) in uses
        assert ("ui.row", {"text": "Дорж", "open": False, "dialog": True}) in uses
        assert ("ui.fill", {"field": "Нэр", "value": "{run_tag} Шинэ ажил"}) in uses
        assert ("ui.key", {"field": "Нэр", "key": "Enter"}) in uses
        assert ("ui.search", {"text": "{run_tag} Шинэ ажил"}) in uses
        i = [u for u, _ in uses].index("ui.click", [u for u, _ in uses].index("ui.search"))
        assert uses[i - 1][0] == "ui.row" and uses[i - 1][1]["text"] == "{run_tag} Шинэ ажил"   # фокус → мөр сонгох
        assert uses[i][1] == {"icon": "fa-trash", "scope": "page"}
        assert ("ui.click", {"button": "Сэргээх", "scope": "page", "any": True}) in uses
        assert not any(u == "ui.row" and w.get("text") in ("13", "14", "15") for u, w in uses)   # календарь мөр биш

        appcfg.save_recorded("127.0.0.1", {"rec: Ажил": {**spec, "code": "R01"}})
        pipe2, _ = make_pipe(srv, tmp_path / "play", [{"kind": "crud", "flow": "rec: Ажил"}], [])
        d2 = asyncio.run(pipe2.run())
        fl = d2.flows[0]
        assert fl["status"] == M.PASS, [(s["name"], s["status"], s["message"]) for s in fl["steps"]]
        assert (srv.state.jobs_created, srv.state.jobs_deleted) == (2, 2)      # тоглуулалт бүртгэж, устгасан
