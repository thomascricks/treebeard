# SKILL: pptx (knowledge skill -- library + doc, not a door service)

Library: python-pptx.
Install (host venv or room image): pip install python-pptx

CREATE
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])  # title+content
    slide.shapes.title.text = "Title"
    body = slide.placeholders[1].text_frame
    body.text = "first line"
    p = body.add_paragraph(); p.text = "second line"; p.level = 1
    prs.save("out.pptx.tmp")
    import os; os.replace("out.pptx.tmp", "out.pptx")

READ / EDIT
    prs = Presentation("in.pptx")
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs)

DISCIPLINES
- Layout indexes vary by template; discover placeholders by iterating
  slide.placeholders and reading .placeholder_format.idx, do not guess.
- temp-then-rename on save; .pptx is a zip, same torn-zip failure as xlsx.
- Ship a test_*.py: reopen the deck, assert slide count and title text.
