import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import models as M  # noqa: E402
from app.models import Finding, ItemResult, MenuItem, RunData, TimelineEvent  # noqa: E402

from app.report.verify import soffice_bin  # noqa: E402

HAS_SOFFICE = bool(soffice_bin())
needs_soffice = pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice суугаагүй")

TEST_PASSWORD = "test-password-1"


def login_client(api, store, username: str = "tester", is_admin: bool = False):
    """api.store-д хэрэглэгч үүсгээд нэвтэрсэн TestClient буцаана (бүх API нэвтрэлт шаарддаг)."""
    from fastapi.testclient import TestClient

    from app import auth
    if not store.get_user(username):
        store.create_user(username, auth.hash_password(TEST_PASSWORD, iterations=1000), "", is_admin)
    c = TestClient(api.app)
    r = c.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    return c


def make_run(n: int = 20, fails: dict | None = None) -> RunData:
    """Синтетик run: n цэс; fails = {index: [Finding, ...]}."""
    run = RunData("t", "https://testaccounting.smartapp.mn/", "Accounting", qa_lead="Оюунаа",
                  login_ok=True, app_version="v26.2.0001.0001",
                  started_at="2026-09-22T10:00:00", finished_at="2026-09-22T10:40:00")
    run.modules_selected = ["Систем", "Мастер дата"]
    run.discovered = {"Систем": n // 2, "Мастер дата": n - n // 2}
    run.login_times = ["2026-09-22T10:00:05"]
    run.session_observed_s = 30 * 60
    for i in range(n):
        it = MenuItem("Мастер дата" if i % 2 else "Систем", "Тохиргоо", f"Цэс {i}", f"/m/{i}")
        r = ItemResult(it, status=M.PASS, checked_at="2026-09-22T10:10:00", load_ms=700, tab_opened=True)
        if fails and i in fails:
            r.findings = list(fails[i])
            if any(f.kind in (M.TIMEOUT, M.LOADING_STUCK) for f in r.findings):
                r.status, r.blocked_reason = M.BLOCKED, "Timeout"
            else:
                r.status = M.FAIL
            r.confirmation = "Баталгаажсан (2/2)"
        run.results.append(r)
    run.timeline = [TimelineEvent("2026-09-22T10:00:05", "Нэвтрэлт", "Pass", "v26")]
    return run


@pytest.fixture
def cfg():
    from app.config import load_config
    return load_config("https://testaccounting.smartapp.mn/")
