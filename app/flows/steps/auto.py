"""Автомат CRUD: YAML бичилгүйгээр цэсний формыг илрүүлж, тестийн утгаар бөглөнө.

Зөвхөн Нэмэх + Устгах хоёулаа байгаа цэс дээр ажиллана (тестийн өгөгдлийг цэвэрлэж чадах ёстой).
Бүртгэх бичлэгийн түлхүүр талбар заавал run_tag агуулна. Формыг найдвартай бөглөж чадахгүй бол
(заавал сонголтын талбар, систем утгыг хүлээж аваагүй …) StepBlocked(skip_flow) — аппын bug биш,
«автоматаар шалгаж чадаагүй» гэж тэмдэглэгдэнэ; ийм цэсэд YAML flow бичнэ.
"""
from __future__ import annotations

import asyncio
import datetime
import re
import time

from app import models as M
from app.flows import ui
from app.flows.context import StepBlocked, StepFailed
from app.flows.engine import step

TEXT_TYPES = {"", "text", "search"}
# config/default.yaml → flow.auto-той ижил (сайтын тохиргоо дарж болно)
DEFAULT_LABELS = {
    "key_labels": ["нэр", "name", "гарчиг", "title", "тайлбар", "утга", "description"],
    "code_labels": ["код", "code", "дугаар"],
    "phone_labels": ["утас", "phone"],
    "email_labels": ["имэйл", "и-мэйл", "email", "e-mail"],
    "number_labels": ["тоо", "дүн", "үнэ", "хувь", "хэмжээ", "хоног", "amount", "qty"],
}


def _match(label: str, words: list[str]) -> bool:
    low = label.lower()
    return any(w in low for w in words)


def _uid(args: dict) -> str:
    """save_as "a06_rec" → "A06" (урсгалын код) — алхам бүрийн утгыг урсгалаар давтагдашгүй болгоно."""
    m = re.match(r"^([a-z]\d+)_", str(args.get("save_as") or ""))
    return m.group(1).upper() if m else ""


def today_str(fields: list[dict]) -> str:
    """Өнөөдрийн огноо — формд бөглөгдсөн өөр огноо (ж: «Баримтын огноо 2026-09-30» эсвэл 30.09.2026) байвал
    түүний форматаар, үгүй бол YYYY-MM-DD."""
    d = datetime.date.today()
    for f in fields:
        v = (f.get("value") or "").strip()
        m = re.fullmatch(r"\d{4}([-./])\d{2}\1\d{2}", v)
        if m:
            return d.strftime(f"%Y{m.group(1)}%m{m.group(1)}%d")
        m = re.fullmatch(r"\d{2}([-./])\d{2}\1\d{4}", v)
        if m:
            return d.strftime(f"%d{m.group(1)}%m{m.group(1)}%Y")
    return d.isoformat()


def plan_values(fields: list[dict], tag: str, auto_cfg: dict,
                uid: str = "") -> tuple[dict[str, str], str, list[str], list[str]]:
    """(бичих утгууд {шошго: утга}, түлхүүр талбар, СОНГОХ заавал талбарууд (select/combobox/lookup),
    бөглөж чадахгүй заавал талбарууд (огноо, checkbox …)).

    uid (ж: "A06") — урсгал бүрийн давтагдашгүй тэмдэг: нэг хүснэгтийн өөр харагдац (Гүйлгээний жагсаалт ба
    /харьцсан дансаар/) нэг run-д бичлэг үүсгэхэд утга давхцаж, хайлт/устгал будилахаас сэргийлнэ."""
    text_val = f"{tag} {uid} автомат тест" if uid else f"{tag} автомат тест"
    code_val = f"{tag}{uid[1:]}" if uid else tag
    words = {**DEFAULT_LABELS, **{k: v for k, v in (auto_cfg or {}).items() if v}}
    key_w, code_w = words["key_labels"], words["code_labels"]
    phone_w, email_w, num_w = words["phone_labels"], words["email_labels"], words["number_labels"]

    def is_choice(f: dict) -> bool:
        return f["tag"] == "select" or bool(f.get("combo")) or bool(f.get("lookup"))

    def is_text(f: dict) -> bool:
        return f["tag"] in ("input", "textarea") and f["type"] in TEXT_TYPES and not is_choice(f)

    def is_empty(f: dict) -> bool:
        v = (f["value"] or "").strip()
        # Заавал тоон талбарын анхдагч 0 (ж: «Дүн * 0») — хоосон гэж үзнэ (мөрийн дүнтэй тэнцүү 1 болно)
        return not v or (f["required"] and re.fullmatch(r"0+([.,]0+)?", v) is not None)

    # DevExpress ComboBox (гараар бичих боломжгүй) input нь readonly — гэхдээ жагсаалтаас сонгож болно
    empty = [f for f in fields if (not f["readonly"] or f.get("combo")) and is_empty(f)]
    texts = [f for f in empty if is_text(f) and not f.get("date")]
    # Түлхүүр: заавал (*) нэр/утга төрлийн → аль ч нэр/утга → заавал текст → код → эхний текст
    key_f = (next((f for f in texts if f["required"] and _match(f["label"], key_w)), None)
             or next((f for f in texts if _match(f["label"], key_w)), None)
             or next((f for f in texts if f["required"] and not _match(f["label"], code_w)), None)
             or next((f for f in texts if _match(f["label"], code_w)), None)
             or (texts[0] if texts else None))
    values: dict[str, str] = {}
    picks: list[str] = []
    unresolved: list[str] = []
    for f in empty:
        label, required = f["label"], f["required"]
        if key_f is not None and f is key_f:
            values[label] = code_val if _match(label, code_w) else text_val
        elif not required:
            continue            # заавал биш талбарыг хөндөхгүй
        elif f.get("date"):
            values[label] = today_str(fields)   # формын бусад огнооны форматаар өнөөдөр
        elif is_choice(f):
            picks.append(label)  # эхний боломжит утгыг сонгоно (lookup "..." / combobox / select)
        elif f["type"] == "email" or (is_text(f) and _match(label, email_w)):
            values[label] = f"{tag.lower()}@example.com"
        elif f["type"] == "tel" or (is_text(f) and _match(label, phone_w)):
            values[label] = "99000000"
        elif f["type"] == "number" or (is_text(f) and _match(label, num_w)):
            values[label] = "1"
        elif is_text(f):
            values[label] = code_val
        else:                   # огноо, checkbox … — таах боломжгүй
            unresolved.append(label)
    return values, (key_f["label"] if key_f else ""), picks, unresolved


async def wait_opened(env, before: int, tabs_before: list[str], menus: bool = True,
                      timeout_s: float | None = None) -> tuple[str, list[str], list[str]]:
    """Товч (Нэмэх/Шинэ/Засах) дарсны дараа юу нээгдсэнийг хүлээнэ:
    "dialog" (popup форм) | "tab" (шинэ таб дээрх баримтын форм) | "menu" (төрөл сонгох dropdown) | "" (юу ч)."""
    if timeout_s is None:
        timeout_s = ui.fcfg(env).get("form_open_timeout_ms", 8000) / 1000
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await ui.dialog_count(env) > before:
            return "dialog", [], []
        new_tabs = [t for t in await ui.tab_names(env) if t not in tabs_before]
        if new_tabs:
            return "tab", new_tabs, []
        menu_items = await ui.popup_menu_items(env) if menus else []
        if menu_items:
            return "menu", [], menu_items
        await asyncio.sleep(0.2)
    return "", [], []


@step("crud.auto_create")
async def auto_create(env, args: dict) -> dict:
    """with: {menu: <цэсний түлхүүр/зам>, save_as: a01_rec, field_as: a01_field}"""
    tag = env.ctx.run_tag
    add, delete = ui.buttons(env, "add"), ui.buttons(env, "delete")
    await ui.ensure_menu(env, args["menu"])
    if not await ui.has_button(env, add):
        raise StepBlocked(f"Нэмэх товч ({' / '.join(add)}) олдсонгүй", skip_flow=True)
    if not await ui.has_button(env, delete):
        raise StepBlocked("Устгах товч олдсонгүй — тестийн өгөгдлийг цэвэрлэх боломжгүй тул бүртгэхгүй",
                          skip_flow=True)
    before, tabs_before = await ui.dialog_count(env), await ui.tab_names(env)
    await ui.click_button(env, add, scope="page")
    opened, new_tabs, menu_items = await wait_opened(env, before, tabs_before)
    env.run.meta["opened"] = opened or "nothing"
    try:
        if opened == "menu":
            raise StepBlocked(f"«{' / '.join(add)}» нь төрөл сонгох цэстэй ({', '.join(menu_items[:8])}) — "
                              "автоматаар сонгохгүй; YAML flow бичнэ үү", skip_flow=True)
        if not opened:
            raise StepBlocked("Нэмэх/Шинэ дарахад форм (цонх эсвэл шинэ таб) нээгдсэнгүй", skip_flow=True)
        if opened == "tab":
            await ui.settle(env)                       # шинэ табын форм (WASM компонент) ачаалагдахыг хүлээнэ
        fields = await ui.scan_form(env, wait_s=15 if opened == "tab" else 10)
        env.run.meta["form"] = [{k: f.get(k) for k in ("label", "type", "tag", "combo", "date", "lookup", "required",
                                                        "readonly", "source")} for f in fields]
        if not fields:
            raise StepBlocked(f"Форм нээгдсэн ({'цонх' if opened == 'dialog' else 'шинэ таб: ' + ', '.join(new_tabs)}) "
                              "боловч талбарын шошго илэрсэнгүй — оношлогоог тайлангаас харна уу", skip_flow=True)
        values, key, picks, unresolved = plan_values(fields, tag, ui.fcfg(env).get("auto") or {},
                                                     _uid(args))
        meta_fields = {l: v.replace(tag, "{run_tag}") for l, v in values.items()}
        env.run.meta.update(fields={**meta_fields, **{l: "TODO" for l in picks + unresolved}}, key=key)
        if unresolved:
            raise StepBlocked(f"Заавал талбарыг автоматаар бөглөх боломжгүй: {', '.join(unresolved)} "
                              "— энэ цэсэд YAML flow бичнэ үү (ноорог тайланд)", skip_flow=True)
        if not key:
            raise StepBlocked("run_tag бичих текст талбар олдсонгүй", skip_flow=True)
        form_level = await ui.dialog_count(env)
        for label, value in values.items():
            await ui.fill_field(env, label, value)
            if await ui.dialog_count(env) > form_level:
                # Текст гэж таньсан талбар бичихэд сонголтын (lookup) цонх нээлээ — хаагаад зогсоно
                await ui.close_form(env, keep=form_level)
                raise StepBlocked(f"'{label}' талбарт бичихэд сонголтын цонх нээгдлээ (lookup гэж танигдаагүй) "
                                  "— YAML flow бичнэ үү", skip_flow=True)
        picked: dict[str, str] = {}
        for label in picks:                    # формын дарааллаар (хамааралтай lookup)
            info = await ui.find_field(env, label)
            current = (info.get("value") or "").strip()
            if current:
                # Өмнөх сонголтоор систем автоматаар бөглөсөн (ж: Дансны төрөл → Дансны бүлэг) — хөндөхгүй
                picked[label] = current
                env.run.meta["fields"][label] = f"(автоматаар бөглөгдсөн: {current[:60]})"
                continue
            val = await ui.pick_option(env, label)
            if await ui.dialog_count(env) > form_level:
                await ui.close_form(env, keep=form_level)
            if not val and info.get("readonly"):
                continue        # идэвхгүй (disabled) сонголт — хадгалах үеийн анхааруулга шийднэ
            if not val:
                raise StepBlocked(f"'{label}' сонголтын талбарт сонгох утга олдсонгүй (хоосон жагсаалт эсвэл "
                                  "сонголт тогтсонгүй) — YAML flow бичнэ үү", skip_flow=True)
            picked[label] = val
            env.run.meta["fields"][label] = f"TODO сонголт (автомат: {val[:60]})"
        env.note("Бөглөсөн: " + ", ".join(f"{l}={v}" for l, v in values.items())
                 + (" | Сонгосон: " + ", ".join(f"{l}={v}" for l, v in picked.items()) if picked else ""))
        await _save_with_lines(env, form_level)
    except StepFailed as e:
        # Оношлогоо + screenshot — формыг хаахаас ӨМНӨ (юу харагдаж байсныг хадгална)
        env.run.meta["probe"] = await ui.probe(env)
        if env.current is not None:
            env.current.screenshot = await env.screenshot("auto-form")
        env.runlog.log(f"[{env.flow_name}] автомат форм оношлогоо: {env.run.meta['probe']}", "warn", to_ui=False)
        await ui.close_form(env, keep=before)
        if menu_items:
            try:
                await env.page.keyboard.press("Escape")
            except Exception:
                pass
        if new_tabs:
            await ui.close_tabs(env, new_tabs)
        if isinstance(e, StepBlocked):
            raise
        if e.findings and all(f.kind == M.WARNING_TOAST for f in e.findings):
            # Систем автоматаар таасан утгыг хүлээж аваагүй (validation) — аппын bug биш
            raise StepBlocked(f"Автоматаар бөглөсөн утгыг систем хүлээж авсангүй: {e}", e.findings,
                              skip_flow=True) from None
        if not e.findings and str(e).startswith(("Товч олдсонгүй", "'")):
            # Хадгалах товч / талбар олдоогүй — тохиргоо (selector) зөрсөн, аппын bug биш
            raise StepBlocked(f"Автомат форм: {e}", skip_flow=True) from None
        if not e.findings and "хаагдсангүй" in str(e):
            # Алдаа/анхааруулгагүйгээр форм хаагдаагүй — автоматаар бөглөсөн утга дутуу байх магадлалтай
            extra = [d.get("text", "").replace("\n", " | ")[:120] for d in env.run.meta["probe"].get("dialogs", [])[1:]]
            raise StepBlocked(f"Хадгалсны дараа форм хаагдсангүй (системийн алдаагүй)"
                              + (f": {'; '.join(extra)}" if extra else "") + " — YAML flow бичнэ үү",
                              skip_flow=True) from None
        raise
    if opened == "tab":
        await _close_form_tabs(env, tabs_before)
    return {args["save_as"]: values[key], args["field_as"]: key}


async def _save_with_lines(env, form_level: int, max_rounds: int = 8) -> None:
    """Хадгална. Систем validation анхааруулга буцаавал (аппын алдаа биш) мессежийг уншиж засаад дахин:

      1. «<Талбар> оруулна уу / сонгоно уу» → формд тийм талбар байвал бөглөнө/сонгоно
         (тоон шошго эсвэл 0 утгатай бол 1, сонголтын талбар бол эхний утга, бусад нь run_tag)
      2. талбар биш бол (ж: «Данс оруулна уу», «Барааны мэдээлэл оруулна уу») → мөрийн grid-д мөр нэмнэ
    Алдаа (SQL, 5xx, «Системийн алдаа») гарвал шууд дамжуулна → Fail/bug."""
    save = ui.buttons(env, "save")
    titles = ui.fcfg(env).get("validation_titles") or []
    words = {**DEFAULT_LABELS, **{k: v for k, v in (ui.fcfg(env).get("auto") or {}).items() if v}}
    tag = env.ctx.run_tag
    handled: set[str] = set()
    tried_lines = False
    last: StepFailed | None = None
    for _ in range(max_rounds):
        try:
            await ui.save_form(env, save)
            return
        except StepFailed as e:
            if not (e.findings and all(f.kind == M.WARNING_TOAST for f in e.findings)):
                raise
            last = e
        message = "\n".join(f.text for f in last.findings)
        await ui.close_form(env, keep=form_level)      # анхааруулгын цонхыг хаана (формыг биш)
        await ui.clear_toasts(env)
        acted = False
        for label in ui.validation_targets(message, titles):
            if label in handled:
                continue
            handled.add(label)
            try:
                info = await ui.reveal_field(env, label)   # формын өөр таб дээр байж болно
            except StepFailed:
                continue                                # талбар биш (мөрийн grid г.м.)
            if info["readonly"] and not info.get("combo"):
                continue
            if info.get("date"):
                value = today_str(await ui.scan_form(env, wait_s=0))
                await ui.fill_field(env, label, value)
                shown = value
            elif info["tag"] == "select" or info.get("combo") or info.get("lookup"):
                value = await ui.pick_option(env, label)
                shown = f"TODO сонголт (автомат: {value[:60]})"
            else:
                value = ("1" if _match(label, words["number_labels"]) or re.fullmatch(r"0+([.,]0+)?", (info["value"] or "").strip())
                         else tag)
                await ui.fill_field(env, label, value)
                shown = value.replace(tag, "{run_tag}")
            if value:
                env.run.meta.setdefault("fields", {})[label] = shown
                env.note(f"Анхааруулгын дагуу бөглөв: {label}={value[:40]}")
                acted = True
        if not acted and not tried_lines:
            tried_lines = True
            res = await ui.fill_lines(env, "1")
            if res is not None:
                env.run.meta["lines"] = res["lines"]
                if res["error"]:
                    raise StepBlocked(f"Баримтын мөр бөглөж чадсангүй: {res['error']} ({last})", skip_flow=True)
                env.note("Мөр нэмсэн: " + " | ".join(res["lines"]))
                acted = True
        if not acted:
            raise last
    raise last


async def _close_form_tabs(env, tabs_before: list[str]) -> None:
    """Хадгалсны дараа нээлттэй үлдсэн форм табыг хаана (гарчиг нь баримтын дугаараар өөрчлөгдсөн ч)."""
    extra = [t for t in await ui.tab_names(env) if t not in tabs_before]
    if extra:
        await ui.close_tabs(env, extra)


@step("crud.auto_update")
async def auto_update(env, args: dict) -> dict:
    """with: {find: "{a01_rec}", field: "{a01_field}", save_as: a01_rec} — Засах товчгүй бол алгасна."""
    tag = env.ctx.run_tag
    find, field = str(args["find"]), str(args["field"])
    edit = ui.buttons(env, "edit")
    await ui.ensure_menu(env, args.get("menu"))
    if not await ui.has_button(env, edit):
        env.note("Засах товч алга — засварыг алгасав")
        return {}
    tabs_before = await ui.tab_names(env)
    sel = await ui.select_row(env, find)
    try:
        if sel.errors:
            # Бичлэгийг засахаар нээхэд системийн алдаа (ж: Invalid column name 'CostCenterId') — аппын bug
            raise StepFailed(f"Бичлэгийг засахаар нээхэд: {sel.errors[0].text}", sel.errors)
        if not sel.opened and not sel.new_tabs:    # мөр дарахад нээгдээгүй бол «Засах»
            if sel.focus == "mismatch":
                # «Засах» фокуслагдсан мөрийг засдаг — фокус манай мөр биш бол өөр бичлэг засагдана
                raise StepBlocked("Фокуслагдсан мөр манай тестийн бичлэг биш — өөр бичлэгийг засахаас сэргийлж зогсоов")
            await ui.click_button(env, edit, scope="page")
        info = await ui.find_field(env, field, wait_s=15)
        if not await ui.form_has_text(env, find):
            # Форм ачаалагдсаны ДАРАА системийн алдааны цонх гарч болно (ж: Данс — Invalid column name
            # 'CostCenterId') — дээд давхарга нь алдааны цонх болж формыг "хоосон" мэт харуулна → аппын bug
            errors, _ = await ui.popup_problems(env)
            if errors:
                if env.current is not None:
                    env.current.screenshot = await env.screenshot("update-error")
                raise StepFailed(f"Бичлэгийг засахаар нээхэд: {errors[0].text}", errors)
            # Нээгдсэн форм манай бичлэгийн утгыг агуулахгүй → өөр бичлэг — ХАДГАЛАХГҮЙ хаана (оношлогоо хаахаас өмнө)
            vals = await ui.form_values(env)
            if env.current is not None:
                env.current.screenshot = await env.screenshot("update-form")
            raise StepBlocked(f"Засварын форм «{find}» бичлэгийг агуулаагүй (өөр бичлэг нээгдсэн) — хадгалаагүй. "
                              f"Формын утгууд: {vals}")
        if info["readonly"]:
            await ui.close_form(env)
            await _close_form_tabs(env, tabs_before)
            env.note(f"'{field}' талбар засварт readonly — засварыг алгасав")
            return {}
        uid = _uid(args)
        new = f"{tag} {uid} автомат засвар" if uid else f"{tag} автомат засвар"
        await ui.fill_field(env, field, new)
        await ui.save_form(env, ui.buttons(env, "save"))
    except StepFailed:
        await ui.close_form(env)
        await _close_form_tabs(env, tabs_before)
        raise
    await _close_form_tabs(env, tabs_before)
    env.run.meta["edit_value"] = new.replace(tag, "{run_tag}")
    env.note(f"Засав: {field} → {new}")
    return {args["save_as"]: new}
