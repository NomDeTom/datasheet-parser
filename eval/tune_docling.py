#!/usr/bin/env python
"""
tune_docling.py — score Docling settings against pages whose right answer is known.

    python eval/tune_docling.py                        # every variant that needs no download
    python eval/tune_docling.py --variants base,pdfium  # a subset
    python eval/tune_docling.py --docs tps62861,tps2378 # a subset of the benchmark
    python eval/tune_docling.py --no-convert            # score caches already made
    python eval/tune_docling.py --allow-download        # include variants whose models aren't cached

The benchmark (eval/benchmark.json) is a handful of pages from the library whose answers were
read off the rendered page by a person: table rows, phrases that must be findable, register maps.
Each variant converts only those pages, into its own cache (output/docling/<sha>-tune-<variant>/,
via docling_extract.py --variant --cache-tag), so the real cache is never touched and a variant
that is interrupted resumes. Scores:

  rows       expected table rows found with the right values and unit (docling_tables.extract)
  find       phrases present, correctly spaced, in the page text or its figure text
  run-tog.   share of text items Docling read without their word spaces (lower is better)
  vs pypdf   share of Docling's words that pypdf does not have on the same page (lower is better)
  registers  registers / with an address / fields, per register document
  s/page     total conversion seconds / total pages (includes each process's model load); peak RSS

Needs DOCLING_PYTHON (the venv with Docling) like docling_extract.py. Results are printed and
written to output/tune/<timestamp>.md. Nothing here changes the library.
"""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import vaultpath                                                    # noqa: E402
from extractor import docling_registers                             # noqa: E402
from extractor.docling_prose import run_together                    # noqa: E402
from extractor.docling_tables import extract as docling_rows, norm_unit  # noqa: E402
from sidecars import _tokens, docling_page_extras                   # noqa: E402

OUT = HERE / "output"
BENCH = Path(__file__).resolve().parent / "benchmark.json"

# name -> (docling_extract --variant settings, extra CLI args, needs a model download?)
VARIANTS = {
    "base":         ({}, [], False),
    "pdfium":       ({"backend": "pypdfium2"}, [], False),
    "parse_v2":     ({"backend": "docling_parse_v2"}, [], False),
    "backend_text": ({"force_backend_text": True}, [], False),
    "no_cellmatch": ({"do_cell_matching": False}, [], False),
    "fast_tables":  ({"table_mode": "fast"}, ["--table-mode", "fast"], False),
    "scale2":       ({"images_scale": 2.0}, [], False),
    "egret_medium": ({"layout": "DOCLING_LAYOUT_EGRET_MEDIUM"}, [], True),
    "egret_large":  ({"layout": "DOCLING_LAYOUT_EGRET_LARGE"}, [], True),
}


def nrm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pages_of(spec):
    a, _, b = spec.partition("-")
    return range(int(a), int(b or a) + 1)


def cache_for(pdf, variant):
    return OUT / "docling" / f"{hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]}-tune-{variant}"


def convert(pdf, spec, variant, log):
    settings, extra, _ = VARIANTS[variant]
    settings = {k: v for k, v in settings.items() if k != "table_mode"}
    cmd = [sys.executable, str(HERE / "docling_extract.py"), str(pdf), "--pages", spec,
           "--cache-tag", f"tune-{variant}", "--chunks-per-process", "0", *extra]
    if settings:
        cmd += ["--variant", json.dumps(settings)]
    env = dict(os.environ, HF_HUB_OFFLINE=os.environ.get("HF_HUB_OFFLINE", "1"),
               TMPDIR=os.environ.get("TMPDIR", "/var/tmp"), PYTHONWARNINGS="ignore")
    log.write(f"$ {' '.join(cmd)}\n")
    log.flush()
    return subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env).returncode


def close(a, b):
    return a is not None and b is not None and math.isclose(a, b, rel_tol=0.005, abs_tol=1e-9)


def score(pdf, entry, cache):
    s = {"rows": [0, 0], "find": [0, 0], "items": 0, "together": 0, "miss": [], "regs": None,
         "s_per_page": [], "rss": 0, "failed": []}
    m = cache / "manifest.json"
    if m.exists():
        man = json.loads(m.read_text())
        for name, c in man.get("chunks", {}).items():
            if str(c.get("status", "")).startswith("error"):
                s["failed"].append(c["status"][:80])
            a, b = (int(x) for x in re.findall(r"\d+", name)[:2])
            s["s_per_page"].append((c.get("seconds") or 0, b - a + 1))
            s["rss"] = max(s["rss"], c.get("peak_rss_mb") or 0)
    pages = {}
    for f in cache.glob("*.pages.json"):
        pages.update({int(k): v for k, v in json.loads(f.read_text()).items()})
    want = set(pages_of(entry["pages"]))

    rows, _ = docling_rows(cache) if cache.is_dir() else ([], 0)
    for exp in entry.get("rows", []):
        s["rows"][1] += 1
        ok = any(r["page"] == exp["page"] and nrm(r["symbol"] or r["parameter"]).startswith(nrm(exp["symbol"]))
                 and all(close(r.get(k), exp[k]) for k in ("min", "typ", "max") if k in exp)
                 and ("unit" not in exp or norm_unit(r.get("unit")) == norm_unit(exp["unit"]))
                 for r in rows)
        s["rows"][0] += ok
        if not ok:
            s["failed"].append(f"row p{exp['page']} {exp['symbol']} {exp}")

    figures, _ = docling_page_extras(cache) if cache.is_dir() else ({}, set())
    for exp in entry.get("find", []):
        s["find"][1] += 1
        hay = " ".join((pages.get(exp["page"], "") + " " + " ".join(figures.get(exp["page"], []))).split()).lower()
        ok = " ".join(exp["text"].split()).lower() in hay
        s["find"][0] += ok
        if not ok:
            s["failed"].append(f"find p{exp['page']} {exp['text']!r}")

    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(str(pdf))
    for js in cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json"):
        for t in json.loads(js.read_text()).get("texts", []):
            page = (t.get("prov") or [{}])[0].get("page_no")
            if page in want and t.get("label") not in ("page_header", "page_footer") and (t.get("text") or "").strip():
                s["items"] += 1
                s["together"] += run_together(t["text"])
    for p in sorted(want & set(pages)):
        b = _tokens(pages[p])
        if len(b) >= 30:
            a = _tokens(reader.pages[p - 1].extract_text() or "")
            s["miss"].append(1 - len(a & b) / len(b))

    if "registers" in entry and cache.is_dir():
        regs = docling_registers.extract(cache)
        s["regs"] = (len(regs), sum(r.address != "?" for r in regs), sum(len(r.fields) for r in regs))
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault")
    ap.add_argument("--variants", default="")
    ap.add_argument("--docs", default="")
    ap.add_argument("--no-convert", action="store_true")
    ap.add_argument("--allow-download", action="store_true",
                    help="include variants whose models are not cached yet (fetched from the network)")
    args = ap.parse_args()
    root = vaultpath.root_of(vaultpath.find_vault(args.vault))
    bench = json.loads(BENCH.read_text())["documents"]
    if args.docs:
        bench = [e for e in bench if e["doc"] in args.docs.split(",")]
    names = args.variants.split(",") if args.variants else \
        [n for n, (_, _, dl) in VARIANTS.items() if args.allow_download or not dl]
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        ap.error(f"unknown variant(s): {unknown}; known: {list(VARIANTS)}")
    (OUT / "tune").mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = open(OUT / "tune" / f"run-{stamp}.log", "w")

    results = {}
    for v in names:
        agg = {"rows": [0, 0], "find": [0, 0], "items": 0, "together": 0, "miss": [], "regs": {},
               "s_per_page": [], "rss": 0, "failed": []}
        for e in bench:
            pdf = root / "Library" / e["doc"] / f"{e['doc']}.pdf"
            if not pdf.exists():
                agg["failed"].append(f"{e['doc']}: PDF not in the library")
                continue
            if not args.no_convert:
                print(f"[{v}] converting {e['doc']} pp {e['pages']}", flush=True)
                convert(pdf, e["pages"], v, log)
            s = score(pdf, e, cache_for(pdf, v))
            for k in ("rows", "find"):
                agg[k][0] += s[k][0]
                agg[k][1] += s[k][1]
            agg["items"] += s["items"]
            agg["together"] += s["together"]
            agg["miss"] += s["miss"]
            agg["s_per_page"] += s["s_per_page"]
            agg["rss"] = max(agg["rss"], s["rss"])
            agg["failed"] += [f"{e['doc']}: {x}" for x in s["failed"]]
            if s["regs"] is not None:
                agg["regs"][e["doc"]] = (s["regs"], e["registers"])
        results[v] = agg

    reg_docs = [e["doc"] for e in bench if "registers" in e]
    head = ["variant", "rows", "find", "run-tog.", "vs pypdf", *reg_docs, "s/page", "peak MB"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for v, a in results.items():
        regs = []
        for d in reg_docs:
            if d in a["regs"]:
                (n, addr, fields), exp = a["regs"][d]
                regs.append(f"{n} ({addr} addr) {fields} f / want {exp['expect_registers']}, ≥{exp['min_fields']} f")
            else:
                regs.append("—")
        sp = a["s_per_page"]
        lines.append("| " + " | ".join([
            v, f"{a['rows'][0]}/{a['rows'][1]}", f"{a['find'][0]}/{a['find'][1]}",
            f"{100 * a['together'] / max(a['items'], 1):.1f} %",
            f"{100 * sum(a['miss']) / max(len(a['miss']), 1):.1f} %", *regs,
            f"{sum(t for t, _ in sp) / max(sum(n for _, n in sp), 1):.1f}" if sp else "—", f"{a['rss']:.0f}" if a["rss"] else "—"]) + " |")
    report = "\n".join(lines)
    details = "\n".join(f"- **{v}**: " + "; ".join(a["failed"][:12]) for v, a in results.items() if a["failed"])
    out = OUT / "tune" / f"results-{stamp}.md"
    out.write_text(f"# Docling tuning — {stamp}\n\n{report}\n\n## Misses\n\n{details or '(none)'}\n")
    print("\n" + report + "\n\nmisses:\n" + (details or "(none)") + f"\n\nwritten to {out}")


if __name__ == "__main__":
    main()
