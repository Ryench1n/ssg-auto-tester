"""python -m app.flows --url https://testaccounting.smartapp.mn --flow customer_group_crud [--flow …] [--smoke]
   python -m app.flows --url … --smoke --forms          # smoke + бүх CRUD цэсийн форм шалгалт (READ-ONLY)
   python -m app.flows --url … --form "Цалин||Ажилтан|employeelist"

Хөтөч харагдах (headed) горимд нээгдэнэ — нээгдсэн хөтөч дээр ГАРААР нэвтэрнэ үү (нууц үг
config/env/log-д хэзээ ч орохгүй). Нэвтэрмэгц сонгосон flow-ууд нэг session-д дараалан ажиллана.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from app import models as M
from app.config import RUNS_DIR, load_config
from app.pipeline.runner import PipelineRunner
from app.pipeline.stages import PipelineConfigError
from app.runner.smoke import RunOptions


def _print_event(etype: str, d: dict) -> None:
    if etype == "log":
        print(d["msg"])
    elif etype == "waiting_login":
        print(f"\n>>> Нээгдсэн хөтөч дээр нэвтэрнэ үү ({d['reason']}) — {d['timeout_s']} сек хүлээнэ <<<\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="SSG component-based E2E flow (гар нэвтрэлттэй)")
    ap.add_argument("--url", required=True)
    ap.add_argument("--flow", action="append", default=[], help="flow нэр (олон удаа өгч болно)")
    ap.add_argument("--auto", action="append", default=[],
                    help="автомат CRUD хийх цэсний түлхүүр 'module|folder|name|href' (олон удаа өгч болно)")
    ap.add_argument("--form", action="append", default=[],
                    help="форм шалгалт (READ-ONLY) хийх цэсний түлхүүр (олон удаа өгч болно)")
    ap.add_argument("--forms", action="store_true",
                    help="smoke-ийн бүртгэсэн бүх Нэмэх/Засах цэсэд форм шалгалт (--smoke шаардлагатай)")
    ap.add_argument("--smoke", action="store_true", help="flow-уудын өмнө smoke (READ-ONLY) ажиллуулах")
    ap.add_argument("--login-timeout", type=int, default=0, help="гар нэвтрэлт хүлээх сек (0 = тохиргооноос)")
    ap.add_argument("--cdp", nargs="?", const="http://127.0.0.1:9222", default=None,
                    help="өөрийн Chrome-д холбогдох (captcha-тай сайт): chrome --remote-debugging-port=9222 …")
    ap.add_argument("--headed", action="store_true", help="(хуучирсан — хөтөч үргэлж харагдана)")
    a = ap.parse_args()
    if not (a.flow or a.auto or a.form or a.forms):
        ap.error("--flow, --auto, --form, --forms-ийн дор хаяж нэгийг өгнө үү")
    url = a.url if a.url.startswith("http") else "https://" + a.url
    cfg = load_config(url, {"timing": {"manual_login_timeout_ms": a.login_timeout * 1000,
                                           "relogin_timeout_ms": max(a.login_timeout * 1000, 1800000)}} if a.login_timeout else None)
    steps = (([{"kind": "smoke"}] if a.smoke else []) + [{"kind": "crud", "flow": f} for f in a.flow]
             + ([{"kind": "formcheck", "menus": a.form}] if a.form else [])
             + ([{"kind": "formcheck", "menus": None, "select": False}] if a.forms else [])
             + ([{"kind": "autocrud", "menus": a.auto}] if a.auto else []))
    run_id = "flow-" + uuid.uuid4().hex[:8]
    try:
        pipe = PipelineRunner(run_id, url, cfg, RunOptions(project="CLI"), RUNS_DIR / run_id, steps,
                              emit=_print_event, headless=False, cdp=a.cdp)
    except PipelineConfigError as e:
        print(f"Тохиргооны алдаа: {e}", file=sys.stderr)
        return 2
    data = asyncio.run(pipe.run())
    print(f"\nrun_tag={data.run_tag}   ({RUNS_DIR / run_id / 'raw.json'})")
    for st in data.pipeline:
        print(f"  {st['status']:<8} {st['name']}{'  — ' + st['message'] if st['message'] else ''}")
    for fl in data.flows:
        meta = fl.get("meta") or {}
        print(f"\n{fl['flow']} → {fl['status']}" + (f"  [алгассан: {meta['skipped']}]" if meta.get("skipped") else ""))
        if meta.get("auto"):
            print(f"     fields={meta.get('fields')}  lines={meta.get('lines')}")
        for s in fl["steps"]:
            print(f"  #{s['index']:<2} {s['status']:<8} {s['name']}{'  — ' + s['message'] if s['message'] else ''}")
            for n in s.get("notes") or []:
                print(f"         · {n}")
    ok = data.login_ok and all(fl["status"] == M.PASS for fl in data.flows)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
