"""Үе 1 — E2E суурь: тайлангаас утга унших (grid / export), Δ шалгах, баримтын дугаар барих, батлах / цуцлах.

Дуусах шалгуур: нэг баримтыг үүсгэж батлахад Гүйлгээ балансын Δ зөв, цуцлахад Δ = 0 (mock «Борлуулалт» +
«Гүйлгээ баланс» — зөвхөн батлагдсан баримтыг тооцно)."""
import asyncio
from pathlib import Path

import pytest

from app import models as M
from app.config import load_config
from app.flows.steps.business import parse_amount, pick_value, read_table_file, sum_values
from app.pipeline.runner import PipelineRunner
from app.runner.smoke import RunOptions
from tests.fixtures.mock_crud.server import PASSWORD, CrudServer
from tests.human_login import HumanLogin
from tests.test_flows import FAST, e2e

SALE = "Мастер дата / Бүртгэл / Борлуулалт"
TB = "Мастер дата / Бүртгэл / Гүйлгээ баланс"
ROWS = {"avlaga": {"match": "1201", "column": "Эцсийн үлдэгдэл"},
        "orlogo": {"match": "5101", "column": "Эцсийн үлдэгдэл"}}


def tb(save_as, mode="grid", always=False):
    w = {"menu": TB, "run": ["Шүүх"], "rows": ROWS, "save_as": save_as, "mode": mode}
    return {"uses": "report.read", "name": f"Гүйлгээ баланс ({save_as})", "always": always, "with": w}


def delta(before, after, avlaga, orlogo, always=False):
    return {"uses": "assert.delta", "name": f"Δ {before}→{after}", "always": always,
            "with": {"before": before, "after": after, "expect": {"avlaga": avlaga, "orlogo": orlogo}}}


ORDER_TO_LEDGER = [
    tb("tb0"),
    {"uses": "crud.create", "name": "Борлуулалтын баримт (ноорог)",
     "with": {"menu": SALE, "fields": {"Тайлбар": "{run_tag} борлуулалт", "Дүн": "100000"}, "key": "Тайлбар",
              "save_as": "s_rec"}},
    {"uses": "doc.number", "name": "Баримтын дугаар", "with": {"menu": SALE, "find": "{s_rec}", "column": "Дугаар",
                                                              "save_as": "s_no"}},
    tb("tb1"),
    delta("tb0", "tb1", 0, 0),                                   # ноорог гүйлгээ бичихгүй
    {"uses": "doc.action", "name": "Батлах", "with": {"menu": SALE, "find": "{s_rec}", "action": "Батлах",
                                                     "expect": {"column": "Төлөв", "value": "Батлагдсан"}}},
    tb("tb2", mode="export"),
    delta("tb0", "tb2", 100000, -100000),
    {"uses": "doc.action", "name": "Цуцлах (цэвэрлэгээ)", "always": True,
     "with": {"menu": SALE, "find": "{s_rec}", "action": "Цуцлах", "expect": {"column": "Төлөв", "value": "Цуцлагдсан"}}},
    tb("tb3", always=True),
    delta("tb0", "tb3", 0, 0, always=True),                      # цуцлахад анхны байдалдаа буцна
]


def run_e2e(tmp_path, steps, name="o2l"):
    with CrudServer() as srv:
        srv.state.e2e = True
        cfg = load_config(srv.url, {**FAST, "flows": {name: steps}})
        pipe = PipelineRunner(name, srv.url, cfg, RunOptions(project="T"), tmp_path,
                              steps=[{"kind": "crud", "flow": name}], headless=True,
                              on_waiting_login=HumanLogin("admin", PASSWORD))
        data = asyncio.run(pipe.run())
    return data, data.flows[0]


# ---------------------------------------------------------------- browser-гүй
def test_pick_value_and_files(tmp_path):
    h = ["Дансны код", "Дансны нэр", "Эцсийн үлдэгдэл"]
    rows = [["1201", "Авлага", "500,000.00"], ["12010", "Бусад авлага", "7.00"]]
    assert pick_value(h, rows, "1201", "Эцсийн үлдэгдэл") == (500000.0, "")      # яг таарсан нүд түрүүлнэ
    assert pick_value(h, rows, "9999", "Эцсийн үлдэгдэл")[0] is None
    assert "багана олдсонгүй" in pick_value(h, rows, "1201", "Дебет")[1]
    # Толгойгүй сонголтын багана (checkbox) — нүд олон бол баруун талаас тааруулна
    assert pick_value(h, [["", "1201", "Авлага", "5.00"]], "1201", "Эцсийн үлдэгдэл")[0] == 5.0
    p = tmp_path / "tb.csv"
    p.write_text('﻿"Тайлан"\n"Дансны код";"Дансны нэр";"Эцсийн үлдэгдэл"\n"1201";"Авлага";"1,5"\n', encoding="utf-8")
    hh, rr = read_table_file(p)
    assert hh == ["Дансны код", "Дансны нэр", "Эцсийн үлдэгдэл"] and pick_value(hh, rr, "1201", "Эцсийн үлдэгдэл")[0] == 1.5
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["Гүйлгээ баланс"])
    wb.active.append(["Дансны код", "Дансны нэр", "Эцсийн үлдэгдэл"])
    wb.active.append(["1201", "Авлага", 1234.5])
    wb.save(tmp_path / "tb.xlsx")
    hh, rr = read_table_file(tmp_path / "tb.xlsx")
    assert pick_value(hh, rr, "1201", "Эцсийн үлдэгдэл")[0] == 1234.5
    assert parse_amount("(2,500.00)") == -2500.0 and parse_amount("—") is None

    # ГД-ийн бичилтийн жагсаалтаас дансны үлдэгдэл: Σ Дебет − Σ Кредит (хоосон нүд = 0, бичилтгүй = 0)
    h = ["№", "Утга", "Данс", "Дебет - ₮", "Кредит - ₮"]
    r = [["ЖУ1", "x", "A01", "1.00", "0.00"], ["ЖУ1", "x", "A02", "0.00", "1.00"], ["ЖУ2", "y", "A01", "", "5"]]
    assert sum_values(h, r, "A01", "Дебет", "Кредит") == (-4.0, "")
    assert sum_values(h, r, "A02", "Дебет", "Кредит") == (-1.0, "")
    assert sum_values(h, [], "A01", "Дебет", "Кредит") == (0.0, "")
    assert "багана олдсонгүй" in sum_values(h, r, "A01", "Үлдэгдэл")[1]


# ---------------------------------------------------------------- mock: Үе 1-ийн дуусах шалгуур
@e2e
def test_document_to_ledger_delta(tmp_path):
    data, fl = run_e2e(tmp_path, ORDER_TO_LEDGER)
    st = {s["index"]: s for s in fl["steps"]}
    assert fl["status"] == M.PASS, [(s["index"], s["name"], s["status"], s["message"][:150]) for s in fl["steps"]]
    assert fl["data"]["s_no"].startswith("БР")                        # систем үүсгэсэн дугаар дамжсан
    assert fl["data"]["tb0"]["avlaga"] == 500000.0
    assert fl["data"]["tb2"]["avlaga"] - fl["data"]["tb0"]["avlaga"] == 100000.0   # export (CSV) горимоор
    assert any("reports/tb2-" in n for n in st[7]["notes"])
    assert fl["data"]["tb3"] == fl["data"]["tb0"]                      # цуцлахад Δ = 0


@e2e
def test_wrong_delta_and_rejected_action_fail(tmp_path):
    """Бизнесийн алдааг илрүүлэх: хүлээгдэж буй Δ зөрвөл Fail; систем үйлдлийг татгалзвал (ноорог биш баримтыг
    дахин батлах) Fail; цэвэрлэгээ (always) ажилласаар байна."""
    steps = ORDER_TO_LEDGER[:6] + [
        tb("tb2"),
        delta("tb0", "tb2", 999, -999),                                # буруу хүлээлт → Fail
        {"uses": "doc.action", "name": "Дахин батлах", "always": True,
         "with": {"menu": SALE, "find": "{s_rec}", "action": "Батлах"}},
        ORDER_TO_LEDGER[8], tb("tb3", always=True), delta("tb0", "tb3", 0, 0, always=True)]
    _, fl = run_e2e(tmp_path, steps, "neg")
    by = {s["name"]: s for s in fl["steps"]}
    assert by["Δ tb0→tb2"]["status"] == M.FAIL and "хүлээгдэж буйгаас өөр" in by["Δ tb0→tb2"]["message"]
    assert by["Дахин батлах"]["status"] == M.FAIL and "ноорог" in by["Дахин батлах"]["message"]
    assert by["Цуцлах (цэвэрлэгээ)"]["status"] == M.PASS and by["Δ tb0→tb3"]["status"] == M.PASS


@e2e
def test_doc_action_refuses_untagged_document(tmp_path):
    _, fl = run_e2e(tmp_path, [{"uses": "doc.action", "with": {"menu": SALE, "find": "Бодит баримт", "action": "Батлах"}}],
                    "guard")
    assert fl["steps"][0]["status"] == M.FAIL and "run_tag" in fl["steps"][0]["message"]


# ---------------------------------------------------------------- Үе 2: бүрэлдэхүүнээс сценари (include) + тайлан
def test_include_expands_and_rejects_cycles():
    from app.flows.engine import FlowConfigError, load_flow
    cfg = {"flows": {"rec: Борлуулалт бүртгэх": {"steps": [{"uses": "crud.open", "name": "Цэс", "with": {"menu": SALE}}]},
                     "o2c": {"type": "e2e", "steps": [{"uses": "crud.open", "with": {"menu": TB}},
                                                      {"include": "rec: Борлуулалт бүртгэх", "always": True}]},
                     "loop_a": [{"include": "loop_b"}], "loop_b": [{"include": "loop_a"}]}}
    steps = load_flow(cfg, "o2c")["steps"]
    assert [s.get("name") for s in steps] == [None, "[Борлуулалт бүртгэх] Цэс"] and steps[1]["always"] is True
    with pytest.raises(FlowConfigError, match="давталт"):
        load_flow(cfg, "loop_a")
    with pytest.raises(FlowConfigError, match="алга"):
        load_flow({"flows": {"x": [{"include": "nope"}]}}, "x")


@e2e
def test_e2e_scenario_from_components_is_reported(tmp_path):
    """Бичсэн (recorder) бүрэлдэхүүн + тайлангийн шалгалтаар бүрдсэн `type: e2e` сценари: TC-FLOW, тайланд
    «Бизнес процесс» гэж Δ-тэйгээ гарна."""
    from app.report.build import generate_reports
    record = [ORDER_TO_LEDGER[1]]                                       # «Урсгал бичих»-ийн оронд
    scenario = {"type": "e2e", "code": "O2L", "description": "Борлуулалт → батлах → Гүйлгээ баланс → цуцлах",
                "steps": [ORDER_TO_LEDGER[0], {"include": "rec: Борлуулалт бүртгэх"}] + ORDER_TO_LEDGER[2:]}
    with CrudServer() as srv:
        srv.state.e2e = True
        cfg = load_config(srv.url, {**FAST, "flows": {"rec: Борлуулалт бүртгэх": {"recorded": True, "steps": record},
                                                       "O2L борлуулалт": scenario}})
        pipe = PipelineRunner("o2l", srv.url, cfg, RunOptions(project="T"), tmp_path,
                              steps=[{"kind": "crud", "flow": "O2L борлуулалт"}], headless=True,
                              on_waiting_login=HumanLogin("admin", PASSWORD))
        data = asyncio.run(pipe.run())
    fl = data.flows[0]
    assert fl["status"] == M.PASS and fl["meta"]["e2e"], [(s["name"], s["status"], s["message"][:120]) for s in fl["steps"]]
    assert fl["steps"][1]["name"].startswith("[Борлуулалт бүртгэх]")
    s = generate_reports(data, cfg, tmp_path, verify=False)
    assert any(t["id"].startswith("TC-FLOW-O2L-") for t in s["tcs"])
    from docx import Document
    doc = Document(str(tmp_path / s["docx"]))
    text = "\n".join(p.text for p in doc.paragraphs) + "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert "Бизнес процессын E2E «O2L борлуулалт»" in text and "Бизнес процесс: O2L борлуулалт" in text
    assert "Тайлангийн шалгалт (Δ)" in text and "Δ +100,000.00" in text


# ---------------------------------------------------------------- Үе 1: журнал → дансны үлдэгдэл (бодит SmartApp-ын бүтэц)
ACCLIST = "Мастер дата / Бүртгэл / Дансны жагсаалт"
JOURNAL_KEY = "Мастер дата|Бүртгэл|Журнал|/md/journal"


def acc_read(save_as, always=False):
    return {"uses": "report.read", "name": f"Дансны үлдэгдэл ({save_as})", "always": always,
            "with": {"menu": ACCLIST, "search": True, "save_as": save_as,
                     "rows": {"debit": {"match": "{acc_debit}", "column": "Үлдэгдэл"},
                              "credit": {"match": "{acc_credit}", "column": "Үлдэгдэл"}}}}


JOURNAL_TO_BALANCE = [
    acc_read("b0"),
    {"uses": "crud.auto_create", "name": "Журналын гүйлгээ (дебет / кредит данс заасан)",
     "with": {"menu": JOURNAL_KEY, "save_as": "j_rec", "field_as": "j_field", "try_as": "j_try",
              "lines": {"debit": "{acc_debit}", "credit": "{acc_credit}", "amount": "1"}}},
    acc_read("b1"),
    {"uses": "assert.delta", "name": "Гүйлгээ: дебет +1, кредит −1",
     "with": {"before": "b0", "after": "b1", "expect": {"debit": 1, "credit": -1}}},
    {"uses": "crud.delete", "name": "Цэвэрлэх: гүйлгээ устгах", "always": True,
     "with": {"menu": JOURNAL_KEY, "find": "{j_rec}"}},
    acc_read("b2", always=True),
    {"uses": "assert.delta", "name": "Цэвэрлэгээ: Δ = 0", "always": True,
     "with": {"before": "b0", "after": "b2", "expect": {"debit": 0, "credit": 0}}},
]


@e2e
def test_phase1_journal_to_account_balance(tmp_path):
    """Үе 1-ийн дуусах шалгуур (SmartApp-ын бодит бүтцээр): ГД-ийн гүйлгээг тодорхой дебет/кредит дансаар
    үүсгэхэд «Данс» жагсаалтын үлдэгдлийн Δ зөв, устгахад Δ = 0. Журналын мөрийн grid «Данс» таб дээр."""
    with CrudServer() as srv:
        srv.state.e2e = True
        cfg = load_config(srv.url, {**FAST, "flows": {"p1": {"type": "e2e", "steps": JOURNAL_TO_BALANCE}},
                                    "e2e": {"vars": {"acc_debit": "1001", "acc_credit": "1101"}}})
        pipe = PipelineRunner("p1", srv.url, cfg, RunOptions(project="T"), tmp_path,
                              steps=[{"kind": "crud", "flow": "p1"}], headless=True,
                              on_waiting_login=HumanLogin("admin", PASSWORD))
        data = asyncio.run(pipe.run())
    fl = data.flows[0]
    assert fl["status"] == M.PASS, [(s["index"], s["name"], s["status"], s["message"][:160]) for s in fl["steps"]]
    b0, b1 = fl["data"]["b0"], fl["data"]["b1"]                  # бодит (бусдын) гүйлгээ ч үлдэгдэлд орно — зөвхөн Δ
    assert (b1["debit"] - b0["debit"], b1["credit"] - b0["credit"]) == (1.0, -1.0)
    assert fl["data"]["b2"] == fl["data"]["b0"]
    notes = " ".join(fl["steps"][1]["notes"])
    assert "1001" in notes and "1101" in notes                    # мөрүүд заасан дансаар


@e2e
def test_phase1_site_scenario_is_self_contained(tmp_path):
    """testaccounting-ийн «P1» сценарийг (config-оос, зөвхөн цэсний түлхүүрийг mock-ийнхоор сольж) ажиллуулна:
    тест өөрийн 2 дансыг үүсгэж → гүйлгээ → Δ +1/−1 → устгахад Δ = 0 → дансуудыг устгана. Тохиргоо (vars) шаардахгүй."""
    import json as _json
    from app.flows.engine import load_flow
    site = load_config("https://testaccounting.smartapp.mn/")
    spec = load_flow(site, "P1: Гүйлгээ → Дансны үлдэгдэл")
    text = _json.dumps(spec, ensure_ascii=False)
    text = text.replace("Мастер дата|Ерөнхий дэвтэр|Данс|accountlist", "Мастер дата|Бүртгэл|Дансны бүртгэл|/md/accm")
    text = text.replace("Ерөнхий дэвтэр|Жагсаалт|Гүйлгээний жагсаалт|generalledger/journallist", JOURNAL_KEY)
    spec = _json.loads(text)
    with CrudServer() as srv:
        srv.state.e2e = True
        cfg = load_config(srv.url, {**FAST, "flows": {"P1": spec}})
        pipe = PipelineRunner("p1s", srv.url, cfg, RunOptions(project="T"), tmp_path,
                              steps=[{"kind": "crud", "flow": "P1"}], headless=True,
                              on_waiting_login=HumanLogin("admin", PASSWORD))
        data = asyncio.run(pipe.run())
    fl = data.flows[0]
    assert fl["status"] == M.PASS, [(s["index"], s["name"], s["status"], s["message"][:160]) for s in fl["steps"]]
    assert fl["data"]["b0"] == {"debit": 0.0, "credit": 0.0}         # шинэ данс — 0-ээс
    assert fl["data"]["b1"] == {"debit": 1.0, "credit": -1.0}
    assert fl["data"]["b2"] == fl["data"]["b0"]
    by = {s["name"]: s for s in fl["steps"]}
    assert "Устгав" in " ".join(by["Цэвэрлэх: тест данс (дебет)"]["notes"])     # данснууд устгагдсан
    assert "Устгав" in " ".join(by["Цэвэрлэх: тест данс (кредит)"]["notes"])
    # testaccounting 2026-10-19: «Туслахтай» төрлийн данс журналын дүнг бүртгүүлээгүй; «Мэдээлэл дутуу» үед
    # «Гүйлгээний загвар» сонгож мөрүүдийг дарж бичсэн — аль аль нь давтагдахгүй
    acc_notes = " ".join(by["Тест данс үүсгэх (дебет)"]["notes"])
    assert "7001 Бусад зардал" in acc_notes and "Туслахтай" not in acc_notes, acc_notes      # НӨАТ ч биш
    # зардлын төрөл зөвхөн дебет — кредит данс орлогын төрөл (testaccounting 2026-10-19)
    assert "5101 Бусад орлого" in " ".join(by["Тест данс үүсгэх (кредит)"]["notes"])
    assert "НӨАТ=" not in " ".join(fl["steps"][3]["notes"])                 # толгойн НӨАТ бөглөөгүй
    assert "Гүйлгээний загвар" not in " ".join(fl["steps"][3]["notes"])
