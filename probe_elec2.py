"""Show page text for pages containing electrical characteristics tables."""
import sys, re
import pdfplumber

pdf_path = sys.argv[1]
START = int(sys.argv[2]) if len(sys.argv) > 2 else 1
END   = int(sys.argv[3]) if len(sys.argv) > 3 else 999

ELEC_KEYWORDS = re.compile(
    r"\b(typ|min|max|unit|condition|parameter|symbol|test|spec|limit)\b",
    re.IGNORECASE,
)

with pdfplumber.open(pdf_path) as pdf:
    for i in range(START-1, min(END, len(pdf.pages))):
        page = pdf.pages[i]
        text = page.extract_text() or ""
        tables = page.extract_tables()
        has_elec = any(
            tbl and len(tbl) > 1 and
            len(ELEC_KEYWORDS.findall(" ".join(str(h or "") for h in tbl[0]))) >= 2
            for tbl in tables
        )
        if has_elec:
            print(f"\n=== PAGE {i+1} ===")
            for line in text.splitlines()[:8]:
                print(repr(line))
