"""runs/ цэвэрлэх бодлого (app/retention.py) ба хувилбарын дугаар давтагдахгүй байх."""
from datetime import datetime

from fastapi.testclient import TestClient

from app import retention
from app.store import Store

NOW = datetime(2026, 10, 13, 12, 0, 0)


def run(i, project="Accounting", day=1, status="done"):
    return {"id": f"{i:012x}", "project": project, "status": status, "created_at": f"2026-{day // 31 + 1:02d}-{day % 31 + 1:02d}T10:00:00"}


def ids(rows):
    return sorted(r["id"] for r in rows)


def test_disabled_policy_selects_nothing():
    runs = [run(i, day=i) for i in range(10)]
    assert retention.select_prunable(runs, 0, 0, NOW) == []


def test_keep_per_project_keeps_newest_per_project():
    runs = [run(i, "A", day=i) for i in range(5)] + [run(100 + i, "B", day=i) for i in range(2)]
    out = retention.select_prunable(runs, keep_per_project=2, now=NOW)
    # A: хамгийн шинэ 2 (day 4, 3) үлдэнэ; B: 2-оос олонгүй
    assert ids(out) == ids([run(i, "A", day=i) for i in range(3)])


def test_max_age_never_deletes_latest_or_running():
    runs = [run(1, "A", day=1), run(2, "A", day=2, status="running"), run(3, "B", day=3)]
    out = retention.select_prunable(runs, max_age_days=30, now=NOW)
    # Бүгд 30 хоногоос хуучин: A-ийн сүүлийнх нь running (үлдэнэ), A-ийн эхнийх устна; B-ийн цорын ганц нь үлдэнэ
    assert ids(out) == [run(1)["id"]]


def test_prune_dry_run_then_apply(tmp_path):
    store = Store(tmp_path / "runs.db")
    for i in range(4):
        store.create(f"{i:012x}", "https://testx.smartapp.mn/", "testx.smartapp.mn", "X", "", f"v1.{i}")
        with store._conn() as c:
            c.execute("UPDATE runs SET created_at=?, status='done' WHERE id=?", (f"2026-01-0{i + 1}T10:00:00", f"{i:012x}"))
        d = tmp_path / f"{i:012x}"
        d.mkdir()
        (d / "raw.json").write_text("x" * 100)
    (tmp_path / "flow-0123abcd").mkdir()           # runs.db-д бүртгэлгүй
    (tmp_path / "keepme").mkdir()                  # run-ын нэрийн хэв маяггүй — хэзээ ч хөндөхгүй

    dry = retention.prune(store, tmp_path, keep_per_project=2, orphans=True)
    assert not dry["applied"] and len(dry["runs"]) == 2 and dry["bytes"] == 200
    assert [p.name for p in dry["orphans"]] == ["flow-0123abcd"]
    assert (tmp_path / f"{0:012x}").exists() and len(store.list()) == 4     # dry-run юу ч устгахгүй

    res = retention.prune(store, tmp_path, keep_per_project=2, orphans=True, apply=True)
    assert res["applied"]
    assert sorted(r["id"] for r in store.list()) == [f"{2:012x}", f"{3:012x}"]
    assert not (tmp_path / f"{0:012x}").exists() and not (tmp_path / "flow-0123abcd").exists()
    assert (tmp_path / "keepme").exists()


def test_next_version_does_not_repeat_after_delete(tmp_path):
    store = Store(tmp_path / "runs.db")
    for i in range(3):
        store.create(f"r{i}", "u", "h", "P", "", store.next_version("P"))
    assert sorted(r["version_label"] for r in store.list()) == ["v1.0", "v1.1", "v1.2"]
    store.delete("r0")
    store.delete("r1")
    assert store.next_version("P") == "v1.3"          # COUNT-аар бол v1.1 — давхардана
    assert store.next_version("Шинэ") == "v1.0"


def test_delete_recorded_rejects_non_test_host(tmp_path, monkeypatch):
    import app.api as api
    monkeypatch.setattr(api, "store", Store(tmp_path / "runs.db"))
    c = TestClient(api.app)
    r = c.delete("/api/recorded", params={"url": "https://hr.smartapp.mn/", "name": "rec: x"})
    assert r.status_code == 400
    assert c.delete("/api/recorded", params={"url": "", "name": "rec: x"}).status_code == 400
