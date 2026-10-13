"""FastAPI routes + SSE.

Нэг run = PipelineRunner: гар нэвтрэлт (headed хөтөч) → сонгосон алхмууд (smoke, crud …).
Нууц үг API-аар хэзээ ч дамжихгүй — хэрэглэгч нээгдсэн хөтөч дээр өөрөө нэвтэрнэ.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import yaml

from app import auth
from app import config as appcfg
from app import retention
from app.config import RUNS_DIR, load_config
from app.flows.engine import FlowConfigError, flow_code, flow_description, host_reason, load_flow
from app.pipeline.runner import PipelineRunner
from app.runner import browser as browser_mod
from app.pipeline.stages import PipelineConfigError, validate_steps
from app.report.build import generate_reports
from app.runner.smoke import RunOptions
from app.store import Store

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="SSG Auto Tester")
store = Store()


@dataclass
class LiveRun:
    runner: PipelineRunner
    task: asyncio.Task | None = None
    events: list[dict] = field(default_factory=list)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    done: bool = False


LIVE: dict[str, LiveRun] = {}


# ---------------------------------------------------------------- нэвтрэлт (энэ системийн хэрэглэгч)
PUBLIC_PATHS = {"/login", "/api/auth/login", "/favicon.svg", "/favicon.ico"}
throttle = auth.Throttle()


@app.middleware("http")
async def require_login(request: Request, call_next):
    """/login-оос бусад бүх хуудас, API, файл татахад нэвтэрсэн байх ёстой."""
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)
    token = request.cookies.get(auth.COOKIE)
    user = store.session_user(auth.token_hash(token)) if token else None
    if not user:
        if path.startswith(("/api/", "/runs/")):
            return JSONResponse({"detail": "Нэвтрэх шаардлагатай"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    request.state.user = user
    return await call_next(request)


def current_user(request: Request) -> dict:
    return request.state.user


def _own(run_id: str, user: dict) -> dict:
    """Run-ыг зөвхөн эзэмшигч (эсвэл admin) хандана. Бусдынх бол «олдсонгүй» — байгаа эсэхийг ч задлахгүй."""
    r = store.get(run_id)
    if not r or not (user["is_admin"] or r.get("owner") == user["username"]):
        raise HTTPException(404)
    return r


def _own_live(run_id: str, user: dict, detail: str | None = None) -> LiveRun:
    _own(run_id, user)
    live = LIVE.get(run_id)
    if not live:
        raise HTTPException(404, detail)
    return live


def _set_session(resp: JSONResponse, username: str) -> JSONResponse:
    token = auth.new_token()
    store.add_session(auth.token_hash(token), username, auth.expires_at())
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True, samesite="strict", path="/")
    return resp


class LoginReq(BaseModel):
    username: str
    password: str


class PasswordReq(BaseModel):
    old_password: str
    new_password: str


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    token = request.cookies.get(auth.COOKIE)
    if token and store.session_user(auth.token_hash(token)):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse((STATIC / "login.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.post("/api/auth/login")
async def login(req: LoginReq):
    username = req.username.strip().lower()
    wait = throttle.blocked(username)
    if wait:
        raise HTTPException(429, f"Олон удаа буруу оролдлоо — {wait} секундийн дараа дахин оролдоно уу")
    user = store.get_user(username)
    if not await asyncio.to_thread(auth.check_login, user, req.password):
        throttle.fail(username)
        raise HTTPException(401, "Нэвтрэх нэр эсвэл нууц үг буруу")
    throttle.reset(username)
    return _set_session(JSONResponse({"username": username, "name": user.get("name") or "",
                                      "is_admin": bool(user["is_admin"])}), username)


@app.post("/api/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.COOKIE)
    if token:
        store.delete_sessions(token_hash=auth.token_hash(token))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
async def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "name": user.get("name") or "", "is_admin": bool(user["is_admin"])}


@app.post("/api/auth/password")
async def change_password(req: PasswordReq, user: dict = Depends(current_user)):
    full = store.get_user(user["username"])
    if not await asyncio.to_thread(auth.check_login, full, req.old_password):
        raise HTTPException(400, "Одоогийн нууц үг буруу")
    problem = auth.password_problem(req.new_password)
    if problem:
        raise HTTPException(400, problem)
    store.update_user(user["username"], pw_hash=auth.hash_password(req.new_password))
    # Бусад төхөөрөмж дээрх session-уудыг хаагаад энэ хөтөчид шинэ session өгнө
    store.delete_sessions(username=user["username"])
    return _set_session(JSONResponse({"ok": True}), user["username"])


class StepReq(BaseModel):
    kind: str                         # smoke | formcheck | crud | autocrud | record (ирээдүйд business …)
    flow: str | None = None           # crud: config/sites/<host>.yaml-ын flows нэр
    modules: list[str] | None = None  # smoke: None → бүгд
    menus: list[str] | None = None    # autocrud/formcheck: цэсний түлхүүрүүд; None → smoke-ийн дараа сонгоно
    select: bool = True               # formcheck, menus=None: smoke-ийн дараа UI-аас сонгуулах


class CrudSelectReq(BaseModel):
    menus: list[str] = []


class RecordedReq(BaseModel):
    url: str
    name: str
    yaml: str


class StartReq(BaseModel):
    # Нэвтрэх нэр/нууц үг/компани талбар БАЙХГҮЙ — хуучин клиент илгээвэл pydantic үл тоомсорлоно.
    url: str
    project: str = ""
    qa_lead: str = ""
    tester: str = ""                     # тест гүйцэтгэсэн QA (баримтын гүйцэтгэгч / Author)
    version: str = ""                    # тестлэж буй хувилбар (ж: v26.2.10) — файлын нэр, баримтад
    wait_ms: int = 3000
    check_risky: bool = True
    confirm_failures: bool = True
    select_modules: bool = True
    modules: list[str] | None = None
    steps: list[StepReq] | None = None   # None → зөвхөн smoke (хуучин клиенттэй нийцтэй)
    cdp: bool = False                    # өөрийн Chrome-д холбогдох (Cloudflare/captcha-тай сайт)


class SelectReq(BaseModel):
    modules: list[str] | None = None


def project_from_url(url: str) -> str:
    host = urlparse(url).hostname or "Project"
    sub = host.split(".")[0]
    sub = re.sub(r"^test", "", sub, flags=re.I) or sub
    return sub.upper() if len(sub) <= 4 else sub.capitalize()


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    return url


def url_warning(url: str) -> str:
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https" or not host.endswith(".smartapp.mn"):
        return "URL нь https://*.smartapp.mn хэлбэрийн биш байна — зөвхөн SSG-ийн тест орчныг шалгана уу."
    return ""


async def _publish(live: LiveRun, etype: str, data: dict) -> None:
    async with live.cond:
        live.events.append({"type": etype, **data})
        live.cond.notify_all()


@app.on_event("startup")
async def _recover_stale_runs() -> None:
    """Сервер run явж байх (тайлан үүсгэж байх) үед хаагдсан бол run «running» төлөвт гацдаг. Сервер асахад
    ийм run-ын тайланг raw.json-оос дахин үүсгэнэ (raw.json байхгүй бол «error» болгоно)."""
    asyncio.get_event_loop().create_task(asyncio.to_thread(_recover_sync))


def _recover_sync() -> None:
    from app.report.rebuild import load_run
    for r in store.list(500):
        if r.get("status") != "running" or r["id"] in LIVE:
            continue
        raw = RUNS_DIR / r["id"] / "raw.json"
        if not raw.exists():
            store.update(r["id"], status="error", error="Сервер run явж байх үед хаагдсан (raw.json алга)")
            continue
        try:
            data = load_run(raw)
            if all(p.get("kind") in ("auth", "record") for p in data.pipeline):
                store.update(r["id"], status="done", summary={"recording": data.recording, "pipeline": data.pipeline})
                continue
            cfg = load_config(data.url)
            summary = generate_reports(data, cfg, RUNS_DIR / r["id"], False,
                                       _previous_summary(r["id"], data.url, data.flows))
            summary["pipeline"] = data.pipeline
            stats = summary.get("stats") or {}
            store.update(r["id"], status="cancelled" if data.cancelled else "done", finished_at=data.finished_at,
                         app_version=data.app_version, pipeline=data.pipeline, decision=summary.get("decision"),
                         pass_rate=stats.get("pass_rate"), total=stats.get("total"), summary=summary,
                         error="Сервер тайлан үүсгэж байх үед хаагдсан — дахин асахад тайланг сэргээв")
        except Exception as e:
            store.update(r["id"], status="error", error=f"Тайланг сэргээж чадсангүй: {e}"[:500])


def _previous_summary(run_id: str, url: str, flows: list | None = None) -> dict | None:
    """Ижил систем (host) дээрх өмнөх дууссан run-ы summary (форм шалгалтын харьцуулалтад) — одоогийн run-тай
    ижил цэс (форм урсгал) давхцсан хамгийн сүүлийнх (давхцалгүй run-тай харьцуулах утгагүй). Зөвхөн тухайн
    run-ын эзэмшигчийн түүхээс (хэрэглэгч бүрийн Түүх тусдаа)."""
    host = urlparse(url).hostname or ""
    owner = (store.get(run_id) or {}).get("owner")
    mine = {f["flow"] for f in flows or [] if str(f.get("flow", "")).startswith("form: ")}
    best, best_n, smoke_load = None, 0, None
    seen = 0
    for r in store.list(200, owner=owner):
        if r["id"] == run_id or r.get("host") != host or r.get("status") not in ("done", "cancelled"):
            continue
        seen += 1
        if seen > 10:                     # сүүлийн 10 run дотроос
            break
        full = store.get(r["id"]) or {}
        s = full.get("summary") or {}
        if smoke_load is None and s.get("smoke_load"):
            smoke_load = s["smoke_load"]
        theirs = {f.get("flow") for f in s.get("flows") or [] if str(f.get("flow", "")).startswith("form: ")}
        n = len(mine & theirs) if mine else len(theirs)
        if theirs and n > best_n:         # хамгийн олон цэс давхцсан (хэсэгчилсэн/үргэлжлүүлсэн run биш)
            best, best_n = {**s, "_version": full.get("version_label", ""), "_run_id": r["id"]}, n
    if best is None and smoke_load:
        return {"smoke_load": smoke_load}
    if best is not None and not best.get("smoke_load") and smoke_load:
        best = {**best, "smoke_load": smoke_load}
    return best


def _emit_factory(live_ref: dict):
    def emit(etype: str, data: dict) -> None:
        live = live_ref.get("live")
        if live:
            asyncio.get_event_loop().create_task(_publish(live, etype, data))
    return emit


async def _run_job(run_id: str, live: LiveRun, cfg: dict) -> None:
    runner = live.runner
    run_dir = runner.run_dir
    try:
        data = await runner.run()
        if all(p.get("kind") in ("auth", "record") for p in data.pipeline):
            # «Урсгал бичих» run — тест биш, тайлан үүсгэхгүй; YAML-ыг UI урьдчилан харж хадгална
            summary = {"recording": data.recording, "pipeline": data.pipeline}
            status = "cancelled" if data.cancelled and not data.recording else "done"
            store.update(run_id, status=status, finished_at=data.finished_at, app_version=data.app_version,
                         pipeline=data.pipeline, summary=summary)
            await _publish(live, "done", {"status": status, "error": None, "summary": summary})
            return
        if data.smoke_ran:
            # Цэсний үйлдлийн каталог — дараагийн run-д smoke-гүйгээр CRUD цэс сонгоход
            store.save_caps(urlparse(data.url).hostname or "", run_id, data.app_version,
                            [{"key": r.item.key, "module": r.item.module, "folder": r.item.folder,
                              "name": r.item.name, "caps": r.caps} for r in data.results])
        await _publish(live, "status", {"state": "reporting"})
        try:
            summary = await asyncio.to_thread(generate_reports, data, cfg, run_dir, True,
                                              _previous_summary(run_id, data.url, data.flows))
            err = None
        except Exception as e:  # тайлангийн шалгалт зөрсөн г.м.
            summary, err = None, str(e)
            runner_log = run_dir / "run.log"
            with open(runner_log, "a", encoding="utf-8") as fh:
                fh.write(f"[ERROR] Тайлан: {e}\n")
        status = "cancelled" if data.cancelled else ("error" if err else "done")
        if summary is not None:
            summary["pipeline"] = data.pipeline
        store.update(run_id, status=status, finished_at=data.finished_at, app_version=data.app_version,
                     pipeline=data.pipeline,
                     decision=(summary or {}).get("decision"),
                     pass_rate=((summary or {}).get("stats") or {}).get("pass_rate"),
                     total=((summary or {}).get("stats") or {}).get("total"),
                     summary=summary, error=err)
        await _publish(live, "done", {"status": status, "error": err, "summary": summary})
    except Exception as e:
        store.update(run_id, status="error", error=str(e))
        await _publish(live, "done", {"status": "error", "error": str(e)})
    finally:
        live.done = True
    await asyncio.to_thread(_auto_prune, cfg, run_dir / "run.log")


def _auto_prune(cfg: dict, log: Path) -> None:
    """config → retention.auto бол run дууссаны дараа хуучин run-уудыг цэвэрлэнэ (алдаа run-д нөлөөлөхгүй)."""
    if not (cfg.get("retention") or {}).get("auto"):
        return
    pol = retention.policy(cfg)
    try:
        res = retention.prune(store, RUNS_DIR, pol["keep_per_project"], pol["max_age_days"], apply=True,
                              skip_ids={k for k, v in LIVE.items() if not v.done})
        for r in res["runs"]:
            LIVE.pop(r["id"], None)
        msg = (f"[INFO] Retention: {len(res['runs'])} хуучин run устгав ({res['bytes'] / 1024 / 1024:.1f} MB)\n"
               if res["runs"] else "")
    except Exception as e:
        msg = f"[WARN] Retention цэвэрлэгээ амжилтгүй: {e}\n"
    if msg and log.parent.exists():
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(msg)


@app.post("/api/runs")
async def start_run(req: StartReq, user: dict = Depends(current_user)):
    url = normalize_url(req.url)
    if not url:
        raise HTTPException(400, "URL заавал")
    cfg = load_config(url, {"timing": {"wait_ms": int(req.wait_ms)}, "check_risky": req.check_risky,
                            "confirm_failures": req.confirm_failures})
    steps = ([s.model_dump() for s in req.steps] if req.steps is not None
             else [{"kind": "smoke", "modules": req.modules}])
    # Аюулгүй байдал: өгөгдөл өөрчилдөг алхам (crud) зөвхөн flow.allowed_hosts дээр — run эхлэхээс өмнө
    try:
        validate_steps(url, cfg, steps)
    except PipelineConfigError as e:
        raise HTTPException(400, str(e))
    project = req.project.strip() or project_from_url(url)
    version = normalize_version(req.version) or store.next_version(project)
    run_id = uuid.uuid4().hex[:12]
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    smoke_modules = next((s.get("modules") for s in steps if s["kind"] == "smoke"), None)
    opts = RunOptions(project=project, qa_lead=req.qa_lead.strip(), tester=req.tester.strip(), modules=smoke_modules,
                      wait_selection=req.select_modules and not smoke_modules,
                      check_risky=req.check_risky, confirm_failures=req.confirm_failures,
                      version_label=version)
    ref: dict = {}
    # Гар нэвтрэлт тул хөтөч ҮРГЭЛЖ headed (headless=False)
    cdp = None
    if req.cdp:
        cdp = browser_mod.cdp_url(cfg)
        if not browser_mod.cdp_alive(cdp):
            raise HTTPException(400, "Өөрийн Chrome-д холбогдож чадсангүй — эхлээд «🌐 Chrome нээх» дарж, нээгдсэн "
                                     "Chrome дээр нэвтэрнэ үү (" + cdp + ")")
    runner = PipelineRunner(run_id, url, cfg, opts, run_dir, steps, _emit_factory(ref), headless=False, cdp=cdp)
    live = LiveRun(runner)
    ref["live"] = live
    LIVE[run_id] = live
    store.create(run_id, url, urlparse(url).hostname or "", project, opts.qa_lead, version, runner.data.pipeline,
                 owner=user["username"])
    live.task = asyncio.create_task(_run_job(run_id, live, cfg))
    return {"id": run_id, "project": project, "version": version, "warning": url_warning(url),
            "run_tag": runner.data.run_tag, "pipeline": runner.data.pipeline}


@app.get("/api/flows")
async def list_flows(url: str):
    """Тухайн host-ын flow-ууд. allowed=False бол CRUD (өгөгдөл өөрчилдөг) — сонгуулахгүй."""
    url = normalize_url(url)
    if not url:
        raise HTTPException(400, "URL заавал")
    cfg = load_config(url)
    reason = host_reason(url, cfg)
    out = []
    for name, raw in (cfg.get("flows") or {}).items():
        err = ""
        try:
            spec = load_flow(cfg, name)
        except FlowConfigError as e:
            spec, err = (raw if isinstance(raw, dict) else {"steps": raw}), str(e)
        out.append({"name": name, "code": flow_code(name, spec), "steps": len(spec.get("steps") or []),
                    "recorded": bool(spec.get("recorded")),
                    "description": flow_description(spec), "allowed": not reason and not err,
                    "reason": reason or err})
    return out


@app.post("/api/runs/{run_id}/select")
async def select(run_id: str, req: SelectReq, user: dict = Depends(current_user)):
    live = _own_live(run_id, user)
    live.runner.select_modules(req.modules)
    return {"ok": True}


@app.post("/api/runs/{run_id}/crud-select")
async def crud_select(run_id: str, req: CrudSelectReq, user: dict = Depends(current_user)):
    """Smoke-ийн дараа автомат CRUD хийх цэсүүд (хоосон → алгасна). Зөвхөн нэр дэвшигчдийг хүлээн авна."""
    live = _own_live(run_id, user)
    if not live.runner.select_crud(req.menus):
        raise HTTPException(409, "Автомат CRUD-ийн сонголт хүлээгдээгүй байна")
    return {"ok": True}


@app.post("/api/runs/{run_id}/form-select")
async def form_select(run_id: str, req: CrudSelectReq, user: dict = Depends(current_user)):
    """Smoke-ийн дараа форм шалгалт хийх цэсүүд (хоосон → алгасна)."""
    live = _own_live(run_id, user)
    if not live.runner.select_forms(req.menus):
        raise HTTPException(409, "Форм шалгалтын сонголт хүлээгдээгүй байна")
    return {"ok": True}


def normalize_version(v: str) -> str:
    """«26.2.10» → «v26.2.10»; файлын нэрэнд орох тул зөвхөн аюулгүй тэмдэгт."""
    v = re.sub(r"[^\w.\-]+", "", (v or "").strip())
    if v and v[0].isdigit():
        v = "v" + v
    return v[:40]


class ChromeReq(BaseModel):
    url: str


def previous_form_na(url: str, exclude: str = "", owner: str | None = None) -> dict:
    """Ижил систем дээрх хамгийн сүүлийн форм шалгалтад засварын форм нь «тест орчинд өгөгдөлгүй» (N/A) байсан
    цэсүүд — баг тест өгөгдөл оруулсны дараа зөвхөн тэдгээрийг дахин шалгахад. owner: тухайн хэрэглэгчийн түүхээс."""
    host = urlparse(url).hostname or ""
    for r in store.list(200, owner=owner):
        if r["id"] == exclude or r.get("host") != host or r.get("status") not in ("done", "cancelled"):
            continue
        raw = RUNS_DIR / r["id"] / "raw.json"
        try:
            flows = json.loads(raw.read_text(encoding="utf-8")).get("flows") or []
        except Exception:
            continue
        form = [f for f in flows if (f.get("meta") or {}).get("formcheck")]
        if not form:
            continue
        keys = [f["meta"]["menu_key"] for f in form if any(
            s.get("uses") == "form.check_edit" and s.get("category") == "nodata" for s in f.get("steps") or [])]
        return {"run_id": r["id"], "version": (store.get(r["id"]) or {}).get("version_label", ""), "keys": keys}
    return {"run_id": "", "version": "", "keys": []}


@app.get("/api/form-na")
async def form_na(url: str, exclude: str = "", user: dict = Depends(current_user)):
    return await asyncio.to_thread(previous_form_na, url, exclude, user["username"])


@app.get("/api/chrome")
async def chrome_status():
    cfg = load_config(None)
    info = browser_mod.cdp_alive(browser_mod.cdp_url(cfg))
    return {"alive": bool(info), "version": (info or {}).get("Browser", ""), "chrome": bool(browser_mod.find_chrome())}


@app.post("/api/chrome/start")
async def chrome_start(req: ChromeReq):
    """Тусдаа профайлтай Chrome-ыг remote debugging-тэй нээнэ — хэрэглэгч captcha-г өөрөө давж нэвтэрнэ."""
    url = normalize_url(req.url)
    if not url:
        raise HTTPException(400, "URL заавал")
    cfg = load_config(url)
    if browser_mod.cdp_alive(browser_mod.cdp_url(cfg)):
        return {"ok": True, "already": True}
    try:
        profile = browser_mod.start_chrome(url)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    for _ in range(30):
        await asyncio.sleep(0.3)
        if browser_mod.cdp_alive(browser_mod.cdp_url(cfg)):
            return {"ok": True, "profile": profile}
    raise HTTPException(500, "Chrome нээгдсэн ч remote debugging хариулсангүй — Chrome-ын бүх цонхыг хаагаад дахин оролдоно уу")


@app.post("/api/runs/{run_id}/record-stop")
async def record_stop(run_id: str, user: dict = Depends(current_user)):
    live = _own_live(run_id, user)
    if not live.runner.stop_recording():
        raise HTTPException(409, "Бичлэг явагдаагүй байна")
    return {"ok": True}


def _recorded_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if name.startswith(appcfg.RECORDED_PREFIX):
        name = name[len(appcfg.RECORDED_PREFIX):]
    if not name or len(name) > 80 or re.search(r"[{}|\/:]", name):
        raise HTTPException(400, "Урсгалын нэр хоосон, хэт урт эсвэл { } | / : тэмдэгт агуулсан байна")
    return appcfg.RECORDED_PREFIX + name


@app.post("/api/recorded")
async def save_recorded(req: RecordedReq):
    """Бичсэн (UI-д засварласан байж болох) YAML-ыг шалгаад config/recorded/<host>.yaml-д хадгална."""
    url = normalize_url(req.url)
    if not url:
        raise HTTPException(400, "URL заавал")
    cfg = load_config(url)
    reason = host_reason(url, cfg)
    if reason:
        raise HTTPException(400, reason)
    key = _recorded_name(req.name)
    try:
        spec = yaml.safe_load(req.yaml)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"YAML алдаа: {str(e)[:300]}")
    if isinstance(spec, list):
        spec = {"steps": spec}
    if not isinstance(spec, dict) or not spec.get("steps"):
        raise HTTPException(400, "YAML-д `steps:` алхмууд байх ёстой")
    host = urlparse(url).hostname or ""
    flows = appcfg.load_recorded(host)
    if not spec.get("code"):
        old = (flows.get(key) or {}).get("code")
        used = {str(f.get("code")) for f in flows.values() if isinstance(f, dict)}
        spec["code"] = old or next(f"R{i:02d}" for i in range(1, 1000) if f"R{i:02d}" not in used)
    spec["recorded"] = True
    try:
        load_flow({"flows": {key: spec}}, key)          # алхам бүр бүртгэгдсэн эсэх
    except FlowConfigError as e:
        raise HTTPException(400, str(e))
    flows[key] = spec
    appcfg.save_recorded(host, flows)
    return {"ok": True, "name": key, "code": spec["code"], "steps": len(spec["steps"])}


@app.delete("/api/recorded")
async def delete_recorded(url: str, name: str):
    url = normalize_url(url)
    if not url:
        raise HTTPException(400, "URL заавал")
    reason = host_reason(url, load_config(url))       # хадгалахтай ижил: зөвхөн зөвшөөрөгдсөн (тест) host
    if reason:
        raise HTTPException(400, reason)
    host = urlparse(url).hostname or ""
    flows = appcfg.load_recorded(host)
    if name not in flows:
        raise HTTPException(404, "Бичсэн урсгал олдсонгүй")
    flows.pop(name)
    appcfg.save_recorded(host, flows)
    return {"ok": True}


@app.get("/api/menus")
async def list_menus(url: str, crud: str | None = "full"):
    """Өмнөх smoke run-уудын бүртгэсэн цэсний үйлдлийн каталог (анхдагч: автомат CRUD хийж болохууд)."""
    url = normalize_url(url)
    if not url:
        raise HTTPException(400, "URL заавал")
    cfg = load_config(url)
    reason = host_reason(url, cfg)
    rows = store.list_caps(urlparse(url).hostname or "", crud or None)
    return {"allowed": not reason, "reason": reason, "menus": [
        {"key": r["key"], "module": r["module"], "folder": r["folder"], "name": r["name"], "crud": r["crud"],
         "path": " / ".join(p for p in (r["module"], r["folder"], r["name"]) if p),
         "buttons": [b["text"] for b in r["caps"].get("buttons") or []], "columns": r["caps"].get("columns") or [],
         "edit": bool(r["caps"].get("edit")), "updated_at": r["updated_at"], "app_version": r["app_version"],
         "run_id": r["run_id"]} for r in rows]}


@app.post("/api/runs/{run_id}/stop")
async def stop(run_id: str, user: dict = Depends(current_user)):
    live = _own_live(run_id, user)
    live.runner.cancel()
    return {"ok": True}


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str, request: Request, since: int = 0, user: dict = Depends(current_user)):
    live = _own_live(run_id, user, "Идэвхтэй run алга")

    async def gen():
        idx = since
        while True:
            if await request.is_disconnected():
                return
            async with live.cond:
                if idx >= len(live.events) and not live.done:
                    try:
                        await asyncio.wait_for(live.cond.wait(), 15)
                    except asyncio.TimeoutError:
                        pass
                batch = live.events[idx:]
            for ev in batch:
                idx += 1
                yield f"id: {idx}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if not batch:
                yield ": keep-alive\n\n"
            if live.done and idx >= len(live.events):
                return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
async def list_runs(user: dict = Depends(current_user)):
    """Хэрэглэгч бүр зөвхөн өөрийн Түүхийг харна; admin бүгдийг (owner баганатай)."""
    return store.list(owner=None if user["is_admin"] else user["username"])


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(current_user)):
    r = _own(run_id, user)
    live = LIVE.get(run_id)
    r["live"] = bool(live and not live.done)
    return r


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, user: dict = Depends(current_user)):
    _own(run_id, user)
    live = LIVE.get(run_id)
    if live and not live.done:
        raise HTTPException(409, "Идэвхтэй ажиллаж байгаа run-ыг устгах боломжгүй — эхлээд зогсооно уу")
    LIVE.pop(run_id, None)
    shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
    store.delete(run_id)
    return {"ok": True}


@app.get("/api/compare")
async def compare(a: str, b: str, user: dict = Depends(current_user)):
    """Regression харьцуулалт: bug signature-аар шинэ / засагдсан / хэвээр."""
    ra, rb = _own(a, user), _own(b, user)
    if not ra or not rb or not ra.get("summary") or not rb.get("summary"):
        raise HTTPException(404, "Хоёр run-ын тайлан хэрэгтэй")
    ba = {x["signature"]: x for x in ra["summary"]["bugs"]}
    bb = {x["signature"]: x for x in rb["summary"]["bugs"]}
    return {
        "a": {k: ra[k] for k in ("id", "created_at", "app_version", "pass_rate", "decision")},
        "b": {k: rb[k] for k in ("id", "created_at", "app_version", "pass_rate", "decision")},
        "new": [bb[s] for s in bb if s not in ba],
        "fixed": [ba[s] for s in ba if s not in bb],
        "still": [bb[s] for s in bb if s in ba],
    }


def _file(run_id: str, kind: str, user: dict) -> tuple[Path, str]:
    r = _own(run_id, user)
    d = RUNS_DIR / run_id
    s = r.get("summary") or {}
    if kind == "xlsx" and s.get("xlsx"):
        return d / s["xlsx"], s["xlsx"]
    if kind == "docx" and s.get("docx"):
        return d / s["docx"], s["docx"]
    form = s.get("form") or {}
    if kind == "form_xlsx" and form.get("xlsx"):
        return d / form["xlsx"], form["xlsx"]
    if kind == "form_docx" and form.get("docx"):
        return d / form["docx"], form["docx"]
    if kind == "shots":
        return d / "screenshots.zip", f"{r['project']}-screenshots-{r['version_label']}.zip"
    if kind == "raw":
        return d / "raw.json", f"{r['project']}-raw-{r['version_label']}.json"
    if kind == "log":
        return d / "run.log", f"{r['project']}-run-{r['version_label']}.log"
    if kind == "draft" and s.get("draft"):
        return d / s["draft"], f"{r['project']}-flows-draft-{r['version_label']}.yaml"
    raise HTTPException(404, "Файл бэлэн болоогүй")


@app.get("/runs/{run_id}/download/{kind}")
async def download(run_id: str, kind: str, user: dict = Depends(current_user)):
    path, name = _file(run_id, kind, user)
    if not path.exists():
        raise HTTPException(404, "Файл олдсонгүй")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return FileResponse(path, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"})


@app.get("/runs/{run_id}/shots/{name}")
async def shot(run_id: str, name: str, user: dict = Depends(current_user)):
    if not re.fullmatch(r"[\w.-]+\.png", name):
        raise HTTPException(400)
    _own(run_id, user)
    p = RUNS_DIR / run_id / "shots" / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/check-url")
async def check_url(url: str):
    return {"warning": url_warning(url), "project": project_from_url(url)}


@app.get("/favicon.svg")
@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
async def index():
    # Кэшлэхгүй — шинэчлэлт (нэр, дүрс) хуудсыг шинэчлэхэд шууд харагдана
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
