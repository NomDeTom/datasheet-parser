# Roadmap

1. **Packaging** — `pyproject.toml`, one `autonotes` command, `pip install` extras (`[docling]`, `[ocr]`), `doctor` (what's installed and what each missing piece would add), a per-vault `autonotes.toml`, `init` for a new vault.
2. **Enrichment inbox and document types** — vendor tables dropped into `Import files/` are recognised and applied; per-type profiles (datasheet, manual, app note, errata, paper); documents linked to the parts they concern (`relates_to`).
3. **Market data hook** — a documented format for outside scripts to supply prices and stock (JLC, LCSC) keyed by LCSC code / part number, read into the database and cards.
4. **Portability** — a cross-platform `watch` mode for Windows and macOS; synthetic test PDFs (vendor PDFs can't ship in the repo).
