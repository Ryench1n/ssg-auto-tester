"""Автомат CRUD-ийн сайжруулалт: DateEdit-ийг шошгоор таних, Fail болсон ч цэвэрлэх, мастер өгөгдлийг түрүүлж
үүсгэх, «мэдээлэл дутуу» үед заавал биш талбар бөглөх, засварын дараа бүх талбарыг дахин нээж шалгах,
хоосон форм хадгалах сөрөг тест."""
import asyncio
import datetime

import pytest

from app import models as M
from app.flows.steps.auto import date_value, is_date_field, plan_values, validation_kind
from app.flows.steps.auto_checks import close_enough, same_value
from app.pipeline.stages import auto_flow_spec, master_for
from app.report.build import auto_skip_counts, generate_reports, leftovers
from tests.fixtures.mock_crud.server import CrudServer
from tests.test_autocrud import e2e, key_of, make_pipe

EMP = key_of("Мастер дата", "Бүртгэл", "Ажилтан", "/md/emp")
LEAVE = key_of("Мастер дата", "Бүртгэл", "Чөлөө", "/md/leave")
NOTE = key_of("Мастер дата", "Бүртгэл", "Тэмдэглэл", "/md/note")
PROJ = key_of("Мастер дата", "Бүртгэл", "Төсөл", "/md/proj")
LOCK = key_of("Мастер дата", "Бүртгэл", "Түгжээ", "/md/lock")
SCHED = key_of("Мастер дата", "Бүртгэл", "Давтамж", "/md/sched")
EBAR = key_of("Мастер дата", "Бүртгэл", "И-баримт", "/md/ebar")
CITY = key_of("Мастер дата", "Бүртгэл", "Хот", "/md/city")
SLOW = key_of("Мастер дата", "Бүртгэл", "Удаан жагсаалт", "/md/slow")
MANUAL = key_of("Мастер дата", "Бүртгэл", "Шүүлттэй жагсаалт", "/md/manual")
GHOST = key_of("Мастер дата", "Бүртгэл", "Үл харагдах жагсаалт", "/md/ghost")
TGRP = key_of("Мастер дата", "Бүртгэл", "Бүлгийн лавлах", "/md/tgrp")
SGRP = key_of("Мастер дата", "Бүртгэл", "Загварын бүлэг", "/md/sgrp")


def f(label, **kw):
    return {"label": label, "tag": "input", "type": "", "combo": False, "readonly": False, "required": False,
            "value": "", **kw}


# ---------------------------------------------------------------- browser-гүй
def test_date_detected_by_label_when_dom_says_combo():
    """HR «Төрсөн огноо»: DateEdit-ийн DOM slDate классгүй, ▾ товчтой → combo гэж андуурч «сонгох утга олдсонгүй»
    гэж алгасдаг байсан. Шошгоор огноо гэж таниад огноо бичнэ (сонгохгүй)."""
    assert is_date_field(f("Төрсөн огноо", combo=True))
    assert is_date_field(f("Эхлэх огноо"))
    assert not is_date_field(f("Огноо", lookup=True))            # огнооны lookup — сонголт хэвээр
    assert not is_date_field(f("Төрөл", combo=True))
    vals, _, picks, _ = plan_values([f("Нэр", required=True), f("Төрсөн огноо", combo=True, required=True)], "T", {})
    assert "Төрсөн огноо" not in picks
    born = datetime.date.fromisoformat(vals["Төрсөн огноо"])
    assert datetime.date.today().year - born.year == 30            # 18+ насны шалгалтад тэнцэнэ
    assert date_value("Дуусах огноо", []) == datetime.date.today().isoformat()


def test_validation_kind():
    assert validation_kind("Анхаар | Мэдээлэл татна уу!") == "workflow"
    assert validation_kind("Давтамжит гүйлгээ | Хуваарь оруулна!") == "workflow"
    assert validation_kind("Анхааруулга | Хадгалах мэдээлэл дутуу байна.") == "validation"
    assert validation_kind("x", {"workflow_texts": ["x"]}) == "workflow"


def test_master_for():
    k = lambda n: key_of("Цалин", "Бүртгэл", n, "/x")
    keys = [k("Ажилтны жагсаалт"), k("Харилцагчийн бүлэг"), k("Харилцагч")]
    assert master_for("Ажилтан", keys) == k("Ажилтны жагсаалт")   # монгол хувилал (Ажилтан → Ажилтны)
    assert master_for("Харилцагч", keys) == k("Харилцагч")        # яг ижил > «… бүлэг»
    assert master_for("Харилцагч", [k("Харилцагчийн бүлэг")]) is None   # туслах ангилал мастер биш
    assert master_for("Байршил", keys) is None
    # HR 2026-10-15: «Ажилтан» lookup-д «Ажилтны үзүүлэлт»-ийг мастер гэж сонгосон — өөр өгөгдөл
    assert master_for("Ажилтан", [k("Ажилтны үзүүлэлт"), k("Ажилтны жагсаалт")]) == k("Ажилтны жагсаалт")
    assert master_for("Ажилтан", [k("Ажилтны үзүүлэлт")]) is None
    assert master_for("Сургалтын байгуулга", [k("Сургалтын байгууллага")]) == k("Сургалтын байгууллага")


def test_saved_value_comparison():
    assert same_value("2026-10-13", "13.10.2026", "date") and not same_value("2026-10-13", "12.10.2026", "date")
    assert same_value("1", "1.00") and same_value("1", "1,00") and not same_value("1", "2")
    assert same_value("SSG01 A01 тест", "SSG01 A01 тест") and not same_value("SSG0101", "")
    assert close_enough("G01 Үндсэн бүлэг", "Үндсэн бүлэг") and not close_enough("G01 Үндсэн бүлэг", "")


def test_auto_flow_spec_has_new_steps():
    steps = auto_flow_spec("m|f|n|/h", "a01", {"Ажилтан": "SSG X"})["steps"]
    assert [s["uses"] for s in steps] == ["crud.auto_create", "crud.search", "crud.auto_update", "crud.auto_verify",
                                          "crud.delete", "crud.search", "crud.auto_cleanup", "crud.auto_empty_save"]
    assert steps[0]["with"]["prefer"] == {"Ажилтан": "SSG X"} and steps[0]["with"]["try_as"] == "a01_try"
    assert [bool(s.get("always")) for s in steps][-2:] == [True, True]


def test_skip_groups_and_leftovers():
    def fl(menu, cat, **meta):
        return {"flow": f"auto: {menu}", "meta": {"auto": True, "menu": menu, **meta},
                "steps": [{"status": M.BLOCKED, "category": cat}]}
    flows = [fl("А", "nodata", skipped="x"), fl("Б", "nodata", skipped="x"), fl("В", "workflow", skipped="x"),
             fl("Г", "", skipped="x"), fl("М", "", skipped="x", setup=True),
             fl("Д", "", leftover=["{run_tag} A05: устгаж чадсангүй"])]
    g = {x["kind"]: x["count"] for x in auto_skip_counts(flows)}
    assert g == {"nodata": 2, "workflow": 1, "": 1}                   # мастер (setup) урсгал тоологдохгүй
    assert leftovers(flows) == [{"menu": "Д", "items": ["{run_tag} A05: устгаж чадсангүй"]}]


# ---------------------------------------------------------------- mock сайт (deps)
@pytest.fixture(scope="module")
def deps_run(tmp_path_factory):
    rd = tmp_path_factory.mktemp("deps")
    with CrudServer() as srv:
        srv.state.deps = True
        pipe, cfg = make_pipe(srv, rd, [{"kind": "autocrud", "menus": [LEAVE, EMP, NOTE, PROJ, LOCK, SCHED, EBAR, CITY, SLOW, MANUAL, GHOST, TGRP, SGRP]}], [])
        data = asyncio.run(pipe.run())
    summary = generate_reports(data, cfg, rd, verify=False)
    by = {}
    for fl in data.flows:
        by.setdefault(fl["meta"]["menu_key"] if not fl["meta"].get("setup") else fl["flow"], fl)
    return {"data": data, "summary": summary, "by": by, "dir": rd}


def steps_of(fl):
    return [(s["index"], s["status"], s["message"][:120]) for s in fl["steps"]]


@e2e
def test_birth_date_by_label_is_filled(deps_run):
    emp = deps_run["by"][EMP]
    assert emp["status"] == M.PASS, steps_of(emp)
    assert emp["meta"]["fields"]["Төрсөн огноо"].startswith(str(datetime.date.today().year - 30))


@e2e
def test_master_created_first_then_dependent_retried(deps_run):
    """«Чөлөө»-ийн «Ажилтан» lookup тест орчинд хоосон → эхний оролдлогоор алгасна → «Ажилтан» цэсээр мастер
    бичлэг үүсгэж (устгалгүй) «Чөлөө»-г дахин ажиллуулна → эцэст нь мастерийг устгана."""
    d, by = deps_run["data"], deps_run["by"]
    leave = by[LEAVE]
    assert leave["status"] == M.PASS and leave["meta"]["master"]["label"] == "Ажилтан", steps_of(leave)
    assert "skipped" not in leave["meta"]
    setup = [fl for fl in d.flows if fl["meta"].get("setup")]
    assert [fl["flow"].split(":")[0] for fl in setup] == ["master", "master-cleanup"]
    assert all(fl["status"] == M.PASS for fl in setup), [steps_of(fl) for fl in setup]
    # Мастер урсгал TC/bug болохгүй, Чөлөө-ний TC нэг л удаа (дахин оролдлого өмнөхийг сольсон)
    ids = [t["id"] for t in deps_run["summary"]["tcs"]]
    assert not any(i.startswith("TC-FLOW-M") for i in ids)
    assert len([i for i in ids if i.startswith(f"TC-FLOW-{leave['code']}-")]) == 8


@e2e
def test_verify_detects_field_lost_after_edit_and_empty_save_without_validation(deps_run):
    note = deps_run["by"][NOTE]
    st = {s["index"]: s for s in note["steps"]}
    assert st[3]["status"] == M.PASS                                   # засвар өөрөө алдаагүй
    assert st[4]["status"] == M.FAIL and "«Код»" in st[4]["message"], steps_of(note)
    assert st[5]["status"] == M.PASS                                   # цэвэрлэгээ үргэлжилнэ
    assert st[8]["status"] == M.FAIL and "validation алга" in st[8]["message"]
    titles = " ".join(b["title"] + b.get("actual", "") for b in deps_run["summary"]["bugs"])
    assert "«Код»" in titles and "validation алга" in titles


@e2e
def test_cleanup_after_failed_create(deps_run):
    """«Төсөл»: хадгалахад системийн алдаа (bug) гарсан ч бичлэг үүссэн → цэвэрлэгээ хайж устгана."""
    proj = deps_run["by"][PROJ]
    st = {s["index"]: s for s in proj["steps"]}
    assert st[1]["status"] == M.FAIL and "ProjectStatus" in st[1]["message"]
    assert st[7]["status"] == M.PASS and proj["meta"]["cleaned"] == [f"{{run_tag}} {proj['code']} автомат тест"]
    assert "leftover" not in proj["meta"]


@e2e
def test_undeletable_leftover_is_reported(deps_run):
    lock = deps_run["by"][LOCK]
    st = {s["index"]: s for s in lock["steps"]}
    assert st[7]["status"] == M.NOT_TESTED and "гараар устгана уу" in st[7]["message"], steps_of(lock)
    left = deps_run["summary"]["leftovers"]
    lock_left = [x for x in left if x["menu"] == "Мастер дата / Бүртгэл / Түгжээ"]
    assert lock_left and "{run_tag}" in lock_left[0]["items"][0]
    from docx import Document
    text = "\n".join(p.text for p in Document(str(deps_run["dir"] / deps_run["summary"]["docx"])).paragraphs)
    tables = "\n".join(c.text for t in Document(str(deps_run["dir"] / deps_run["summary"]["docx"])).tables
                       for r in t.rows for c in r.cells)
    assert "Цэвэрлэгдээгүй тестийн өгөгдөл" in text + tables


@e2e
def test_not_applicable_checks_are_not_counted(deps_run):
    """«Хамаарахгүй» алхам (ж: өгөгдөл хадгалаагүй үеийн цэвэрлэгээ) TC болж Pass rate-ийг хиймлээр өсгөхгүй."""
    lock = deps_run["by"][LOCK]
    tcs = [t for t in deps_run["summary"]["tcs"] if t["id"].startswith(f"TC-FLOW-{lock['code']}-")]
    # Өмнөх алхмаас хамаарч ажиллаагүй алхмууд тусдаа TC биш — эх алхмын TC-д «дараагийн N алхам ажиллаагүй»
    dependent = [s for s in lock["steps"] if s["status"] in (M.BLOCKED, M.NOT_TESTED)
                 and s["message"].startswith(("Өмнөх алхам", "Шаардлагатай өгөгдөл алга"))]
    # Ажиллаагүй (Шалгагдаагүй) алхам ч TC биш — тайлангийн «Шалгагдаагүй» жагсаалтад шалтгаантай нь
    not_run = [s for s in lock["steps"] if s["status"] == M.NOT_TESTED and s not in dependent]
    assert dependent and len(tcs) == len(lock["steps"]) - len(dependent) - len(not_run)
    assert any("алхам ажиллаагүй" in t["note"] for t in tcs), [t["note"] for t in tcs]
    proj_na = [s for fl in deps_run["data"].flows for s in fl["steps"] if s.get("category") == "na"]
    na_ids = {f"TC-FLOW-{fl['code']}-{s['index']:03d}" for fl in deps_run["data"].flows for s in fl["steps"]
              if s.get("category") == "na" and not fl["meta"].get("setup")}
    assert not na_ids & {t["id"] for t in deps_run["summary"]["tcs"]}, proj_na


@e2e
def test_missing_master_is_explained(tmp_path):
    with CrudServer() as srv:
        srv.state.deps = True
        pipe, cfg = make_pipe(srv, tmp_path, [{"kind": "autocrud", "menus": [LEAVE]}], [])
        d = asyncio.run(pipe.run())
    [leave] = d.flows
    assert leave["meta"]["needs"] == "Ажилтан" and "мастер цэс сонгогдоогүй" in leave["meta"]["skipped"]
    s = generate_reports(d, cfg, tmp_path, verify=False)
    assert s["auto_skips"][0]["kind"] == "nodata" and s["auto_skips"][0]["count"] == 1


@e2e
def test_generic_missing_info_fills_optional_fields(deps_run):
    """«Хадгалах мэдээлэл дутуу байна.» (аль талбар гэдгийг заагаагүй) → заавал биш хоосон талбаруудыг бөглөөд
    дахин хадгална; дахин нээж шалгахад тэр талбар ч хадгалагдсан байна."""
    sched = deps_run["by"][SCHED]
    assert sched["status"] == M.PASS, steps_of(sched)
    assert sched["meta"]["fields"]["Нэмэлт тайлбар"].startswith("(заавал биш)")
    assert any("заавал биш талбаруудыг бөглөв" in n for n in sched["steps"][0]["notes"])
    assert "2 талбарыг шалгав" in " ".join(sched["steps"][3]["notes"])


@e2e
def test_workflow_form_is_classified(deps_run):
    ebar = deps_run["by"][EBAR]
    assert "тусгай үйлдэл" in ebar["meta"]["skipped"] and ebar["steps"][0]["category"] == "workflow"
    kinds = {g["kind"]: g for g in deps_run["summary"]["auto_skips"]}
    assert kinds["workflow"]["menus"] == ["Мастер дата / Бүртгэл / И-баримт"]


@e2e
def test_lookup_skips_all_row(deps_run):
    """Lookup-ийн «999 Бүх улс» нэгтгэл мөрийг бусад мөр байвал сонгохгүй (HR «Хот, аймаг»)."""
    city = deps_run["by"][CITY]
    assert city["status"] == M.PASS, steps_of(city)
    assert "Монгол" in city["meta"]["fields"]["Улс"] and "Бүх" not in city["meta"]["fields"]["Улс"]


@e2e
def test_delayed_grid_refresh_is_not_a_failure(deps_run):
    """Хадгалсан/устгасан бичлэг жагсаалтад хоцорч тусах үед хайлт/устгалын шалгалт хүлээж дахин оролдоно
    (HR 2026-10-14: «мөр олдсонгүй», «устгасны дараа мөр хэвээр» гэсэн худал Fail)."""
    slow = deps_run["by"][SLOW]
    assert slow["status"] == M.PASS, steps_of(slow)


def test_validation_target_phrasings():
    from app.flows.ui import validation_targets
    t = ["Анхаар", "Анхааруулга"]
    assert validation_targets("Анхааруулга\nБүлэг сонгоогүй байна.", t) == ["Бүлэг"]
    assert validation_targets("Анхааруулга\nУлс сонгогдоогүй байна!", t) == ["Улс"]
    assert validation_targets("Анхаар\nЗөвшөөрөх эсвэл цуцлах хүний тоо 0 байж болохгүй!", t) == \
        ["Зөвшөөрөх эсвэл цуцлах хүний тоо"]



# ---------------------------------------------------------------- Accounting 2026-10-14 run-ын худал Fail
@e2e
def test_list_needing_filter_click_is_refreshed(deps_run):
    """Баримтын жагсаалт хадгалсны дараа өөрөө шинэчлэгддэггүй — «Шүүх» дарж дахин хайна (A13)."""
    m = deps_run["by"][MANUAL]
    assert m["status"] == M.PASS, steps_of(m)


@e2e
def test_saved_but_invisible_record_is_not_reported_clean(deps_run):
    """Хадгалсан бичлэг жагсаалтаас олдохгүй бол цэвэрлэгээ «үлдээгүй» гэж худал хэлэхгүй — гараар шалгуулна."""
    g = deps_run["by"][GHOST]
    st = {s["index"]: s for s in g["steps"]}
    assert st[1]["status"] == M.PASS and st[2]["status"] == M.FAIL
    assert st[7]["status"] == M.NOT_TESTED and "гараар шалгана уу" in st[7]["message"], steps_of(g)
    assert any(x["menu"].endswith("Үл харагдах жагсаалт") for x in deps_run["summary"]["leftovers"])


def test_key_prefers_column_shown_in_list():
    """Түлхүүр талбар жагсаалтад баганатай байх ёстой (A22: «Гүйлгээний утга» баганагүй)."""
    fields = [f("Гүйлгээний утга", required=True), f("Баримтын №", required=True)]
    _, key, _, _ = plan_values(fields, "T", {})
    assert key == "Гүйлгээний утга"
    _, key, _, _ = plan_values(fields, "T", {}, columns=["№", "Огноо", "Баримтын №"])
    assert key == "Баримтын №"


def test_item_level_lists_are_not_autocrud_candidates():
    from types import SimpleNamespace as NS
    from app.models import MenuItem
    from app.runner.capabilities import crud_candidates
    mk = lambda name, cols: NS(item=MenuItem("Систем", "Маягт", name, "/x/" + name), tab_opened=True, status="Pass",
                               caps={"crud": "full", "columns": cols, "buttons": []})
    names = [c["name"] for c in crud_candidates([
        mk("Шаардах хуудасны жагсаалт", ["№", "Огноо", "Гүйлгээний утга"]),
        mk("Шаардах хуудасны жагсаалт /бараагаар/", ["№", "Барааны код", "Барааны нэр"])])]
    assert names == ["Шаардах хуудасны жагсаалт"]


def test_validation_titled_dialog_with_error_word_is_warning():
    """«Анхаар — Нөхцөл алдаатай байна!» — хэрэглэгчийн утгын validation (A60), системийн алдаа биш."""
    import asyncio as aio
    from app.config import load_config
    from app.flows.ui import wait as ui_wait

    class St:
        dialogs = ["Анхаар\nНөхцөл алдаатай байна!"]
        toasts: list = []
        loading = False

    class Env:
        cfg = load_config("http://x/")
        page = None

    async def snap(page, cfg):
        return St()
    orig = ui_wait.snapshot
    ui_wait.snapshot = snap
    try:
        errors, warnings = aio.run(ui_wait.popup_problems(Env()))
        assert errors == [] and warnings
        St.dialogs = ["Анхаар\nData is Null. This method cannot be called on Null values."]
        errors, _ = aio.run(ui_wait.popup_problems(Env()))
        assert errors                                         # «Null» — системийн алдаа хэвээр
        St.dialogs = ["Анхааруулга\nSSG1: Устгахдаа итгэлтэй байна уу?"]
        assert aio.run(ui_wait.popup_problems(Env())) == ([], [])     # асуулт
    finally:
        ui_wait.snapshot = orig



def test_variant_lists_are_not_candidates():
    """«… /дэвсгэртээр/», «/хөрөнгөөр/», «/ажилтнаар/» — үндсэн жагсаалт нь тусдаа цэс бол хасна (Accounting A13, A171)."""
    from types import SimpleNamespace as NS
    from app.models import MenuItem
    from app.runner.capabilities import crud_candidates
    mk = lambda name: NS(item=MenuItem("Систем", "Маягт", name, "/x/" + name), tab_opened=True, status="Pass",
                         caps={"crud": "full", "columns": ["№", "Огноо"], "buttons": []})
    names = [c["name"] for c in crud_candidates([mk("Тушаалын жагсаалт"), mk("Тушаалын жагсаалт /ажилтнаар/"),
                                                 mk("Ганц жагсаалт /тусгай/")])]
    assert names == ["Тушаалын жагсаалт", "Ганц жагсаалт /тусгай/"]     # үндсэнгүй хувилбар үлдэнэ


@e2e
def test_key_not_in_list_columns_is_not_created(tmp_path):
    """Түлхүүр талбар жагсаалтын баганад алга бол бичлэг ҮҮСГЭХГҮЙ — олж цэвэрлэх боломжгүй (Accounting A19, HR A148)."""
    from app.pipeline.stages import auto_flow_spec
    with CrudServer() as srv:
        srv.state.deps = True
        spec = auto_flow_spec(EMP, "a01", columns=["Төрсөн огноо"])
        pipe, cfg = make_pipe(srv, tmp_path, [{"kind": "crud", "flow": "x"}], [], extra={"flows": {"x": spec}})
        d = asyncio.run(pipe.run())
    st = d.flows[0]["steps"][0]
    assert st["status"] == M.NOT_TESTED and "баганад алга" in st["message"] and st["category"] == "tool", st
    assert not d.flows[0]["data"]


def test_fill_lines_header_shorter_than_cells():
    """IndexError: толгойн нүд мөрийн нүднээс цөөн (Accounting «Клиринг жагсаалт /хөрөнгөөр/»)."""
    import inspect
    from app.flows.ui import lines
    assert "i < len(headers)" in inspect.getsource(lines.fill_lines)


@e2e
def test_group_required_from_tree_is_selected_and_retried(deps_run):
    """«Харьяалагдах бүлэг сонгоогүй байна!» (формд бүлгийн талбаргүй) — зүүн талын модноос «Бүх» биш бүлгийг сонгоод
    дахин бүртгэнэ (testaccounting ~11 бүлгийн лавлах Blocked байсан)."""
    fl = deps_run["by"][TGRP]
    assert fl["status"] == M.PASS, steps_of(fl)
    assert fl["meta"]["group"] == "Санхүү"
    assert "бүлгийн модноос «Санхүү» сонгоод" in " ".join(fl["steps"][0]["notes"])


@e2e
def test_outage_mid_run_is_recovered_not_reported_as_bugs(tmp_path):
    """testaccounting 2026-10-21: апп эвдэрсний (цэсний мод алга, runtime «амьд») дараа 118 цэс дараалан «цэс
    олдсонгүй» гэж Fail болсон. Одоо: орчны тасалдлын шинжтэй урсгалыг апп-ыг дахин ачаалж дахин шалгана → Pass."""
    from tests.test_autocrud import CUSTOMERS, CATS
    outage = key_of("Мастер дата", "Бүртгэл", "Тасалдал", "/md/outagelist")
    with CrudServer() as srv:
        srv.state.crash_menu = True
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "autocrud", "menus": [outage, CUSTOMERS, CATS]}], [])
        data = asyncio.run(pipe.run())
    by = {f["meta"]["menu_key"]: f for f in data.flows if not f["meta"].get("setup")}
    cust = by[CUSTOMERS]
    assert cust["meta"].get("rechecked"), steps_of(cust)              # дахин ачаалж дахин шалгасан
    assert cust["status"] == M.PASS, steps_of(cust)
    assert not any("цэс олдсонгүй" in s["message"] for f in data.flows for s in f["steps"] if s["status"] == M.FAIL)


@e2e
def test_warning_name_matches_field_by_last_word(deps_run):
    """«Бүлэг сонгоогүй байна!» ↔ заавал биш «Харьяалагдах бүлэг» талбар (testaccounting ~15 цэс Blocked байсан)."""
    fl = deps_run["by"][SGRP]
    assert fl["status"] == M.PASS, steps_of(fl)
    assert "Анхааруулгын «Бүлэг» → формын «Харьяалагдах бүлэг» талбар" in " ".join(fl["steps"][0]["notes"])


@e2e
def test_lost_toolbar_after_app_error_is_recovered(tmp_path):
    """testhr 2026-10-22: «Хоолны меню»-ийн «Data is Null»-ийн дараа бүх жагсаалтын toolbar алга болж 68 цэс
    «Нэмэх товч олдсонгүй». Smoke Нэмэхтэй гэж бүртгэсэн цэст энэ нь орчны тасалдал — дахин ачаалж дахин шалгана."""
    tbloss = key_of("Мастер дата", "Бүртгэл", "Эвдэрдэг цэс", "/md/tbloss")
    with CrudServer() as srv:
        srv.state.deps = True
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "autocrud", "menus": [tbloss, CITY]}], [])
        data = asyncio.run(pipe.run())
    by = {f["meta"]["menu_key"]: f for f in data.flows if not f["meta"].get("setup")}
    city = by[CITY]
    assert city["meta"].get("rechecked") and city["status"] == M.PASS, steps_of(city)


@e2e
def test_leftover_not_in_own_list_is_swept_from_sibling_list(tmp_path):
    """testaccounting 2026-10-23: «ДҮ - ээс орлого» баримт өөрийн жагсаалтад харагдахгүй тул run бүр 5–7 тестийн бичлэг
    үлддэг байсан. Ижил модулийн, ижил үгээр эхэлсэн өөр жагсаалтаас олж устгана."""
    dua = key_of("Мастер дата", "Бүртгэл", "ДҮ баримтын жагсаалт", "/md/dua")
    dub = key_of("Мастер дата", "Бүртгэл", "ДҮ орлогын жагсаалт", "/md/dub")
    with CrudServer() as srv:
        srv.state.deps = True
        pipe, cfg = make_pipe(srv, tmp_path, [{"kind": "autocrud", "menus": [dub, dua]}], [])
        data = asyncio.run(pipe.run())
    by = {f["meta"]["menu_key"]: f for f in data.flows if not f["meta"].get("setup")}
    b = by[dub]
    assert b["meta"].get("swept") and not b["meta"].get("leftover"), b["meta"]
    s = generate_reports(data, cfg, tmp_path, verify=False)
    assert not s["leftovers"], s["leftovers"]
