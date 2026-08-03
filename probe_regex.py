"""Debug: run each spec pattern against a PDF's corpus and show what matches."""
import sys, re
import pdfplumber
from extractor.device_info import _SPEC_RES, _parse_bullets, _norm_val

pdf_path = sys.argv[1]

with pdfplumber.open(pdf_path) as pdf:
    pages_text = [pdf.pages[i].extract_text() or "" for i in range(min(3, len(pdf.pages)))]

p1_text  = pages_text[0]
all_text = "\n".join(pages_text)

features     = _parse_bullets(p1_text, "1 Features", "3 Description")
title_lines  = [ln.strip() for ln in p1_text.splitlines() if ln.strip()]
title        = title_lines[0] if title_lines else ""
corpus       = title + "\n" + "\n".join(features) + "\n" + all_text[:4000]

print(f"Features count: {len(features)}")
print(f"Corpus length : {len(corpus)}")
print()

for name, pattern in _SPEC_RES:
    m = pattern.search(corpus)
    if m:
        raw = repr(m.group(0))[:80]
        print(f"MATCH  {name!r:25s}  raw={raw}  groups={m.groups()}")
    else:
        print(f"no     {name!r:25s}")
