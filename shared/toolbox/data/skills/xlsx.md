# SKILL: xlsx (knowledge skill -- library + doc, not a door service)

Library: openpyxl.
Install (host venv or room image): pip install openpyxl

READ
    from openpyxl import load_workbook
    wb = load_workbook("in.xlsx", data_only=True)  # data_only: values,
    ws = wb.active                                 # not formula strings
    rows = [[c.value for c in row] for row in ws.iter_rows()]

WRITE
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "data"
    ws.append(["name", "count"])          # header row
    for name, count in items: ws.append([name, count])
    wb.save("out.xlsx.tmp")
    import os; os.replace("out.xlsx.tmp", "out.xlsx")

FORMULAS / FORMAT
    ws["C2"] = "=A2*B2"                   # written as text; Excel computes
    ws.column_dimensions["A"].width = 24
    from openpyxl.styles import Font
    for c in ws[1]: c.font = Font(bold=True)

DISCIPLINES
- data_only=True reads cached values; a file never opened by Excel has
  none. Compute in Python when the number must be real.
- temp-then-rename on save; .xlsx is a zip and a torn zip is garbage.
- Ship a test_*.py: reload the written file, assert cell values.
