"""
Page-indexed full-text cache for very large PDFs (TRMs, reference manuals).

The pdfplumber-based extractors in this package are accurate but slow — they
are built for 30–100 page datasheets.  A vendor TRM is a different animal:
Rockchip_RK3506_TRM_Part_1 is 1140 pages / 30 MB, where per-page table
analysis costs minutes and usually is not what you want anyway.  You want to
locate the handful of pages that mention a signal, then read those.

This module builds a one-off text cache with `=== PAGE n ===` markers using
pypdf (roughly 10x faster than pdfplumber for plain text), then searches it
while reporting real PDF page numbers so results can be cross-checked against
the document.

Handles:
  - Register definitions split across lines by the PDF's table layout
  - Rockchip-style address blocks: "Address: Operational Base(0xFF950000) + offset (0x000C)"
  - TOC lines of the form "19.5 Interface Description ......... 591"
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from pypdf import PdfReader

PAGE_MARKER = "=== PAGE %d ==="
_PAGE_RE = re.compile(r"^=== PAGE (\d+) ===$")

# "Address: Operational Base(0xFF950000) + offset (0x000C)"
_ADDR_RE = re.compile(
    r"Address:\s*Operational\s*Base\s*\(\s*0x([0-9A-Fa-f]+)\s*\)\s*"
    r"\+\s*offset\s*\(\s*0x([0-9A-Fa-f]+)\s*\)",
    re.IGNORECASE,
)

# A register identifier: UPPER_SNAKE with at least one underscore, >= 6 chars
_REGNAME_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")

# "19.5 Interface Description ................. 591"
_TOC_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+(.+?)\s*\.{3,}\s*(\d+)\s*$")

# "Chapter 29 Serial Peripheral Interface (SPI) ......... 733"
# Chapter headings do not lead with a bare section number, so they are missed
# by _TOC_RE -- which silently hides whole chapters from `toc --filter`.
_TOC_CHAPTER_RE = re.compile(
    r"^\s*Chapter\s+(\d+)\s+(.+?)\s*\.{3,}\s*(\d+)\s*$", re.IGNORECASE
)


# ── Index building ────────────────────────────────────────────────────────────

def cache_path_for(pdf_path: Path, cache_dir: Path) -> Path:
    return cache_dir / pdf_path.stem / "text.txt"


def build_index(pdf_path: Path, cache_dir: Path, force: bool = False,
                progress=None) -> Path:
    """Extract every page's text into a flat, page-marked cache file."""
    dest = cache_path_for(pdf_path, cache_dir)
    if dest.exists() and not force:
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)

    with dest.open("w", encoding="utf-8", errors="replace") as fh:
        for i, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 - a bad page must not abort the run
                text = f"<<extract error: {exc}>>"
            fh.write("\n" + (PAGE_MARKER % i) + "\n")
            fh.write(text)
            if progress and i % 100 == 0:
                progress(i, total)

    return dest


def iter_pages(cache: Path) -> Iterator[tuple[int, str]]:
    """Yield (page_number, page_text) from a cache built by build_index."""
    page_no: Optional[int] = None
    buf: list[str] = []
    with cache.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _PAGE_RE.match(line.rstrip("\n"))
            if m:
                if page_no is not None:
                    yield page_no, "".join(buf)
                page_no = int(m.group(1))
                buf = []
            else:
                buf.append(line)
    if page_no is not None:
        yield page_no, "".join(buf)


# ── Search ────────────────────────────────────────────────────────────────────

@dataclass
class Hit:
    page: int
    line_no: int          # line number within the page
    line: str
    before: list[str]
    after: list[str]


def search(cache: Path, pattern: str, context: int = 0,
           ignore_case: bool = True) -> list[Hit]:
    """Regex-search the cache, returning hits tagged with real page numbers."""
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(pattern, flags)
    hits: list[Hit] = []

    for page_no, text in iter_pages(cache):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if rx.search(line):
                hits.append(Hit(
                    page=page_no,
                    line_no=i + 1,
                    line=line.rstrip(),
                    before=[l.rstrip() for l in lines[max(0, i - context):i]],
                    after=[l.rstrip() for l in lines[i + 1:i + 1 + context]],
                ))
    return hits


# ── Register map extraction ───────────────────────────────────────────────────

@dataclass
class Register:
    name: str
    base: int
    offset: int
    page: int

    @property
    def address(self) -> int:
        return self.base + self.offset


def find_registers(cache: Path) -> list[Register]:
    """Pull register definitions with absolute addresses out of a vendor TRM.

    The register name normally sits on the nearest preceding non-empty line;
    TRM tables often wrap it, so we scan back a few lines for the first token
    that looks like a register identifier.
    """
    regs: list[Register] = []

    for page_no, text in iter_pages(cache):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = _ADDR_RE.search(line)
            if not m:
                continue
            base = int(m.group(1), 16)
            offset = int(m.group(2), 16)

            name = ""
            for back in range(i - 1, max(-1, i - 6), -1):
                cand = lines[back].strip()
                if not cand:
                    continue
                token = cand.split()[0].strip()
                if _REGNAME_RE.match(token):
                    name = token
                    break
            regs.append(Register(name=name or "<unnamed>", base=base,
                                 offset=offset, page=page_no))
    return regs


# ── Table of contents ─────────────────────────────────────────────────────────

@dataclass
class TocEntry:
    number: str
    title: str
    page: int


def find_toc(cache: Path, max_page: int = 40) -> list[TocEntry]:
    """Recover numbered TOC entries, so chapters can be located by page."""
    out: list[TocEntry] = []
    for page_no, text in iter_pages(cache):
        if page_no > max_page:
            break
        for line in text.splitlines():
            m = _TOC_CHAPTER_RE.match(line)
            if m:
                out.append(TocEntry(number=f"Chapter {m.group(1)}",
                                    title=m.group(2).strip(),
                                    page=int(m.group(3))))
                continue
            m = _TOC_RE.match(line)
            if m:
                out.append(TocEntry(number=m.group(1),
                                    title=m.group(2).strip(),
                                    page=int(m.group(3))))
    return out
