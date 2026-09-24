#!/usr/bin/env python
"""
migrate_layout.py — move an AutoNotes vault from folder-per-category to one-folder-per-document.

    python migrate_layout.py plan  [-o migrate-plan.tsv]     # propose; touches nothing
    #   ... review / edit the TSV (the checking step) ...
    python migrate_layout.py apply migrate-plan.tsv          # execute; journal + backups
    python migrate_layout.py undo  migrate-journal-*.json    # the escape route

Target layout, under the vault's `AutoNotes/`:

    Library/<doc>/<doc>.pdf         one folder per PDF, flat; category becomes card metadata
    Library/<doc>/<doc>.md          the card (cards.py regenerates everything above `## Notes`)
    Library/<doc>/<doc>.text.md     full text          } from sidecars.py
    Library/<doc>/<doc>.data.json   typed rows         }
    Papers/                         unchanged
    Topics/                         hand-written family / comparison / design pages
    0archive/                       retired hand-maintained indexes

Plan rows (TSV: action, source, target, category, reason), all paths relative to AutoNotes/:

    move     a PDF or its new sidecars into Library/<doc>/; `category` records the old folder
    merge    a single-part note -> appended under `## Notes` of that PDF's card; source removed
    topic    a family / topic note -> Topics/
    archive  a hand-maintained index -> 0archive/ (Bases views replace it)
    delete   an old sidecar (twin note, .registers.json, text/*.txt) — only once the PDF's
             .data.json exists, because that is where their data now lives; or an exact
             duplicate of a note that already exists elsewhere
    keep     leave the file where it is
    ?        needs a decision — `apply` refuses the plan while any remain

After the file operations, every wikilink to a merged note is rewritten to its card.

Every operation is journalled as it happens; anything deleted or overwritten is copied to a backup
folder beside the journal first. `undo` restores from those, newest first, and verifies hashes
before touching anything.
"""
import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

from file_pdfs import clean_name
from vaultpath import find_vault, write_text

FIELDS = ["action", "source", "target", "category", "reason"]
PARSE_CACHE = Path(__file__).resolve().parent / "output"   # parse.py caches, keyed by PDF stem
TWIN = " (datasheet).md"
INDEX_NAMES = {"component index", "#datasheets", "datasheet parameter tables"}
_LINK = re.compile(r"\[\[([^\]|#]+)(#[^\]|]*)?(\|[^\]]*)?\]\]")


def sha256(p: Path) -> str:
    if p.is_dir():                      # parse-cache folders move whole; identity is the name
        return "dir"
    return hashlib.sha256(p.read_bytes()).hexdigest()


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ── plan ─────────────────────────────────────────────────────────────────────
def pdf_links(md: Path, pdf_names: dict):
    text = md.read_text(encoding="utf-8", errors="replace")
    found = []
    for m in _LINK.finditer(text):
        name = Path(m.group(1).strip()).name.lower()
        if name in pdf_names and pdf_names[name] not in found:
            found.append(pdf_names[name])
    return found


def cmd_plan(args):
    rm = find_vault(args.vault)                # .../AutoNotes/Reference Material
    root = rm.parent                           # .../AutoNotes
    rel = lambda p: p.relative_to(root).as_posix()
    rows = []

    pdfs = [p for pat in ("*.pdf", "*.PDF") for p in rm.rglob(pat) if "Papers" not in p.parts]
    pdfs = sorted(set(pdfs))
    pdf_names = {p.name.lower(): p for p in pdfs}

    # hand-written notes first: a note about exactly one PDF merges into that PDF's card, and
    # names the document when the PDF's own name is only a distributor stock code
    note_rows, doc_name = [], {}
    for md in sorted(rm.rglob("*.md")):
        if md.name.endswith((TWIN, ".text.md")) or "Papers" in md.parts:
            continue
        linked = pdf_links(md, pdf_names)
        if md.stem.lower() in INDEX_NAMES:
            note_rows.append((md, "archive", None, "hand-maintained index; replaced by Bases views"))
        elif len(linked) == 1 and (_names_same_part(md.stem, linked[0].stem) or _opaque(linked[0].stem)):
            if _opaque(linked[0].stem):
                doc_name[linked[0]] = clean_name(md.stem)
            note_rows.append((md, "merge", linked[0], f"single-part note for {linked[0].name}"))
        else:
            note_rows.append((md, "topic", None, f"links {len(linked)} PDF(s)"))

    by_hash = {}
    for p in pdfs:
        by_hash.setdefault(sha256(p), []).append(p)
    for copies in by_hash.values():
        if len(copies) < 2:
            continue
        keep = min(copies, key=lambda q: (q not in doc_name, clean_name(q.stem) != q.stem, len(q.name)))
        for extra in copies:
            if extra is keep:
                continue
            pdfs.remove(extra)
            for f in (extra, *(extra.with_name(extra.stem + s) for s in
                               (TWIN, ".registers.json", ".text.md", ".data.json")),
                      extra.parent / "text" / (extra.stem + ".txt")):
                if f.exists():
                    rows.append(dict(action="delete", source=rel(f), target="", category="",
                                     reason=f"byte-identical duplicate of {keep.name}"))

    for p in pdfs:
        folder = p.parent.parent if p.parent.name.lower() == "attachments" else p.parent
        cat = folder.relative_to(rm).as_posix() if folder != rm else ""
        name = doc_name.get(p) or (p.stem if p.name.startswith(("SUPERSEDED - ", "DUPLICATE - "))
                                   else clean_name(p.stem))
        doc_name[p] = name
        lib = root / "Library" / name
        why = "document" if name == p.stem else f"document, renamed from {p.stem}"
        rows.append(dict(action="move", source=rel(p), target=rel(lib / (name + p.suffix.lower())),
                         category=cat, reason=why))
        for suffix in (".text.md", ".data.json"):
            side = p.with_name(p.stem + suffix)
            if side.exists():
                rows.append(dict(action="move", source=rel(side), target=rel(lib / (name + suffix)),
                                 category="", reason="new sidecar"))
        for old in (p.with_name(p.stem + TWIN), p.with_name(p.stem + ".registers.json"),
                    p.parent / "text" / (p.stem + ".txt")):
            if old.exists():
                rows.append(dict(action="delete", source=rel(old), target="", category="",
                                 reason=f"old sidecar of {p.name}; data now in .data.json"))

    # A "topic" named like a document (ignoring case) is really about that part, and would collide
    # with its card: Obsidian resolves [[IP5310]] by name, so one of the two becomes unreachable.
    by_name = {n.lower(): n for n in doc_name.values()}
    for i, (md, action, pdf, why) in enumerate(note_rows):
        if action == "topic" and md.stem.lower() in by_name:
            name = by_name[md.stem.lower()]
            note_rows[i] = (md, "merge", next(p for p, n in doc_name.items() if n == name),
                            f"named like the document {name}; merged so the names don't collide")

    for md, action, pdf, why in note_rows:
        if action == "archive":
            tgt = root / "0archive" / md.name
        elif action == "merge":
            tgt = root / "Library" / doc_name[pdf] / (doc_name[pdf] + ".md")
        else:
            tgt = root / "Topics" / md.name
        rows.append(dict(action=action, source=rel(md), target=rel(tgt), category="", reason=why))

    # whole folders that keep their shape, one level up
    for name in ("Papers", "Attachments"):
        if (rm / name).is_dir():
            rows.append(dict(action="move", source=rel(rm / name), target=name, category="",
                             reason="folder, unchanged inside"))
    # old sidecars no PDF claims (e.g. a superseded document's twin), and papers' .txt sidecars
    claimed = {r["source"] for r in rows}
    for old in sorted([*rm.rglob("*" + TWIN), *rm.rglob("*.registers.json"),
                       *rm.glob("**/attachments/text/*.txt")]):
        if rel(old) in claimed:
            continue
        if old.suffix == ".txt" and "Papers" in old.parts:
            pdf = old.parent.parent / (old.stem + ".pdf")
            if not pdf.with_name(pdf.stem + ".text.md").exists():
                continue                      # no replacement yet: keep
        rows.append(dict(action="delete", source=rel(old), target="", category="",
                         reason="orphan old sidecar"))
    claimed = {r["source"] for r in rows}
    for f in sorted(q for q in rm.rglob("*") if q.is_file()):
        r_ = rel(f)
        if r_ in claimed or f.name == ".filing-rules.json" or f.suffix.lower() in (".pdf", ".md") \
                or any(part in ("Papers", "Attachments") for part in f.relative_to(rm).parts[:1]):
            continue
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".html", ".htm"):
            rows.append(dict(action="move", source=r_, target=f"Attachments/{f.name}", category="",
                             reason="embedded asset; notes find it by name"))
        elif f.suffix.lower() in (".csv", ".xlsx", ".xls"):
            rows.append(dict(action="move", source=r_, target=f"Parametrics/{f.name}", category="",
                             reason="vendor parametric export"))
        else:
            rows.append(dict(action="?", source=r_, target="", category="", reason="other file: decide"))
    if (rm / ".filing-rules.json").exists():
        rows.append(dict(action="move", source=rel(rm / ".filing-rules.json"),
                         target=".filing-rules.json", category="",
                         reason="filing rules: their `folder` now sets a card's category"))

    proj = root / "Projects"
    if proj.is_dir():
        for f in sorted(p for p in proj.rglob("*") if p.is_file()):
            rows.append(dict(action="?", source=rel(f), target="", category="",
                             reason="project note: choose a destination in the note tree, or delete"))

    _clashes(rows)
    out = Path(args.output)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    counts = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    print(f"{out}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


def _opaque(pdf_stem):
    """A name that says nothing about the part: an LCSC code ('C28646261') or a bare number."""
    return bool(re.fullmatch(r"C\d{4,}|[\d_-]+", pdf_stem.strip()))


def _names_same_part(note_stem, pdf_stem):
    """'BQ24074' ~ 'bq24074';  'TPS55287 Design Equations' is not the part note for 'tps55287'."""
    a, b = norm(note_stem), norm(pdf_stem)
    return a == b or (a in b and len(a) >= 5) or (b in a and len(a) - len(b) <= 3)


def _clashes(rows):
    """Two rows may not write the same target; basenames must stay unique for wikilinks."""
    seen = {}
    for r in rows:
        if r["action"] in ("move", "topic", "archive") and r["target"]:
            key = Path(r["target"]).name.lower()
            if key in seen:
                r["action"], r["reason"] = "?", f"basename clash with {seen[key]}"
            seen.setdefault(key, r["target"])


# ── apply ────────────────────────────────────────────────────────────────────
class Journal:
    def __init__(self, path: Path):
        self.path, self.ops = path, []
        self.backup = path.with_suffix("")
        self.backup.mkdir(parents=True, exist_ok=True)

    def save(self):
        write_text(self.path, json.dumps({"ops": self.ops}, indent=1))

    def keep(self, p: Path) -> str:
        """Copy a file about to be destroyed; return where."""
        dst = self.backup / f"{len(self.ops):05d}-{p.name}"
        shutil.copy2(p, dst)
        return str(dst)

    def move(self, a: Path, b: Path):
        if b.exists():
            raise SystemExit(f"refusing to overwrite {b} (journal so far: {self.path})")
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(b))
        self.ops.append({"op": "move", "from": str(a), "to": str(b), "sha256": sha256(b)})
        self.save()

    def delete(self, p: Path):
        kept = self.keep(p)
        self.ops.append({"op": "delete", "path": str(p), "backup": kept, "sha256": sha256(p)})
        p.unlink()
        self.save()

    def write(self, p: Path, text: str):
        kept = self.keep(p) if p.exists() else None
        write_text(p, text)
        self.ops.append({"op": "write", "path": str(p), "backup": kept, "sha256": sha256(p)})
        self.save()


def demote(md_text: str) -> str:
    """Headings one level down, so a merged note nests under the card's `## Notes`."""
    return re.sub(r"^(#{1,5}) ", lambda m: "#" + m.group(1) + " ", md_text, flags=re.M)


def strip_frontmatter(md_text: str):
    m = re.match(r"---\n(.*?)\n---\n?", md_text, re.S)
    return (md_text[m.end():], m.group(1)) if m else (md_text, "")


def cmd_apply(args):
    rm = find_vault(args.vault)
    root = rm.parent
    notes_root = root.parent                   # the enclosing note tree, for Projects moves
    with open(args.plan, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    problems, targets = [], set()
    for r in rows:
        src = root / r["source"]
        if r["action"] == "?":
            problems.append(f"undecided: {r['source']}")
        elif r["action"] == "keep":
            continue
        elif r["action"] not in ("move", "merge", "topic", "archive", "delete"):
            problems.append(f"unknown action {r['action']!r}: {r['source']}")
        elif not src.exists():
            problems.append(f"source gone: {r['source']}")
        if r["action"] in ("move", "topic", "archive", "merge") and r["target"]:
            if r["action"] != "merge":
                key = str((root / r["target"]).resolve()).lower()
                if key in targets:
                    problems.append(f"two rows target {r['target']}")
                targets.add(key)
        if r["action"] in ("move", "topic", "archive") and r["target"]:
            tgt = (root / r["target"]).resolve()
            if not str(tgt).startswith(str(notes_root.resolve())):
                problems.append(f"target outside the note tree: {r['target']}")
            elif tgt.exists():
                problems.append(f"target exists: {r['target']}")
        if r["action"] == "delete" and r["reason"].startswith("old sidecar"):
            pdf_name = r["reason"].split(" of ", 1)[1].split(";")[0]
            stem = pdf_name.rsplit(".", 1)[0]
            # the old sidecar sits beside its PDF (twin, registers) or one level down (text/)
            if not any((d / (stem + ".data.json")).exists() for d in (src.parent, src.parent.parent)):
                problems.append(f"{r['source']}: no .data.json for {pdf_name} yet — run sidecars.py first")
    if problems:
        print("plan refused, nothing changed:")
        for p in problems[:40]:
            print("  " + p)
        if len(problems) > 40:
            print(f"  … {len(problems) - 40} more")
        sys.exit(1)

    j = Journal(Path(args.plan).with_name(f"migrate-journal-{time.strftime('%Y%m%d-%H%M%S')}.json"))
    renamed = {}                               # old note stem -> card stem, for link rewriting
    renamed_pdf = {}                           # old pdf filename (lower) -> new filename
    order = {"delete": 0, "move": 1, "merge": 2, "topic": 3, "archive": 4}
    for r in sorted((r for r in rows if r["action"] != "keep"), key=lambda r: order[r["action"]]):
        src, tgt = root / r["source"], root / r["target"] if r["target"] else None
        if r["action"] == "move":
            j.move(src, tgt)
            if tgt.suffix.lower() == ".pdf" and tgt.name != src.name:
                renamed_pdf[src.name.lower()] = tgt.name
                renamed[src.stem] = tgt.stem          # links to the bare stem follow too
                cache = PARSE_CACHE / src.stem
                if (args.cache_moves and tgt.stem != src.stem and cache.is_dir()
                        and not (PARSE_CACHE / tgt.stem).exists()):
                    j.move(cache, PARSE_CACHE / tgt.stem)
            card = tgt.with_name(tgt.stem + ".md")
            if tgt.suffix.lower() == ".pdf" and not card.exists():
                # starter card: the old folder becomes the category property, and the download
                # name is kept (it can carry the LCSC part number). cards.py fills in the rest
                # and never touches these, or `## Notes`.
                cat = json.dumps(r["category"]) if r["category"] else '""'
                j.write(card, f"---\ncategory: {cat}\noriginal_name: {json.dumps(src.name)}\n"
                              f"---\n\n## Notes\n")
        elif r["action"] in ("topic", "archive"):
            j.move(src, tgt)
        elif r["action"] == "merge":
            body, fm = strip_frontmatter(src.read_text(encoding="utf-8"))
            prior = tgt.read_text(encoding="utf-8") if tgt.exists() else "## Notes\n"
            note = prior.rstrip() + f"\n\n<!-- merged from {src.name} -->\n" + demote(body).strip() + "\n"
            j.write(tgt, note)
            j.delete(src)
            renamed[src.stem] = tgt.stem
        elif r["action"] == "delete":
            j.delete(src)

    # a deleted duplicate PDF's links follow the copy that was kept
    final = {Path(r["source"]).name: Path(r["target"]).name for r in rows
             if r["action"] == "move" and r["source"].lower().endswith(".pdf")}
    for r in rows:
        if r["action"] == "delete" and r["source"].lower().endswith(".pdf") \
                and r["reason"].startswith("byte-identical duplicate of "):
            kept = r["reason"][len("byte-identical duplicate of "):]
            gone = Path(r["source"])
            if kept in final:
                renamed_pdf[gone.name.lower()] = final[kept]
                renamed[gone.stem] = Path(final[kept]).stem

    # rewrite wikilinks to merged notes, across the whole note tree
    if renamed or renamed_pdf:
        low = {k.lower(): v for k, v in renamed.items()}
        low.update({k: v for k, v in renamed_pdf.items()})
        for md in notes_root.rglob("*.md"):
            if ".git" in md.parts or ".obsidian" in md.parts:
                continue
            text = md.read_text(encoding="utf-8", errors="replace")

            def sub(m):
                target = m.group(1).strip()
                new = low.get(Path(target).name.lower())
                return f"[[{new}{m.group(2) or ''}{m.group(3) or ''}]]" if new else m.group(0)
            new_text = _LINK.sub(sub, text)
            if new_text != text:
                j.write(md, new_text)
    views = Path(__file__).resolve().parent / "views"
    for v in sorted(views.glob("*.base")) if views.is_dir() else []:
        dst = root / "Views" / v.name
        if not dst.exists():                  # never overwrite a view someone has edited
            dst.parent.mkdir(exist_ok=True)
            j.write(dst, v.read_text(encoding="utf-8"))
    txt_ref = re.compile(r"attachments/text/([\w.-]+)\.txt")
    for md in (root / "Papers").rglob("*.md") if (root / "Papers").is_dir() else []:
        text = md.read_text(encoding="utf-8")
        new_text = txt_ref.sub(lambda m: f"attachments/{m.group(1)}.text.md", text)
        if new_text != text:
            j.write(md, new_text)
    for d in sorted((p for p in rm.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        if not any(d.iterdir()):
            d.rmdir()
    if rm.is_dir() and not any(rm.iterdir()):
        rm.rmdir()
    print(f"{len(j.ops)} operations; journal {j.path}")


# ── undo ─────────────────────────────────────────────────────────────────────
def cmd_undo(args):
    journal = Path(args.journal)
    ops = json.loads(journal.read_text(encoding="utf-8"))["ops"]
    discarded = journal.with_suffix("") / "discarded-at-undo"
    bad = 0

    def edited(p: Path) -> bool:
        """True if p changed since the migration wrote it; with --discard-edits, save it first."""
        if not args.discard_edits:
            return True
        discarded.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, discarded / f"{len(list(discarded.iterdir())):05d}-{p.name}")
        return False
    for op in reversed(ops):
        if op["op"] == "move":
            a, b = Path(op["from"]), Path(op["to"])
            if not b.exists() or a.exists() or (sha256(b) != op["sha256"] and edited(b)):
                print(f"  skip move-back {b.name}: changed or blocked")
                bad += 1
                continue
            a.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(b), str(a))
        elif op["op"] == "delete":
            p = Path(op["path"])
            if p.exists():
                print(f"  skip restore {p.name}: something is there")
                bad += 1
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(op["backup"], p)
        elif op["op"] == "write":
            p = Path(op["path"])
            if p.exists() and sha256(p) != op["sha256"] and edited(p):
                print(f"  skip {p.name}: edited since migration")
                bad += 1
                continue
            if op["backup"]:
                shutil.copy2(op["backup"], p)
            elif p.exists():
                p.unlink()
    # remove folders the migration created and left empty
    for op in ops:
        for key in ("to", "path"):
            if key in op:
                d = Path(op[key]).parent
                while d.exists() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
    print(f"undone; {bad} step(s) skipped")
    sys.exit(1 if bad else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="AutoNotes vault (see vaultpath.py)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("-o", "--output", default="migrate-plan.tsv")
    p = sub.add_parser("apply")
    p.add_argument("plan")
    p.add_argument("--no-cache-moves", dest="cache_moves", action="store_false",
                   help="leave the parser's output/<stem> caches alone (rehearsals on a copy)")
    p = sub.add_parser("undo")
    p.add_argument("journal")
    p.add_argument("--discard-edits", action="store_true",
                   help="also undo files changed since the migration (e.g. regenerated cards); "
                        "each is copied into the journal's backup folder first")
    args = ap.parse_args()
    {"plan": cmd_plan, "apply": cmd_apply, "undo": cmd_undo}[args.cmd](args)


if __name__ == "__main__":
    main()
