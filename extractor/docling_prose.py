"""
docling_prose.py — textspec.py's prose reading on Docling's text instead of pdftotext's.

pdftotext -layout puts TI's two columns side by side on one line, which is why textspec has to
cut every line into x-position "column streams" before any pattern runs. Docling's layout model
already emits each paragraph and bullet as its own item in reading order, so each item is a
stream as it stands — the interleave never happens. The patterns, sentence splitting and
tie-breaks are textspec.scan(), unchanged.
"""
import json
import re
from pathlib import Path

from extractor.docling_tables import fix_symbol_pua
import textspec


def _texts(cache: Path, last=None):
    for js in sorted(cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json")):
        if last and int(js.name[1:5]) > last:
            break
        doc = json.loads(js.read_text(encoding="utf-8"))
        for t in doc.get("texts", []):
            page = (t.get("prov") or [{}])[0].get("page_no")
            if page and (not last or page <= last):
                yield page, fix_symbol_pua(t.get("text") or "")


def run_together(text: str) -> bool:
    """Docling sometimes drops the spaces between words on TI's tight kerning
    ("Inputvoltagerange:1.8Vto5.5V", TPS63020 p1; the TPS2378 pin table). Patterns expect spaces."""
    t = text.strip()
    return len(t) >= 20 and t.count(" ") / len(t) < 0.06


def extract(cache: Path, pages: int = 2, pdf: Path | None = None) -> dict:
    items = [(p, text) for p, text in _texts(cache, pages) if text.strip()]
    streams = [text for _, text in items]
    # Where Docling ran words together, read that page's prose from pypdf too, which keeps the
    # spaces. Only those pages, so the column-clean Docling text still carries everything else.
    bad_pages = sorted({p for p, text in items if run_together(text)})
    if bad_pages and pdf is not None:
        import logging
        from pypdf import PdfReader
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        reader = PdfReader(str(pdf))
        for p in bad_pages:
            try:
                streams += [ln for ln in (reader.pages[p - 1].extract_text() or "").splitlines()
                            if ln.strip()]
            except Exception:                                   # noqa: BLE001
                continue
    whole = "\n".join(text for _, text in _texts(cache))
    return textspec.scan(streams, bool(textspec._REGISTER_HEADING.search(whole)))
