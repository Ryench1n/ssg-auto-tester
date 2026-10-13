"""runs/ хавтсыг цэвэрлэх бодлого (retention).

python -m app.retention [--keep N] [--days D] [--orphans] [--apply]

Анхдагчаар зөвхөн юу устгагдахыг харуулна (dry-run); `--apply` өгвөл устгана. Утга өгөөгүй бол
config/default.yaml → `retention`-оос авна. Дүрэм:
  * хэрэглэгч × төсөл бүрийн хамгийн сүүлийн `keep_per_project` run-ыг үлдээнэ (0 = хязгааргүй);
  * `max_age_days`-ээс хуучин run-ыг устгана (0 = хязгааргүй);
  * төсөл бүрийн ХАМГИЙН СҮҮЛИЙН run-ыг хэзээ ч устгахгүй (дараагийн run-ын харьцуулалт, хувилбарын дугаар);
  * «running» төлөвтэй run-ыг хэзээ ч устгахгүй.
`--orphans`: runs.db-д бүртгэлгүй run хавтсыг (ж: гараар хуулсан, DB устсан) мөн устгана.
"""
from __future__ import annotations

import argparse
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from app.config import RUNS_DIR, load_config

_RUN_DIR = re.compile(r"^(?:[0-9a-f]{12}|(?:flow|explore|real)-[0-9a-f]{8})$")


def policy(cfg: dict) -> dict:
    r = cfg.get("retention") or {}
    return {"keep_per_project": int(r.get("keep_per_project") or 0), "max_age_days": int(r.get("max_age_days") or 0)}


def select_prunable(runs: list[dict], keep_per_project: int = 0, max_age_days: int = 0,
                    now: datetime | None = None) -> list[dict]:
    """Устгах run-уудыг сонгоно (цэвэр функц). runs: store.list()-ийн мөрүүд (created_at, project, status)."""
    if keep_per_project <= 0 and max_age_days <= 0:
        return []
    cutoff = (now or datetime.now()) - timedelta(days=max_age_days) if max_age_days > 0 else None
    # Хэрэглэгч бүрийн Түүх тусдаа — нэг хэрэглэгчийн шинэ run бусдын run-ыг шахахгүй
    by_project: dict[tuple[str, str], list[dict]] = {}
    for r in sorted(runs, key=lambda r: r.get("created_at") or "", reverse=True):
        by_project.setdefault((r.get("owner") or "", r.get("project") or ""), []).append(r)
    out = []
    for rows in by_project.values():
        for i, r in enumerate(rows):
            if i == 0 or r.get("status") == "running":
                continue
            too_many = keep_per_project > 0 and i >= keep_per_project
            try:
                too_old = cutoff is not None and datetime.fromisoformat(r.get("created_at") or "") < cutoff
            except ValueError:
                too_old = False
            if too_many or too_old:
                out.append(r)
    return out


def orphan_dirs(runs_dir: Path, known_ids: set[str]) -> list[Path]:
    """runs.db-д бүртгэлгүй run хавтсууд (зөвхөн run-ын нэрийн хэв маягтай хавтас — бусдыг хөндөхгүй)."""
    if not runs_dir.is_dir():
        return []
    return sorted(p for p in runs_dir.iterdir() if p.is_dir() and _RUN_DIR.match(p.name) and p.name not in known_ids)


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0


def prune(store, runs_dir: Path, keep_per_project: int = 0, max_age_days: int = 0, orphans: bool = False,
          apply: bool = False, skip_ids: set[str] | None = None) -> dict:
    """Сонгоод (apply=True бол) устгана. skip_ids: идэвхтэй (LIVE) run-ууд."""
    runs = store.list(1_000_000)
    victims = [r for r in select_prunable(runs, keep_per_project, max_age_days)
               if r["id"] not in (skip_ids or set())]
    stray = orphan_dirs(runs_dir, {r["id"] for r in runs}) if orphans else []
    freed = sum(dir_size(runs_dir / r["id"]) for r in victims) + sum(dir_size(p) for p in stray)
    if apply:
        for r in victims:
            shutil.rmtree(runs_dir / r["id"], ignore_errors=True)
            store.delete(r["id"])
        for p in stray:
            shutil.rmtree(p, ignore_errors=True)
    return {"runs": victims, "orphans": stray, "bytes": freed, "applied": apply}


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def main() -> None:
    pol = policy(load_config(None))
    ap = argparse.ArgumentParser(description="runs/ хавтсыг цэвэрлэх (анхдагч: dry-run)")
    ap.add_argument("--keep", type=int, default=pol["keep_per_project"],
                    help="төсөл бүрийн сүүлийн N run-ыг үлдээнэ (0 = хязгааргүй)")
    ap.add_argument("--days", type=int, default=pol["max_age_days"], help="D хоногоос хуучин run-ыг устгана (0 = хязгааргүй)")
    ap.add_argument("--orphans", action="store_true", help="runs.db-д бүртгэлгүй run хавтсыг мөн устгана")
    ap.add_argument("--apply", action="store_true", help="бодитоор устгана (өгөхгүй бол зөвхөн жагсаана)")
    a = ap.parse_args()
    if a.keep <= 0 and a.days <= 0 and not a.orphans:
        print("Бодлого тохируулаагүй: --keep, --days эсвэл --orphans өгнө үү (эсвэл config → retention).")
        return
    from app.store import Store
    res = prune(Store(), RUNS_DIR, a.keep, a.days, a.orphans, a.apply)
    for r in res["runs"]:
        print(f"  {r['id']}  {r.get('created_at', '')}  {r.get('project', '')} {r.get('version_label', '')}"
              f"  {r.get('status', '')}")
    for p in res["orphans"]:
        print(f"  {p.name}  (runs.db-д бүртгэлгүй)")
    verb = "Устгалаа" if a.apply else "Устгагдах"
    print(f"{verb}: {len(res['runs'])} run, {len(res['orphans'])} бүртгэлгүй хавтас, {_mb(res['bytes'])}"
          + ("" if a.apply else "  — бодитоор устгахдаа --apply нэмнэ үү"))


if __name__ == "__main__":
    main()
