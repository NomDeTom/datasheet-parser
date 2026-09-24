#!/usr/bin/env python
"""
sidecars.py — write the full-text and data sidecars beside every PDF in the vault.

    python sidecars.py                      # every PDF under the vault
    python sidecars.py --only SX126         # filename filter
    python sidecars.py --report             # sizes and coverage only, writes nothing

For each <stem>.pdf, beside it:

  <stem>.text.md    Full text for search and cross-checks. One `=== PAGE n ===` marker per page,
                    so `grep -n` hits convert to page numbers. Docling's Markdown (compact tables,
                    picture placeholders, no embedded images) where the Docling cache has the page;
                    pypdf text otherwise, marked. Symbol-font private-use glyphs are translated
                    (µ, Ω, ° … would otherwise be invisible).

  <stem>.data.json  Database-ready rows, schema `autonotes.data/1`:
                      document    identity (sha256, pages, type, parts, manufacturer, revision)
                      params      fused key parameters (vin/vout/iout/fsw) with confidence
                      spec_rows   typed table rows from Docling and pdfplumber, merged, with page
                                  and confidence (both extractors agree -> high)
                      registers   register maps with bit fields
                    Every value carries a page. Confidence is high / medium / low / none, the same
                    scale as fuse.py.

Inputs, all optional per PDF: the Docling cache (output/docling/<sha[:16]>/, from
docling_extract.py), the parse cache (output/<stem>/, from twin_notes.py --cache-only), and —
once, for migration — a legacy `.registers.json` beside the PDF. Enrichment tables (vendor
parametric exports, see enrichment.py) found in `Parametrics/` add readings. Values a person
verified — card property `verified: [vin, …]` — are snapshotted into `.data.json` and never
overwritten by extraction; a later disagreement is flagged instead.
"""
import argparse
import datetime
import hashlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import classify
import enrichment
import fuse
import vaultpath
import dataclasses

from extractor import docling_device_info, docling_prose, docling_registers
from extractor.docling_tables import extract as docling_rows, fix_symbol_pua, norm_unit, to_number

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"
SCHEMA = "autonotes.data/2"
PAGE_MARKER = "=== PAGE %d ==="
TWIN_SUFFIX = " (datasheet)"
SKIP_PREFIXES = ("SUPERSEDED - ", "DUPLICATE - ")
PARAM_KEYS = {"vin": ("vin_min", "vin_max", "V"), "vout": ("vout_min", "vout_max", "V"),
              "iout": (None, "iout_max", "A"), "fsw": ("freq_min_khz", "freq_max_khz", "kHz")}


def clean_text(s: str) -> str:
    """Join UTF-16 surrogate pairs some extractors emit for math-alphabet glyphs (U+1D400…);
    replace any half that has no partner, since it cannot be written as UTF-8."""
    return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parser_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                              capture_output=True, text=True).stdout.strip() or None
    except OSError:
        return None


def docling_version(cache: Path):
    m = cache / "manifest.json"
    return json.loads(m.read_text())["versions"]["docling"] if m.exists() else None


# ── text ─────────────────────────────────────────────────────────────────────
def pypdf_pages(pdf: Path) -> dict:
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # font-encoding chatter, not errors
    reader = PdfReader(str(pdf))
    out = {}
    for i, page in enumerate(reader.pages, 1):
        try:
            out[i] = page.extract_text() or ""
        except Exception:                                  # noqa: BLE001 — one bad page, not the file
            out[i] = ""
    return out


def docling_pages(cache: Path) -> dict:
    pages = {}
    for f in sorted(cache.glob("*.pages.json")):
        pages.update({int(k): v for k, v in json.loads(f.read_text(encoding="utf-8")).items()})
    return pages


_TAG = re.compile(r"(?<![\w\\&/])#(?=[^\s#\d])")


def obsidian_safe(text: str) -> str:
    """Stop extracted text becoming vault structure: `[[2:1]]` bit ranges would be links and
    `#define` would be a tag. Markdown escapes render the same characters literally."""
    text = text.replace("[[", "\\[\\[").replace("]]", "\\]\\]")
    return "\n".join(line if line.startswith("#") and re.match(r"#{1,6} ", line) else _TAG.sub("\\#", line)
                     for line in text.split("\n"))


_TOKEN = re.compile(r"[a-z0-9µ]+(?:\.[0-9]+)?")
DISAGREE = 0.30      # share of Docling's tokens that pypdf does not have; ~ the worst 1-2 % of pages


def _tokens(text: str) -> set:
    text = re.sub(r"<!--.*?-->", " ", fix_symbol_pua(text or "").lower())
    return {w for w in _TOKEN.findall(text) if len(w) > 1}


def readers_disagree(docling_md: str, pypdf_text: str) -> bool:
    """Two independent decoders of the same text layer. Docling legitimately drops headers,
    footers and chart labels, so only Docling text *missing from pypdf* counts: measured over 2,356
    pages, median 0.6 %, 95th percentile 16 %. A high share means one of them misread the page —
    e.g. Docling running words together on TI's tight kerning (TPS2378 p3,
    "ConnecttopositivePoEinputpowerrail"), which makes the page unsearchable."""
    b = _tokens(docling_md)
    if len(b) < 30:
        return False
    return 1 - len(b & _tokens(pypdf_text)) / len(b) > DISAGREE


def _has_text(md: str) -> bool:
    return bool(re.sub(r"<!-- image -->|\s", "", md or ""))


def cache_coverage(cache: Path) -> int:
    """Pages a Docling cache has converted (from its chunk names), whether or not they held text.
    Recorded in the .text.md so a later run can tell the cache has grown since (ingest --refresh)."""
    if not cache.is_dir():
        return 0
    return sum(int(f.name[6:10]) - int(f.name[1:5]) + 1 for f in cache.glob("p*-*.pages.json"))


_WORDY = re.compile(r"[A-Za-z]")


def docling_page_extras(cache: Path):
    """One pass over the chunk JSON -> ({page: figure words}, {pages with run-together items}).

    Figure text: Docling keeps the text inside pictures (pin names on a package drawing, chart
    titles and legends) as the picture's children, but the Markdown export leaves it out. Only
    items with letters are kept, so bare axis ticks ("80%", "2.5") don't flood the search.
    Run-together: an item Docling read without its word spaces (see docling_prose.run_together)."""
    from extractor.docling_prose import run_together
    figures, together = {}, set()
    for js in sorted(cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json")):
        doc = json.loads(js.read_text(encoding="utf-8"))
        texts = doc.get("texts", [])
        for t in texts:
            page = (t.get("prov") or [{}])[0].get("page_no")
            if page and t.get("label") not in ("page_header", "page_footer") \
                    and run_together(t.get("text") or ""):
                together.add(page)
        for pic in doc.get("pictures", []):
            page = (pic.get("prov") or [{}])[0].get("page_no")
            words = []
            for ref in pic.get("children", []):
                kind, _, idx = ref.get("$ref", "").lstrip("#/").partition("/")
                if kind == "texts":
                    w = re.sub(r"(?:\s?\.){4,}\s?", " … ",
                               " ".join((texts[int(idx)].get("text") or "").split()))
                    if w and _WORDY.search(w) and w not in words:
                        words.append(w)
            if page and words:
                figures.setdefault(page, []).extend(w for w in words if w not in figures.get(page, []))
    return figures, together


def build_text(pdf, digest, raw_pages, dpages, today, ocr_pages=None, coverage=(0, 0),
               extras=({}, set())):
    lines, used = [], {"docling": 0, "pypdf": 0, "ocr": 0, "empty": 0, "disagree": []}
    figures, together = extras
    ocr_pages = ocr_pages or {}
    for n in range(1, len(raw_pages) + 1):
        lines += ["", PAGE_MARKER % n, ""]
        if _has_text(dpages.get(n)):
            lines.append(obsidian_safe(fix_symbol_pua(dpages[n])).strip())
            used["docling"] += 1
            if figures.get(n):
                lines += ["", "<!-- figure text (inside pictures: pin names, chart labels) -->",
                          obsidian_safe(fix_symbol_pua(" · ".join(figures[n])))]
            if raw_pages[n].strip() and (n in together or readers_disagree(dpages[n], raw_pages[n])):
                # keep the other reading, so the page stays searchable whichever one is wrong
                used["disagree"].append(n)
                lines += ["", "<!-- alternative reading: pypdf — the two text readers disagree on "
                          "this page; check the PDF -->",
                          obsidian_safe(fix_symbol_pua(raw_pages[n])).strip()]
        elif raw_pages[n].strip():
            lines += ["<!-- text: pypdf -->", obsidian_safe(fix_symbol_pua(raw_pages[n])).strip()]
            used["pypdf"] += 1
        elif _has_text(ocr_pages.get(n)):
            lines += ["<!-- text: OCR (RapidOCR) — recognised from the page image; check values "
                      "against the PDF -->", obsidian_safe(ocr_pages[n]).strip()]
            used["ocr"] += 1
        else:
            lines.append("<!-- no text layer on this page -->")
            used["empty"] += 1
    head = ["---",
            f'source: "[[{pdf.name}]]"',
            f"sha256: {digest}",
            f"pages: {len(raw_pages)}",
            f"text_docling_pages: {used['docling']}",
            f"text_pypdf_pages: {used['pypdf']}",
            f"text_ocr_pages: {used['ocr']}",
            f"text_disagreement_pages: {json.dumps(used['disagree'])}",
            f"docling_cache_pages: {coverage[0]}",
            f"ocr_cache_pages: {coverage[1]}",
            f"generated: {today}",
            "generator: datasheet-parser/sidecars.py",
            "---",
            "",
            f"# {pdf.stem} — full text",
            "",
            "Machine-extracted. Page markers are exact; tables and wording may not be — "
            "check the PDF before relying on a value."]
    return "\n".join(head + lines).rstrip() + "\n", used


# ── legacy and previous data ─────────────────────────────────────────────────
def read_frontmatter(md: Path) -> dict:
    if not md.exists():
        return {}
    text = md.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        k, sep, v = line.partition(":")
        if not sep or line.startswith(" "):
            continue
        v = v.strip()
        try:
            out[k.strip()] = json.loads(v)
        except ValueError:
            out[k.strip()] = v
    return out


def previous(pdf: Path) -> dict:
    """The last .data.json written for this PDF, if any: the home of sticky facts."""
    f = pdf.with_name(pdf.stem + ".data.json")
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


def card_props(pdf: Path) -> dict:
    """Properties a person owns on the card: verified, verified_on, verified_note, lcsc,
    original_name, category. (cards.py never writes these.)"""
    return read_frontmatter(pdf.with_name(pdf.stem + ".md"))


_LCSC = re.compile(r"_(C\d{4,})(?:\.pdf)?$", re.I)


def document_ids(pdf, card, prev_doc, original_name):
    ids = dict((prev_doc or {}).get("ids") or {})
    m = (_LCSC.search(Path(original_name or "").stem + ".pdf") or _LCSC.search(original_name or "")
         or re.match(r"(C\d{4,})[ _]", original_name or ""))   # "C84817 MT3608_plain.pdf"
    if m:
        ids["lcsc"] = m.group(1).upper()
    if card.get("lcsc"):
        ids["lcsc"] = str(card["lcsc"]).upper()
    return ids


def legacy_registers(pdf: Path):
    f = pdf.with_name(pdf.stem + ".registers.json")
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8")).get("registers", [])
    return []


def best_registers(*candidates):
    """Register fields never shrink: a later reading that finds fewer fields does not replace a
    fuller map. Counted in fields, not registers — Docling merges pdfplumber's anonymous
    per-table fragments into fewer, named registers. On a tie the first (current) reading wins."""
    return max((c or [] for c in candidates), key=lambda rs: sum(len(r.get("fields", [])) for r in rs))


# ── data ─────────────────────────────────────────────────────────────────────
def doc_type(pdf: Path) -> str:
    n = pdf.stem
    if "Papers" in pdf.parts:
        return "paper"
    if re.search(r"(?i)(^|[_ -])UM([_ -]|$)|user.?manual", n):
        return "user_manual"
    if re.search(r"(?i)errata", n):
        return "errata"
    if re.search(r"(?i)TRM|reference.?manual", n):
        return "reference_manual"
    if re.search(r"(?i)^AN[_ -]?\d|application.?note|^an\d|app.?note|^dn\d|^slva|^swra|^TB\d", n):
        return "app_note"
    return "datasheet"


def load_parse(stem: str):
    d = OUTPUT / stem

    def load(name):
        f = d / name
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    return load("device_info.json"), load("elec_chars.json") or [], \
        load("registers.json") or [], load("textspec.json")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def merge_rows(d_rows, elec):
    """Docling rows + pdfplumber rows. A row both extractors read identically becomes `high`."""
    p_rows = []
    for section in elec or []:
        for s in section.get("specs") or []:
            p_rows.append({
                "section": section.get("name", ""), "page": s.get("source_page"),
                "group": s.get("group", ""), "symbol": s.get("symbol", ""),
                "parameter": s.get("parameter", ""), "conditions": s.get("conditions", ""),
                "min": to_number(s.get("min")), "typ": to_number(s.get("typ")),
                "max": to_number(s.get("max")), "unit": norm_unit(s.get("unit", "")),
                "extractor": "pdfplumber", "confidence": "medium", "flags": [],
            })
    used = set()
    for r in d_rows:
        for i, p in enumerate(p_rows):
            if i in used or p["page"] != r["page"]:
                continue
            same_id = (_norm(p["symbol"]) and _norm(p["symbol"]) == _norm(r["symbol"])) or \
                      (_norm(p["parameter"])[:20] and _norm(p["parameter"])[:20] == _norm(r["parameter"])[:20])
            same_vals = all(p[v] == r[v] for v in ("min", "typ", "max")) and \
                any(r[v] is not None for v in ("min", "typ", "max"))
            if same_id and same_vals:
                used.add(i)
                r["extractor"] = "docling+pdfplumber"
                if not r["flags"]:
                    r["confidence"] = "high"
                break
    # Rows both readers found but read differently. Checked against rendered pages (TPS62861 p6,
    # TPS61230 p5): on TI tables with an empty MIN column pdfplumber shifts TYP into MIN, and
    # Docling had every disputed row right — so Docling's reading stands and pdfplumber's is kept,
    # as evidence, at low confidence.
    for i, p in enumerate(p_rows):
        if i in used:
            continue
        same_row = [r for r in d_rows if r["page"] == p["page"] and r["extractor"] == "docling"
                    and ((_norm(p["symbol"]) and _norm(p["symbol"]) == _norm(r["symbol"])) or
                         (_norm(p["parameter"])[:20] and
                          _norm(p["parameter"])[:20] == _norm(r["parameter"])[:20]))]
        if not same_row:
            continue
        pv = sorted(v for v in (p["min"], p["typ"], p["max"]) if v is not None)
        # several sub-rows share a symbol (IQ at 2.3 and 2.5 µA): pair with the one holding the
        # same numbers if there is one, else the first
        twin = next((r for r in same_row if pv and
                     sorted(v for v in (r["min"], r["typ"], r["max"]) if v is not None) == pv),
                    same_row[0])
        dv = sorted(v for v in (twin["min"], twin["typ"], twin["max"]) if v is not None)
        if not dv:
            continue                               # Docling has no numbers here: nothing to prefer
        p["confidence"] = "low"
        if pv and pv == dv:
            p["flags"] = p["flags"] + ["column_shift_vs_docling"]
            twin["flags"] = sorted(set(twin["flags"]) | {"pdfplumber_placed_columns_differently"})
        else:
            p["flags"] = p["flags"] + ["readers_disagree"]
            twin["flags"] = sorted(set(twin["flags"]) | {"readers_disagree"})
    rows = d_rows + [p for i, p in enumerate(p_rows) if i not in used]
    for r in rows:
        r.pop("raw", None) if r.get("raw") is None else None
    return [r for r in rows if r.get("page")]            # no page, no row


def _docling_complete(cache: Path, n_pages: int) -> bool:
    return cache.is_dir() and cache_coverage(cache) >= n_pages


def _looks_like_part(s) -> bool:
    return bool(s) and len(s) <= 30 and bool(re.search(r"\d", s)) and not s.lower().startswith("www")


def build_data(pdf, digest, n_pages, cache, raw_pages, today, exports=None, overrides=None,
               curated=None, ocr=False):
    flags_extra = []
    info, elec, registers, text = load_parse(pdf.stem)
    readers = {"registers": "pdfplumber" if registers else None,
               "device_info": "pdfplumber" if info else None,
               "prose": "pdftotext" if text else None,
               "tables": "pdfplumber" if elec else None}
    if not ocr and _docling_complete(cache, n_pages):
        # Docling is the reader wherever it is installed and has converted the document:
        # nothing from pdfplumber or pdftotext is used, cached or not, so every document is read
        # the same way. They remain the fallback for documents Docling has not reached.
        pl_registers = registers
        info, elec, registers, text = None, [], [], None
        readers = {"registers": None, "device_info": None, "prose": None, "tables": "docling"}
        # Docling is the primary reader for register maps and the page-1 reading (measured
        # 2026-09-24: same fields, far better register identity; page-1 values a draw).
        # pdfplumber stays the fallback where Docling is absent or hasn't finished a document.
        d_regs = docling_registers.as_dicts(docling_registers.extract(cache))
        n_fields = lambda rs: sum(len(r.get("fields", [])) for r in rs or [])
        registers, readers["registers"] = d_regs, ("docling" if d_regs else None)
        if n_fields(pl_registers) > n_fields(d_regs):
            flags_extra.append("register_fields_fewer_than_pdfplumber")
        d_info = docling_device_info.extract(cache)
        if d_info is not None:
            d_info = dataclasses.asdict(d_info)
            if not _looks_like_part(d_info.get("part_number")):
                # a company name or "Features" is no part number; the file stem usually is one
                d_info["part_number"] = pdf.stem if _looks_like_part(pdf.stem) else None
            d_info["freq_min"], d_info["freq_max"] = d_info.get("freq_min"), d_info.get("freq_max")
            info, readers["device_info"] = d_info, "docling"
        # Prose claims from Docling's text (column-clean, no pdftotext needed), with pypdf read
        # in for pages where Docling ran words together. Measured against the TI export
        # 2026-09-24: precision 97 % either way, recall 54 % vs pdftotext's 49 %.
        text, readers["prose"] = docling_prose.extract(cache, pdf=pdf), "docling"
    d_rows, repaired = docling_rows(cache, raw_pages) if cache.is_dir() else ([], 0)
    if ocr:
        # Recognised text is a weaker reading than a text layer ("25μs" for a supply current on
        # the MAX810S scan): every row it yields is flagged and capped at low confidence.
        for r in d_rows:
            r["extractor"] = "docling-ocr"
            r["confidence"] = "low"
            r["flags"] = sorted(set(r.get("flags", [])) | {"ocr"})

    # Docling's rows regrouped into elec_chars shape, so fuse.roc_range can read them
    d_sections = {}
    for r in d_rows:
        d_sections.setdefault(r["section"], []).append(
            {"symbol": r["symbol"], "parameter": r["parameter"],
             "min": "" if r["min"] is None else str(r["min"]),
             "typ": "" if r["typ"] is None else str(r["typ"]),
             "max": "" if r["max"] is None else str(r["max"]), "unit": r["unit"]})
    d_elec = [{"name": k, "specs": v} for k, v in d_sections.items()]

    kind = doc_type(pdf)
    prev = previous(pdf)
    card = card_props(pdf)
    prev_doc = prev.get("document", {})
    original_name = card.get("original_name") or prev_doc.get("original_name") or pdf.name
    params = []
    if kind == "datasheet" and (info or d_rows):
        fused = fuse.fuse(info or {}, elec, text, d_elec)
        d_pages = d_elec_pages(d_rows)
        for key, (lo, hi) in fused["values"].items():
            p = {"key": key, "min": lo, "typ": None, "max": hi, "unit": PARAM_KEYS[key][2],
                 "doc_min": lo, "doc_max": hi, "doc_confidence": fused["confidence"][key],
                 "readings": _readings(key, fused["readings"][key], elec, d_pages, text,
                                       raw_pages, info, readers["device_info"],
                                       readers["prose"]),
                 "disagreement": fused["disagreements"].get(key),
                 "flags": [f for f in fused["flags"] if f.startswith(key)]}
            p["page"] = _value_page(p)
            if lo is None and hi is None:
                p["doc_confidence"] = "none"
            elif p["page"] is None:
                continue                                    # no page, no value
            params.append(p)

    data = {"document": {}, "params": params}
    keys = [enrichment.norm(k) for k in
            [*((info or {}).get("part_number") and [_clean(info["part_number"])] or []),
             pdf.stem, Path(original_name).stem] if k]
    if exports:
        enrichment.apply(data, exports, overrides or {}, keys)
    override = next(((overrides or {})[k] for k in keys if k in (overrides or {})), None)
    for p in data["params"]:
        enrichment.reconcile(p, override)
    data["params"] = [p for p in data["params"]
                      if p["min"] is not None or p["max"] is not None]
    _apply_verification(data["params"], card, prev.get("params", []), today)

    regs = best_registers(registers, legacy_registers(pdf), prev.get("registers"))
    flags = list(flags_extra)
    if (text or {}).get("_has_register_map") and not regs:
        flags.append("register_map_not_extracted")

    ti = data.get("enrichment", {}).get("ti_export", {}).get("values", {})
    shown = {p["key"]: p for p in data["params"]}
    category = card.get("category") or _folder_category(pdf)
    klass = classify.classify_record(
        pdf.stem, original_name, category,
        {"ti_category": data.get("enrichment", {}).get("ti_export", {}).get("category"),
         "ti_subcategory": ti.get("ti_subcategory"), "ti_function": ti.get("ti_function"),
         "topology": ti.get("topology"), "text_topology": (text or {}).get("_topology"),
         "title": _clean((info or {}).get("title")), "ti_description": ti.get("ti_description"),
         "doc_id": _clean((info or {}).get("doc_id")),
         "part": _clean((info or {}).get("part_number")), "source_pdf": pdf.name,
         "vin_min": (shown.get("vin") or {}).get("min"),
         "vout_max": (shown.get("vout") or {}).get("max")},
        curated)
    if kind != "datasheet":
        klass["product_type"], klass["classified_by"] = kind.replace("_", "-"), "doc-type"
    if not klass["converts_voltage"]:
        # Vin/Vout/Iout/fsw describe a conversion. For an MCU, radio or sensor, page-1 guesses at
        # them are noise (SX1261: "Vin 4-4 V", "fsw 960000 kHz" — its RF band). Keep only what a
        # table, the prose or an export actually stated, and never fsw.
        data["params"] = [p for p in data["params"] if p["key"] != "fsw" and
                          any(r["source"] != "page1" for r in p["readings"])]

    document = {
        "file": pdf.name, "original_name": original_name, "sha256": digest, "pages": n_pages,
        "doc_type": kind, "category": category or None,
        "title": _clean((info or {}).get("title")),
        "parts": [p for p in [_clean((info or {}).get("part_number"))] if p],
        "revision": _revision(pdf.stem),
        "ids": document_ids(pdf, card, prev_doc, original_name),
        **klass,
    }
    if kind == "paper":
        # a paper's own metadata, read by the same code as papers.py (Docling when converted)
        from extractor import paper_source
        meta = paper_source.metadata(paper_source.read(pdf))
        document["paper"] = meta
        document["title"] = meta.get("title") or document["title"]
        document["ids"].update({k: meta[k] for k in ("doi", "arxiv") if meta.get(k)})
    out = {
        "schema": SCHEMA,
        "document": document,
        "extraction": {
            "generated": today,
            "readers": readers,
            "tools": {"datasheet-parser": parser_commit(), "docling": docling_version(cache)},
            "docling_rows": len(d_rows), "pdfplumber_sections": len(elec or []),
            "unit_repairs": repaired,
        },
        "params": data["params"],
        "spec_rows": merge_rows(d_rows, elec),
        "registers": regs,
        "flags": flags,
    }
    if data.get("enrichment"):
        out["enrichment"] = data["enrichment"]
    return out


def _folder_category(pdf: Path) -> str:
    """Legacy layout (before migration): the folder above attachments/, relative to
    Reference Material, stands in for the card's category."""
    parts = list(pdf.parts)
    if "Reference Material" in parts and pdf.parent.name.lower() == "attachments":
        return "/".join(parts[parts.index("Reference Material") + 1:-2])
    return ""


def _readings(key, raw, elec, d_pages, text, raw_pages, info, page1_reader="pdfplumber",
              prose_reader="pdftotext"):
    """Every source's own reading of one parameter, with where it was read."""
    out = []
    spec = [("page1", page1_reader or "pdfplumber", raw["page1"], lambda: 1),
            ("roc", "pdfplumber", raw["roc_pdfplumber"], lambda: _roc_page(elec, key)),
            ("roc", "docling", raw["roc_docling"], lambda: _roc_page(d_pages, key)),
            ("prose", prose_reader or "pdftotext", raw["prose"],
             lambda: _claim_page(text, key, raw_pages))]
    for source, extractor, pair, page in spec:
        if pair and any(v is not None for v in pair):
            out.append({"source": source, "extractor": extractor, "page": page(),
                        "min": pair[0], "max": pair[1]})
    return out


def _value_page(p):
    """Page of the first reading that supports the shown value."""
    for r in p["readings"]:
        if r.get("page") and fuse._pair_agrees((r["min"], r["max"]), (p["min"], p["max"])):
            return r["page"]
    return next((r["page"] for r in p["readings"] if r.get("page")), None)


def _apply_verification(params, card, prev_params, today):
    """Card property `verified: [vin, vout]` (+ verified_on, verified_note) marks values checked
    by a person. The value at the moment of checking is snapshotted and kept; if extraction later
    produces something else, the verified value still shows and the change is flagged."""
    want = card.get("verified")
    if isinstance(want, str):                  # YAML flow list `[vin, vout]` or `vin, vout`
        want = [w.strip().strip("'\"") for w in want.strip("[]").split(",") if w.strip()]
    if not isinstance(want, list):
        want = []                              # `verified: false/true` is the old generated flag
    before = {p["key"]: p["verified"] for p in prev_params
              if isinstance(p.get("verified"), dict) and p["verified"].get("by") == "user"}
    for p in params:
        if p["key"] not in want:
            continue
        snap = before.get(p["key"]) or {"by": "user",
                                        "on": str(card.get("verified_on") or today),
                                        "evidence": card.get("verified_note"),
                                        "value": [p["min"], p["max"]]}
        if not fuse._pair_agrees(tuple(snap["value"]), (p["min"], p["max"])):
            p["flags"] = sorted(set(p.get("flags", [])) | {"verified_value_changed"})
            p["extracted_value"] = [p["min"], p["max"]]
        p["min"], p["max"] = snap["value"]
        p["verified"] = snap


def d_elec_pages(d_rows):
    secs = {}
    for r in d_rows:
        secs.setdefault(r["section"], []).append({**r, "source_page": r["page"],
                                                  "min": str(r["min"] or ""),
                                                  "typ": str(r["typ"] or ""),
                                                  "max": str(r["max"] or "")})
    return [{"name": k, "specs": v} for k, v in secs.items()]


_TEXTSPEC_KEYS = {"vin": ("vin_min", "vin_max"), "vout": ("vout_min", "vout_max"),
                  "iout": ("iout_max",), "fsw": ("fsw_min_khz", "fsw_max_khz", "fsw_fixed_khz")}


def _claim_page(text, key, raw_pages):
    """Page of the prose sentence textspec took the value from."""
    for k in _TEXTSPEC_KEYS[key]:
        ev = ((text or {}).get(k) or {}).get("evidence") or ""
        probe = _norm(ev)[:40]
        if len(probe) < 12:
            continue
        for n, page in raw_pages.items():
            if probe in _norm(page):
                return n
    return None


def _page1_only(p, info):
    """A value that only the page-1 summary gave comes from page 1."""
    fields = {"vin": ("vin_min", "vin_max"), "vout": ("vout_min", "vout_max"),
              "iout": ("iout_max",), "fsw": ("freq_min", "freq_max")}[p["key"]]
    got = [(info or {}).get(k) for k in fields]
    return 1 if any(v not in (None, "") for v in got) else None


def _roc_page(elec, key):
    pattern, _ = fuse._ROW_PATTERNS[key]
    for section in elec or []:
        if "recommended operating" not in (section.get("name") or "").lower():
            continue
        for spec in section.get("specs") or []:
            if pattern.search(f"{spec.get('symbol', '')} {spec.get('parameter', '')}"):
                return spec.get("source_page") or section.get("source_page")
    return None


def _clean(v):
    return " ".join(str(v).split()) if v else None


def _revision(stem):
    m = re.search(r"(?i)(?:^|[_ -])(?:v|rev)[_ .-]?(\d+(?:[._p-]\d+)*)(?=[_ -]|$)", stem)
    return re.sub(r"[_p-]", ".", m.group(1)) if m else None


# ── driver ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="AutoNotes vault (see vaultpath.py)")
    ap.add_argument("--only", default="", help="substring filter on the PDF filename")
    ap.add_argument("--report", action="store_true", help="report coverage and sizes; write nothing")
    ap.add_argument("--pdf", action="append", default=[], type=Path,
                    help="only this PDF (repeatable); used by ingest.py")
    args = ap.parse_args()
    vault = vaultpath.find_vault(args.vault)
    pdfs = [p for pat in ("*.pdf", "*.PDF") for p in sorted(vault.rglob(pat))]
    pdfs = [p for p in vaultpath.dedupe(pdfs)
            if not p.name.startswith(SKIP_PREFIXES) and args.only.lower() in p.name.lower()]
    if args.pdf:
        wanted = {vaultpath.path_key(p.resolve()) for p in args.pdf}
        pdfs = [p for p in pdfs if vaultpath.path_key(p.resolve()) in wanted]
    pdfs = [p for p in pdfs if "Import files" not in p.parts]     # the inbox is not the library
    today = datetime.date.today().isoformat()
    root = vaultpath.root_of(vault)
    exports = []
    for folder in (root / "Parametrics", root / "Reference Material" / "TI Parametrics"):
        exports += enrichment.load_exports(folder)
    overrides = enrichment.load_overrides()
    curated = {}
    for folder in (root / "0archive", root / "Reference Material"):
        if (folder / classify.CURATED_INDEX).exists():
            curated = classify.load_curated(folder)
            break
    print(f"enrichment: {len(exports)} export(s), {sum(len(e.rows) for e in exports)} rows, "
          f"{len(overrides)} override(s); curated index: {len(curated)} names", flush=True)
    tot_pdf = tot_side = 0
    ratios = []
    for pdf in pdfs:
        digest = sha256(pdf)
        cache = OUTPUT / "docling" / digest[:16]
        ocr_cache = OUTPUT / "docling" / (digest[:16] + "-ocr")
        raw = pypdf_pages(pdf)
        dpages = docling_pages(cache) if cache.is_dir() else {}
        ocr_pages = docling_pages(ocr_cache) if ocr_cache.is_dir() else {}
        extras = docling_page_extras(cache) if cache.is_dir() else ({}, set())
        text, used = build_text(pdf, digest, raw, dpages, today, ocr_pages,
                                (cache_coverage(cache), cache_coverage(ocr_cache)), extras)
        use_ocr = used["ocr"] > 0 and used["docling"] + used["pypdf"] == 0
        data = build_data(pdf, digest, len(raw), ocr_cache if use_ocr else cache, raw, today,
                          exports, overrides, curated, ocr=use_ocr)
        data["extraction"]["text_disagreement_pages"] = used["disagree"]
        if used["disagree"]:
            data["flags"] = sorted(set(data["flags"]) | {"text_readers_disagree"})
        if used["docling"] + used["pypdf"] == 0:
            # scanned, or text drawn as outlines: every text-layer extractor sees nothing
            data["flags"] = sorted(set(data["flags"]) | {"no_text_layer"}
                                   | ({"text_from_ocr"} if use_ocr else set()))
        text = clean_text(text)
        blob = clean_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        side = len(text.encode()) + len(blob.encode())
        size = pdf.stat().st_size
        tot_pdf += size
        tot_side += side
        ratios.append((side / size, pdf.name))
        if not args.report:
            vaultpath.write_text(pdf.with_name(pdf.stem + ".text.md"), text)
            vaultpath.write_text(pdf.with_name(pdf.stem + ".data.json"), blob + "\n")
        print(f"{pdf.name[:48]:48} pages {len(raw):4} (docling {used['docling']:4}, "
              f"pypdf {used['pypdf']:4}, ocr {used['ocr']:3}, disagree {len(used['disagree']):3})  rows {len(data['spec_rows']):4}  "
              f"regs {len(data['registers']):3}  {side / 1024:7.0f} KB = {side / size:5.2f}x pdf",
              flush=True)
    ratios.sort(reverse=True)
    print(f"\n{len(pdfs)} PDFs: sidecars {tot_side / 2**20:.1f} MB vs PDFs {tot_pdf / 2**20:.1f} MB "
          f"({tot_side / max(tot_pdf, 1):.2f}x overall)")
    over = [r for r in ratios if r[0] > 1]
    if over:
        print(f"warning: {len(over)} sidecar set(s) larger than their PDF, largest:")
        for r, n in over[:10]:
            print(f"  {r:5.2f}x  {n}")


if __name__ == "__main__":
    main()
