"""Тайлангийн шалгалт: LibreOffice headless recalculation + placeholder үлдээгүй эсэх."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from openpyxl import load_workbook


class VerificationError(Exception):
    pass


# Windows-д LibreOffice/poppler суулгагч PATH-д нэмдэггүй тул стандарт байршлыг мөн хайна
_WIN_SOFFICE = [Path(os.environ.get(v, "")) / "LibreOffice" / "program" / "soffice.exe"
                for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)]


def soffice_bin() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return next((str(p) for p in _WIN_SOFFICE if p.is_file()), None)


def pdftotext_bin() -> str | None:
    found = shutil.which("pdftotext")
    if found or os.name != "nt":
        return found
    # winget (oschwartz10612.Poppler) нь WinGet/Packages/oschwartz10612.Poppler_*/poppler-*/Library/bin-д задална
    pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    hits = sorted(pkgs.glob("oschwartz10612.Poppler_*/poppler-*/Library/bin/pdftotext.exe")) if pkgs.is_dir() else []
    return str(hits[-1]) if hits else None


def _run_soffice(args: list[str], timeout: int = 180) -> None:
    exe = soffice_bin()
    if not exe:
        raise VerificationError("LibreOffice (soffice) олдсонгүй — шалгалт хийх боломжгүй")
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        env = dict(os.environ, HOME=os.environ.get("HOME", profile))
        subprocess.run([exe, "--headless", "--norestore", f"-env:UserInstallation={Path(profile).as_uri()}", *args],
                       check=True, capture_output=True, timeout=timeout, env=env)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def recalc_copy(xlsx: Path) -> Path:
    """LibreOffice-оор нээж бүх томьёог тооцоолоод ТУСДАА хуулбар болгон хадгална.
    (Хүлээлгэн өгөх файлыг LibreOffice-оор дахин хадгалахгүй — emoji, DV, стиль хөндөгдөхгүй.)"""
    outdir = Path(tempfile.mkdtemp(prefix="recalc_"))
    src = outdir / "in.xlsx"
    shutil.copyfile(xlsx, src)
    _run_soffice(["--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(outdir / "out"), str(src)])
    out = outdir / "out" / "in.xlsx"
    if not out.exists():
        raise VerificationError("LibreOffice recalculation амжилтгүй")
    return out


def _norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).replace(",", ".").strip()


def verify_xlsx(xlsx: Path, expected: dict) -> dict:
    """P4, R4, S4, T4, U4, V4 (+ Bug Tracker A4) утгыг Python-ы тооцоотой тулгана. Зөрвөл алдаа."""
    rc = recalc_copy(xlsx)
    try:
        wb = load_workbook(rc, data_only=True)
        ws = wb["📋 Test Cases"]
        got = {k: ws[k].value for k in ("P4", "R4", "S4", "T4", "U4", "V4")}
        got["BUG_A4"] = wb["🐛 Bug Tracker"]["A4"].value
        got["BUG_C4"] = wb["🐛 Bug Tracker"]["C4"].value
        dash = wb["📊 Dashboard"]
        got["DASH_B6"] = dash["B6"].value
    finally:
        shutil.rmtree(rc.parent.parent, ignore_errors=True)
    mismatches = []
    for k in ("P4", "R4", "S4", "T4", "U4", "V4", "BUG_A4", "BUG_C4"):
        if _norm(got.get(k)) != _norm(expected.get(k)):
            mismatches.append(f"{k}: xlsx={got.get(k)!r} ≠ python={expected.get(k)!r}")
    if _norm(got.get("DASH_B6")) != _norm(expected.get("P4")):
        mismatches.append(f"Dashboard B6: {got.get('DASH_B6')!r} ≠ {expected.get('P4')!r}")
    if mismatches:
        raise VerificationError("xlsx тулгалт зөрлөө: " + "; ".join(mismatches))
    return got


# ---------------------------------------------------------------- docx
_BRACKET = re.compile(r"\[[^\[\]\n]{1,120}\]")


def docx_text(docx: Path) -> str:
    """docx → PDF (LibreOffice) → текст."""
    outdir = Path(tempfile.mkdtemp(prefix="docx2pdf_"))
    try:
        src = outdir / "in.docx"
        shutil.copyfile(docx, src)
        _run_soffice(["--convert-to", "pdf", "--outdir", str(outdir), str(src)])
        pdf = outdir / "in.pdf"
        if not pdf.exists():
            raise VerificationError("docx → PDF хөрвүүлэлт амжилтгүй")
        pdftotext = pdftotext_bin()
        if pdftotext:
            txt = outdir / "in.txt"
            subprocess.run([pdftotext, "-layout", str(pdf), str(txt)], check=True, timeout=60)
            return txt.read_text(encoding="utf-8", errors="replace")
        from pypdf import PdfReader  # fallback
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf)).pages)
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def template_placeholders(template_docx: Path) -> set[str]:
    """Загвар дахь бүх `[...]` placeholder-ийг (толгой, хөл орно) цуглуулна."""
    from docx import Document
    d = Document(str(template_docx))
    texts = []
    for p in d.paragraphs:
        texts.append(p.text)
    for t in d.tables:
        for row in t.rows:
            for c in row.cells:
                texts.append(c.text)
    for s in d.sections:
        for part in (s.header, s.footer):
            texts.extend(p.text for p in part.paragraphs)
    found = set()
    for t in texts:
        found.update(_BRACKET.findall(t))
    return found


def verify_docx(docx: Path, template_docx: Path, use_pdf: bool = True) -> list[str]:
    text = docx_text(docx) if use_pdf else _plain_docx_text(docx)
    flat = re.sub(r"\s+", " ", text)
    problems = []
    for marker in ("{{", "}}", "{%", "%}", "?]"):
        if marker in flat:
            problems.append(f"бөглөгдөөгүй тэмдэгт: {marker}")
    for ph in template_placeholders(template_docx):
        if re.sub(r"\s+", " ", ph) in flat:
            problems.append(f"загварын placeholder үлдсэн: {ph}")
    if problems:
        raise VerificationError("docx шалгалт: " + "; ".join(sorted(set(problems))))
    return problems


def _plain_docx_text(docx: Path) -> str:
    from docx import Document
    d = Document(str(docx))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.extend(c.text for c in row.cells)
    for s in d.sections:
        parts.extend(p.text for p in s.header.paragraphs)
    return "\n".join(parts)
