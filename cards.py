#!/usr/bin/env python
"""
cards.py — write or refresh the Obsidian card beside every PDF that has a `.data.json`.

    python cards.py                 # every document
    python cards.py --only SX126

A card, `<doc>.md` beside `<doc>.pdf`, is what Obsidian Bases and Dataview query: flat
frontmatter properties generated from `<doc>.data.json`, plus a short body. Ownership is split so
regeneration never loses anything a person wrote:

  generated   the frontmatter keys in OWNED, and the body above `## Notes`
  yours       every other frontmatter key (category, tags, aliases, description, …)
              and everything from the `## Notes` heading down

Papers are skipped: they carry their own hand-written notes.
"""
import argparse
import json
import re
from pathlib import Path

import vaultpath

NOTES = "## Notes"
ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}
OWNED = [
    "doc_type", "part", "parts", "manufacturer", "title", "revision", "pages", "lcsc",
    "pdf", "text", "confidence", "contested", "verified_params",
    "vin_min", "vin_max", "confidence_vin", "vout_min", "vout_max", "confidence_vout",
    "iout_max", "confidence_iout", "fsw_min_khz", "fsw_max_khz", "confidence_fsw",
    "spec_rows", "spec_rows_high", "registers", "flags",
    "product_type", "topology", "converts_voltage", "steps_down",
    "is_converter", "is_charger", "is_ldo", "is_power_monitor", "is_protection", "is_load_switch",
    "is_mcu", "is_rf", "is_sensor", "is_buck", "is_boost", "is_buck_boost", "is_linear",
    "ti_part", "lifecycle", "generated",
]
# Properties a person owns and cards.py never writes: category, verified (list of params checked),
# verified_on, verified_note, lcsc (correction), original_name, tags, aliases, …
# The schema-1 cards wrote a generated boolean `verified`; that one is dropped on regeneration.
LEGACY_OWNED = {"verified": (True, False, "true", "false")}


def yaml_value(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(str(v), ensure_ascii=False)


def split_card(text: str):
    """-> (frontmatter lines as {key: raw line block}, key order, notes section text)."""
    fm, order, body = {}, [], text
    m = re.match(r"---\n(.*?)\n---\n?", text, re.S)
    if m:
        body = text[m.end():]
        key = None
        for line in m.group(1).splitlines():
            k = re.match(r"([^\s:#][^:]*):", line)
            if k and not line.startswith((" ", "-")):
                key = k.group(1).strip()
                fm[key] = [line]
                order.append(key)
            elif key:
                fm[key].append(line)          # continuation of a multi-line property
    i = body.find("\n" + NOTES)
    notes = body[i + 1:] if i >= 0 else (body if body.startswith(NOTES) else NOTES + "\n")
    return fm, order, notes


def properties(data: dict, pdf: Path) -> dict:
    d = data["document"]
    params = {p["key"]: p for p in data.get("params", [])}
    props = {
        "doc_type": d.get("doc_type"),
        "part": (d.get("parts") or [None])[0],
        "parts": d.get("parts") or [],
        "manufacturer": d.get("manufacturer"),
        "title": d.get("title"),
        "revision": d.get("revision"),
        "pages": d.get("pages"),
        "pdf": f"[[{pdf.name}]]",
        "text": f"[[{pdf.stem}.text.md]]",
    }
    for key, lo_k, hi_k in (("vin", "vin_min", "vin_max"), ("vout", "vout_min", "vout_max"),
                            ("iout", None, "iout_max"), ("fsw", "fsw_min_khz", "fsw_max_khz")):
        p = params.get(key)
        if lo_k:
            props[lo_k] = p.get("min") if p else None
        props[hi_k] = p.get("max") if p else None
        props[f"confidence_{key}"] = p.get("confidence") if p else None
    rated = [p["confidence"] for p in params.values() if p.get("confidence") not in (None, "none")]
    props["confidence"] = min(rated, key=ORDER.get) if rated else "none"
    props["contested"] = sorted(k for k, p in params.items() if p.get("contested"))
    props["verified_params"] = sorted(k for k, p in params.items() if isinstance(p.get("verified"), dict))
    props["lcsc"] = (d.get("ids") or {}).get("lcsc")
    topo = d.get("topology_class") or ""
    ptype = d.get("product_type") or ""
    for t in ("converter", "charger", "ldo", "power-monitor", "protection", "load-switch",
              "mcu", "rf", "sensor"):
        props["is_" + t.replace("-", "_")] = ptype == t
    props["is_buck"] = topo in ("buck", "buck-boost")
    props["is_boost"] = topo in ("boost", "buck-boost")
    props["is_buck_boost"] = topo == "buck-boost"
    props["is_linear"] = topo == "linear"
    props["converts_voltage"] = bool(d.get("converts_voltage"))
    props["steps_down"] = bool(d.get("steps_down"))
    ti = (data.get("enrichment") or {}).get("ti_export") or {}
    props["ti_part"] = ti.get("part") or None
    props["lifecycle"] = (ti.get("values") or {}).get("lifecycle")
    rows = data.get("spec_rows", [])
    props["spec_rows"] = len(rows)
    props["spec_rows_high"] = sum(1 for r in rows if r.get("confidence") == "high")
    props["registers"] = len(data.get("registers", []))
    props["flags"] = sorted({*data.get("flags", []),
                             *(f for p in params.values() for f in p.get("flags", []))})
    props["product_type"] = ptype or None
    props["topology"] = topo or None
    props["generated"] = data.get("extraction", {}).get("generated")
    return props


def body(data: dict, pdf: Path) -> str:
    d = data["document"]
    title = d.get("title") or ""
    lines = [f"# {pdf.stem}" + (f" — {title}" if title else ""), "",
             f"**PDF:** [[{pdf.name}]] · **Full text:** [[{pdf.stem}.text.md|text]] · "
             f"**Data:** `{pdf.stem}.data.json`", ""]
    params = data.get("params", [])
    if params:
        lines += ["| Parameter | Min | Max | Unit | Confidence | Sources | Page |",
                  "|---|---|---|---|---|---|---|"]
        for p in params:
            conf = p["confidence"]
            if p.get("contested"):
                conf += " · contested"
            if isinstance(p.get("verified"), dict):
                conf += f" · verified ({p['verified'].get('by')}, {p['verified'].get('on')})"
            srcs = sorted({r["source"] for r in p.get("readings", [])})
            lines.append(f"| {p['key']} | {_n(p.get('min'))} | {_n(p.get('max'))} | "
                         f"{p.get('unit', '')} | {conf} | {', '.join(srcs)} | "
                         f"{p.get('page') or '—'} |")
        lines.append("")
    rows = data.get("spec_rows", [])
    if rows:
        by = {}
        for r in rows:
            by[r["confidence"]] = by.get(r["confidence"], 0) + 1
        summary = ", ".join(f"{by[c]} {c}" for c in ("high", "medium", "low") if c in by)
        lines += [f"{len(rows)} spec-table rows ({summary}) and "
                  f"{len(data.get('registers', []))} registers in the data file.", ""]
    lines += ["> [!note] Generated", "> Everything above **Notes** is rewritten by "
              "`cards.py`. Write below it, or add your own properties. To mark values you have "
              "checked against the PDF, add `verified: [vin, vout]` (and `verified_on`).", ""]
    return "\n".join(lines)


def _n(v):
    return "—" if v is None else f"{v:g}"


def render(card: Path, data: dict, pdf: Path) -> str:
    old = card.read_text(encoding="utf-8") if card.exists() else ""
    fm, order, notes = split_card(old)
    props = properties(data, pdf)
    out = ["---"]
    for k in OWNED:
        out.append(f"{k}: {yaml_value(props.get(k))}")
    for k in order:                                   # everything the person added, verbatim
        if k in OWNED:
            continue
        if k in LEGACY_OWNED and fm[k][0].split(":", 1)[1].strip() in LEGACY_OWNED[k]:
            continue                                  # schema-1 generated flag, not the person's
        out += fm[k]
    out.append("---")
    return "\n".join(out) + "\n\n" + body(data, pdf) + "\n" + notes.rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="AutoNotes vault (see vaultpath.py)")
    ap.add_argument("--only", default="")
    ap.add_argument("--doc", action="append", default=[], help="exact document name (repeatable)")
    args = ap.parse_args()
    root = vaultpath.root_of(vaultpath.find_vault(args.vault))
    n = 0
    for data_file in sorted(root.rglob("*.data.json")):
        stem = data_file.name[: -len(".data.json")]
        if args.only.lower() not in stem.lower() or (args.doc and stem not in args.doc):
            continue
        data = json.loads(data_file.read_text(encoding="utf-8"))
        if data["document"].get("doc_type") == "paper":
            continue
        pdf = data_file.with_name(data["document"]["file"])
        if not pdf.exists():
            pdf = data_file.with_name(stem + ".pdf")
        card = data_file.with_name(stem + ".md")
        vaultpath.write_text(card, render(card, data, pdf))
        n += 1
    print(f"{n} card(s) written")


if __name__ == "__main__":
    main()
