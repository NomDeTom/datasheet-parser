# Running it

## The ingest: one command for everything

```bash
export AUTONOTES_VAULT=/path/to/AutoNotes DOCLING_PYTHON=~/docling-venv/bin/python
python ingest.py              # process the inbox
python ingest.py --dry-run    # say what would happen
python ingest.py --refresh    # absorb newer Docling/OCR output into existing documents
python ingest.py --nightly    # the deferred long documents, then a full check
```

One pass:

1. **settle** — waits until each PDF has stopped growing (sync tools write in pieces) and ignores their temporary files;
2. **file** — `file_pdfs.py plan` → `apply`, journalled:
   - byte-identical to a filed PDF → renamed `DUPLICATE - <name>`, left in the inbox;
   - same document, not newer, different bytes → renamed `REVIEW - <name>` for a person (remove the prefix to retry);
   - otherwise → `Library/<clean name>/`, with a starter card;
3. **extract** — Docling for documents up to `INGEST_MAX_PAGES` (default 250; longer ones get pypdf text now and Docling in the nightly run), OCR for documents without a text layer, pdfplumber / pdftotext only if Docling is unavailable;
4. **build** — `sidecars.py` and `cards.py` for the new documents, `build_db.py`, `verify_library.py`;
5. **refresh** — documents whose Docling cache has grown since their sidecars were built;
6. **report** — a dated block in `Import log.md`.

Only one pass runs at a time; a pass repeats while files keep arriving. Plans, journals and run logs live in `$XDG_STATE_HOME/autonotes-ingest/`, not in the vault.

## Automatically, when a file lands

`systemd/` holds user units (Linux, or WSL with `[boot] systemd=true`):

| Unit | Does |
|---|---|
| `autonotes-ingest.path` | runs a pass when anything changes in the inbox (inotify) |
| `autonotes-ingest.timer` | 2 min after boot, then every 30 min — missed events, files present at boot |
| `autonotes-nightly.timer` | 03:00, `Persistent=true` — deferred long documents, full check |

```bash
cp systemd/ingest.env.example ~/.config/autonotes/ingest.env    # edit AUTONOTES_VAULT, DOCLING_PYTHON
systemd/install.sh                    # install, enable, start (re-run after editing)
systemd/install.sh --remove
loginctl enable-linger $USER          # run without a login session
```

Watch a Linux filesystem, not a Windows drive mounted into WSL (`/mnt/…`): inotify does not work over 9p.

## Step by step

Every stage is also a script, safe to re-run:

| Script | Does |
|---|---|
| `file_pdfs.py plan <files/dirs> -o plan.tsv` | propose filing: identity by sha256, clean names, newer versions, category; writes an editable plan, touches nothing |
| `file_pdfs.py apply plan.tsv [--skip-undecided]` | execute a reviewed plan; refuses if anything changed since; journals every move |
| `file_pdfs.py undo journal.json` / `check` | reverse a filing / audit the vault's names and links |
| `docling_extract.py PDF… [--ocr] [--pages A-B]` | Docling conversion in 20-page chunks, resumable, cached by content hash in `output/docling/<sha>` (`--ocr`: separate cache) |
| `sidecars.py [--pdf PDF]…` | write `.text.md` + `.data.json` |
| `cards.py [--doc NAME]…` | write the Obsidian cards |
| `build_db.py` | rebuild `autonotes.sqlite` |
| `verify_library.py [--min-registers N]` | the gate: exits non-zero on any failure |
| `papers.py inventory / extract / outline / skeleton / render` | research papers: metadata, text, outline, a skeleton reading note, page images |
| `trm.py index / toc / find / regs / pins` | page-indexed search in long reference manuals |
| `migrate_layout.py plan / apply / undo` | move a folder-per-category vault to the Library layout |
| `eval/compare_readers.py` | measure Docling against pdfplumber / pdftotext on your library |
