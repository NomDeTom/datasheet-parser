"""
corrections.py — hand corrections written on a document's card, in one free-text list property:

    fix:
      - name = LR2021_DS_V1.1
      - product_type = rf
      - parts = LR2021, LR2021X

One `field = value` per entry. `name` renames the document everywhere (file_pdfs.py rename),
and `superseded_by = <document>` marks it `SUPERSEDED - `; both entries are removed once done.
Every other field overrides what extraction and classification decided, in the `.data.json`,
so the card, the database and the views all carry the corrected value. A correction to
`product_type`, `topology` or `manufacturer` records `card` as the deciding source in
`classified_by` / `topology_by` / `manufacturer_by`. An entry that names no known field or holds
an invalid value is not applied; it is listed on the card and flagged `fix_rejected`.

`=` rather than `:` because `- product_type: rf` is a YAML mapping, not a string.
"""
import json
import re

import classify

DOC_TYPES = ("datasheet", "user_manual", "errata", "reference_manual", "app_note")
TOPOLOGIES = ("buck", "boost", "buck-boost", "linear")
# field -> allowed values (None: any non-empty text); `parts` is a comma-separated list
FIELDS = {
    "name": None, "doc_type": DOC_TYPES, "product_type": tuple(classify.TYPES),
    "topology": TOPOLOGIES, "manufacturer": None, "parts": None, "title": None, "revision": None,
    "superseded_by": None,
}
RENAMING = ("name", "superseded_by")  # applied by file_pdfs.py rename, not to the .data.json
KEY = "fix"
REFUSED_KEY = "fix_refused"          # why the last `name` correction was not applied


def _scalar(v: str):
    v = v.strip()
    if not v:
        return ""
    try:
        return json.loads(v)
    except ValueError:
        return v[1:-1] if len(v) > 1 and v[0] == v[-1] == "'" else v


def parse_frontmatter(text: str) -> dict:
    """Flat frontmatter as {key: value}; flow values (`[..]`, `".."`, numbers) and the block
    lists Obsidian writes when a list property is edited (`key:` then `  - item`) both read."""
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    out, key = {}, None
    for line in (m.group(1) if m else "").splitlines():
        item = re.match(r"\s*-\s+(.*)$", line) or re.match(r"\s*-$", line)
        if item and key is not None and (out[key] == "" or isinstance(out[key], list)):
            out[key] = (out[key] or []) + [_scalar(item.group(1) if item.groups() else "")]
            continue
        k = re.match(r"([^\s:#][^:]*):(.*)$", line)
        if k:
            key = k.group(1).strip()
            out[key] = _scalar(k.group(2))
        elif not line.startswith(" "):
            key = None
    return out


def read(props: dict):
    """-> ({field: value}, [problem, ...]) from a card's properties."""
    raw = props.get(KEY)
    entries = raw if isinstance(raw, list) else [raw] if raw not in (None, "") else []
    fixes, problems = {}, []
    for e in entries:
        e = str(e if e is not None else "").strip()
        if not e:
            continue
        field, sep, value = e.partition("=")
        field, value = re.sub(r"[\s-]+", "_", field.strip().lower()), value.strip()
        if not sep:
            problems.append(f"`{e}`: expected `field = value`")
        elif field not in FIELDS:
            problems.append(f"`{e}`: no field `{field}` (one of {', '.join(FIELDS)})")
        elif not value:
            problems.append(f"`{e}`: empty value")
        elif FIELDS[field] and value not in FIELDS[field]:
            problems.append(f"`{e}`: `{value}` is not one of {', '.join(FIELDS[field])}")
        elif field in fixes:
            problems.append(f"`{e}`: `{field}` corrected twice; the first is used")
        else:
            fixes[field] = [p.strip() for p in value.split(",") if p.strip()] \
                if field == "parts" else value
    return fixes, problems


def apply(document: dict, fixes: dict):
    """Overwrite a `.data.json` document block with the card's corrections (not the renaming ones)."""
    applied = {k: v for k, v in fixes.items() if k not in RENAMING}
    for k, v in applied.items():
        if k == "product_type":
            document.update(product_type=v, classified_by="card",
                            converts_voltage=v in classify.CONVERTS)
        elif k == "topology":
            document.update(topology_class=v, topology_by="card")
        elif k == "manufacturer":
            document.update(manufacturer=v, manufacturer_by="card")
        else:
            document[k] = v
    document["corrections"] = applied
    return document


def entry(field: str, value) -> str:
    return f"{field} = {', '.join(value) if isinstance(value, list) else value}"


def block(key: str, items) -> list:
    """A block list property the way Obsidian writes one; [] when there is nothing left."""
    def q(s):
        return json.dumps(s, ensure_ascii=False) if re.search(r"[:#\[\]{}\"'&*!|>%@`]|^-|^\s|\s$", s) else s
    return [f"{key}:"] + [f"  - {q(str(i))}" for i in items] if items else []
