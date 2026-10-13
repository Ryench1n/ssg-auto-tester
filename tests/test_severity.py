from app.runner.severity import Severity, XLSX_PRIORITIES


def test_xlsx_values_match_template_validation():
    import openpyxl
    from app.config import TEMPLATES_DIR
    wb = openpyxl.load_workbook(TEMPLATES_DIR / "07_Test-Cases-Загвар.xlsx")
    ws = wb["📋 Test Cases"]
    lists = [dv.formula1.strip('"').split(",") for dv in ws.data_validations.dataValidation]
    h_list = next(l for l in lists if any("Critical" in x for x in l))
    assert [s.xlsx for s in Severity] == h_list
    assert list(XLSX_PRIORITIES) == h_list


def test_docx_mapping():
    assert Severity.CRITICAL.p_level == "P1"
    assert Severity.MAJOR.docx == "P2 High"
    assert Severity.MINOR.docx == "P3 Medium"
    assert Severity.CRITICAL.rank < Severity.MAJOR.rank < Severity.MINOR.rank
