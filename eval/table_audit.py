#!/usr/bin/env python
"""
table_audit.py — find Docling tables that lost text-layer content, per table, without models.

    python eval/table_audit.py --vault ~/autonotes-v2/AutoNotes            # whole library
    python eval/table_audit.py --list pdfs.txt --out audit.tsv
    python eval/table_audit.py x.pdf --min-lost 5 --show 3

TableFormer's MatchingPostProcessor drops PDF cells that fit no row or column band of its grid
and logs only a count ("N of M pdf cells matched neither a row nor a column band ...") — no
page, no table. The dropped text is in no table cell and no other text item, but it is still in
the PDF's text layer. So, for every table in the production cache (output/docling/<sha16>/):

  letters and digits of the text layer inside the table's bbox   (pdfplumber, word centre inside)
  minus letters and digits of the table's cells                  (multiset, case-folded)
  = lost characters; lost share = lost / text-layer characters

Characters are compared, not words: the layer splits V<sub>DD</sub> into "V" and "DD" where a
cell has "VDD", and reading order or cell splits must not count as loss either. The sample
column lists layer words that occur nowhere in the table's cell text, as a hint of what went.
Rows are one table each, sorted by lost characters, so the worst tables lead the re-scan queue.
Needs only the parser's own venv. Read-only.
"""
import argparse
import glob
import hashlib
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import pdfplumber

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "output" / "docling"


def sha16(pdf: Path) -> str:
    h = hashlib.sha256()
    with open(pdf, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:16]


def chars(text: str) -> Counter:
    return Counter(ch for ch in text.casefold() if ch.isalnum())


def squash(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def tables_by_page(cache: Path) -> dict:
    """{page_no: [(table_id, bbox_topleft, cell_text, rows, cols)]} over every chunk."""
    out = {}
    for js in sorted(cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json")):
        doc = json.loads(js.read_text(encoding="utf-8"))
        heights = {int(k): v["size"]["height"] for k, v in doc.get("pages", {}).items()}
        for t in doc.get("tables", []):
            if not t.get("prov"):
                continue
            pr = t["prov"][0]
            p, bb = pr["page_no"], pr["bbox"]
            if bb.get("coord_origin") == "BOTTOMLEFT":
                h = heights[p]
                box = (bb["l"], h - bb["t"], bb["r"], h - bb["b"])
            else:
                box = (bb["l"], bb["t"], bb["r"], bb["b"])
            cells = t["data"].get("table_cells", [])
            ct = " ".join(c.get("text", "") for c in cells)
            out.setdefault(p, []).append((f"{js.stem}{t['self_ref'][1:]}", box, ct,
                                          t["data"].get("num_rows", 0), t["data"].get("num_cols", 0)))
    return out


def audit(pdf: Path, min_lost: int, pad: float = 1.0):
    cache = OUT / sha16(pdf)
    if not (cache / "manifest.json").exists():
        return None
    by_page = tables_by_page(cache)
    rows = []
    with pdfplumber.open(pdf) as doc:
        for p in sorted(by_page):
            if p > len(doc.pages):
                continue
            page = doc.pages[p - 1]
            try:
                words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            finally:
                page.close()
            for tid, (x0, y0, x1, y1), ct, nr, nc in by_page[p]:
                inside = [w["text"] for w in words
                          if x0 - pad <= (w["x0"] + w["x1"]) / 2 <= x1 + pad
                          and y0 - pad <= (w["top"] + w["bottom"]) / 2 <= y1 + pad]
                lc = chars(" ".join(inside))
                n_lost, n_layer = sum((lc - chars(ct)).values()), sum(lc.values())
                cell_sq = squash(ct)
                gone = [w for w in inside if squash(w) and squash(w) not in cell_sq]
                if n_lost >= min_lost:
                    rows.append({"pdf": pdf, "page": p, "table": tid, "grid": f"{nr}x{nc}",
                                 "layer_chars": n_layer, "lost": n_lost,
                                 "share": n_lost / n_layer if n_layer else 0.0,
                                 "sample": " ".join(gone[:12]),
                                 "bbox": f"{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}"})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="*", type=Path)
    ap.add_argument("--list", type=Path, help="one PDF path per line")
    ap.add_argument("--vault", type=Path, help="audit every PDF under <vault>/Library")
    ap.add_argument("--min-lost", type=int, default=10, help="report tables losing at least N characters")
    ap.add_argument("--out", type=Path, help="write the full table list as TSV")
    ap.add_argument("--show", type=int, default=25, help="print the worst N tables")
    args = ap.parse_args()
    logging.getLogger("pdfminer").setLevel(logging.ERROR)

    pdfs = list(args.pdfs)
    if args.list:
        pdfs += [Path(l.strip()) for l in args.list.read_text().splitlines() if l.strip()]
    if args.vault:
        pdfs += sorted(Path(p) for p in glob.glob(str(args.vault / "Library" / "**" / "*.pdf"), recursive=True))
    if not pdfs:
        ap.error("no PDFs given")

    allrows, per_doc, skipped = [], [], []
    for i, pdf in enumerate(pdfs, 1):
        try:
            rows = audit(pdf, args.min_lost)
        except Exception as exc:  # one unreadable PDF must not stop the audit
            print(f"[{i}/{len(pdfs)}] {pdf.name}: error {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped.append(pdf)
            continue
        if rows is None:
            skipped.append(pdf)
            continue
        allrows += rows
        per_doc.append((pdf.name, len(rows), sum(r["lost"] for r in rows)))
        print(f"[{i}/{len(pdfs)}] {pdf.name}: {len(rows)} table(s), {sum(r['lost'] for r in rows)} chars lost",
              file=sys.stderr, flush=True)

    allrows.sort(key=lambda r: (-r["lost"], -r["share"]))
    if args.out:
        cols = ["pdf", "page", "table", "grid", "layer_chars", "lost", "share", "bbox", "sample"]
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\t".join(cols) + "\n")
            for r in allrows:
                f.write("\t".join(f"{r[c]:.3f}" if c == "share" else str(r[c]) for c in cols) + "\n")

    print(f"\n{len(per_doc)} PDF(s) audited, {len(skipped)} without a cache or unreadable")
    print(f"{len(allrows)} table(s) losing >= {args.min_lost} characters, {sum(r['lost'] for r in allrows)} characters in all")
    for band, lo, hi in (("<10%", 0, .1), ("10-50%", .1, .5), (">=50%", .5, 1.01)):
        print(f"  lost share {band:7} {sum(1 for r in allrows if lo <= r['share'] < hi):5} table(s)")
    print(f"\nworst {min(args.show, len(allrows))}:")
    for r in allrows[:args.show]:
        print(f"  {r['lost']:5} ({r['share']:4.0%})  {r['pdf'].name[:40]:40} p{r['page']:<5} {r['grid']:>7}  {r['sample'][:60]}")


if __name__ == "__main__":
    main()
