"""Форм шалгалт (READ-ONLY): цэс бүрийн формыг нээж, алдаа гарах эсэхийг шалгана — ЮУ Ч ХАДГАЛАХГҮЙ.

  form.check_new  — «Нэмэх/Шинэ» → форм (цонх / шинэ таб; dropdown бол баримтын төрөл бүр) нээгдэхэд алдаа,
                    формын lookup ("...") бүрийг нээж ачаалагдахыг хүлээгээд СОНГОЛГҮЙ хаана → форм хадгалалгүй хаана
  form.check_edit — жагсаалтын эхний мөрийг засварын горимд нээгээд (мөр дарах эсвэл «Засах») алдааг шалгаж,
                    хадгалалгүй хаана

Бодит SmartApp-д олдсон bug-уудын ихэнх (Invalid column name 'ActiveStatus' / 'DistrictCode' / 'CostCenterId')
хадгалах биш НЭЭХ үед гардаг тул өгөгдөл үүсгэлгүйгээр бүх цэсийг хурдан хамарна.
Алдаа нэг бүрийг цуглуулж (эхний алдаагаар зогсохгүй) алхмын төгсгөлд Fail болгоно.
"""
from __future__ import annotations

import asyncio
import time

from app import models as M
from app.flows import ui
from app.flows.context import StepBlocked, StepFailed
from app.flows.engine import step
from app.flows.steps.auto import wait_opened

MAX_TYPES = 5        # «Шинэ» dropdown-ы баримтын төрлөөс шалгах дээд тоо
MAX_LOOKUPS = 15     # нэг формд нээх lookup-ийн дээд тоо


class _Timer:
    """Алхмын үе шат бүрийн хугацаа — удаашралыг таамаглалгүй хэмжиж тайлан/run.log-д бичнэ («⏱ …»)."""

    def __init__(self, env):
        self.env, self.t, self.parts = env, time.monotonic(), []

    def lap(self, name: str) -> None:
        now = time.monotonic()
        self.parts.append((name, now - self.t))
        self.t = now

    def done(self) -> None:
        if self.parts:
            self.env.note("⏱ " + " · ".join(f"{n} {s:.1f}с" for n, s in self.parts if s >= 0.05))


async def _close_all(env, before: int, tabs_before: list[str]) -> None:
    """Шалгалтын нээсэн бүх цонх/табыг ХАДГАЛАЛГҮЙ хаана (Болих/Үгүй/✕)."""
    try:
        await ui.close_form(env, keep=before)
        extra = [t for t in await ui.tab_names(env) if t not in tabs_before]
        if extra:
            await ui.close_tabs(env, extra)
        if await ui.popup_menu_items(env):
            await env.page.keyboard.press("Escape")
        await ui.clear_toasts(env)
    except Exception:
        pass


async def _check_form(env, where: str, level: int, problems: list[str], findings: list,
                      tm: "_Timer | None" = None, mode: str = "new", t_open: float | None = None) -> None:
    """Нээгдсэн формыг шалгана. level — форм нээлттэй үеийн цонхны тоо (түүнээс дээшхийг хаана).

    Формын ДОТООД таб бүрийг ээлжлэн нээж (Анкет / Цалин …) таб бүрийн lookup, ComboBox-ийг шалгана.
    mode="edit" — бодит бичлэгийн засварын форм: өгөгдөл ачаалагдсан эсэхийг шалгана; мөрийн grid-д мөр
    НЭМЭХГҮЙ (бодит бичлэгт өөрчлөлт хийхгүй); lookup/ComboBox-оос юу ч сонгохгүй."""
    lap = tm.lap if tm else (lambda n: None)
    fc = ui.fcfg(env)
    await ui.settle(env, 10)
    await asyncio.sleep(fc.get("form_error_grace_ms", 500) / 1000)   # settle-ийн дараах хоцорсон алдааны цонх
    errors, _ = await ui.popup_problems(env)
    if errors:
        problems.append(f"{where} нээхэд: {errors[0].text}")
        findings.extend(errors)
        return                                    # эвдэрсэн формын lookup-ийг шалгах утгагүй
    fields = await ui.scan_form(env, wait_s=5)
    if mode == "edit":
        # Засварын форм нээгдсэн ч бичлэгийн өгөгдөл ачаалагдаагүй (бүх талбар хоосон) — аппын алдаа
        deadline = time.monotonic() + fc.get("edit_data_wait_ms", 5000) / 1000
        while not any((f.get("value") or "").strip() for f in fields) and time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            fields = await ui.scan_form(env, wait_s=0)
        if len(fields) >= 2 and not any((f.get("value") or "").strip() for f in fields):
            problems.append(f"{where}: форм нээгдсэн ч бүх талбар ({len(fields)}) хоосон — бичлэгийн өгөгдөл ачаалагдсангүй")
            findings.append(M.Finding(M.SYSTEM_ERROR, f"{where}: бичлэгийн өгөгдөл форм дээр ачаалагдсангүй"))
    if t_open is not None:
        took = time.monotonic() - t_open
        if took * 1000 > fc.get("slow_form_ms", 8000):
            env.warn(f"{where} удаан нээгдэв: {took:.1f} сек (> {fc.get('slow_form_ms', 8000) / 1000:.0f} сек)")
    lap("форм ачаалал")

    tabs = await ui.form_tabs(env)
    done: set[str] = set()
    budget = int(fc.get("max_lookups", MAX_LOOKUPS))
    skipped = 0
    for ti in range(max(1, len(tabs))):
        tname = tabs[ti] if tabs else ""
        here = f"{where}{' › ' + tname if tname and len(tabs) > 1 else ''}"
        if ti > 0:                                     # дараагийн дотоод таб
            await ui.form_tabs(env, click=ti)
            await ui.settle(env, 5)
            await asyncio.sleep(0.3)
            errors, _ = await ui.popup_problems(env)
            if errors:
                problems.append(f"{here} табыг нээхэд: {errors[0].text}")
                findings.extend(errors)
                await ui.close_form(env, keep=level)
                continue
            # settle аль хэдийн ачааллыг хүлээсэн — талбаргүй таб (дэд жагсаалт/grid) дээр 2 сек хүлээхгүй
            # (HR «Ажилтан»: 19 ийм таб × 2.5с ≈ 47с)
            fields = await ui.scan_form(env, wait_s=fc.get("tab_fields_wait_ms", 600) / 1000)
            if not fields and not await ui.form_has_inputs(env):
                env.note(f"{here}: талбаргүй таб (дэд жагсаалт)")
                lap(f"таб «{tname}»")
                continue

        lookups = [f["label"] for f in fields if f.get("lookup") and not f.get("readonly") and f["label"] not in done]
        # Энэ модульд аль хэдийн алдаагүй шалгагдсан ижил нэртэй lookup-ийг алгасна (алдаатай нь кэшлэгдэхгүй)
        cache = env.ctx.__dict__.setdefault("_fc_lookup_seen", {})
        module = str((env.run.meta or {}).get("menu", "")).split(" / ")[0] if env.run else ""
        reuse = int(fc.get("lookup_repeat_per_module", 1))
        cached = [lb for lb in lookups if reuse > 0 and cache.get((module, lb), 0) >= reuse]
        if cached:
            done.update(cached)
            lookups = [lb for lb in lookups if lb not in cached]
            env.note(f"{here}: энэ модульд өмнө шалгагдсан {len(cached)} lookup алгасав ({', '.join(cached[:6])}"
                     + (" …" if len(cached) > 6 else "") + ")")
        skipped += max(0, len(lookups) - budget)
        lookups = lookups[:budget]
        budget -= len(lookups)
        ok, empty = [], []
        for label in lookups:
            done.add(label)
            try:
                await ui.find_field(env, label)
                row = await ui.pick_from_lookup(env, f"{here}: '{label}' талбарын сонголтын", pick=False)
                (ok if row else empty).append(label)
                cache[(module, label)] = cache.get((module, label), 0) + 1
            except StepFailed as e:
                if e.findings:
                    problems.append(str(e))
                    findings.extend(e.findings)
                else:
                    env.note(f"{here}: '{label}' lookup-ийг нээж чадсангүй ({str(e)[:80]})")
            except Exception as e:
                if "has been closed" in str(e) or type(e).__name__ == "TargetClosedError":
                    raise
                # ж: lookup-ийн товчийг өөр элемент хааж Page.click Timeout — хэрэгслийн хязгаарлалт, аппын bug биш
                env.note(f"{here}: '{label}' lookup-ийг нээж чадсангүй ({str(e).splitlines()[0][:100]})")
            await ui.close_form(env, keep=level)      # үлдсэн lookup/алдааны цонх
            await ui.clear_toasts(env)
        for label in empty:
            env.warn(f"«{label}» сонголтын жагсаалт хоосон ({here})")
        lap(f"lookup×{len(lookups)}")

        combos = [f["label"] for f in fields if f.get("combo") and not f.get("date") and f["tag"] != "select"
                  and f["label"] not in done][:MAX_LOOKUPS]
        c_ok, c_empty, c_fail = [], [], []
        for label in combos:
            done.add(label)
            try:
                opts = await ui.peek_combo(env, label)
            except StepFailed:
                opts = None
            errors, _ = await ui.popup_problems(env)
            if errors:
                problems.append(f"{here}: '{label}' жагсаалтыг нээхэд: {errors[0].text}")
                findings.extend(errors)
            elif opts is None:
                c_fail.append(label)
            else:
                (c_ok if opts else c_empty).append(label)
            await ui.close_form(env, keep=level)
        for label in c_empty:
            env.warn(f"«{label}» жагсаалт (ComboBox) хоосон ({here})")
        lap(f"ComboBox×{len(combos)}")

        line_txt = ""
        if mode == "new":
            # Мөрийн grid (баримтын мөр) — «+» мөр нэмж (хадгалахгүй) мөрийн lookup-уудыг нээнэ
            try:
                lines = await ui.peek_line_lookups(env, here)
            except StepFailed as e:
                if e.findings:
                    problems.append(str(e))
                    findings.extend(e.findings)
                lines = None
            except Exception as e:
                env.note(f"{here}: мөрийн grid-ийг шалгаж чадсангүй ({str(e).splitlines()[0][:80]})")
                lines = None
            if lines is not None:
                line_txt = ("; мөрийн grid"
                            + (f" — lookup ачаалагдсан: {', '.join(lines['ok'])}" if lines["ok"] else "")
                            + (f"; хоосон: {', '.join(lines['empty'])}" if lines["empty"] else "")
                            + (f" ({lines['note']})" if lines["note"] else ""))
                for col in lines["empty"]:
                    env.warn(f"Мөрийн «{col}» сонголтын жагсаалт хоосон ({here})")
            lap("мөрийн grid")
        await ui.close_form(env, keep=level)
        env.note(f"{here}: {len(fields)} талбар, lookup {len(lookups)}"
                 + (f" — ачаалагдсан: {', '.join(ok)}" if ok else "")
                 + (f"; хоосон: {', '.join(empty)}" if empty else "")
                 + (f"; ComboBox {len(combos)}" + (f" — нээгдсэн: {', '.join(c_ok)}" if c_ok else "")
                    + (f"; хоосон: {', '.join(c_empty)}" if c_empty else "")
                    + (f"; нээгдээгүй: {', '.join(c_fail)}" if c_fail else "") if combos else "")
                 + line_txt)
    if len(tabs) > 1:
        env.note(f"{where}: {len(tabs)} дотоод таб шалгав ({', '.join(tabs)})")
    if skipped:
        env.note(f"{where}: lookup-ийн хязгаар ({MAX_LOOKUPS}) — {skipped} lookup шалгаагүй")


@step("form.check_new")
async def check_new(env, args: dict) -> dict:
    """with: {menu: <цэсний түлхүүр/зам>}"""
    add = ui.buttons(env, "add")
    tm = _Timer(env)
    t_open = time.monotonic()
    await ui.ensure_menu(env, args["menu"])
    tm.lap("цэс")
    if await ui.is_form_page(env):
        # Шууд форм цэс: цэсийг нээхэд баримтын форм өөрөө таб дээр нээгддэг — «Шинэ» дарахгүй, тэр формыг шалгана
        env.note("Шууд форм цэс (жагсаалтгүй) — цэсийн формыг шалгав")
        problems: list[str] = []
        findings: list = []
        before, tabs_before = await ui.dialog_count(env), await ui.tab_names(env)
        try:
            await _check_form(env, "Цэсийн форм", before, problems, findings, tm, t_open=t_open)
        finally:
            await _close_all(env, before, tabs_before)
            tm.lap("хаах")
            tm.done()
        if findings:
            raise StepFailed("; ".join(problems)[:1500], findings)
        return {}
    if not await ui.has_button(env, add):
        env.note("Нэмэх/Шинэ товч алга — шинэ форм шалгаагүй")
        return {}
    before, tabs_before = await ui.dialog_count(env), await ui.tab_names(env)
    problems: list[str] = []
    findings: list = []
    try:
        t_open = time.monotonic()
        await ui.click_button(env, add, scope="page")
        opened, _, items = await wait_opened(env, before, tabs_before)
        if not opened and not (await ui.popup_problems(env))[0]:
            # Том форм удаан нээгдэж болно — нэг удаа нэмэлт хугацаа хүлээнэ
            opened, _, items = await wait_opened(env, before, tabs_before,
                                                 timeout_s=ui.fcfg(env).get("form_open_extra_ms", 8000) / 1000)
        if not opened and not (await ui.popup_problems(env))[0]:
            # Master-detail жагсаалт: шинэ бичлэг бүлэг сонгосны дараа л үүсдэг байж болно
            group = await ui.select_side_group(env)
            if group:
                await ui.click_button(env, add, scope="page")
                opened, _, items = await wait_opened(env, before, tabs_before, timeout_s=4)
                if opened:
                    env.note(f"Бүлгийн панелээс «{group}» сонгосны дараа «Шинэ» ажиллав")
                else:
                    env.note(f"Бүлгийн панелээс «{group}» сонгоод «Шинэ» дахин дарав — нээгдсэнгүй "
                             "(тест орчинд бүлэг бүртгэгдээгүй байж болзошгүй)")
        dd_index = -1
        tried_dd: list[str] = []
        if not opened and not (await ui.popup_problems(env))[0]:
            # «Шинэ» дангаараа юу ч хийсэнгүй — toolbar-ын dropdown (ж: «Тогтмол үйл ажиллагаа ▾»)-оор шинэ бичлэгийн
            # төрөл сонгодог цэс байж болно
            for k, label in enumerate((await ui.toolbar_dropdowns(env))[:2]):
                tried_dd.append(label)
                await ui.toolbar_dropdowns(env, mark=k)
                await ui.mark_seen(env)
                try:
                    await env.page.click("[data-ssg-tbdd='1']", timeout=3000)
                except Exception:
                    continue
                opened, _, items = await wait_opened(env, before, tabs_before, timeout_s=4)
                if opened:
                    dd_index = k
                    env.note(f"«Шинэ» дангаараа юу ч нээгээгүй — «{label} ▾» dropdown-аар шинэ бичлэг үүсгэв")
                    break
                # оношилгоо: dropdown дарсны дараа ШИНЭЭР гарсан DOM (Escape-ээс өмнө)
                env.runlog.log(f"Форм шалгалт оношилгоо «{label}»: {await ui.new_dom_hint(env)}", "warn", to_ui=False)
                await env.page.keyboard.press("Escape")

        async def reopen() -> None:                      # төрөл сонгох dropdown-ыг дахин нээх
            if dd_index >= 0:
                await ui.toolbar_dropdowns(env, mark=dd_index)
                await env.page.click("[data-ssg-tbdd='1']", timeout=3000)
            else:
                await ui.click_button(env, add, scope="page")
        tm.lap("Шинэ")
        if not opened:
            errors, _ = await ui.popup_problems(env)
            if errors:
                raise StepFailed(f"«Нэмэх» дарахад: {errors[0].text}", errors)
            if tried_dd:
                env.note("Toolbar dropdown оролдсон: " + ", ".join(f"«{x}»" for x in tried_dd) + " — юу ч нээгдсэнгүй")
            raise StepBlocked("Нэмэх/Шинэ дарахад форм (цонх эсвэл шинэ таб) нээгдсэнгүй", category="tool")
        if opened != "menu":
            await _check_form(env, "Шинэ форм", before + (opened == "dialog"), problems, findings, tm,
                              t_open=t_open)
        else:
            env.note(f"Шинэ бичлэгийн төрөл сонгох цэстэй: {', '.join(items)}")
            items, skipped_items = ui.safe_type_items(env, items)
            if skipped_items:
                env.note("Өгөгдөл өөрчилж болзошгүй тул дараагүй: " + ", ".join(f"«{x}»" for x in skipped_items))
            if not items:
                await env.page.keyboard.press("Escape")
                raise StepBlocked("Dropdown-д зөвхөн өгөгдөл өөрчилж болзошгүй зүйл байна ("
                                  + ", ".join(skipped_items) + ") — дарсангүй", category="na")
            for i, item in enumerate(items[:MAX_TYPES]):
                if i:                                 # дараагийн төрөл: dropdown-ыг дахин нээнэ
                    await _close_all(env, before, tabs_before)
                    await ui.ensure_menu(env, args["menu"])
                    await reopen()
                    if (await wait_opened(env, before, tabs_before))[0] != "menu":
                        env.note("Dropdown дахин нээгдсэнгүй — үлдсэн төрлийг шалгаагүй")
                        break
                if not await ui.click_popup_item(env, item):
                    env.note(f"«{item}» зүйлийг дарж чадсангүй")
                    continue
                t_open = time.monotonic()
                o, _, _ = await wait_opened(env, before, tabs_before, menus=False)
                if not o:
                    errors, _ = await ui.popup_problems(env)
                    if errors:
                        problems.append(f"«{item}» нээхэд: {errors[0].text}")
                        findings.extend(errors)
                    else:
                        env.note(f"«{item}»: форм нээгдсэнгүй")
                    continue
                await _check_form(env, f"«{item}» форм", before + (o == "dialog"), problems, findings, tm,
                                  t_open=t_open)
    finally:
        await _close_all(env, before, tabs_before)
        tm.lap("хаах")
        tm.done()
    if findings:
        raise StepFailed("; ".join(problems)[:1500], findings)
    return {}


async def _select_row(env, args: dict) -> str:
    """Жагсаалтын эхний бичлэгийг СОНГОНО (уншилт). Мөр дарахад засварын форм нээгддэг систем (өмнөх цэсүүдээс
    суралцсан _fc_edit_mode == "row") дээр дарахгүй — DevExpress-ийн анхдагч фокуслагдсан мөрийг ашиглана.
    Тодорхойгүй үед дараад форм нээгдвэл хаагаад горимыг тэмдэглэнэ."""
    if getattr(env.ctx, "_fc_edit_mode", "") == "row":
        return ""
    row = await ui.mark_first_row(env, wait_s=1.5)
    if not row:
        return ""
    before, tabs_before = await ui.dialog_count(env), await ui.tab_names(env)
    try:
        target = await env.page.evaluate(ui.MARK_ROW_TARGET_JS)
        await env.page.click("[data-ssg-rowsel='1']" if target else "[data-ssg-row='0']", timeout=3000)
    except Exception:
        return ""
    opened, _, _ = await wait_opened(env, before, tabs_before, menus=False, timeout_s=0.8)
    if opened:
        env.ctx._fc_edit_mode = "row"
        await _close_all(env, before, tabs_before)
        await ui.ensure_menu(env, args["menu"])
        return ""
    return row


@step("form.check_actions")
async def check_actions(env, args: dict) -> dict:
    """with: {menu} — жагсаалтын УНШИЛТЫН товчнуудыг (Хэвлэх / Экспорт / PDF харах) дарж алдаа гарах эсэхийг шалгана.
    Өгөгдөл өөрчлөх үгтэй товчийг (Нэмэх, Устгах, Импорт, Татах, Илгээх …) хэзээ ч дарахгүй. window.print-ийг
    түр орлуулна (хэвлэх цонх апп-ыг хаахгүй), файл татагдвал цуцална, шинэ хөтчийн цонх нээгдвэл хаана."""
    tm = _Timer(env)
    await ui.ensure_menu(env, args["menu"])
    tm.lap("цэс")
    labels = (await ui.action_buttons(env))[:ui.fcfg(env).get("max_actions", 3)]
    if not labels:
        raise StepBlocked("Уншилтын товч (Хэвлэх / Экспорт / PDF харах) алга — хамаарахгүй", category="na")
    selected = await _select_row(env, args) if not args.get("empty") else ""
    if selected:
        env.note(f"Хэвлэхээс өмнө бичлэг сонгов: {selected[:60]}")
    tm.lap("мөр сонгох")
    problems: list[str] = []
    findings: list = []
    results: list[str] = []
    verified = 0
    downloads: list = []

    def on_download(d) -> None:
        downloads.append(d)
        asyncio.ensure_future(d.cancel())           # хэрэглэгчийн Downloads-д файл үлдээхгүй

    env.page.on("download", on_download)
    try:
        for k, label in enumerate(labels):
            await ui.action_buttons(env, mark=k)
            before, tabs_before = await ui.dialog_count(env), await ui.tab_names(env)
            pages_before = list(env.page.context.pages)
            n_dl = len(downloads)
            try:
                await env.page.evaluate("() => { window.__ssgPrinted = 0; "
                                        "window.print = () => { window.__ssgPrinted += 1; }; }")
                await env.page.click("[data-ssg-act='1']", timeout=4000)
            except Exception as e:
                env.note(f"«{label}» товчийг дарж чадсангүй ({str(e).splitlines()[0][:80]})")
                continue
            await asyncio.sleep(0.6)
            await ui.settle(env, 8)
            errors, warnings = await ui.popup_problems(env)
            new_pages = [p for p in env.page.context.pages if p not in pages_before]
            what = []
            if await ui.dialog_count(env) > before:
                what.append("цонх")
            if [t for t in await ui.tab_names(env) if t not in tabs_before]:
                what.append("шинэ таб")
            if new_pages:
                what.append("хөтчийн шинэ цонх")
            if len(downloads) > n_dl:
                what.append("файл татах (цуцлав)")
            try:
                if await env.page.evaluate("() => window.__ssgPrinted || 0"):
                    what.append("хэвлэх цонх")
            except Exception:
                pass
            if errors:
                problems.append(f"«{label}» дарахад: {errors[0].text}")
                findings.extend(errors)
                results.append(f"«{label}» — алдаа")
            elif warnings:
                # «Мөр сонгоно уу» мэт — бичлэг сонгоогүйгээс; алдаа биш
                results.append(f"«{label}» — анхааруулга: {warnings[0][:60]}")
            else:
                results.append(f"«{label}» — " + (", ".join(what) if what else "харагдах өөрчлөлтгүй"))
                if what:
                    verified += 1
            for p in new_pages:
                try:
                    await p.close()
                except Exception:
                    pass
            await _close_all(env, before, tabs_before)
            tm.lap(label)
    finally:
        env.page.remove_listener("download", on_download)
        tm.done()
    env.note("Уншилтын товч: " + "; ".join(results))
    if findings:
        raise StepFailed("; ".join(problems)[:1500], findings)
    if not verified:
        # Дарахад юу ч гараагүй (ихэвчлэн бичлэг сонгох шаардлагатай) — алдаагүй ч ажилласан нь батлагдаагүй тул
        # Pass гэж тооцохгүй (N/A)
        raise StepBlocked("Уншилтын товч дарахад харагдах үр дүн (цонх / таб / файл / хэвлэх) гарсангүй — "
                          "бичлэг сонгох шаардлагатай байж болзошгүй; баталгаажаагүй", category="na")
    return {}


@step("form.check_edit")
async def check_edit(env, args: dict) -> dict:
    """with: {menu: <цэсний түлхүүр/зам>} — эхний мөрийг засварын горимд нээгээд хадгалалгүй хаана."""
    tm = _Timer(env)
    try:
        return await _check_edit(env, args, tm)
    finally:
        tm.done()


async def _check_edit(env, args: dict, tm: _Timer) -> dict:
    await ui.ensure_menu(env, args["menu"])
    tm.lap("цэс")
    if await ui.is_form_page(env):
        # Шууд форм цэс: засах жагсаалт байхгүй (формын мөрийн grid-ийг жагсаалт гэж андуурахгүй, формын огноог
        # шүүлтүүр гэж өөрчлөхгүй) — засварын шалгалт хамаарахгүй
        raise StepBlocked("Шууд форм цэс — засах жагсаалт байхгүй тул засварын шалгалт хамаарахгүй", category="na")
    # Smoke жагсаалтыг хоосон («Бичлэг олдсонгүй») гэж бүртгэсэн бол эхний урт хүлээлтийг алгасна
    row = await ui.mark_first_row(env, wait_s=0.5 if args.get("empty") else 5.0)
    tried: list[str] = []
    if not row and not args.get("empty"):
        # 1) Хайлтын талбарт үлдсэн текст / жагсаалт ачаалагдаагүй — хайлтыг цэвэрлээд «Сэргээх» (уншилт л).
        #    Smoke яг шинээр нээгээд хоосон гэж бүртгэсэн бол давтах утгагүй
        if await ui.clear_grid_search(env):
            tried.append("хайлтыг цэвэрлэв")
        if await ui.refresh_list(env):
            tried.append("«Сэргээх» дарав")
        row = await ui.mark_first_row(env, wait_s=4)
    if not row:
        # 1б) Master-detail: бүлгийн панелээс бүлэг («Бүх …») сонгоход үндсэн жагсаалт дүүрч болно (уншилт)
        group = await ui.select_side_group(env)
        if group:
            tried.append(f"бүлэг «{group}» сонгов")
            row = await ui.mark_first_row(env, wait_s=3)
    if not row:
        # 2) Жагсаалт ихэвчлэн энэ сар/улирлаар шүүгддэг — огнооны хүрээг сунгаж дахин үзнэ
        widened = await ui.widen_date_filter(env)
        if widened:
            tried.append(f"огноог {widened}-аас сунгав")
            row = await ui.mark_first_row(env)
    tm.lap("жагсаалт")
    if tried:
        env.note("Жагсаалт хоосон байсан — " + ", ".join(tried) + (" → бичлэг гарлаа" if row else ""))
    if not row:
        # Жагсаалт хоосон нь өгөгдөлгүйгээс биш, ачааллын алдаанаас (ж: «Мэдээлэл татах боломжгүй — Cannot create a
        # DbSet …» цонх) болсон бол N/A биш — алдаа
        errors, _ = await ui.popup_problems(env)
        if errors:
            raise StepFailed(f"Жагсаалт ачаалахад: {errors[0].text}", errors)
        raise StepBlocked("Жагсаалтад бичлэг алга" + (f" ({', '.join(tried)} ч)" if tried else "")
                          + " — тест орчинд өгөгдөл байхгүй тул засварын форм шалгаагүй", category="nodata")
    edit = ui.buttons(env, "edit")
    before, tabs_before = await ui.dialog_count(env), await ui.tab_names(env)
    problems: list[str] = []
    findings: list = []
    try:
        t_open = time.monotonic()
        opened, how = await _open_marked_row(env, edit, before, tabs_before)
        if not opened:
            errors, _ = await ui.popup_problems(env)
            if errors:
                raise StepFailed(f"Бичлэгийг нээхэд: {errors[0].text}", errors)
            raise StepBlocked("Мөр дарах / «Засах»-аар засварын форм нээгдсэнгүй", category="tool")
        env.ctx._fc_edit_mode = "row" if how == "мөр дарахад" else "button"
        tm.lap("форм нээх")
        await ui.settle(env, 10)
        # ж: Данс — CostCenterId алдаа серверийн хариугаар ~1с-ийн дараа гарна; settle түүнийг хүлээнэ, энэ нь нөөц
        await asyncio.sleep(ui.fcfg(env).get("form_error_grace_ms", 500) / 1000)
        errors, _ = await ui.popup_problems(env)
        if errors:
            raise StepFailed(f"Бичлэгийг ({row[:60]}) засахаар нээхэд: {errors[0].text}", errors)
        tm.lap("алдаа хүлээх")
        env.note(f"Засварын форм {how} нээгдлээ: {row[:80]}")
        # Засварын форм дээр ч lookup / ComboBox / дотоод табууд (бодит бичлэг — юу ч сонгохгүй, мөр нэмэхгүй)
        await _check_form(env, "Засварын форм", before + (opened == "dialog"), problems, findings, tm,
                          mode="edit", t_open=t_open)
    finally:
        await _close_all(env, before, tabs_before)
        tm.lap("хаах")
    # Эхний мөр онцгой (систем/анхны) бичлэг байж болно — дараагийн мөрүүдийг ч (lookup-гүй, хурдан) нээж шалгана
    await _extra_rows(env, row, edit, problems, findings)
    tm.lap("нэмэлт мөр")
    if findings:
        raise StepFailed("; ".join(problems)[:1500], findings)
    return {}


async def _open_marked_row(env, edit: list[str], before: int, tabs_before: list[str]) -> tuple[str, str]:
    """[data-ssg-row='0'] мөрийг засварын горимд нээнэ (мөр дарах, үгүй бол «Засах»). (opened, how)."""
    target = await env.page.evaluate(ui.MARK_ROW_TARGET_JS)
    await env.page.click("[data-ssg-rowsel='1']" if target else "[data-ssg-row='0']", timeout=5000)
    # SmartApp: мөр дарахад бичлэг шууд нээгдэнэ; үгүй бол фокуслагдсан мөрийг «Засах» (уншихад л — хадгалахгүй).
    # Систем бүр нэг аргаар ажилладаг тул өмнөх цэсүүдээс суралцана: «Засах»-аар нээгддэг бол мөрийн
    # хүлээлтийг (3 сек) цэс бүрт давтахгүй.
    mode = getattr(env.ctx, "_fc_edit_mode", "")
    opened, _, _ = await wait_opened(env, before, tabs_before, menus=False,
                                     timeout_s=0.6 if mode == "button" else 1.5)
    how = "мөр дарахад"
    if not opened and await ui.has_button(env, edit):
        await ui.click_button(env, edit, scope="page")
        opened, _, _ = await wait_opened(env, before, tabs_before, menus=False)
        how = "«Засах»-аар"
    return opened, how


async def _extra_rows(env, first: str, edit: list[str], problems: list[str], findings: list) -> None:
    """Жагсаалтын 2..N дэх бичлэгийг засварын горимд нээж алдаа / өгөгдөл ачаалагдсан эсэхийг шалгана (хадгалахгүй)."""
    want = max(0, int(ui.fcfg(env).get("edit_rows", 3)) - 1)
    seen, checked = {first}, []
    idx = 0
    while len(checked) < want and idx < want + 3:
        idx += 1
        try:
            row = await env.page.evaluate(ui.MARK_FIRST_ROW_JS, [ui.sel_payload(env), idx])
        except Exception:
            row = None
        if not row:
            break
        if row in seen:
            continue
        seen.add(row)
        where = f"Засварын форм (мөр {idx + 1}: {row[:40]})"
        before, tabs_before = await ui.dialog_count(env), await ui.tab_names(env)
        try:
            opened, _ = await _open_marked_row(env, edit, before, tabs_before)
            if not opened:
                errors, _ = await ui.popup_problems(env)
                if errors:
                    problems.append(f"{where} нээхэд: {errors[0].text}")
                    findings.extend(errors)
                else:
                    env.note(f"{where}: нээгдсэнгүй — алгасав")
                continue
            await ui.settle(env, 10)
            await asyncio.sleep(ui.fcfg(env).get("form_error_grace_ms", 500) / 1000)
            errors, _ = await ui.popup_problems(env)
            if errors:
                problems.append(f"{where} нээхэд: {errors[0].text}")
                findings.extend(errors)
                continue
            fields = await ui.scan_form(env, wait_s=2)
            deadline = time.monotonic() + ui.fcfg(env).get("edit_data_wait_ms", 5000) / 1000
            while not any((f.get("value") or "").strip() for f in fields) and time.monotonic() < deadline:
                await asyncio.sleep(0.4)
                fields = await ui.scan_form(env, wait_s=0)
            if len(fields) >= 2 and not any((f.get("value") or "").strip() for f in fields):
                problems.append(f"{where}: бүх талбар ({len(fields)}) хоосон — бичлэгийн өгөгдөл ачаалагдсангүй")
                findings.append(M.Finding(M.SYSTEM_ERROR, f"{where}: бичлэгийн өгөгдөл форм дээр ачаалагдсангүй"))
            checked.append(row[:40])
        except StepFailed as e:
            problems.append(f"{where}: {e}")
            findings.extend(e.findings)
        except Exception as e:
            env.note(f"{where}: шалгаж чадсангүй ({str(e).splitlines()[0][:80]})")
        finally:
            await _close_all(env, before, tabs_before)
    if checked:
        env.note(f"Нэмэлт {len(checked)} бичлэгийг засварын горимд нээв: " + "; ".join(checked))
