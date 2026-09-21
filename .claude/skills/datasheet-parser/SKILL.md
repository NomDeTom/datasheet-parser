---
name: datasheet-parser
description: How to run the datasheet-parser scripts (<checkout>) to pull structured data — device info, register maps, electrical characteristics, pinouts — out of a PDF datasheet. Use whenever a task needs facts (pin functions, register addresses, elec specs) sourced from a datasheet PDF rather than guessed from memory.
---

# datasheet-parser

Full detail lives in `README.md` in this folder — read it for the vault pipeline
(`twin_notes.py` etc.) and the accuracy/precision numbers. This skill is the short version:
which script to reach for, and the one real gap to know about before trusting pin output.

## Setup (once per machine)

```bash
python -m pip install pdfplumber pypdf click
```

`pdftotext` (poppler) must also be on PATH — ships with Git for Windows in `mingw64/bin`.
Confirm with `which pdftotext`. No `AUTONOTES_VAULT` setup is needed for plain extraction —
that's only for the vault-note pipeline (stage 2+ of the README).

Drop the target PDF into `input/datasheets/` (or `input/manuals/` for a TRM) — or point a script
at any path directly. `python batch.py` runs whatever is in `input/` with the right tool per
subfolder and moves the originals out to `--dest` or `processed/<type>/`; see README "The inbox".

## Which script for which question

| Question | Script |
|---|---|
| Device info / Vin-Vout / interfaces (small analog/power ICs) | `python parse.py datasheet.pdf` |
| I²C register map with bit fields | `python parse.py datasheet.pdf --registers-only` |
| A claim made in prose ("up to 36 V input") | `python textspec.py datasheet.pdf` |
| Anything in a **huge** doc (MCU datasheet, vendor TRM, 50+ pages) | `python trm.py ...` |

For an MCU datasheet (this is the common case for board bring-up), always start with `trm.py`:

```bash
python trm.py index input/manuals/PART.pdf              # one-off, builds output/PART/text.txt
python trm.py toc   input/manuals/PART.pdf --filter pin  # find the pinout chapter/page
python trm.py find  input/manuals/PART.pdf "Pin Definition|Pinout" -C 2
```

## Pin lists: read the real table, don't trust `trm.py pins` blind

`trm.py pins` is built for **single-column index→signal tables** (e.g. Rockchip's IOMUX
list: `12  SPI0_CLK`, one pair per row). It matches on a bare `\d+\s+[A-Z_]+` regex and picks
the densest page(s).

**It silently mis-parses multi-package pin tables** — the kind WCH, ST, etc. print where one
row is a *signal* and there are 5 separate pin-number columns (one per package variant:
QFN20 / LQFP32 / QFN32 / LQFP48 / ...). The regex has no idea which number belongs to which
package column, so it grabs whichever digit run comes first per line and reports it as if it
were a single sequential index. Confirmed on `CH32V203DS0.PDF`: it produced entries like pin 17
mapped to two different signals, and dropped NRST/OSC_IN/OSC_OUT/VDDA entirely (they're marked
`-` in the columns nearest to whichever page it anchored on).

**What actually works for that case: pull the table with pdfplumber directly, and prefer the
physical pinout *diagram* over the multi-package table when both exist.**

```python
import pdfplumber
with pdfplumber.open("input/manuals/PART.pdf") as pdf:
    page = pdf.pages[N]                 # 0-indexed; toc/find above give 1-indexed page numbers
    tables = page.extract_tables()
    for t in tables:
        print(len(t), len(t[0]))        # sanity-check row/col count before trusting it
```

Two gotchas found doing this on CH32V203:

1. **Rotated column headers extract reversed.** A vertically-printed header like `QFN20`
   comes back as the literal string `02NFQ`. Decode by eyeballing `header[::-1]`, don't assume
   column order from position alone — print the header row first.
2. **When a datasheet gives both a physical pin-diagram (words positioned on a package outline)
   and a pooled multi-package pin-definition table, the diagram is the more trustworthy source
   for one specific orderable part.** The pooled table exists to cover a whole family in one
   grid and is more prone to the vendor's own transcription slips (this is the same class of
   issue the main README documents for TI parts under "vendor prose cross-references can be
   wrong"). Get the diagram's page with `page.extract_words()`, cluster by `top` (y-position)
   into rows, and read pin number / label pairs directly — it's plain text even though it's
   laid out as a picture, unless the PDF rasterized it (check word count > 0 first).

Cross-reference the two: the diagram gives you the authoritative pin-number → label mapping
for your exact part; the pooled table (matched by base signal name, e.g. `PA0`) fills in the
full alternate-function / remap-function detail that the diagram abbreviates.

## Worked example

`CH32V203F8U6` (QFN20) pin list was built this way — table pages found via
`trm.py find ... "Pin Definition"`, diagram located on the same page as the part number
(`trm.py find ... "F8U6"`), then extracted per the method above. Output:
`output/CH32V203DS0/ch32v203f8u6_pins.json`.
