#!/usr/bin/env python
"""
compare_variant.py — whole-document comparison of a tuning variant against the production cache.

    python eval/compare_variant.py --list pdfs.txt --tag tune-pdfium

For each PDF, reads output/docling/<sha16>/ (the setting in production) and
output/docling/<sha16>-<tag>/ (the variant, converted with docling_extract.py --cache-tag) and
reports, per document and in total:

  rows       spec rows each reading typed (extractor.docling_tables)
  agree      rows with the same page and values in both; of those, how many carry the same symbol
  only       rows only one reading has, and how many of those pypdf's text of the same page
             supports (every value of the row appears on the page) — the witness for who is right
  registers  registers / with an address / fields (extractor.docling_registers)
  run-tog.   share of text items read without their word spaces

There is no ground truth here, only agreement and a text-layer witness; the benchmark
(tune_docling.py) is the ground truth. Needs only the parser's own venv.
"""
import argparse
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from extractor import docling_registers                             # noqa: E402
from extractor.docling_prose import run_together                    # noqa: E402
from extractor.docling_tables import extract as docling_rows        # noqa: E402

OUT = HERE / "output" / "docling"


def fmt(v):
    return None if v is None else f"{v:g}"


def vkey(r):
    return (r["page"], fmt(r["min"]), fmt(r["typ"]), fmt(r["max"]))


def sym(r):
    return re.sub(r"[^a-z0-9]", "", (r["symbol"] or r["parameter"] or "").lower())


def witnessed(r, text):
    vals = [v for v in (fmt(r["min"]), fmt(r["typ"]), fmt(r["max"])) if v is not None]
    return bool(vals) and all(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", text) for v in vals)


def together(cache):
    items = tog = 0
    for js in cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json"):
        for t in json.loads(js.read_text()).get("texts", []):
            if t.get("label") not in ("page_header", "page_footer") and (t.get("text") or "").strip():
                items += 1
                tog += run_together(t["text"])
    return items, tog


def regs(cache):
    r = docling_registers.extract(cache)
    return len(r), sum(x.address != "?" for x in r), sum(len(x.fields) for x in r)


def compare(pdf, tag):
    from pypdf import PdfReader
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]
    a_dir, b_dir = OUT / sha, OUT / f"{sha}-{tag}"
    if not (a_dir / "manifest.json").exists() or not (b_dir / "manifest.json").exists():
        return None
    reader = PdfReader(str(pdf))
    text = {}

    def page(p):
        if p not in text:
            try:
                text[p] = reader.pages[p - 1].extract_text() or ""
            except Exception:
                text[p] = ""
        return text[p]

    a, _ = docling_rows(a_dir)
    b, _ = docling_rows(b_dir)
    ca, cb = Counter(map(vkey, a)), Counter(map(vkey, b))
    common = ca & cb
    same_sym = 0
    for k in common:
        sa = Counter(sym(r) for r in a if vkey(r) == k)
        sb = Counter(sym(r) for r in b if vkey(r) == k)
        same_sym += sum((sa & sb).values())
    only_a = [r for r in a if (ca - cb)[vkey(r)] > 0]
    only_b = [r for r in b if (cb - ca)[vkey(r)] > 0]
    ia, ta = together(a_dir)
    ib, tb = together(b_dir)
    return {
        "doc": pdf.stem, "rows": (len(a), len(b)), "agree": sum(common.values()), "same_sym": same_sym,
        "only": (sum((ca - cb).values()), sum((cb - ca).values())),
        "witness": (sum(witnessed(r, page(r["page"])) for r in only_a),
                    sum(witnessed(r, page(r["page"])) for r in only_b)),
        "regs": (regs(a_dir), regs(b_dir)),
        "tog": (100 * ta / max(ia, 1), 100 * tb / max(ib, 1)),
        "examples": ([(r["page"], r["symbol"], fmt(r["min"]), fmt(r["typ"]), fmt(r["max"]), r["unit"]) for r in only_a[:4]],
                     [(r["page"], r["symbol"], fmt(r["min"]), fmt(r["typ"]), fmt(r["max"]), r["unit"]) for r in only_b[:4]]),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", required=True, help="text file, one PDF path per line")
    ap.add_argument("--tag", required=True, help="variant cache tag, e.g. tune-pdfium")
    args = ap.parse_args()
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    pdfs = [Path(x.strip()) for x in Path(args.list).read_text().splitlines() if x.strip()]
    res = [r for r in (compare(p, args.tag) for p in pdfs) if r]
    lines = ["| document | rows base / variant | agree (same symbol) | only base (text-backed) | only variant (text-backed) "
             "| registers base | registers variant | run-tog. base | run-tog. variant |", "|" + "---|" * 9]
    tot = Counter()
    for r in res:
        (ra, rb), (oa, ob), (wa, wb) = r["rows"], r["only"], r["witness"]
        lines.append(f"| {r['doc'][:40]} | {ra} / {rb} | {r['agree']} ({r['same_sym']}) | {oa} ({wa}) | {ob} ({wb}) "
                     f"| {'/'.join(map(str, r['regs'][0]))} | {'/'.join(map(str, r['regs'][1]))} "
                     f"| {r['tog'][0]:.1f} % | {r['tog'][1]:.1f} % |")
        tot.update({"ra": ra, "rb": rb, "agree": r["agree"], "same": r["same_sym"], "oa": oa, "ob": ob, "wa": wa, "wb": wb})
    lines.append(f"| **total** | {tot['ra']} / {tot['rb']} | {tot['agree']} ({tot['same']}) | {tot['oa']} ({tot['wa']}) "
                 f"| {tot['ob']} ({tot['wb']}) | | | | |")
    print("\n".join(lines))
    print(f"\n{len(res)} of {len(pdfs)} documents had both caches\n\nexamples (page, symbol, min, typ, max, unit):")
    for r in res:
        print(f"- {r['doc']}: only base {r['examples'][0]}; only variant {r['examples'][1]}")


if __name__ == "__main__":
    main()
