"""Нэг удаагийн бэлтгэл: 09_Test-Report-Загвар.docx → templates/report_tpl.docx

Загвар дахь `[...]` placeholder, жишээ мөрүүдийг Jinja tag ({{ }}, {%p for %}, {%tr for %})-аар
сольж docxtpl-д зориулсан хуулбар үүсгэнэ. Стиль, өнгө, толгой/хөл, хүснэгтийн форматыг
хөндөхгүй: текстийг байгаа run-д нь бичиж, мөр/догол мөрийг deepcopy-гоор олшруулна.

Ажиллуулах:  python scripts/make_report_template.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "templates" / "09_Test-Report-Загвар.docx"
DST = ROOT / "templates" / "report_tpl.docx"


class TemplateError(Exception):
    pass


# ------------------------------------------------------------------ helpers
PLACEHOLDER_COLORS = {"AAAAAA", "718096"}   # загварын саарал налуу "[...]" placeholder
DATA_COLOR = "1A202C"                       # загварын үндсэн текстийн өнгө


def unplaceholder(p) -> None:
    """Бөглөсөн өгөгдлийг placeholder шиг (саарал, налуу) харагдуулахгүй."""
    for r in p.runs:
        rpr = r._r.rPr
        if rpr is None:
            continue
        col = rpr.find(qn("w:color"))
        if col is not None and col.get(qn("w:val"), "").upper() in PLACEHOLDER_COLORS:
            col.set(qn("w:val"), DATA_COLOR)
            for tag in ("w:i", "w:iCs"):
                el = rpr.find(qn(tag))
                if el is not None:
                    rpr.remove(el)


def set_runs(p, *texts: str) -> None:
    """Эхний run(үүд)-ийн форматыг хадгалж текстийг солино; илүү run-уудыг устгана."""
    runs = p.runs
    if not runs:
        p.add_run(texts[0] if texts else "")
        return
    for i, t in enumerate(texts):
        if i < len(runs):
            runs[i].text = t
        else:
            runs[-1].text += t
    for r in runs[len(texts):]:
        r._r.getparent().remove(r._r)


def set_cell(cell, *lines: str) -> None:
    """Нүдний догол мөр бүрийн текстийг солино (мөрийн тоо = догол мөрийн тоо)."""
    paras = cell.paragraphs
    for i, line in enumerate(lines):
        if i < len(paras):
            set_runs(paras[i], line)
            unplaceholder(paras[i])
    for p in paras[len(lines):]:
        p._p.getparent().remove(p._p)


def clone_para_before(p, text: str):
    new = copy.deepcopy(p._p)
    p._p.addprevious(new)
    from docx.text.paragraph import Paragraph
    np = Paragraph(new, p._parent)
    set_runs(np, text)
    return np


def clone_para_after(p, text: str):
    new = copy.deepcopy(p._p)
    p._p.addnext(new)
    from docx.text.paragraph import Paragraph
    np = Paragraph(new, p._parent)
    set_runs(np, text)
    return np


def remove(el) -> None:
    el.getparent().remove(el)


def find_para(doc, startswith: str):
    for p in doc.paragraphs:
        if p.text.strip().startswith(startswith):
            return p
    raise TemplateError(f"Догол мөр олдсонгүй: {startswith!r}")


def paras_after(doc, heading_text: str, stop_on_heading: bool = True) -> list:
    """Гарчгийн дараах ListParagraph (жишээ bullet) догол мөрүүд."""
    ps = doc.paragraphs
    idx = next(i for i, p in enumerate(ps) if p.text.strip().startswith(heading_text))
    out = []
    started = False
    for p in ps[idx + 1:]:
        sid = p.style.style_id if p.style is not None else ""
        pPr = p._p.pPr
        is_list = pPr is not None and pPr.numPr is not None
        if is_list:
            out.append(p)
            started = True
        elif started:
            break
        elif stop_on_heading and sid.startswith("Heading"):
            break
    if not out:
        raise TemplateError(f"Жагсаалт олдсонгүй: {heading_text!r}")
    return out


def list_loop(doc, heading_text: str, var: str, seq: str):
    """(жишээ bullet догол мөр, {%p endfor %} догол мөр) буцаана."""
    items = paras_after(doc, heading_text)
    first = items[0]
    for p in items[1:]:
        remove(p._p)
    set_runs(first, "{{ " + var + " }}")
    unplaceholder(first)
    clone_para_before(first, "{%p for " + var + " in " + seq + " %}")
    end = clone_para_after(first, "{%p endfor %}")
    return first, end


def insert_after(anchor, src, text: str):
    """src догол мөрийн форматыг хуулж anchor-ын дараа text-тэй шинэ догол мөр оруулна."""
    from docx.text.paragraph import Paragraph
    new = copy.deepcopy(src._p)
    anchor._p.addnext(new)
    np = Paragraph(new, anchor._parent)
    set_runs(np, text)
    unplaceholder(np)
    return np


def row_loop(table, tpl_row_idx: int, loop: str, cells: list[str]) -> None:
    """tpl_row_idx мөрийг {%tr for %} мөрөөр хүрээлж, бусад жишээ мөрийг устгана."""
    rows = table.rows
    tpl = rows[tpl_row_idx]
    for r in rows[tpl_row_idx + 1:]:
        remove(r._tr)
    for c, text in zip(tpl.cells, cells):
        set_cell(c, text)
    for tag, where in (("{%tr " + loop + " %}", "before"), ("{%tr endfor %}", "after")):
        new = copy.deepcopy(tpl._tr)
        (tpl._tr.addprevious if where == "before" else tpl._tr.addnext)(new)
        from docx.table import _Row
        nr = _Row(new, table)
        for i, c in enumerate(nr.cells):
            set_cell(c, tag if i == 0 else "")


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise TemplateError(msg)


# ------------------------------------------------------------------ main
def build(src: Path = SRC, dst: Path = DST) -> Path:
    doc = Document(str(src))
    T = doc.tables
    expect(len(T) == 23, f"Загварын хүснэгтийн тоо 23 байх ёстой, {len(T)} байна")

    # --- толгой (header) ----------------------------------------------------
    for s in doc.sections:
        for p in s.header.paragraphs:
            if "[Төслийн нэр]" in p.text:
                set_runs(p, "{{ project }} — TEST REPORT  |  {{ version }}")

    # --- нүүр --------------------------------------------------------------
    set_runs(find_para(doc, "Тестийн тайлан — Загвар"), "Тестийн тайлан — {{ project }} ({{ test_kind }})")
    set_runs(find_para(doc, "SSG-QA-TR-TEMPLATE"), "SSG-QA-TR  |  {{ test_kind_title }}  |  {{ version }}")
    cover = T[0]
    set_cell(cover.rows[0].cells[1], "{{ file_name }}")
    set_cell(cover.rows[1].cells[1], "{{ version }}")
    set_cell(cover.rows[2].cells[1], "{{ date }}")
    set_cell(cover.rows[3].cells[1], "{{ author }}")
    set_cell(cover.rows[4].cells[1], "{{ period }}")
    set_cell(cover.rows[5].cells[1], "{{ decision }}")

    # --- 1. Танилцуулга ----------------------------------------------------
    box = T[1].rows[0].cells[0]
    set_runs(box.paragraphs[2], "Файлын нэр: {{ file_name }}")
    set_cell(T[2].rows[0].cells[1], "{{ project_full }}")
    set_cell(T[3].rows[0].cells[1], "{{ period }}")
    set_cell(T[4].rows[0].cells[1], "{{ qa_lead }}")
    set_cell(T[5].rows[0].cells[1], "{{ qa_team }}")
    set_cell(T[6].rows[0].cells[1], "{{ author }}")
    set_cell(T[7].rows[0].cells[1], "{{ date }}")

    # --- 2. Хамрах хүрээ ---------------------------------------------------
    list_loop(doc, "2.1. Тестлэсэн хэсгүүд", "x", "tested")
    bullet, end_untested = list_loop(doc, "2.2. Тестлээгүй хэсгүүд", "x", "untested")

    # --- 2.3 E2E урсгал (CRUD flow байвал л гарна) -------------------------
    h22 = find_para(doc, "2.2. Тестлээгүй хэсгүүд")
    body_p = find_para(doc, "Энэхүү Test Report нь")
    a = insert_after(end_untested, body_p, "{%p if flows or flow_summary %}")
    a = insert_after(a, h22, "2.3. {{ flow_title }}")
    a = insert_after(a, body_p, "{%p for x in flow_summary %}")
    a = insert_after(a, body_p, "{{ x }}")
    a = insert_after(a, body_p, "{%p endfor %}")
    a = insert_after(a, body_p, "{%p for f in flows %}")
    a = insert_after(a, body_p, "«{{ f.name }}» ({{ f.tc }}-xxx) — {{ f.status }}")
    a = insert_after(a, body_p, "{%p if f.info %}")
    a = insert_after(a, body_p, "{{ f.info }}")
    a = insert_after(a, body_p, "{%p endif %}")
    a = insert_after(a, body_p, "{%p if f.cleanup %}")
    a = insert_after(a, body_p, "Цэвэрлэгээ: {{ f.cleanup }}")
    a = insert_after(a, body_p, "{%p endif %}")
    a = insert_after(a, body_p, "{%p for fs in f.steps %}")
    a = insert_after(a, bullet, "{{ fs }}")
    a = insert_after(a, body_p, "{%p endfor %}")
    a = insert_after(a, body_p, "{%p endfor %}")
    insert_after(a, body_p, "{%p endif %}")

    # --- 3.1 Ерөнхий үр дүн -----------------------------------------------
    kpi = T[11].rows[1].cells
    for c, v in zip(kpi, ("s.total", "s.pass", "s.fail", "s.blocked")):
        set_cell(c, "{{ " + v + " }}")
    st = T[12]
    rows = [("s.total", "100%"), ("s.pass", "{{ s.pass_pct }}%"), ("s.fail", "{{ s.fail_pct }}%"),
            ("s.blocked", "{{ s.blocked_pct }}%"), ("s.pending", "{{ s.pending_pct }}%")]
    for i, (cnt, pct) in enumerate(rows, start=1):
        set_cell(st.rows[i].cells[1], "{{ " + cnt + " }}")
        set_cell(st.rows[i].cells[2], pct)
    set_cell(st.rows[6].cells[2], "{{ s.pass_rate }}% — {{ pass_rate_verdict }} (≥95% байх ёстой)")

    # --- 3.2 Эрэмбийн задаргаа --------------------------------------------
    pt = T[13]
    for i, key in enumerate(("critical", "major", "minor"), start=1):
        for j, f in enumerate(("total", "pass", "fail", "blocked"), start=1):
            set_cell(pt.rows[i].cells[j], "{{ pr." + key + "." + f + " }}")

    # --- 4. Bug хүснэгт ----------------------------------------------------
    row_loop(T[14], 1, "for b in bugs", ["{{ b.id }}", "{{ b.desc }}", "{{ b.sev }}", "{{ b.status }}",
                                         "{{ b.tcs }}"])
    fixed = find_para(doc, "Fixed: [тоо]")
    set_runs(fixed, "Fixed: {{ st_fixed }}")
    set_runs(find_para(doc, "Open: [тоо]"), "Open: {{ st_open }}")
    set_runs(find_para(doc, "Closed: [тоо]"), "Closed: {{ st_closed }}")

    # --- 5. Тестийн явц ----------------------------------------------------
    row_loop(T[15], 1, "for e in progress", ["{{ e.date }}", "{{ e.activity }}", "{{ e.result }}",
                                             "{{ e.note }}"])

    # --- 6. Pass Rate ------------------------------------------------------
    set_runs(T[16].rows[0].cells[0].paragraphs[0],
             "Pass Rate = {{ s.pass }} / {{ s.total }} × 100 = {{ s.pass_rate }}%")
    cur = find_para(doc, "Одоогийн байдал:")
    expect(len(cur.runs) == 4, "6-р бүлгийн 'Одоогийн байдал' мөрийн run-ийн тоо өөрчлөгдсөн")
    set_runs(cur, "Одоогийн байдал: ", "{{ s.pass_rate }}% ", "→ ", "{{ pass_rate_verdict_long }}")
    unplaceholder(cur)

    # --- 7. GO / NO-GO -----------------------------------------------------
    g = T[17]
    for i, key in enumerate(("pass_rate", "critical", "major", "blocked", "report", "all_executed",
                             "regression"), start=1):
        set_cell(g.rows[i].cells[2], "{{ go." + key + " }}")
    dec = T[18].rows[0].cells[0]
    set_runs(dec.paragraphs[0], "ШИЙДВЭР: {{ decision }}")
    set_runs(dec.paragraphs[1], "Шалтгаан: {{ decision_reason }}")
    list_loop(doc, "7.1. Дараагийн алхам", "x", "next_steps")
    set_cell(T[20].rows[0].cells[1], "{{ next_meeting }}")

    # --- 8. Зөвлөмж --------------------------------------------------------
    list_loop(doc, "8.1. Техникийн зөвлөмж", "x", "tech_recs")
    list_loop(doc, "8.2. Процессын сайжруулалт", "x", "process_recs")
    list_loop(doc, "8.3. Дараагийн спринтэд", "x", "sprint_recs")

    # --- 9. Баталгаажуулалт: гарын үсгийн хэсэг хоосон үлдэнэ ---------------
    set_runs(T[22].rows[0].cells[0].paragraphs[0], "Эцсийн шийдвэр: {{ final_decision }}")

    # --- Хавсралт А (тохиргоогоор) -----------------------------------------
    footer_line = find_para(doc, "Smart Software Group — Test Report Template")
    set_runs(footer_line, "Smart Software Group — Test Report {{ version }}  |  SSG-QA-TR")
    h1 = find_para(doc, "9. БАТАЛГААЖУУЛАЛТ")
    h2 = find_para(doc, "8.1. Техникийн зөвлөмж")
    body = find_para(doc, "Энэхүү Test Report нь")

    def ins(src_p, text):
        new = copy.deepcopy(src_p._p)
        footer_line._p.addprevious(new)
        from docx.text.paragraph import Paragraph
        np = Paragraph(new, footer_line._parent)
        set_runs(np, text)
        return np

    ins(body, "{%p if appendix %}")
    ap_h1 = ins(h1, "ХАВСРАЛТ А. BUG-ИЙН ДЭЛГЭРЭНГҮЙ")
    # хавсралтыг шинэ хуудсаас эхлүүлнэ
    pPr = ap_h1._p.get_or_add_pPr()
    pb = pPr.makeelement(qn("w:pageBreakBefore"), {})
    pPr.append(pb)
    ins(body, "{%p for b in bugs_detail %}")
    ins(h2, "{{ b.id }} — {{ b.title }}")
    ins(body, "Эрэмбэ: {{ b.sev }}  |  Модуль: {{ b.modules }}  |  Статус: {{ b.status }}  |  {{ b.confirmed }}")
    ins(body, "Холбогдох TC: {{ b.tcs }}")
    ins(body, "Давтах алхам: {{ b.steps }}")
    ins(body, "Хүлээгдэж буй үр дүн: {{ b.expected }}")
    ins(body, "Бодит үр дүн: {{ b.actual }}")
    ins(body, "Консолын stack trace (эхний 5 мөр): {{ b.stack }}")
    ins(body, "Засах санал: {{ b.fix }}")
    ins(body, "{%p if b.security %}")
    ins(body, "{{ b.security }}")
    ins(body, "{%p endif %}")
    ins(body, "{%p if b.image %}")
    ins(body, "{{ b.image }}")
    ins(body, "{%p endif %}")
    ins(body, "{%p endfor %}")
    ins(body, "{%p endif %}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dst))
    return dst


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else SRC,
                Path(sys.argv[2]) if len(sys.argv) > 2 else DST)
    print(f"Үүслээ: {out}")
