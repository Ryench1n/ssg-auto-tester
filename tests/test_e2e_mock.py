"""Mock SPA (tests/fixtures/mock_site) дээр бүх илрүүлэгч + recovery-г төгсгөлөөс-төгсгөл шалгана.

Mock сайтын цэсүүд:
  Журнал / Роль / Сесс шалгах → хэвийн; Робот (RPA) → static CSS 404 (screenshot-тай)
  Дансны төлөвлөгөө → HTTP 500 + "Системийн алдаа" модал (+ хориотой "Хадгалах" товч)
  Ерөнхий журнал    → location.reload()
  Мөнгөн гүйлгээ    → эхний удаа 503 + msdeploy, дараа нь хэвийн
  Гүйлгээний тайлан → таб хаасны ДАРАА ObjectDisposedException
  Хэрэглэгч         → Unhandled Exception, UI зогсоно
  Эрх               → _403 таб
  Session TTL       → 14 сек-ийн дараа /auth/login руу шилжүүлнэ

Smoke нь PipelineRunner-оор (нэвтрэх → smoke) ажиллана. Хүний гар нэвтрэлтийг тестийн
HumanLogin hook дуурайна.
"""
import asyncio
import os
import shutil

import pytest

from app import models as M
from app.config import load_config
from app.pipeline.runner import PipelineRunner
from app.report.build import generate_reports
from app.runner.smoke import RunOptions
from tests.fixtures.mock_site.server import MockServer
from tests.human_login import HumanLogin

pytestmark = pytest.mark.skipif(os.environ.get("SSG_SKIP_E2E") == "1", reason="SSG_SKIP_E2E=1")

FAST = {"timing": {"wait_ms": 1200, "post_close_wait_ms": 800, "crash_settle_ms": 800,
                   "deploy_retry_wait_ms": 1000, "loading_blocked_ms": 4000,
                   "app_ready_timeout_ms": 10000, "login_timeout_ms": 10000, "item_timeout_s": 25}}
PASSWORD = "Secr3t-Pass!"


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    rd = tmp_path_factory.mktemp("e2e")
    # speed_brain=True: Cloudflare-ийн адил prefetch-д 503 буцаана — илрүүлэгч үүнийг үл тоох ёстой
    with MockServer(session_ttl_s=14, speed_brain=True) as srv:
        cfg = load_config(srv.url, FAST)
        events: list[tuple[str, dict]] = []
        human = HumanLogin("qa", PASSWORD, "SSG")
        pipe = PipelineRunner("e2e", srv.url, cfg, RunOptions(project="Mock"), rd, steps=[{"kind": "smoke"}],
                              emit=lambda t, d: events.append((t, d)), headless=True, on_waiting_login=human)
        data = asyncio.run(pipe.run())
        summary = generate_reports(data, cfg, rd, verify=bool(shutil.which("soffice")))
        yield {"data": data, "summary": summary, "state": srv.state, "dir": rd, "events": events,
               "human": human}


def _res(e2e, name):
    return next(r for r in e2e["data"].results if r.item.name == name)


def _kinds(e2e, name):
    return {f.kind for f in _res(e2e, name).findings}


def test_login_and_discovery(e2e):
    d = e2e["data"]
    assert d.login_ok and d.app_version == "v2.4.1.7"
    assert d.discovered == {"Ерөнхий дэвтэр": 8, "Систем": 6}


def test_normal_items_pass(e2e):
    for n in ("Журнал", "Роль"):
        assert _res(e2e, n).status == M.PASS, n


def test_system_error_modal_and_5xx(e2e):
    r = _res(e2e, "Дансны төлөвлөгөө")
    assert r.status == M.FAIL and {M.SYSTEM_ERROR, M.HTTP_5XX} <= _kinds(e2e, "Дансны төлөвлөгөө")
    assert r.confirmation.startswith("Баталгаажсан")
    assert r.screenshot


def test_network_only_error_has_screenshot(e2e):
    """Зөвхөн сүлжээний алдаатай (static CSS 404) цэс — алдааг табыг хаасны дараа цуглуулдаг тул урьд нь
    screenshot-гүй үлддэг байсан (testbalancelite «Тайлан» модуль)."""
    r = _res(e2e, "Робот (RPA)")
    assert M.HTTP_4XX in _kinds(e2e, "Робот (RPA)")
    assert r.screenshot, "screenshot алга"


def test_devexpress_popup_closed_by_x(e2e):
    """role=dialog-гүй DxPopup (<dxbl-modal-root> overlay) → Fail; ✕-ээр хаагдаж дараагийн цэс
    саадгүй дарагдана (өмнө нь "intercepts pointer events" → Blocked болдог байсан)."""
    r = _res(e2e, "Эрсдэлийн бүртгэл")
    assert r.status == M.FAIL and M.SYSTEM_ERROR in _kinds(e2e, "Эрсдэлийн бүртгэл"), r.findings
    assert any("RiskStatus" in f.text for f in r.findings)
    nxt = _res(e2e, "Сесс шалгах")
    assert nxt.status == M.PASS and not nxt.findings, (nxt.blocked_reason, nxt.findings)


def test_page_reload_detected(e2e):
    assert M.PAGE_RELOAD in _kinds(e2e, "Ерөнхий журнал")


def test_deploy_503_retried(e2e):
    r = _res(e2e, "Мөнгөн гүйлгээ")
    assert r.status == M.PASS
    assert "Мөнгөн гүйлгээ" in e2e["data"].temp_5xx


def test_speculative_prefetch_503_ignored(e2e):
    """Cloudflare Speed Brain-ийн prefetch 503 нь алдаа биш: retry, finding, Blocked үүсгэхгүй —
    цэс эхний оролдлогоор шууд дүгнэгдэнэ."""
    if e2e["state"].prefetch_hits == 0:
        pytest.skip("энэ Chromium хувилбар speculative prefetch хийсэнгүй")
    # зөвхөн жинхэнэ /api/deploy 503-тай цэс л дахин оролдогдоно
    assert e2e["data"].temp_5xx == ["Мөнгөн гүйлгээ"]
    for n in ("Журнал", "Роль", "Сесс шалгах"):
        r = _res(e2e, n)
        assert r.status == M.PASS and not r.findings, n
    # prefetch-ийн 503 нэг ч цэсэнд http_5xx болж бүртгэгдээгүй (зөвхөн жинхэнэ /api/fail-ынх)
    bad = [r.item.name for r in e2e["data"].results
           if any(f.kind == M.HTTP_5XX for f in r.findings)]
    assert sorted(bad) == ["Дансны жагсаалт", "Дансны төлөвлөгөө"], bad


def test_initial_tab_kept(e2e):
    """Анхнаасаа нээлттэй таб (цэсээр нээгдээгүй)-ыг smoke хаахгүй — зөвхөн шалгасан цэсний табыг хаана."""
    assert e2e["state"].initial_tab_closed == 0


def test_runtime_crash_and_recovery(e2e):
    assert M.RUNTIME_CRASH in _kinds(e2e, "Хэрэглэгч")
    assert any(c["item"] == "Хэрэглэгч" and c["recovered"] for c in e2e["data"].crashes)


def test_late_crash_after_tab_close(e2e):
    r = _res(e2e, "Гүйлгээний тайлан")
    assert any(f.kind == M.RUNTIME_CRASH and f.after_close for f in r.findings)


def test_forbidden(e2e):
    assert M.FORBIDDEN in _kinds(e2e, "Эрх")


def test_risky_item_checked_last(e2e):
    # эхний 10 үр дүн = үндсэн давталт (дараагийнх нь давтан баталгаажуулалт биш — results-д нэмэгддэггүй)
    names = [r.item.name for r in e2e["data"].results]
    assert names[-1] == "Робот (RPA)"


def test_session_logout_pauses_for_manual_relogin(e2e):
    """Session тасрахад run түр зогсож waiting_login мэдэгдэл гаргана; "хүн" дахин нэвтэрмэгц
    тасарсан цэснээс үргэлжилнэ."""
    d = e2e["data"]
    assert len(d.logouts) >= 1
    assert e2e["state"].logins >= 2
    waits = [ev for t, ev in e2e["events"] if t == "waiting_login"]
    assert len(waits) >= 2 and any("session тасарсан" in w["reason"] for w in waits)
    assert all(w["timeout_s"] > 0 for w in waits)
    assert e2e["human"].calls == len(waits)
    assert d.pipeline[1]["kind"] == "smoke" and d.pipeline[1]["status"] in (M.FAIL, M.PASS)
    assert any(b["title"].startswith("Session") for b in e2e["summary"]["bugs"])


def test_read_only_no_mutations(e2e):
    assert e2e["state"].mutations == 0


def test_password_never_written(e2e):
    for p in e2e["dir"].rglob("*"):
        if p.is_file() and p.suffix in (".log", ".json", ".jsonl"):
            assert PASSWORD not in p.read_text(encoding="utf-8", errors="ignore"), p


def test_reports_created(e2e):
    s = e2e["summary"]
    assert (e2e["dir"] / "Mock-Test-Cases-v1.0.xlsx").exists()
    assert (e2e["dir"] / "Mock-Test-Report-v1.0.docx").exists()
    assert s["decision"] == "NO-GO"
    assert s["bug_counts"]["critical"] >= 2
    if shutil.which("soffice"):
        assert s["verification"] == {"xlsx": "ok", "docx": "ok"}


def test_soft_findings_pass_with_warning(e2e):
    """Minor консол алдаа нь цэсийг Fail болгохгүй — Pass + анхааруулга, bug-ийн жагсаалтад 🟢 Minor хэвээр."""
    r = _res(e2e, "Консол анхааруулга")
    assert r.status == M.PASS and M.CONSOLE_ERROR in _kinds(e2e, "Консол анхааруулга"), r.findings
    assert any("NestedSettingsHelper" in b["title"] for b in e2e["summary"]["bugs"])


def test_blank_page_detected(e2e):
    r = _res(e2e, "Хоосон хуудас")
    assert r.status == M.FAIL and M.BLANK_PAGE in _kinds(e2e, "Хоосон хуудас"), r.findings
    # хэвийн цэс цагаан дэлгэц гэж худал илрэхгүй
    assert M.BLANK_PAGE not in _kinds(e2e, "Журнал")


def test_duplicate_menu_counted_once(e2e):
    """Нэг хуудас хоёр модульд цэс болж давхардсан бол bug-ийн давтамж хуудсаар (1) тоологдоно."""
    b = next(b for b in e2e["summary"]["bugs"] if "Системийн алдааны цонх" in b["title"])
    assert len(b["tcs"]) == 2 and "нэг хуудас" in b["actual"] and "давхардсан" in b["actual"], b["actual"]
