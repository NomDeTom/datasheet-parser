"""
Probe first N pages of a PDF — show raw text so we can see what
key device info (features, package, specs) looks like and where it lives.
"""
import sys
import pdfplumber

pdf_path = sys.argv[1]
pages    = int(sys.argv[2]) if len(sys.argv) > 2 else 4

with pdfplumber.open(pdf_path) as pdf:
    for i in range(min(pages, len(pdf.pages))):
        page = pdf.pages[i]
        text = page.extract_text() or ""
        tables = page.extract_tables()
        print(f"\n{'='*70}")
        print(f"PAGE {i+1}  ({len(tables)} table(s))")
        print('='*70)
        for line in text.splitlines():
            print(repr(line))
