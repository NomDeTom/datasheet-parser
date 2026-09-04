"""
enrich_ti.py — overlay TI parametric-selector exports onto the generated twin notes.

Works across TI's product categories. Each export has a *different* column set — DC/DC converters
carry Vin/Vout/Iout/topology, battery-management ICs carry cell chemistry and charge current, digital
power monitors carry common-mode range and ADC resolution, amplifiers carry almost nothing numeric —
and only `Product or Part number`, `Description`, `Package type`, `Price`, `Status`, `Rating` and
`Operating temperature range` are common to all. So columns are matched **by header name**, never by
position, and any column without a canonical mapping is still rendered into the note body rather than
dropped.

TI's curated data beats anything extractable from the PDF (99% fill on Vin/Vout against roughly 70%
accuracy from a two-source fusion of the document), so where an export covers a part it wins:
`source: ti-parametric`, `verified: true`.

It is not treated as gospel. Checked against the PDFs on 2026-09-04, the disagreements between TI's
selector and TI's own datasheets are NOT one phenomenon — they split three ways, and which source to
trust differs by case:

  1. TI database error — trust the DATASHEET.
     TPS55287's field says Vin max 30, contradicted by the same row's own Description ("36V 4A
     buck-boost converter with I2C interface") and by the datasheet twice ("36-V, 4-A" in the title,
     "up to 36V input voltage capability" in the body). Its resistor-set twin TPS552872 correctly
     reads 36 in the same export, and TPS55289 genuinely is a 30V part — so the 30 looks copied
     across from the TPS55289 row. Corrected via ti_overrides.json.

  2. Selector-only derated figure — both right, different meanings.
     TPS55288/TPS552882-Q1 report Vout max 21.26V. "21.26" appears nowhere in the 53-page datasheet,
     which states 0.8-22V. A computed ceiling TI publishes only in the selector.

  3. Bad PDF extraction — trust the SELECTOR.
     TPS54202 was extracted as Vout 0.1-7V; page 1 states no Vout range at all, so that was
     assembled from unrelated rows. TI says 0.6-26V.

So no single rule holds. The default lets the export win — right for (3), harmless for (2), WRONG
for (1), which is why the override file exists. A conflicting PDF-derived value is always preserved
as `datasheet_<field>` with a `ti_datasheet_mismatch` flag; an overridden part additionally keeps
TI's own figure as `ti_published_<field>` and carries `ti_override_applied`. The flagged parts want
a human decision, not a default.

Note on idempotency: mismatch *detection* only fires the first time an export is applied, because
afterwards the primary field already holds TI's value. The flags and `datasheet_*` fields persist,
but the console count drops on re-runs — read the flag totals from verify_twins.py, not the tail
line here.

Usage:
    python enrich_ti.py --csv ti_dcdc.csv --csv ti_battery.csv --dry-run
    python enrich_ti.py --csv-dir <folder>          # every ti_*.csv in a folder
"""
import argparse
import csv
import json
import re
from pathlib import Path

import vaultpath

DEFAULT_VAULT = None        # resolved at run time by vaultpath.find_vault()
PART_HEADER = "Product or Part number"
OVERRIDES_PATH = Path(__file__).resolve().parent / "ti_overrides.json"


def load_overrides():
    """Corrections for fields where TI's own selector database is demonstrably wrong.

    Applied after the export, so a refreshed export cannot silently reinstate a known-bad value.
    See ti_overrides.json for the evidence behind each entry.
    """
    if not OVERRIDES_PATH.exists():
        return {}
    data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}

# TI header name -> (frontmatter key, scale applied to the numeric value)
NUMERIC = {
    "Vin (min) (V)": ("vin_min", 1), "Vin (max) (V)": ("vin_max", 1),
    "Vout (min) (V)": ("vout_min", 1), "Vout (max) (V)": ("vout_max", 1),
    "Iout (max) (A)": ("iout_max", 1),
    "Switch current limit (typ) (A)": ("switch_ilim_a", 1),
    "Iq (typ) (A)": ("iq_typ_ua", 1e6), "Iq (max) (mA)": ("iq_max_ua", 1e3),
    "Supply voltage (min) (V)": ("vsupply_min", 1),
    "Supply voltage (max) (V)": ("vsupply_max", 1),
    "Common-mode voltage (min) (V)": ("vcm_min", 1),
    "Common-mode voltage (max) (V)": ("vcm_max", 1),
    "Resolution (Bits)": ("resolution_bits", 1),
    "Number of channels": ("channels", 1),
    "Charge current (max) (A)": ("charge_current_max_a", 1),
    "Number of series cells (min)": ("cells_min", 1),
    "Number of series cells (max)": ("cells_max", 1),
    "Duty cycle (max) (%)": ("duty_max_pct", 1),
    "Package area (mm^2)": ("package_area_mm2", 1),
    "Pin count": ("pin_count", 1),
    "Price|Quantity (USD)": ("ti_price_usd", 1),
}

TEXT = {
    "Topology": "topology", "Control mode": "control_mode",
    "Cell chemistry": "cell_chemistry", "Function": "ti_function",
    "Digital interface": "digital_interface", "Subcategory": "ti_subcategory",
    "Package type": "ti_package", "Package size (L x W) (mm)": "ti_package_size",
    "Operating temperature range (°C)": "temp_range_c", "Status": "lifecycle",
    "Rating": "ti_rating", "Description": "ti_description",
    "TI functional safety category": "ti_functional_safety",
}

# Fields where a conflict with the PDF-derived value is worth recording rather than overwriting.
CONFLICT_KEYS = {"vin_min", "vin_max", "vout_min", "vout_max", "iout_max",
                 "vsupply_min", "vsupply_max"}


# Browser download artifacts: "ina219 (1).pdf", "bq25672 (1).pdf", "spv1040 (2).pdf". Stripping
# punctuation without removing these first turns INA219 into INA2191, which matches nothing — so the
# part is silently never enriched. Only a trailing parenthesised 1-2 digit number is removed, so
# genuine part numbers like INA2227 are untouched.
_DL_ARTIFACT = re.compile(r"\s*\(\d{1,2}\)\s*$")


def norm(text):
    return re.sub(r"[^A-Z0-9]", "", _DL_ARTIFACT.sub("", (text or "").strip()).upper())


def num(text):
    if text is None or str(text).strip() == "":
        return None
    try:
        return float(str(text).strip())
    except ValueError:
        return None


def yaml_scalar(value):
    if value is None or value == "":
        return "null"
    if isinstance(value, float):
        return str(round(value, 6))
    if isinstance(value, int):
        return str(value)
    return '"' + str(value).replace('"', "'") + '"'


def load_export(csv_path: Path):
    """-> (category, {normalised part number: {header: value}})"""
    rows = list(csv.reader(csv_path.open(encoding="utf-8")))
    header_i = next((i for i, r in enumerate(rows) if r and r[0].strip() == PART_HEADER), None)
    if header_i is None:
        return None, {}
    header = [h.strip() for h in rows[header_i]]

    category = csv_path.stem
    for r in rows[:header_i]:
        if len(r) > 1 and "Parametric details for" in (r[0] or ""):
            category = r[1].strip() or category
            break

    table = {}
    for row in rows[header_i + 1:]:
        if not row or not row[0].strip():
            continue
        table[norm(row[0])] = {header[i]: (row[i] if i < len(row) else "").strip()
                               for i in range(len(header)) if header[i]}
    return category, table


def split_frontmatter(text):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5:]


def read_field(front, key):
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", front, re.M)
    if not m:
        return None
    raw = m.group(1).strip()
    return None if raw in ("null", "") else raw.strip('"')


def set_field(front, key, value):
    pattern = rf"^{re.escape(key)}:.*$"
    if re.search(pattern, front, re.M):
        return re.sub(pattern, f"{key}: {value}", front, count=1, flags=re.M)
    return front.rstrip("\n") + f"\n{key}: {value}"


def enrich(note: Path, rec, category, overrides=None, dry_run=False):
    text = note.read_text(encoding="utf-8")
    front, body = split_frontmatter(text)
    if front is None:
        return None

    mismatches, mapped = [], set()

    for header, (key, scale) in NUMERIC.items():
        raw = rec.get(header)
        value = num(raw)
        if value is None:
            continue
        mapped.add(header)
        value = round(value * scale, 6)
        if key in CONFLICT_KEYS:
            old = num(read_field(front, key))
            if old is not None and abs(old - value) > max(0.02 * abs(value), 1e-6):
                front = set_field(front, f"datasheet_{key}", yaml_scalar(old))
                mismatches.append(f"{key}: datasheet {old:g} vs TI {value:g}")
        front = set_field(front, key, yaml_scalar(value))

    for header, key in TEXT.items():
        raw = rec.get(header)
        if raw:
            mapped.add(header)
            front = set_field(front, key, yaml_scalar(raw))

    # Apply vetted corrections over the export, recording what TI actually published.
    applied = []
    for key, value in (overrides or {}).items():
        if key in ("why", "checked"):
            continue
        published = num(read_field(front, key))
        if published is not None and abs(published - float(value)) > 1e-6:
            front = set_field(front, f"ti_published_{key}", yaml_scalar(published))
        front = set_field(front, key, yaml_scalar(float(value)))
        applied.append(f"{key}={value}")
    if applied:
        existing = read_field(front, "flags") or ""
        flags = re.findall(r'"([^"]+)"', existing)
        if "ti_override_applied" not in flags:
            flags.append("ti_override_applied")
        front = set_field(front, "flags", "[" + ", ".join(f'"{f}"' for f in flags) + "]")

    front = set_field(front, "source", '"ti-parametric"')
    front = set_field(front, "ti_category", yaml_scalar(category))
    front = set_field(front, "ti_part", yaml_scalar(rec.get(PART_HEADER, "").strip()))
    front = set_field(front, "verified", "true")
    if any(k in rec for k in ("Vin (min) (V)", "Supply voltage (min) (V)",
                              "Common-mode voltage (min) (V)")):
        front = set_field(front, "confidence", '"high"')
        front = set_field(front, "confidence_vin", '"high"')
        front = set_field(front, "confidence_vout", '"high"')

    if mismatches:
        existing = read_field(front, "flags") or ""
        flags = re.findall(r'"([^"]+)"', existing)
        if "ti_datasheet_mismatch" not in flags:
            flags.append("ti_datasheet_mismatch")
        front = set_field(front, "flags", "[" + ", ".join(f'"{f}"' for f in flags) + "]")

    # Body block: every column TI supplied, including ones with no canonical mapping.
    block = ["", "## TI parametric selector", "",
             f"Authoritative values from TI's **{category}** export for "
             f"**{rec.get(PART_HEADER, '').strip()}**.", "",
             "| Parameter | Value |", "|---|---|"]
    for header, value in rec.items():
        if header in (PART_HEADER, "PDF datasheet", "HTML datasheet") or not value:
            continue
        marker = "" if header in mapped else " *(unmapped)*"
        block.append(f"| {header}{marker} | {value[:180]} |")
    block.append("")
    if applied:
        block += ["> [!note] Override applied — TI's export is wrong here",
                  "> **Corrected:** " + ", ".join(applied),
                  "> ",
                  "> " + " ".join((overrides or {}).get("why", "").split()),
                  "> ",
                  "> TI's published value is kept as `ti_published_*`. Defined in "
                  "`datasheet-parser/ti_overrides.json`; a refreshed export cannot reinstate it.",
                  ""]
    if mismatches:
        block += ["> [!warning] TI's selector disagrees with its own datasheet",
                  "> " + " · ".join(mismatches),
                  "> ",
                  "> The selector lists a narrower *recommended* envelope than the datasheet",
                  "> headline. Both are kept — `datasheet_*` fields hold the PDF-derived value.",
                  ""]

    body = re.sub(r"\n## TI parametric selector\n.*?(?=\n## |\Z)", "\n", body, flags=re.S)
    body = body.rstrip("\n") + "\n" + "\n".join(block)

    if not dry_run:
        vaultpath.write_text(note, "---\n" + front.rstrip("\n") + "\n---\n" + body)
    return mismatches


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, action="append", default=[])
    ap.add_argument("--csv-dir", type=Path)
    ap.add_argument("--vault", type=Path, default=None,
                    help="vault Reference Material folder; else $AUTONOTES_VAULT, "
                         ".autonotes-vault, or a conventional location")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    args.vault = vaultpath.find_vault(args.vault)

    paths = list(args.csv)
    if args.csv_dir:
        paths += sorted(args.csv_dir.glob("ti_*.csv"))
    if not paths:
        ap.error("give --csv or --csv-dir")

    overrides = load_overrides()
    if overrides:
        print(f"{len(overrides)} vetted override(s): {', '.join(sorted(overrides))}")

    exports = []
    for path in paths:
        category, table = load_export(path)
        if not table:
            print(f"  !! no '{PART_HEADER}' header in {path.name} — skipped")
            continue
        exports.append((category, table))
        print(f"{len(table):>5} parts — {category}  ({path.name})")

    matched = mismatched = 0
    for note in sorted(args.vault.rglob("* (datasheet).md")):
        stem = note.stem.replace(" (datasheet)", "")
        key = norm(stem)
        # A part can appear in several exports — TI files INA3221 under both Amplifiers and Digital
        # power monitors. Taking the first match meant filename order decided, so INA3221 got the
        # Amplifiers row (almost no numeric columns) instead of the monitor row (common-mode range,
        # ADC resolution, offset drift). Pick the export whose row actually carries the most mapped
        # fields, and only fall back to order on a genuine tie.
        candidates = [(category, table[key]) for category, table in exports if key in table]
        if candidates:
            def richness(rec):
                return sum(1 for h in list(NUMERIC) + list(TEXT)
                           if str(rec.get(h, "")).strip())
            candidates.sort(key=lambda c: -richness(c[1]))
            if len(candidates) > 1:
                best, *rest = candidates
                print(f"  · {stem:<20} in {len(candidates)} exports; chose [{best[0]}] "
                      f"({richness(best[1])} fields) over "
                      + ", ".join(f"[{c[0]}] ({richness(c[1])})" for c in rest))

        for category, rec in candidates:
            result = enrich(note, rec, category, overrides.get(key), args.dry_run)
            if result is None:
                print(f"  !! no frontmatter: {note.name}")
                break
            matched += 1
            if result:
                mismatched += 1
                print(f"  ~ {stem:<20} [{category}] {'; '.join(result)}")
            else:
                print(f"  + {stem:<20} [{category}]")
            break

    verb = "would enrich" if args.dry_run else "enriched"
    print(f"\n{verb} {matched} twin note(s); {mismatched} carry a TI/datasheet mismatch")


if __name__ == "__main__":
    main()
