"""Show ALL text lines + table count for a specific page."""
import sys
import pdfplumber

pdf_path = sys.argv[1]
page_num = int(sys.argv[2])

with pdfplumber.open(pdf_path) as pdf:
    page = pdf.pages[page_num - 1]
    text = page.extract_text() or ""
    tables = page.extract_tables()
    print(f"=== Page {page_num} | {len(tables)} table(s) ===")
    for line in text.splitlines():
        print(repr(line))
    print(f"\n--- Tables ---")
    for i, tbl in enumerate(tables):
        print(f"Table {i}: headers={tbl[0] if tbl else 'empty'}")
