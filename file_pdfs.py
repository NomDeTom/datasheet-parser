#!/usr/bin/env python
"""
file_pdfs.py — file incoming PDFs into the vault, repeatably, with a review step and an undo.

Filing is split so that nothing moves until a person (or an agent) has looked at the plan:

    python file_pdfs.py plan  <file-or-dir> ... [-o plan.tsv]   # propose; touches nothing
    #   ... review / edit plan.tsv ...                             # the checking step
    python file_pdfs.py apply plan.tsv [--copy]                  # execute; writes a journal
    python file_pdfs.py undo  journal.json                       # the escape route
    python file_pdfs.py check                                    # audit the vault afterwards

plan
    Hashes every source PDF and compares it with every PDF already in the vault:
      * byte-identical to a filed PDF        -> action `skip`
      * same document name, newer version    -> `file`, plus a `rename` row marking the old
                                                one `SUPERSEDED - ` (nothing is deleted)
      * same document name, older/same ver.  -> `file` under a `DUPLICATE - ` name, for review
      * otherwise                            -> `file`
    The destination folder comes from, in order: a rule in `<vault>/.filing-rules.json`
    (regex on filename or page-1 text -> folder), the folder of an already-filed PDF of the same
    family (`LR1110_UM` follows `LR1110_DS`), or `?` — which `apply` refuses, so an unplaceable
    file forces a decision instead of landing somewhere arbitrary.
    Names are cleaned the way the vault expects: download suffixes like ` (3)` dropped, spaces to
    `_`, `Datasheet`/`data_sheet` to `_DS`, version `V2_2` to `V2.2`, extension lower-cased.

apply
    Refuses the whole plan if any source changed since planning (sha256), any destination exists,
    any folder is `?`, or any destination is outside the vault. Then moves (or copies) files and
    records every operation in `filing-journal-<timestamp>.json` beside the plan.

undo
    Replays a journal backwards. Each step is verified first — the file at the destination must
    still have the recorded sha256 and the original location must be free — and a step that fails
    verification is reported and left alone rather than forced.

check
    Reports PDFs outside an `attachments/` folder, names that break the conventions, PDFs no note
    links to, and anything still sitting in `Import files/`. Exit 1 if any are found.

.filing-rules.json (optional, in the vault's `Reference Material/`), first match wins:
    {"rules": [{"match": "(?i)^(SX|LR)1\\d|AN1200", "folder": "RF/LoRa"},
               {"text": "(?i)USB\\s+hub", "folder": "Comms"}]}
`match` tests the filename, `text` tests page 1. Folders are relative to `Reference Material/`.
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

from vaultpath import dedupe, find_vault, path_key, root_of, write_text

PLAN_FIELDS = ["action", "source", "sha256", "folder", "name", "reason", "page1"]
MARK_PREFIXES = ("DUPLICATE - ", "SUPERSEDED - ")
RULES_FILE = ".filing-rules.json"


# ── identification ───────────────────────────────────────────────────────────
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def page1_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
        return (PdfReader(str(path)).pages[0].extract_text() or "")[:2000]
    except Exception:
        return ""


def headline(text: str) -> str:
    """First few meaningful words of page 1, for the reviewer — not used for decisions."""
    words = " ".join(text.split())
    return words[:90]


_VER = re.compile(r"(?:^|[_ -])(?:v|rev)[_ .-]?(\d+(?:[._p-]\d+)*)(?=[_ -]|$)", re.I)


def clean_name(stem: str) -> str:
    s = re.sub(r"\s*\(\d+\)$", "", stem)                       # "CH334DS1 (3)"
    s = re.sub(r"^\d{10}_", "", s)                              # LCSC download timestamp
    s = re.sub(r"_C\d{4,}$", "", s)                             # LCSC part code
    s = re.sub(r"(?i)[_ -]?data[_ -]?sheet", "_DS", s)
    s = re.sub(r"\s+", "_", s.strip())
    s = _VER.sub(lambda m: "_V" + re.sub(r"[_p-]", ".", m.group(1)), s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def family_key(stem: str) -> str:
    """Name with the version removed: 'LR20xx_DS_V2.2' -> 'lr20xxds'.

    The document type stays in: a user manual is not a newer datasheet.
    """
    s = _VER.sub("", clean_name(stem))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def part_prefix(stem: str) -> str:
    """Leading part-number-like token, for finding neighbours: 'LR1110_UM_V2.3' -> 'lr1110'."""
    m = re.match(r"[A-Za-z]+\d+[A-Za-z0-9]*", clean_name(stem))
    return m.group(0).lower() if m else ""


def version_of(stem: str):
    m = _VER.search(clean_name(stem))
    return tuple(int(x) for x in re.split(r"[._p-]", m.group(1))) if m else None


# ── vault inventory ──────────────────────────────────────────────────────────
def vault_pdfs(vault: Path):
    found = []
    for pattern in ("*.pdf", "*.PDF"):
        found.extend(sorted(vault.rglob(pattern)))
    return dedupe(found)


def load_rules(vault: Path):
    f = vault / RULES_FILE
    if not f.exists():
        return []
    return json.loads(f.read_text(encoding="utf-8")).get("rules", [])


def rule_folder(rules, name: str, text: str):
    for r in rules:
        if "match" in r and re.search(r["match"], name):
            return r["folder"], f"rule /{r['match']}/"
        if "text" in r and re.search(r["text"], text):
            return r["folder"], f"rule text /{r['text']}/"
    return None, None


def is_library(vault: Path) -> bool:
    """The one-folder-per-document layout (see migrate_layout.py): AutoNotes/Library/<doc>/."""
    return (vault / "Library").is_dir()


def card_category(pdf: Path) -> str:
    card = pdf.with_name(pdf.stem + ".md")
    if card.exists():
        m = re.search(r'^category:\s*"?([^"\n]*)"?\s*$', card.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip()
    return ""


def rel_folder(vault: Path, pdf: Path) -> str:
    if is_library(vault):
        return card_category(pdf)          # the category plays the part a folder used to
    folder = pdf.parent
    if folder.name.lower() == "attachments":
        folder = folder.parent
    return folder.relative_to(vault).as_posix()


# ── plan ─────────────────────────────────────────────────────────────────────
def cmd_plan(args):
    vault = find_vault(args.vault)
    rules = load_rules(vault)
    sources = []
    for s in args.sources:
        p = Path(s)
        if p.is_dir():
            sources += [q for pat in ("*.pdf", "*.PDF") for q in sorted(p.rglob(pat))]
        elif p.is_file():
            sources.append(p)
        else:
            sys.exit(f"not found: {p}")
    sources = dedupe([p.resolve() for p in sources])
    sources = [p for p in sources if not p.name.startswith(MARK_PREFIXES)]

    library = is_library(vault)
    if library:
        filed = [p for p in vault_pdfs(vault / "Library")]
    else:
        filed = [p for p in vault_pdfs(vault) if "attachments" in (x.lower() for x in p.parts)]
    by_hash, by_family, by_prefix = {}, {}, {}
    for p in filed:
        by_hash.setdefault(sha256(p), p)
        by_family.setdefault(family_key(p.stem), []).append(p)
        by_prefix.setdefault(part_prefix(p.stem), []).append(p)

    rows, seen_hash, planned_names = [], {}, set()
    for src in sources:
        digest = sha256(src)
        text = page1_text(src)
        row = {"source": str(src), "sha256": digest, "page1": headline(text)}
        if digest in by_hash:
            # left where it is, renamed so it is visibly a duplicate and never picked up again
            row.update(action="mark", folder="", name="DUPLICATE - " + src.name,
                       reason=f"identical to filed {by_hash[digest].name}")
            rows.append(row)
            continue
        if digest in seen_hash:
            row.update(action="mark", folder="", name="DUPLICATE - " + src.name,
                       reason=f"identical to {seen_hash[digest]}, also in this batch")
            rows.append(row)
            continue
        seen_hash[digest] = src.name

        name = clean_name(src.stem) + ".pdf"
        folder, why = rule_folder(rules, src.name, text)
        kin = by_family.get(family_key(src.stem), [])
        if not folder:
            prefix = part_prefix(src.stem)
            neighbours = kin or (by_prefix.get(prefix, []) if prefix else [])
            folders = sorted({rel_folder(vault, p) for p in neighbours})
            folders = [f for f in folders if f] if library else folders
            if len(folders) == 1:
                folder, why = folders[0], f"beside {neighbours[0].name}"
            elif library:
                # a document folder needs no category; leave it for the card to be edited later
                folder, why = "", ("family split across " + ", ".join(folders)) if folders \
                    else "no rule or neighbour — category left empty"
            elif folders:
                folder, why = "?", "family split across " + ", ".join(folders)
            else:
                folder, why = "?", "no rule or neighbour — choose a folder"
        row.update(action="file", folder=folder, name=name, reason=why)

        # versions of the same document already filed
        mine = version_of(src.stem)
        for old in kin:
            if old.name.startswith(("SUPERSEDED - ", "DUPLICATE - ")):
                continue
            theirs = version_of(old.stem)
            if mine and theirs and mine > theirs:
                rows.append({"action": "rename", "source": str(old), "sha256": sha256(old),
                             "folder": rel_folder(vault, old), "name": "SUPERSEDED - " + old.name,
                             "reason": f"superseded by {name}", "page1": ""})
                row["reason"] += f"; supersedes {old.name}"
            elif mine and theirs and mine <= theirs:
                if library:
                    # same document, not newer, different bytes: a person decides
                    row["action"], row["folder"] = "?", "?"
                    row["reason"] += f"; not newer than filed {old.name} but not identical"
                else:
                    row["name"] = "DUPLICATE - " + name
                    row["reason"] += f"; not newer than filed {old.name}"
        key = (row["folder"], row["name"].lower())
        if key in planned_names:
            row["name"] = f"{Path(row['name']).stem}_{digest[:6]}.pdf"
            row["reason"] += "; name clash in this plan"
        planned_names.add(key)
        rows.append(row)

    out = Path(args.output)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, PLAN_FIELDS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    counts = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    undecided = sum(1 for r in rows if r["folder"] == "?")
    print(f"{out}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
          + (f" — {undecided} need a folder" if undecided else ""))
    for r in rows:
        print(f"  {r['action']:6} {Path(r['source']).name[:44]:44} -> "
              f"{(r['folder'] + '/' if r['folder'] else '')}{r['name']}   [{r['reason']}]")


# ── apply ────────────────────────────────────────────────────────────────────
def read_plan(path: Path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def target_of(vault: Path, row) -> Path:
    if row["action"] in ("rename", "mark"):
        return Path(row["source"]).with_name(row["name"])
    if is_library(vault):
        stem = Path(row["name"]).stem
        return vault / "Library" / stem / row["name"]
    return vault / row["folder"] / "attachments" / row["name"]


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def cmd_apply(args):
    vault = find_vault(args.vault)
    plan_path = Path(args.plan)
    all_rows = read_plan(plan_path)
    undecided = [r for r in all_rows if r["action"] == "?" or r["folder"] == "?"]
    if undecided and not args.skip_undecided:
        print("plan refused, nothing moved — undecided rows (edit them, or --skip-undecided):")
        for r in undecided:
            print(f"  {Path(r['source']).name}: {r['reason']}")
        sys.exit(1)
    rows = [r for r in all_rows if r["action"] in ("file", "rename", "mark") and r not in undecided]
    problems, targets = [], set()
    for r in rows:
        src = Path(r["source"])
        if r["action"] == "mark":
            dst = target_of(vault, r)
            if dst.exists():
                problems.append(f"{src.name}: {dst.name} already exists")
            continue
        if r["folder"] == "?" or not r["name"] or (not r["folder"] and not is_library(vault)):
            problems.append(f"{src.name}: no folder/name decided")
            continue
        dst = target_of(vault, r)
        if not src.is_file():
            problems.append(f"{src.name}: source gone")
        elif sha256(src) != r["sha256"]:
            problems.append(f"{src.name}: changed since the plan was made")
        if dst.exists():
            problems.append(f"{src.name}: destination exists: {dst}")
        if not inside(dst, vault):
            problems.append(f"{src.name}: destination outside the vault: {dst}")
        if path_key(dst) in targets:
            problems.append(f"{src.name}: two rows target {dst.name}")
        targets.add(path_key(dst))
        if is_library(vault) and r["action"] == "file" and dst.parent.exists():
            problems.append(f"{src.name}: document folder already exists: {dst.parent.name}")
        if not is_library(vault) and r["action"] == "file" and not (vault / r["folder"]).is_dir() \
                and not args.mkdir:
            problems.append(f"{src.name}: folder {r['folder']} does not exist (use --mkdir)")
    if problems:
        print("plan refused, nothing moved:")
        for p in problems:
            print("  " + p)
        sys.exit(1)

    journal_path = plan_path.with_name(f"filing-journal-{time.strftime('%Y%m%d-%H%M%S')}.json")
    journal = {"vault": str(vault), "plan": str(plan_path.resolve()), "ops": []}
    # renames first, so a superseded name is free before anything new lands
    for r in sorted(rows, key=lambda r: r["action"] != "rename"):
        src, dst = Path(r["source"]), target_of(vault, r)
        op = "copy" if (args.copy and r["action"] == "file") else "move"
        if args.dry_run:
            print(f"  would {op} {src} -> {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copy2 if op == "copy" else shutil.move)(str(src), str(dst))
        journal["ops"].append({"op": op, "from": str(src), "to": str(dst), "sha256": r["sha256"]})
        write_text(journal_path, json.dumps(journal, indent=1))   # after every op: crash-safe
        print(f"  {op:4} {src.name} -> {dst.relative_to(vault)}")
        card = dst.with_name(dst.stem + ".md")
        if is_library(vault) and r["action"] == "file" and not card.exists():
            # starter card: cards.py fills in everything except `category` and `## Notes`
            write_text(card, f"---\ncategory: {json.dumps(r['folder'])}\n"
                             f"original_name: {json.dumps(src.name)}\n---\n\n## Notes\n")
            journal["ops"].append({"op": "create", "to": str(card), "sha256": sha256(card)})
            write_text(journal_path, json.dumps(journal, indent=1))
    if not args.dry_run:
        print(f"journal: {journal_path}")


# ── undo ─────────────────────────────────────────────────────────────────────
def cmd_undo(args):
    journal = json.loads(Path(args.journal).read_text(encoding="utf-8"))
    failed = 0
    for op in reversed(journal["ops"]):
        if op["op"] == "create":
            card = Path(op["to"])
            if card.exists() and (sha256(card) == op["sha256"] or args.discard_edits):
                if not args.dry_run:
                    card.unlink()
                print(f"  removed {card.name}")
            elif card.exists():
                print(f"  skip {card.name}: edited since filing (use --discard-edits)")
                failed += 1
            continue
        src, dst = Path(op["from"]), Path(op["to"])
        if not dst.is_file() or sha256(dst) != op["sha256"]:
            print(f"  skip {dst.name}: missing or modified since filing")
            failed += 1
            continue
        if op["op"] == "copy":
            if args.dry_run:
                print(f"  would remove copy {dst}")
                continue
            if src.is_file() and sha256(src) == op["sha256"]:
                dst.unlink()                      # the original is intact, so this loses nothing
                print(f"  removed copy {dst.name}")
            else:
                print(f"  skip {dst.name}: original no longer intact, keeping the copy")
                failed += 1
            continue
        if src.exists():
            print(f"  skip {dst.name}: original location {src} is occupied")
            failed += 1
            continue
        if args.dry_run:
            print(f"  would move {dst} -> {src}")
            continue
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        print(f"  restored {src.name}")
        if is_library(Path(journal["vault"])) and dst.parent.parent.name == "Library":
            # the document folder may still hold generated sidecars; they go with the document
            for side in dst.parent.glob(dst.stem + ".*"):
                if side.suffix in (".json", ".md") and side.name != dst.stem + ".md":
                    side.unlink()
            if dst.parent.exists() and not any(dst.parent.iterdir()):
                dst.parent.rmdir()
    sys.exit(1 if failed else 0)


# ── check ────────────────────────────────────────────────────────────────────
_BAD_NAME = re.compile(r"\s\(\d+\)\.pdf$|^\d{10}_|_C\d{4,}\.pdf$", re.I)
_LINK = re.compile(r"\[\[([^\]|#]+)")


def cmd_check(args):
    vault = find_vault(args.vault)
    root = root_of(vault)                     # AutoNotes/
    pdfs = vault_pdfs(root)
    linked = set()
    for md in root.rglob("*.md"):
        if md.name.endswith("(datasheet).md"):
            continue                          # machine twins link their own PDF; not evidence of a real note
        for m in _LINK.finditer(md.read_text(encoding="utf-8", errors="replace")):
            linked.add(Path(m.group(1).strip()).name.lower())
    issues = {"in Import files": [], "outside attachments/": [], "name breaks convention": [],
              "not linked from any note": []}
    for p in pdfs:
        rel = p.relative_to(root).as_posix()
        if rel.lower().startswith("import files/"):
            issues["in Import files"].append(rel)
            continue
        if "Reference Material/" not in rel:
            continue                          # Projects/ keeps its own conventions
        if p.parent.name.lower() != "attachments":
            issues["outside attachments/"].append(rel)
        if _BAD_NAME.search(p.name):
            issues["name breaks convention"].append(rel)
        if (p.name.lower() not in linked and p.stem.lower() not in linked
                and not p.name.startswith(("SUPERSEDED - ", "DUPLICATE - "))):
            issues["not linked from any note"].append(rel)
    total = 0
    for k, v in issues.items():
        print(f"{k}: {len(v)}")
        for x in v[: args.limit]:
            print(f"    {x}")
        if len(v) > args.limit:
            print(f"    … {len(v) - args.limit} more")
        total += len(v)
    sys.exit(1 if total else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="AutoNotes vault (see vaultpath.py for defaults)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="propose filing; touches nothing")
    p.add_argument("sources", nargs="+")
    p.add_argument("-o", "--output", default="filing-plan.tsv")
    p = sub.add_parser("apply", help="execute a reviewed plan")
    p.add_argument("plan")
    p.add_argument("--copy", action="store_true", help="copy sources instead of moving them")
    p.add_argument("--mkdir", action="store_true", help="allow new folders")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-undecided", action="store_true",
                   help="apply the decided rows and leave undecided files where they are")
    p = sub.add_parser("undo", help="reverse an apply from its journal")
    p.add_argument("journal")
    p.add_argument("--discard-edits", action="store_true",
                   help="also remove starter cards that have been filled in since")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("check", help="audit filing conventions")
    p.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()
    {"plan": cmd_plan, "apply": cmd_apply, "undo": cmd_undo, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    main()
