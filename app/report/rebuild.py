"""raw.json-оос RunData-г сэргээх — сервер тайлан үүсгэж байх үед хаагдсан (эсвэл тайлангийн алдаатай) run-ын
тайланг дахин үүсгэхэд."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from app import models as M


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
    from app.runner.detectors import classify_popup
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
    from app.runner.classify.cause import status_for
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
