"""
xlsx_to_csv.py — dump a sheet from an .xlsx straight to CSV, bypassing openpyxl.

TI's parametric-selector exports carry a stylesheet openpyxl refuses to load ("Colors must be aRGB
hex values"), so the whole workbook is unreadable through the normal path. The cell data itself is
fine — this reads `xl/worksheets/sheetN.xml` (plus `xl/sharedStrings.xml` when present) directly and
ignores styling entirely.

Usage:
    python xlsx_to_csv.py <file.xlsx> [-o out.csv] [--sheet 1]
"""
import argparse
import csv
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def col_index(ref):
    """'A' -> 0, 'B' -> 1, 'AA' -> 26."""
    letters = _CELL_REF.match(ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def shared_strings(zf):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out = []
    for si in root.findall("m:si", NS):
        out.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    return out


# TI writes part numbers and datasheet links as =HYPERLINK("url","display") with no cached value,
# so the cell reads as empty unless the formula's display argument is recovered.
_HYPERLINK = re.compile(r'HYPERLINK\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)', re.I)


def cell_text(cell, strings, want_url=False):
    kind = cell.get("t")
    if kind == "inlineStr":
        is_el = cell.find("m:is", NS)
        return "".join(t.text or "" for t in is_el.iter(f"{{{NS['m']}}}t")) if is_el is not None else ""

    v = cell.find("m:v", NS)
    if v is None or v.text is None:
        f = cell.find("m:f", NS)
        if f is not None and f.text:
            m = _HYPERLINK.search(f.text)
            if m:
                return m.group(1) if want_url else m.group(2)
        return ""
    if kind == "s":
        try:
            return strings[int(v.text)]
        except (ValueError, IndexError):
            return ""
    return v.text


def convert(xlsx: Path, out: Path, sheet: int = 1):
    with zipfile.ZipFile(xlsx) as zf:
        strings = shared_strings(zf)
        name = f"xl/worksheets/sheet{sheet}.xml"
        if name not in zf.namelist():
            sys.exit(f"{name} not in workbook; found "
                     f"{[n for n in zf.namelist() if 'worksheets/' in n]}")
        root = ET.fromstring(zf.read(name))

    rows, widest = [], 0
    for row in root.iter(f"{{{NS['m']}}}row"):
        values = {}
        for cell in row.findall("m:c", NS):
            ref = cell.get("r")
            if not ref:
                continue
            values[col_index(ref)] = cell_text(cell, strings)
        if not values:
            rows.append([])
            continue
        width = max(values) + 1
        widest = max(widest, width)
        rows.append([values.get(i, "") for i in range(width)])

    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        for r in rows:
            writer.writerow(r + [""] * (widest - len(r)))

    return len(rows), widest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--sheet", type=int, default=1)
    args = ap.parse_args()

    out = args.out or args.xlsx.with_suffix(".csv")
    n_rows, n_cols = convert(args.xlsx, out, args.sheet)
    print(f"{out}  —  {n_rows} rows x {n_cols} cols")


if __name__ == "__main__":
    main()
