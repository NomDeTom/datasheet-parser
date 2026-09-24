#!/usr/bin/env python
"""
compare_readers.py — pdfplumber vs Docling, measured on this library.

    python eval/compare_readers.py [--vault …]

Three comparisons, each only over documents Docling has fully converted:

  registers    per document: registers, registers with an address, fields, and fields whose
               bits and name agree between the two readers
  device info  page-1 Vin/Vout/Iout from each reader against the vendor export (TI parametric
               selector), which is the closest thing to ground truth available: exact (within 2 %),
               wrong, or missing
  spec rows    Docling's typed rows against pdfplumber's on the same pages: rows found by both,
               and of those, rows whose min/typ/max agree

Evaluation only; changes nothing.
"""
import argparse
import dataclasses
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import vaultpath                                              # noqa: E402
from enrichment import load_exports, match, export_values, norm  # noqa: E402
from extractor import docling_device_info, docling_prose, docling_registers  # noqa: E402
from extractor.docling_tables import extract as docling_rows   # noqa: E402
from fuse import value_of                                       # noqa: E402

OUT = HERE / "output"


def cache_for(pdf):
    return OUT / "docling" / hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]


def complete(pdf, data):
    c = cache_for(pdf)
    if not c.is_dir():
        return None
    got = sum(int(f.name[6:10]) - int(f.name[1:5]) + 1 for f in c.glob("p*-*.pages.json"))
    return c if got >= data["document"]["pages"] else None


def close(a, b):
    return a is not None and b is not None and abs(a - b) <= max(0.02 * max(abs(a), abs(b)), 1e-6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault")
    args = ap.parse_args()
    root = vaultpath.root_of(vaultpath.find_vault(args.vault))
    exports = load_exports(root / "Parametrics")
    docs = []
    for dj in sorted((root / "Library").rglob("*.data.json")):
        data = json.loads(dj.read_text(encoding="utf-8"))
        pdf = dj.with_name(dj.name[: -len(".data.json")] + ".pdf")
        if pdf.exists():
            docs.append((pdf, data, complete(pdf, data)))
    print(f"{sum(1 for *_, c in docs if c)} of {len(docs)} documents fully converted by Docling\n")

    # ── registers ─────────────────────────────────────────────────────────────
    print("REGISTERS")
    tot = Counter()
    for pdf, data, cache in docs:
        pl = data.get("registers") or []
        if not pl or not cache:
            continue
        dl = docling_registers.as_dicts(docling_registers.extract(cache))
        key = lambda f: (re.sub(r"\s", "", f["bits"]), re.sub(r"\s", "", f["name"]))
        pf = Counter(key(f) for r in pl for f in r["fields"])
        df = Counter(key(f) for r in dl for f in r["fields"])
        same = sum((pf & df).values())
        row = dict(p_regs=len(pl), p_addr=sum(r["address"] != "?" for r in pl),
                   p_fields=sum(pf.values()), d_regs=len(dl),
                   d_addr=sum(r["address"] != "?" for r in dl), d_fields=sum(df.values()),
                   same=same)
        tot.update(row)
        print(f"  {pdf.stem:18} pdfplumber {row['p_regs']:3} regs ({row['p_addr']:3} addressed) "
              f"{row['p_fields']:4} fields | docling {row['d_regs']:3} ({row['d_addr']:3}) "
              f"{row['d_fields']:4} | fields agreeing {same}")
    if tot:
        print(f"  TOTAL pdfplumber {tot['p_regs']} regs ({tot['p_addr']} addressed) {tot['p_fields']} "
              f"fields | docling {tot['d_regs']} ({tot['d_addr']}) {tot['d_fields']} | "
              f"agreeing {tot['same']}")

    # ── device info vs the vendor export ─────────────────────────────────────
    print("\nDEVICE INFO (page 1) vs vendor export")
    score = {"pdfplumber": Counter(), "docling": Counter()}
    fields = {"vin_min": "V", "vin_max": "V", "vout_min": "V", "vout_max": "V", "iout_max": "A"}
    for pdf, data, cache in docs:
        if not cache:
            continue
        exp, rec = match(exports, [norm(pdf.stem), norm(data["document"].get("original_name", ""))])
        if not rec:
            continue
        truth = export_values(rec)
        pinfo = OUT / pdf.stem / "device_info.json"
        readers = {"pdfplumber": json.loads(pinfo.read_text()) if pinfo.exists() else {},
                   "docling": dataclasses.asdict(docling_device_info.extract(cache) or
                                                 docling_device_info.DeviceInfo())}
        for f, unit in fields.items():
            t = truth.get(f)
            if t is None:
                continue
            for name, info in readers.items():
                v = value_of(info.get(f), unit)
                score[name]["exact" if close(v, t) else "missing" if v is None else "wrong"] += 1
    for name, c in score.items():
        n = sum(c.values())
        if n:
            print(f"  {name:10} exact {c['exact']:3}  wrong {c['wrong']:3}  missing {c['missing']:3}"
                  f"   (of {n} export values)")

    # ── prose claims (textspec) vs the vendor export ─────────────────────────
    print("\nPROSE CLAIMS (textspec: pdftotext vs Docling text) vs vendor export")
    pscore = {"pdftotext": Counter(), "docling": Counter()}
    for pdf, data, cache in docs:
        if not cache:
            continue
        exp, rec = match(exports, [norm(pdf.stem), norm(data["document"].get("original_name", ""))])
        if not rec:
            continue
        truth = export_values(rec)
        tcache = OUT / pdf.stem / "textspec.json"
        readers = {"pdftotext": json.loads(tcache.read_text()) if tcache.exists() else {},
                   "docling": docling_prose.extract(cache, pdf=pdf)}
        for f in fields:
            t = truth.get(f)
            if t is None:
                continue
            for name, claims in readers.items():
                v = (claims.get(f) or {}).get("value") if isinstance(claims.get(f), dict) else None
                pscore[name]["exact" if close(v, t) else "missing" if v is None else "wrong"] += 1
    for name, c in pscore.items():
        n = sum(c.values())
        answered = c["exact"] + c["wrong"]
        if n:
            print(f"  {name:10} exact {c['exact']:3}  wrong {c['wrong']:3}  missing {c['missing']:3}"
                  f"   precision {c['exact'] / max(answered, 1):.0%}  recall {c['exact'] / n:.0%}"
                  f"   (of {n} export values)")

    # ── spec rows ─────────────────────────────────────────────────────────────
    print("\nSPEC ROWS on pages both readers covered")
    agg = Counter()
    for pdf, data, cache in docs:
        if not cache:
            continue
        prows = [r for r in data.get("spec_rows", []) if r.get("extractor") in ("pdfplumber", "docling+pdfplumber")]
        if not prows:
            continue
        drows, _ = docling_rows(cache)
        ident = lambda r: (r["page"], re.sub(r"[^a-z0-9]", "", (r.get("symbol") or r.get("parameter") or "").lower())[:20])
        dmap = {}
        for r in drows:
            dmap.setdefault(ident(r), []).append(r)
        both = agree = 0
        for r in prows:
            cands = dmap.get(ident(r))
            if cands:
                both += 1
                agree += any(all(c[k] == r[k] for k in ("min", "typ", "max")) for c in cands)
        agg.update(p=len(prows), d=len(drows), both=both, agree=agree)
        if pdf.stem == "bq24074" or len(prows) > 40:
            print(f"  {pdf.stem:18} pdfplumber {len(prows):4} rows  docling {len(drows):4}  "
                  f"matched {both:4}  values agree {agree:4}")
    if agg:
        print(f"  TOTAL pdfplumber {agg['p']} rows, docling {agg['d']}; pdfplumber rows Docling also "
              f"found {agg['both']} ({agg['both'] / max(agg['p'], 1):.0%}), values agree on "
              f"{agg['agree']} ({agg['agree'] / max(agg['both'], 1):.0%} of matched)")


if __name__ == "__main__":
    main()
