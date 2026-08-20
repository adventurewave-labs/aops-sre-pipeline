#!/usr/bin/env python3
"""Convert the A.O.P.S. UAT Markdown report to a styled PDF.

Uses Python's `markdown` (with tables + fenced_code extensions) to render the
Markdown to HTML, then wraps it in a styled HTML document and uses
`wkhtmltopdf` if available, otherwise falls back to a Chromium-headless
render via Playwright. As a final fallback, uses ReportLab Platypus to
build the PDF from the structured content.
"""
import os
import sys
import subprocess
import shutil

MD_PATH = "/home/z/my-project/download/AOPS-UAT-Report.md"
PDF_PATH = "/home/z/my-project/download/AOPS-UAT-Report.pdf"

import markdown

with open(MD_PATH) as f:
    md_text = f.read()

html_body = markdown.markdown(
    md_text,
    extensions=["tables", "fenced_code", "codehilite", "toc", "sane_lists"],
)

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body { font-family: 'DejaVu Sans', 'Noto Sans SC', sans-serif;
       font-size: 10.5pt; line-height: 1.5; color: #1a1d21; }
h1 { font-size: 22pt; color: #1a1d21; border-bottom: 3px solid #5d3bf2;
     padding-bottom: 6px; margin-top: 22pt; }
h2 { font-size: 15pt; color: #2c2f36; border-bottom: 1px solid #c0c0c0;
     padding-bottom: 3px; margin-top: 18pt; }
h3 { font-size: 12pt; color: #2c2f36; margin-top: 14pt; }
p  { margin: 6pt 0; }
ul, ol { margin: 4pt 0 8pt 18pt; padding: 0; }
li { margin: 2pt 0; }
table { border-collapse: collapse; margin: 8pt 0; width: 100%;
        font-size: 9.5pt; }
th, td { border: 1px solid #c0c0c0; padding: 4pt 6pt; text-align: left;
         vertical-align: top; }
th { background: #f4f4f8; font-weight: 700; }
tr:nth-child(even) { background: #fafafa; }
code { font-family: 'DejaVu Sans Mono', 'Sarasa Mono SC', monospace;
       font-size: 9pt; background: #f4f4f8; padding: 1px 3px;
       border-radius: 3px; }
pre { background: #1a1d21; color: #d0d0d0; padding: 8pt 10pt;
      border-radius: 5px; overflow-x: auto; font-size: 8.5pt;
      line-height: 1.4; }
pre code { background: transparent; color: inherit; padding: 0;
           font-size: 8.5pt; }
strong { color: #1a1d21; font-weight: 700; }
hr { border: 0; border-top: 1px solid #c0c0c0; margin: 14pt 0; }
"""

html_doc = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>A.O.P.S. UAT Report</title>
<style>{CSS}</style>
</head><body>
{html_body}
</body></html>"""

# Save the HTML for inspection
with open("/tmp/aops-uat-report.html", "w") as f:
    f.write(html_doc)

# Try conversion methods in order
print("Trying PDF conversion methods...")

# Method 1: wkhtmltopdf (if installed)
if shutil.which("wkhtmltopdf"):
    print("  using wkhtmltopdf")
    rc = subprocess.run(["wkhtmltopdf", "--enable-local-file-access",
                          "/tmp/aops-uat-report.html", PDF_PATH],
                         capture_output=True)
    if rc.returncode == 0:
        print(f"  PDF written to {PDF_PATH}")
        sys.exit(0)
    print(f"  wkhtmltopdf failed: {rc.stderr.decode(errors='replace')[:300]}")

# Method 2: weasyprint (if installed)
try:
    import weasyprint
    print("  using weasyprint")
    weasyprint.HTML(string=html_doc).write_pdf(PDF_PATH)
    print(f"  PDF written to {PDF_PATH}")
    sys.exit(0)
except ImportError:
    pass

# Method 3: Playwright (Chromium headless)
try:
    from playwright.sync_api import sync_playwright
    print("  using playwright")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html_doc, wait_until="load")
        page.pdf(path=PDF_PATH, format="A4",
                  margin={"top":"18mm","bottom":"18mm","left":"16mm","right":"16mm"})
        browser.close()
    print(f"  PDF written to {PDF_PATH}")
    sys.exit(0)
except ImportError:
    pass

# Method 4: ReportLab fallback — build a basic PDF
print("  falling back to ReportLab (basic layout)")
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                  Table, TableStyle, Preformatted, PageBreak)
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT

doc = SimpleDocTemplate(PDF_PATH, pagesize=A4,
                         topMargin=18*mm, bottomMargin=18*mm,
                         leftMargin=16*mm, rightMargin=16*mm)
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="Mono", fontName="Courier",
                           fontSize=8, leading=10, textColor=colors.HexColor("#1a1d21")))
styles.add(ParagraphStyle(name="Body2", fontName="Helvetica",
                           fontSize=10, leading=14, textColor=colors.HexColor("#1a1d21")))

story = []
in_table = False
current_table_rows = []
current_table_headers = []

def flush_table():
    global current_table_rows, current_table_headers
    if not current_table_rows:
        return
    data = [current_table_headers] + current_table_rows if current_table_headers else current_table_rows
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f4f4f8")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.HexColor("#1a1d21")),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME", (0,1), (-1,-1), "Helvetica"),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#c0c0c0")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]))
    story.append(t)
    story.append(Spacer(1, 6))
    current_table_rows = []
    current_table_headers = []

# Very rough MD->reportlab translation: strip markdown and dump as plain paragraphs.
# Skip tables (just include as preformatted text).
import re
# Remove HTML table elements that markdown produced
md_no_html = re.sub(r"<[^>]+>", "", md_text)
for line in md_no_html.split("\n"):
    if line.startswith("# "):
        story.append(Paragraph(line[2:].strip(), styles["Heading1"]))
        story.append(Spacer(1, 4))
    elif line.startswith("## "):
        story.append(Paragraph(line[3:].strip(), styles["Heading2"]))
        story.append(Spacer(1, 3))
    elif line.startswith("### "):
        story.append(Paragraph(line[4:].strip(), styles["Heading3"]))
        story.append(Spacer(1, 2))
    elif line.startswith("```"):
        continue
    elif line.startswith("|"):
        # Tables: just render as monospace lines
        story.append(Preformatted(line, styles["Mono"]))
    elif line.strip() == "":
        story.append(Spacer(1, 4))
    else:
        story.append(Paragraph(line.strip(), styles["Body2"]))

doc.build(story)
print(f"  PDF written to {PDF_PATH}")
