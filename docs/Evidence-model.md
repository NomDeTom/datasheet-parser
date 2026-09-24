# What a value means: readings, confidence, verification

`.data.json` (schema `autonotes.data/2`) keeps the evidence, not only the verdict:

```json
{"key": "vin", "min": 3.0, "max": 36.0, "unit": "V",
 "readings": [
   {"source": "page1", "extractor": "docling", "page": 1, "min": 3.0, "max": 36.0},
   {"source": "roc",   "extractor": "docling", "page": 5, "min": 3.0, "max": 36.0},
   {"source": "prose", "extractor": "docling", "page": 1, "min": 3.0, "max": 36.0},
   {"source": "ti_export", "file": "TI DC-DC converters 2026-09-04.xlsx", "min": 3.0, "max": 30.0}],
 "confidence": "medium", "contested": true,
 "verified": {"by": "ti_overrides.json", "on": "2026-09-04", "evidence": "…", "value": [3.0, 36.0]}}
```

- **Confidence measures corroboration** and is automatic. Sources are *independent statements*: page 1, the Recommended Operating Conditions table, the prose, an external table. Two extractors reading the same table are one source read twice, not two votes.

  | level | meaning |
  |---|---|
  | `high` | two or more independent sources agree |
  | `medium` | one source, read cleanly |
  | `low` | sources disagree, or the reading needed a repair |
  | `none` | nothing found |

- **Contested** — independent sources disagree after the readings themselves are trusted. The value shown is the document's own if it corroborates itself, else the external table's; every reading is kept either way.
- **Verified** — a person checked it against the PDF. Nothing automatic sets it. Add `verified: [vin, vout]` (and `verified_on`, `verified_note`) to a card's properties; the value is snapshotted, and if a later extraction disagrees the verified value still shows and the parameter is flagged `verified_value_changed`. Entries in `ti_overrides.json` count as verification because each records its evidence and a check date.
- **No page, no value.** A value with neither a page nor an external file behind it is not stored.

Table rows (`spec_rows`) carry the same fields. Rows Docling had to repair — a merged cell split into sub-rows, a unit restored, a recognised (OCR) page — are `low` and flagged.
