---
name: paper-importer
description: >
  Extract research papers (PDFs) dropped into a vault folder into one structured
  Obsidian note per paper plus a folder index. Use when the user says to extract,
  import, read, or summarise papers, or points at a folder of PDFs in the vault.
  Sibling of the datasheet-parser skill in this repo; same shape, different
  fields. The mechanical part (inventory, text sidecars, outline, skeleton
  note, page render) is papers.py; the reading is done by hand from the text.
---

# Paper importer

Turns a folder of PDFs into notes that answer three questions per paper: what it
claims, which numbers are worth lifting, and what it means for the project the
folder belongs to. Follows the `vault-notes` conventions for placement and indexes.

## Environment

| | |
|---|---|
| Interpreter | the repo `.venv` (see README "Setup"); `papers.py` additionally needs `pymupdf` |
| Script | `papers.py` at the repo root (`~/datasheet-parser` on WSL, `<checkout>` on Windows) |
| Inbox | `input/papers/` in the repo, when staged ahead; `batch.py --type papers --dest <vault folder>` runs steps 1–2 and moves the PDFs into the vault folder |
| Papers | `<project>/YYYY-MM-DD-<topic>/*.pdf` — the vault folder the notes live in; PDFs already there are processed in place and not moved |
| Text sidecars | `<same folder>/text/<stem>.txt` via `-o`; without `-o` the cache goes to `output/<stem>/text.txt` like `trm.py` |
| Notes | `<same folder>/<author>-<year>-<slug>.md` |
| Folder index | `<same folder>/YYYY-MM-DD-<topic>.md` |

If pymupdf is missing: `pip install pymupdf` into the repo venv.

## Workflow

### 1. Inventory

```
papers.py inventory <folder>
```

One block per PDF: pages, text layer or SCANNED, year, DOI / arXiv id, title guess.
Anonymised submissions ("Author 1, Author 2") are identified from their title via
another paper's reference list or the venue metadata; say in the note how.

### 2. Extract

```
papers.py extract <folder> -o <folder>/text
```

Column-aware reading order, hyphenation folded, `=== PAGE n ===` markers (trm.py's, so
`trm.py find <pdf> PATTERN` searches a paper once extracted). Equations
do not survive; when a note needs one, cite the equation number and page and read it
off the PDF (`papers.py render <pdf> <page>` for a PNG). Known noise: running
headers, figure axis ticks, references bleeding into the last section.

`papers.py outline <pdf>` prints the numbered headings — a reading aid, unreliable
on two-column IEEE layouts.

### 3. Anchor the relevance first

Before reading, grep the project for the constants and rules the papers are likely to
touch and note file:line. Every note's **Relevance** line and "What it means" section
points at code, not at a topic.

### 4. Read and write one note per paper

`papers.py skeleton <pdf> -o <note>.md` gives the header and abstract; the rest
is written from the sidecar. Shape:

```markdown
# <First author> et al. <year> — <short title>

**Authors:** …
**Venue:** <journal/conference, volume, pages, year, doi or arXiv>
**Source:** [[<pdf stem>]] (<n> pp) · text sidecar `text/<stem>.txt`
**Read on:** <date>, against <repo> `<commit>`
**Relevance:** HIGH | MEDIUM | LOW — one sentence naming the file:line it bears on

---

## What it claims
## Numbers worth lifting
## What it means for <project>
```

Rules: pin every figure to its table/figure/section; state the measurement setup
(chip, channel, packet size, threshold definition) next to any threshold; a LOW
paper gets a short note saying why, not no note; never invent a number the text
does not contain.

Filename: `<firstauthor>-<year>-<slug>.md`, lowercase, unique across the vault.

### 5. Synthesis note (when the folder has a theme)

One note collecting the numbers across papers in the form the project would consume
them (a table keyed the way the code is keyed), the constants they contradict, what a
change would look like in order of cost, and which cited papers to fetch next.

### 6. Indexes

Folder index: a line on what the folder is for, then one dated entry per note,
newest last. Project index: one row for the folder. Both by hand (`vault-notes`).

## Handling

- **Scanned PDF**: `render` the pages and read visually; mark the note
  `Scanned — extracted visually; verify against original`.
- **Equations needed**: read off the rendered page, cite the equation number.
- **Long paper (> 20 pp)**: `outline` it, read the sections the project needs in full,
  say in the header which sections were read and which skimmed.
- **Existing note for the paper**: extend it; do not create a second.
