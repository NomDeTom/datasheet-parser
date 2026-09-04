"""
twin_notes.py — generate a twinned Markdown note for every datasheet PDF in the AutoNotes vault.

For each `<folder>/attachments/<stem>.pdf` this writes `<folder>/attachments/<stem> (datasheet).md`
containing YAML frontmatter with machine-readable parameters (bare numbers, SI-normalised) plus a
readable body: key specs, extracted I2C register map, and an index of electrical-characteristics
sections.

Why the " (datasheet)" suffix rather than a bare `<stem>.md`: Obsidian resolves `[[wikilinks]]` by
basename across the whole vault, and 35 PDF stems already collide with hand-written notes
(bq25601.pdf vs BQ25601.md, ...). On Windows's case-insensitive filesystem those would become
ambiguous links. The suffix keeps every basename unique.

Everything the parser produces is marked `verified: false`. The extractor is regularly wrong on
page-1 device-info fields (it has read an 8A part as 3A, and echoed Vin into the Vout columns), so
these notes are a starting point for review, not a source of truth.

Usage:
    python twin_notes.py                     # process the whole vault, skip existing notes
    python twin_notes.py --force             # regenerate notes that already exist
    python twin_notes.py --limit 10          # first 10 only (smoke test)
    python twin_notes.py --only tps55        # only PDFs whose name contains this substring
    python twin_notes.py --max-mb 12         # PDFs larger than this get a stub note, not a parse
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from fuse import fuse, fmt as fuse_fmt
import textspec

PARSER_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT = Path(r"D:\Clod\AutoNotes\Reference Material")
SUFFIX = " (datasheet)"

# ── unit handling ────────────────────────────────────────────────────────────
_PREFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}
_VALUE_RE = re.compile(r"([-+]?\d*\.?\d+)\s*([pnuµμmkKMG]?)\s*(V|A|Hz|W|Ohm|°C)?", re.IGNORECASE)


def to_number(raw, want):
    """'2.2MHz' -> 2200.0 (kHz) · '3.0V' -> 3.0 · '500mA' -> 0.5 (A).

    `want` is one of 'V', 'A', 'kHz'. Returns None if nothing parseable is found.
    """
    if not raw:
        return None
    m = _VALUE_RE.search(str(raw))
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    val *= _PREFIX.get(m.group(2) or "", 1.0)
    if want == "kHz":
        val /= 1e3
    return round(val, 6)


def clean(text):
    """Collapse whitespace and strip characters that break YAML scalars."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).replace('"', "'").strip()


def yaml_scalar(value):
    if value is None or value == "":
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace('"', "'") + '"'


# ── parser driver ────────────────────────────────────────────────────────────
def load_cache(pdf: Path):
    """Read a previous parse from output/<stem>/ without touching the PDF.

    Rendering is cheap; parsing is not (~33s/PDF). Keeping them separate means the extraction logic
    can improve and every note re-renders in seconds.
    """
    out_dir = PARSER_DIR / "output" / pdf.stem
    if not (out_dir / "device_info.json").exists():
        return None, None, None, "no cached parse — run without --from-cache first", {}

    def load(name):
        path = out_dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            return None

    return (load("device_info.json"), load("registers.json") or [],
            load("elec_chars.json") or [], None, load("textspec.json") or {})


def cache_textspec(pdf: Path):
    """Run the regex prose extractor and cache it next to the pdfplumber output.

    pdftotext is ~0.2s per PDF against pdfplumber's ~30s, so this is cheap enough to redo whenever
    the pattern library changes.
    """
    out_dir = PARSER_DIR / "output" / pdf.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    result = textspec.extract(pdf)
    (out_dir / "textspec.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result


def run_parser(pdf: Path, timeout: int):
    """Run parse.py against one PDF; return (device_info, registers, elec_chars, error)."""
    out_dir = PARSER_DIR / "output" / pdf.stem
    try:
        subprocess.run(
            [sys.executable, "parse.py", str(pdf)],
            cwd=PARSER_DIR, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, None, None, f"parser timed out after {timeout}s", {}
    except Exception as exc:                                   # noqa: BLE001
        return None, None, None, f"parser failed: {exc}", {}

    def load(name):
        path = out_dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            return None

    info = load("device_info.json")
    if info is None:
        return None, None, None, "parser produced no device_info.json", {}
    return (info, load("registers.json") or [], load("elec_chars.json") or [], None,
            cache_textspec(pdf))


# ── note rendering ───────────────────────────────────────────────────────────
def build_note(pdf: Path, info, registers, elec, note_error, size_mb, text=None):
    part = clean((info or {}).get("part_number")) or pdf.stem
    title = clean((info or {}).get("title"))
    doc_id = clean((info or {}).get("document_id"))

    packages = (info or {}).get("packages") or []
    pkg_type = clean(packages[0].get("package_type")) if packages else ""
    pkg_size = clean(packages[0].get("body_size")) if packages else ""
    pkg_pins = packages[0].get("pins") if packages else None

    iface = [clean(i) for i in ((info or {}).get("interface") or []) if clean(i)]

    # Fuse page-1 device_info with the Recommended Operating Conditions table. See fuse.py for why.
    fused = fuse(info, elec, text) if info else {
        "values": {k: (None, None) for k in ("vin", "vout", "iout", "fsw")},
        "confidence": {k: "none" for k in ("vin", "vout", "iout", "fsw")},
        "disagreements": {}, "flags": [], "overall": "none",
    }
    val = fused["values"]

    fields = [
        ("part", yaml_scalar(part)),
        ("title", yaml_scalar(title)),
        ("doc_id", yaml_scalar(doc_id)),
        ("vin_min", yaml_scalar(val["vin"][0])),
        ("vin_max", yaml_scalar(val["vin"][1])),
        ("vin_startup", yaml_scalar(to_number((info or {}).get("vin_startup"), "V"))),
        ("vout_min", yaml_scalar(val["vout"][0])),
        ("vout_max", yaml_scalar(val["vout"][1])),
        ("vsupply_min", yaml_scalar(to_number((info or {}).get("vsupply_min"), "V"))),
        ("vsupply_max", yaml_scalar(to_number((info or {}).get("vsupply_max"), "V"))),
        ("iout_max", yaml_scalar(val["iout"][1])),
        ("freq_min_khz", yaml_scalar(val["fsw"][0])),
        ("freq_max_khz", yaml_scalar(val["fsw"][1])),
        ("text_topology", yaml_scalar((text or {}).get("_topology") or None)),
        ("text_topology_terms",
         "[" + ", ".join(yaml_scalar(t) for t in ((text or {}).get("_topology_terms") or [])) + "]"),
        ("confidence", yaml_scalar(fused["overall"])),
        ("confidence_vin", yaml_scalar(fused["confidence"]["vin"])),
        ("confidence_vout", yaml_scalar(fused["confidence"]["vout"])),
        ("package", yaml_scalar(pkg_type)),
        ("package_pins", yaml_scalar(pkg_pins)),
        ("package_size", yaml_scalar(pkg_size)),
        ("registers", yaml_scalar(len(registers or []))),
        ("elec_sections", yaml_scalar(len(elec or []))),
        ("pdf_mb", yaml_scalar(round(size_mb, 1))),
        ("source_pdf", yaml_scalar(pdf.name)),
        ("generated", yaml_scalar(str(date.today()))),
        ("generator", yaml_scalar("twin_notes.py + datasheet-parser")),
        ("verified", "false"),
    ]

    lines = ["---", "type: datasheet"]
    if iface:
        lines.append("interface: [" + ", ".join(yaml_scalar(i) for i in iface) + "]")
    if fused["flags"]:
        lines.append("flags: [" + ", ".join(yaml_scalar(f) for f in fused["flags"]) + "]")
    if note_error:
        lines.append("parse_error: " + yaml_scalar(note_error))
    lines += [f"{k}: {v}" for k, v in fields]
    lines += ["---", ""]

    lines.append(f"# {part} — extracted parameters")
    lines.append("")
    lines.append(f"**Datasheet:** [[{pdf.name}]]" + (f" *({doc_id})*" if doc_id else ""))
    if title:
        lines.append(f"**Title line:** {title}")
    lines.append("")
    banner = {"high": ("tip", "Machine-extracted — both sources agree"),
              "medium": ("info", "Machine-extracted from the specs table only"),
              "low": ("warning", "Machine-extracted — sources disagree, check this one"),
              "none": ("failure", "Nothing usable extracted")}[fused["overall"]]
    lines.append(f"> [!{banner[0]}] {banner[1]}")
    lines.append("> Fused from two views of the PDF: the page-1 device summary and the")
    lines.append("> **Recommended Operating Conditions** table. Where they agree, confidence is high;")
    lines.append("> where they disagree the table wins and the row is flagged below.")
    if fused["flags"]:
        lines.append("> ")
        lines.append("> **Flags:** " + ", ".join(f"`{f}`" for f in fused["flags"]))
    if fused["disagreements"]:
        lines.append("> ")
        for key, clash in fused["disagreements"].items():
            lines.append(f"> **{key} disagreement:** {clash}")
    lines.append("> ")
    lines.append("> Verify against the PDF, correct above, then set `verified: true`.")
    lines.append("")

    if note_error:
        lines += ["## Not parsed", "", f"`{note_error}`", ""]
        return "\n".join(lines) + "\n"

    def rng(key, unit):
        lo, hi = val[key]
        if lo is None and hi is None:
            return ""
        if lo is not None and hi is not None:
            return f"{fuse_fmt(lo)}–{fuse_fmt(hi)}{unit}"
        return f"{fuse_fmt(lo if lo is not None else hi)}{unit}"

    lines += ["## Key Specifications", "", "| Parameter | Value | Confidence |", "|---|---|---|"]
    rows = [
        ("Input voltage", rng("vin", "V"), fused["confidence"]["vin"]),
        ("Start-up voltage", clean(info.get("vin_startup")), ""),
        ("Output voltage", rng("vout", "V"), fused["confidence"]["vout"]),
        ("Supply voltage", fmt_range(info.get("vsupply_min"), info.get("vsupply_max")), ""),
        ("Max output current", rng("iout", "A"), fused["confidence"]["iout"]),
        ("Switching frequency", rng("fsw", "kHz"), fused["confidence"]["fsw"]),
        ("Package",
         " ".join(x for x in [pkg_type, f"({pkg_pins})" if pkg_pins else "", pkg_size] if x), ""),
        ("Interface", ", ".join(iface), ""),
    ]
    for label, value, conf in rows:
        if value:
            lines.append(f"| {label} | {value} | {conf} |")
    lines.append("")

    if registers:
        lines += [f"## Register Map ({len(registers)} extracted)", "",
                  "| Addr | Register | Reset | Fields |", "|---|---|---|---|"]
        for reg in registers:
            names = ", ".join(clean(f.get("name")) for f in reg.get("fields", []) if clean(f.get("name")))
            lines.append(
                f"| `{clean(reg.get('address')) or '?'}` | {clean(reg.get('name')) or 'UNKNOWN'} "
                f"| {clean(reg.get('reset'))} | {names[:200]} |"
            )
        lines.append("")

    if elec:
        lines += ["## Electrical Characteristics — sections found", ""]
        for sec in elec:
            lines.append(
                f"- **{clean(sec.get('name'))}** — {len(sec.get('specs', []))} spec(s), "
                f"p.{sec.get('source_page')}"
            )
        lines.append("")
        lines.append("*Full extracted values: "
                     f"`datasheet-parser/output/{pdf.stem}/elec_chars.json`*")
        lines.append("")

    return "\n".join(lines) + "\n"


def fmt_range(lo, hi):
    lo, hi = clean(lo), clean(hi)
    if lo and hi:
        return f"{lo}–{hi}"
    return lo or hi or ""


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--force", action="store_true", help="regenerate notes that already exist")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="substring filter on the PDF filename")
    ap.add_argument("--max-mb", type=float, default=12.0,
                    help="PDFs larger than this get a stub note instead of a parse")
    ap.add_argument("--timeout", type=int, default=300, help="per-PDF parser timeout, seconds")
    ap.add_argument("--from-cache", action="store_true",
                    help="re-render notes from output/<stem>/*.json without re-parsing the PDFs "
                         "(seconds instead of hours; implies --force)")
    args = ap.parse_args()
    if args.from_cache:
        args.force = True

    # rglob is case-insensitive on Windows, so a single *.pdf pattern already catches .PDF —
    # globbing both and concatenating listed every file twice.
    seen, pdfs = set(), []
    for pattern in ("*.pdf", "*.PDF"):
        for p in sorted(args.vault.rglob(pattern)):
            if p.parent.name.lower() != "attachments":
                continue
            key = str(p).lower()
            if key not in seen:
                seen.add(key)
                pdfs.append(p)
    if args.only:
        pdfs = [p for p in pdfs if args.only.lower() in p.name.lower()]
    if args.limit:
        pdfs = pdfs[: args.limit]

    made = skipped = stubbed = failed = 0
    started = time.time()
    print(f"{len(pdfs)} PDF(s) under {args.vault}", flush=True)

    for i, pdf in enumerate(pdfs, 1):
        note = pdf.with_name(pdf.stem + SUFFIX + ".md")
        if note.exists() and not args.force:
            skipped += 1
            continue

        size_mb = pdf.stat().st_size / 1e6
        if size_mb > args.max_mb and not args.from_cache:
            body = build_note(pdf, {}, [], [],
                              f"skipped: {size_mb:.1f}MB exceeds --max-mb {args.max_mb} "
                              f"(reference manuals are not parameter sources)", size_mb, {})
            note.write_text(body, encoding="utf-8")
            stubbed += 1
            print(f"[{i}/{len(pdfs)}] STUB  {pdf.name} ({size_mb:.1f}MB)", flush=True)
            continue

        if args.from_cache:
            info, registers, elec, err, text = load_cache(pdf)
            # Backfill for parses made before textspec existed, and refresh caches written before
            # topology detection was added — otherwise a stale cache silently omits the new keys.
            if info is not None and (not text or "_topology" not in text):
                text = cache_textspec(pdf)
            if err and size_mb > args.max_mb:
                err = (f"skipped: {size_mb:.1f}MB exceeds --max-mb {args.max_mb} "
                       f"(reference manuals are not parameter sources)")
        else:
            info, registers, elec, err, text = run_parser(pdf, args.timeout)
        body = build_note(pdf, info or {}, registers, elec, err, size_mb, text)
        note.write_text(body, encoding="utf-8")
        if err:
            failed += 1
            print(f"[{i}/{len(pdfs)}] FAIL  {pdf.name}: {err}", flush=True)
        else:
            made += 1
            part = clean((info or {}).get("part_number")) or "?"
            print(f"[{i}/{len(pdfs)}] ok    {pdf.name} -> {part}", flush=True)

    print(f"\ndone in {time.time() - started:.0f}s — "
          f"{made} parsed, {stubbed} stubbed (too large), {failed} failed, {skipped} skipped",
          flush=True)


if __name__ == "__main__":
    main()
