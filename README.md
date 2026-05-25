# Datasheet Parser

Extracts structured data from PDF datasheets, starting with I2C register maps.

## Setup

```bash
pip install -r requirements.txt
```

> Note: `camelot-py[cv]` requires Ghostscript installed on the system.
> Download from https://www.ghostscript.com/releases/gsdnld.html

## Usage

```bash
# Parse a single PDF
python parse.py datasheet.pdf

# Parse all PDFs in the datasheets/ folder
python parse.py --all

# Output as JSON
python parse.py datasheet.pdf --format json

# Output as CSV (one file per register table)
python parse.py datasheet.pdf --format csv
```

## Output

Results are written to `output/<device_name>/`:
- `registers.json` — all I2C registers and their fields
- `registers.csv`  — flat table of all registers
