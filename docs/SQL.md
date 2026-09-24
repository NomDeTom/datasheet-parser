# SQL

```sql
-- converters that take ≥ 24 V in, where the value is corroborated
SELECT doc, min, max FROM params WHERE key = 'vin' AND max >= 24 AND confidence = 'high';
-- what needs a person
SELECT doc, key FROM params WHERE contested AND verified_by IS NULL;
-- why a value is contested
SELECT source, extractor, page, file, min, max FROM readings WHERE doc = 'tps55287' AND key = 'vin';
-- full-text search with page numbers
SELECT doc, page, snippet(pages, 2, '[', ']', '…', 12) FROM pages WHERE pages MATCH 'IQ NEAR polarity';
```

Tables: `documents`, `params`, `readings`, `spec_rows`, `registers`, `register_fields`, `enrichment`, and `pages` (FTS5). Rebuilt from the sidecars in seconds, written atomically; never edit it.
