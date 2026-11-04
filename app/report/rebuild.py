"""raw.json-оос RunData-г сэргээх — сервер тайлан үүсгэж байх үед хаагдсан (эсвэл тайлангийн алдаатай) run-ын
тайланг дахин үүсгэхэд.

Гараар (тайланг одоогийн ангиллын дүрмээр дахин үүсгэнэ; xlsx/docx/summary/screenshot zip дарагдана):
    python -m app.report.rebuild <run_id> [<run_id> ...]
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from app import models as M

_LAP = re.compile(r"^(\S+) \[\w+\] ⏱ (.+?): [\d.]+с \|")


def adopt_confirm_shots(run_dir: Path, results: list[M.ItemResult]) -> int:
    """Хуучин run (2026-11-04-өөс өмнөх): давтан баталгаажуулалтын screenshot (item-*.png) аль ч TC-д холбогдоогүй,
    raw.json-д ч бүртгэгдээгүй. run.log-ийн давтан шалгалтын «⏱ <цэс>» мөрийн (цэс бүрийн шалгалт дууссан) цагийг
    файлын цагтай тулгаж Fail цэст нь хавсаргана: screenshot нь тухайн цэсийн шалгалт дуусахаас өмнө авагдана.
    Хавсаргасан тоог буцаана."""
    run_dir = Path(run_dir)
    log = run_dir / "run.log"
    orphans = sorted((run_dir / "shots").glob("item-*.png"))
    if not log.exists() or not orphans or any(r.confirm_screenshot for r in results):
        return 0
    lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
    start = next((i for i, l in enumerate(lines) if "Давтан баталгаажуулалт:" in l), None)
    if start is None:
        return 0
    laps = [(datetime.fromisoformat(m.group(1)).timestamp(), m.group(2))
            for m in map(_LAP.match, lines[start + 1:]) if m]
    # Давтан шалгалт нь Fail цэсүүдийг results-ийн дарааллаар явна; нэг цэс дахин оролдвол хэд хэдэн ⏱ мөртэй
    ends: list[tuple[float, M.ItemResult]] = []
    i = 0
    for r in (x for x in results if x.confirmation):
        last = None
        while i < len(laps) and laps[i][1] == r.item.name:
            last = laps[i][0]
            i += 1
        if last is not None:
            ends.append((last, r))
    n = 0
    for p in orphans:
        t = p.stat().st_mtime
        # лог секундээр таслагдсан — дуусах цаг [ts, ts+1) хооронд; screenshot нь түүнээс өмнө
        r = next((r for end, r in ends if t < end + 1 and not r.confirm_screenshot), None)
        if r is not None:
            r.confirm_screenshot = p.relative_to(run_dir).as_posix()
            n += 1
    return n


def _mk(cls, data: dict):
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in names})


def load_run(raw_path: Path) -> M.RunData:
    d = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    results = []
    for r in d.get("results") or []:
        r = dict(r)
        r["item"] = _mk(M.MenuItem, r.get("item") or {})
        r["findings"] = [_mk(M.Finding, f) for f in r.get("findings") or []]
        results.append(_mk(M.ItemResult, r))
    timeline = [_mk(M.TimelineEvent, t) for t in d.get("timeline") or []]
    # Хуучин raw.json: хэрэгслийн (Python) дотоод алдааг Fail (аппын bug) гэж бүртгэсэн байж болно → Blocked «tool»
    from app.flows.engine import TOOL_ERRORS
    tool_re = "^(" + "|".join(e.__name__ for e in TOOL_ERRORS) + "): "
    import re as _re
    for f in d.get("flows") or []:
        for s in f.get("steps") or []:
            if s.get("status") == M.FAIL and _re.match(tool_re, s.get("message") or ""):
                s["status"], s["category"] = M.BLOCKED, "tool"
                s["message"] = f"Хэрэгслийн дотоод алдаа (аппын bug биш): {s['message']}"
                if (f.get("meta") or {}).get("auto"):
                    f["meta"]["skipped"] = s["message"]
                f["status"] = _flow_status(f["steps"])
    # Хуучин raw.json: E2E / бичсэн сценари «алгассан» гэж тэмдэглэгдэж TC-гээ алдсан байж болно (P2, 2026-10-19)
    for f in d.get("flows") or []:
        meta = f.get("meta") or {}
        if meta.get("skipped") and not meta.get("auto"):
            meta.pop("skipped")
    # Хуучин (ангилалгүй) raw.json: форм шалгалтын Blocked-ийн ангиллыг мессежээс нөхнө
    for f in d.get("flows") or []:
        if not (f.get("meta") or {}).get("formcheck"):
            continue
        for s in f.get("steps") or []:
            if s.get("status") != M.BLOCKED or s.get("category"):
                continue
            msg = s.get("message") or ""
            if msg.startswith("Жагсаалтад бичлэг алга"):
                s["category"] = "nodata"
            elif "нээгдсэнгүй" in msg and ("Нэмэх/Шинэ" in msg or "засварын форм" in msg):
                s["category"] = "tool"
        f["status"] = _flow_status(f["steps"])
    import re
    from app.runner.detectors import tab_mismatch_finding
    from app.runner.smoke import smoke_status
    from app.config import load_config
    from app.runner.detectors import classify_popup, is_asset
    try:
        cfg = load_config(d.get("url"))
    except Exception:
        cfg = {}
    for r in results:
        for i, f in enumerate(r.findings):
            if f.kind == M.SYSTEM_ERROR and cfg and not f.after_close:
                nf = classify_popup(f.text, cfg, False)       # одоогийн ангилал («Анхаар … тохиргоо» цонх)
                if nf.kind != f.kind:
                    r.findings[i] = f = nf
            if f.kind == M.HTTP_4XX and is_asset("", f.url):
                # Хуучин run: дутуу CSS/зураг/фонт нь цэсийг Fail болгодог байсан → анхааруулга (asset_4xx)
                r.findings[i] = f = dataclasses.replace(f, kind=M.ASSET_4XX, text=f"HTTP {f.status} (статик файл)")
            m = re.match(r"Таб: «(.*)» ≠ цэс: «(.*?)»", f.text or "") if f.kind == M.TAB_NAME_MISMATCH else None
            if m and "—" not in f.text:
                r.findings[i] = tab_mismatch_finding(m.group(1), m.group(2))
        if r.status == M.FAIL and smoke_status(r, {}) == M.PASS:
            r.status = M.PASS
    for r in results:
        if r.status == M.FAIL and smoke_status(r, {}) == M.PASS:
            r.status = M.PASS
    # Хуучин raw.json: Blocked-ийг шалтгаанаар нь дахин ангилна — апп талын нотолгоотой саад л Blocked,
    # хэрэгсэл/тохиргоо/өгөгдлийн шалтгаантай нь «Шалгагдаагүй» (classify/cause.py)
    from app.runner.classify.cause import OUTAGE_PREFIX, is_outage, status_for
    from app.runner.smoke import classify_item
    for r in results:
        if not r.cause:
            classify_item(r)
    from app.pipeline.stages import reclassify_invisible_record, reclassify_update_outcomes
    for f in d.get("flows") or []:
        auto = bool((f.get("meta") or {}).get("auto"))
        changed = auto and reclassify_invisible_record(f.get("steps") or [])
        changed = (auto and reclassify_update_outcomes(f.get("steps") or [])) or changed
        for s in f.get("steps") or []:
            if (s.get("status") == M.FAIL and not s.get("findings")
                    and "товчийг дарж чадсангүй (өөр цонх/элемент хааж байна)" in (s.get("message") or "")):
                # Хуучин run: алдаа/анхааруулгагүй click амжилтгүй — хэрэгслийн асуудал, bug биш
                s["status"], s["cause"], s["category"] = M.NOT_TESTED, "tool", "tool"
                changed = True
            if s.get("status") == M.FAIL and is_outage(s.get("message") or ""):
                # Хуучин run: сервер/gateway хүрэх боломжгүй (502/503/504) — функциональ bug биш, орчны саад
                s["status"], s["category"], s["cause"] = M.BLOCKED, "infra", "app"
                s["message"] = f"{OUTAGE_PREFIX}: {s.get('message')}"[:500]
                changed = True
            if s.get("status") == M.BLOCKED and not s.get("cause"):
                s["status"], s["cause"] = status_for(s.get("category") or "", s.get("message") or "")
                changed = True
        if changed:
            f["status"] = _flow_status(f["steps"])
    return _mk(M.RunData, {**d, "results": results, "timeline": timeline})


def _flow_status(steps: list[dict]) -> str:
    sts = {x.get("status") for x in steps
           if not (x.get("status") in (M.BLOCKED, M.NOT_TESTED)
                   and (x.get("category") in ("nodata", "na") or x.get("cause") in ("data", "na")))}
    if M.FAIL in sts:
        return M.FAIL
    if M.BLOCKED in sts:
        return M.BLOCKED
    if M.NOT_TESTED in sts:
        return M.NOT_TESTED
    return M.PASS if sts else M.NA


def rebuild(run_id: str) -> dict:
    """Run-ын тайланг raw.json-оос одоогийн дүрмээр дахин үүсгэж Түүхийн (runs.db) мэдээллийг шинэчилнэ."""
    from app.api import RUNS_DIR, _previous_summary, store
    from app.config import load_config
    from app.report.build import generate_reports
    run_dir = RUNS_DIR / run_id
    data = load_run(run_dir / "raw.json")
    adopted = adopt_confirm_shots(run_dir, data.results)
    summary = generate_reports(data, load_config(data.url), run_dir, False,
                               _previous_summary(run_id, data.url, data.flows))
    summary["pipeline"] = data.pipeline
    stats = summary.get("stats") or {}
    store.update(run_id, decision=summary.get("decision"), pass_rate=stats.get("pass_rate"),
                 total=stats.get("total"), summary=summary)
    summary["adopted_shots"] = adopted
    return summary


def main(argv: list[str] | None = None) -> None:
    ids = (argv if argv is not None else sys.argv[1:])
    if not ids:
        print(__doc__)
        raise SystemExit(2)
    for rid in ids:
        s = rebuild(rid)
        print(f"{rid}: {s['decision']} · Pass Rate {s['stats']['pass_rate']}% · bug {s['bug_counts']['total']}"
              + (f" · давтан шалгалтын {s['adopted_shots']} screenshot хавсаргав" if s["adopted_shots"] else ""))


if __name__ == "__main__":
    main()
