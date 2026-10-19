"""Бизнес процессын (E2E) алхмууд: баримтын үйлдэл (батлах / цуцлах), систем үүсгэсэн дугаар барих,
тайлангаас утга унших, өмнө/дараах зөрүүг (Δ) шалгах.

Зарчим: ERP-д «амжилттай хадгалагдлаа» нь зөв гэсэн үг биш — тайлангийн тоо зөв хөдөлсөн эсэх нь жинхэнэ шалгуур.
Нийтлэг тест орчинд бусдын өгөгдөл байсан ч абсолют утга биш зөвхөн Δ-г шалгана.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
from pathlib import Path

from app.flows import ui
from app.flows.context import StepBlocked, StepFailed
from app.flows.engine import step
from app.flows.steps.auto import is_date_field
from app.flows.steps.crud import _require_tag
from app.flows.ui._js import _COMMON_JS

# Харагдах (цонхны бус) grid-үүдээс {headers, rows} уншина: хүссэн баганууд (want) хамгийн их таарсан, дараа нь
# хамгийн олон мөртэй. Групп мод (dxbl-tree-list) алгасна — SmartApp «Данс» жагсаалтын зүүн «Групп» мод олон мөртэй
# тул хайлтаар шүүсэн (1 мөртэй) үндсэн grid-ийн оронд сонгогдож байсан (testaccounting 2026-10-19).
READ_GRID_JS = r"""
([sel, want]) => {
  /*__COMMON__*/
  const dialogs = __findDialogs(sel);
  const grids = Array.from(document.querySelectorAll(sel.grid || '.dxbl-grid, [role=grid], table')).filter(__vis)
    .filter(g => !dialogs.some(d => d.contains(g)))
    .filter(g => g.tagName !== 'DXBL-TREE-LIST' && !g.closest('dxbl-tree-list'))
    .filter((g, k, a) => !a.some(o => o !== g && g.contains(o)));        // дотоод table — гадна grid-ээр нь
  const clean = (e) => __txt(e).replace(/No filter applied|Filter applied/gi, '').replace(/\s+/g, ' ').trim();
  const norm = (s) => s.toLowerCase();
  const wanted = (want || []).map(norm);
  let best = null, bestKey = null;
  for (const g of grids) {
    // Навч толгой: хамгийн олон нүдтэй толгойн мөр — «Дүн» мэт бүлгийн (band) толгойн мөрийг холих юм бол индекс
    // шилжиж «Кредит»-ийг «Дебет» гэж уншдаг байсан (testaccounting P1, 2026-10-19)
    const hrows = Array.from(g.querySelectorAll('thead tr, tr.dxbl-grid-header-row, [role=row]'))
      .filter(r => r.querySelector('th, [role=columnheader]'));
    const leaf = hrows.map(r => Array.from(r.querySelectorAll('th, [role=columnheader]')).filter(__vis))
      .sort((a, b) => b.length - a.length)[0] || [];
    if (!leaf.length) continue;
    const heads = leaf.map(clean);
    const hbox = leaf.map(h => h.getBoundingClientRect());
    // Нүдийг толгойтой ДЭЛГЭЦИЙН БАЙРЛАЛААР тааруулна (нүдний голын x нь толгойн хүрээнд) — нуугдмал/нэмэлт багана,
    // сонголтын checkbox байсан ч зөв; байрлал тодорхойгүй бол дарааллаар
    const align = (cells) => {
      const out = heads.map(() => '');
      let used = 0;
      for (const c of cells) {
        const b = c.getBoundingClientRect(), x = b.left + b.width / 2;
        const k = hbox.findIndex(h => h.width > 0 && x >= h.left - 1 && x <= h.right + 1);
        if (k >= 0 && !out[k]) { out[k] = clean(c); used++; }
      }
      if (!used) return cells.map(clean);
      return out;
    };
    const rows = Array.from(g.querySelectorAll('tbody tr, [role=row]')).filter(__vis)
      .filter(r => !r.querySelector('th, [role=columnheader]') && !r.closest('thead'))
      .map(r => align(Array.from(r.querySelectorAll('td, [role=gridcell]')).filter(c => !c.closest('table') || c.closest('tr') === r)))
      .filter(cells => cells.some(c => c));
    const hit = wanted.filter(w => heads.some(h => norm(h) === w || norm(h).includes(w))).length;
    const key = [hit, rows.length];
    if (!best || key[0] > bestKey[0] || (key[0] === bestKey[0] && key[1] > bestKey[1])) {
      best = {headers: heads, rows}; bestKey = key;
    }
  }
  return best;
}
""".replace("/*__COMMON__*/", _COMMON_JS)


# ---------------------------------------------------------------- тоо, хүснэгт
def parse_amount(text) -> float | None:
    """«1,234,567.89», «1 234 567,89», «(1,000.00)», «-500» → float; тоо биш бол None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = re.sub(r"[\s ₮$]", "", str(text))
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if not re.fullmatch(r"[-+]?[\d.,]+", t or "x"):
        return None
    if "," in t and "." in t:
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", "") if re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+", t) else t.replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _col_index(headers: list[str], column: str) -> int:
    """Баганын индекс: «Эцсийн үлдэгдэл|Үлдэгдэл» — хувилбаруудыг дарааллаар (яг ижил, эс бөгөөс агуулсан)."""
    hs = [_norm(h) for h in headers]
    for alt in [a for a in str(column).split("|") if a.strip()] or [""]:
        c = _norm(alt)
        idx = hs.index(c) if c in hs else next((i for i, h in enumerate(hs) if c and c in h), -1)
        if idx >= 0:
            return idx
    return -1


def _want_columns(rows_spec: dict) -> list[str]:
    """READ_GRID_JS-д: хүссэн баганууд (хувилбар бүрийг тусад нь) — grid сонгоход."""
    out: list[str] = []
    for x in rows_spec.values():
        for k in ("column", "minus"):
            out += [a.strip() for a in str(x.get(k) or "").split("|") if a.strip()]
    return out


def pick_value(headers: list[str], rows: list[list[str]], match: str, column: str) -> tuple[float | None, str]:
    """Мөр (аль нэг нүд нь match-тай тэнцүү, эс бөгөөс агуулсан) × багана (толгой нь column) → (тоо, тайлбар)."""
    idx = _col_index(headers, column)
    if idx < 0:
        return None, f"«{column}» багана олдсонгүй (баганууд: {', '.join(headers[:12])})"
    m = _norm(match)
    row = next((r for r in rows if any(_norm(x) == m for x in r)), None) or \
        next((r for r in rows if any(m in _norm(x) for x in r)), None)
    if row is None:
        return None, f"«{match}» мөр олдсонгүй"
    # DevExpress grid-ийн сонголтын (checkbox) багана толгойгүй байж болно — нүд олон бол баруун талаас тааруулна
    shift = max(0, len(row) - len(headers))
    cell = row[idx + shift] if idx + shift < len(row) else ""
    if not str(cell).strip():
        return 0.0, ""          # SmartApp тэг үлдэгдлийг хоосон нүдээр харуулдаг (шинэ данс)
    v = parse_amount(cell)
    return v, ("" if v is not None else f"«{match}» × «{column}» нүд тоо биш: «{cell}»")


def sum_values(headers: list[str], rows: list[list[str]], match: str, column: str,
               minus: str = "") -> tuple[float | None, str]:
    """match агуулсан БҮХ мөрийн Σ column (− Σ minus) — ж: ГД-ийн бичилтийн жагсаалтаас дансны үлдэгдэл
    (Σ Дебет − Σ Кредит). Таарах мөргүй бол 0."""
    i, j = _col_index(headers, column), (_col_index(headers, minus) if minus else -1)
    if i < 0 or (minus and j < 0):
        miss = column if i < 0 else minus
        return None, f"«{miss}» багана олдсонгүй (баганууд: {', '.join(headers[:12])})"
    m = _norm(match)
    total = 0.0
    for r in rows:
        if not any(_norm(x) == m or m in _norm(x) for x in r):
            continue
        shift = max(0, len(r) - len(headers))
        for k, sign in ((i, 1), (j, -1)):
            if k < 0:
                continue
            cell = r[k + shift] if k + shift < len(r) else ""
            v = parse_amount(cell) if str(cell).strip() else 0.0
            if v is None:
                return None, f"«{match}» мөрийн «{headers[k]}» нүд тоо биш: «{cell}»"
            total += sign * v
    return total, ""


def read_table_file(path: Path) -> tuple[list[str], list[list[str]]]:
    """Export хийсэн тайлан (xlsx / csv) → (толгой, мөрүүд). Толгой = эхний ≥2 текст нүдтэй мөр."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
        raw = [[("" if v is None else str(v)) for v in r] for ws in wb.worksheets for r in ws.iter_rows(values_only=True)]
    else:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t") if text.strip() else csv.excel
        raw = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    raw = [[c.strip() for c in r] for r in raw if any((c or "").strip() for c in r)]
    for i, r in enumerate(raw):
        if sum(1 for c in r if c and parse_amount(c) is None) >= 2:
            return r, raw[i + 1:]
    return (raw[0] if raw else []), raw[1:]


# ---------------------------------------------------------------- report.read
@step("report.read")
async def report_read(env, args: dict) -> dict:
    """Тайлангаас утгуудыг уншина.

    with:
      menu: "Ерөнхий дэвтэр / Тайлан / Гүйлгээ баланс"
      params: {"Эхлэх огноо": "2026-01-01", "Дуусах огноо": "{today}"}   # шүүлтийн талбарууд
      run: ["Шүүх"]                  # тайланг гаргах товч (заавал биш)
      mode: grid | export            # grid — дэлгэцийн хүснэгт; export — файл татаж задлах (report viewer)
      search: true                   # grid: мөр бүрийг жагсаалтын хайлтаар шүүж уншина (ж: «Данс» жагсаалт)
      export: ["Файл үүсгэх", "Excel"]  # export горимын товч/цэсийн дараалал (сүүлийнх нь файл татна)
      rows: {avlaga: {match: "1201", column: "Эцсийн үлдэгдэл"}}
            # эсвэл нийлбэр: {match: "<данс>", column: "Дебет", minus: "Кредит", agg: sum} — ГД-ийн бичилтийн жагсаалтаас
      refresh: true                  # уншихын өмнө жагсаалтыг «Шүүх»/«Сэргээх»-ээр шинэчилнэ (нээлттэй таб хуучирсан байж болно)
      save_as: tb_before             # → ctx[tb_before] = {avlaga: 500000.0, …}
    """
    await ui.close_form(env)
    by_match: dict[str, list] = {}           # search: match → тэр хайлтын мөрүүд
    await ui.clear_toasts(env)
    await ui.ensure_menu(env, args["menu"])
    words = (ui.fcfg(env).get("auto") or {})
    for label, value in (args.get("params") or {}).items():
        info = await ui.find_field(env, label, wait_s=10)
        await ui.fill_field(env, label, value, as_date=is_date_field({**info, "label": label}, words or None))
    if args.get("run"):
        await ui.click_button(env, ui.buttons(env, "filter", args["run"]), scope="page")
        await ui.settle(env)
    await ui.raise_on_problems(env, "Тайлан гаргахад")
    mode = args.get("mode", "grid")
    if mode == "export":
        seq = list(args.get("export") or ["Файл үүсгэх", "Excel"])
        out_dir = env.run_dir / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        async with env.page.expect_download(timeout=int(args.get("timeout_ms", 60000))) as dl_info:
            for i, b in enumerate(seq):
                if i and await ui.click_popup_item(env, b):
                    continue
                await ui.click_button(env, [b], scope="page")
                await asyncio.sleep(0.3)
        dl = await dl_info.value
        path = out_dir / f"{args.get('save_as', 'report')}-{dl.suggested_filename}"
        await dl.save_as(str(path))
        headers, rows = read_table_file(path)
        env.note(f"Тайлангийн файл: reports/{path.name} ({len(rows)} мөр)")
    elif args.get("search"):
        # Жагсаалт (ж: «Данс» — Код, Үлдэгдэл): мөр бүрийг хайлтаар шүүж уншина (олон хуудастай жагсаалт)
        if args.get("refresh", True):
            await ui.reload_list(env)       # өмнө нээсэн таб — шинэ бичилт харагдахгүй байж болно
        # Нэг match-ыг НЭГ удаа хайна; alias бүр зөвхөн өөрийн хайлтын мөрөөс тооцно — ижил match-тай хоёр alias
        # (ж: sale_dt, sale_net) мөрийг давхар нэмж Σ-г 2 дахин өсгөж байсан (P2 mock, 2026-10-19)
        headers, rows = [], []
        for spec in (args.get("rows") or {}).values():
            m = str(spec["match"])
            if m in by_match:
                continue
            await ui.grid_find_settled(env, m)
            grid = await env.page.evaluate(READ_GRID_JS, [ui.sel_payload(env), _want_columns(args.get("rows") or {})])
            by_match[m] = (grid or {}).get("rows") or []
            if grid and grid["rows"]:
                headers = grid["headers"]
                rows += grid["rows"]
        if not rows and not all((x.get("agg") == "sum") for x in (args.get("rows") or {}).values()):
            raise StepBlocked("Жагсаалтаас хайсан мөр олдсонгүй — rows.match (дансны код)-ыг шалгана уу", category="tool")
        if not headers:      # нийлбэр, бичилтгүй (шинэ данс) — баганыг хоосон grid-ийн толгойноос
            grid = await env.page.evaluate(READ_GRID_JS, [ui.sel_payload(env), _want_columns(args.get("rows") or {})])
            headers = (grid or {}).get("headers") or []
        if not headers:
            # Хүснэгт огт зурагдаагүй (толгойгүй) — ж: testaccounting «Харилцагчийн үлдэгдлийн жагсаалт /дансаар/»
            # өгөгдөл ачаалахад ErrorBoundary ('CustomerGroup' not found) унаж таб хоосон болсон (2026-10-19)
            raise StepFailed("Жагсаалт ачаалагдсангүй — хүснэгт (толгой) зурагдаагүй, таб хоосон "
                             "(консолын алдааг findings-ээс харна уу)")
    else:
        grid = None
        for _ in range(int(args.get("wait_s", 10) * 2)):
            grid = await env.page.evaluate(READ_GRID_JS, [ui.sel_payload(env), _want_columns(args.get("rows") or {})])
            if grid and grid["rows"]:
                break
            await asyncio.sleep(0.5)
        if not grid or not grid["rows"]:
            raise StepBlocked("Тайлангийн хүснэгт олдсонгүй/хоосон — mode: export (Файл үүсгэх) эсвэл params/run-ийг шалгана уу",
                              category="tool")
        headers, rows = grid["headers"], grid["rows"]
    values, problems = {}, []
    for alias, spec in (args.get("rows") or {}).items():
        if spec.get("agg") == "sum":
            own = by_match.get(str(spec["match"]), rows)
            v, why = sum_values(headers, own, str(spec["match"]), str(spec["column"]), str(spec.get("minus") or ""))
        else:
            v, why = pick_value(headers, rows, str(spec["match"]), str(spec["column"]))
        if v is None:
            problems.append(f"{alias}: {why}")
        else:
            values[alias] = v
    if problems:
        raise StepFailed("Тайлангаас утга уншиж чадсангүй: " + "; ".join(problems))
    env.note("Тайлан: " + ", ".join(f"{k}={v:,.2f}" for k, v in values.items()))
    return {args.get("save_as", "report"): values}


# ---------------------------------------------------------------- assert.delta
@step("assert.delta", needs_session=False)
async def assert_delta(env, args: dict) -> dict:
    """Хоёр тайлангийн агшны зөрүүг шалгана.

    with:
      before: tb_before              # report.read-ийн save_as нэр
      after: tb_after
      expect: {avlaga: "{amount}", orlogo: "-{amount}"}   # Δ = after − before
      tolerance: 0.01
    """
    missing = [str(args[k]) for k in ("before", "after") if str(args[k]) not in env.ctx.data]
    if missing:
        # Тайлангийн агшин авагдаагүй (өмнөх уншилт амжилтгүй) — харьцуулах зүйлгүй, аппын шинэ алдаа биш
        raise StepBlocked(f"Харьцуулах тайлангийн агшин алга: {', '.join(missing)} (уншилтын алхам амжилтгүй болсон)")
    before = env.ctx.data.get(str(args["before"])) or {}
    after = env.ctx.data.get(str(args["after"])) or {}
    tol = float(args.get("tolerance", 0.01))
    lines, wrong = [], []
    for alias, exp in (args.get("expect") or {}).items():
        e = parse_amount(exp)
        if e is None:
            raise StepFailed(f"assert.delta: «{alias}»-ийн хүлээгдэж буй утга тоо биш: {exp!r}")
        if alias not in before or alias not in after:
            raise StepFailed(f"assert.delta: «{alias}» тайлангийн агшинд алга (before/after-ийн rows-д нэмнэ үү)")
        d = after[alias] - before[alias]
        lines.append(f"{alias}: {before[alias]:,.2f} → {after[alias]:,.2f} (Δ {d:+,.2f}, хүлээгдсэн {e:+,.2f})")
        if abs(d - e) > tol:
            wrong.append(lines[-1])
    if wrong:
        raise StepFailed("Тайлангийн зөрүү хүлээгдэж буйгаас өөр: " + "; ".join(wrong))
    env.note("; ".join(lines))
    return {}


# ---------------------------------------------------------------- doc.number
@step("doc.number")
async def doc_number(env, args: dict) -> dict:
    """Хадгалсны дараа систем үүсгэсэн утгыг (баримтын дугаар, код) жагсаалтаас уншиж дамжуулна.

    with: {menu, find: "{s01_rec}", column: "Дугаар", save_as: s01_no}"""
    find = str(args["find"])
    await ui.ensure_menu(env, args.get("menu"))
    if not await ui.grid_find_settled(env, find):
        raise StepFailed(f"'{find}' хайлтаар мөр олдсонгүй")
    grid = await env.page.evaluate(READ_GRID_JS, [ui.sel_payload(env), [str(args.get("column", "Дугаар"))]])
    col = _norm(args.get("column", "Дугаар"))
    hs = [_norm(h) for h in (grid or {}).get("headers") or []]
    idx = hs.index(col) if col in hs else next((i for i, h in enumerate(hs) if col in h), -1)
    row = next((r for r in (grid or {}).get("rows") or [] if any(_norm(find) in _norm(x) for x in r)), None)
    if idx < 0 or row is None:
        raise StepFailed(f"«{args.get('column', 'Дугаар')}» баганыг «{find}» мөрөөс уншиж чадсангүй")
    shift = max(0, len(row) - len(hs))
    value = row[idx + shift] if idx + shift < len(row) else ""
    if not value:
        raise StepFailed(f"«{args.get('column', 'Дугаар')}» хоосон — систем дугаар олгоогүй")
    env.note(f"{args.get('column', 'Дугаар')} = {value}")
    return {args["save_as"]: value}


# ---------------------------------------------------------------- doc.action
@step("doc.action")
async def doc_action(env, args: dict) -> dict:
    """Тестийн баримт дээр бизнес үйлдэл (Батлах / Цуцлах / Илгээх …) хийнэ.

    with:
      menu: "..."
      find: "{s01_rec}"              # заавал run_tag агуулна — бодит баримтад хэзээ ч хүрэхгүй
      action: "Батлах"               # эсвэл ["Батлах", "Approve"]
      confirm: true                  # баталгаажуулах цонхонд «Тийм»
      expect: {column: "Төлөв", value: "Батлагдсан"}   # үйлдлийн дараах төлөв (заавал биш)
    """
    find = str(args["find"])
    _require_tag(env, find, "Үйлдэл хийх баримт")
    action = args["action"]
    texts = [action] if isinstance(action, str) else list(action)
    await ui.close_form(env)
    await ui.clear_toasts(env)
    await ui.ensure_menu(env, args.get("menu"))
    if not await ui.grid_find_settled(env, find):
        raise StepFailed(f"'{find}' баримт жагсаалтаас олдсонгүй")
    sel = await ui.select_row(env, find)
    if sel.errors:
        raise StepFailed(f"Баримтыг нээхэд: {sel.errors[0].text}", sel.errors)
    if sel.focus == "mismatch":
        raise StepBlocked(f"Фокуслагдсан мөр «{find}» биш — өөр баримт дээр үйлдэл хийхээс сэргийлж зогсоов")
    before = await ui.dialog_count(env)
    await ui.click_button(env, texts, scope="auto" if (sel.opened or sel.new_tabs) else "page")
    await asyncio.sleep(0.4)
    if args.get("confirm", True) and await ui.dialog_count(env) > before:
        try:
            await ui.click_button(env, ui.buttons(env, "confirm") + ["Тийм"], scope="dialog", wait_s=2)
        except StepFailed:
            pass                                   # баталгаажуулалтгүй — гарсан цонх нь үр дүн/алдаа байж болно
    await asyncio.sleep(0.3)
    await ui.settle(env)
    # Анхааруулга (ж: «Зөвхөн ноорог баримтыг батална») — систем үйлдлийг хүлээж аваагүй → Fail
    await ui.raise_on_problems(env, f"«{texts[0]}» үйлдэлд")
    if sel.opened or sel.new_tabs:
        await ui.close_form(env, keep=sel.level)
        if sel.new_tabs:
            await ui.close_tabs(env, sel.new_tabs)
        await ui.ensure_menu(env, args.get("menu"))
    exp = args.get("expect") or {}
    if exp:
        want = _norm(exp.get("value"))
        got = ""
        for _ in range(4):
            await ui.grid_find_settled(env, find)
            grid = await env.page.evaluate(READ_GRID_JS, [ui.sel_payload(env), [str(exp.get("column", "Төлөв"))]])
            hs = [_norm(h) for h in (grid or {}).get("headers") or []]
            c = _norm(exp.get("column", "Төлөв"))
            idx = hs.index(c) if c in hs else -1
            row = next((r for r in (grid or {}).get("rows") or [] if any(_norm(find) in _norm(x) for x in r)), None)
            if row is not None and idx >= 0:
                got = row[min(idx + max(0, len(row) - len(hs)), len(row) - 1)]
                if _norm(got) == want:
                    break
            await asyncio.sleep(1.0)
        if _norm(got) != want:
            raise StepFailed(f"«{texts[0]}» үйлдлийн дараа {exp.get('column', 'Төлөв')} = «{got}», хүлээгдсэн «{exp.get('value')}»")
    env.note(f"«{texts[0]}»: {find}" + (f" → {exp.get('value')}" if exp else ""))
    return {}
