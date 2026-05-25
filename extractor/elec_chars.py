"""
Extracts Electrical Characteristics tables from TI-style PDF datasheets.

Section types handled:
  - Absolute Maximum Ratings       (MIN | MAX | UNIT)
  - ESD Ratings                    (VALUE | UNIT)
  - Recommended Operating Conditions (MIN | NOM | MAX | UNIT)
  - Electrical Characteristics     (PARAMETER | TEST CONDITIONS | MIN | TYP | MAX | UNIT)
  - Timing Requirements            (similar to EC)

Handles:
  - Multi-page sections ("(continued)")
  - Multiple sections on one page
  - Sub-group header rows ("QUIESCENT CURRENTS", "I2C TIMING", …)
  - Packed MIN/TYP/MAX values in a single cell ("3.05 3.20 3.31")
  - Merged PARAMETER cells across rows (carry-forward)
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ElecSpec:
    symbol: str           # short identifier from col 0 (e.g. "I_Q_BAT_ON")
    parameter: str        # description from col 1 (or combined if single col)
    conditions: str       # per-row test conditions
    min: str
    typ: str
    max: str
    unit: str
    group: str            # sub-group row text (e.g. "QUIESCENT CURRENTS")
    source_page: int


@dataclass
class ElecSection:
    name: str             # "Electrical Characteristics", "Absolute Maximum Ratings" …
    conditions: str       # global test conditions from section preamble text
    source_page: int
    specs: list[ElecSpec] = field(default_factory=list)


# ── Regexes ───────────────────────────────────────────────────────────────────

# TI section heading — section number prefix, then recognised title.
# Excludes "(continued)" continuations from being treated as new sections.
_SECTION_RE = re.compile(
    r"^\d+(?:\.\d+)*\s+"
    r"(?P<name>"
    r"Absolute Maximum Ratings|"
    r"ESD Ratings|"
    r"Recommended Operating Conditions|"
    r"Electrical Characteristics|"
    r"Timing Requirements[^\n]*"
    r")"
    r"(?!\s*\(continued\))",   # negative lookahead — skip continuation headings
    re.MULTILINE | re.IGNORECASE,
)

_CONTINUED_RE = re.compile(r"\(continued\)", re.IGNORECASE)

# A token that looks like a numeric value (with optional sign and SI suffix).
# Includes en-dash (–) and minus (−) used by TI PDFs for negative values.
_NUM_TOK = re.compile(
    r"^[±\+\-–−]?\d[\d.,]*"
    r"(?:[kMGTµmnpfuKΩ°%Cc]*)$"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _looks_like_spec_table(headers: list) -> bool:
    words = {str(h or "").strip().upper() for h in headers}
    has_value = bool(words & {"MIN", "MAX", "TYP", "NOM", "VALUE"})
    has_unit  = bool(words & {"UNIT", "UNITS"})
    return has_value and has_unit


def _build_col_map(headers: list) -> dict:
    """
    Returns a dict with keys:  min, typ, max, unit, conditions, param_cols (list[int]).
    'param_cols' is the list of column indices that are not a recognised value column.
    """
    m: dict = {"param_cols": []}
    for i, h in enumerate(headers):
        hu = str(h or "").strip().upper()
        if hu in ("MIN", "MINIMUM"):
            m["min"] = i
        elif hu in ("TYP", "NOM", "TYPICAL", "NOMINAL"):
            m["typ"] = i
        elif hu in ("MAX", "MAXIMUM"):
            m["max"] = i
        elif hu in ("UNIT", "UNITS"):
            m["unit"] = i
        elif hu in ("VALUE",):
            # ESD-rating style: single value column — treat as max
            m.setdefault("max", i)
        elif "CONDITION" in hu or "TEST" in hu:
            m["conditions"] = i
        else:
            m["param_cols"].append(i)
    return m


def _unpack_values(mn: str, ty: str, mx: str) -> tuple[str, str, str]:
    """
    When TYP and MAX cells are empty but MIN contains space-separated numbers,
    unpack them:
      "3.05 3.20 3.31"  →  ("3.05", "3.20", "3.31")
      "17 24"           →  ("17",   "",     "24")
    Does nothing if TYP or MAX already have a value.
    """
    if ty.strip() or mx.strip():
        return mn, ty, mx
    parts = mn.split()
    nums = [p for p in parts if _NUM_TOK.match(p)]
    if nums and len(nums) == len(parts):
        if len(nums) == 3:
            return nums[0], nums[1], nums[2]
        if len(nums) == 2:
            return nums[0], "", nums[1]
    return mn, ty, mx


def _is_group_row(cells: list[str], col_map: dict) -> bool:
    """
    True when ALL value columns (min/typ/max/unit/conditions) are empty
    and the first parameter column has all-caps-style text.
    These rows are sub-section headers like "QUIESCENT CURRENTS".
    """
    value_keys = ("min", "typ", "max", "unit", "conditions")
    for key in value_keys:
        idx = col_map.get(key)
        if idx is not None and idx < len(cells) and cells[idx]:
            return False
    param_cols: list[int] = col_map.get("param_cols", [])
    if not param_cols:
        return False
    first_text = cells[param_cols[0]] if param_cols[0] < len(cells) else ""
    if not first_text:
        return False
    # Allow digits, spaces, slashes, parens, hyphens — must be mostly upper-case
    upper_ratio = sum(1 for c in first_text if c.isupper()) / max(len(first_text), 1)
    return upper_ratio >= 0.6 and len(first_text) <= 80


def _clean_text(s: str) -> str:
    """Normalise whitespace/newlines in a cell value.

    TI PDFs render subscripts as separate lines, e.g.:
      "I\nQ_BAT_ON"  →  "I_Q_BAT_ON"   (short 1–2 char prefix + subscript)
      "Electrostatic\ndischarge" → "Electrostatic discharge"  (regular words)
    """
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if not lines:
        return ""
    result = lines[0]
    for ln in lines[1:]:
        # Use underscore ONLY when the current accumulated text is a short symbol
        # prefix (1-3 chars, all alphanumeric) — the classic TI subscript layout.
        is_subscript = (
            1 <= len(result) <= 3
            and result.replace("_", "").isalnum()
            and ln and ln[0].isalnum()
        )
        result += ("_" if is_subscript else " ") + ln
    # Collapse multiple spaces
    return re.sub(r"  +", " ", result).strip()


def _conditions_from_text(text: str, heading_end: int) -> str:
    """Extract the global test conditions text that appears between the section
    heading and the first table header row."""
    after = text[heading_end:]
    lines = [ln.strip() for ln in after.splitlines() if ln.strip()]
    cond_parts = []
    for ln in lines[:6]:
        # Stop when we hit what looks like a table header row
        if re.search(r"\b(PARAMETER|TEST CONDITIONS|MIN|MAX|TYP|UNIT)\b", ln, re.IGNORECASE):
            break
        cond_parts.append(ln)
    return " ".join(cond_parts).strip()


# ── Extractor ──────────────────────────────────────────────────────────────────

class ElecCharExtractor:

    def __init__(self, pdf_path: Path, debug: bool = False):
        self.pdf_path = Path(pdf_path)
        self.debug = debug

    def extract(self) -> list[ElecSection]:
        sections: list[ElecSection] = []
        pending: Optional[ElecSection] = None
        # carry-forward: last non-empty symbol/parameter (for merged cells)
        last_symbol: str = ""
        last_param: str = ""

        with pdfplumber.open(self.pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                tables = page.extract_tables()

                spec_tables = [t for t in tables
                               if t and len(t) > 1 and _looks_like_spec_table(t[0])]

                if not spec_tables:
                    if not _CONTINUED_RE.search(text):
                        pending = None
                        last_symbol = last_param = ""
                    continue

                is_continuation = _CONTINUED_RE.search(text) is not None

                # New section headings on this page — filter out "(continued)" headings,
                # whether "(continued)" appears in the matched name or right after it.
                section_matches = [
                    m for m in _SECTION_RE.finditer(text)
                    if not re.search(r"\(continued\)", m.group("name"), re.IGNORECASE)
                    and not re.search(r"\(continued\)",
                                      text[m.end(): m.end() + 30], re.IGNORECASE)
                ]

                n_new  = len(section_matches)
                n_cont = max(0, len(spec_tables) - n_new) if is_continuation else 0

                if self.debug and (section_matches or spec_tables):
                    print(f"  p{page_num}: {n_new} section(s), "
                          f"{len(spec_tables)} spec table(s), cont={is_continuation}")
                    for sm in section_matches:
                        print(f"    section: {sm.group('name')!r}")

                sec_idx = 0
                current_group = ""

                for tbl_idx, tbl in enumerate(spec_tables):
                    col_map = _build_col_map(tbl[0])

                    if tbl_idx < n_cont:
                        # Continuation of the pending section
                        sec = pending
                        if sec is None:
                            sec = ElecSection("Unknown", "", page_num)
                            sections.append(sec)
                            pending = sec
                    elif sec_idx < len(section_matches):
                        sm = section_matches[sec_idx]
                        sec = ElecSection(
                            name=sm.group("name").strip(),
                            conditions=_conditions_from_text(text, sm.end()),
                            source_page=page_num,
                        )
                        sections.append(sec)
                        pending = sec
                        sec_idx += 1
                        current_group = ""
                        last_symbol = last_param = ""
                    else:
                        sec = pending or ElecSection("Unknown", "", page_num)
                        if sec not in sections:
                            sections.append(sec)
                        pending = sec

                    param_cols: list[int] = col_map.get("param_cols", [])

                    for row in tbl[1:]:
                        cells = [str(c or "").strip() for c in row]

                        # Sub-group header row?
                        if _is_group_row(cells, col_map):
                            first_txt = cells[param_cols[0]] if param_cols else ""
                            if first_txt:
                                current_group = first_txt
                                last_symbol = last_param = ""
                            continue

                        # Extract parameter columns
                        pc = [cells[i] for i in param_cols if i < len(cells)]
                        symbol    = _clean_text(pc[0]) if pc else ""
                        parameter = _clean_text(pc[1]) if len(pc) > 1 else ""

                        # Carry-forward merged cells: if BOTH param cols are blank,
                        # this row continues the previous entry's symbol/parameter
                        if not symbol and not parameter:
                            symbol    = last_symbol
                            parameter = last_param
                        else:
                            if symbol:
                                last_symbol = symbol
                            if parameter:
                                last_param = parameter

                        # Skip rows with no useful content at all
                        def _get(key: str) -> str:
                            idx = col_map.get(key)
                            return cells[idx] if idx is not None and idx < len(cells) else ""

                        raw_min = _get("min")
                        raw_typ = _get("typ")
                        raw_max = _get("max")
                        unit    = _clean_text(_get("unit"))
                        conds   = _clean_text(_get("conditions"))

                        if not any([symbol, parameter, raw_min, raw_typ, raw_max, unit]):
                            continue

                        mn, ty, mx = _unpack_values(raw_min, raw_typ, raw_max)

                        sec.specs.append(ElecSpec(
                            symbol=symbol,
                            parameter=parameter,
                            conditions=conds,
                            min=mn,
                            typ=ty,
                            max=mx,
                            unit=unit,
                            group=current_group,
                            source_page=page_num,
                        ))

        return sections
