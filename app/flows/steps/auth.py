"""Auth & State модуль: pipeline-ын session ашиглах, session дамжуулах, эрх шалгах.

Нэвтрэлт ЗӨВХӨН гараар (pipeline эхлэхэд). auth.login нууц үг уншихгүй — идэвхтэй session-ийг
ашиглана, session тасарсан бол хэрэглэгч дахин гараар нэвтрэхийг хүлээнэ.
"""
from __future__ import annotations

from app.flows import ui
from app.flows.context import StepFailed
from app.flows.engine import step
from app.runner.session import base_url, wait_login_or_app


@step("auth.login", needs_session=False)
async def auth_login(env, args: dict) -> dict:
    """Pipeline-ын идэвхтэй (гараар нэвтэрсэн) session-ийг ашиглана.

    with:
      role: admin            # мэдээллийн чанартай — олон role дэмжигдэхгүй; идэвхтэй session-ээр үргэлжилнэ
      new_context: false     # true → идэвхтэй session-ийн storage_state-ээр ШИНЭ browser context нээнэ
                             #        (тусдаа тест модуль session-ийг хүлээн авч буйг шалгана)
    """
    role = args.get("role")
    if role:
        env.role = role
        env.log(f"[{env.flow_name}] role '{role}' заасан ч олон role дэмжигдэхгүй — идэвхтэй (гараар "
                f"нэвтэрсэн) session-ээр үргэлжилнэ", "warn")
        env.note(f"role '{role}' → идэвхтэй session ашиглав")
    if args.get("fresh"):
        env.note("fresh: дэмжигдэхгүй (нэвтрэлт зөвхөн гараар) — идэвхтэй session ашиглав")
    await env.session.ensure(f"{env.flow_name} › auth.login")
    if args.get("new_context"):
        # storage_state (cookie + localStorage) зөвхөн санах ойд — файлд бичигдэхгүй
        main_page = env._main[0]
        state = await main_page.context.storage_state()
        await env.open_context(state)
        cfg = env.cfg
        await env.page.goto(base_url(env.url), wait_until="domcontentloaded",
                            timeout=cfg["timing"]["app_ready_timeout_ms"])
        if await wait_login_or_app(env.page, cfg, cfg["timing"]["app_ready_timeout_ms"],
                                   env.cancel_event) != "app":
            raise StepFailed("Идэвхтэй session-ийн storage_state-ээр шинэ context нэвтэрсэн төлөвт орсонгүй")
        env.note("Идэвхтэй session-ийн storage_state-ээр шинэ context дахин нэвтрэлгүй орлоо")
        return {}
    env.note("Pipeline-ын идэвхтэй session-ийг ашиглав")
    return {}


@step("auth.expect_access")
async def auth_expect_access(env, args: dict) -> dict:
    """Идэвхтэй role тухайн цэсэд хандах эрхтэй/эрхгүйг шалгана.

    with:
      menu: "Мастер дата / Бүртгэл / Харилцагч"
      allowed: true          # false → 403 эсвэл цэс харагдахгүй байх ёстой
    """
    menu = args["menu"]
    allowed = bool(args.get("allowed", True))
    outcome = await ui.open_menu(env, menu)
    env.note(f"Үр дүн: {outcome}")
    if allowed and outcome != "ok":
        raise StepFailed(f"'{env.role}' '{menu}' цэсэд хандах ёстой байсан ч {outcome}")
    if not allowed and outcome == "ok":
        raise StepFailed(f"'{env.role}' эрхгүй байх ёстой '{menu}' цэсийг нээж чадлаа (эрхийн алдаа)")
    return {}
