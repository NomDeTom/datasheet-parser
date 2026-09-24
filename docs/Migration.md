# Migrating from the folder-per-category layout

`migrate_layout.py` moves `Reference Material/<Category>/attachments/…` into `Library/<doc>/`: category becomes a card property, single-part notes merge into their card's `## Notes`, family and comparison notes go to `Topics/`, hand-maintained indexes to `0archive/`, old sidecars (twin notes, `.registers.json`, `text/*.txt`) are deleted once their data is in `.data.json`, byte-identical duplicates collapse to one copy, and every wikilink to anything renamed or merged is rewritten.

```bash
python sidecars.py                                  # new sidecars first (reads the old ones once)
python migrate_layout.py plan -o plan.tsv           # review it; rows marked ? need a decision
python migrate_layout.py apply plan.tsv
python cards.py && python build_db.py && python verify_library.py
python migrate_layout.py undo migrate-journal-*.json --discard-edits   # the escape route
```

Every deleted or overwritten file is backed up beside the journal. Rehearse on a copy first: the same sequence plus `undo` should leave the copy byte-identical.
