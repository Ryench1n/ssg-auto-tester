"""07_Test-Cases-Загвар.xlsx загварыг бөглөнө (openpyxl). Загварыг шинээр үүсгэхгүй —
хуулж нээгээд sheet нэр, стиль, томьёо, data validation, merged cell-ийг хадгална."""
from __future__ import annotations

import math
import re
import shutil
from copy import copy
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.models import Bug, TestCase
from app.runner.classify import stats, template_v4_expected
from app.runner.severity import (XLSX_AUTOMATION, XLSX_PRIORITIES, XLSX_RESULTS,
                                 XLSX_TEST_TYPES)

TC_SHEET = "📋 Test Cases"
BUG_SHEET = "🐛 Bug Tracker"
TC_FIRST_ROW = 6
TC_TEMPLATE_LAST_ROW = 111      # загварын data validation-ий мөр
FORMULA_LAST_ROW = 2000         # P4:V4 томьёоны муж
BUG_FIRST_ROW = 6
BUG_NOTES_ROWS = (10, 11, 12)   # "BUG ID ХОЛБООНЫ ЖУРАМ" блок
BUG_SPACER_ROW = 9              # загварын хоосон (стильтэй) мөр
BUG_DV_LAST_ROW = 58
CELL_MAX = 32000


class XlsxWriteError(Exception):
    pass


def _copy_style(src, dst) -> None:
    dst._style = copy(src._style)


def _est_height(values: list[str], widths: list[float], base: float, line_h: float = 13.0,
                cap: float = 160.0) -> float:
    lines = 1
    for v, w in zip(values, widths):
        if not v:
            continue
        per_line = max(int((w or 10) * 1.15), 8)
        n = sum(max(1, math.ceil(len(part) / per_line)) for part in str(v).split("\n"))
        lines = max(lines, n)
    return min(max(base, lines * line_h + 6), cap)


def _validate_values(tc: TestCase) -> None:
    """Загварын dropdown-той яг таарах ёстой — эс бөгөөс Dashboard буруу тооцоолно."""
    checks = [("G", tc.test_type, XLSX_TEST_TYPES), ("H", tc.priority, XLSX_PRIORITIES),
              ("I", tc.automation, XLSX_AUTOMATION), ("J", tc.result, XLSX_RESULTS)]
    for col, val, allowed in checks:
        if val not in allowed:
            raise XlsxWriteError(f"{tc.tc_id}: {col} баганын утга «{val}» нь загварын жагсаалтад алга: {allowed}")


def _set_sqref(ws, first_col: str, new_last: int) -> None:
    for dv in ws.data_validations.dataValidation:
        ref = str(dv.sqref)
        m = re.fullmatch(rf"{first_col}(\d+):{first_col}(\d+)", ref)
        if m and int(m.group(2)) < new_last:
            dv.sqref = type(dv.sqref)(f"{first_col}{m.group(1)}:{first_col}{new_last}")


def write_test_cases_xlsx(template: Path, out: Path, project: str, version: str,
                          tcs: list[TestCase], bugs: list[Bug], qa_tester: str, kind: str = "Smoke тест") -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, out)
    wb = load_workbook(out)
    for name in (TC_SHEET, BUG_SHEET):
        if name not in wb.sheetnames:
            raise XlsxWriteError(f"Загварт «{name}» sheet алга")

    # ------------------------------------------------------ 📋 Test Cases
    ws = wb[TC_SHEET]
    ws["A1"] = f"SSG TEST CASE TRACKER — {project} / {kind} — {version}"
    cols = range(1, 15)  # A..N
    style_rows = [{c: copy(ws.cell(TC_FIRST_ROW, c)._style) for c in cols},
                  {c: copy(ws.cell(TC_FIRST_ROW + 1, c)._style) for c in cols}]
    base_h = ws.row_dimensions[TC_FIRST_ROW].height or 57.75
    widths = [ws.column_dimensions[get_column_letter(c)].width or 12 for c in cols]

    # жишээ мөрүүд (6–11) болон бусад үлдэгдлийг цэвэрлэнэ
    for r in range(TC_FIRST_ROW, max(ws.max_row, TC_TEMPLATE_LAST_ROW) + 1):
        for c in cols:
            ws.cell(r, c).value = None

    last = TC_FIRST_ROW - 1
    for i, tc in enumerate(tcs):
        _validate_values(tc)
        r = TC_FIRST_ROW + i
        last = r
        values = [tc.tc_id, tc.req_ref, tc.purpose, tc.precondition, tc.steps, tc.expected,
                  tc.test_type, tc.priority, tc.automation, tc.result, tc.executor, tc.date,
                  ", ".join(tc.bug_ids), tc.note]
        # Мөр бүрийн стилийг 6-р мөрөөс (fill/border/alignment/wrap) хуулна; загварын
        # ээлжлэн өнгийг (zebra) хадгалахын тулд сондгой мөрөнд 7-р мөрийн стилийг хэрэглэнэ.
        st = style_rows[i % 2]
        for c, v in zip(cols, values):
            cell = ws.cell(r, c)
            cell._style = copy(st[c])
            cell.value = (str(v)[:CELL_MAX] if isinstance(v, str) else v) or None
        ws.row_dimensions[r].height = _est_height(
            [values[2], values[3], values[4], values[5], values[13]],
            [widths[2], widths[3], widths[4], widths[5], widths[13]], base_h)

    # хоосон үлдсэн загварын мөрүүдийн өндрийг хэвийн болгоно (111 хүртэл)
    for r in range(last + 1, TC_TEMPLATE_LAST_ROW + 1):
        ws.row_dimensions[r].height = ws.row_dimensions[TC_TEMPLATE_LAST_ROW].height or 36.0

    if last > TC_TEMPLATE_LAST_ROW:
        for col in ("G", "H", "I", "J"):
            _set_sqref(ws, col, last)
    if last > FORMULA_LAST_ROW:
        for ref in ("P4", "R4", "S4", "T4", "V4"):
            f = ws[ref].value
            if isinstance(f, str):
                ws[ref] = f.replace(str(FORMULA_LAST_ROW), str(last))

    # ------------------------------------------------------ 🐛 Bug Tracker
    wb_ = wb[BUG_SHEET]
    wb_["A1"] = f"SSG BUG TRACKER — {project}"
    bcols = range(1, 11)  # A..J
    bug_style = {c: copy(wb_.cell(BUG_FIRST_ROW, c)._style) for c in bcols}
    bug_h = wb_.row_dimensions[BUG_FIRST_ROW].height or 43.5
    spacer_style = {c: copy(wb_.cell(BUG_SPACER_ROW, c)._style) for c in bcols}
    spacer_h = wb_.row_dimensions[BUG_SPACER_ROW].height or 27.75
    bwidths = [wb_.column_dimensions[get_column_letter(c)].width or 12 for c in bcols]

    # Журмын тайлбарын блокыг (10–12-р мөр) УСТГАХГҮЙ — утга, стиль, өндөр, merge-ийг
    # нь хадгалаад bug-уудын доор зөөнө.
    # Шийдвэр: bug-ийг 13-р мөрөөс хойш бичвэл 6–9-р мөр хоосон үлдэж хүснэгт тасарна,
    # A4 (=COUNTA) томьёо толгой болон тайлбарын мөрийг тоолж буруу болно. Харин блокыг
    # доош зөөвөл bug-ууд толгойн доор тасралтгүй, A4 = COUNTA(A6:A<сүүлийн bug>) цэвэр ажиллана.
    notes = []
    for r in BUG_NOTES_ROWS:
        notes.append({
            "height": wb_.row_dimensions[r].height,
            "cells": {c: (wb_.cell(r, c).value, copy(wb_.cell(r, c)._style)) for c in bcols},
            "merged": any(str(m) == f"A{r}:J{r}" for m in wb_.merged_cells.ranges),
        })
    for r in BUG_NOTES_ROWS:
        rng = f"A{r}:J{r}"
        if any(str(m) == rng for m in wb_.merged_cells.ranges):
            wb_.unmerge_cells(rng)

    n = len(bugs)
    last_bug = BUG_FIRST_ROW + n - 1
    notes_start = max(BUG_NOTES_ROWS[0], last_bug + 2)
    clear_to = max(wb_.max_row, BUG_DV_LAST_ROW, notes_start + len(notes))
    for r in range(BUG_FIRST_ROW, clear_to + 1):
        for c in bcols:
            wb_.cell(r, c).value = None

    for i, b in enumerate(bugs):
        r = BUG_FIRST_ROW + i
        note = b.fix + (f" | {b.security_note}" if b.security_note else "")
        values = [b.bug_id, ", ".join(b.tc_ids)[:CELL_MAX], b.title, b.severity.xlsx, b.status,
                  b.found_date, "[Засагдаагүй]", None, qa_tester, note]
        for c, v in zip(bcols, values):
            cell = wb_.cell(r, c)
            cell._style = copy(bug_style[c])
            cell.value = v
        wb_.row_dimensions[r].height = _est_height([values[1], values[2], values[9]],
                                                  [bwidths[1], bwidths[2], bwidths[9]], bug_h)

    # bug ба тайлбарын хоорондох болон тайлбараас хойшхи мөрүүд — загварын хоосон мөрийн стиль
    for r in list(range(last_bug + 1, notes_start)) + list(range(notes_start + len(notes), clear_to + 1)):
        if r < BUG_FIRST_ROW:
            continue
        for c in bcols:
            wb_.cell(r, c)._style = copy(spacer_style[c])
        wb_.row_dimensions[r].height = spacer_h

    for k, nd in enumerate(notes):
        r = notes_start + k
        for c, (v, stl) in nd["cells"].items():
            cell = wb_.cell(r, c)
            cell._style = stl
            cell.value = v
        wb_.row_dimensions[r].height = nd["height"]
        if nd["merged"]:
            wb_.merge_cells(f"A{r}:J{r}")

    wb_["A4"] = f"=COUNTA(A{BUG_FIRST_ROW}:A{max(last_bug, BUG_FIRST_ROW)})"
    if last_bug > BUG_DV_LAST_ROW:
        for col in ("D", "E"):
            _set_sqref(wb_, col, last_bug)

    # Excel нээхэд бүх томьёог дахин тооцоолно (Dashboard, 📖 Заавар хөндөгдөхгүй)
    wb.calculation.fullCalcOnLoad = True
    wb.save(out)

    s = stats(tcs)
    expected = {
        "P4": s["total"], "R4": s["pass"], "S4": s["fail"], "T4": s["blocked"],
        "U4": "—" if s["total"] == 0 else f"{s['pass'] / s['total'] * 100:.1f}%",
        "V4": template_v4_expected(tcs),
        "BUG_A4": n,
        "BUG_C4": sum(1 for b in bugs if b.status == "Open"),
        "notes_row": notes_start,
    }
    return expected
