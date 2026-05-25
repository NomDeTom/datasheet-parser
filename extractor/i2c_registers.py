"""
Extracts I2C register bit-field tables from TI-style PDF datasheets.

TI register-description pages follow this structure:
  - Text line:  "9.5.1.1 REG00_Name Register (Offset = 0h) [reset = X]"
             or "7.6.2.1 Config Register (address = 00h) [reset = 7127h]"
  - Table:  Bit | Field | Type | Reset | [Notes |] Description

A single page can contain multiple register headings + tables (common in INA-series).
A table can continue on the next page ("Table x-y. ... (continued)").
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class BitField:
    bits: str
    name: str
    access: str
    reset: str
    description: str


@dataclass
class Register:
    name: str
    address: str     # normalised, e.g. "0x0A"
    reset: str = ""
    description: str = ""
    fields: list[BitField] = field(default_factory=list)
    source_page: int = 0


# ── Regexes ───────────────────────────────────────────────────────────────────

# Matches a TI register heading at the start of a line, e.g.:
#   "9.5.1.1 REG00_Minimal_System_Voltage Register (Offset = 0h) [reset = X]"
#   "7.6.2.1 Configuration Register (address = 00h) [reset = 7127h]"
# Uses MULTILINE so ^ anchors to each line start.
_HEADING_RE = re.compile(
    r"^\d+(?:\.\d+)*\s+"                          # section number: 9.5.1.1 or 7.6.2.1
    r"(?P<name>[A-Za-z][^\n]+?)"                  # name: starts with letter, lazy (allows parens inside)
    r"\s+Register\s*"
    r"\(\s*(?:Offset|address)\s*=\s*"
    r"(?P<addr>[0-9A-Fa-f]{1,4}h)\s*\)"
    r"(?:[^\n]*\[reset\s*=\s*(?P<reset>[^\]]*)\])?",
    re.MULTILINE | re.IGNORECASE,
)

# Marks a continuation table ("... Field Descriptions (continued)")
_CONTINUED_RE = re.compile(r"\(continued\)", re.IGNORECASE)

# Columns that signal a bit-field description table
_BIT_HEADER_WORDS = {"bit", "field", "type", "reset", "access", "r/w"}


def _is_bitfield_table(headers: list) -> bool:
    words = {str(h or "").lower().strip() for h in headers}
    return "bit" in words and len(words & _BIT_HEADER_WORDS) >= 2


def _normalise_addr(raw: str) -> str:
    """Convert '2Eh', '0h', '00h' → '0x2E', '0x00', '0x00'."""
    val = raw.strip().rstrip("hH")
    return f"0x{val.upper().zfill(2)}"


def _build_col_map(headers: list) -> dict[str, int]:
    m: dict[str, int] = {}
    for i, h in enumerate(headers):
        h_low = str(h or "").lower().strip()
        if re.search(r"\bbit[s]?\b", h_low):
            m.setdefault("bits", i)
        if re.search(r"\bfield\b|\bname\b|\bsignal\b", h_low):
            m.setdefault("name", i)
        if re.search(r"\btype\b|r/?w|access", h_low):
            m.setdefault("access", i)
        if re.search(r"\breset\b|\bdefault\b", h_low):
            m.setdefault("reset", i)
        if re.search(r"\bdesc", h_low):
            m.setdefault("description", i)
    return m


def _row_to_field(row: list, col_map: dict[str, int]) -> Optional[BitField]:
    def get(key: str) -> str:
        idx = col_map.get(key)
        return str(row[idx] or "").strip() if idx is not None and idx < len(row) else ""

    bits = get("bits")
    name = get("name")
    if not bits and not name:
        return None
    # Skip header-repetition rows
    if bits.lower() in ("bit", "bits") and name.lower() in ("field", "name", ""):
        return None
    return BitField(
        bits=bits,
        name=name,
        access=get("access"),
        reset=get("reset"),
        description=get("description"),
    )


# ── Extractor ──────────────────────────────────────────────────────────────────

class I2CRegisterExtractor:

    def __init__(self, pdf_path: Path, debug: bool = False):
        self.pdf_path = Path(pdf_path)
        self.debug = debug

    def extract(self) -> list[Register]:
        registers: list[Register] = []
        # Last register seen — may continue on the next page
        pending_reg: Optional[Register] = None

        with pdfplumber.open(self.pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                tables = page.extract_tables()

                # Collect all register headings on this page, in text order
                headings = [
                    (m.group("name").strip(),
                     _normalise_addr(m.group("addr")),
                     (m.group("reset") or "").strip())
                    for m in _HEADING_RE.finditer(text)
                ]

                # Collect bit-field tables only
                bf_tables = [t for t in tables if t and len(t) > 1 and _is_bitfield_table(t[0])]

                is_continuation_page = _CONTINUED_RE.search(text) is not None

                if self.debug and (headings or bf_tables):
                    print(f"  p{page_num}: {len(headings)} heading(s), "
                          f"{len(bf_tables)} bitfield table(s), "
                          f"continuation={is_continuation_page}")
                    for h in headings:
                        print(f"    heading: {h}")

                # A page can have continuation tables AND new register tables together.
                # Continuation tables appear first (before the first new heading), so their
                # count = max(0, total_tables - new_headings_on_this_page).
                n_new = len(headings)
                n_cont = max(0, len(bf_tables) - n_new) if is_continuation_page else 0

                heading_idx = 0

                for tbl_idx, tbl in enumerate(bf_tables):
                    col_map = _build_col_map(tbl[0])

                    if tbl_idx < n_cont:
                        # Continuation of the previous-page register
                        reg = pending_reg
                        if reg is None:
                            reg = Register(name="UNKNOWN", address="?", source_page=page_num)
                            registers.append(reg)
                            pending_reg = reg
                    elif heading_idx < n_new:
                        name, addr, rst = headings[heading_idx]
                        reg = Register(name=name, address=addr, reset=rst, source_page=page_num)
                        registers.append(reg)
                        heading_idx += 1
                        pending_reg = reg
                    else:
                        reg = Register(name="UNKNOWN", address="?", source_page=page_num)
                        registers.append(reg)
                        pending_reg = reg

                    for row in tbl[1:]:
                        bf = _row_to_field([str(c or "") for c in row], col_map)
                        if bf:
                            reg.fields.append(bf)

                # If no bit-field tables on this page (and not a continuation),
                # the pending chain is broken
                if not bf_tables and not is_continuation_page:
                    pending_reg = None

        return registers
