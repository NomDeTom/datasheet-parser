#!/usr/bin/env python
"""Research papers: inventory, page-marked text cache, outline, skeleton note, page render.

    papers.py inventory [DIR|PDF...]        one block per PDF: pages, text layer, DOI/arXiv, title
    papers.py extract   [DIR|PDF...] [-o DIR]  column-aware full text; default output/<stem>/text.txt
                                            (no paths = everything in input/papers/)
    papers.py outline   PDF                 numbered section headings with page numbers
    papers.py skeleton  PDF [-o note.md]    note header + abstract, ready for the reading pass
    papers.py render    PDF PAGE [-o png]   rasterise one page (figures, scanned pages)

The sibling of trm.py for two-column journal PDFs. It writes the same `=== PAGE n ===` markers
to the same cache path, so `trm.py find <pdf> PATTERN` works on a paper once it is extracted.
Backend is pymupdf (fast, column geometry available); pdfplumber is not used here. Nothing in
this file interprets the paper - that is the reading pass described in .claude/skills/paper-importer.
"""
import argparse
import os
import re
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:  # the fitz name on older wheels
    import fitz as pymupdf

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>|]+?)(?=[\s\"<>|,;)]|$)")
ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?)")
# Section headings as IEEE/Springer set them: "I. INTRODUCTION", "2 Related work", "A. Capture effect"
HEADING_RE = re.compile(
    r"^\s*(?:(?P<roman>[IVX]{1,5})\.|(?P<num>\d{1,2}(?:\.\d{1,2}){0,2})\.?|(?P<alpha>[A-H])\.)\s+"
    r"(?P<title>[A-Z][^\n]{2,80}?)\s*$"
)
STOP_HEADINGS = {"references", "acknowledgment", "acknowledgments", "acknowledgement", "acknowledgements"}


def pdfs(paths):
    for p in paths or [Path("input") / "papers"]:
        p = Path(p)
        if p.is_dir():
            yield from sorted(q for q in p.iterdir() if q.suffix.lower() == ".pdf")
        else:
            yield p


def page_text(page):
    """Column-aware reading order: blocks whose centre sits left of the page midline come first.

    Two-column journal layouts interleave if sorted purely by y. Full-width blocks (title,
    abstract on some templates, wide tables) are emitted with the left column at their y.
    """
    mid = page.rect.width / 2
    blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    left, right = [], []
    for x0, y0, x1, y1, text, *_ in blocks:
        wide = (x1 - x0) > 0.6 * page.rect.width
        ((left if (wide or (x0 + x1) / 2 < mid) else right)).append((y0, text))
    order = sorted(left) + sorted(right)
    t = "\n".join(text for _, text in order)
    t = t.replace("\u00ad", "")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)  # "inter-\nference" -> "interference"
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t


PAGE_MARKER = "=== PAGE %d ==="  # trm.py's convention, so its `find` works on a paper cache


def full_text(doc):
    return "".join(f"\n{PAGE_MARKER % (i + 1)}\n{page_text(p)}" for i, p in enumerate(doc))


def ids(text):
    doi = DOI_RE.search(text)
    arx = ARXIV_RE.search(text)
    return (doi.group(1).rstrip(".") if doi else None, arx.group(1) if arx else None)


def guess_title(doc):
    """Metadata title if it looks like one, else the largest-font run on page 1."""
    meta = (doc.metadata.get("title") or "").strip()
    if meta and not meta.lower().endswith(".pdf") and len(meta) > 12 and " " in meta:
        return meta
    spans = []
    for b in doc[0].get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s["text"].strip():
                    spans.append((round(s["size"], 1), s["text"].strip()))
    if not spans:
        return None
    big = max(sz for sz, _ in spans)
    return " ".join(t for sz, t in spans if sz == big)


def year_of(doc, text):
    m = re.search(r"D:(\d{4})", doc.metadata.get("creationDate") or "")
    if m:
        return m.group(1)
    m = re.search(r"\b(19|20)\d{2}\b", text[:3000])
    return m.group(0) if m else None


def abstract_of(text):
    m = re.search(r"Abstract\s*[—\-–:.]?\s*(.+?)(?=\n\s*(?:Index Terms|Keywords|I\.\s+INTRODUCTION|1\s+Introduction|1\.\s+Introduction))",
                  text, re.S | re.I)
    if not m:
        return None
    return re.sub(r"\s*\n\s*", " ", m.group(1)).strip()


def outline(doc):
    out, seen = [], set()
    for i, p in enumerate(doc):
        for line in page_text(p).splitlines():
            m = HEADING_RE.match(line)
            if not m:
                continue
            title = m.group("title").strip()
            if title.lower() in seen or len(title.split()) > 12:
                continue
            seen.add(title.lower())
            label = m.group("roman") or m.group("num") or m.group("alpha")
            out.append((i + 1, label, title))
            if title.lower() in STOP_HEADINGS:
                return out
    return out


def cmd_inventory(a):
    for p in pdfs(a.paths):
        doc = pymupdf.open(p)
        text = full_text(doc)
        doi, arx = ids(text)
        layer = "text" if len(text) / max(len(doc), 1) > 200 else "SCANNED"
        print(f"{p.name}\n  pages={len(doc)} {layer} year={year_of(doc, text)} doi={doi} arxiv={arx}\n  title={guess_title(doc)}")


def cmd_extract(a):
    outdir = Path(a.out) if a.out else None
    for p in pdfs(a.paths):
        doc = pymupdf.open(p)
        # -o DIR puts <stem>.txt beside notes in a vault; the default matches trm.py's cache.
        target = (outdir / (p.stem + ".txt")) if outdir else Path("output") / p.stem / "text.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(full_text(doc), encoding="utf-8")
        print(f"{target}  ({len(doc)} pages)")


def cmd_outline(a):
    doc = pymupdf.open(a.pdf)
    for page, label, title in outline(doc):
        indent = "  " if re.fullmatch(r"[A-H]|\d+\.\d+(\.\d+)?", label) else ""
        print(f"{indent}{label:>4}  {title}  (p{page})")


def cmd_skeleton(a):
    doc = pymupdf.open(a.pdf)
    text = full_text(doc)
    doi, arx = ids(text)
    src = f"doi:{doi}" if doi else (f"arXiv:{arx}" if arx else "—")
    lines = [
        f"# {guess_title(doc) or Path(a.pdf).stem}",
        "",
        f"**Authors:** —",
        f"**Venue:** {doc.metadata.get('subject') or '—'} ({year_of(doc, text) or '—'})",
        f"**Source:** {src} · [[{Path(a.pdf).stem}]] ({len(doc)} pp)",
        "**Read on:** —",
        "**Relevance:** —",
        "",
        "---",
        "",
        "## Abstract (as published)",
        "",
        abstract_of(text) or "—",
        "",
        "## What it claims",
        "",
        "## Numbers and models worth lifting",
        "",
        "## Outline",
        "",
    ]
    for page, label, title in outline(doc):
        lines.append(f"- {label}. {title} (p{page})")
    body = "\n".join(lines) + "\n"
    if a.out:
        Path(a.out).write_text(body, encoding="utf-8")
        print(a.out)
    else:
        sys.stdout.write(body)


def cmd_render(a):
    doc = pymupdf.open(a.pdf)
    pix = doc[a.page - 1].get_pixmap(dpi=a.dpi)
    out = a.out or f"{Path(a.pdf).stem}-p{a.page}.png"
    pix.save(out)
    print(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("inventory"); s.add_argument("paths", nargs="*"); s.set_defaults(f=cmd_inventory)
    s = sub.add_parser("extract"); s.add_argument("paths", nargs="*"); s.add_argument("-o", "--out"); s.set_defaults(f=cmd_extract)
    s = sub.add_parser("outline"); s.add_argument("pdf"); s.set_defaults(f=cmd_outline)
    s = sub.add_parser("skeleton"); s.add_argument("pdf"); s.add_argument("-o", "--out"); s.set_defaults(f=cmd_skeleton)
    s = sub.add_parser("render"); s.add_argument("pdf"); s.add_argument("page", type=int); s.add_argument("-o", "--out"); s.add_argument("--dpi", type=int, default=150); s.set_defaults(f=cmd_render)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
