"""Цэс нээх, шаардлагатай бол аппыг дахин ачаалах."""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from app import models as M
from app.flows.context import StepBlocked, StepFailed
from app.models import Finding
from app.runner import discovery
from app.runner.detectors import snapshot, tab_is
from app.flows.ui.base import fcfg, menu_item
from app.flows.ui.wait import raise_on_problems, settle

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine


# ------------------------------------------------------------------ цэс
async def reload_app(env: "FlowEngine", why: str) -> bool:
    """Апп-ыг дахин ачаална (smoke-ийн crash recovery-тэй ижил). Blazor компонент унасны (unhandled rendering
    exception) дараа WASM апп буруу төлөвт орж цэс/таб хэвийн ажиллахаа больдог. Нэвтрэлт хадгалагдана;
    login хуудас гарвал engine-ийн session guard дараагийн алхмын өмнө гар нэвтрэлт хүлээнэ."""
    from app.runner.session import wait_login_or_app
    t = env.cfg["timing"]
    env.log(f"Апп-ыг дахин ачаалж байна — {why}", "warn")
    try:
        await env.page.reload(wait_until="domcontentloaded", timeout=t["app_ready_timeout_ms"])
        state = await wait_login_or_app(env.page, env.cfg, t["app_ready_timeout_ms"], env.cancel_event)
    except Exception as e:
        env.log(f"Дахин ачаалах алдаа: {e!r}", "warn")
        return False
    if state == "app":
        await settle(env, 15)
        return True
    return state == "login"


async def open_menu(env: "FlowEngine", path: str, _retry: bool = True) -> str:
    """Цэсийг нээнэ. 'ok' | 'forbidden' | 'missing' буцаана. Таб нээгдэхгүй бол апп-ыг дахин ачаалж нэг удаа
    дахин оролдоно (өмнөх цэсийн Blazor уналтаас үлдсэн эвдэрсэн төлөв)."""
    item = menu_item(path)
    page, cfg = env.page, env.cfg
    aliases = env.ctx.__dict__.setdefault("_tab_alias", {})    # цэсийн нэр → бодит табын гарчиг
    st = await snapshot(page, cfg)
    if st and (tab_is(st.active_tab, item.name) or (aliases.get(item.name) and st.active_tab == aliases[item.name])):
        return "ok"
    tabs_before = st.tab_texts() if st else []
    if not await discovery.mark_leaf(page, cfg, item):
        st = await snapshot(page, cfg)
        if _retry and (st is None or st.dead) and await reload_app(
                env, f"'{item.name}' цэс олдсонгүй — апп (.NET runtime) унасан байна"):
            env.note("Апп унасан байсан тул дахин ачаалж цэсийг дахин хайв")
            return await open_menu(env, path, _retry=False)
        return "missing"
    try:
        await page.click("[data-ssg-target='1']", timeout=5000)
    except Exception as e:
        # Цэсний холбоосыг дарж чадсангүй (өөр элемент хааж байна, цэс хураагдсан г.м.) — хэрэгслийн
        # хязгаарлалт, аппын bug биш
        raise StepBlocked(f"'{item.name}' цэсийг дарж чадсангүй: {str(e).splitlines()[0][:150]}") from None
    forbidden_tab = cfg["selectors"].get("forbidden_tab_name", "_403")
    # Таб ихэвчлэн 1–3 сек-д нээгддэг; ачаалал (loading) үргэлжилж байвал loading_blocked_ms хүртэл хүлээнэ
    deadline = time.monotonic() + cfg["timing"]["loading_blocked_ms"] / 1000
    no_load_deadline = time.monotonic() + fcfg(env).get("tab_open_timeout_ms", 10000) / 1000
    while time.monotonic() < deadline:
        st = await snapshot(page, cfg)
        if st:
            if st.forbidden or st.active_tab == forbidden_tab:
                return "forbidden"
            if tab_is(st.active_tab, item.name) and not st.loading:
                break
            # Шинэ таб нээгдсэн ч гарчиг нь цэсийн нэрээс өөр (товчилсон/англи нэр) — нээгдсэн гэж үзэж санана
            if (st.active_tab and st.active_tab not in tabs_before and st.active_tab != forbidden_tab
                    and not st.loading and not st.dialogs):
                aliases[item.name] = st.active_tab
                env.note(f"Табын гарчиг «{st.active_tab}» цэсийн нэр «{item.name}»-ээс өөр")
                break
            await raise_on_problems(env, f"'{item.name}' цэсийг нээхэд", warnings_too=False)
            if st.dead or (not st.loading and time.monotonic() > no_load_deadline):
                deadline = 0          # ачааллагүйгээр таб нээгдэхгүй байна — эвдэрсэн төлөв
                break
        await asyncio.sleep(0.15)
    if deadline == 0 or time.monotonic() >= deadline:
        if _retry and await reload_app(env, f"'{item.name}' цэсний таб нээгдсэнгүй (идэвхтэй таб: "
                                            f"{st.active_tab if st else '—'})"):
            env.note("Цэсний таб нээгдээгүй тул апп-ыг дахин ачаалж дахин нээв")
            return await open_menu(env, path, _retry=False)
        raise StepFailed(f"'{item.name}' цэсний таб нээгдсэнгүй (идэвхтэй таб: {st.active_tab if st else '—'})")
    await settle(env)
    await raise_on_problems(env, f"'{item.name}' цэсийг нээхэд", warnings_too=False)
    return "ok"


async def open_menu_any(env: "FlowEngine", key: str) -> None:
    """Бичсэн урсгалын цэс: модуль мэдэгдэж байвал шууд, үгүй бол (бичихэд модуль сонгоогүй) модуль бүрээс хайна."""
    item = menu_item(key)
    if item.module:
        return await ensure_menu(env, key)
    outcome = await open_menu(env, key)
    if outcome == "missing":
        for m in await discovery.list_modules(env.page, env.cfg):
            outcome = await open_menu(env, f"{m}|{item.folder}|{item.name}|{item.href}")
            if outcome != "missing":
                break
    if outcome == "forbidden":
        raise StepFailed(f"'{item.name}' цэсэд хандах эрхгүй (403)", [Finding(M.FORBIDDEN, item.name)])
    if outcome == "missing":
        raise StepFailed(f"'{item.name}' цэс олдсонгүй")


async def ensure_menu(env: "FlowEngine", path: str | None) -> None:
    if not path:
        return
    outcome = await open_menu(env, path)
    if outcome == "forbidden":
        raise StepFailed(f"'{path}' цэсэд хандах эрхгүй (403)", [Finding(M.FORBIDDEN, path)])
    if outcome == "missing":
        raise StepFailed(f"'{path}' цэс олдсонгүй")
