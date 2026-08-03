"""
Extract LoRa receiver sensitivity tables from the SX1276/77/78/79 datasheet
and write a clean markdown file.

Usage:
    python sx1276_sensitivity.py <pdf_path> [output.md]

Sensitivity symbols follow the pattern:
  RFS_L{BW}_{BAND}   e.g.  RFS_L125_LF  = LoRa 125kHz BW, low-frequency bands
  RFS_L10_HF         = LoRa 10kHz BW,  high-frequency band

All multi-value cells are packed as newline-separated strings by pdfplumber
e.g. "SF6\\nSF7\\nSF8" and "-131\\n-134\\n-138" -- so we zip them.
"""
import sys
import re
import pdfplumber
from pathlib import Path
from collections import defaultdict

PDF = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("SX1276-9_DS_V7.pdf")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("sx1276_sensitivity.md")


def split_cell(cell):
    if cell is None:
        return []
    return [t.strip() for t in re.split(r"[\n]+", str(cell).strip()) if t.strip()]


def parse_sensitivity_row(symbol, desc, conditions_cell, typ_cell, unit_cell):
    conds = split_cell(conditions_cell)
    typs  = split_cell(typ_cell)
    unit  = split_cell(unit_cell)[0] if unit_cell else "dBm"
    rows  = []
    for cond, typ in zip(conds, typs):
        if typ.strip("-").strip():
            rows.append((symbol, desc, cond.strip(), typ.strip(), unit))
    return rows


SENSITIVITY_RE = re.compile(r"^RFS_L", re.IGNORECASE)
BW_RE    = re.compile(r"RFS_L(\d+)_([LH]F)", re.IGNORECASE)
SF_RE    = re.compile(r"SF\s*=?\s*(\d+)", re.IGNORECASE)
BW_LABEL = {"10": "10.4 kHz", "62": "62.5 kHz", "125": "125 kHz",
             "250": "250 kHz", "500": "500 kHz"}
BAND_LABEL = {"LF": "Bands 1–3 (137–1020 MHz)", "HF": "Band 3 HF / LnaBoost"}

sections = {}

with pdfplumber.open(PDF) as pdf:
    for page_num, page in enumerate(pdf.pages, 1):
        for tbl in page.extract_tables():
            if not tbl or len(tbl) < 2:
                continue
            headers = [str(h or "").strip().lower() for h in tbl[0]]
            joined  = " ".join(headers)
            if "conditions" not in joined:
                continue
            if "typ" not in joined and "min" not in joined:
                continue

            col = {}
            for i, h in enumerate(tbl[0]):
                hu = str(h or "").strip().lower()
                if hu == "symbol":             col["symbol"] = i
                elif hu == "description":      col["desc"]   = i
                elif "condition" in hu:        col["cond"]   = i
                elif hu in ("typ", "typical"): col["typ"]    = i
                elif hu == "min":              col.setdefault("min", i)
                elif hu in ("unit", "units"):  col["unit"]   = i

            if "symbol" not in col or ("typ" not in col and "min" not in col):
                continue

            last_symbol = last_desc = ""
            for row in tbl[1:]:
                def get(key):
                    idx = col.get(key)
                    return row[idx] if idx is not None and idx < len(row) else None

                symbol = (get("symbol") or "").strip() or last_symbol
                desc   = ((get("desc") or "").strip().replace("\n", " ")) or last_desc
                if symbol: last_symbol = symbol
                if desc:   last_desc   = desc

                if not SENSITIVITY_RE.match(symbol):
                    continue

                typ_key = "typ" if "typ" in col else "min"
                parsed  = parse_sensitivity_row(symbol, desc, get("cond"), get(typ_key), get("unit"))
                if parsed:
                    sections.setdefault((symbol, desc), []).extend(parsed)

tables_out = defaultdict(lambda: defaultdict(dict))
for (symbol, desc), rows in sections.items():
    m = BW_RE.search(symbol)
    if not m:
        continue
    bw_raw, band_raw = m.group(1), m.group(2).upper()
    bw_label   = BW_LABEL.get(bw_raw, f"{bw_raw} kHz")
    band_label = BAND_LABEL.get(band_raw, band_raw)
    for (sym, dsc, cond, typ, unit) in rows:
        sf_m = SF_RE.search(cond)
        if sf_m:
            tables_out[bw_label][band_label][int(sf_m.group(1))] = typ

lines = []
lines.append("# SX1276/77/78/79 — LoRa Receiver Sensitivity")
lines.append("")
lines.append("*Extracted from DS_SX1276-7-8-9_W_APP_V7. All values typical dBm,*")
lines.append("*highest LNA gain (LnaBoost on where stated), measured at 0.1% BER.*")
lines.append("*Split RF paths (separate Rx/Tx pins); RF switch loss not included.*")
lines.append("")

BW_ORDER = ["10.4 kHz", "62.5 kHz", "125 kHz", "250 kHz", "500 kHz"]
bw_sorted = sorted(tables_out.keys(), key=lambda b: BW_ORDER.index(b) if b in BW_ORDER else 99)

for bw_label in bw_sorted:
    bands = tables_out[bw_label]
    band_list = sorted(bands.keys())
    sfs = sorted({sf for band in bands.values() for sf in band})
    lines.append(f"## BW = {bw_label}")
    lines.append("")
    lines.append("| SF | " + " | ".join(band_list) + " |")
    lines.append("|" + "---|" * (len(band_list) + 1))
    for sf in sfs:
        cells = [f"SF{sf}"] + [bands[b].get(sf, "—") for b in band_list]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

if "125 kHz" in tables_out:
    lines.append("---")
    lines.append("")
    lines.append("## Cross-family reference: 125 kHz BW")
    lines.append("")
    lines.append("| SF | SX1276 LF bands | SX1262 (RxBoosted) | LR1121 (RxBoosted) | LR20xx |")
    lines.append("|---|---|---|---|---|")
    sx1262 = {7: "−124", 12: "−137"}
    lr1121  = {7: "−127", 12: "−141"}
    lr20xx  = {5: "−122", 6: "−125", 7: "−127.5", 8: "−130.5",
               9: "−133",  10: "−136", 11: "−138.5", 12: "−141.5"}
    lf_band = sorted(tables_out["125 kHz"].keys())[0]
    all_sfs = sorted({*tables_out["125 kHz"][lf_band].keys(), *lr20xx.keys()})
    for sf in all_sfs:
        sx76 = tables_out["125 kHz"][lf_band].get(sf, "—")
        lines.append(f"| SF{sf} | {sx76} | {sx1262.get(sf,'—')} | {lr1121.get(sf,'—')} | {lr20xx.get(sf,'—')} |")
    lines.append("")
    lines.append("*See [[LoRa Sensitivity Comparison]] for full cross-family detail.*")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"Written {OUT}")
print(f"Extracted {sum(len(v) for v in sections.values())} rows across {len(sections)} symbols")
