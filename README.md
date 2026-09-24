# datasheet-parser

Turns electronics PDFs — datasheets, application notes, user manuals, errata, research papers — into a searchable, queryable reference library inside an [Obsidian](https://obsidian.md) vault.

Drop a PDF into the vault's `Import files/` folder and the pipeline files it, reads it, and writes beside it:

- **`<doc>.text.md`** — the full text, one `=== PAGE n ===` marker per page, so a `grep -n` hit is a page number. Tables kept as tables, figure labels included.
- **`<doc>.data.json`** — typed, database-ready data: key parameters, electrical-characteristics rows, register maps, paper metadata. Every value carries the page it came from, every reading that produced it, and a confidence level.
- **`<doc>.md`** — an Obsidian card: generated properties for Bases / Dataview, and a `## Notes` section that is yours and is never overwritten.

and rebuilds **`autonotes.sqlite`**, a derived database (values, readings, registers and a full-text index of every page) for SQL queries.

The PDF stays the authority. Everything the pipeline writes is evidence about it, labelled with where it came from.

## What it's for

A tool someone else can download, point at an Obsidian vault, and leave running:

- **Drop files in, get a library out.** PDFs placed in the vault's inbox are filed, read and turned into notes, text and data without further steps — by hand, or automatically whenever a file lands.
- **Choose the tooling you can tolerate.** Docling gives the best reading but is heavy (a ~2.2 GB install, ~2 GB RAM, seconds per page); without it the pipeline still runs on lighter readers, and every value records which reader produced it, so confidence stays honest.
- **More than datasheets.** Application notes, user manuals, errata and research papers go through the same inbox, each with the sidecars that suit it, and are linked to the parts they concern.
- **Enrichment from outside tables.** Vendor parametric exports (TI's selector today) are parsed into the same data files as extra evidence — corroborating or contesting what the PDF says, never silently overwriting it.
- **Hooks for live data.** Places where outside scripts can supply things that change — prices and stock from JLC or LCSC — keyed by the part numbers the library already holds.
- **Evidence, not answers.** Every value keeps the page it came from and every reading behind it; agreement between independent sources is corroboration, and only a person can mark a value verified.

**Where it stands:** the pipeline, the ingest automation (systemd), Docling / OCR reading, TI enrichment and the Obsidian side work and are in daily use on one library (~230 documents). Packaging (`pip install` with optional extras, a `doctor` command, a per-vault config file), the enrichment inbox, document-type profiles, the price hook and a cross-platform watcher are planned — see [Roadmap](docs/Roadmap.md).

## Install

Python 3.11+. Two virtual environments, because Docling is large and has its own dependency tree.

```bash
# 1. the pipeline
python -m venv .venv
.venv/bin/pip install pdfplumber pypdf click        # pypdfium2 arrives with pdfplumber

# 2. Docling (recommended) — CPU-only torch; skip the GPU wheels
python -m venv ~/docling-venv
TMPDIR=/var/tmp ~/docling-venv/bin/pip install \
    --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \
    docling
# its OCR engine pulls the GUI build of OpenCV (needs libGL); swap it for the headless build
~/docling-venv/bin/pip uninstall -y rapidocr opencv-python
~/docling-venv/bin/pip install opencv-python-headless

# 3. OCR for scans (optional): RapidOCR without its GUI-OpenCV dependency, plus its backend
~/docling-venv/bin/pip install --no-deps rapidocr
~/docling-venv/bin/pip install onnxruntime

export DOCLING_PYTHON=~/docling-venv/bin/python     # the pipeline re-runs Docling steps under it
```

The first Docling run downloads ~500 MB of layout and table models to `~/.cache/huggingface`.

**Results are not guaranteed to be deterministic across installs.** Docling does not currently pin its models concretely: installing a given Docling version fixes its code and the table model (TableFormer, tag `v2.3.0`), but the layout model is fetched at revision `main` the first time it is needed. Two installs of the same Docling version can therefore get different layout weights, and layout drives what the tables and text come out as. On one machine, runs are repeatable once the models are cached: keep `~/.cache/huggingface` and set `HF_HUB_OFFLINE=1` so nothing is fetched again. Each conversion records the Docling version it was made with (`output/docling/<sha>/manifest.json`).

`requirements.txt` lists the same three packages for step 1 (`.venv/bin/pip install -r requirements.txt`).

**pdftotext** (optional, fallback only): `apt install poppler-utils`, `brew install poppler`, or Git for Windows' `mingw64/bin`. Under WSL without root, `wsl/pdftotext` wraps the Windows binary — `install -m 755 wsl/pdftotext ~/.local/bin/pdftotext`.

## Documentation

| Page | |
|---|---|
| [Readers](docs/Readers.md) | how a PDF is read: Docling first, the other readers, and the measurements behind that |
| [Vault layout](docs/Vault-layout.md) | the folders the scripts expect, and how they find the vault |
| [Running](docs/Running.md) | the ingest pass, systemd automation, and every script |
| [Evidence model](docs/Evidence-model.md) | what a value in `.data.json` means: readings, confidence, contested, verified |
| [Enrichment](docs/Enrichment.md) | vendor parametric exports and vetted overrides |
| [Obsidian](docs/Obsidian.md) | cards, Bases views, search |
| [SQL](docs/SQL.md) | the derived database and example queries |
| [Migration](docs/Migration.md) | moving a folder-per-category vault to the Library layout |
| [Gotchas](docs/Gotchas.md) | what goes wrong with PDFs and with the machine |
| [Repository layout](docs/Repository-layout.md) | what each file is for, and what's legacy |
| [Roadmap](docs/Roadmap.md) | what's planned |

## Licence

[The Unlicense](https://unlicense.org): released into the public domain. See [LICENSE](LICENSE).

The libraries it uses keep their own licences. They are installed separately rather than shipped with this code. Docling, pdfplumber, onnxruntime and RapidOCR are MIT or Apache-2.0; pypdf and pypdfium2 are BSD or Apache-2.0; poppler's `pdftotext`, an optional fallback, is GPL and is only ever run as a separate program.
