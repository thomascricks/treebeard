# SKILL: pdf (knowledge skill -- library + doc, not a door service)

Library: pypdf (read/merge/split/extract), reportlab (create).
Install (host venv or room image): pip install pypdf reportlab

READ / EXTRACT
    from pypdf import PdfReader
    r = PdfReader("in.pdf")
    text = "\n".join(page.extract_text() or "" for page in r.pages)

MERGE / SPLIT
    from pypdf import PdfWriter, PdfReader
    w = PdfWriter()
    for src in ("a.pdf", "b.pdf"):
        for page in PdfReader(src).pages:
            w.add_page(page)
    with open("out.pdf.tmp", "wb") as f: w.write(f)
    import os; os.replace("out.pdf.tmp", "out.pdf")

CREATE
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas("out.pdf.tmp", pagesize=letter)
    c.setFont("Helvetica", 11)
    y = 750
    for line in lines:
        c.drawString(72, y, line); y -= 14
        if y < 72: c.showPage(); c.setFont("Helvetica", 11); y = 750
    c.save()
    import os; os.replace("out.pdf.tmp", "out.pdf")

DISCIPLINES
- temp-then-rename on every output (the wall clock can kill mid-write).
- extract_text() returns None on image-only pages; guard it. There is no
  OCR in this stack; an image-only PDF is a CANT, not a retry.
- Ship a test_*.py: assert page count, assert a known string extracts.
