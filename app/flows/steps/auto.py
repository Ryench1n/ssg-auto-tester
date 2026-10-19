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
    "number_labels": ["тоо", "дүн", "үнэ", "хувь", "хэмжээ", "хоног", "дараалал", "amount", "qty"],
    # DevExpress DateEdit нь ▾ товчтой тул DOM-оор (slDate г.м. класс таарахгүй үед) combo мэт харагддаг —
    # шошгоор нь огноо гэж таниад сонгохын оронд огноо бичнэ (HR «Төрсөн огноо»)
    "date_labels": ["огноо", "date"],
    "birth_labels": ["төрсөн", "birth"],          # төрсөн огноо — өнөөдөр биш (насны шалгалт)
    # Validation мессеж эдгээрийг агуулбал форм тусгай үйлдэл шаарддаг (И-баримт «Мэдээлэл татна уу!»,
    # Давтамжит гүйлгээ «Хуваарь оруулна!») — автоматаар хийхгүй, ангилж мэдээлнэ
    "workflow_texts": ["татна уу", "татах", "хуваарь"],
}


def in_columns(label: str, columns: list[str] | None) -> bool:
    """Талбар жагсаалтад баганатай эсэх: яг ижил, эсвэл багана нь шошгын нэг үг («Гүйлгээний утга» ↔ «Утга»),
    эсвэл шошго нь баганын нэг үг. Жагсаалтын хайлт зөвхөн харагдах баганаар хайдаг."""
    lab = re.sub(r"\s+", " ", label or "").strip().lower()
    words = set(lab.split())
    for c in columns or []:
        col = re.sub(r"\s+", " ", c or "").strip().lower()
        if col and (col == lab or (len(col) >= 3 and col in words) or (len(lab) >= 3 and lab in col.split())):
            return True
    return False


def validation_kind(message: str, auto_cfg: dict | None = None) -> str:
    """Систем хүлээж аваагүй шалтгааны ангилал: "workflow" (тусгай үйлдэл) | "validation" (утга таарсангүй)."""
    words = (auto_cfg or {}).get("workflow_texts") or DEFAULT_LABELS["workflow_texts"]
    return "workflow" if _match(message, words) else "validation"


def _match(label: str, words: list[str]) -> bool:
    low = label.lower()
    return any(w in low for w in words)


def _uid(args: dict) -> str:
    """save_as "a06_rec" → "A06" (урсгалын код) — алхам бүрийн утгыг урсгалаар давтагдашгүй болгоно."""
    m = re.match(r"^([a-z]\d+)_", str(args.get("save_as") or ""))
    return m.group(1).upper() if m else ""


def is_date_field(f: dict, words: dict | None = None) -> bool:
    """DOM-оор огноо (type=date, slDate …), эсвэл шошго нь «огноо» бөгөөд lookup/select биш (DateEdit-ийг combo
    гэж андуурахаас сэргийлнэ)."""
    if f.get("date"):
        return True
    if f.get("lookup") or f.get("tag") == "select" or f.get("type") in ("checkbox", "radio", "number"):
        return False
    return _match(f.get("label") or "", (words or DEFAULT_LABELS).get("date_labels") or DEFAULT_LABELS["date_labels"])


def date_value(label: str, fields: list[dict], words: dict | None = None) -> str:
    """Огнооны талбарын утга: төрсөн огноо бол 30 жилийн өмнө, бусад нь өнөөдөр (формын огнооны форматаар)."""
    w = (words or DEFAULT_LABELS).get("birth_labels") or DEFAULT_LABELS["birth_labels"]
    if _match(label, w):
        t = datetime.date.today()
        return today_str(fields, t.replace(year=t.year - 30, day=min(t.day, 28)))
    return today_str(fields)


def today_str(fields: list[dict], d: datetime.date | None = None) -> str:
    """Өнөөдрийн (эсвэл d) огноо — формд бөглөгдсөн өөр огноо (ж: «Баримтын огноо 2026-09-30» эсвэл 30.09.2026)
    байвал түүний форматаар, үгүй бол YYYY-MM-DD."""
    d = d or datetime.date.today()
    for f in fields:
        v = (f.get("value") or "").strip()
        m = re.fullmatch(r"\d{4}([-./])\d{2}\1\d{2}", v)
        if m:
            return d.strftime(f"%Y{m.group(1)}%m{m.group(1)}%d")
        m = re.fullmatch(r"\d{2}([-./])\d{2}\1\d{4}", v)
        if m:
            return d.strftime(f"%d{m.group(1)}%m{m.group(1)}%Y")
    return d.isoformat()


def plan_values(fields: list[dict], tag: str, auto_cfg: dict, uid: str = "",
                columns: list[str] | None = None) -> tuple[dict[str, str], str, list[str], list[str]]:
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
        return not is_date_field(f, words) and (f["tag"] == "select" or bool(f.get("combo")) or bool(f.get("lookup")))

    def is_text(f: dict) -> bool:
        return f["tag"] in ("input", "textarea") and f["type"] in TEXT_TYPES and not is_choice(f)

    def is_empty(f: dict) -> bool:
        v = (f["value"] or "").strip()
        # Заавал тоон талбарын анхдагч 0 (ж: «Дүн * 0») — хоосон гэж үзнэ (мөрийн дүнтэй тэнцүү 1 болно)
        return not v or (f["required"] and re.fullmatch(r"0+([.,]0+)?", v) is not None)

    # DevExpress ComboBox (гараар бичих боломжгүй) input нь readonly — гэхдээ жагсаалтаас сонгож болно
    empty = [f for f in fields if (not f["readonly"] or f.get("combo")) and is_empty(f)]
    texts = [f for f in empty if is_text(f) and not is_date_field(f, words)]
    # Жагсаалтын хайлт зөвхөн харагдах багана дээр ажилладаг — түлхүүр нь жагсаалтад баганатай талбар байх ёстой
    # (Accounting «Бараа материалын хүлээн авалтын акт»: «Гүйлгээний утга» баганагүй → хайлтаар олдоогүй)
    # тоо/дүн (ж: журналын «Дүн» нь баганатай ч мөрийг давтагдашгүй тодорхойлохгүй) түлхүүр болохгүй
    shown = [f for f in texts if in_columns(f["label"], columns) and not _match(f["label"], num_w)]
    # Түлхүүр: (жагсаалтад харагдах) заавал нэр/утга → нэр/утга → заавал текст → код → эхний текст
    key_f = (next((f for f in shown if f["required"] and _match(f["label"], key_w)), None)
             or next((f for f in shown if _match(f["label"], key_w)), None)
             or next((f for f in shown if f["required"]), None)
             or (shown[0] if shown else None)
             or next((f for f in texts if f["required"] and _match(f["label"], key_w)), None)
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
        elif is_date_field(f, words):
            values[label] = date_value(label, fields, words)   # формын бусад огнооны форматаар
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


def want_matches(want: str, text: str) -> bool:
    """prefer-ийн утга (энгийн, «=X», «a&!b» нөхцөл) одоогийн утгатай таарах эсэх."""
    w, t = want.lstrip("=").strip().lower(), (text or "").lower()
    if "&" in w or w.startswith("!"):
        toks = [x.strip() for x in w.split("&") if x.strip()]
        return all(x in t for x in toks if not x.startswith("!")) and not any(x[1:] in t for x in toks if x.startswith("!"))
    return w in t


def _field_for(fields: list[dict], label: str) -> dict | None:
    n = re.sub(r"\s+", " ", label.replace("*", "")).strip().lower()
    norm = lambda f: re.sub(r"\s+", " ", (f.get("label") or "").replace("*", "")).strip().lower()
    return next((f for f in fields if norm(f) == n), None) or next((f for f in fields if n and n in norm(f)), None)


def apply_explicit(fields: list[dict], values: dict, picks: list[str], unresolved: list[str], key: str,
                   explicit: dict, prefer: dict) -> tuple[dict, list[str], list[str], list[str]]:
    """Сценарийн заасан утгууд (values) ба сонголтууд (prefer) — заавал БИШ талбарт ч хэрэглэнэ (ж: борлуулалтын
    «Харилцагч» = тест харилцагч, барааны «Худалдах үнэ»). Түлхүүр (run_tag) талбарыг дарж бичихгүй.
    Буцаах: (values, picks — формын дарааллаар, unresolved, формд олдоогүй шошгууд)."""
    values, unresolved, skipped = dict(values), list(unresolved), []
    for label, value in explicit.items():
        f = _field_for(fields, str(label))
        if f is None:
            skipped.append(str(label))
            continue
        if f["label"] == key:
            continue
        v = str(value)
        values[f["label"]] = (v.lower() == "true") if v.lower() in ("true", "false") else v
        if f["label"] in unresolved:
            unresolved.remove(f["label"])
        if f["label"] in picks:
            picks = [p for p in picks if p != f["label"]]
    want_pick = set(picks)
    for label in prefer:
        f = _field_for(fields, str(label))
        if f is None:
            skipped.append(str(label))
        elif f["label"] not in values and (f["tag"] == "select" or f.get("combo") or f.get("lookup")):
            want_pick.add(f["label"])
    order = [f["label"] for f in fields if f["label"] in want_pick]
    return values, list(dict.fromkeys(order + [p for p in picks if p not in order])), unresolved, skipped


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


# «Бүлэг сонгоогүй байна!» / «Харьяалагдах бүлэг сонгоно уу» — формд тийм талбаргүй: бүлгийг жагсаалтын (зүүн)
# бүлгийн модноос сонгосны дараа «Шинэ» дардаг (testaccounting: ~11 бүлгийн лавлах, 2026-10-19)
GROUP_MISSING = re.compile(r"(бүлэг|групп)\w*\s+(сонгоогүй|сонгогдоогүй|сонгоно уу|хоосон)", re.I)


@step("crud.auto_create")
async def auto_create(env, args: dict) -> dict:
    try:
        return await _auto_create(env, args)
    except StepBlocked as e:
        if args.get("_group") or not GROUP_MISSING.search(str(e)):
            raise
        group = await ui.select_side_group(env, nonroot=True)
        if not group:
            raise
        env.note(f"«{GROUP_MISSING.search(str(e)).group(0)}» — бүлгийн модноос «{group}» сонгоод дахин бүртгэв")
        env.run.meta["group"] = group
        return await _auto_create(env, {**args, "_group": group})


async def _auto_create(env, args: dict) -> dict:
    """with: {menu: <цэсний түлхүүр/зам>, save_as: a01_rec, field_as: a01_field,
              try_as: a01_try,      # хадгалахын ӨМНӨ ctx-д бичнэ — Fail болсон ч цэвэрлэгээ хайж устгана
              values_as: a01_vals,  # бөглөсөн утгууд — засварын дараа дахин нээж шалгахад (crud.auto_verify)
              prefer: {Ажилтан: "<мастер бичлэгийн утга>"},   # lookup-д энэ бичлэгийг сонгоно (заавал биш
                                                              # талбарт ч — ж: борлуулалтын «Харилцагч»)
              values: {"Худалдах үнэ": "1000"},   # тодорхой утга (заавал биш талбарт ч); формд алга бол алгасна
              add_menu: "Кассын орлогын ордер",   # «Шинэ» нь төрөл сонгох цэстэй бол энэ зүйлийг сонгоно
              lines: {debit: "1001", credit: "5101", amount: "1"}}  # мөрийн grid: данс бүрийг заах (E2E Δ)
              # эсвэл барааны мөр: {item: "{run_tag}12", cells: {"Тоо хэмжээ": "1", "Нэгж үнэ": "1000"}}"""
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
            # «Шинэ» → баримтын төрөл сонгох цэс. Сценари заасан бол (add_menu) тэрийг, үгүй бол аюулгүй эхний төрлийг
            # (устгах/батлах … хориотой — safe_type_items; «A B» гэсэн нийлмэл эх зүйлийг алгасна) сонгоно —
            # өмнө нь энд зогсож автомат CRUD-ийн 21 цэс Blocked болдог байсан (testaccounting, 2026-10-19)
            want = str(args.get("add_menu") or "")
            all_items = menu_items
            if want:
                item = next((m for m in menu_items if m.strip() == want), None) or \
                    next((m for m in menu_items if want.lower() in m.lower()), None)
                safe, _ = ui.safe_type_items(env, [item] if item else [])
            else:
                safe, _ = ui.safe_type_items(env, menu_items)
                safe = [m for m in safe if not any(o != m and o.strip() and o.strip() in m for o in safe)]
            if not safe or not await ui.click_popup_item(env, safe[0]):
                raise StepBlocked(f"«{' / '.join(add)}» нь төрөл сонгох цэстэй ({', '.join(all_items[:8])}) — "
                                  + (f"«{want}» алга" if want else "аюулгүй сонгох төрөл алга; YAML flow бичнэ үү"),
                                  skip_flow=True, category="tool" if want else "")
            menu_items = []
            env.run.meta["type_item"] = safe[0]
            if not want:
                env.note(f"«{' / '.join(add)}» → төрөл «{safe[0]}» сонгов (сонголтууд: {', '.join(all_items[:6])})")
            opened, new_tabs, _ = await wait_opened(env, before, tabs_before, menus=False)
            env.run.meta["opened"] = opened or "nothing"
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
                                                     _uid(args), args.get("columns"))
        values, picks, unresolved, skipped = apply_explicit(fields, values, picks, unresolved, key,
                                                            args.get("values") or {}, args.get("prefer") or {})
        if skipped:
            env.note("Эхний табад алга (бусад табаас хайна): " + ", ".join(skipped))
        meta_fields = {l: str(v).replace(tag, "{run_tag}") for l, v in values.items()}
        prefer_by = {(_field_for(fields, str(k)) or {}).get("label", k): v for k, v in (args.get("prefer") or {}).items()}
        env.run.meta.update(fields={**meta_fields, **{l: "TODO" for l in picks + unresolved}}, key=key)
        if unresolved:
            raise StepBlocked(f"Заавал талбарыг автоматаар бөглөх боломжгүй: {', '.join(unresolved)} "
                              "— энэ цэсэд YAML flow бичнэ үү (ноорог тайланд)", skip_flow=True)
        if not key:
            raise StepBlocked("run_tag бичих текст талбар олдсонгүй", skip_flow=True)
        if args.get("columns") and not in_columns(key, args["columns"]):
            raise StepBlocked(f"Түлхүүр талбар «{key}» жагсаалтын баганад алга ({', '.join((args.get('columns') or [])[:8])}) — "
                              "бичлэгийг хайж олох, цэвэрлэх боломжгүй тул үүсгэсэнгүй; YAML flow бичнэ үү",
                              skip_flow=True, category="tool")
        form_level = await ui.dialog_count(env)
        words = {**DEFAULT_LABELS, **{k: v for k, v in (ui.fcfg(env).get("auto") or {}).items() if v}}
        dates = {f["label"] for f in fields if is_date_field(f, words)}
        for label, value in values.items():
            await ui.fill_field(env, label, value, as_date=label in dates)
            if await ui.dialog_count(env) > form_level:
                # Текст гэж таньсан талбар бичихэд сонголтын (lookup) цонх нээлээ — хаагаад зогсоно
                await ui.close_form(env, keep=form_level)
                raise StepBlocked(f"'{label}' талбарт бичихэд сонголтын цонх нээгдлээ (lookup гэж танигдаагүй) "
                                  "— YAML flow бичнэ үү", skip_flow=True)
        picked: dict[str, str] = {}
        for label in picks:                    # формын дарааллаар (хамааралтай lookup)
            info = await ui.find_field(env, label)
            current = (info.get("value") or "").strip()
            mine = str(prefer_by.get(label) or "")
            # Анхдагч утгатай талбар: сценари өөр утга заасан бол солино (ж: барааны «Төрөл» анхдагч «Бараа материал»)
            if current and (not mine or want_matches(mine, current)):
                # Өмнөх сонголтоор систем автоматаар бөглөсөн (ж: Дансны төрөл → Дансны бүлэг) — хөндөхгүй
                picked[label] = current
                env.run.meta["fields"][label] = f"(автоматаар бөглөгдсөн: {current[:60]})"
                continue
            val = await ui.pick_option(env, label, want=str(prefer_by.get(label) or ""))
            if await ui.dialog_count(env) > form_level:
                await ui.close_form(env, keep=form_level)
            if not val and tag in mine:
                raise StepBlocked(f"'{label}' сонголтод тестийн «{mine}» бичлэг олдсонгүй — бодит бичлэгээр "
                                  "орлуулаагүй (өмнөх алхам үүсгэсэн эсэхийг шалгана уу)", skip_flow=True, category="tool")
            if not val and mine and not current:
                raise StepBlocked(f"'{label}' сонголтод сценарийн заасан «{mine}» олдсонгүй — lookup-д байгаа мөрүүдийг "
                                  "тэмдэглэлээс харж сценарийг тааруулна уу", skip_flow=True, category="tool")
            if not val and current:
                # Анхдагчийг солих гэсэн ч хүссэн сонголт алга — анхдагч хэвээр (сонголтуудыг тэмдэглэлээс харна)
                picked[label] = current
                env.note(f"'{label}': «{mine}» сонгогдсонгүй — анхдагч «{current[:40]}» хэвээр")
                continue
            if not val and info.get("readonly"):
                continue        # идэвхгүй (disabled) сонголт — хадгалах үеийн анхааруулга шийднэ
            if not val:
                # Ихэвчлэн тест орчинд лавлах (мастер) өгөгдөл алга — AutoCrudStage тохирох мастер цэсээр
                # бичлэг үүсгээд энэ цэсийг дахин оролдоно
                env.run.meta["needs"] = label
                raise StepBlocked(f"'{label}' сонголтын талбарт сонгох утга олдсонгүй (тест орчинд «{label}» "
                                  "өгөгдөл алга эсвэл сонголт тогтсонгүй) — «" + label + "» мастер цэсийг автомат "
                                  "CRUD-д сонговол түрүүлж үүсгэнэ; эс бөгөөс YAML flow бичнэ үү",
                                  skip_flow=True, category="nodata")
            picked[label] = val
            env.run.meta["fields"][label] = f"TODO сонголт (автомат: {val[:60]})"
        if skipped:
            # Эхний табад харагдаагүй заасан талбарууд — формын өөр таб дээр (ж: борлуулалтын «Бусад» → «НӨАТ-ын данс»)
            await _fill_other_tabs(env, form_level, skipped, args, values, picked)
        env.note("Бөглөсөн: " + ", ".join(f"{l}={v}" for l, v in values.items())
                 + (" | Сонгосон: " + ", ".join(f"{l}={v}" for l, v in picked.items()) if picked else ""))
        if args.get("try_as"):
            # Хадгалах оролдлого — «Хадгалах боломжгүй» алдаа гарсан ч бичлэг үүссэн байж болно (цэвэрлэгээ)
            env.ctx.data[args["try_as"]] = values[key]
        keep = {(_field_for(fields, str(k)) or {}).get("label", k) for k in [*(args.get("prefer") or {}), *(args.get("values") or {})]}
        await _save_with_lines(env, form_level, values, picked, line_spec=args.get("lines") or {}, keep=keep)
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
            kind = validation_kind(str(e), ui.fcfg(env).get("auto") or {})
            hint = (" — форм тусгай үйлдэл (мэдээлэл татах / хуваарь г.м.) шаарддаг тул YAML flow бичнэ үү"
                    if kind == "workflow" else "")
            raise StepBlocked(f"Автоматаар бөглөсөн утгыг систем хүлээж авсангүй: {e}{hint}", e.findings,
                              skip_flow=True, category=kind) from None
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
    out = {args["save_as"]: values[key], args["field_as"]: key}
    if args.get("values_as"):
        out[args["values_as"]] = {"values": dict(values), "picks": dict(picked), "dates": sorted(dates & set(values))}
    return out


async def _save_with_lines(env, form_level: int, values: dict | None = None, picked: dict | None = None,
                           max_rounds: int = 8, line_spec: dict | None = None, keep: set | None = None) -> None:
    """Хадгална. Систем validation анхааруулга буцаавал (аппын алдаа биш) мессежийг уншиж засаад дахин:

      1. «<Талбар> оруулна уу / сонгоно уу» → формд тийм талбар байвал бөглөнө/сонгоно
         (тоон шошго эсвэл 0 утгатай бол 1, сонголтын талбар бол эхний утга, бусад нь run_tag)
      2. талбар заагаагүй ерөнхий мессеж (ж: «Хадгалах мэдээлэл дутуу байна») → заавал БИШ хоосон талбаруудыг
         бөглөнө (текст: run_tag, тоо: 1, огноо, сонголт: эхний утга) — нэг удаа
      3. талбар биш бол (ж: «Данс оруулна уу», «Барааны мэдээлэл оруулна уу») → мөрийн grid-д мөр нэмнэ
    values / picked: бөглөсөн утгууд (энд нэмж бөглөснийг ч бичнэ — засварын дараах шалгалтад)
    Алдаа (SQL, 5xx, «Системийн алдаа») гарвал шууд дамжуулна → Fail/bug."""
    save = ui.buttons(env, "save")
    titles = ui.fcfg(env).get("validation_titles") or []
    words = {**DEFAULT_LABELS, **{k: v for k, v in (ui.fcfg(env).get("auto") or {}).items() if v}}
    tag = env.ctx.run_tag
    handled: set[str] = set()
    filled_targets: list[str] = []
    tried_lines = tried_optional = False
    line_spec = line_spec or {}
    values = values if values is not None else {}
    picked = picked if picked is not None else {}
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
        env.note("Хадгалахад анхааруулга: " + " | ".join(x.strip() for x in message.splitlines() if x.strip())[:200])
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
            cur = (info.get("value") or "").strip()
            if label in (keep or set()) and cur and not re.fullmatch(r"0+([.,]0+)?", cur):
                # Сценарийн заасан талбар бөглөгдсөн (ж: авлагын «Данс») — анхааруулга өөр (мөр/таб)
                # талбарын тухай; ДАРЖ БИЧИХГҮЙ (testaccounting борлуулалт: 120101-ийг 00099901-ээр сольж байсан)
                filled_targets.append(label)
                continue
            if is_date_field({**info, "label": label}, words):
                value = date_value(label, await ui.scan_form(env, wait_s=0), words)
                await ui.fill_field(env, label, value, as_date=True)
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
                (picked if shown.startswith("TODO") else values)[label] = value
                acted = True
        # Мөр бөглөсний дараах анхааруулга мөрийн тухай (ж: «Уг дансны төрлөөр кредит бичилт…») — заавал биш толгойн
        # талбар (НӨАТ, мөнгөн гүйлгээ …) бөглөвөл шинэ алдаа үүсгэнэ (testaccounting журнал, 2026-10-19)
        # Сценари мөрийг заасан (lines) бол «мэдээлэл дутуу» нь эхлээд мөрийн тухай — заавал биш талбар (Борлуулагч,
        # Гэрээ …) таамаглаж бөглөхгүй
        if not acted and not tried_optional and not tried_lines and not line_spec \
                and not ui.validation_targets(message, titles):
            tried_optional = True
            acted = await _fill_optional(env, form_level, words, values, picked)
        if not acted and not tried_lines:
            tried_lines = True
            res = await ui.fill_lines(env, str(line_spec.get("amount") or "1"),
                                      [line_spec[k] for k in ("debit", "credit", "item") if line_spec.get(k)] or None,
                                      cells=line_spec.get("cells") or None)
            if res is not None:
                env.run.meta["lines"] = res["lines"]
                if res["error"]:
                    raise StepBlocked(f"Баримтын мөр бөглөж чадсангүй: {res['error']} ({last})", skip_flow=True)
                env.note("Мөр нэмсэн: " + " | ".join(res["lines"]))
                acted = True
        if not acted:
            await _survey_before_giving_up(env, filled_targets)
            raise last
    await _survey_before_giving_up(env, filled_targets)
    raise last


async def _fill_other_tabs(env, form_level: int, labels: list[str], args: dict, values: dict, picked: dict) -> None:
    """Сценарийн заасан (prefer / values) боловч эхний табад алга талбаруудыг бусад табаас хайж бөглөнө.
    Олдохгүй бол тэмдэглээд үргэлжилнэ (формд огт байхгүй байж болно). Дараа нь эхний табыг сэргээнэ."""
    prefer, explicit = args.get("prefer") or {}, args.get("values") or {}
    moved = False
    for label in labels:
        try:
            info = await ui.reveal_field(env, label)
        except StepFailed:
            env.note(f"'{label}' аль ч табад алга — алгасав")
            continue
        moved = True
        if label in prefer and (info["tag"] == "select" or info.get("combo") or info.get("lookup")):
            cur = (info.get("value") or "").strip()
            if cur and want_matches(str(prefer[label]), cur):
                picked[label] = cur
                continue
            val = await ui.pick_option(env, label, want=str(prefer[label]))
            if await ui.dialog_count(env) > form_level:
                await ui.close_form(env, keep=form_level)
            if val:
                picked[label] = val
            else:
                env.note(f"'{label}': «{prefer[label]}» сонгож чадсангүй")
        elif label in explicit:
            v = str(explicit[label])
            value = (v.lower() == "true") if v.lower() in ("true", "false") else v
            await ui.fill_field(env, label, value)
            values[label] = value
    if moved:
        await ui.form_tabs(env, 0)
        await asyncio.sleep(0.3)


async def _survey_before_giving_up(env, filled_targets: list[str]) -> None:
    """«<Талбар> сонгогдоогүй» гэсэн ч тэр талбар бөглөгдсөн — аль табын юу дутуу байгааг тайланд (калибровк)."""
    if not filled_targets:
        return
    try:
        flow = re.sub(r"[^\w-]+", "_", getattr(env, "flow_name", ""), flags=re.U).strip("_")[:30]
        tabs = await ui.survey_tabs(env, shot_prefix=f"survey-{flow}")
        env.note(f"«{', '.join(dict.fromkeys(filled_targets))}» бөглөгдсөн байхад анхааруулга давтагдав — табууд: "
                 + " | ".join(tabs))
    except Exception as e:                                   # оношлогоо — үндсэн алдааг дарахгүй
        env.note(f"Табын оношлогоо амжилтгүй: {e}")


async def _fill_optional(env, form_level: int, words: dict, values: dict, picked: dict) -> bool:
    """Ерөнхий «мэдээлэл дутуу» анхааруулгад: заавал биш хоосон талбаруудыг бөглөнө. Бөглөсөн бол True."""
    tag = env.ctx.run_tag
    filled = []
    fields = await ui.scan_form(env, wait_s=0)
    for f in fields:
        label = f["label"]
        if f["required"] or label in values or label in picked or (f.get("value") or "").strip():
            continue
        if _match(label, ["загвар", "template", "нөат", "татвар", "vat"]):
            # Загвар сонгоход форм бүхэлдээ (утга, мөрүүд) загвараар солигддог — testaccounting журнал: «Цалингийн
            # гүйлгээ» загвар 2 мөрийг 13 мөрөөр сольж, «Гүйлгээний утга»-ыг дарж бичсэн (2026-10-19)
            continue
        if f["readonly"] and not f.get("combo"):
            continue
        if f.get("type") in ("checkbox", "radio"):
            continue
        try:
            if is_date_field(f, words):
                value = date_value(label, fields, words)
                await ui.fill_field(env, label, value, as_date=True)
                values[label] = value
            elif f["tag"] == "select" or f.get("combo") or f.get("lookup"):
                value = await ui.pick_option(env, label)
                if not value:
                    continue
                picked[label] = value
            else:
                value = ("1" if f["type"] == "number" or _match(label, words["number_labels"])
                         else f"{tag.lower()}@example.com" if f["type"] == "email" or _match(label, words["email_labels"])
                         else "99000000" if f["type"] == "tel" or _match(label, words["phone_labels"])
                         else tag)
                await ui.fill_field(env, label, value)
                values[label] = value
        except StepFailed:
            continue
        if await ui.dialog_count(env) > form_level:          # текст бичихэд lookup цонх нээгдсэн — хаана
            await ui.close_form(env, keep=form_level)
        filled.append(label)
        env.run.meta.setdefault("fields", {})[label] = f"(заавал биш) {str(value).replace(tag, '{run_tag}')[:60]}"
    if filled:
        env.note("«Мэдээлэл дутуу» — заавал биш талбаруудыг бөглөв: " + ", ".join(filled))
    return bool(filled)


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
