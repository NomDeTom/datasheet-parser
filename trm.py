"""
Large-document tool: index, search and mine vendor TRMs / reference manuals.

Companion to parse.py.  Where parse.py does deep table extraction on ordinary
datasheets, this handles documents too large for that to be practical — build
a page-indexed text cache once, then query it repeatedly in milliseconds.

Usage:
    python trm.py index  <pdf>                  # build/refresh the text cache
    python trm.py toc    <pdf> [--filter spi]   # list numbered TOC entries
    python trm.py find   <pdf> <regex> [-C 3]   # search, with page numbers
    python trm.py regs   <pdf> [--filter IOMUX] # register names -> absolute addrs
    python trm.py pins   <pdf> --signal SPI0    # signals in a pin-mux matrix
"""
import re
import sys
from pathlib import Path

import click

# Ensure Unicode characters (µ, ×, –, °, …) survive on Windows consoles
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from extractor.textindex import (
    build_index,
    cache_path_for,
    find_registers,
    find_toc,
    search,
)

CACHE_DIR = Path("output")


def _ensure_index(pdf: Path, force: bool = False) -> Path:
    cache = cache_path_for(pdf, CACHE_DIR)
    if cache.exists() and not force:
        return cache

    click.echo(f"Indexing {pdf.name} (first run, this is the slow part) ...")

    def progress(done, total):
        click.echo(f"  ...{done}/{total}")

    cache = build_index(pdf, CACHE_DIR, force=force, progress=progress)
    click.echo(f"  cached -> {cache}")
    return cache


@click.group()
def cli():
    """Index and query large PDF technical reference manuals."""


@cli.command()
@click.argument("pdf", type=click.Path(exists=True, path_type=Path))
@click.option("--force", is_flag=True, help="Rebuild even if a cache exists")
def index(pdf, force):
    """Build the page-indexed text cache for PDF."""
    cache = _ensure_index(pdf, force=force)
    size = cache.stat().st_size / 1024
    click.echo(f"OK  {cache}  ({size:,.0f} KB)")


@cli.command()
@click.argument("pdf", type=click.Path(exists=True, path_type=Path))
@click.option("--filter", "flt", default="", help="Only entries matching this substring")
def toc(pdf, flt):
    """List numbered table-of-contents entries with their page numbers."""
    cache = _ensure_index(pdf)
    entries = find_toc(cache)
    if flt:
        low = flt.lower()
        entries = [e for e in entries if low in e.title.lower()]
    for e in entries:
        click.echo(f"  p{e.page:<5} {e.number:<10} {e.title}")
    click.echo(f"\n{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")


@cli.command()
@click.argument("pdf", type=click.Path(exists=True, path_type=Path))
@click.argument("pattern")
@click.option("-C", "context", default=0, help="Lines of context around each hit")
@click.option("--case", is_flag=True, help="Case-sensitive search")
@click.option("--limit", default=60, show_default=True, help="Max hits to print")
def find(pdf, pattern, context, case, limit):
    """Regex-search PDF, reporting real page numbers."""
    cache = _ensure_index(pdf)
    hits = search(cache, pattern, context=context, ignore_case=not case)

    for h in hits[:limit]:
        click.echo(f"\n── p{h.page} ──")
        for b in h.before:
            click.echo(f"    {b}")
        click.echo(f"  > {h.line}")
        for a in h.after:
            click.echo(f"    {a}")

    shown = min(len(hits), limit)
    click.echo(f"\n{len(hits)} hit(s){'' if shown == len(hits) else f', showing {shown}'}")


@cli.command()
@click.argument("pdf", type=click.Path(exists=True, path_type=Path))
@click.option("--filter", "flt", default="", help="Only registers whose name contains this")
def regs(pdf, flt):
    """List register definitions with computed absolute addresses."""
    cache = _ensure_index(pdf)
    found = find_registers(cache)
    if flt:
        low = flt.lower()
        found = [r for r in found if low in r.name.lower()]

    click.echo(f"  {'ADDRESS':<12} {'BASE':<12} {'OFFSET':<8} {'PAGE':<6} NAME")
    for r in found:
        click.echo(f"  0x{r.address:08X}   0x{r.base:08X}   0x{r.offset:04X}   "
                   f"p{r.page:<5} {r.name}")
    click.echo(f"\n{len(found)} register(s)")


@cli.command()
@click.argument("pdf", type=click.Path(exists=True, path_type=Path))
@click.option("--signal", default="", help="Only rows mentioning this signal, e.g. SPI0")
@click.option("--pages", "pages_opt", default="",
              help="Restrict to these pages, e.g. 28-29 (else auto-detect densest table)")
def pins(pdf, signal, pages_opt):
    """Extract pin-mux matrix rows (function index -> signal name).

    Targets tables like the RK3506 'Rockchip Matrix IO Function List', which
    enumerate 'index signal' pairs, often two columns per printed line.
    """
    cache = _ensure_index(pdf)
    rx = re.compile(r"(?<!\d)(\d{1,3})\s+([A-Z][A-Z0-9_]{2,})")

    # Score pages by how many index->signal pairs they contain.  A pin-mux
    # table is dense with them; incidental "number WORD" prose is not.  Taking
    # the first match document-wide instead would let unrelated early pages
    # claim the low indices before the real table is ever reached.
    per_page: dict[int, list[tuple[int, str]]] = {}
    hits = search(cache, r"\d+\s+[A-Z][A-Z0-9_]{2,}", ignore_case=False)
    for h in hits:
        pairs = [(int(i), n) for i, n in rx.findall(h.line) if 0 < int(i) <= 512]
        if pairs:
            per_page.setdefault(h.page, []).extend(pairs)

    if not per_page:
        click.echo("  no pin-mux style table found")
        return

    if pages_opt:
        lo, _, hi = pages_opt.partition("-")
        rng = range(int(lo), int(hi or lo) + 1)
        table_pages = sorted(p for p in per_page if p in rng)
    else:
        # Anchor on the single densest page, then walk outwards only through
        # pages that continue the same table.  Merging every dense page in the
        # document would let an unrelated earlier table claim the indices.
        anchor = max(per_page, key=lambda p: len(per_page[p]))
        threshold = len(per_page[anchor]) * 0.4
        table_pages = [anchor]
        for step in (-1, 1):
            p = anchor + step
            while p in per_page and len(per_page[p]) >= threshold:
                table_pages.append(p)
                p += step
        table_pages.sort()

    seen: dict[int, str] = {}
    pages: dict[int, int] = {}
    for p in table_pages:
        for i, name in per_page[p]:
            if i not in seen:
                seen[i] = name
                pages[i] = p

    click.echo(f"  (table pages: {', '.join('p%d' % p for p in table_pages)})\n")
    items = sorted(seen.items())
    if signal:
        up = signal.upper()
        items = [(i, n) for i, n in items if up in n]

    for i, name in items:
        click.echo(f"  {i:<5} {name:<24} p{pages[i]}")
    click.echo(f"\n{len(items)} signal(s)")


if __name__ == "__main__":
    cli()
