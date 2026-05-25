"""Write extracted register and electrical characteristics data to JSON or CSV."""
import csv
import json
from dataclasses import asdict
from pathlib import Path

from extractor.i2c_registers import Register
from extractor.elec_chars import ElecSection


# ── Registers ─────────────────────────────────────────────────────────────────

def write_registers(registers: list[Register], dest: Path, fmt: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        _write_json(registers, dest / "registers.json")
    elif fmt == "csv":
        _write_registers_csv(registers, dest / "registers.csv")


def _write_json(data, path: Path) -> None:
    path.write_text(json.dumps([asdict(r) for r in data], indent=2), encoding="utf-8")


def _write_registers_csv(registers: list[Register], path: Path) -> None:
    rows = []
    for reg in registers:
        if reg.fields:
            for bf in reg.fields:
                rows.append({
                    "register_name": reg.name,
                    "address": reg.address,
                    "reset": reg.reset,
                    "register_description": reg.description,
                    "bits": bf.bits,
                    "field_name": bf.name,
                    "access": bf.access,
                    "field_reset": bf.reset,
                    "description": bf.description,
                    "source_page": reg.source_page,
                })
        else:
            rows.append({
                "register_name": reg.name,
                "address": reg.address,
                "reset": reg.reset,
                "register_description": reg.description,
                "bits": "", "field_name": "", "access": "",
                "field_reset": "", "description": "",
                "source_page": reg.source_page,
            })
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ── Electrical Characteristics ────────────────────────────────────────────────

def write_elec_chars(sections: list[ElecSection], dest: Path, fmt: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        _write_elec_json(sections, dest / "elec_chars.json")
    elif fmt == "csv":
        _write_elec_csv(sections, dest / "elec_chars.csv")


def _write_elec_json(sections: list[ElecSection], path: Path) -> None:
    path.write_text(json.dumps([asdict(s) for s in sections], indent=2), encoding="utf-8")


def _write_elec_csv(sections: list[ElecSection], path: Path) -> None:
    rows = []
    for sec in sections:
        for spec in sec.specs:
            rows.append({
                "section": sec.name,
                "global_conditions": sec.conditions,
                "group": spec.group,
                "symbol": spec.symbol,
                "parameter": spec.parameter,
                "conditions": spec.conditions,
                "min": spec.min,
                "typ": spec.typ,
                "max": spec.max,
                "unit": spec.unit,
                "source_page": spec.source_page,
            })
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ── Legacy shim (keep old call sites working) ─────────────────────────────────

def write_output(registers: list[Register], dest: Path, fmt: str) -> None:
    write_registers(registers, dest, fmt)
