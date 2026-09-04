# Datasheet Parser

Extracts structured data from PDF datasheets, and maintains the parameter layer of the AutoNotes
vault at `D:\Clod\AutoNotes`.

Two halves that can be used independently:

- **Extraction** — `parse.py` (device info, electrical characteristics, I²C register maps),
  `textspec.py` (parameters claimed in prose), `trm.py` (vendor reference manuals),
  `xlsx_to_csv.py` (vendor parametric exports).
- **Vault pipeline** — `twin_notes.py` → `enrich_ti.py` → `classify.py` → `verify_twins.py`,
  which writes a Markdown "twin" note next to every PDF in the vault carrying machine-readable
  frontmatter for Obsidian Dataview.

---

## Setup

```bash
python -m pip install pdfplumber pypdf click
```

Runs on Windows, macOS and Linux — no hardcoded paths. The vault is located by, first hit wins:

1. `--vault /path/to/AutoNotes/Reference Material`
2. the `AUTONOTES_VAULT` environment variable (`export` / `setx`)
3. a `.autonotes-vault` file beside these scripts, holding the path on line 1
4. conventional locations (`~/Clod/AutoNotes/...`, `~/Documents/Clod/...`, `~/Sync/Clod/...`,
   and `D:`/`C:`/`E:` drive roots on Windows)

Any of the vault root, its parent, or `Reference Material` itself is accepted. A path that does not
contain `Component Index.md` or an `attachments/` folder is rejected with a message rather than
producing a silently empty run.

That is the whole dependency set. `requirements.txt` is **over-specified**: `camelot-py[cv]`,
`opencv-python`, `pandas` and `rich` are never imported by any module, and `camelot-py[cv]`
additionally needs a system Ghostscript install. (`pdfminer.six` is also listed but arrives
transitively with pdfplumber, so it is harmless.) Installing the full file works but pulls a large
unused tree; the three packages above are sufficient for every script here — verified 2026-09-04,
after `parse.py` failed on a missing `pdfplumber` that the old README claimed was already available.

`pdftotext` (poppler, ships with Git for Windows in `mingw64/bin`) is also required — `textspec.py`
shells out to it. Confirm with `which pdftotext`.

Backends, so you know what a given script costs:

| Backend | Used by | Speed |
|---|---|---|
| `pdfplumber` | `parse.py` and everything under `extractor/` | ~30 s per 50-page datasheet |
| `pypdf` | `trm.py` via `extractor/textindex.py` | ~10× faster, plain text only |
| `pdftotext` | `textspec.py` | ~0.2 s per PDF |
| `zipfile` + `xml.etree` | `xlsx_to_csv.py` | instant |

---

## Extraction

### `parse.py` — device info, electrical characteristics, register maps

```bash
python parse.py datasheet.pdf
python parse.py --all                       # every PDF in datasheets/
python parse.py datasheet.pdf --format csv
python parse.py datasheet.pdf --elec-only   # or --registers-only
python parse.py datasheet.pdf --generic     # force the non-TI extractor
```

Writes `output/<pdf_stem>/`:

- `device_info.json` — part number, doc id, packages, Vin/Vout/Iout/frequency, interfaces
- `elec_chars.json` — every specs table found, as `{name, conditions, source_page, specs[]}`
- `registers.json` / `.csv` — I²C registers with bit fields, access, reset values

**Accuracy, measured against a hand-verified set of 13 TI converters:**

| Source | Precision | Note |
|---|---|---|
| `registers.json` | high | Real table structure; the most trustworthy output |
| `elec_chars.json` "Recommended Operating Conditions" | good | Real table structure |
| `device_info.json` page-1 fields | **poor** | Scrapes two-column prose; see below |

`device_info` is the weak one, and predictably so. TI puts Features in a left column and Description
in a right column; pdfplumber's text order interleaves them, so a number from one column gets married
to a label from the other. Concretely, on an 8-part verified subset its Vout fields scored
**2 exact / 1 partial / 5 wrong** — including reporting Vout as identical to Vin on five of five
TPS552xx parts, and reading an 8 A part (TPS55285A) as 3 A. Do not use `device_info` numeric fields
raw; use `fuse.py`.

### `textspec.py` — parameters claimed in prose

The third source, and the one that reads what a datasheet says in sentences: the title line
("TPS55287 36-V, 4-A Buck-Boost Converter"), "has up to 36V input voltage capability", "programmed
from 0.8V to 22V with 10mV step".

```bash
python textspec.py datasheet.pdf [more.pdf ...]
```

Returns a dict of `{key: {value, evidence, pattern}}` plus `_claims` (every hit, including
corroborating and conflicting ones), `_topology` and `_topology_terms`. Each claim keeps the sentence
it came from, so a disagreement can be settled by reading rather than guessing.

**Precision 97%, recall 76%** on the same verified set — when it answers it is almost always right;
when it cannot tell it stays silent, which is what a cross-check needs.

Two implementation details carry that number, and both are non-obvious:

1. **Text is split into column streams before any pattern runs.** `pdftotext -layout` puts both
   columns on one output line, so a regex over a raw line reproduces exactly the interleave bug that
   makes `device_info` unreliable. Splitting on runs of 3+ spaces and bucketing by x-position keeps a
   column a column.
2. **TI's bullet character decodes to `U+FFFD`.** It is a Symbol-font glyph that pdftotext emits as
   bytes that are not valid UTF-8. Until `U+FFFD` was added to the fragment splitter, one "sentence"
   spanned several Features bullets and a Vout pattern happily consumed the Vin bullet's numbers.

### `fuse.py` — reconcile the three sources

Not a CLI; imported by `twin_notes.py`. Takes `device_info`, `elec_chars` and the `textspec` claims
and votes: **any two agreeing beats a lone dissenter**, tie-break prose → ROC table → page-1.

```
all agree / any two agree  -> confidence "high"
single source (prose|ROC)  -> confidence "medium"
single source (page-1)     -> confidence "low"
no two agree               -> most reliable wins, confidence "low", the clash recorded
nothing                    -> None, confidence "none"
```

Plus anomaly flags rather than silent belief: `vout_echoes_vin` (with a fallback to the other source
before discarding), `vin_min_zero_suspect`, `*_range_inverted`, `vout_from_page1_fallback`.

Measured on the verified set: page-1 alone gave 2 exact / 5 wrong; fusion gives **7 exact / 1 partial
/ 0 wrong**.

> `lo == hi` is **not** an inverted range — fixed-output parts (TPS61097A-33 is 3.3 V only) report an
> identical min and max. Only `lo > hi` is impossible. The stricter test silently nulled every
> fixed-output part's Vout.

### `trm.py` — vendor reference manuals

`parse.py` is impractical on a 1000+ page TRM. `trm.py` builds a page-indexed text cache once with
pypdf, then answers queries instantly. Page numbers are real PDF pages.

```bash
python trm.py index Rockchip_RK3506_TRM_Part_1.pdf     # one-off, slow
python trm.py toc   <pdf> --filter spi                 # find the chapter
python trm.py find  <pdf> "RM_IO\d+" -C 2              # search with context
python trm.py regs  <pdf> --filter IOMUX               # absolute register addrs
python trm.py pins  <pdf> --pages 28-29 --signal SPI0  # pin-mux matrix signals
```

`regs` is the useful one for bring-up: it parses vendor address blocks
(`Address: Operational Base(0xFF950000) + offset (0x000C)`) and computes the absolute address, which
is what you need to poke `/dev/mem` and verify a pad's actual mux rather than trusting the device tree.

`pins` auto-detects the densest table when `--pages` is omitted, but that is a heuristic: where a
manual has several dense tables on consecutive pages it will merge them and return wrong indices.
Find the page with `toc`/`find` first and pass `--pages` for anything load-bearing.

Cache lands in `output/<pdf_stem>/text.txt`, reused until `--force`.

### `xlsx_to_csv.py` — vendor parametric exports

```bash
python xlsx_to_csv.py "DC_DC converters-parametrics.xlsx" -o ti_dcdc.csv
```

Reads `xl/worksheets/sheetN.xml` directly. **openpyxl cannot open TI's exports at all** — their
stylesheet raises `ValueError: Colors must be aRGB hex values` and the whole workbook is refused.
It also recovers part numbers from `HYPERLINK("url","LM65440-Q1")` formulas, which have no cached
value: without that, column A reads as empty and you get 1,594 rows with no part numbers.

---

## Vault pipeline

Four stages, in order. Each is idempotent and safe to re-run.

```bash
python twin_notes.py                 # 1. new PDFs only (~30 s/PDF — pdfplumber)
python twin_notes.py --from-cache    #    re-render all 207 from cached JSON (~5 s)
python enrich_ti.py --csv-dir <dir>  # 2. overlay vendor parametric exports
python classify.py                   # 3. product type, topology, manufacturer
python verify_twins.py               # 4. check the result; non-zero exit on failure
```

> [!] **Stages 2 and 3 must re-run after any `twin_notes.py` regeneration.** `--force` and
> `--from-cache` rewrite frontmatter from the parse, discarding what `enrich_ti.py` and
> `classify.py` added. `twin_notes.py` now prints a reminder, and `verify_twins.py` reports
> "(nothing enriched yet)" if you forget.

### 1 · `twin_notes.py`

Writes `<folder>/attachments/<stem> (datasheet).md` beside every PDF, with frontmatter holding
SI-normalised bare numbers (`vin_max: 36.0`, `freq_max_khz: 2200.0`) so Dataview sorts and filters
correctly, plus a body with the key-spec table, register map and elec-char section index.

**The ` (datasheet)` suffix is deliberate.** Obsidian resolves `[[wikilinks]]` by basename across the
whole vault, and 35 PDF stems already collide with hand-written notes (`bq25601.pdf` ↔ `BQ25601.md`).
On a case-insensitive filesystem, bare `<stem>.md` twins would make 35 existing links ambiguous.

It also writes **`<stem>.registers.json`** beside each PDF for any part with an extracted register
map — currently 24 files, 415 registers, 551 KB. The note's table keeps only addresses, names and
field *names*; the sidecar keeps bit ranges, access, per-field reset values and the enum tables
inside each field description. Before this, that detail existed only in gitignored `output/`, outside
the vault and therefore unsynced and unbacked-up. The writer never deletes: if a part yielded
registers once and a later parse yields none, the existing sidecar is the better data.

`--from-cache` re-renders from `output/<stem>/*.json` without touching a PDF: **5 seconds for 207
notes, against 105 minutes (6,324 s) to parse them.** Allow ~25 s on the first run after a
`textspec.py` change, when the prose caches are rebuilt via pdftotext. Use it after any change to
`fuse.py`, `textspec.py` or the note template. It self-heals a cache written before a new key
existed (it checks for `_topology`), so a stale cache cannot silently omit new fields.

```bash
python twin_notes.py --only tps552   # substring filter
python twin_notes.py --max-mb 12     # larger PDFs get a stub note, not a parse
```

### 2 · `enrich_ti.py`

Overlays vendor parametric exports. **Columns are matched by header name, never by position** — the
four TI categories share only 7 columns (DC/DC has Vin/Vout/topology, battery-management has cell
chemistry and charge current, digital power monitors have common-mode range and ADC bits, amplifiers
have almost nothing numeric). Any column without a canonical mapping still renders into the note body
marked *(unmapped)* rather than being dropped.

Vendor data wins over PDF extraction (99% fill on Vin/Vout vs ~70% accuracy from the document), so a
match sets `source: ti-parametric`, `verified: true`.

**But it is not gospel.** Verified against the PDFs, the disagreements split three ways:

| Kind | Example | Trust |
|---|---|---|
| Vendor database error | TPS55287 field says Vin max 30; the same row's Description says "36V 4A buck-boost" and the datasheet says 36 twice | **datasheet** |
| Selector-only derated figure | TPS55288 Vout max 21.26 V; "21.26" appears nowhere in its 53-page datasheet, which says 22 V | both, different meanings |
| Bad PDF extraction | TPS54202 extracted as Vout 0.1–7 V; page 1 states no Vout range at all | **export** |

So no single rule holds. A conflicting PDF value is always kept as `datasheet_<field>` with a
`ti_datasheet_mismatch` flag, and `ti_overrides.json` carries vetted corrections applied *after* the
export — each requiring recorded evidence and a check date, so a refreshed export cannot silently
reinstate a known-bad value. Overridden parts keep the vendor's figure as `ti_published_<field>`.

> Mismatch *detection* only fires the first time an export is applied, since afterwards the primary
> field already holds the vendor value. Flags and `datasheet_*` persist, but the console count drops
> on re-runs — read flag totals from `verify_twins.py`, not the tail line here.

### 3 · `classify.py`

Writes `product_type`, boolean `is_*` fields, `topology_class`, `manufacturer`, and `converts_voltage`.

Without this, every query filters on "has a voltage range", so an ATtiny (supply 1.8–5.5 V) appears
beside buck-boost converters. `converts_voltage` is the umbrella — true for converters, chargers,
LDOs, power-bank SoCs and MPPT parts; false for MCUs, connectors, discretes, amplifiers, app notes.

Signals in descending order of trust, with `classified_by` / `topology_by` / `manufacturer_by`
recording which decided:

1. **Vendor export category** (`ti_category`, `ti_subcategory`, `Function`)
2. **The curated section index** — `#Datasheets.md` in the vault already sorts 283 datasheet names
   into hand-written sections (Buck / Boost / Buck-Boost / Protection / LDOs / MOSFETs / …). That is a
   human classification of exactly this corpus, and it carries topology too. It settles ~98 of 207
   notes and cut `unknown` from 32 to 4.
3. **Vault folder** (`MCUs/`, `Sensors/`, `Comms/`, …)
4. **Title/description keywords**

Topology booleans: `is_buck`, `is_boost`, `is_buck_boost`, `is_linear`, `is_switching`.
**`is_buck` and `is_boost` are both true for a buck-boost** — it genuinely does both, and "can this
step my rail down?" has to catch them.

> **`steps_down` is not `is_buck`.** A linear regulator steps down too, so `vin_min > vout_max`
> cannot distinguish a buck from an LDO. The range fact is exposed as `steps_down` and never promoted
> to a topology claim. `linear` is a topology in its own right, not the absence of one.

Non-TI parts get topology from the prose via `textspec`, which covers the Consonance solar chargers,
Injoinic power-bank SoCs and the LDO clones — 89 of 97 converting parts now have a topology, from 75.

### 4 · `verify_twins.py`

Reports, and exits non-zero if a structural check fails: one twin per PDF, no basename collisions
with curated notes, frontmatter parses, `source_pdf` resolves, no impossible ranges, and every
enriched value cross-checked against its source export (deliberate overrides excluded).

Confidence and flag distributions are informational — a low-confidence note is the system working.

---

## Gotchas worth knowing

- **`rglob` is case-insensitive on Windows.** Globbing `*.pdf` *and* `*.PDF` and concatenating lists
  every file twice. Both `twin_notes.py` and `verify_twins.py` dedupe by lowercased path; without it
  the progress denominator reads 414 for 207 files.
- **Some equations and figures are images, not text.** BQ25798 §10.2.2 is the worked example: every
  formula is a rendered image, so `pdftotext` returns only the numbers `(4)`–`(10)` and pdfplumber
  finds no tables. There is no parsing route — render the page and read it
  (`page.to_image(resolution=200)`), which is how `BQ25798 Design Equations.md` was produced.
- **Vendor prose cross-references can be wrong.** Every in-text equation reference in BQ25798
  §10.2.2 points at the equation *before* the one it means. Trust the number printed beside the
  equation, not the sentence citing it.
- **`D` is not one quantity.** In BQ25798 the buck equations use `D = V_SYS/V_BUS` and the boost
  equations use `D = 1 − V_BUS/V_SYS`. Reusing one duty cycle is wrong in one mode.
- **Filesystem case behaviour differs.** `rglob('*.pdf')` matches `.PDF` too on Windows but not on
  Linux, so both patterns are globbed and then deduped (`vaultpath.find_pdfs`). Comparison keys use
  `os.path.normcase`, not `.lower()` — blanket lowercasing would merge `Foo.pdf` and `foo.pdf`,
  which are two distinct files on a case-sensitive filesystem.
- **Generated files are written with LF endings on every platform** (`vaultpath.write_text`).
  Python's default would emit CRLF on Windows, so a vault synced between machines churned every
  generated file on each regeneration.
- **Don't write patch scripts with regex in heredocs.** `\\b` collapses to a literal backspace before
  Python sees it, so string matches fail with no useful error. Edit the file directly.

## Layout

```
vaultpath.py        vault/tool discovery, case-safe paths, LF writes  (import first)
parse.py            device info + elec chars + registers   (pdfplumber)
textspec.py         parameters + topology claimed in prose (pdftotext)
fuse.py             three-source voting, confidence, flags (imported)
trm.py              page-indexed search for large TRMs     (pypdf)
xlsx_to_csv.py      vendor .xlsx -> CSV, bypassing openpyxl

twin_notes.py       stage 1 — generate/refresh twin notes
enrich_ti.py        stage 2 — overlay vendor parametric exports
ti_overrides.json   vetted corrections to vendor data, with evidence
classify.py         stage 3 — product type, topology, manufacturer
verify_twins.py     stage 4 — structural checks, non-zero exit on failure

extractor/          device_info, elec_chars, i2c_registers, generic, textindex, output
output/<stem>/      device_info.json, elec_chars.json, registers.json, textspec.json, text.txt
probe*.py           one-off exploration scripts; not part of any pipeline
check*.py           ad-hoc verification scripts; superseded by verify_twins.py
lr11xx_sensitivity.py, sx1276_sensitivity.py   part-specific sensitivity-table extractors
```

The vault-side conventions these scripts follow are documented in
`D:\Clod\AutoNotes\TASK - Datasheet Organisation.md`; the generated tables live in
`Reference Material\Datasheet Parameter Tables.md` and `Reference Material\TI Parametrics\`.
