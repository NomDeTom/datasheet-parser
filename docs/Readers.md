# How it reads a PDF

Docling is the primary reader. The others are either always-on helpers or fallbacks used only where Docling is not installed or has not reached a document.

| Reader | Role | Cost | Needed? |
|---|---|---|---|
| **[Docling](https://github.com/docling-project/docling)** (docling-parse, RT-DETRv2 layout, TableFormer tables) | text in reading order, headings, tables with their columns, register maps, page-1 facts, prose claims, paper metadata | 5–8 s/page on CPU, ~2 GB RAM, ~2.2 GB install | recommended |
| **RapidOCR** (via Docling, full-page) | text for scans, image-tile sheets, text drawn as outlines | ~20 s/page | only for PDFs without a text layer |
| **pypdf** | every page, fast: fallback text, cross-check reader, text-layer probe, filing hints | seconds | core |
| **pypdfium2** | page rendering, page counts, light-tier paper reading | seconds | core (comes with pdfplumber) |
| **pdfplumber** (`parse.py`) | tables, page-1 facts, registers — **fallback only** | ~30 s/PDF | core, for installs without Docling |
| **pdftotext** (poppler) | prose claims — **fallback only** | ~0.2 s/PDF | optional external binary |

Why Docling first — measured on this library, 2026-09-24 (`eval/compare_readers.py`):

| | pdfplumber / pdftotext | Docling |
|---|---|---|
| Register maps (first 6 register datasheets) | 47 registers, **27 with an address**, 178 fields | 50 registers, **43 with an address**, same 178 fields |
| Page-1 Vin/Vout/Iout vs the vendor's export | 71 exact · 16 wrong · 39 missing | 68 · 17 · 41 (a draw) |
| Prose claims vs the vendor's export | precision 97 %, recall 49 % | precision 97 %, recall 54 % |
| Electrical tables | **shifts TYP into MIN** when the MIN column is empty (23 % of shared rows; checked on rendered pages) | reads those rows correctly |
| Non-TI layouts | 0 rows on many sheets | reads them |

With Docling absent the pipeline still runs end to end on pypdf, pdfplumber and pdftotext — fewer and weaker table rows, and `extraction.readers` in each `.data.json` says so, so confidence follows evidence behind it.

## Determinism

Docling does not currently pin its models concretely. A given Docling version fixes its code and the table model (TableFormer, tag `v2.3.0`), but the layout model is fetched at revision `main` on first use, so two installs of the same version can read the same PDF differently. **Results are therefore not guaranteed to be deterministic across installs or over time.** On one machine, conversions are repeatable once the models are cached (keep `~/.cache/huggingface`, set `HF_HUB_OFFLINE=1`); each conversion's manifest records the Docling version used. pypdf, pypdfium2, pdfplumber and pdftotext have no models and are deterministic for a given version.

