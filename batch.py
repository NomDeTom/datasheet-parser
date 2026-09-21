#!/usr/bin/env python
"""Run the typed inbox: input/<type>/ decides the toolchain, then the originals move out.

    python batch.py                     # every type, originals -> processed/<type>/
    python batch.py --type papers --dest /path/to/vault/folder
    python batch.py --dry-run           # say what would run and where files would go
    python batch.py --keep              # process but leave the inputs in place

Folders and the tool each one routes to:

    input/datasheets/   parse.py            device info, elec-char tables, register maps
    input/papers/       papers.py extract   column-aware text (+ inventory line)
    input/manuals/      trm.py index        page-indexed text cache for TRMs
    input/parametrics/  xlsx_to_csv.py      vendor .xlsx -> output/<stem>.csv

Nothing is deleted. A processed file is moved to --dest (a vault folder, typically) or,
when no --dest is given, to processed/<type>/ beside this script, which is gitignored.
With --dest and papers, the text sidecars go to <dest>/text/ where the notes expect them.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import click

from vaultpath import dedupe

HERE = Path(__file__).resolve().parent
INPUT = HERE / "input"
PROCESSED = HERE / "processed"

TYPES = {
    "datasheets": {"exts": (".pdf",), "tool": "parse.py"},
    "papers": {"exts": (".pdf",), "tool": "papers.py"},
    "manuals": {"exts": (".pdf",), "tool": "trm.py"},
    "parametrics": {"exts": (".xlsx",), "tool": "xlsx_to_csv.py"},
}


def files_in(kind):
    folder = INPUT / kind
    if not folder.is_dir():
        return []
    found = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in TYPES[kind]["exts"]]
    return dedupe(sorted(found))


def commands_for(kind, f, dest):
    """The argv lists to run for one file, in order."""
    py = sys.executable
    if kind == "datasheets":
        return [[py, str(HERE / "parse.py"), str(f)]]
    if kind == "papers":
        cmds = [[py, str(HERE / "papers.py"), "inventory", str(f)]]
        extract = [py, str(HERE / "papers.py"), "extract", str(f)]
        if dest:
            extract += ["-o", str(dest / "text")]
        return cmds + [extract]
    if kind == "manuals":
        return [[py, str(HERE / "trm.py"), "index", str(f)]]
    if kind == "parametrics":
        out = HERE / "output" / (f.stem + ".csv")
        return [[py, str(HERE / "xlsx_to_csv.py"), str(f), "-o", str(out)]]
    raise ValueError(kind)


def move_out(f, kind, dest, dry_run):
    target_dir = dest if dest else PROCESSED / kind
    target = target_dir / f.name
    if target.exists():
        click.echo(f"    !! not moved: {target} already exists")
        return False
    if dry_run:
        click.echo(f"    -> would move to {target}")
        return True
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(f), str(target))
    click.echo(f"    -> {target}")
    return True


@click.command(help=__doc__, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--type", "kinds", multiple=True, type=click.Choice(sorted(TYPES)),
              help="Only these input types (repeatable). Default: all.")
@click.option("--dest", type=click.Path(file_okay=False, path_type=Path),
              help="Move processed originals here (e.g. the vault folder the notes live in).")
@click.option("--keep", is_flag=True, help="Leave the originals in input/ after processing.")
@click.option("--dry-run", is_flag=True, help="Print the plan; run nothing, move nothing.")
def main(kinds, dest, keep, dry_run):
    kinds = kinds or tuple(sorted(TYPES))
    done, failed, empty = [], [], []
    for kind in kinds:
        files = files_in(kind)
        if not files:
            empty.append(kind)
            continue
        click.echo(f"== {kind}: {len(files)} file(s) -> {TYPES[kind]['tool']}")
        for f in files:
            click.echo(f"  {f.name}")
            ok = True
            for cmd in commands_for(kind, f, dest):
                if dry_run:
                    click.echo("    $ " + " ".join(cmd[1:]))
                    continue
                res = subprocess.run(cmd, cwd=HERE)
                if res.returncode != 0:
                    ok = False
                    click.echo(f"    !! {Path(cmd[1]).name} exited {res.returncode}; original left in place")
                    break
            if not ok:
                failed.append(f)
                continue
            if not keep:
                move_out(f, kind, dest, dry_run)
            done.append(f)
    click.echo()
    click.echo(f"{'Planned' if dry_run else 'Processed'} {len(done)}, failed {len(failed)}"
               + (f", empty: {', '.join(empty)}" if empty else ""))
    for f in failed:
        click.echo(f"  failed: {f}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
