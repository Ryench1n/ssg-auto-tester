"""Форм шалгалт (READ-ONLY) — mock CRUD сайт дээр smoke → formcheck.

Шалгах зүйл: цэс бүрийн Шинэ / засварын форм, lookup-уудыг нээж, системийн алдааг bug болгоно;
ЮУ Ч ХАДГАЛАХГҮЙ (сервер дээр created/updated/deleted өөрчлөгдөхгүй, бичлэгийн сум дарагдахгүй).
"""
from __future__ import annotations

import asyncio

import pytest

from app import models as M
from app.config import load_config
from app.pipeline.runner import PipelineRunner
from app.pipeline.stages import PipelineConfigError, form_flow_spec
from app.report.build import generate_reports
from app.runner.smoke import RunOptions
from tests.fixtures.mock_crud.server import CrudServer
from tests.test_autocrud import ACCT, CATS, CUSTOMERS, GROUPS, JOURNAL, TXN, e2e, make_pipe


def test_formcheck_requires_test_host_and_smoke(tmp_path):
    cfg = load_config("https://accounting.smartapp.mn/")
    with pytest.raises(PipelineConfigError, match="allowed_hosts"):
        PipelineRunner("x", "https://accounting.smartapp.mn/", cfg, RunOptions(project="X"), tmp_path,
                       [{"kind": "smoke"}, {"kind": "formcheck", "menus": None}])
    cfg = load_config("https://testaccounting.smartapp.mn/")
    with pytest.raises(PipelineConfigError, match="smoke"):
        PipelineRunner("x", "https://testaccounting.smartapp.mn/", cfg, RunOptions(project="X"), tmp_path,
                       [{"kind": "formcheck", "menus": None}])


def test_form_flow_spec():
    assert [s["uses"] for s in form_flow_spec("k", {"add": True})["steps"]] == [
        "form.check_edit", "form.check_new", "form.check_actions"]
    assert [s["uses"] for s in form_flow_spec("k", {"add": False, "edit": True})["steps"]] == [
        "form.check_edit", "form.check_actions"]
    assert all(s["always"] for s in form_flow_spec("k")["steps"])


@pytest.fixture(scope="module")
def checked(tmp_path_factory):
    with CrudServer() as srv:
        rd = tmp_path_factory.mktemp("fc")
        events: list = []
        holder: dict = {}
        offered: list = []

        def on_event(t, d):
            events.append((t, d))
            if t == "form_candidates":
                # UI-ийн хэрэглэгч smoke-ийн дараа цэс сонгоно: Журнал-аас бусад бүгд
                offered.extend(m["key"] for m in d["menus"])
                asyncio.get_running_loop().call_soon(holder["pipe"].select_forms,
                                                     [k for k in offered if k != JOURNAL])

        before = (srv.state.created, srv.state.updated, srv.state.deleted)
        pipe, cfg = make_pipe(srv, rd, [{"kind": "smoke"}, {"kind": "formcheck", "menus": None}], events, on_event)
        holder["pipe"] = pipe
        data = asyncio.run(pipe.run())
        summary = generate_reports(data, cfg, rd, verify=False)
        after = (srv.state.created, srv.state.updated, srv.state.deleted)
        yield {"data": data, "summary": summary, "events": events, "srv": srv, "before": before, "after": after,
               "offered": offered, "dir": rd}


def _step(fl, uses):
    return next(s for s in fl["steps"] if s["uses"] == uses)


def _flow(d, key):
    return next(f for f in d.flows if f["meta"]["menu_key"] == key)


@e2e
def test_formcheck_is_read_only(checked):
    assert checked["after"] == checked["before"]            # юу ч бүртгээгүй, засаагүй, устгаагүй
    assert checked["srv"].state.record_opened == 0          # lookup-ийн бичлэг нээх сум дарагдаагүй
    assert checked["srv"].state.untagged_touched == 0
    fc = next(p for p in checked["data"].pipeline if p["kind"] == "formcheck")
    assert "Error" not in (fc.get("message") or ""), fc            # ж: TypeError (caps dict-ийг hash хийх)


@e2e
def test_formcheck_results(checked):
    d = checked["data"]
    flows = {f["meta"]["menu_key"]: f for f in d.flows}
    assert all(f["code"].startswith("F") for f in d.flows)
    st = lambda key: [(s["uses"], s["status"]) for s in flows[key]["steps"]]
    # Ангилал: «Төрөл» lookup нээхэд ActiveStatus → Fail (bug)
    new = _step(flows[CATS], "form.check_new")
    assert any("ComboBox 1 — нээгдсэн: Төлөв" in n for n in new["notes"]), new["notes"]   # сонгоогүй
    assert new["status"] == M.FAIL and "ActiveStatus" in new["message"] and "'Төрөл'" in new["message"], new["message"]
    # Бүлэг: шинэ таб дээрх форм, 2 lookup ачаалагдсан — алдаагүй
    g = _step(flows[GROUPS], "form.check_new")
    assert g["status"] == M.PASS and any("Эцэг бүлэг" in n and "Бүлгийн төрөл" in n for n in g["notes"]), g
    # Гүйлгээ: «Шинэ» dropdown — баримтын төрөл бүрийн формыг нээсэн
    t = _step(flows[TXN], "form.check_new")
    assert t["status"] == M.PASS and any("Орлого" in n for n in t["notes"]), t
    # Данс: засварын форм нээгдсэнээс ~1с-ийн дараа CostCenterId → Fail (bug); шинэ форм хэвийн
    an, ae = _step(flows[ACCT], "form.check_new"), _step(flows[ACCT], "form.check_edit")
    assert an["status"] == M.PASS and ae["status"] == M.FAIL and "CostCenterId" in ae["message"], (an, ae)
    # Ангилал: жагсаалт энэ улирлаар шүүгдээд хоосон → огнооны шүүлтүүрийг сунгаж хуучин бичлэгийг нээсэн
    ce = _step(flows[CATS], "form.check_edit")
    assert ce["status"] == M.PASS and any("сунгав" in n for n in ce["notes"]), ce
    assert any("Үндсэн ангилал" in n for n in ce["notes"])
    # Харилцагч: энгийн форм — Pass
    assert all(s["status"] == M.PASS for s in flows[CUSTOMERS]["steps"])
    assert d.pipeline[2]["kind"] == "formcheck" and d.pipeline[2]["status"] == M.FAIL
    s = checked["summary"]
    assert any("ActiveStatus" in str(b) for b in s["bugs"])
    form_tcs = [t for t in s["tcs"] if t["id"].startswith("TC-FORM-F")]
    assert form_tcs and {t["module"] for t in form_tcs} == {"Мастер дата"}
    assert any("«Ангилал» цэс" in t["purpose"] for t in form_tcs)
    print({k.split("|")[2]: st(k) for k in flows})


@e2e
def test_formcheck_menu_selection_after_smoke(checked):
    """Smoke-ийн дараа нэр дэвшигчдийг UI-д санал болгож, зөвхөн сонгосныг шалгана."""
    from tests.test_autocrud import ITEMS
    assert JOURNAL in checked["offered"] and CUSTOMERS in checked["offered"]
    assert ITEMS not in checked["offered"]                       # CRUD товчгүй цэс санал болгогдохгүй
    ran = [f["meta"]["menu_key"] for f in checked["data"].flows]
    assert JOURNAL not in ran and sorted(ran) == sorted(k for k in checked["offered"] if k != JOURNAL)
    assert any(t == "status" and d["state"] == "awaiting_form_selection" for t, d in checked["events"])


@e2e
def test_formcheck_separate_report_files(checked):
    from openpyxl import load_workbook
    f = checked["summary"]["form"]
    assert f and f["xlsx"].endswith(".xlsx") and "Form-Check" in f["docx"]
    assert (checked["dir"] / f["xlsx"]).exists() and (checked["dir"] / f["docx"]).exists()
    assert f["menus"] == len(checked["data"].flows) and f["fail"] >= 2 and f["bugs"] >= 2
    wb = load_workbook(checked["dir"] / f["xlsx"])
    ids = [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row
           if isinstance(c.value, str) and c.value.startswith("TC-")]
    assert ids and all(i.startswith("TC-FORM-") for i in ids)       # зөвхөн форм шалгалтын TC


def test_tab_is_strict():
    """Restaurant: «Түлхүүр» таб идэвхтэй байхад «Түлхүүрийн төрөл»-ийг нээгдсэн гэж андуурч өмнөх цэсийг
    шалгадаг байсан (Меню ↔ Менюны загвар ч мөн)."""
    from app.runner.detectors import tab_is
    assert tab_is("Түлхүүр", "Түлхүүр") and tab_is(" Банк ", "банк")
    assert not tab_is("Түлхүүр", "Түлхүүрийн төрөл") and not tab_is("Менюны загвар", "Меню")
    assert tab_is("Гүйлгээний жагс…", "Гүйлгээний жагсаалт")


@e2e
def test_interrupted_formcheck_records_resume(tmp_path):
    """Session тасарсан / зогсоосон үед шалгаагүй цэсүүд Blocked биш «ажиллаагүй» болж resume-д бүртгэгдэнэ —
    UI «Үлдсэн цэсүүдийг үргэлжлүүлэх»-ээр зөвхөн тэдгээрийг дахин ажиллуулна."""
    with CrudServer() as srv:
        events: list = []
        holder: dict = {}
        menus = [CUSTOMERS, GROUPS, ACCT, CATS]

        def on_event(t, d):
            events.append((t, d))
            if t == "flow_step" and d["flow"].endswith("Бүлэг") and d["state"] == "running" and d["index"] == 1:
                holder["pipe"].cancel()          # 2 дахь цэсийн дунд зогсооно

        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": menus}], events, on_event)
        holder["pipe"] = pipe
        d = asyncio.run(pipe.run())
    ran = [f["meta"]["menu_key"] for f in d.flows]
    rest = d.resume.get("formcheck") or []
    assert ran and rest and ACCT in rest and CATS in rest
    assert not set(ran) & set(rest) and set(ran) | set(rest) == set(menus)
    PipelineRunner("x", srv.url, load_config(srv.url), RunOptions(project="X"), tmp_path / "r",
                   [{"kind": "formcheck", "menus": rest}])        # үргэлжлүүлэх run: smoke-гүй хүчинтэй


@e2e
def test_formcheck_lines_and_combos(tmp_path):
    """Журнал: мөрийн grid-д «+» мөр нэмж (хадгалахгүй) мөрийн lookup-ийг нээж шалгана; Данс: ComboBox."""
    with CrudServer() as srv:
        before = (srv.state.created, srv.state.updated, srv.state.deleted)
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [JOURNAL]}], [])
        d = asyncio.run(pipe.run())
        assert (srv.state.created, srv.state.updated, srv.state.deleted) == before
        assert srv.state.untagged_touched == 0
    j = _step(d.flows[0], "form.check_new")
    assert j["status"] == M.PASS, j
    assert any("мөрийн grid" in n and "lookup ачаалагдсан" in n for n in j["notes"]), j["notes"]


@e2e
def test_formcheck_fast_with_never_quiet_network(tmp_path, monkeypatch):
    """Бодит SmartApp-д байнга нээлттэй (long-poll) хүсэлт байдаг тул сүлжээ «тайван» болохгүй. Урьд нь settle
    алхам бүрт loading_blocked_ms (30 сек) хүртэл хүлээж, HR-д шинэ форм ~43 сек болдог байсан."""
    import time
    with CrudServer(longpoll=True) as srv:
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [CUSTOMERS, ACCT]}], [])
        pipe.cfg["timing"]["loading_blocked_ms"] = 30000          # production-ийн утга
        from app.flows import ui as ui_mod
        closed: list = []
        real_close = ui_mod.close_tabs

        async def spy(env, names):
            closed.append(list(names))
            await real_close(env, names)
        monkeypatch.setattr(ui_mod, "close_tabs", spy)
        t0 = time.monotonic()
        d = asyncio.run(pipe.run())
        took = time.monotonic() - t0
    durs = [s["duration_ms"] / 1000 for f in d.flows for s in f["steps"]]
    assert len(d.flows) == 2 and all(s["status"] != M.BLOCKED for f in d.flows for s in f["steps"]), d.flows
    assert max(durs) < 12, durs     # long-poll «ачаалал» биш (засварын формын lookup шалгалт нэмэгдсэн)
    # Цэс бүрийн таб шалгалтын дараа хаагдана (хуримтлагдвал апп удааширдаг)
    assert ["Харилцагч"] in closed and ["Данс"] in closed, closed
    assert all(f["meta"].get("seconds") for f in d.flows)
    assert "сек/цэс" in d.pipeline[1]["message"]
    print(f"2 цэс {took:.1f} сек, алхмууд {durs}")
    print([n for f in d.flows for s in f["steps"] for n in s["notes"] if n.startswith("⏱")])


@e2e
def test_blazor_crash_reloads_app_and_next_menus_work(tmp_path):
    """HR «Робот (RPA)»: Шинэ форм Blazor компонентыг унагаж апп эвдэрсэн → дараагийн бүх цэс 60 сек хүлээгээд
    «таб нээгдсэнгүй» болдог байсан. Одоо уналтын дараа апп-ыг дахин ачаалж үргэлжилнэ."""
    import time
    robot = "Мастер дата|Бүртгэл|Робот|/md/robot"
    with CrudServer(crash_menu=True) as srv:
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [robot, ACCT, CUSTOMERS]}], [])
        t0 = time.monotonic()
        d = asyncio.run(pipe.run())
        took = time.monotonic() - t0
    fl = {f["meta"]["menu_key"]: f for f in d.flows}
    print({k: [(s["status"], s["message"][:120]) for s in f["steps"]] for k, f in fl.items()}, d.pipeline, d.resume)
    new = _step(fl[robot], "form.check_new")
    assert new["status"] == M.FAIL and "Blazor компонент унав" in new["message"], new
    assert fl[robot]["meta"].get("reloaded")
    for k in (ACCT, CUSTOMERS):
        msgs = [s["message"] for s in fl[k]["steps"]]
        assert not any("таб нээгдсэнгүй" in m for m in msgs), msgs
    assert all(s["status"] == M.PASS for s in fl[CUSTOMERS]["steps"])
    assert took < 60, took


@e2e
def test_formcheck_improvements(checked):
    """(1) Хоосон жагсаалт = N/A (TC/Blocked биш); (3) формын дотоод табууд; (4) засварын формын lookup/өгөгдөл;
    (5) хоосон lookup → Minor анхааруулга (алхам Pass); (6) bug үндсэн шалтгаанаар."""
    d, s = checked["data"], checked["summary"]
    flows = {f["meta"]["menu_key"]: f for f in d.flows}
    # (1) Бүлэг/Гүйлгээ: жагсаалт хоосон → nodata ангилал, урсгалын төлөв түүнээс болж Blocked болохгүй
    g_edit = _step(flows[GROUPS], "form.check_edit")
    assert g_edit["status"] == M.BLOCKED and g_edit["category"] == "nodata"
    assert flows[GROUPS]["status"] == M.PASS
    tc_ids = {t["id"] for t in s["tcs"]}
    assert f"TC-FORM-{flows[GROUPS]['code']}-{g_edit['index']:03d}" not in tc_ids      # N/A — TC биш
    # (3) Данс: Шинэ форм «Нэмэлт» таб дээрх ComboBox «Ангилал» шалгагдсан
    an = _step(flows[ACCT], "form.check_new")
    assert any("2 дотоод таб" in n for n in an["notes"]), an["notes"]
    assert any("Нэмэлт" in n and "Ангилал" in n for n in an["notes"]), an["notes"]
    # (4)+(5) Харилцагч: засварын форм дээр ч «Бүс» lookup нээгдсэн; хоосон → анхааруулга, алхам Pass
    ce = _step(flows[CUSTOMERS], "form.check_edit")
    cn = _step(flows[CUSTOMERS], "form.check_new")
    assert ce["status"] == M.PASS and cn["status"] == M.PASS
    assert any("Засварын форм" in n and "Бүс" in n for n in ce["notes"]), ce["notes"]
    assert any(f["kind"] == M.FORM_WARNING and "Бүс" in f["text"] for f in cn["findings"])
    # Хоосон жагсаалтууд НЭГ ажиглалт болж нэгтгэгдэнэ (тест орчны лавлах өгөгдөл дутуу)
    warn_bugs = [b for b in s["bugs"] if "лавлах өгөгдөл дутуу" in b["title"]]
    assert len(warn_bugs) == 1 and warn_bugs[0]["severity"].endswith("Minor"), [b["title"] for b in s["bugs"]]
    assert "«Бүс»" in warn_bugs[0]["actual"]
    assert not any("«Бүс» сонголтын жагсаалт хоосон" in b["title"] for b in s["bugs"])
    # (6) ActiveStatus → үндсэн шалтгаанаар нэрлэгдсэн bug
    assert any(b["title"].startswith("Форм шалгалт: Invalid column name 'ActiveStatus'") for b in s["bugs"])


def test_root_cause_groups_same_error_across_menus():
    from app.models import RunData
    from app.runner.classify import build_bugs, build_test_cases

    def flow(code, menu, msg):
        return {"flow": f"form: {menu}", "code": code, "run_tag": "T", "status": M.FAIL,
                "meta": {"formcheck": True, "menu": menu, "menu_key": menu},
                "steps": [{"index": 1, "uses": "form.check_new", "name": "Шинэ форм", "status": M.FAIL,
                           "message": msg, "findings": [{"kind": M.SYSTEM_ERROR, "text": msg}], "notes": []}]}
    run = RunData("r", "https://testhr.smartapp.mn/", "HR", login_ok=True)
    run.flows = [flow("F01", "Цалин / Ажилбар", "'Ажилтан' lookup: Invalid column name 'ActiveStatus'."),
                 flow("F02", "Цалин / Амралт", "'Ажилтан' lookup: Invalid column name 'ActiveStatus'."),
                 flow("F03", "Цалин / Хэлтэс", "Invalid column name 'DistrictCode'. Invalid column name 'DistrictName'.")]
    cfg = __import__("app.config", fromlist=["load_config"]).load_config(run.url)
    tcs, _ = build_test_cases(run, cfg, "Э.Ренчин", "2026-10-08")
    bugs = [b for b in build_bugs(run, tcs, cfg) if b.signature.startswith("formroot|")]
    titles = sorted(b.title for b in bugs)
    assert titles[0].startswith("Форм шалгалт: Invalid column name 'ActiveStatus' — 2 цэс"), titles
    active = next(b for b in bugs if "ActiveStatus" in b.title)
    assert len(active.tc_ids) == 2 and "Цалин / Ажилбар" in active.actual and "Цалин / Амралт" in active.actual
    assert sum("DistrictCode" in t or "DistrictName" in t for t in titles) == 2


@e2e
def test_direct_form_menu(tmp_path):
    """Accounting «Нэхэмжлэх», «Гүйлгээ», «Эхний үлдэгдэл» шиг ШУУД форм цэс: урьд нь «Шинэ дарахад форм
    нээгдсэнгүй» (Blocked) болж, засварын шалгалт формын ОГНОО-г жагсаалтын шүүлтүүр гэж 2021-01-01 болгодог байсан."""
    inv = "Мастер дата|Бүртгэл|Нэхэмжлэх|/md/invoice"
    with CrudServer(rich=True) as srv:
        holder: dict = {}
        due: list = []

        def on_event(t, d):
            if t == "flow_step" and d.get("state") != "running" and d.get("index") == 2:
                due.append(1)

        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [inv]}], [], on_event)
        holder["pipe"] = pipe
        d = asyncio.run(pipe.run())
    fl = d.flows[0]
    ed, nw = _step(fl, "form.check_edit"), _step(fl, "form.check_new")
    assert ed["status"] == M.BLOCKED and ed["category"] == "na", ed
    assert nw["status"] == M.PASS and any("Шууд форм цэс" in n for n in nw["notes"]), nw
    assert any("Харилцагч" in n for n in nw["notes"]), nw["notes"]           # формын lookup шалгагдсан
    assert fl["status"] == M.PASS
    assert not any("сунгав" in n for n in ed.get("notes") or [])              # огноог өөрчлөөгүй


def test_form_dialog_with_error_word_in_labels_is_not_error():
    """HR «Цалингийн загвар»: формын шошгонд «Томьёо алдаатай» байсныг алдааны цонх гэж андуурч худал Fail."""
    import asyncio as aio
    from app.config import load_config
    from app.flows import ui

    class St:
        dialogs = ["Цалин бодох загвар\nКод *\nНэр *\nГүйлгээний төрөл\nТомьёо оруулаагүй\nТомьёо алдаатай\n"
                   "Идэвхтэй эсэх\nКод\nНэр\nЦалин бодолтод оруулах"]
        toasts: list = []

    class Env:
        cfg = load_config("http://x/")
        page = None

    async def fake_snapshot(page, cfg):
        return St()
    from app.flows.ui import wait as ui_wait
    orig = ui_wait.snapshot
    ui_wait.snapshot = fake_snapshot          # popup_problems нь ui.wait модульд
    try:
        errors, _ = aio.run(ui.popup_problems(Env()))
        assert errors == []
        St.dialogs = ["Cистемийн алдаа\nInvalid object name 'JournalTemplate'."]
        errors, _ = aio.run(ui.popup_problems(Env()))
        assert len(errors) == 1
    finally:
        ui_wait.snapshot = orig


@e2e
def test_new_via_toolbar_dropdown_and_na_flow_status(tmp_path):
    """HR «Үйл ажиллагааны загвар»: «Шинэ» нь юу ч хийдэггүй, төрлийг «… ▾» dropdown-аас сонгож үүсгэнэ — урьд нь
    «хэрэгсэл таньсангүй» (Blocked). Мөн зөвхөн N/A алхамтай урсгал Blocked биш «N/A»."""
    act = "Мастер дата|Бүртгэл|Үйл ажиллагааны загвар|/md/act"
    with CrudServer(rich=True) as srv:
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [act]}], [])
        d = asyncio.run(pipe.run())
        touched = srv.state.untagged_touched
    assert touched == 0                                     # «Баримт устгах» хэзээ ч дарагдаагүй
    fl = d.flows[0]
    nw = _step(fl, "form.check_new")
    assert nw["status"] == M.PASS, nw
    assert any("dropdown-аар шинэ бичлэг" in n for n in nw["notes"]), nw["notes"]
    assert any("Тогтмол" in n and "Нэг удаагийн" in n for n in nw["notes"]), nw["notes"]
    assert any("дараагүй" in n and "Баримт устгах" in n for n in nw["notes"]), nw["notes"]
    assert _step(fl, "form.check_edit")["category"] == "nodata" and fl["status"] == M.PASS


def test_flow_status_all_na():
    from app.flows.context import FlowRun, StepResult
    r = FlowRun(run_id="x", url="u", flow="form: X", run_tag="T")
    r.steps = [StepResult(index=1, uses="form.check_edit", name="e", status=M.BLOCKED, category="nodata")]
    assert r.status == M.NA


def test_hr_run5_patterns(tmp_path):
    """HR run f5a22365: (1) «Робот (RPA)»-ийн timer таб хаагдсаны дараа runtime-ийг унагаж, дараагийн «Компани»
    цэс «олдсонгүй» гэж худал Critical болсон → уналт өмнөх цэсийн bug, дараагийн цэс дахин шалгагдана;
    (2) master-detail жагсаалт: «SL групп» бүлгийн мөрийг засах бичлэг гэж андуурсан; «Шинэ» бүлэг сонгосны дараа
    ажилладаг; (3) «Мэдээлэл татах боломжгүй — Cannot create a DbSet» цонх → click timeout биш, системийн алдаа."""
    timer = "Мастер дата|Бүртгэл|Таймер|/md/timerlist"
    emp = "Мастер дата|Бүртгэл|Ажилтны жагсаалт|/md/employees"
    dbset = "Мастер дата|Бүртгэл|Тодорхойлолтын загвар|/md/deftemplate"
    with CrudServer(crash_menu=True) as srv:
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [timer, CUSTOMERS, emp, dbset]}], [])
        d = asyncio.run(pipe.run())
    fl = {f["meta"]["menu_key"]: f for f in d.flows}
    print({k: [(s["status"], s.get("category"), s["message"][:150], s["notes"][:3]) for s in f["steps"]]
           for k, f in fl.items()})
    # (1) уналт «Таймер»-т хамаарна; «Харилцагч» эвдэрсэн апп дээр биш, хэвийн шалгагдана
    assert fl[timer]["status"] == M.FAIL
    msg = next(s["message"] for s in fl[timer]["steps"] if "хаасны дараа апп унав" in s["message"])
    assert "ObjectDisposedException" in msg and "TimerList" in msg, msg
    close = _step(fl[timer], "form.close_tab")          # тусдаа алхам — Шинэ/Хэвлэх алхмын үр дүн өөрчлөгдөөгүй
    assert close["status"] == M.FAIL and close["name"] == "Цэсийн табыг хаах"
    assert _step(fl[timer], "form.check_new")["status"] == M.PASS
    assert fl[CUSTOMERS]["status"] == M.PASS, [(s["status"], s["message"]) for s in fl[CUSTOMERS]["steps"]]
    # (2) засварын шалгалт: бүлгийн мөр биш — үндсэн жагсаалт хоосон → N/A; Шинэ: бүлэг сонгоод форм нээгдэнэ
    ee, en = _step(fl[emp], "form.check_edit"), _step(fl[emp], "form.check_new")
    assert ee["status"] == M.BLOCKED and ee["category"] == "nodata", ee
    assert en["status"] == M.PASS, en
    # (3) DbSet цонх → Fail (алдааны текстээр), «дарж чадсангүй»/tool биш
    msgs = [s["message"] for s in fl[dbset]["steps"]]
    assert fl[dbset]["status"] == M.FAIL and any("DbSet" in m for m in msgs), msgs
    assert not any("дарж чадсангүй" in m for m in msgs), msgs


def test_formcheck_actions_rows_and_test_data(tmp_path):
    """(D) Уншилтын товч: Хэвлэх (window.print) / Excel (файл татах) дарагдаж алдаагүй, «Excel-ээс импорт» ХЭЗЭЭ Ч
    дарагдахгүй, Данс «Хэвлэх» → FileNotFoundException Fail; (E) засварын горимд 2 дахь бичлэгийг ч нээнэ;
    (F) Form Check xlsx-д «Тест өгөгдөл» хуудас (хоосон лавлах жагсаалт)."""
    from openpyxl import load_workbook
    with CrudServer() as srv:
        srv.state.customers.append({"id": 2, "code": "C-0002", "name": "Хоёр дахь харилцагч", "reg": "7654321",
                                    "phone": "70002222"})
        srv.state.seq = max(srv.state.seq, 2)
        pipe, cfg = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [CUSTOMERS, ACCT]}], [])
        d = asyncio.run(pipe.run())
        touched = srv.state.untagged_touched
        summary = generate_reports(d, cfg, tmp_path, verify=True)
    fl = {f["meta"]["menu_key"]: f for f in d.flows}
    ca = _step(fl[CUSTOMERS], "form.check_actions")
    print(ca["status"], ca["message"], ca["notes"])
    assert touched == 0                                       # «Excel-ээс импорт» дарагдаагүй
    assert ca["status"] == M.PASS, ca
    note = next(n for n in ca["notes"] if n.startswith("Уншилтын товч"))
    assert "хэвлэх цонх" in note and "файл татах" in note and "импорт" not in note, note
    aa = _step(fl[ACCT], "form.check_actions")
    assert aa["status"] == M.FAIL and "FileNotFoundException" in aa["message"], aa
    ce = _step(fl[CUSTOMERS], "form.check_edit")
    assert ce["status"] == M.PASS and any("Нэмэлт 1 бичлэгийг" in n and "C-0002" in n for n in ce["notes"]), ce["notes"]
    wb = load_workbook(tmp_path / summary["form"]["xlsx"])
    assert "Тест өгөгдөл" in wb.sheetnames
    vals = [c.value for row in wb["Тест өгөгдөл"].iter_rows() for c in row]
    assert any("Бүс" in str(v) for v in vals), vals


def test_previous_form_na(tmp_path, monkeypatch):
    """Баг тест өгөгдөл оруулсны дараа: өмнөх run-ий засварын форм N/A (nodata) байсан цэсүүдийг л дахин сонгох."""
    import json
    from app import api

    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    flows = [
        {"meta": {"formcheck": True, "menu_key": "A|B|Хоосон|x"},
         "steps": [{"uses": "form.check_edit", "status": "Blocked", "category": "nodata"}]},
        {"meta": {"formcheck": True, "menu_key": "A|B|Шууд форм|y"},
         "steps": [{"uses": "form.check_edit", "status": "Blocked", "category": "na"}]},
        {"meta": {"formcheck": True, "menu_key": "A|B|Хэвийн|z"},
         "steps": [{"uses": "form.check_edit", "status": "Pass", "category": ""}]},
    ]
    (run_dir / "raw.json").write_text(json.dumps({"flows": flows}), encoding="utf-8")

    class FakeStore:
        def list(self, n):
            return [{"id": "now", "host": "testhr.smartapp.mn", "status": "running"},
                    {"id": "r1", "host": "testhr.smartapp.mn", "status": "done"}]

        def get(self, rid):
            return {"version_label": "v1.0"}

    monkeypatch.setattr(api, "store", FakeStore())
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path)
    r = api.previous_form_na("https://testhr.smartapp.mn/", exclude="now")
    assert r == {"run_id": "r1", "version": "v1.0", "keys": ["A|B|Хоосон|x"]}
    assert api.previous_form_na("https://other.smartapp.mn/")["keys"] == []



def test_environment_outage_not_reported_as_fail(tmp_path):
    """HR run b67e: тест орчин түр тасарч (цэсний мод алга, «Failed to fetch») дараагийн цэсүүд «цэс олдсонгүй»
    гэж худал Fail болсон → одоо апп-ыг дахин ачаалж дахин шалгана, Fail бүртгэхгүй."""
    from app.pipeline.stages import infra_broken
    outage = "Мастер дата|Бүртгэл|Тасалдал|/md/outagelist"
    with CrudServer(crash_menu=True) as srv:
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [outage, CUSTOMERS]}], [])
        d = asyncio.run(pipe.run())
    fl = {f["meta"]["menu_key"]: f for f in d.flows}
    msgs = [s["message"] for f in d.flows for s in f["steps"]]
    assert not any("цэс олдсонгүй" in m for m in msgs), msgs
    assert fl[CUSTOMERS]["status"] == M.PASS and fl[CUSTOMERS]["meta"].get("rechecked"), fl[CUSTOMERS]


def test_infra_broken_classifier():
    from app.flows.context import FlowRun, StepResult
    from app.pipeline.stages import infra_broken

    def run(*steps):
        return FlowRun(run_id="r", url="u", flow="f", run_tag="t", steps=[
            StepResult(index=i, uses="u", name="n", status=st, message=msg, category=cat)
            for i, (st, msg, cat) in enumerate(steps, 1)])
    assert infra_broken(run((M.FAIL, "'X' цэс олдсонгүй", ""), (M.BLOCKED, "'X' цэсийг дарж чадсангүй: Timeout", ""),
                            (M.BLOCKED, "Уншилтын товч алга", "na")))
    assert infra_broken(run((M.FAIL, "'X' цэсийг нээхэд: Анхааруулга Сервертэй холбогдож чадсангүй. TypeError: Failed to fetch", "")))
    # жинхэнэ аппын алдаа холилдсон бол орчны тасалдал биш
    assert not infra_broken(run((M.FAIL, "'X' цэс олдсонгүй", ""), (M.FAIL, "Invalid object name 'T'", "")))
    assert not infra_broken(run((M.PASS, "", ""), (M.FAIL, "'X' цэс олдсонгүй", "")))



def test_accounting_patterns_tab_alias_and_save_new_form(tmp_path):
    """testaccounting run a8972: (1) табын гарчиг цэсийн нэрээс өөр («Насжилтын интервал») — худал «таб нээгдсэнгүй /
    орчин тасарсан» биш; (2) «Хадгалах & Шинэ» товчтой шууд форм цэс — «Шинэ нээгдсэнгүй» (tool) биш, формыг шалгана,
    ХАДГАЛАХ товч ХЭЗЭЭ Ч дарагдахгүй."""
    alias = "Мастер дата|Бүртгэл|Насжилтын интервалын жагсаалт|/md/aliaslist"
    dform = "Мастер дата|Бүртгэл|Хөрөнгийн эхний үлдэгдэл|/md/openingbalance"
    with CrudServer(crash_menu=True) as srv:
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [alias, dform]}], [])
        d = asyncio.run(pipe.run())
        touched = srv.state.untagged_touched
    fl = {f["meta"]["menu_key"]: f for f in d.flows}
    print({k: [(s["status"], s.get("category"), s["message"][:120]) for s in f["steps"]] for k, f in fl.items()})
    assert touched == 0
    an = _step(fl[alias], "form.check_new")
    assert an["status"] == M.PASS, an
    assert not any("таб нээгдсэнгүй" in s["message"] for s in fl[alias]["steps"])
    dn, de = _step(fl[dform], "form.check_new"), _step(fl[dform], "form.check_edit")
    assert dn["status"] == M.PASS and any("Шууд форм" in n for n in dn["notes"]), dn
    assert de["category"] == "na", de



def test_lookup_cache_per_module(tmp_path):
    """Хурд: нэг модульд ижил нэртэй lookup-ийг (ж: «Бүс») нэг л удаа шалгана — засварын формд шалгасан бол Шинэ
    формд алгасна (Accounting: 1 594 lookup-ийн 1 460 нь давталт байсан)."""
    with CrudServer() as srv:
        pipe, _ = make_pipe(srv, tmp_path, [{"kind": "formcheck", "menus": [CUSTOMERS]}], [],
                            extra={"flow": {"lookup_repeat_per_module": 1}})
        d = asyncio.run(pipe.run())
    fl = d.flows[0]
    ce, cn = _step(fl, "form.check_edit"), _step(fl, "form.check_new")
    assert ce["status"] == M.PASS and cn["status"] == M.PASS
    assert any("Бүс" in n and "Засварын форм" in n for n in ce["notes"]), ce["notes"]
    assert any("өмнө шалгагдсан" in n and "Бүс" in n for n in cn["notes"]), cn["notes"]
