# Repository layout

```
ingest.py              the whole pipeline for the inbox (systemd runs this)
file_pdfs.py           filing: plan / apply / undo / check
docling_extract.py     Docling conversion, chunked, resumable, content-addressed cache
sidecars.py            .text.md + .data.json
cards.py               Obsidian cards
build_db.py            autonotes.sqlite
verify_library.py      checks
migrate_layout.py      folder-per-category → Library layout
enrichment.py          vendor tables → readings (TI parametric adapter); ti_overrides.json
fuse.py                page-1 / ROC / prose voting, anomaly flags
classify.py            product type, topology, manufacturer
papers.py              research papers (on extractor/paper_source.py)
trm.py                 search and register addresses in long reference manuals
vaultpath.py           vault discovery, case-safe paths, LF writes

extractor/
  docling_tables.py      Docling grids → typed rows
  docling_registers.py   register maps from Docling
  docling_device_info.py page-1 facts from Docling (same rules as device_info.py)
  docling_prose.py       prose claims from Docling text (textspec.scan)
  paper_source.py        paper metadata and text (Docling; pypdfium2 fallback)
  device_info.py, elec_chars.py, i2c_registers.py, generic.py   pdfplumber readers (fallback)
  textindex.py           trm.py's page-indexed text cache

parse.py, textspec.py  pdfplumber / pdftotext fallback readers
xlsx_to_csv.py         read .xlsx without openpyxl
tools/                 dev scripts, not part of the pipeline:
  dump_page.py           every text line and table header on one page (pdfplumber)
  spec_regex.py          run extractor/device_info.py's spec regexes against a PDF
  lr11xx_sensitivity.py, sx1276_sensitivity.py   one-off LoRa sensitivity tables → markdown
eval/compare_readers.py   reader comparison on your library
systemd/               unit templates, install.sh, ingest.env.example
views/                 Obsidian Bases view templates
wsl/pdftotext          WSL shim around Windows poppler
.claude/skills/        agent skills: datasheet-parser (which tool for which question),
                       paper-importer (reading a paper into a vault note)
```

**Legacy**, kept for vaults still on the twin-note layout and not part of the pipeline above: `twin_notes.py` (its `--cache-only` parse driver is the pdfplumber fallback). `input/datasheets/` is the default folder for `parse.py --all`.
