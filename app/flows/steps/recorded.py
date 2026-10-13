"""Бичсэн урсгалын (record → replay) ерөнхий `ui.*` алхмууд — app/flows/recorder.py-ийн үүсгэсэн YAML.

Аюулгүй байдал (crud.*-тэй ижил зарчим): өгөгдөл өөрчилдөг товч (Хадгалах / Устгах / Тийм …) зөвхөн
  - шинэ бичлэг (Нэмэх/Шинэ дарсны дараа), эсвэл
  - run_tag агуулсан мөр сонгосны дараа
дарагдана. run_tag-гүй (бодит) мөр сонгоод хадгалах/устгах гэвэл StepBlocked — бодит өгөгдөлд хүрэхгүй.
Товч бүрийн дараа системийн алдааг шалгана; хадгалах төрлийн товчны дараа validation анхааруулга ч Fail.
"""
from __future__ import annotations

import asyncio

from app.flows import ui
from app.flows.context import StepBlocked, StepFailed
from app.flows.engine import step
from app.flows.recorder import button_kind, is_cancel

MUTATING = {"save", "delete", "confirm"}


def _set_scope(env, tagged: bool | None) -> None:
    """None — сонголтгүй (цэс шинээр нээсэн); True — шинэ эсвэл tag-тай бичлэг; False — бодит бичлэг."""
    env.ctx._rec_scope = tagged


async def _after(env, what: str, strict: bool) -> None:
    await asyncio.sleep(0.2)
    await ui.settle(env, 15)
    await asyncio.sleep(ui.fcfg(env).get("form_error_grace_ms", 500) / 1000)
    await ui.raise_on_problems(env, what, warnings_too=strict)


@step("ui.menu")
async def ui_menu(env, args: dict) -> dict:
    """with: {menu: "module|folder|name|href"} — модуль тодорхойгүй бол бүх модулиас хайна."""
    await ui.open_menu_any(env, args["menu"])
    _set_scope(env, None)
    return {}


@step("ui.click")
async def ui_click(env, args: dict) -> dict:
    """with: {button: "Хадгалах", scope: page|dialog|popup} эсвэл {icon: "fa-trash", scope} (текстгүй товч);
    any: true — товч биш дарагддаг элемент (холбоос, toolbar/menu item)."""
    text = str(args.get("button") or "")
    icon = str(args.get("icon") or "")
    scope = args.get("scope") or "auto"
    kind = button_kind(text or icon)
    label = text or icon
    scope_tagged = getattr(env.ctx, "_rec_scope", None)
    if kind in MUTATING and scope_tagged is False:
        raise StepBlocked(f"«{label}» — run_tag-гүй (бодит) бичлэг сонгогдсон байна; бодит өгөгдлийг өөрчлөхөөс "
                          "сэргийлж дарсангүй. Бичихдээ өөрийн бүртгэсэн бичлэгийг сонгоно уу.")
    if kind == "delete" and scope_tagged is None:
        # Мөр дараагүй (ж: хайсны дараа шууд «Устгах») — Устгах ФОКУСЛАГДСАН мөр дээр ажилладаг тул тэр мөр
        # манай (run_tag-тай) бичлэг эсэхийг шалгана; үгүй бол бодит бичлэг устгахаас сэргийлж зогсооно
        if not await ui.only_tagged_focus(env):
            raise StepBlocked(f"«{label}» — фокуслагдсан мөр run_tag-тай бичлэг биш (эсвэл тодорхойгүй); бодит "
                              "бичлэг устгахаас сэргийлж дарсангүй")
    if scope == "popup":
        for _ in range(20):
            if await ui.click_popup_item(env, text):
                break
            await asyncio.sleep(0.25)
        else:
            raise StepFailed(f"Dropdown-ы «{text}» зүйл олдсонгүй")
    elif icon and not text:
        if not await ui.click_icon(env, icon, scope):
            raise StepFailed(f"«{icon}» дүрстэй товч олдсонгүй")
    else:
        try:
            await ui.click_button(env, [text], scope=scope, wait_s=10 if not args.get("any") else 2)
        except StepFailed as e:
            # Товч биш дарагддаг элемент (холбоос, toolbar/menu item) эсвэл товч өөр selector-той
            if e.findings or not await ui.click_text_any(env, text, scope):
                raise
    if kind == "add" or scope == "popup":
        _set_scope(env, True)          # шинэ бичлэгийн форм
    if is_cancel(text):
        await asyncio.sleep(0.3)
        return {}
    await _after(env, f"«{label}» дарахад", strict=kind in MUTATING)
    return {}


@step("ui.fill")
async def ui_fill(env, args: dict) -> dict:
    """with: {field: "Нэр", value: "{run_tag} ..."}"""
    field = str(args["field"])
    info = await ui.reveal_field(env, field)
    if info.get("readonly") and not info.get("combo"):
        raise StepFailed(f"'{field}' талбар засах боломжгүй (readonly)")
    await ui.fill_field(env, field, args.get("value", ""))
    await ui.raise_on_problems(env, f"'{field}' бөглөхөд", warnings_too=False)
    return {}


@step("ui.check")
async def ui_check(env, args: dict) -> dict:
    field = str(args["field"])
    await ui.reveal_field(env, field)
    await ui.fill_field(env, field, bool(args.get("value")))
    return {}


@step("ui.choose")
async def ui_choose(env, args: dict) -> dict:
    """with: {field, option} — select / ComboBox-оос яг тэр сонголтыг."""
    field, option = str(args["field"]), str(args["option"])
    info = await ui.reveal_field(env, field)
    if info["tag"] == "select":
        await env.page.locator("[data-ssg-field='1']").select_option(label=option)
        return {}
    if not await ui.choose_combo(env, field, option):
        raise StepFailed(f"'{field}' жагсаалтаас «{option}» олдсонгүй")
    await ui.raise_on_problems(env, f"'{field}' сонгоход", warnings_too=False)
    return {}


@step("ui.pick")
async def ui_pick(env, args: dict) -> dict:
    """with: {field, row} — lookup ("...") цонхноос `row` текст агуулсан мөрийг сонгоно."""
    field, want = str(args["field"]), str(args.get("row") or "")
    info = await ui.reveal_field(env, field)
    if not info.get("lookup"):
        raise StepFailed(f"'{field}' талбарт сонгох («...») товч олдсонгүй")
    got = await ui.pick_from_lookup(env, f"'{field}' талбарын сонголтын", want=want)
    if not got:
        raise StepFailed(f"'{field}' lookup-оос «{want}» агуулсан мөр олдсонгүй")
    env.note(f"{field} ← {got[:80]}")
    return {}


@step("ui.row")
async def ui_row(env, args: dict) -> dict:
    """with: {text, open, dialog} — жагсаалтаас (dialog: дээд цонхны grid-ээс) мөр сонгоно
    (open: давхар дарж бичлэгийг нээнэ)."""
    text = str(args["text"])
    if args.get("dialog"):
        if not await ui.dialog_row(env, text, bool(args.get("open"))):
            raise StepFailed(f"Цонхны жагсаалтаас «{text}» агуулсан мөр олдсонгүй")
        # Цонх (форм) доторх жагсаалтаас утга сонгох — бичлэг нээх биш, аюулгүй байдлын хүрээг өөрчлөхгүй
        await _after(env, "Мөр сонгоход", strict=False)
        return {}
    sel = await ui.select_row(env, text)
    if sel.errors:
        raise StepFailed(f"Мөр сонгоход: {sel.errors[0].text}", sel.errors)
    _set_scope(env, env.ctx.run_tag in text)
    if args.get("open") and not (sel.opened or sel.new_tabs):
        try:
            await env.page.dblclick("[data-ssg-rowsel='1'], [data-ssg-row='0']", timeout=5000)
        except Exception:
            pass
        await _after(env, "Бичлэг нээхэд", strict=False)
    return {}


@step("ui.key")
async def ui_key(env, args: dict) -> dict:
    """with: {field, key} — талбар дээр товчлуур (ж: Enter-ээр хадгалах). Хадгалах мэт тул strict шалгалттай."""
    field = str(args["field"])
    if getattr(env.ctx, "_rec_scope", None) is False:
        raise StepBlocked(f"«{field}» дээр Enter — run_tag-гүй (бодит) бичлэг нээлттэй; хадгалж магадгүй тул дарсангүй")
    await ui.reveal_field(env, field)
    await env.page.locator("[data-ssg-field='1']").press(str(args.get("key") or "Enter"))
    await _after(env, f"«{field}» дээр {args.get('key') or 'Enter'}", strict=True)
    return {}


@step("ui.row_check")
async def ui_row_check(env, args: dict) -> dict:
    """with: {text, value, dialog} — мөрийн сонголтын checkbox."""
    text = str(args["text"])
    if not await ui.row_check(env, text, bool(args.get("value", True)), bool(args.get("dialog"))):
        raise StepFailed(f"«{text}» мөрийн checkbox олдсонгүй")
    if not args.get("dialog"):
        _set_scope(env, env.ctx.run_tag in text)
    return {}


@step("ui.search")
async def ui_search(env, args: dict) -> dict:
    text = str(args.get("text") or "")
    if args.get("dialog"):
        await ui._lookup_search(env, text)
        await _after(env, "Хайхад", strict=False)
        return {}
    if not await env.page.evaluate(ui.MARK_SEARCH_JS, [ui.sel_payload(env)]):
        raise StepFailed("Жагсаалтын хайлтын талбар олдсонгүй")
    loc = env.page.locator("[data-ssg-search='1']")
    await loc.fill(text)
    await loc.press("Enter")
    await _after(env, "Хайхад", strict=False)
    return {}


@step("ui.tab")
async def ui_tab(env, args: dict) -> dict:
    name = str(args["name"])
    if not await ui.click_tab(env, name):
        raise StepFailed(f"«{name}» таб олдсонгүй")
    await ui.settle(env, 10)
    return {}


@step("ui.line_add")
async def ui_line_add(env, args: dict) -> dict:
    if not await env.page.evaluate(ui.MARK_LINES_ADD_JS, [ui.sel_payload(env)]):
        raise StepFailed("Мөрийн grid-ийн «+» товч олдсонгүй")
    env._line_hint = ""
    await env.page.click("[data-ssg-lineadd='1']", timeout=5000)
    await asyncio.sleep(0.4)
    await ui.raise_on_problems(env, "Мөр нэмэхэд", warnings_too=False)
    return {}


@step("ui.cell")
async def ui_cell(env, args: dict) -> dict:
    column, value = str(args["column"]), str(args.get("value") or "")
    i = await ui.line_column(env, column)
    if not await ui._fill_cell(env, i, value):
        raise StepFailed(f"Мөрийн «{column}» нүдэнд утга оруулж чадсангүй")
    return {}


@step("ui.cell_pick")
async def ui_cell_pick(env, args: dict) -> dict:
    column, want = str(args["field"]), str(args.get("row") or "")
    i = await ui.line_column(env, column)
    await ui._activate_cell(env, i)
    if not await env.page.evaluate(ui.MARK_CELL_LOOKUP_JS, i):
        raise StepFailed(f"Мөрийн «{column}» нүдний «...» товч олдсонгүй")
    got = await ui.pick_from_lookup(env, f"Мөрийн '{column}' баганын сонголтын", want=want)
    if not got:
        raise StepFailed(f"Мөрийн «{column}» lookup-оос «{want}» олдсонгүй")
    env._line_hint = got.split()[0] if got.split() else ""
    return {}
