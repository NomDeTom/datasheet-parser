"""
Extract LoRa receiver sensitivity tables from LR1121 / LR1110 / LR1120 datasheets.
Parses pdftotext -layout output since pdfplumber cannot detect these tables.

Usage:
    python lr11xx_sensitivity.py <pdf_path> [output.md]
"""
import re
import sys
import subprocess
from pathlib import Path
from collections import defaultdict

PDF = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("LR1121_DS_V2_1.pdf")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(PDF.stem + "_sensitivity.md")

raw = subprocess.run(
    ["pdftotext", "-layout", str(PDF), "-"],
    capture_output=True, text=True, encoding="utf-8", errors="replace"
).stdout

# Match sensitivity rows.  The description column may contain text like
# "RxBoosted = 1" or "Sensitivity LoRa," so we skip anything before BWL=.
ROW_RE = re.compile(
    r"^ {1,8}"
    r"(?P<symbol>RXS\w+)"
    r" {2,}"
    r"(?:(?!BWL)[^\n]{0,60}? {2,})?"    # optional description
    r"(?P<cond>BWL\s*=\s*[\d.]+\s*kHz[^-\n]*?)"
    r" {2,}"
    r"-\s+"
    r"(?P<typ>-\d[\d.]*)"
    r"\s+-\s+"
    r"dBm",
    re.MULTILINE
)

BWL_RE = re.compile(r"BWL\s*=\s*([\d.]+)", re.IGNORECASE)
SF_RE  = re.compile(r"SF\s*=\s*(\d+)", re.IGNORECASE)

# Band assignment by symbol number for HF symbols.
# LR1121:  RXSLHF1-6  = S-Band, RXSLHF7-10 = 2.4GHz, RXSLHF11-14 = L-Band
# LR1110/1120 share the same numbering scheme for their HF tables.
LBAND_SYMS = {"RXSLHF11", "RXSLHF12", "RXSLHF13", "RXSLHF14"}
GHZ24_SYMS = {"RXSLHF7",  "RXSLHF8",  "RXSLHF9",  "RXSLHF10"}
SBAND_SYMS = {"RXSLHF1",  "RXSLHF2",  "RXSLHF3",
              "RXSLHF4",  "RXSLHF5",  "RXSLHF6"}

def band_for(sym):
    base = re.sub(r"HP\d+", "", sym).upper()
    if "HF" not in base:    return "Sub-GHz"
    if base in LBAND_SYMS:  return "L-Band"
    if base in GHZ24_SYMS:  return "2.4GHz"
    if base in SBAND_SYMS:  return "S-Band"
    return "HF"

def fmt(v):
    """Format float: drop .0 for integers, keep .5 for half-dB values."""
    return str(int(v)) if v == int(v) else str(v)

records = []
for m in ROW_RE.finditer(raw):
    sym  = m.group("symbol")
    cond = m.group("cond").strip()
    typ  = float(m.group("typ"))
    bw_m = BWL_RE.search(cond)
    sf_m = SF_RE.search(cond)
    if not bw_m or not sf_m:
        continue
    records.append(dict(
        symbol  = sym,
        band    = band_for(sym),
        bw      = float(bw_m.group(1)),
        sf      = int(sf_m.group(1)),
        boosted = "HP7" in sym or "HP3" in sym,
        typ     = typ,
    ))

print(f"Parsed {len(records)} rows")
for r in records:
    print(f"  {r['symbol']:18s}  {r['band']:8s}  BW={r['bw']:6g}kHz  "
          f"SF{r['sf']:2d}  {'boosted' if r['boosted'] else 'standard':8s}  {r['typ']} dBm")

tables = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
for r in records:
    mode = "RxBoosted" if r["boosted"] else "Standard"
    tables[r["band"]][mode][r["bw"]][r["sf"]] = r["typ"]

chip_m = re.match(r"(LR\d+)", PDF.stem, re.IGNORECASE)
chip   = chip_m.group(1).upper() if chip_m else PDF.stem
BAND_ORDER = ["Sub-GHz", "S-Band", "2.4GHz", "L-Band"]

lines = [
    f"# {chip} — LoRa Receiver Sensitivity Tables", "",
    f"*Extracted from {PDF.name}.*",
    "*All values typical dBm, PER = 1%, 64-byte packet, CR = 4/5, CRC on.*",
    "*S-Band and L-Band on RFIO_HF port, all RxBoosted = 1.*", "",
]

for band in BAND_ORDER:
    if band not in tables:
        continue
    lines += [f"## {band}", ""]
    for mode in ["Standard", "RxBoosted"]:
        if mode not in tables[band]:
            continue
        bw_data   = tables[band][mode]
        all_sfs   = sorted({sf for d in bw_data.values() for sf in d})
        bw_sorted = sorted(bw_data.keys())
        lines += [f"### {mode}", ""]
        lines.append("| BW | " + " | ".join(f"SF{sf}" for sf in all_sfs) + " |")
        lines.append("|" + "---|" * (len(all_sfs) + 1))
        for bw in bw_sorted:
            row = [f"{bw:g} kHz"] + [
                fmt(bw_data[bw][sf]) if sf in bw_data[bw] else "—"
                for sf in all_sfs
            ]
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"\nWritten {OUT}")
