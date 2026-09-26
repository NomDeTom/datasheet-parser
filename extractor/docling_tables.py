"""
docling_tables.py — Docling's reconstructed tables -> typed spec rows, vendor-independently.

TableFormer recovers column structure that pdfplumber loses (symbol/parameter split, per-variant
qualifiers), but stops at text. This module types those grids into ElecSpec-shaped rows with a
page number and a confidence level, and repairs the two defects measured on real sheets:

  * merged sub-rows — one cell holding several rows' values ("600 1.2", units "nA A"). Split only
    when every value and unit cell carries the same token count; otherwise kept whole and flagged.
  * Symbol-font glyphs — some vendors (Semtech) encode µ, Ω, ° … as private-use code points
    (U+F06D for µ). Docling passes them through untranslated, so they are invisible in most
    viewers and µA reads as A. fix_symbol_pua() maps them back; applied to every cell.
    repair_units() is a fallback for a glyph that is genuinely missing: pypdf's text of the same
    page says how many micro-units the page has, and a bare unit is restored only when that
    count is unambiguous.

Reads the chunk JSON written by docling_extract.py; needs only the standard library.
"""
import json
import re
from collections import Counter
from pathlib import Path

# Adobe Symbol font, as mapped into the private-use area at U+F000 + code
SYMBOL_PUA = {
    0xF06D: "µ", 0xF057: "Ω", 0xF0B0: "°", 0xF0B1: "±", 0xF0A3: "≤", 0xF0B3: "≥",
    0xF044: "Δ", 0xF071: "θ", 0xF074: "τ", 0xF062: "β", 0xF061: "α", 0xF070: "π",
    0xF0B4: "×", 0xF0D6: "√", 0xF02D: "−", 0xF0AE: "→", 0xF0BB: "≈", 0xF0B9: "≠",
    0xF06C: "λ", 0xF077: "ω", 0xF0BE: "—", 0xF020: " ",
    # bullets: Symbol 0xB7, and Wingdings code points that sheets use as list markers
    0xF0B7: "•", 0xF0A7: "•", 0xF0D8: "•",
    # Symbol's sans-serif ® © ™ in the U+F8E8… block
    0xF8E8: "®", 0xF8E9: "©", 0xF8EA: "™",
}


def fix_symbol_pua(text: str) -> str:
    return text.translate(SYMBOL_PUA)


def norm_unit(unit: str) -> str:
    """One spelling per unit: Greek mu (U+03BC) -> micro sign (U+00B5), Ohm sign -> Omega."""
    return fix_symbol_pua(unit or "").replace("\u03bc", "\u00b5").replace("\u2126", "\u03a9") \
        .replace("\u2103", "\u00b0C").replace("\u2109", "\u00b0F").strip()


HEADER_MAP = {
    "symbol": "symbol", "sym": "symbol",
    "parameter": "parameter", "parameters": "parameter", "description": "parameter",
    "characteristic": "parameter", "characteristics": "parameter", "item": "parameter",
    "mode": "parameter",
    "conditions": "conditions", "testconditions": "conditions", "testcondition": "conditions",
    "condition": "conditions", "test": "conditions", "comments": "conditions",
    "min": "min", "minimum": "min", "min.": "min",
    "typ": "typ", "typical": "typ", "nom": "typ", "nominal": "typ",
    "max": "max", "maximum": "max",
    "unit": "unit", "units": "unit", "uni": "unit",
    "value": "typ", "rating": "max", "ratings": "max",
}
VALUES = ("min", "typ", "max")
_EMPTY = {"", "-", "–", "—", "n/a", "na", "/"}
_NUM = re.compile(r"^[-+−–]?\d*\.?\d+(?:[eE][-+]?\d+)?$")
_MICRO_UNIT = re.compile(r"(?:|[µμ])\s?(Hz|A|V|W|F|H|s|S|Ω|m)(?![A-Za-z])")
_BARE_UNIT = re.compile(r"(?<![A-Za-zµμ])(Hz|A|V|W|F|H|s|S|m)(?![A-Za-z])")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9.]", "", (s or "").lower()).rstrip(".")


def to_number(tok: str):
    t = (tok or "").strip().replace("−", "-").replace("–", "-").replace(",", "")
    t = re.sub(r"^([-+])\s+(?=[\d.])", r"\1", t)   # '– 0.3' (pypdfium2 backend)
    if t.lower() in _EMPTY:
        return None
    return float(t) if _NUM.match(t) else None


# ── reading the cache ────────────────────────────────────────────────────────
def iter_tables(cache_dir: Path):
    """Yield (table, section_text, page) in reading order across every chunk."""
    for js in sorted(cache_dir.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json")):
        doc = json.loads(js.read_text(encoding="utf-8"))
        texts, tables = doc.get("texts", []), doc.get("tables", [])
        section = ""
        for ref in _walk(doc, doc.get("body", {})):
            kind, _, idx = ref.lstrip("#/").partition("/")
            if kind == "texts":
                t = texts[int(idx)]
                if t.get("label") in ("section_header", "title", "caption"):
                    section = t.get("text", "").strip()
            elif kind == "tables":
                t = tables[int(idx)]
                if t.get("label") == "document_index":
                    continue              # contents pages
                cap = _caption(doc, t)
                page = (t.get("prov") or [{}])[0].get("page_no")
                yield t, cap or section, page


def _walk(doc, node):
    for child in node.get("children", []):
        ref = child.get("$ref", "")
        yield ref
        kind, _, idx = ref.lstrip("#/").partition("/")
        if kind == "groups":
            yield from _walk(doc, doc["groups"][int(idx)])


def _caption(doc, table):
    for c in table.get("captions", []):
        kind, _, idx = c.get("$ref", "").lstrip("#/").partition("/")
        if kind == "texts":
            return doc["texts"][int(idx)].get("text", "").strip()
    return ""


# ── typing ───────────────────────────────────────────────────────────────────
def _header(grid):
    """-> (last header row index, {col: field}) or (-1, {})."""
    for h in range(min(3, len(grid))):
        fields, seen = {}, set()
        for j, cell in enumerate(grid[h]):
            f = HEADER_MAP.get(norm(cell.get("text")))
            if f == "parameter" and "parameter" in seen and "symbol" not in seen \
                    and fields.get(j - 1) == "parameter":
                # TI's PARAMETER header spans two columns — symbol, then description — and
                # Docling repeats the header text in both cells
                fields[j - 1], fields[j] = "symbol", "parameter"
                seen.add("symbol")
                continue
            if f and f not in seen:          # any other repeated header keeps its first column
                fields[j] = f
                seen.add(f)
        if sum(1 for f in fields.values() if f in VALUES) >= 1 and \
                ({"unit"} & seen) and ({"symbol", "parameter"} & seen or 0 not in fields):
            last = h
            # stacked headers: absorb following rows flagged column_header
            while last + 1 < len(grid) and any(c.get("column_header") for c in grid[last + 1]):
                last += 1
            if 0 not in fields and fields.get(1) == "parameter" and "symbol" not in seen \
                    and _looks_like_symbols(grid[last + 1:]):
                # PARAMETER centred over two columns lands in the second cell only (pypdfium2
                # backend, fast table mode): the unlabelled first column is the symbol
                fields[0] = "symbol"
            return last, fields
    return -1, {}


def _looks_like_symbols(body):
    col = [" ".join((r[0].get("text") or "").split()) for r in body if r]
    col = [c for c in col if c]
    return bool(col) and sum(len(c) <= 14 for c in col) >= len(col) / 2


def _symbol(txt):
    """'I Q(VIN)' -> 'IQ(VIN)': the pypdfium2 backend puts a space before a subscript."""
    return re.sub(r"^([A-Za-zΔθτηψα])\s+(?=[A-Za-z0-9(])", r"\1", txt)


def _cells(row, fields):
    rec = {}
    for j, cell in enumerate(row):
        f = fields.get(j)
        txt = fix_symbol_pua(" ".join((cell.get("text") or "").split()))
        if f == "symbol":
            txt = _symbol(txt)
        if f and txt and f not in rec:
            rec[f] = txt
    # text in unmapped leading columns is part of the parameter description
    lead = [fix_symbol_pua(" ".join((row[j].get("text") or "").split())) for j in range(len(row))
            if j not in fields and j < min(fields or [0])]
    if lead and not rec.get("parameter"):
        rec["parameter"] = " ".join(x for x in lead if x)
    return rec


def _split(rec):
    """Split a merged multi-row record when every value cell agrees on the token count.

    The unit cell must either agree too ("nA A" for two rows) or hold a single unit that spans
    the sub-rows (TPS61230 UVLO: typ "2.0 2.1", max "2.1 2.2", unit "V") — then it is shared."""
    vcols = [c for c in VALUES if rec.get(c)]
    counts = {len(rec[c].split()) for c in vcols}
    if len(counts) != 1 or counts == {1} or not rec.get("unit"):
        return [rec], False
    n = counts.pop()
    units = rec["unit"].split()
    if len(units) not in (1, n):
        return [rec], False
    out = []
    for i in range(n):
        r = {k: v for k, v in rec.items() if k not in vcols and k != "unit"}
        for c in vcols:
            r[c] = rec[c].split()[i]
        r["unit"] = units[i] if len(units) == n else units[0]
        out.append(r)
    return out, True


def type_rows(table, section, page):
    grid = (table.get("data") or {}).get("grid") or []
    h, fields = _header(grid)
    if h < 0:
        return []
    recs, group = [], ""
    for row in grid[h + 1:]:
        rec = _cells(row, fields)
        if not any(rec.get(v) for v in VALUES):
            label = rec.get("parameter") or rec.get("symbol") or ""
            if label and not rec.get("unit"):
                group = label                 # sub-heading row, e.g. "QUIESCENT CURRENTS"
            continue
        recs.append((rec, group, []))
    _join_fragments(recs)
    _label_orphans(recs)
    rows = []
    for rec, group, extra in recs:
        if not (rec.get("symbol") or rec.get("parameter")):
            continue
        parts, was_split = _split(rec)
        for r in parts:
            flags = (["split_from_merged_row"] if was_split else []) + extra
            nums = {v: to_number(r.get(v)) for v in VALUES}
            unparsed = [v for v in VALUES
                        if r.get(v) and r[v].lower() not in _EMPTY and nums[v] is None]
            if unparsed:
                flags.append("value_not_numeric")
            rows.append({
                "section": section, "page": page, "group": group,
                "symbol": r.get("symbol", ""), "parameter": r.get("parameter", ""),
                "conditions": r.get("conditions", ""),
                "min": nums["min"], "typ": nums["typ"], "max": nums["max"],
                "unit": norm_unit(r.get("unit", "")),
                "raw": {v: r.get(v, "") for v in VALUES} if unparsed else None,
                "extractor": "docling",
                "confidence": "low" if set(flags) - {"symbol_joined"} else "medium",
                "flags": flags,
            })
    return rows


def _join_fragments(recs):
    """A symbol split across two rows — 'V' then 'UVLO' (pypdfium2 backend, subscript on its
    own line) — becomes 'VUVLO' on both rows; the second row takes the first's description."""
    for (a, _, fa), (b, _, fb) in zip(recs, recs[1:]):
        sa, sb = a.get("symbol", ""), b.get("symbol", "")
        if len(sa) == 1 and sa.isalpha() and sb and sb != sa and not b.get("parameter"):
            a["symbol"] = b["symbol"] = sa + sb
            b["parameter"] = a.get("parameter", "")
            fa.append("symbol_joined")
            fb.append("symbol_joined")


def _common_prefix(x, y):
    x, y = norm(x), norm(y)
    n = 0
    while n < min(len(x), len(y)) and x[n] == y[n]:
        n += 1
    return n


def _label_orphans(recs):
    """A row with values but no symbol or description belongs to a merged label cell of a
    neighbour. The label sits on the row above (TI's usual layout) or below (the pypdfium2
    backend places a vertically centred label on the later row): take the neighbour whose test
    conditions share the longer prefix with the orphan's, the row above on a tie."""
    labelled = lambda r: r.get("symbol") or r.get("parameter")
    orphans = [i for i, (r, _, _) in enumerate(recs) if not labelled(r)]
    for i in orphans:
        prev = next((j for j in range(i - 1, -1, -1) if labelled(recs[j][0])), None)
        nxt = next((j for j in range(i + 1, len(recs)) if labelled(recs[j][0])), None)
        if prev is not None and prev < i - 1 and nxt is not None and nxt > i + 1:
            continue                     # a run of orphans between two labels: too ambiguous
        cond = recs[i][0].get("conditions", "")
        score = lambda j: -1 if j is None else _common_prefix(cond, recs[j][0].get("conditions", ""))
        src = prev if score(prev) >= score(nxt) and prev is not None else nxt
        if src is None:
            continue
        for k in ("symbol", "parameter"):
            if recs[src][0].get(k):
                recs[i][0][k] = recs[src][0][k]
        if not recs[i][0].get("unit") and recs[src][0].get("unit"):
            recs[i][0]["unit"] = recs[src][0]["unit"]
        recs[i][2].append("label_from_neighbour")
    # the unit cell of a split row sits on one half only
    for i, (r, _, extra) in enumerate(recs):
        if r.get("unit") or not r.get("symbol"):
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(recs) and recs[j][0].get("symbol") == r["symbol"] and recs[j][0].get("unit"):
                r["unit"] = recs[j][0]["unit"]
                extra.append("unit_from_neighbour")
                break


# ── unit repair against the text layer ──────────────────────────────────────
def repair_units(rows, page_text: dict):
    """Restore µ dropped by Docling, using pypdf's text of the same page as the witness.

    page_text: {page_no: pypdf text, PUA intact}. Returns the number of cells repaired.
    """
    by_page = {}
    for r in rows:
        by_page.setdefault(r["page"], []).append(r)
    fixed = 0
    for page, prs in by_page.items():
        text = page_text.get(page) or ""
        micro = Counter(_MICRO_UNIT.findall(text))
        if not micro:
            continue
        plain = Counter(u for u in _BARE_UNIT.findall(_MICRO_UNIT.sub(" ", text)))
        for unit, n_micro in micro.items():
            bare = [r for r in prs if r["unit"] == unit]
            if not bare:
                continue
            if plain[unit] == 0:          # the page has no plain unit of this kind: all are micro
                for r in bare:
                    r["unit"] = "µ" + unit
                    r["flags"].append("unit_restored")
                    r["confidence"] = "low"
                    fixed += 1
            else:
                for r in bare:
                    r["flags"].append("unit_maybe_micro")
                    r["confidence"] = "low"
    return fixed


def extract(cache_dir: Path, page_text: dict | None = None):
    rows = []
    for table, section, page in iter_tables(cache_dir):
        rows += type_rows(table, section, page)
    repaired = repair_units(rows, page_text or {})
    return rows, repaired
