"""
For pages with register bit-field tables, show the text that precedes each table
to understand how register name/address headings are formatted.
"""
import sys
import re
import pdfplumber

pdf_path = sys.argv[1] if len(sys.argv) > 1 else "datasheets/bq25798.pdf"
START_PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 59
END_PAGE   = int(sys.argv[3]) if len(sys.argv) > 3 else 65

BIT_HEADERS = {"bit", "field", "type", "reset"}

with pdfplumber.open(pdf_path) as pdf:
    for i in range(START_PAGE - 1, min(END_PAGE, len(pdf.pages))):
        page = pdf.pages[i]
        text = page.extract_text() or ""
        tables = page.extract_tables()
        has_reg_table = any(
            len(tbl) > 1 and
            sum(1 for h in tbl[0] if str(h or "").lower().strip() in BIT_HEADERS) >= 2
            for tbl in tables
        )
        if has_reg_table:
            print(f"\n=== PAGE {i+1} ===")
            # Print first 15 lines of the page text
            for line in text.splitlines()[:15]:
                print(repr(line))
