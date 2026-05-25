"""
Probe electrical characteristics tables across PDFs.
Shows headers + first 3 rows for any table that looks like a specs table.
"""
import sys
import re
import pdfplumber

pdf_path = sys.argv[1]
MAX_PAGES = int(sys.argv[2]) if len(sys.argv) > 2 else 999

ELEC_KEYWORDS = re.compile(
    r"\b(typ|min|max|unit|condition|parameter|symbol|test|spec|limit|value|nominal)\b",
    re.IGNORECASE,
)

with pdfplumber.open(pdf_path) as pdf:
    print(f"Pages: {len(pdf.pages)}")
    found = 0
    for i, page in enumerate(pdf.pages, 1):
        if i > MAX_PAGES:
            break
        tables = page.extract_tables()
        for t, tbl in enumerate(tables):
            if not tbl or len(tbl) < 2:
                continue
            headers = [str(h or "").strip() for h in tbl[0]]
            header_text = " ".join(headers).lower()
            hits = len(ELEC_KEYWORDS.findall(header_text))
            if hits >= 2:
                found += 1
                print(f"\n--- page {i}, table {t} ---")
                print("HEADERS:", headers)
                for row in tbl[1:4]:
                    print("  ROW:", [str(c or "").replace("\n", " ")[:60] for c in row])
    print(f"\nTotal matching tables: {found}")
