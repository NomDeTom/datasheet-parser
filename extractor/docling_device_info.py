"""
docling_device_info.py — the page-1 reading (part, doc id, title, packages, features, key
values) on Docling's output, through the same rules as the pdfplumber path
(device_info.device_info_from), so the two can be compared on equal terms.

What Docling changes about the input, and why it should help:

  * reading order — pdfplumber's page-1 text interleaves TI's Features and Description columns
    ("Vout echoed Vin on 5/5 TPS552xx"); Docling's layout model emits each column whole
  * the package table arrives as a real grid
  * page headers/footers are kept (they carry TI's literature number, SLVSxxx), because the text is
    built from the JSON — the Markdown export drops them as furniture

Bullets are rejoined with "•" so the features splitter sees what it expects.
"""
import json
import re
from pathlib import Path

from extractor.device_info import DeviceInfo, device_info_from
from extractor.docling_tables import fix_symbol_pua


_PART = re.compile(r"^[A-Za-z]{1,6}[-_]?\d{2,}[A-Za-z0-9\-_/,. x]*$")
_NOT_PART = re.compile(r"^(www\.|http|page|rev|figure|table|\d)", re.I)


def _pages(cache: Path, last: int = 3):
    """{page: [text lines in JSON order]} and {page: [table grids]} for pages 1..last."""
    texts, tables = {}, {}
    for js in sorted(cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json")):
        if int(js.name[1:5]) > last:
            break
        doc = json.loads(js.read_text(encoding="utf-8"))
        for t in doc.get("texts", []):
            page = (t.get("prov") or [{}])[0].get("page_no")
            if page and page <= last:
                line = fix_symbol_pua(t.get("text") or "").strip()
                if t.get("label") == "list_item" and line and not line.startswith("•"):
                    line = "• " + line
                texts.setdefault(page, []).append(line)
        for tb in doc.get("tables", []):
            page = (tb.get("prov") or [{}])[0].get("page_no")
            if page and page <= last:
                grid = [[fix_symbol_pua(" ".join((c.get("text") or "").split())) for c in row]
                        for row in (tb.get("data") or {}).get("grid") or []]
                tables.setdefault(page, []).append(grid)
    return texts, tables


def extract(cache: Path) -> DeviceInfo | None:
    texts, tables = _pages(cache)
    if 1 not in texts:
        return None
    # device_info_from takes line 0 as the part number, which is right for pdfplumber's text (the
    # big part-number banner comes first) but not for Docling's, which starts with whatever block
    # was read first — a company name, "Features", a page header. Put the first short line that
    # looks like a part number at the top.
    p1 = texts[1]
    pn = next((i for i, ln in enumerate(p1[:25])
               if len(ln) <= 30 and _PART.match(ln) and not _NOT_PART.match(ln)), None)
    if pn:
        p1.insert(0, p1.pop(pn))
    pages_text = ["\n".join(texts.get(p, [])) for p in (1, 2, 3) if p in texts]
    return device_info_from(pages_text, tables.get(1, []))
