# Gotchas worth knowing

**About PDFs**

- **Symbol-font glyphs.** Some vendors store µ, Ω, °, ± as private-use code points (µ = U+F06D): invisible in a terminal, so µA reads as "A". Mapped back everywhere. Also normalised: Greek μ vs micro sign µ, `℃` vs `°C`.
- **Docling runs words together** on tight kerning ("Inputvoltagerange:1.8Vto5.5V"). Detected per text item; that page's pypdf text is used for prose claims and kept as an alternative reading.
- **TI's PARAMETER header spans two columns** (symbol, description); a naive table reader loses one of them.
- **pdfplumber shifts TYP into MIN** where the MIN column is empty.
- **pdftotext** mis-decodes some subset fonts (Semtech: `LoRa` → `-P3B`) and `-layout` shears table rows.
- **Some PDFs have no text layer** — scans, image tiles, or text drawn as vector outlines. Every text reader returns nothing; they are flagged `no_text_layer` and OCR'd if RapidOCR is installed.
- **Some equations and figures are images.** No text route exists — render the page (`papers.py render`) and read it.
- **Vendor prose cross-references can be wrong** (every in-text equation reference in BQ25798 §10.2.2 points at the one before). Trust the number beside the equation.
- **A file's name is not its identity.** Distributor names (`2410121637_Vendor-PART_C2759992`), download suffixes (`(1)`), stock-code-only names (`C28646261.pdf`) — identity is the sha256; names are cleaned on filing and the LCSC code is kept in `document.ids.lcsc`.
- **A paper's DOI is on its first page.** Searching the whole text returns the first *cited* DOI.

**About the machine**

- `/tmp` may be RAM-backed: use `TMPDIR=/var/tmp` for Docling installs and runs.
- systemd user units start with a minimal `PATH`; the units set it (`~/.local/bin` for a pdftotext shim).
- In a unit file, `PathChanged=` takes the path literally — do not escape spaces as `\x20`.
- `rglob('*.pdf')` also matches `.PDF` on Windows but not on Linux: both patterns are globbed and deduplicated by `os.path.normcase`.
- Generated files are written with LF endings everywhere, so a synced vault doesn't churn.
