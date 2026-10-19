"""Автомат CRUD-ийн нэмэлт шалгалтууд: засварын дараа бүх талбарыг дахин нээж шалгах, Fail болсон ч тестийн
өгөгдлийг цэвэрлэх, хоосон форм хадгалах сөрөг тест."""
from __future__ import annotations

import re

from app import models as M
from app.flows import ui
from app.flows.context import StepBlocked, StepFailed
from app.flows.engine import step
from app.flows.steps.auto import _close_form_tabs, wait_opened


# ---------------------------------------------------------------- засварын дараах бүрэн шалгалт
def _num(v: str) -> float | None:
    t = re.sub(r"\s", "", v or "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def same_value(expected: str, got: str, kind: str = "") -> bool:
    """Хадгалсан утга формд буцаж харагдах утгатай тэнцүү эсэх (огнооны формат, 1 ↔ 1.00 зөрүүг тооцохгүй)."""
    e, g = (expected or "").strip(), (got or "").strip()
    if kind == "date":
        return sorted(re.findall(r"\d+", e)) == sorted(re.findall(r"\d+", g)[:3])
    if _num(e) is not None and _num(g) is not None:
        return _num(e) == _num(g)
    return e == g or (len(e) >= 3 and e in g)


def close_enough(picked: str, got: str) -> bool:
    """Сонголтын талбар: lookup-ийн мөрийн текст (Код Нэр …) ↔ талбарт харагдах утга (зөвхөн нэр эсвэл код)."""
    p, g = (picked or "").lower(), (got or "").strip().lower()
    return bool(g) and (g in p or p in g or bool(set(re.findall(r"\w{2,}", g)) & set(re.findall(r"\w{2,}", p))))


@step("crud.auto_verify")
async def auto_verify(env, args: dict) -> dict:
    """with: {menu, find: "{a01_rec}", field: "{a01_field}", values: a01_vals}

    Жагсаалтаас олоод бичлэгийг ДАХИН НЭЭЖ, бүртгэх/засах үед бөглөсөн бүх талбар хадгалагдсаныг шалгана
    (хадгалагдсан мэт харагдсан ч зарим талбар алга болсон bug). Юу ч хадгалахгүй хаана."""
    find, field = str(args["find"]), str(args["field"])
    await ui.ensure_menu(env, args.get("menu"))
    rows = await ui.grid_find_settled(env, find)
    if not rows:
        raise StepFailed(f"'{find}' хайлтаар мөр олдсонгүй")
    snap = env.ctx.data.get(str(args.get("values") or "")) or {}
    expected = {**(snap.get("values") or {}), field: find}
    dates = set(snap.get("dates") or [])
    picks = dict(snap.get("picks") or {})
    tabs_before = await ui.tab_names(env)
    sel = await ui.select_row(env, find)
    try:
        if sel.errors:
            raise StepFailed(f"Бичлэгийг дахин нээхэд: {sel.errors[0].text}", sel.errors)
        if not sel.opened and not sel.new_tabs:
            edit = ui.buttons(env, "edit")
            if sel.focus == "mismatch" or not await ui.has_button(env, edit):
                env.note(f"Бичлэгийг дахин нээх боломжгүй — зөвхөн жагсаалтаар шалгав ({len(rows)} мөр)")
                return {}
            await ui.click_button(env, edit, scope="page")
        try:
            await ui.find_field(env, field, wait_s=15)
        except StepFailed:
            errors, _ = await ui.popup_problems(env)
            if errors:
                raise StepFailed(f"Бичлэгийг дахин нээхэд: {errors[0].text}", errors) from None
            env.note("Дахин нээсэн форм танигдсангүй — зөвхөн жагсаалтаар шалгав")
            return {}
        if not await ui.form_has_text(env, find):
            errors, _ = await ui.popup_problems(env)
            if errors:
                raise StepFailed(f"Бичлэгийг дахин нээхэд: {errors[0].text}", errors)
            raise StepBlocked(f"Дахин нээсэн форм «{find}» бичлэгийг агуулаагүй (өөр бичлэг) — шалгаж чадсангүй")
        wrong, soft, checked = [], [], 0
        for label, exp in expected.items():
            try:
                got = await ui.read_field(env, label)
            except StepFailed:
                try:
                    got = (await ui.reveal_field(env, label))["value"]
                except StepFailed:
                    soft.append(f"{label}: талбар олдсонгүй")
                    continue
            checked += 1
            if not same_value(str(exp), got, "date" if label in dates else ""):
                wrong.append(f"«{label}» хүлээгдсэн «{str(exp)[:40]}», хадгалагдсан «{(got or '')[:40]}»")
        for label, val in picks.items():
            try:
                got = await ui.read_field(env, label)
            except StepFailed:
                continue
            checked += 1
            if not close_enough(val, got):
                soft.append(f"{label}: сонгосон «{val[:30]}» ↔ талбарт «{(got or '')[:30]}»")
        if soft:
            env.note("Шалгаж чадаагүй / тодорхойгүй: " + "; ".join(soft))
        if wrong:
            if env.current is not None:
                env.current.screenshot = await env.screenshot("verify-mismatch")
            raise StepFailed("Хадгалсан утга дахин нээхэд өөр байна: " + "; ".join(wrong))
        env.note(f"Дахин нээж {checked} талбарыг шалгав — бүгд хадгалагдсан")
        return {}
    finally:
        await ui.close_form(env)                    # ХАДГАЛАХГҮЙ хаана
        await _close_form_tabs(env, tabs_before)


# ---------------------------------------------------------------- цэвэрлэгээ (Fail болсон ч)
@step("crud.auto_cleanup")
async def auto_cleanup(env, args: dict) -> dict:
    """with: {menu, data: a01} — always. Бүртгэл/засвар/устгал Fail болсон ч run_tag-тай бичлэг үлдсэн эсэхийг
    хайж, үлдсэн бол устгана. Устгаж чадахгүй бол meta.leftover-т бичиж тайланд «цэвэрлээгүй өгөгдөл» гэж гаргана."""
    from app.flows.steps.crud import crud_delete
    p = str(args["data"])
    tag = env.ctx.run_tag
    texts = list(dict.fromkeys(str(t) for t in (env.ctx.data.get(f"{p}_rec"), env.ctx.data.get(f"{p}_try"))
                               if t and tag in str(t)))
    if not texts:
        raise StepBlocked("Бүртгэл хадгалах хүртэл явагдаагүй — цэвэрлэх өгөгдөл алга (хамаарахгүй)", category="na")
    await ui.close_form(env)
    await ui.clear_toasts(env)
    await ui.ensure_menu(env, args.get("menu"))
    cleaned, left = [], []
    saved = env.ctx.data.get(f"{p}_rec")           # бүртгэх алхам Pass — бичлэг хадгалагдсан нь тодорхой
    deleted = env.ctx.data.get("_deleted") or set()
    for text in texts:
        if not await ui.grid_find_settled(env, text, tries=2):
            if saved and text not in deleted and text == saved:
                # Хадгалсан ч жагсаалтаас олдохгүй (шүүлтүүр, мөрийн түвшний жагсаалт, баганагүй талбар) —
                # «үлдээгүй» гэж худал хэлэхгүй
                left.append(f"{text.replace(tag, '{run_tag}')}: хадгалсан ч жагсаалтаас олдсонгүй — "
                            "устгагдсан эсэхийг гараар шалгана уу")
            continue
        try:
            await crud_delete(env, {"menu": args.get("menu"), "find": text})
            cleaned.append(text)
        except StepFailed as e:
            left.append(f"{text.replace(tag, '{run_tag}')}: {str(e)[:200]}")
    if cleaned:
        env.run.meta["cleaned"] = [t.replace(tag, "{run_tag}") for t in cleaned]
        failed_before = any(s.status != "Pass" and s.category not in ("na",) for s in env.run.steps[:-1])
        env.note(("Өмнөх алхам амжилтгүй болсон ч үүссэн тестийн өгөгдлийг устгав: " if failed_before
                  else "Тестийн өгөгдлийг устгав: ") + ", ".join(cleaned))
    if left:
        env.run.meta["leftover"] = left
        raise StepBlocked("Тестийн өгөгдөл үлдсэн (гараар устгана уу): " + "; ".join(left))
    if not cleaned:
        env.note("Тестийн өгөгдөл үлдээгүй")
    return {}


# ---------------------------------------------------------------- сөрөг тест: хоосон форм хадгалах
@step("crud.auto_empty_save")
async def auto_empty_save(env, args: dict) -> dict:
    """with: {menu} — always. Шинэ формыг ХООСНООР хадгалахад заавал талбарын анхааруулга гарах ёстой:
    анхааруулга / форм нээлттэй үлдсэн → Pass; системийн алдаа (SQL, 500, NullReference) → Fail (bug);
    заавал талбар хоосон байхад форм хадгалагдаж хаагдсан → Fail (validation алга).
    Заавал (*) хоосон талбаргүй форм, Нэмэх товчгүй цэс → хамаарахгүй (N/A — TC/тооцоонд орохгүй)."""
    if not (ui.fcfg(env).get("auto") or {}).get("negative_empty_save", True):
        raise StepBlocked("Сөрөг тест унтраалттай (flow.auto.negative_empty_save: false)", category="na")
    add = ui.buttons(env, "add")
    await ui.close_form(env)
    await ui.clear_toasts(env)
    await ui.ensure_menu(env, args.get("menu"))
    if not await ui.has_button(env, add):
        raise StepBlocked("Нэмэх товч алга — хамаарахгүй", category="na")
    before, tabs_before = await ui.dialog_count(env), await ui.tab_names(env)
    await ui.click_button(env, add, scope="page")
    opened, new_tabs, menu_items = await wait_opened(env, before, tabs_before)
    try:
        if opened not in ("dialog", "tab"):
            raise StepBlocked("Шинэ форм нээгдсэнгүй (төрөл сонгох цэс г.м.) — хамаарахгүй", category="na")
        if opened == "tab":
            await ui.settle(env)
        fields = await ui.scan_form(env, wait_s=15 if opened == "tab" else 10)
        required = [f["label"] for f in fields if f["required"] and not f["readonly"]
                    and not (f.get("value") or "").strip()]
        if not required:
            raise StepBlocked("Заавал (*) хоосон талбаргүй форм — сөрөг шалгалт хамаарахгүй", category="na")
        form_level = await ui.dialog_count(env)
        try:
            await ui.save_form(env, ui.buttons(env, "save"))
        except StepFailed as e:
            if e.findings and all(f.kind == M.WARNING_TOAST for f in e.findings):
                env.note(f"Хоосон хадгалахад анхааруулга гарав: {str(e)[:200]}")
                return {}
            if not e.findings and "хаагдсангүй" in str(e):
                env.note("Хоосон хадгалахад форм нээлттэй үлдэв (талбарын validation)")
                return {}
            raise StepFailed(f"Хоосон форм хадгалахад: {e}", e.findings) from None
        now_tabs = await ui.tab_names(env)
        saved = (opened == "dialog" and await ui.dialog_count(env) < form_level) or \
                (opened == "tab" and any(t not in now_tabs for t in new_tabs))
        if saved:
            raise StepFailed(f"Заавал талбар ({', '.join(required[:5])}) хоосон байхад форм хадгалагдаж хаагдлаа — "
                             "validation алга; хоосон (run_tag-гүй) бичлэг үүссэн байж болзошгүй тул гараар шалгана уу")
        env.note("Хоосон хадгалахад форм нээлттэй үлдэв")
        return {}
    finally:
        await ui.close_form(env, keep=before)       # ХАДГАЛАХГҮЙ хаана
        if menu_items:
            try:
                await env.page.keyboard.press("Escape")
            except Exception:
                pass
        await _close_form_tabs(env, tabs_before)
