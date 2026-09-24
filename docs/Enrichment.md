# Enrichment: vendor tables

Put vendor parametric-selector exports in `AutoNotes/Parametrics/`. Today there is one adapter, **TI's parametric selector** (`.xlsx` as downloaded; TI's stylesheet defeats openpyxl, so the sheet XML is read directly — `xlsx_to_csv.py`). Columns are matched by header name, never by position; every column is kept, mapped or not.

A matching document (by part number) gets the whole export row under `enrichment.ti_export` and `ti_export` readings on its parameters, which then corroborate or contest the document. The export is right more often than extraction (99 % fill on Vin/Vout), but not always — three kinds of disagreement were found, and which side to trust differs:

| Kind | Example | Trust |
|---|---|---|
| vendor database error | TPS55287 export says Vin max 30; its own description and datasheet say 36 | the datasheet |
| selector-only derated figure | TPS55288 Vout max 21.26; the datasheet says 22 | both, different meanings |
| bad extraction | TPS54202 read as Vout 0.1–7 V; TI says 0.6–26 | the export |

`ti_overrides.json` holds vetted corrections (part → field → value, with `why` and `checked`), applied after the export so a refreshed export cannot reinstate a known-bad value. `verify_library.py` checks that every enriched value still matches the export file it cites.

Adapters register in `enrichment.ADAPTERS` (`detect(path)`, `load(path)`); more vendors, distributor catalogues and price feeds are planned.
