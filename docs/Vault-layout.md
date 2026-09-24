# The vault

The scripts work on an `AutoNotes/` folder inside an Obsidian vault:

```
AutoNotes/
├── Import files/               the inbox
├── Library/<doc>/              one folder per document (identity = the PDF's sha256)
│   ├── <doc>.pdf
│   ├── <doc>.md                the card
│   ├── <doc>.text.md           full text
│   └── <doc>.data.json         typed data
├── Papers/                     research papers (<first-author>-<year>-<topic>), same sidecars
├── Topics/                     your summary / comparison pages, with embedded queries
├── Parametrics/                vendor parametric exports used for enrichment (.xlsx / .csv)
├── Views/                      Obsidian Bases views (templates in views/)
├── .filing-rules.json          optional: file-name / page-1 patterns → card category
├── Import log.md               what each ingest pass did
└── autonotes.sqlite            derived; gitignore it
```

Scripts find the vault by, first hit wins: `--vault PATH`, the `AUTONOTES_VAULT` environment variable, a `.autonotes-vault` file beside the scripts holding the path, then conventional locations (`~/AutoNotes`, `~/Documents/AutoNotes`, one folder below either). Any of `AutoNotes/` or its parent is accepted. The older folder-per-category layout (`AutoNotes/Reference Material/`) is still recognised, for migration.

**A new vault:** create `AutoNotes/Library/` and `AutoNotes/Import files/`, copy `views/*.base` into `AutoNotes/Views/`, and drop PDFs into the inbox.

**An existing folder-per-category vault** (`Reference Material/<Category>/attachments/`): see [Migration](Migration.md).
