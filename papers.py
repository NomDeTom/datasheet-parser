#!/usr/bin/env python
"""Research papers: inventory, page-marked text cache, outline, skeleton note, page render.

    papers.py inventory [DIR|PDF...]        one block per PDF: pages, backend, DOI/arXiv, title
    papers.py extract   [DIR|PDF...] [-o DIR]  full text; default output/<stem>/text.txt
                                            (no paths = everything in input/papers/)
    papers.py outline   PDF                 section headings with page numbers
    papers.py skeleton  PDF [-o note.md]    note header + abstract + outline, ready for the reading pass
    papers.py render    PDF PAGE [-o png]   rasterise one page (equations, figures, scans)

The sibling of trm.py for two-column journal PDFs. It writes the same `=== PAGE n ===` markers
to the same cache path, so `trm.py find <pdf> PATTERN` works on a paper once it is extracted.

Reading is extractor/paper_source.py: Docling when docling_extract.py has converted the paper
(layout-model reading order, labelled title, headings with pages), otherwise pypdfium2 reading
each page's left half then right half. Rendering is pypdfium2. In the vault, a paper's full text
and its metadata (document.paper in .data.json) are written by sidecars.py from the same reader.
Nothing in this file interprets the paper — that is the reading pass described in
.claude/skills/paper-importer.
"""
import argparse
import re
import sys
from pathlib import Path

from extractor import paper_source as ps


def pdfs(paths):
    for p in paths or [Path("input") / "papers"]:
        p = Path(p)
        if p.is_dir():
            yield from sorted(q for q in p.iterdir() if q.suffix.lower() == ".pdf")
        else:
            yield p


def cmd_inventory(a):
    for p in pdfs(a.paths):
        paper = ps.read(p)
        doi, arx = ps.ids(paper)
        layer = "text" if len(paper.text) / max(len(paper.pages), 1) > 200 else "SCANNED"
        print(f"{p.name}\n  pages={len(paper.pages)} {layer} read-by={paper.backend} "
              f"year={ps.year(paper)} doi={doi} arxiv={arx}\n  title={ps.title(paper)}")


def cmd_extract(a):
    outdir = Path(a.out) if a.out else None
    for p in pdfs(a.paths):
        paper = ps.read(p)
        # -o DIR puts <stem>.txt beside notes; the default matches trm.py's cache.
        target = (outdir / (p.stem + ".txt")) if outdir else Path("output") / p.stem / "text.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(paper.text, encoding="utf-8")
        print(f"{target}  ({len(paper.pages)} pages, {paper.backend})")


def cmd_outline(a):
    for page, label, title in ps.outline(ps.read(a.pdf)):
        indent = "  " if re.fullmatch(r"[A-H]|\d+\.\d+(\.\d+)?", label or "") else ""
        print(f"{indent}{label or '':>4}  {title}  (p{page})")


def cmd_skeleton(a):
    paper = ps.read(a.pdf)
    md = ps.metadata(paper)
    src = f"doi:{md['doi']}" if md["doi"] else (f"arXiv:{md['arxiv']}" if md["arxiv"] else "—")
    lines = [
        f"# {md['title'] or Path(a.pdf).stem}",
        "",
        f"**Authors:** {md['authors'] or '—'}",
        f"**Venue:** {md['venue'] or '—'} ({md['year'] or '—'})",
        f"**Source:** {src} · [[{Path(a.pdf).name}]] ({len(paper.pages)} pp)",
        "**Read on:** —",
        "**Relevance:** —",
        "",
        "---",
        "",
        "## Abstract (as published)",
        "",
        md["abstract"] or "—",
        "",
        "## What it claims",
        "",
        "## Numbers and models worth lifting",
        "",
        "## Outline",
        "",
    ]
    lines += [f"- {o['label'] + '. ' if o['label'] else ''}{o['title']} (p{o['page']})"
              for o in md["outline"]]
    body = "\n".join(lines) + "\n"
    if a.out:
        Path(a.out).write_text(body, encoding="utf-8")
        print(a.out)
    else:
        sys.stdout.write(body)


def cmd_render(a):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(a.pdf))
    image = doc[a.page - 1].render(scale=a.dpi / 72).to_pil()
    out = a.out or f"{Path(a.pdf).stem}-p{a.page}.png"
    image.save(out)
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
