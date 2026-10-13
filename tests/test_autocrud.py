"""Цэсний үйлдлийн бүртгэл + автомат CRUD (mock CRUD сайт).

  Харилцагч — Нэмэх/Засах/Устгах + энгийн форм → автомат CRUD бүрэн Pass
  Бараа     — CRUD товчгүй → crud=none (нэр дэвшихгүй)
  Ангилал   — заавал огноо (DateEdit) бөглөнө; lookup нээхэд системийн алдаа → Fail (bug)
  Бүлэг     — «Шинэ» формыг ШИНЭ ТАБ дээр нээнэ, FormLayout caption-гүй (<span> шошго) → Pass
  Гүйлгээ   — «Шинэ» нь төрөл сонгох dropdown → алгасна (сонголт хийхгүй)
"""
import asyncio
import datetime
import os

import pytest
import yaml

from app import models as M
from app.config import load_config
from app.flows.steps.auto import plan_values
from app.pipeline.runner import PipelineRunner
from app.pipeline.stages import PipelineConfigError
from app.report.build import generate_reports
from app.runner.capabilities import classify
from app.runner.smoke import RunOptions
from app.store import Store
from tests.conftest import login_client
from tests.fixtures.mock_crud.server import PASSWORD, CrudServer
from tests.human_login import HumanLogin

e2e = pytest.mark.skipif(os.environ.get("SSG_SKIP_E2E") == "1", reason="SSG_SKIP_E2E=1")

FAST = {"timing": {"app_ready_timeout_ms": 10000, "login_timeout_ms": 10000, "loading_blocked_ms": 6000,
                   "wait_ms": 1200, "post_close_wait_ms": 500, "item_timeout_s": 25},
        # lookup_repeat_per_module 0: хуучин тестүүд форм бүрийн lookup-ийг шалгадаг гэж үздэг (кэшийн тест тусдаа)
        "flow": {"step_timeout_s": 40, "settle_ms": 250, "save_timeout_ms": 6000, "lookup_repeat_per_module": 0},
        "confirm_failures": False}


def key_of(module, folder, name, href):
    return f"{module}|{folder}|{name}|{href}"


CUSTOMERS = key_of("Мастер дата", "Бүртгэл", "Харилцагч", "/md/customers")
CATS = key_of("Мастер дата", "Бүртгэл", "Ангилал", "/md/cats")
ITEMS = key_of("Мастер дата", "Бүртгэл", "Бараа", "/md/items")
GROUPS = key_of("Мастер дата", "Бүртгэл", "Бүлэг", "/md/groups")
TXN = key_of("Мастер дата", "Бүртгэл", "Гүйлгээ", "/md/txn")
JOURNAL = key_of("Мастер дата", "Бүртгэл", "Журнал", "/md/journal")
ACCT = key_of("Мастер дата", "Бүртгэл", "Данс", "/md/accounts")


@pytest.fixture(scope="module")
def srv():
    with CrudServer() as s:
        yield s


def make_pipe(srv, rd, steps, events, on_event=None, extra=None):
    from app.config import deep_merge
    cfg = load_config(srv.url, deep_merge(FAST, extra or {}))
    pipe = PipelineRunner("ac", srv.url, cfg, RunOptions(project="Auto"), rd, steps,
                          emit=on_event or (lambda t, d: events.append((t, d))), headless=True,
                          on_waiting_login=HumanLogin("admin", PASSWORD))
    return pipe, cfg


# ---------------------------------------------------------------- browser-гүй
def test_classify_buttons():
    cfg = load_config("http://127.0.0.1:1/")
    b = lambda *ts: {"buttons": [{"text": t, "disabled": False} for t in ts]}
    assert classify(b("Нэмэх", "Засах", "Устгах"), cfg)["crud"] == "full"
    assert classify(b("Нэмэх", "Засах"), cfg)["crud"] == "partial"      # Устгахгүй → автомат CRUD хийхгүй
    assert classify(b("Экспорт", "Хэвлэх"), cfg)["crud"] == "none"
    assert classify(b("Нэмэх", "Устгах", "Батлах"), cfg)["process"] == ["Батлах"]


def test_plan_values():
    f = lambda label, **kw: {"label": label, "tag": "input", "type": "", "combo": False, "readonly": False,
                             "required": False, "value": "", **kw}
    vals, key, picks, unresolved = plan_values(
        [f("Код", readonly=True, value="C-1"), f("Нэр"), f("Утас"), f("Имэйл", required=True),
         f("Тоо ширхэг", required=True), f("Төрөл", tag="select", required=True),
         f("Дансны төрөл", required=True, lookup=True), f("Валют", required=True, combo=True),
         f("Огноо", date=True, required=True), f("Зөвшөөрсөн", type="checkbox", required=True),
         f("Бүлэг", lookup=True)], "SSG01", {})
    assert key == "Нэр" and vals["Нэр"] == "SSG01 автомат тест"
    assert "Утас" not in vals                      # заавал биш талбарыг хөндөхгүй
    assert vals["Имэйл"] == "ssg01@example.com" and vals["Тоо ширхэг"] == "1"
    assert picks == ["Төрөл", "Дансны төрөл", "Валют"]      # lookup-д текст бичихгүй — сонгоно
    assert "Дансны төрөл" not in vals and unresolved == ["Зөвшөөрсөн"]
    assert vals["Огноо"] == datetime.date.today().isoformat()   # заавал огноо → өнөөдөр
    # Огнооны формат формын бусад огнооноос (ж: «Баримтын огноо 30.09.2026»)
    vals, _, _, _ = plan_values([f("Нэр", required=True), f("Баримтын огноо", date=True, value="30.09.2026"),
                                 f("Дуусах огноо", date=True, required=True)], "T", {})
    assert vals["Дуусах огноо"] == datetime.date.today().strftime("%d.%m.%Y")
    vals, key, _, _ = plan_values([f("Код"), f("Тайлбар")], "T", {})
    assert key == "Тайлбар" and "Код" not in vals
    _, key, _, _ = plan_values([f("Нэр", lookup=True), f("Код")], "T", {})
    assert key == "Код"                            # lookup талбар түлхүүр болохгүй
    # Урсгал бүрийн давтагдашгүй утга (нэг хүснэгтийн өөр харагдац нэг run-д давхцахгүй)
    vals, _, _, _ = plan_values([f("Код", required=True), f("Нэр", required=True)], "SSG01", {}, "A06")
    assert vals == {"Код": "SSG0106", "Нэр": "SSG01 A06 автомат тест"}


def test_validation_targets():
    from app.flows.ui import validation_targets
    t = ["Анхаар", "Анхааруулга"]
    assert validation_targets("Давтамжит гүйлгээ\nНийт дүн оруулна уу!", t) == ["Нийт дүн"]
    assert validation_targets("Анхаар\nДанс оруулна уу !", t) == ["Данс"]
    assert validation_targets("Анхаар\nТөрөл сонгоно уу", t) == ["Төрөл"]
    assert validation_targets("Анхааруулга\nХадгалах мэдээлэл дутуу байна.", t) == []
    assert validation_targets("Анхаар\nАжил мэргэжлийн ангилал хоосон байх боломжгүй!", t) == ["Ажил мэргэжлийн ангилал"]


def test_autocrud_after_smoke_requires_smoke(tmp_path):
    cfg = load_config("http://127.0.0.1:1/")
    with pytest.raises(PipelineConfigError, match="smoke"):
        PipelineRunner("x", "http://127.0.0.1:1/", cfg, RunOptions(project="X"), tmp_path,
                       [{"kind": "autocrud", "menus": None}])
    PipelineRunner("x", "http://127.0.0.1:1/", cfg, RunOptions(project="X"), tmp_path,
                   [{"kind": "autocrud", "menus": [CUSTOMERS]}])            # урьдчилан сонгосон → OK


def test_autocrud_rejected_on_non_test_host(tmp_path):
    cfg = load_config("https://accounting.smartapp.mn/")
    with pytest.raises(PipelineConfigError, match="allowed_hosts"):
        PipelineRunner("x", "https://accounting.smartapp.mn/", cfg, RunOptions(project="X"), tmp_path,
                       [{"kind": "autocrud", "menus": [CUSTOMERS]}])


def test_store_caps_catalog(tmp_path):
    s = Store(tmp_path / "runs.db")
    s.save_caps("h", "r1", "v1", [{"key": "a", "module": "M", "folder": "F", "name": "A", "caps": {"crud": "full"}},
                                  {"key": "b", "module": "M", "folder": "F", "name": "B", "caps": {"crud": "none"}},
                                  {"key": "c", "module": "M", "folder": "F", "name": "C", "caps": {}}])
    assert [r["key"] for r in s.list_caps("h", "full")] == ["a"]
    s.save_caps("h", "r2", "v2", [{"key": "a", "module": "M", "folder": "F", "name": "A", "caps": {"crud": "partial"}}])
    assert s.list_caps("h", "full") == [] and len(s.list_caps("h")) == 2 and s.list_caps("x") == []


def test_api_menus_catalog_and_crud_select(tmp_path, monkeypatch):
    import app.api as api
    monkeypatch.setattr(api, "store", Store(tmp_path / "runs.db"))
    api.store.save_caps("testaccounting.smartapp.mn", "r1", "v26", [
        {"key": CUSTOMERS, "module": "Мастер дата", "folder": "Бүртгэл", "name": "Харилцагч",
         "caps": {"crud": "full", "edit": True, "buttons": [{"text": "Нэмэх"}, {"text": "Устгах"}], "columns": ["Нэр"]}},
        {"key": ITEMS, "module": "Мастер дата", "folder": "Бүртгэл", "name": "Бараа", "caps": {"crud": "none"}}])
    c = login_client(api, api.store)
    r = c.get("/api/menus", params={"url": "https://testaccounting.smartapp.mn/"}).json()
    assert r["allowed"] and [m["key"] for m in r["menus"]] == [CUSTOMERS]
    m = r["menus"][0]
    assert m["path"] == "Мастер дата / Бүртгэл / Харилцагч" and m["buttons"] == ["Нэмэх", "Устгах"]
    assert m["app_version"] == "v26" and m["edit"]
    assert len(c.get("/api/menus", params={"url": "https://testaccounting.smartapp.mn/", "crud": ""}).json()["menus"]) == 2
    assert c.post("/api/runs/nope/crud-select", json={"menus": []}).status_code == 404
    bad = c.post("/api/runs", json={"url": "https://testaccounting.smartapp.mn/",
                                    "steps": [{"kind": "autocrud", "menus": ["буруу"]}]})
    assert bad.status_code == 400
    no_smoke = c.post("/api/runs", json={"url": "https://testaccounting.smartapp.mn/",
                                         "steps": [{"kind": "autocrud", "menus": None}]})
    assert no_smoke.status_code == 400 and "smoke" in no_smoke.json()["detail"]


# ---------------------------------------------------------------- smoke → сонголт → автомат CRUD
@pytest.fixture(scope="module")
def after_smoke(srv, tmp_path_factory):
    rd = tmp_path_factory.mktemp("ac")
    events: list = []
    before = (srv.state.created, srv.state.deleted, srv.state.updated)
    chosen: list = []
    holder: dict = {}

    def on_event(t, d):
        events.append((t, d))
        if t == "crud_candidates":
            # UI-ийн хэрэглэгч: Харилцагч + Ангилал + (нэр дэвшээгүй) Бараа-г сонгоно
            chosen.extend(m["key"] for m in d["menus"])
            asyncio.get_running_loop().call_soon(holder["pipe"].select_crud,
                                                 [CUSTOMERS, CATS, GROUPS, TXN, JOURNAL, ACCT, ITEMS])

    pipe, cfg = make_pipe(srv, rd, [{"kind": "smoke"}, {"kind": "autocrud", "menus": None}], events, on_event)
    holder["pipe"] = pipe
    data = asyncio.run(pipe.run())
    summary = generate_reports(data, cfg, rd, verify=False)
    return {"data": data, "summary": summary, "events": events, "dir": rd, "srv": srv, "before": before,
            "candidates": chosen}


@e2e
def test_smoke_records_menu_actions(after_smoke):
    caps = {r.item.name: r.caps for r in after_smoke["data"].results}
    assert caps["Харилцагч"]["crud"] == "full" and caps["Харилцагч"]["edit"]
    assert {"Нэмэх", "Засах", "Устгах"} <= {b["text"] for b in caps["Харилцагч"]["buttons"]}
    assert {"Код", "Нэр", "Утас"} <= set(caps["Харилцагч"]["columns"]) and caps["Харилцагч"]["search"]
    assert caps["Бараа"]["crud"] == "none"
    assert caps["Ангилал"]["crud"] == "full"


@e2e
def test_candidates_only_full_crud_menus(after_smoke):
    assert sorted(after_smoke["candidates"]) == sorted([CUSTOMERS, CATS, GROUPS, TXN, JOURNAL, ACCT])
    assert any(t == "status" and d["state"] == "awaiting_crud_selection" for t, d in after_smoke["events"])


@e2e
def test_auto_crud_full_lifecycle(after_smoke):
    d, srv = after_smoke["data"], after_smoke["srv"]
    # Бараа нэр дэвшигч биш тул backend хүлээж аваагүй → 5 урсгал
    assert [f["meta"]["menu_key"] for f in d.flows] == [CUSTOMERS, CATS, GROUPS, TXN, JOURNAL, ACCT]
    cust = d.flows[0]
    assert cust["code"] == "A01" and cust["status"] == M.PASS, [(s["name"], s["message"]) for s in cust["steps"]]
    assert cust["meta"]["key"] == "Нэр" and cust["meta"]["fields"] == {"Нэр": "{run_tag} A01 автомат тест"}
    assert cust["data"]["a01_rec"] == f"{d.run_tag} A01 автомат засвар"
    c0, d0, u0 = after_smoke["before"]
    assert (srv.state.created - c0, srv.state.updated - u0, srv.state.deleted - d0) == (1, 1, 1)
    assert not any(d.run_tag in c["name"] for c in srv.state.customers)
    # «Данс» (mock) засахад CostCenterId bug гардаг тул автомат CRUD алхмын нийт төлөв Fail
    assert d.pipeline[2]["kind"] == "autocrud" and d.pipeline[2]["status"] == M.FAIL


@e2e
def test_form_in_new_tab_without_captions(after_smoke):
    """Бодит SmartApp-ын баримт шиг: «Шинэ» → шинэ таб, шошго нь <span> — шинэ табыг таньж, шошгыг
    орчноос таана; хадгалсны дараа жагсаалтын таб руу буцаж хайх/засах/устгах нь ажиллана."""
    g = after_smoke["data"].flows[2]
    assert g["status"] == M.PASS, [(s["name"], s["status"], s["message"]) for s in g["steps"]]
    assert g["meta"]["opened"] == "tab" and g["meta"]["key"] == "Бүлгийн нэр"
    # Тайлбар (заавал биш) хөндөгдөөгүй; Эцэг бүлэг (lookup "...") — цонхноос эхний мөрийг сонгосон
    assert g["meta"]["fields"]["Бүлгийн нэр"] == "{run_tag} A03 автомат тест"
    # SmartApp lookup: групп модыг алгасч grid-ийн фокуслагдсан мөр → «Сонгох» (сумыг дараагүй)
    assert "G01 Үндсэн бүлэг" in g["meta"]["fields"]["Эцэг бүлэг"] and "Бүх бүлэг" not in g["meta"]["fields"]["Эцэг бүлэг"]
    # Зөвхөн модтой lookup: «Бүх …» үндсэн зангилаа биш эхний навч
    assert "Бараа" in g["meta"]["fields"]["Бүлгийн төрөл"] and "Бүх" not in g["meta"]["fields"]["Бүлгийн төрөл"]
    assert after_smoke["srv"].state.record_opened == 0
    assert {f["source"] for f in g["meta"]["form"]} == {"nearby"}
    assert [f["label"] for f in g["meta"]["form"] if f["lookup"]] == ["Эцэг бүлэг", "Бүлгийн төрөл"]
    assert g["data"][f"{g['code'].lower()}_rec"].endswith("автомат засвар")


@e2e
def test_journal_with_lines_full_crud(after_smoke):
    """SmartApp-ын «Гүйлгээ» (бодит DOM-оор): хадгалахад «Данс оруулна уу !» → анхааруулгыг хааж, мөрийн
    grid-д 2 тэнцвэртэй мөр (данс lookup + дебет 1 / кредит 1) нэмж, Дүн*=1 болгоод хадгална → бүрэн CRUD Pass."""
    j = after_smoke["data"].flows[4]
    assert j["meta"]["key"] == "Гүйлгээний утга"            # заавал «утга» талбар түлхүүр болсон
    assert j["status"] == M.PASS, [(s["name"], s["status"], s["message"]) for s in j["steps"]]
    assert j["meta"]["fields"]["Дүн"] == "1"                 # заавал тоон талбарын 0 → 1
    lines = j["meta"]["lines"]
    assert len(lines) == 2 and "Дебет=1" in lines[0] and "Кредит=1" in lines[1] and "1001" in lines[0]
    assert any("Мөр нэмсэн" in n for n in j["steps"][0]["notes"])
    assert any(t["id"].startswith("TC-FLOW-A05-") for t in after_smoke["summary"]["tcs"])
    assert after_smoke["srv"].state.record_opened == 0
    # ХАВХ: бодит (tag-гүй) гүйлгээ анхнаасаа ФОКУСЛАГДСАН; «Засах/Устгах» фокус дээр ажилладаг —
    # автомат CRUD тэр бичлэгийг хэзээ ч засаж/устгах ёсгүй (фокусыг манай мөр рүү шилжүүлж шалгадаг)
    assert after_smoke["srv"].state.untagged_touched == 0
    # 8 алхам: бүртгэх, хайх, засах, бүх талбарыг дахин нээж шалгах, устгах, устгагдсаныг шалгах, цэвэрлэгээ, сөрөг тест
    assert [s["status"] for s in j["steps"]] == [M.PASS] * 8, [(s["name"], s["message"]) for s in j["steps"]]


@e2e
def test_dropdown_new_button_is_skipped(after_smoke):
    t = after_smoke["data"].flows[3]
    assert t["meta"]["opened"] == "menu" and "Орлого" in t["meta"]["skipped"]
    assert t["meta"]["probe"]["menus"] and t["steps"][0]["screenshot"]
    # Бүртгэл алгассан тул CRUD алхмууд Blocked; цэвэрлэгээ (хадгалаагүй — өгөгдөлгүй) ба сөрөг тест (төрөл сонгох
    # цэс) always тул ажиллаад «хамаарахгүй» (N/A)
    assert [s["status"] for s in t["steps"]] == [M.BLOCKED] * 8
    assert [s["category"] for s in t["steps"][6:]] == ["na", "na"]


@e2e
def test_lookup_system_error_is_bug_and_date_is_filled(after_smoke):
    """Бодит SmartApp-д олон lookup нээхэд «Invalid column name 'ActiveStatus'» гардаг — өмнө нь
    «сонгох утга олдсонгүй — YAML бичнэ үү» (Blocked) гэж андуурдаг байсан. Одоо Fail + bug.
    DateEdit (slDate, role=combobox) — сонголтын талбар биш, өнөөдрийн огноо бичнэ."""
    d, s = after_smoke["data"], after_smoke["summary"]
    cats = d.flows[1]
    st = cats["steps"][0]
    assert st["status"] == M.FAIL and "ActiveStatus" in st["message"] and "'Төрөл'" in st["message"], st["message"]
    assert cats["meta"]["fields"]["Огноо"] == datetime.date.today().isoformat()
    assert "skipped" not in cats["meta"]
    assert any("ActiveStatus" in b["title"] + b.get("actual", "") + str(b) for b in s["bugs"])


@e2e
def test_required_field_on_hidden_form_tab(after_smoke):
    """Бодит «Ажилтан» шиг: заавал readonly ComboBox формын өөр таб дээр — «… хоосон байх боломжгүй!»
    анхааруулгаас талбарыг олж, табыг нээж, ▾ товчоор сонгоод хадгална."""
    a = after_smoke["data"].flows[5]
    st = a["steps"][0]
    assert st["status"] == M.PASS, st["message"]
    assert "Хөрөнгө" in a["meta"]["fields"]["Ангилал"]
    assert a["steps"][4]["status"] == M.PASS                     # цэвэрлэгээ


@e2e
def test_unfillable_menu_is_skipped_not_bug(after_smoke):
    s = after_smoke["summary"]
    ids = [t["id"] for t in s["tcs"]]
    assert any(i.startswith("TC-FLOW-A01-") for i in ids) and any(i.startswith("TC-FLOW-A03-") for i in ids)
    assert not any(i.startswith("TC-FLOW-A04-") for i in ids)          # dropdown «Шинэ» — алгассан
    assert not any("A04" in ",".join(b["tcs"]) for b in s["bugs"])
    assert s["decision"] != "GO" or s["stats"]["blocked"] == 0
    assert any(f["skipped"] for f in s["flows"])


@e2e
def test_yaml_draft_generated_and_loadable(after_smoke, tmp_path):
    d = after_smoke["data"]
    assert d.draft == "flows-draft.yaml" and after_smoke["summary"]["draft"] == d.draft
    doc = yaml.safe_load((after_smoke["dir"] / d.draft).read_text(encoding="utf-8"))
    flows = doc["flows"]
    assert len(flows) == 6 and {"auto_khariltsagch", "auto_angilal"} <= set(flows), list(flows)
    txn = next(f for f in flows.values() if "Гүйлгээ" in f["description"])
    assert txn["diagnostics"]["opened"] == "menu" and "Орлого" in " ".join(txn["diagnostics"]["menus"])
    create = flows["auto_khariltsagch"]["steps"][1]
    assert create["uses"] == "crud.create" and create["with"]["fields"] == {"Нэр": "{run_tag} A01 автомат тест"}
    assert create["with"]["menu"] == "Мастер дата / Бүртгэл / Харилцагч"
    assert any(s["uses"] == "crud.update" for s in flows["auto_khariltsagch"]["steps"])
    assert {"Төрөл * (lookup)", "Огноо * (огноо)"} <= set(flows["auto_angilal"]["form_fields"])
    # Ноорогийг flow болгож ачаалж болно (Харилцагч)
    from app.flows.engine import load_flow
    cfg = load_config("http://127.0.0.1:1/", {"flows": {"x": flows["auto_khariltsagch"]}})
    assert len(load_flow(cfg, "x")["steps"]) == 7


# ---------------------------------------------------------------- каталогоос урьдчилан сонгох (smoke-гүй)
@e2e
def test_preselected_menus_without_smoke(srv, tmp_path):
    events: list = []
    c0 = srv.state.created
    pipe, _ = make_pipe(srv, tmp_path, [{"kind": "autocrud", "menus": [CUSTOMERS, ITEMS]}], events)
    d = asyncio.run(pipe.run())
    cust, items = d.flows
    assert cust["status"] == M.PASS and not d.smoke_ran
    assert "Нэмэх товч" in items["meta"]["skipped"]                  # CRUD товчгүй цэс → алгассан
    assert srv.state.created - c0 == 1
    assert not any(t == "crud_candidates" for t, _ in events)


@e2e
def test_system_error_after_edit_form_loads_is_bug(after_smoke):
    """Бодит SmartApp «Данс»: засварын форм манай бичлэгийг нээгээд, өгөгдөл ачаалагдсаны ДАРАА
    «Системийн алдаа — Invalid column name 'CostCenterId'» гарна → засвар Fail (bug), хадгалахгүй;
    цэвэрлэгээ (хожуу алдааны цонхыг хааж) амжилттай устгана."""
    a = after_smoke["data"].flows[5]
    st = [s["status"] for s in a["steps"]]
    assert st[:3] == [M.PASS, M.PASS, M.FAIL] and st[4] == M.PASS, [(s["name"], s["status"], s["message"]) for s in a["steps"]]
    assert "CostCenterId" in a["steps"][2]["message"]
    bugs = [b for b in after_smoke["summary"]["bugs"] if "CostCenterId" in (b.get("actual") or b.get("title") or "")]
    assert bugs and bugs[0]["severity"] == "🟡 Major"
