"""Энэ системийн хэрэглэгчийн нэвтрэлт ба хэрэглэгч бүрийн Түүх тусдаа байх."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import auth, users
from app.store import LEGACY_OWNER, Store
from tests.conftest import TEST_PASSWORD, login_client


@pytest.fixture
def api(tmp_path, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "store", Store(tmp_path / "runs.db"))
    monkeypatch.setattr(api_mod, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(api_mod, "throttle", auth.Throttle())
    return api_mod


def add_run(api, tmp_path, run_id, owner):
    api.store.create(run_id, "https://testx.smartapp.mn/", "testx.smartapp.mn", "X", "", "v1.0", owner=owner)
    api.store.update(run_id, status="done", summary={"bugs": [], "xlsx": "X.xlsx"})
    d = tmp_path / run_id
    (d / "shots").mkdir(parents=True)
    (d / "X.xlsx").write_bytes(b"xlsx")
    (d / "shots" / "a.png").write_bytes(b"png")


# ---------------------------------------------------------------- нууц үг
def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$") and "correct horse" not in h
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("same", iterations=1000) != auth.hash_password("same", iterations=1000)   # salt


def test_throttle_locks_after_failures():
    t = auth.Throttle(max_failures=3, window_s=60)
    for _ in range(3):
        assert not t.blocked("u")
        t.fail("u")
    assert t.blocked("u") > 0 and not t.blocked("other")
    t.reset("u")
    assert not t.blocked("u")


# ---------------------------------------------------------------- нэвтрэлт
def test_everything_requires_login(api):
    c = TestClient(api.app)
    assert c.get("/api/runs").status_code == 401
    assert c.get("/api/flows", params={"url": "https://testx.smartapp.mn/"}).status_code == 401
    assert c.get("/runs/abc/download/xlsx").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/static/index.html", follow_redirects=False).status_code == 303
    assert c.get("/login").status_code == 200 and "Нэвтрэх" in c.get("/login").text


def test_login_logout_and_bad_password(api):
    api.store.create_user("qa01", auth.hash_password(TEST_PASSWORD, iterations=1000))
    c = TestClient(api.app)
    r = c.post("/api/auth/login", json={"username": "qa01", "password": "wrong"})
    assert r.status_code == 401 and "буруу" in r.json()["detail"]
    assert c.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": " QA01 ", "password": TEST_PASSWORD})
    assert r.status_code == 200 and r.json() == {"username": "qa01", "name": "", "is_admin": False}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/auth/me").json()["username"] == "qa01"
    assert c.get("/login", follow_redirects=False).status_code == 303          # нэвтэрсэн бол нүүр рүү
    # Токен DB-д түүхий хэлбэрээр хадгалагдахгүй
    with sqlite3.connect(api.store.path) as db:
        stored = [r[0] for r in db.execute("SELECT token_hash FROM sessions")]
    assert c.cookies.get(auth.COOKIE) not in stored and len(stored) == 1
    c.post("/api/auth/logout")
    assert c.get("/api/runs").status_code == 401


def test_login_throttled_after_5_failures(api):
    api.store.create_user("qa02", auth.hash_password(TEST_PASSWORD, iterations=1000))
    c = TestClient(api.app)
    for _ in range(auth.MAX_FAILURES):
        assert c.post("/api/auth/login", json={"username": "qa02", "password": "bad"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "qa02", "password": TEST_PASSWORD})
    assert r.status_code == 429                                               # зөв нууц үгтэй ч түгжээтэй


def test_disabled_user_cannot_login_and_loses_session(api):
    c = login_client(api, api.store, "qa03")
    api.store.update_user("qa03", disabled=1)
    assert c.get("/api/runs").status_code == 401
    r = TestClient(api.app).post("/api/auth/login", json={"username": "qa03", "password": TEST_PASSWORD})
    assert r.status_code == 401


def test_change_password(api):
    c = login_client(api, api.store, "qa04")
    other = login_client(api, api.store, "qa04")                              # 2 дахь төхөөрөмж
    bad = c.post("/api/auth/password", json={"old_password": "nope", "new_password": "newpassword1"})
    assert bad.status_code == 400
    short = c.post("/api/auth/password", json={"old_password": TEST_PASSWORD, "new_password": "short"})
    assert short.status_code == 400 and "8" in short.json()["detail"]
    ok = c.post("/api/auth/password", json={"old_password": TEST_PASSWORD, "new_password": "newpassword1"})
    assert ok.status_code == 200
    assert c.get("/api/runs").status_code == 200                              # энэ хөтөч нэвтэрсэн хэвээр
    assert other.get("/api/runs").status_code == 401                          # бусад session хаагдсан
    r = TestClient(api.app).post("/api/auth/login", json={"username": "qa04", "password": "newpassword1"})
    assert r.status_code == 200


# ---------------------------------------------------------------- Түүх тусдаа
def test_history_is_per_user_and_admin_sees_all(api, tmp_path):
    a = login_client(api, api.store, "qa01")
    b = login_client(api, api.store, "qa02")
    admin = login_client(api, api.store, "admin", is_admin=True)
    add_run(api, tmp_path, "aaaaaaaaaaaa", "qa01")
    add_run(api, tmp_path, "bbbbbbbbbbbb", "qa02")

    assert [r["id"] for r in a.get("/api/runs").json()] == ["aaaaaaaaaaaa"]
    assert [r["id"] for r in b.get("/api/runs").json()] == ["bbbbbbbbbbbb"]
    assert sorted(r["owner"] for r in admin.get("/api/runs").json()) == ["qa01", "qa02"]

    # Бусдын run-д ямар ч замаар хандахгүй (404 — байгаа эсэхийг ч задлахгүй)
    for path in ("/api/runs/bbbbbbbbbbbb", "/runs/bbbbbbbbbbbb/download/xlsx", "/runs/bbbbbbbbbbbb/shots/a.png",
                 "/api/runs/bbbbbbbbbbbb/events", "/api/compare?a=aaaaaaaaaaaa&b=bbbbbbbbbbbb"):
        assert a.get(path).status_code == 404, path
    for path in ("/api/runs/bbbbbbbbbbbb/stop", "/api/runs/bbbbbbbbbbbb/select", "/api/runs/bbbbbbbbbbbb/record-stop"):
        assert a.post(path, json={}).status_code == 404, path
    assert a.delete("/api/runs/bbbbbbbbbbbb").status_code == 404
    assert (tmp_path / "bbbbbbbbbbbb").exists() and api.store.get("bbbbbbbbbbbb")

    # Өөрийнхөө run-д хандана; admin бүгдэд
    assert a.get("/api/runs/aaaaaaaaaaaa").status_code == 200
    assert a.get("/runs/aaaaaaaaaaaa/download/xlsx").content == b"xlsx"
    assert admin.get("/runs/bbbbbbbbbbbb/shots/a.png").content == b"png"
    assert a.delete("/api/runs/aaaaaaaaaaaa").status_code == 200
    assert admin.delete("/api/runs/bbbbbbbbbbbb").status_code == 200


def test_previous_run_lookups_use_only_own_history(api, tmp_path):
    for rid, owner in (("aaaaaaaaaaaa", "qa01"), ("bbbbbbbbbbbb", "qa02")):
        add_run(api, tmp_path, rid, owner)
        (tmp_path / rid / "raw.json").write_text(json.dumps({"flows": [{"meta": {"formcheck": True, "menu_key": rid},
            "steps": [{"uses": "form.check_edit", "category": "nodata"}]}]}), encoding="utf-8")
    assert api.previous_form_na("https://testx.smartapp.mn/", owner="qa01")["run_id"] == "aaaaaaaaaaaa"
    assert api.previous_form_na("https://testx.smartapp.mn/", owner="qa02")["run_id"] == "bbbbbbbbbbbb"
    assert api.previous_form_na("https://testx.smartapp.mn/", owner="qa09")["run_id"] == ""
    c = login_client(api, api.store, "qa09")
    assert c.get("/api/form-na", params={"url": "https://testx.smartapp.mn/"}).json()["keys"] == []


def test_legacy_runs_migrate_to_admin(tmp_path):
    db = tmp_path / "runs.db"
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE runs (id TEXT PRIMARY KEY, created_at TEXT, finished_at TEXT, url TEXT, host TEXT,
            project TEXT, qa_lead TEXT, version_label TEXT, app_version TEXT, status TEXT, decision TEXT,
            pass_rate REAL, total INTEGER, summary_json TEXT, error TEXT, pipeline TEXT)""")
        c.execute("INSERT INTO runs (id, created_at, project, status) VALUES ('old', '2026-01-01', 'P', 'done')")
    s = Store(db)
    assert s.get("old")["owner"] == LEGACY_OWNER == "admin"
    assert [r["id"] for r in s.list(owner="admin")] == ["old"] and s.list(owner="qa01") == []


# ---------------------------------------------------------------- CLI
def test_users_init_creates_15_once(tmp_path):
    s = Store(tmp_path / "runs.db")
    created = users.init_users(s, 15)
    names = [u for u, _, _ in created]
    assert len(created) == 15 and names[0] == "admin" and names[1:] == [f"qa{i:02d}" for i in range(1, 15)]
    assert [u for u, _, adm in created if adm] == ["admin"]
    assert all(len(pw) == 12 for _, pw, _ in created) and len({pw for _, pw, _ in created}) == 15
    admin_pw = created[0][1]
    assert auth.verify_password(admin_pw, s.get_user("admin")["pw_hash"])
    assert users.init_users(s, 15) == []                     # дахин ажиллуулахад нууц үг өөрчлөгдөхгүй
    assert auth.verify_password(admin_pw, s.get_user("admin")["pw_hash"])
    with pytest.raises(ValueError):
        users.add_user(s, "Bad Name!")
    new = users.reset_password(s, "qa05")
    assert auth.verify_password(new, s.get_user("qa05")["pw_hash"])
