"""
enrichment.py — external data tables as extra *readings* in a document's `.data.json`.

An enrichment source is a table someone else curated: a vendor's parametric-selector export today;
other vendors, distributor catalogues or in-house tables later. Its values are evidence like any
other, so they are added as readings next to what was extracted from the PDF. They are never
written over it. Reconciliation then decides:

    export agrees with the document      -> corroborated fact, confidence "high"
    export only (document gave nothing)  -> one source, confidence "medium"
    export disagrees with the document   -> contested, flag `ti_datasheet_mismatch`, all readings
                                            kept. Shown: the document's value if the document
                                            corroborates itself (two+ independent statements agree;
                                            confidence "medium"), else the export's ("low")

Why the export is shown on a disagreement: TI's selector fills Vin/Vout for ~99% of parts against
~70% accuracy from the documents, and of the three kinds of disagreement found on 2026-09-04
(docs/Enrichment.md) only one favours the datasheet — and that one is handled by a vetted
override. Showing a value is not the same as believing it: the `contested` flag stays until a
person verifies one reading.

Overrides (ti_overrides.json) are corrections a person made with recorded evidence and a check
date, so they count as verification: {"by": "ti_overrides.json", "on": checked, "evidence": why}.

Adapters register in ADAPTERS; each has `detect(path) -> bool` and `load(path) -> Export`.
"""
import csv
import io
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
OVERRIDES = HERE / "ti_overrides.json"

# data.json param key -> (export field for min, export field for max)
PARAM_FIELDS = {"vin": ("vin_min", "vin_max"), "vout": ("vout_min", "vout_max"),
                "iout": (None, "iout_max")}
TOLERANCE = 0.02            # same 2% the old overlay used to call a mismatch

# ── TI export columns ─────────────────────────────────────────────────────────
PART_HEADER = "Product or Part number"

# TI header name -> (field key, scale applied to the numeric value)
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


@dataclass
class Export:
    adapter: str
    file: str
    category: str
    date: str
    rows: dict = field(default_factory=dict)     # normalised part -> {header: value}


# ── TI parametric selector ───────────────────────────────────────────────────
def _read_rows(path: Path):
    if path.suffix.lower() == ".csv":
        return list(csv.reader(path.open(encoding="utf-8")))
    # xlsx: TI's stylesheet defeats openpyxl; xlsx_to_csv reads the sheet XML directly
    from xlsx_to_csv import convert
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sheet.csv"
        convert(path, out)
        return list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))


class TiParametric:
    name = "ti_export"

    @staticmethod
    def detect(path: Path) -> bool:
        if path.suffix.lower() not in (".csv", ".xlsx"):
            return False
        try:
            rows = _read_rows(path)[:40]
        except Exception:                                     # noqa: BLE001 — not ours
            return False
        return any(r and r[0].strip() == PART_HEADER for r in rows) and \
            any(r and "Parametric details for" in (r[0] or "") for r in rows)

    @staticmethod
    def load(path: Path) -> Export:
        rows = _read_rows(path)
        header_i = next(i for i, r in enumerate(rows) if r and r[0].strip() == PART_HEADER)
        header = [h.strip() for h in rows[header_i]]
        category, date = path.stem, ""
        for r in rows[:header_i]:
            if len(r) > 1 and "Parametric details for" in (r[0] or ""):
                category = r[1].strip() or category
            if len(r) > 1 and "File generated on" in (r[0] or ""):
                date = r[1].strip()
        table = {}
        for row in rows[header_i + 1:]:
            if row and row[0].strip():
                table[norm(row[0])] = {header[i]: (row[i] if i < len(row) else "").strip()
                                       for i in range(len(header)) if header[i]}
        return Export(TiParametric.name, path.name, category, date, table)


ADAPTERS = [TiParametric]


def load_exports(folder: Path):
    """Every recognised table in `folder`. A .csv beside an .xlsx of the same stem is the same
    export converted, so the .xlsx (the original) wins and the .csv is skipped."""
    out = []
    if not folder or not folder.is_dir():
        return out
    files = sorted(folder.iterdir())
    stems_with_xlsx = {f.stem for f in files if f.suffix.lower() == ".xlsx"}
    for f in files:
        if f.suffix.lower() == ".csv" and f.stem in stems_with_xlsx:
            continue
        for adapter in ADAPTERS:
            if adapter.detect(f):
                out.append(adapter.load(f))
                break
    return out


def load_overrides():
    if not OVERRIDES.exists():
        return {}
    data = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _richness(rec):
    return sum(1 for h in list(NUMERIC) + list(TEXT) if str(rec.get(h, "")).strip())


def match(exports, keys):
    """The richest export row for any of the candidate part keys (INA3221 appears under both
    Amplifiers and Digital power monitors; the monitor row carries far more)."""
    found = [(e, e.rows[k]) for k in keys if k for e in exports if k in e.rows]
    if not found:
        return None, None
    return max(found, key=lambda c: _richness(c[1]))


def export_values(rec):
    vals = {}
    for header, (key, scale) in NUMERIC.items():
        v = num(rec.get(header))
        if v is not None:
            vals[key] = round(v * scale, 6)
    for header, key in TEXT.items():
        if rec.get(header):
            vals[key] = rec[header]
    return vals


# ── applying to one document ─────────────────────────────────────────────────
def _close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(TOLERANCE * max(abs(a), abs(b)), 1e-6)


def apply(data: dict, exports, overrides, part_keys):
    """Add export readings to data['params'], record the whole row under data['enrichment'].

    Idempotent: previous readings from any enrichment source are removed first, so a refreshed
    export replaces the old one instead of stacking on it.
    """
    data.setdefault("enrichment", {}).pop(TiParametric.name, None)
    for p in data.get("params", []):
        p["readings"] = [r for r in p.get("readings", []) if r.get("source") != TiParametric.name]
    exp, rec = match(exports, part_keys)
    if not exp:
        return data
    vals = export_values(rec)
    data["enrichment"][TiParametric.name] = {
        "file": exp.file, "category": exp.category, "date": exp.date,
        "part": rec.get(PART_HEADER, ""), "values": vals,
        "unmapped": {h: v for h, v in rec.items()
                     if v and h not in NUMERIC and h not in TEXT and h != PART_HEADER},
    }
    params = {p["key"]: p for p in data.setdefault("params", [])}
    for key, (lo_f, hi_f) in PARAM_FIELDS.items():
        lo = vals.get(lo_f) if lo_f else None
        hi = vals.get(hi_f)
        if lo is None and hi is None:
            continue
        p = params.get(key)
        if p is None:
            p = {"key": key, "min": None, "typ": None, "max": None,
                 "unit": {"vin": "V", "vout": "V", "iout": "A"}[key], "readings": [],
                 "confidence": "none", "page": None, "flags": []}
            data["params"].append(p)
            params[key] = p
        p["readings"].append({"source": TiParametric.name, "file": exp.file, "date": exp.date,
                              "category": exp.category, "min": lo, "max": hi})
    ov = next((overrides[k] for k in part_keys if k in overrides), None)
    if ov:
        data["enrichment"][TiParametric.name]["override"] = ov
    return data


def reconcile(p: dict, override=None):
    """Settle a param's shown value, confidence and `contested` from its readings.

    The document's own fused value (from fuse.py, already stored in p) is one side; an external
    reading is the other. Verification and overrides are applied last and never lose.
    """
    ext = [r for r in p.get("readings", []) if r.get("source") == TiParametric.name]
    doc_value = (p.get("doc_min"), p.get("doc_max"))
    doc_conf = p.get("doc_confidence", "none")
    flags = [f for f in p.get("flags", []) if f not in ("ti_datasheet_mismatch", "ti_override_applied")]
    contested = False
    lo, hi, conf = doc_value[0], doc_value[1], doc_conf
    if ext:
        e = ext[0]
        has_doc = any(v is not None for v in doc_value)
        if not has_doc:
            lo, hi, conf = e["min"], e["max"], "medium"
        elif (_close(e["min"], doc_value[0]) or e["min"] is None or doc_value[0] is None) and \
                (_close(e["max"], doc_value[1]) or e["max"] is None or doc_value[1] is None):
            lo = e["min"] if e["min"] is not None else doc_value[0]
            hi = e["max"] if e["max"] is not None else doc_value[1]
            conf = "high"
        else:
            # Contested either way. Which reading to *show* depends on how well the document
            # corroborates itself: two or more independent statements in the datasheet agreeing
            # (doc confidence "high") outweigh the export alone — TPS55287's Vin 36 (title, ROC,
            # prose vs the export's 30) and TPS55288's Vout 22 (ROC + prose vs a derated 21.26).
            # Against a weak document reading the export is right far more often — TPS54202's
            # Vout "0.1-7" was assembled from unrelated rows; TI says 0.6-26.
            contested = True
            flags.append("ti_datasheet_mismatch")
            if doc_conf == "high":
                conf = "medium"
            else:
                lo, hi, conf = e["min"], e["max"], "low"
    if override:
        lo_f, hi_f = PARAM_FIELDS.get(p["key"], (None, None))
        o_lo = override.get(lo_f) if lo_f else None
        o_hi = override.get(hi_f) if hi_f else None
        if o_lo is not None or o_hi is not None:
            lo = float(o_lo) if o_lo is not None else lo
            hi = float(o_hi) if o_hi is not None else hi
            flags.append("ti_override_applied")
            p["verified"] = {"by": "ti_overrides.json", "on": override.get("checked"),
                             "evidence": override.get("why"), "value": [lo, hi]}
    p.update(min=lo, max=hi, confidence=conf, contested=contested, flags=sorted(set(flags)))
    return p
