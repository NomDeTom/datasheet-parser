#!/usr/bin/env python
"""
verify_library.py — check the vault's sidecars and cards; non-zero exit on any failure.

    python verify_library.py
    python verify_library.py --min-registers 415      # fail if the register total drops below

Failures:
  * a PDF without its `.text.md` or `.data.json`, or (non-paper) without its card
  * a `.text.md` whose page markers don't match the PDF's page count
  * a `.data.json` with the wrong schema, a confidence outside high/medium/low/none, a value
    with no page and no external file behind it (verified values excepted: a person is their
    source), or an impossible range (min > max)
  * two notes whose names differ only in case: Obsidian resolves [[links]] by name, so one of
    them becomes unreachable (e.g. Topics/IP5310.md vs Library/IP5310/IP5310.md)
  * an enriched document whose export readings no longer match the export file they cite (the
    export was refreshed and enrichment not re-run)
  * fewer registers in total than --min-registers
Warnings (reported, never fatal):
  * sidecars larger than their PDF
"""
import argparse
import json
import re
import sys
from pathlib import Path

import enrichment
import vaultpath

LEVELS = {"high", "medium", "low", "none"}
SKIP = ("SUPERSEDED - ", "DUPLICATE - ")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault")
    ap.add_argument("--min-registers", type=int, default=0)
    args = ap.parse_args()
    root = vaultpath.root_of(vaultpath.find_vault(args.vault))
    pdfs = [p for pat in ("*.pdf", "*.PDF") for p in root.rglob(pat)]
    pdfs = [p for p in vaultpath.dedupe(sorted(pdfs))
            if not p.name.startswith(SKIP) and "Projects" not in p.parts
            and "Import files" not in p.parts]

    fail, warn, regs, checked_values = {}, [], 0, 0
    exports = {}
    for folder in (root / "Parametrics", root / "Reference Material" / "TI Parametrics"):
        exports.update({e.file: e for e in enrichment.load_exports(folder)})
    names = {}
    for md in root.rglob("*.md"):
        if md.name.endswith(".text.md") or ".obsidian" in md.parts:
            continue
        names.setdefault(md.stem.lower(), []).append(md.relative_to(root).as_posix())
    add = lambda k, v: fail.setdefault(k, []).append(v)
    for pdf in pdfs:
        text, data_f, card = (pdf.with_name(pdf.stem + s) for s in (".text.md", ".data.json", ".md"))
        name = pdf.relative_to(root).as_posix()
        if not text.exists():
            add("missing .text.md", name)
        if not data_f.exists():
            add("missing .data.json", name)
            continue
        data = json.loads(data_f.read_text(encoding="utf-8"))
        if data.get("schema") != "autonotes.data/2":
            add("wrong schema", name)
        paper = data.get("document", {}).get("doc_type") == "paper"
        if not paper and not card.exists():
            add("missing card", name)
        if text.exists():
            marks = len(re.findall(r"^=== PAGE \d+ ===$", text.read_text(encoding="utf-8"), re.M))
            if marks != data["document"].get("pages"):
                add("page markers != pages", f"{name} ({marks} vs {data['document'].get('pages')})")
        for p in data.get("params", []):
            if p.get("confidence") not in LEVELS:
                add("bad confidence", f"{name} param {p.get('key')}")
            external = any(r.get("file") for r in p.get("readings", []))
            if p.get("page") is None and not p.get("verified") and not external:
                add("value without page or source file", f"{name} param {p.get('key')}")
            if p.get("min") is not None and p.get("max") is not None and p["min"] > p["max"]:
                add("impossible range", f"{name} {p['key']} {p['min']} > {p['max']}")
        ti = (data.get("enrichment") or {}).get("ti_export")
        if ti:
            exp = exports.get(ti.get("file"))
            if exp is None:
                add("enrichment cites a missing export", f"{name}: {ti.get('file')}")
            else:
                row = exp.rows.get(enrichment.norm(ti.get("part")))
                if row is None or enrichment.export_values(row) != ti.get("values"):
                    add("enrichment out of date with its export", f"{name}: {ti.get('file')}")
                else:
                    checked_values += len(ti.get("values", {}))
        for r in data.get("spec_rows", []):
            if r.get("confidence") not in LEVELS:
                add("bad confidence", f"{name} row p{r.get('page')}")
            if not r.get("page"):
                add("value without page", f"{name} row {r.get('symbol') or r.get('parameter')}")
        regs += len(data.get("registers", []))
        side = data_f.stat().st_size + (text.stat().st_size if text.exists() else 0)
        if side > pdf.stat().st_size:
            warn.append((side / pdf.stat().st_size, name))

    for same in (v for v in names.values() if len(v) > 1):
        add("note names that differ only in case", " ≠ ".join(same))
    if args.min_registers and regs < args.min_registers:
        add("registers below baseline", f"{regs} < {args.min_registers}")

    print(f"{len(pdfs)} PDFs, {regs} registers, {checked_values} enriched values match their export")
    for k, v in fail.items():
        print(f"FAIL {k}: {len(v)}")
        for x in v[:10]:
            print(f"    {x}")
        if len(v) > 10:
            print(f"    … {len(v) - 10} more")
    if warn:
        warn.sort(reverse=True)
        print(f"warning: {len(warn)} sidecar set(s) larger than their PDF, largest:")
        for r, n in warn[:5]:
            print(f"    {r:5.2f}x  {n}")
    if not fail:
        print("[ok] all checks passed")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
