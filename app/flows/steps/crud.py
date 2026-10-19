"""CRUD / Data Management модуль: Create → Read/Search → Update → Delete.

Аюулгүй байдал: бүртгэх утга болон устгах мөр заавал run_tag агуулна — тестийн бус
(бодит) өгөгдлийг хэзээ ч засахгүй, устгахгүй.
"""
from __future__ import annotations

import asyncio

from app import models as M
from app.flows import ui
from app.flows.context import StepBlocked, StepFailed
from app.flows.engine import step


def _require_tag(env, text: str, what: str) -> None:
    if env.ctx.run_tag not in (text or ""):
        raise StepFailed(f"{what} '{text}' нь run_tag ({env.ctx.run_tag}) агуулаагүй — тестийн бус "
                         f"өгөгдөлд хүрэхээс сэргийлж зогсоов. Утгад {{run_tag}} оруулна уу.")


async def _fill_all(env, fields: dict) -> None:
    for label, value in (fields or {}).items():
        await ui.fill_field(env, label, value)


async def _capture(env, capture: dict | None) -> dict:
    out = {}
    for name, label in (capture or {}).items():
        out[name] = await ui.read_field(env, label)
        if not out[name]:
            raise StepFailed(f"'{label}' талбарын утга хоосон — '{name}' болгон авах боломжгүй")
    return out


@step("crud.open")
async def crud_open(env, args: dict) -> dict:
    """with: {menu: "Модуль / Хавтас / Цэс"}"""
    await ui.ensure_menu(env, args["menu"])
    return {}


@step("crud.create")
async def crud_create(env, args: dict) -> dict:
    """Шинэ бичлэг бүртгэнэ.

    with:
      menu: "Мастер дата / Бүртгэл / Харилцагч"
      fields: {"Нэр": "{run_tag} Тест харилцагч", "Утас": "99112233"}
      key: "Нэр"                 # бичлэгийг дараа нь олох талбар (default: fields-ийн эхнийх)
      save_as: customer          # key-ийн утгыг ctx.data["customer"] болгож дамжуулна
      capture: {customer_code: "Код"}   # хадгалахаас өмнө формоос унших (автомат код г.м.)
      button: "Нэмэх"            # default: flow.buttons.add
    """
    fields = args.get("fields") or {}
    if not fields:
        raise StepFailed("crud.create: fields хоосон")
    key = args.get("key") or next(iter(fields))
    if key not in fields:
        raise StepFailed(f"crud.create: key '{key}' fields-д алга")
    key_value = str(fields[key])
    _require_tag(env, key_value, "Бүртгэх утга")
    await ui.ensure_menu(env, args.get("menu"))
    if env.resumed and await ui.grid_find(env, key_value):
        # Session тасрахаас өмнө хадгалагдсан байж болно — давхар бүртгэхгүй
        env.note(f"Session тасрахаас өмнө бүртгэгдсэн байсан: {key}={key_value} (дахин бүртгээгүй)")
        if args.get("capture"):
            raise StepFailed("Session тасрахаас өмнө бүртгэгдсэн тул capture талбарыг формоос уншиж чадсангүй")
        return {args.get("save_as") or "record": key_value}
    await ui.click_button(env, ui.buttons(env, "add", args.get("button")), scope="page")
    if args.get("add_menu"):
        # «Шинэ» → баримтын төрөл сонгох цэс (ж: «Кассын орлогын ордер»)
        want = str(args["add_menu"])
        for _ in range(20):
            items = await ui.popup_menu_items(env)
            if want in items:
                break
            await asyncio.sleep(0.25)
        safe, _ = ui.safe_type_items(env, [want])
        if not safe or not await ui.click_popup_item(env, want):
            raise StepFailed(f"«Шинэ» цэсэнд «{want}» төрөл олдсонгүй")
    await ui.find_field(env, key, wait_s=10)          # форм нээгдэхийг хүлээнэ
    await _fill_all(env, fields)
    captured = await _capture(env, args.get("capture"))
    await ui.save_form(env, ui.buttons(env, "save", args.get("save_button")))
    env.note(f"Бүртгэв: {key}={key_value}" + (f" {captured}" if captured else ""))
    return {args.get("save_as") or "record": key_value, **captured}


@step("crud.search")
async def crud_search(env, args: dict) -> dict:
    """Grid-ээс хайна.

    with:
      menu: "..."                # (заавал биш) өөр цэс рүү шилжих бол
      text: "{customer}"
      expect: found              # found | absent
      contains: ["99112233"]     # (заавал биш) олдсон мөрөнд байх ёстой утгууд
    """
    text = str(args["text"])
    expect = args.get("expect", "found")
    await ui.ensure_menu(env, args.get("menu"))
    rows = await ui.grid_find_settled(env, text, expect)
    if expect == "absent":
        if rows:
            raise StepFailed(f"'{text}' олдохгүй байх ёстой байсан ч {len(rows)} мөр олдлоо: {rows[0]}")
        return {}
    if not rows:
        raise StepFailed(f"'{text}' хайлтаар мөр олдсонгүй")
    for v in args.get("contains") or []:
        if not any(str(v) in r for r in rows):
            raise StepFailed(f"'{text}' мөр олдсон ч '{v}' утга агуулаагүй: {rows[0]}")
    env.note(f"{len(rows)} мөр: {rows[0]}")
    return {}


@step("crud.update")
async def crud_update(env, args: dict) -> dict:
    """Бичлэгийг олж засна.

    with:
      find: "{customer}"
      fields: {"Утас": "99887766"}
      save_as: customer          # (заавал биш) key талбар өөрчлөгдвөл шинэ утгаар дамжуулна
      key: "Нэр"
      button: "Засах"            # null → мөрийг давхар дарж засна
    """
    find = str(args["find"])
    _require_tag(env, find, "Засах мөр")
    fields = args.get("fields") or {}
    await ui.ensure_menu(env, args.get("menu"))
    sel = await ui.select_row(env, find)
    if sel.errors:      # бичлэг нээхэд системийн алдаа — аппын bug
        raise StepFailed(f"Бичлэгийг засахаар нээхэд: {sel.errors[0].text}", sel.errors)
    if sel.opened or sel.new_tabs:
        pass            # SmartApp: мөр дарахад засварын форм (цонх/таб) шууд нээгдсэн
    elif sel.focus == "mismatch":
        raise StepBlocked("Фокуслагдсан мөр манай тестийн бичлэг биш — өөр бичлэгийг засахаас сэргийлж зогсоов")
    elif "button" in args and args["button"] is None:
        await env.page.dblclick("[data-ssg-row='0']", timeout=5000)
    else:
        await ui.click_button(env, ui.buttons(env, "edit", args.get("button")), scope="page")
    first = next(iter(fields), None)
    if first:
        await ui.find_field(env, first, wait_s=10)
    if not await ui.form_has_text(env, find):
        errors, _ = await ui.popup_problems(env)       # форм ачаалагдсаны дараах системийн алдаа — аппын bug
        if errors:
            await ui.close_form(env, keep=sel.level)
            raise StepFailed(f"Бичлэгийг засахаар нээхэд: {errors[0].text}", errors)
        await ui.close_form(env, keep=sel.level)       # өөр бичлэг — ХАДГАЛАХГҮЙ хаана
        raise StepBlocked(f"Засварын форм «{find}» бичлэгийг агуулаагүй (өөр бичлэг нээгдсэн) — хадгалаагүй")
    await _fill_all(env, fields)
    await ui.save_form(env, ui.buttons(env, "save", args.get("save_button")))
    out = {}
    if args.get("save_as"):
        key = args.get("key")
        if not key or key not in fields:
            raise StepFailed("crud.update: save_as ашиглахад key (fields доторх талбар) заавал")
        _require_tag(env, str(fields[key]), "Шинэ утга")
        out[args["save_as"]] = str(fields[key])
    env.note(f"Засав: {find} → {fields}")
    return out


@step("crud.delete")
async def crud_delete(env, args: dict) -> dict:
    """Бичлэгийг устгаж, дахин хайхад олдохгүйг баталгаажуулна.

    with:
      find: "{customer}"         # заавал run_tag агуулна
      button: "Устгах"
      confirm: ["Тийм"]          # баталгаажуулах цонхны товч (default: flow.buttons.confirm)
    """
    find = str(args["find"])
    _require_tag(env, find, "Устгах мөр")
    # Цэвэрлэгээ найдвартай байх ёстой: өмнөх алхмаас үлдсэн форм/алдааны цонхыг эхлээд хаана
    await ui.close_form(env)
    await ui.clear_toasts(env)
    await ui.ensure_menu(env, args.get("menu"))
    sel = await ui.select_row(env, find)
    if sel.opened or sel.new_tabs:
        # SmartApp: мөр дарахад бичлэг нээгдсэн — хааж жагсаалтаас устгана (алдаа гарсан бол тэмдэглэнэ)
        if sel.errors:
            env.note(f"Мөр дарахад бичлэг нээгдэж алдаа гарав: {sel.errors[0].text[:150]}")
        await ui.close_form(env, keep=sel.level)
        await asyncio.sleep(1.0)                      # хожуу гарах алдааны цонх (ж: CostCenterId) — дахин хаана
        await ui.close_form(env, keep=sel.level)
        await ui.clear_toasts(env)
        if sel.new_tabs:
            await ui.close_tabs(env, sel.new_tabs)
        await ui.ensure_menu(env, args.get("menu"))
    # «Устгах» фокуслагдсан мөрийг устгадаг — фокус манай run_tag-тай мөр биш бол ХЭЗЭЭ Ч дарахгүй
    if await ui.focus_check(env, find) == "mismatch":
        raise StepBlocked(f"Фокуслагдсан мөр «{find}» биш — өөр бичлэгийг устгахаас сэргийлж зогсоов")
    before = await ui.dialog_count(env)
    await ui.click_button(env, ui.buttons(env, "delete", args.get("button")), scope="page")
    await asyncio.sleep(0.3)
    if await ui.dialog_count(env) > before:
        await ui.click_button(env, ui.buttons(env, "confirm", args.get("confirm")), scope="dialog")
    await asyncio.sleep(0.2)
    await ui.settle(env)
    try:
        await ui.raise_on_problems(env, "Устгахад")
    except StepFailed as e:
        if e.findings and all(f.kind == M.WARNING_TOAST for f in e.findings):
            # ж: «Мэдээллийг ашигласан байна» (FK) — бичлэг өөр баримтад холбогдсон; аппын bug биш, цэвэрлэгээ амжилтгүй
            await ui.close_form(env)
            raise StepBlocked(f"Устгах боломжгүй — бичлэг өөр мэдээлэлд ашиглагдсан байж болзошгүй "
                              f"(тестийн өгөгдөл «{find}» үлдсэн): {str(e)[:300]}", e.findings) from None
        raise
    rows = await ui.grid_find_settled(env, find, "absent")
    if rows:
        await asyncio.sleep(1.0)                   # хожуу гарсан системийн алдаа (ж: RiskStatus) — шалтгааныг мэдээлнэ
        errors, _ = await ui.popup_problems(env)
        if errors:
            await ui.close_form(env)
            raise StepFailed(f"Устгахад: {errors[0].text} (тестийн өгөгдөл «{find}» үлдсэн)", errors)
        raise StepFailed(f"Устгасны дараа '{find}' мөр хэвээр байна: {rows[0]}")
    env.ctx.data.setdefault("_deleted", set()).add(find)
    env.note(f"Устгав: {find}")
    return {}
