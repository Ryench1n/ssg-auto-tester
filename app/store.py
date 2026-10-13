"""SQLite хадгалалт (runs.db). Нууц үг, token-ыг ХЭЗЭЭ Ч хадгалахгүй."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from app.config import RUNS_DIR

_lock = threading.Lock()


class Store:
    def __init__(self, path: Path = RUNS_DIR / "runs.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        with _lock, self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, created_at TEXT, finished_at TEXT, url TEXT, host TEXT,
                project TEXT, qa_lead TEXT, version_label TEXT, app_version TEXT, status TEXT,
                decision TEXT, pass_rate REAL, total INTEGER, summary_json TEXT, error TEXT,
                pipeline TEXT)""")
            # Migration: хуучин runs.db-д pipeline багана байхгүй — өгөгдлийг хөндөхгүй нэмнэ
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
            if "pipeline" not in cols:
                c.execute("ALTER TABLE runs ADD COLUMN pipeline TEXT")
            # Цэсний үйлдлийн каталог (smoke-ийн READ-ONLY scan) — host бүрийн цэсийн сүүлийн бүртгэл
            c.execute("""CREATE TABLE IF NOT EXISTS menu_caps (
                host TEXT, key TEXT, module TEXT, folder TEXT, name TEXT, crud TEXT, caps_json TEXT,
                app_version TEXT, run_id TEXT, updated_at TEXT, PRIMARY KEY (host, key))""")

    def save_caps(self, host: str, run_id: str, app_version: str, items: list[dict]) -> int:
        """items: [{key, module, folder, name, caps}] — энэ run-д шалгасан цэсүүдийг шинэчилнэ (upsert)."""
        now = datetime.now().isoformat(timespec="seconds")
        rows = [(host, i["key"], i["module"], i["folder"], i["name"], (i["caps"] or {}).get("crud", "none"),
                 json.dumps(i["caps"], ensure_ascii=False), app_version, run_id, now) for i in items if i.get("caps")]
        with _lock, self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO menu_caps VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def list_caps(self, host: str, crud: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM menu_caps WHERE host=?", [host]
        if crud:
            q, args = q + " AND crud=?", args + [crud]
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY module, folder, name", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["caps"] = json.loads(d.pop("caps_json") or "{}")
            out.append(d)
        return out

    def create(self, run_id: str, url: str, host: str, project: str, qa_lead: str, version_label: str,
               pipeline: list | None = None) -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT INTO runs (id, created_at, url, host, project, qa_lead, version_label, status,"
                      " pipeline) VALUES (?,?,?,?,?,?,?,?,?)",
                      (run_id, datetime.now().isoformat(timespec="seconds"), url, host, project, qa_lead,
                       version_label, "running", json.dumps(pipeline or [], ensure_ascii=False)))

    def update(self, run_id: str, **fields) -> None:
        if "summary" in fields:
            fields["summary_json"] = json.dumps(fields.pop("summary"), ensure_ascii=False)
        if "pipeline" in fields:
            fields["pipeline"] = json.dumps(fields["pipeline"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        with _lock, self._conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def get(self, run_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r, with_summary=False) for r in rows]

    def delete(self, run_id: str) -> bool:
        with _lock, self._conn() as c:
            cur = c.execute("DELETE FROM runs WHERE id=?", (run_id,))
        return cur.rowcount > 0

    def next_version(self, project: str) -> str:
        """Ижил төсөлд дахин run хийвэл v1.0 → v1.1 → v1.2."""
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM runs WHERE project=?", (project,)).fetchone()[0]
        return f"v1.{n}"

    @staticmethod
    def _row(row: sqlite3.Row, with_summary: bool = True) -> dict:
        d = dict(row)
        try:
            d["pipeline"] = json.loads(d.get("pipeline") or "[]")
        except ValueError:
            d["pipeline"] = []
        s = d.pop("summary_json", None)
        if with_summary:
            d["summary"] = json.loads(s) if s else None
        return d
