"""
Quick structural probe: show table headers and sample rows from every page
that has a table with register-like content.
"""
import sys
import pdfplumber

pdf_path = sys.argv[1] if len(sys.argv) > 1 else "datasheets/bq25798.pdf"

KEYWORDS = {"bit", "field", "register", "addr", "address", "offset",
            "r/w", "rw", "reset", "default", "access", "name"}

with pdfplumber.open(pdf_path) as pdf:
    print(f"Pages: {len(pdf.pages)}")
    found = 0
    for i, page in enumerate(pdf.pages, 1):
        tables = page.extract_tables()
        for t, tbl in enumerate(tables):
            if not tbl or len(tbl) < 2:
                continue
            headers = [str(h or "").lower().strip() for h in tbl[0]]
            hits = sum(any(k in h for k in KEYWORDS) for h in headers)
            if hits >= 2:
                found += 1
                print(f"\n--- page {i}, table {t} ---")
                print("HEADERS:", tbl[0])
                for row in tbl[1:4]:   # first 3 data rows
                    print("  ROW:", row)
    print(f"\nTotal matching tables: {found}")
