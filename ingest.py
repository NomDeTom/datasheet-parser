#!/usr/bin/env python
"""
ingest.py — process whatever has been dropped into the vault's `Import files/` folder.

    python ingest.py                 # one pass (what the systemd .path / .timer units run)
    python ingest.py --nightly       # the slow work deferred from imports, plus a full check
    python ingest.py --dry-run       # say what would happen; change nothing
    python ingest.py --refresh       # only absorb newer Docling/OCR output into existing documents

One pass, for PDFs that have stopped growing (a sync tool may still be writing them):

  1. file    file_pdfs.py plan -> apply --skip-undecided, journalled (file_pdfs.py undo reverses it)
               identical to a filed PDF  -> renamed `DUPLICATE - <name>`, left in the inbox
               needs a person's decision -> renamed `REVIEW - <name>`, left in the inbox
                                            (remove the prefix to have it retried)
  2. extract Docling (docling_extract.py) for documents up to --max-pages; longer ones are queued
             for the nightly run and get pypdf text now. Then the pdfplumber parse.
  3. build   sidecars.py, cards.py for the new documents; build_db.py; verify_library.py
  4. report  one dated block per pass appended to `<AutoNotes>/Import log.md`, readable in Obsidian
  5. refresh documents whose Docling / OCR cache has grown since their sidecars were built
             (the overnight batch, the deferred manuals): sidecars, cards, database, check

Only one pass runs at a time (a lock in the state folder); a pass that finds the lock taken exits
quietly — the periodic timer picks up anything it would have handled. The pass repeats until the
inbox holds nothing new, so files that land mid-run are not missed.

State (plans, journals, the nightly queue, the lock) lives in $XDG_STATE_HOME/autonotes-ingest,
not in the vault. Settings come from the environment: AUTONOTES_VAULT, DOCLING_PYTHON, and
optionally INGEST_MAX_PAGES / INGEST_SETTLE_SECONDS.
"""
import argparse
import csv
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import vaultpath

HERE = Path(__file__).resolve().parent
PY = sys.executable
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "autonotes-ingest"
MARKED = ("DUPLICATE - ", "SUPERSEDED - ", "REVIEW - ")
PARTIAL = re.compile(r"^(\.syncthing\.|~syncthing~|\.~|~\$)|\.(tmp|part|crdownload)$", re.I)


def inbox_pdfs(inbox: Path):
    return sorted(p for p in inbox.iterdir()
                  if p.is_file() and p.suffix.lower() == ".pdf"
                  and not p.name.startswith(MARKED) and not PARTIAL.search(p.name))


def settled(paths, seconds: float):
    """Those whose size and mtime did not change over `seconds`."""
    before = {p: (p.stat().st_size, p.stat().st_mtime) for p in paths if p.exists()}
    time.sleep(seconds)
    return [p for p, sig in before.items()
            if p.exists() and (p.stat().st_size, p.stat().st_mtime) == sig and sig[0] > 0]


def run(cmd, log, env=None, check=False):
    """Run a step; its output goes to the per-pass log file, not the console."""
    log.write(f"\n$ {' '.join(str(c) for c in cmd)}\n")
    log.flush()
    r = subprocess.run([str(c) for c in cmd], cwd=HERE, env=env, stdout=log,
                       stderr=subprocess.STDOUT, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{Path(str(cmd[1])).name} exited {r.returncode}")
    return r.returncode


def page_count(pdf: Path) -> int:
    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    return len(PdfReader(str(pdf)).pages)


def has_text_layer(pdf: Path, sample: int = 8) -> bool:
    """Any real text on the first pages? Scans, image tiles and outline-drawn text have none."""
    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    for page in PdfReader(str(pdf)).pages[:sample]:
        try:
            if len((page.extract_text() or "").strip()) >= 20:
                return True
        except Exception:                                   # noqa: BLE001 — unreadable page
            continue
    return False


def docling_env():
    env = dict(os.environ)
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TMPDIR", "/var/tmp")
    env.setdefault("PYTHONWARNINGS", "ignore")
    return env


def extract(docs, root, log, max_pages, queue: Path, dry):
    """Docling (or queue it), then the pdfplumber parse, for each newly filed PDF."""
    import twin_notes
    notes = []
    for pdf in docs:
        try:
            notes.append(_extract_one(pdf, log, max_pages, queue, dry, twin_notes))
        except BaseException as exc:          # noqa: BLE001 — incl. SystemExit from a missing tool
            if isinstance(exc, KeyboardInterrupt):
                raise
            notes.append(f"**extraction failed**: {exc} — retried on the next pass")
    return notes


def _extract_one(pdf, log, max_pages, queue, dry, twin_notes):
    n = page_count(pdf)
    if dry:
        return f"would extract ({n} pp)"
    parts, docling_ok = [], False
    if n <= max_pages:
        rc = run([PY, HERE / "docling_extract.py", pdf], log, env=docling_env())
        docling_ok = rc == 0
        parts.append(f"Docling {'ok' if docling_ok else 'FAILED — see run log'}")
    else:
        with open(queue, "a", encoding="utf-8") as fh:
            fh.write(f"{pdf}\n")
        parts.append(f"{n} pp: pypdf text now, Docling tonight")
    if not has_text_layer(pdf):
        if os.environ.get("INGEST_OCR", "1") == "1" and n <= max_pages:
            rc = run([PY, HERE / "docling_extract.py", "--ocr", pdf], log, env=docling_env())
            parts.append("no text layer: OCR " + ("ok — values flagged `ocr`, check against the "
                                                  "PDF" if rc == 0 else "FAILED (is the [ocr] "
                                                  "extra installed?)"))
        else:
            parts.append("no text layer: OCR off or document too long — flagged `no_text_layer`")
        return "; ".join(parts)                  # nothing for pdfplumber to read
    # Docling is the reader wherever it is installed and has converted the document; pdfplumber
    # and pdftotext are only the fallback (Docling absent, failed, or deferred to the night)
    if not docling_ok and n <= 250:
        _, _, _, err, _ = twin_notes.run_parser(pdf, timeout=300)
        parts.append("fallback readers (pdfplumber, pdftotext)" + (f": {err}" if err else ""))
    return "; ".join(parts)


def _frontmatter(md: Path) -> dict:
    out = {}
    try:
        head = md.read_text(encoding="utf-8").split("\n---\n", 1)[0]
    except OSError:
        return out
    for line in head.splitlines()[1:]:
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


def stale(root: Path):
    """Documents whose Docling (or OCR) cache has grown since their sidecars were built — the
    overnight batch, the deferred manuals, any re-run. Cheap: one frontmatter read and a directory
    listing per document, no PDF opened."""
    import sidecars
    out = []
    for d in sorted(p for p in (root / "Library").iterdir() if p.is_dir()):
        if d.name.startswith(MARKED):
            continue
        tmd = d / (d.name + ".text.md")
        if not (d / (d.name + ".pdf")).exists() or not tmd.exists():
            continue
        fm = _frontmatter(tmd)
        sha = fm.get("sha256", "")
        if len(sha) < 16:
            continue
        cache = sidecars.OUTPUT / "docling" / sha[:16]
        ocr = sidecars.OUTPUT / "docling" / (sha[:16] + "-ocr")
        # older .text.md files predate the coverage fields: fall back to pages that used Docling
        had = int(fm.get("docling_cache_pages", fm.get("text_docling_pages", 0)) or 0)
        had_ocr = int(fm.get("ocr_cache_pages", fm.get("text_ocr_pages", 0)) or 0)
        if sidecars.cache_coverage(cache) > had or sidecars.cache_coverage(ocr) > had_ocr:
            out.append(d / (d.name + ".pdf"))
    return out


def refresh(root: Path, log, dry=False):
    """Rebuild sidecars and cards for stale documents; report lines (empty if nothing to do)."""
    docs = stale(root)
    if not docs:
        return []
    if dry:
        return [f"would refresh {len(docs)} document(s) with new Docling pages"]
    rc, verify = build(docs, root, log)
    names = ", ".join(f"[[{d.stem}]]" for d in docs[:25]) + (" …" if len(docs) > 25 else "")
    return [f"refreshed {len(docs)} document(s) from newer Docling / OCR output: {names}",
            "library check " + ("passed" if verify == 0 else "**failed**")
            + ("" if rc == 0 else " — rebuild had errors, see run log")]


def unfinished(root: Path):
    """Filed documents without their data sidecar — an earlier pass stopped part-way."""
    out = []
    for d in sorted((root / "Library").iterdir()):
        pdf = d / (d.name + ".pdf")
        if d.name.startswith(MARKED):
            continue                          # superseded/duplicate documents get no sidecars
        if d.is_dir() and pdf.exists() and not (d / (d.name + ".data.json")).exists():
            out.append(pdf)
    return out


def build(docs, root, log):
    rc = 0
    if docs:
        rc |= run([PY, HERE / "sidecars.py", "--vault", root, *sum((["--pdf", d] for d in docs), [])], log)
        rc |= run([PY, HERE / "cards.py", "--vault", root, *sum((["--doc", d.stem] for d in docs), [])], log)
    rc |= run([PY, HERE / "build_db.py", "--vault", root], log)
    verify = run([PY, HERE / "verify_library.py", "--vault", root], log)
    return rc, verify


def write_report(root: Path, lines):
    if not lines:
        return
    report = root / "Import log.md"
    head = "" if report.exists() else ("# Import log\n\nWritten by `ingest.py` after each pass over "
                                      "`Import files/`. Newest at the bottom.\n")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(report, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(head + f"\n## {stamp}\n\n" + "\n".join(f"- {l}" for l in lines) + "\n")


def one_pass(root: Path, inbox: Path, args, log):
    """File and extract one batch. Returns report lines, or None when there was nothing to do."""
    fresh = inbox_pdfs(inbox)
    if not fresh:
        return None
    ready = settled(fresh, args.settle)
    if not ready:
        return None                                   # still arriving; the next trigger will see them
    stamp = time.strftime("%Y%m%d-%H%M%S")
    plan = STATE / f"plan-{stamp}.tsv"
    run([PY, HERE / "file_pdfs.py", "--vault", root, "plan", *ready, "-o", plan], log, check=True)
    rows = list(csv.DictReader(open(plan, encoding="utf-8"), delimiter="\t"))

    lines = []
    if not args.dry_run:
        run([PY, HERE / "file_pdfs.py", "--vault", root, "apply", plan, "--skip-undecided"], log)
    filed = []
    for r in rows:
        src = Path(r["source"])
        if r["action"] == "file":
            dst = root / "Library" / Path(r["name"]).stem / r["name"]
            filed.append(dst)
            cat = f", category `{r['folder']}`" if r["folder"] else ", no category yet"
            lines.append(f"**{src.name}** → [[{dst.stem}]]{cat} ({r['reason']})")
        elif r["action"] == "mark":
            lines.append(f"**{src.name}**: duplicate — {r['reason']}; left in the inbox as `{r['name']}`")
        elif r["action"] == "rename":
            lines.append(f"{src.name} marked superseded ({r['reason']})")
        elif r["action"] == "?" or r["folder"] == "?":
            review = src.with_name("REVIEW - " + src.name)
            if not args.dry_run and src.exists() and not review.exists():
                src.rename(review)
            lines.append(f"**{src.name}**: needs a decision — {r['reason']}. Left as `{review.name}`; "
                         f"remove the prefix to retry, or file it by hand with `file_pdfs.py`.")
    filed = [d for d in filed if d.exists() or args.dry_run]
    notes = extract(filed, root, log, args.max_pages, STATE / "nightly-docling.txt", args.dry_run)
    for d, n in zip(filed, notes):
        lines.append(f"[[{d.stem}]]: {n}")
    if filed and not args.dry_run:
        rc, verify = build(filed, root, log)
        lines.append("sidecars, cards and database rebuilt" + ("" if rc == 0 else " — with errors, see run log"))
        lines.append("library check passed" if verify == 0 else
                     "**library check failed** — run `verify_library.py` for detail")
    lines.append(f"run log: `{Path(log.name).name}` in `{STATE}`")
    return lines


def nightly(root: Path, args, log):
    queue = STATE / "nightly-docling.txt"
    todo = [Path(l) for l in queue.read_text(encoding="utf-8").splitlines()] if queue.exists() else []
    todo = [p for p in dict.fromkeys(todo) if p.exists()]
    lines = []
    for pdf in todo:
        if args.dry_run:
            lines.append(f"would run Docling on {pdf.name}")
            continue
        rc = run([PY, HERE / "docling_extract.py", pdf], log, env=docling_env())
        lines.append(f"[[{pdf.stem}]]: nightly Docling {'ok' if rc == 0 else 'FAILED — will retry'}")
    if not args.dry_run:
        done = [p for p, l in zip(todo, lines) if l.endswith(" ok")]
        rest = [p for p in todo if p not in done]
        queue.write_text("".join(f"{p}\n" for p in rest), encoding="utf-8")
        _, verify = build(done, root, log)
        lines.append("nightly library check " + ("passed" if verify == 0 else "**failed**"))
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault")
    ap.add_argument("--nightly", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true",
                    help="only rebuild documents whose Docling/OCR cache has grown since")
    ap.add_argument("--max-pages", type=int, default=int(os.environ.get("INGEST_MAX_PAGES", 250)))
    ap.add_argument("--settle", type=float, default=float(os.environ.get("INGEST_SETTLE_SECONDS", 20)))
    args = ap.parse_args()
    root = vaultpath.root_of(vaultpath.find_vault(args.vault))
    if not (root / "Library").is_dir():
        sys.exit(f"{root} is not in the Library layout; run migrate_layout.py first")
    inbox = root / "Import files"
    inbox.mkdir(exist_ok=True)                        # a fresh vault may not have one yet
    STATE.mkdir(parents=True, exist_ok=True)

    lock = open(STATE / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another pass is running; leaving it to that one")
        return

    log_path = STATE / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"
    with open(log_path, "w", encoding="utf-8") as log:
        if args.nightly:
            write_report(root, nightly(root, args, log) + refresh(root, log, args.dry_run))
            return
        if args.refresh:
            lines = refresh(root, log, args.dry_run)
            print("\n".join(lines) or "nothing stale")
            if not args.dry_run:
                write_report(root, lines)
            return
        behind = unfinished(root)
        if behind and not args.dry_run:
            notes = extract(behind, root, log, args.max_pages, STATE / "nightly-docling.txt", False)
            rc, verify = build(behind, root, log)
            write_report(root, [f"[[{d.stem}]]: caught up after an interrupted pass — {n}"
                                for d, n in zip(behind, notes)]
                         + ["library check " + ("passed" if verify == 0 else "**failed**")])
        for _ in range(20):                           # repeat while files keep arriving
            before = inbox_pdfs(inbox)
            lines = one_pass(root, inbox, args, log)
            if lines is None:
                break
            if inbox_pdfs(inbox) == before and not args.dry_run:
                lines.insert(0, "**nothing could be filed** — the plan was refused; "
                                "see the run log, fix, and the next trigger retries")
                write_report(root, lines)
                break
            if not args.dry_run:
                write_report(root, lines)
            print("\n".join(lines))
            if args.dry_run:
                break
        # background Docling work (batch, deferred manuals) flows in here, not only at night
        lines = refresh(root, log, args.dry_run)
        if lines and not args.dry_run:
            write_report(root, lines)
    if log_path.stat().st_size == 0:
        log_path.unlink()


if __name__ == "__main__":
    main()
