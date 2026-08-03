"""Show all tables detected on page 1 of each PDF."""
import sys
import pdfplumber
from pathlib import Path

paths = sys.argv[1:] if len(sys.argv) > 1 else list(Path("datasheets").glob("*.pdf"))

for pdf_path in paths:
    print(f"\n{'='*60}")
    print(f"  {Path(pdf_path).name}")
    print('='*60)
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        for i, tbl in enumerate(page.extract_tables()):
            if not tbl:
                continue
            print(f"  Table {i}: headers={tbl[0]}")
            for row in tbl[1:5]:
                print(f"    {row}")
