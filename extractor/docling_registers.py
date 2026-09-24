"""
docling_registers.py — register maps from Docling's headings and tables.

The same reading as i2c_registers.py (pdfplumber), on Docling's structure instead of page text:
a register heading opens a register, and every bit-field table after it (header row with "Bit"
plus Field/Type/Reset/…) belongs to it until the next heading. The chunks of a docling_extract.py
cache are walked in reading order with the open register carried across chunk boundaries, so a
field table that TI continues onto the next page — or that 20-page chunking split — joins the
register it started in. Bit-layout diagrams (a row of bit numbers) are not field tables.

Register identity, which pdfplumber gets only from one TI heading form, comes from any of:

  1. a heading with an address: "9.5.1.1 REG00_… Register (Offset = 0h) [reset = X]",
     "7.6.2.1 Configuration Register (address = 00h)", "7.1.1 CONFIG1 Register (Address = 0x10h)"
  2. a heading naming a mnemonic: "Pin Status Register (PIN_STAT):" (WCH), whose address is then
     looked up in the document's register summary table (a table with a name column and an
     address/offset column)
  3. a caption "Register CONTROL2 Format" naming the register the next field table belongs to
  4. with no heading at all, a field table whose bits restart at the top (15 or 7) after the
     previous one reached bit 0 is a new register, not a continuation

Standard library only; column mapping and row parsing are shared with i2c_registers.py so both
readers interpret a field table identically.
"""
import json
import re
from pathlib import Path

from extractor.docling_tables import _walk, fix_symbol_pua
from extractor.i2c_registers import Register, _build_col_map, _is_bitfield_table, _row_to_field

_ADDR = r"(?:0x)?[0-9A-Fa-f]{1,4}h?"
_HEAD_ADDR = re.compile(
    r"(?:^|\s)(?:\d+(?:\.\d+)*\s+)?(?P<name>[A-Za-z][^\n]*?)\s+Register\s*"
    rf"\(\s*(?:Offset|Address|Addr)\s*=\s*(?P<addr>{_ADDR})\s*\)"
    r"(?:[^\n\[]*\[\s*reset\s*=\s*(?P<reset>[^\]]*)\])?", re.I)
# TI's newer form: "7.6.1.2 Register CONTROL1 (Register address: 0x02; Default: 0x08)"
_HEAD_REG_FIRST = re.compile(
    r"Register\s+(?P<mn>[A-Z][A-Z0-9_]+)\s*\(\s*Register\s+address:\s*(?P<addr>(?:0x)?[0-9A-Fa-f]+h?)"
    r"\s*(?:;\s*Default:\s*(?P<reset>[^)]*))?\)", re.I)
# "Table 7-21. MANUFACTURE_ID Register Field Descriptions" (a caption naming the register)
_FIELD_CAPTION = re.compile(r"\b(?P<mn>[A-Z][A-Z0-9_]{2,})\s+Register\s+Field\s+Descriptions(?!\s*\(continued)")
# "7.1.11 Flags Register", "7.1.8 CURRENT Registers" (a numbered section per register or family)
_HEAD_SECTION = re.compile(r"^\d+(?:\.\d+){1,}\s+(?P<name>[A-Za-z][A-Za-z0-9_ ]*?)\s+Registers?\s*$")
_HEAD_MNEMONIC = re.compile(r"(?P<name>[A-Za-z][\w /-]*?Register)\s*\(\s*(?P<mn>[A-Z][A-Z0-9_]+)\s*\)")
_CAPTION = re.compile(r"Register\s+(?P<mn>[A-Z][A-Z0-9_]+)\s+Format", re.I)
_RESET = re.compile(r"\[\s*reset\s*=\s*([^\]]*)\]", re.I)
_MNEMONIC_CELL = re.compile(r"^[A-Z][A-Z0-9_]{1,}$")


def normalise_addr(raw: str) -> str:
    """'2Eh', '0x10h', '0x01', '00h', '6' -> '0x2E', '0x10', '0x01', '0x00', '0x06'."""
    v = raw.strip()
    hexa = v.lower().startswith("0x") or v.lower().endswith("h")
    v = re.sub(r"(?i)^0x|h$", "", v)
    try:
        n = int(v, 16) if hexa or re.search(r"[A-Fa-f]", v) else int(v)
    except ValueError:
        return "?"
    return f"0x{n:02X}"


def _items(cache: Path):
    """(kind, obj, page) in reading order across every chunk of the cache."""
    for js in sorted(cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json")):
        doc = json.loads(js.read_text(encoding="utf-8"))
        for ref in _walk(doc, doc.get("body", {})):
            kind, _, idx = ref.lstrip("#/").partition("/")
            if kind not in ("texts", "tables"):
                continue
            obj = doc[kind][int(idx)]
            yield kind, obj, (obj.get("prov") or [{}])[0].get("page_no")


def _grid(table):
    return [[fix_symbol_pua(" ".join((c.get("text") or "").split())) for c in row]
            for row in (table.get("data") or {}).get("grid") or []]


def _top_bit(bits: str):
    m = re.match(r"\s*(\d+)", bits or "")
    return int(m.group(1)) if m else None


def _low_bit(bits: str):
    nums = re.findall(r"\d+", bits or "")
    return int(nums[-1]) if nums else None


_last_layout = {}


def _summary_addresses(rows):
    """{mnemonic: address} from a register summary table, found by its header row. A summary table
    continued onto the next page arrives without its header; if it has the same column count as
    the last summary table and starts with a mnemonic, the earlier layout is reused (CH211)."""
    head = [c.lower() for c in rows[0]]
    addr_col = next((i for i, h in enumerate(head) if "addr" in h or "offset" in h), None)
    body = rows[1:]
    if addr_col is None:
        if _last_layout.get("ncols") == len(rows[0]) and any(_MNEMONIC_CELL.match(c) for c in rows[0]):
            addr_col, body = _last_layout["addr_col"], rows
        else:
            return {}
    _last_layout.update(ncols=len(rows[0]), addr_col=addr_col)
    out = {}
    for row in body:
        names = [c for i, c in enumerate(row) if i != addr_col and _MNEMONIC_CELL.match(c)]
        if names and addr_col < len(row) and row[addr_col]:
            a = normalise_addr(row[addr_col].split()[0])
            if a != "?":
                out.setdefault(names[0], a)
    return out


def extract(cache: Path) -> list:
    registers, summary = [], {}
    current, heading_since_table, expect_reset, caption_name = None, False, False, None

    def open_register(name, addr="?", reset="", page=None, mnemonic=None):
        nonlocal current, heading_since_table
        current = Register(name=name, address=addr, reset=reset, source_page=page)
        current.mnemonic = mnemonic
        registers.append(current)
        heading_since_table = True

    _last_layout.clear()
    for kind, obj, page in _items(cache):
        if kind == "texts":
            text = fix_symbol_pua(obj.get("text") or "")
            m = _HEAD_REG_FIRST.search(text)
            if m:
                open_register(m.group("mn"), normalise_addr(m.group("addr")),
                              (m.group("reset") or "").strip(), page, m.group("mn"))
                continue
            m = _HEAD_ADDR.search(text)
            if m:
                open_register(m.group("name").strip(), normalise_addr(m.group("addr")),
                              (m.group("reset") or "").strip(), page)
                expect_reset = not current.reset
                continue
            m = _HEAD_MNEMONIC.search(text)
            if m and obj.get("label") in ("section_header", "title", "caption", "text"):
                open_register(m.group("mn"), page=page, mnemonic=m.group("mn"))
                continue
            m = _CAPTION.search(text) or _FIELD_CAPTION.search(text)
            if m:
                caption_name = m.group("mn")
                continue
            m = _HEAD_SECTION.match(text.strip())
            if m and obj.get("label") == "section_header":
                name = m.group("name").strip()
                open_register(name, page=page, mnemonic=name.upper().replace(" ", "_"))
                continue
            if expect_reset and current is not None:
                r = _RESET.search(text)
                if r:
                    current.reset = r.group(1).strip()
                expect_reset = False
            continue

        rows = _grid(obj)
        if len(rows) < 2 or obj.get("label") == "document_index":
            continue
        header_i = next((i for i, r in enumerate(rows[:3]) if _is_bitfield_table(r)), None)
        if header_i is None:
            summary.update(_summary_addresses(rows))
            continue
        col_map = _build_col_map(rows[header_i])
        fields = [bf for row in rows[header_i + 1:]
                  if (bf := _row_to_field(row, col_map)) and not _is_bitfield_table(row)]
        if not fields:
            continue
        restarts = (current is not None and current.fields and not heading_since_table
                    and (_top_bit(fields[0].bits) or 0) >= (_top_bit(current.fields[0].bits) or 0)
                    and _low_bit(current.fields[-1].bits) == 0)
        if current is None or restarts or (caption_name and not heading_since_table):
            open_register(caption_name or "UNKNOWN", page=page, mnemonic=caption_name)
        if caption_name and current.name == "UNKNOWN":
            current.name, current.mnemonic = caption_name, caption_name
        caption_name = None
        current.fields.extend(fields)
        heading_since_table = False

    for r in registers:
        mn = getattr(r, "mnemonic", None) or r.name
        if r.address == "?" and mn in summary:
            r.address = summary[mn]
    return [r for r in registers if r.fields]


def as_dicts(registers):
    return [{"name": r.name, "address": r.address, "reset": r.reset, "page": r.source_page,
             "fields": [{"bits": f.bits, "name": f.name, "access": f.access, "reset": f.reset,
                         "description": f.description} for f in r.fields]}
            for r in registers]
