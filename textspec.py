"""
textspec.py — third parameter source: the claims a datasheet makes in prose.

`device_info.json` scrapes page-1 fields and `elec_chars.json` reads the Recommended Operating
Conditions table. Both miss the most human-legible statement of a part's ratings — the sentences:

    "TPS55287 36-V, 4-A Buck-Boost Converter with I2C Interface"
    "The TPS55287 has up to 36V input voltage capability."
    "can be programmed from 0.8V to 22V with 10mV step"
    "3.0V to 36V wide input voltage range"

This module extracts those as explicit *claims*, each keeping the sentence it came from so a
disagreement can be adjudicated by reading rather than guessing. On TPS55287 — where the page-1
scrape put Vin into Vout and TI's own selector said 30V — the title and body both say 36V, which
is the correct answer.

## Why the two-column split matters

TI datasheets put Features in a left column and Description in a right column. `pdftotext -layout`
preserves that visually, so a single output line holds *both* columns:

    "� 3.0V to 36V wide input voltage range      can deliver 35W from a 12V input. It is capable of"

Running a regex over that line can marry a number from one column to a unit from the other — which
is precisely how the existing page-1 extractor produces "Vout 3-36V" for a 0.8-22V part. So the text
is split into column streams on runs of 3+ spaces *before* any pattern is applied, and no pattern is
ever allowed to span a column boundary.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import vaultpath

# A number with an optional hyphen before its unit: "36-V", "36V", "36 V", "2.2-MHz"
_N = r"(\d+(?:\.\d+)?)"
_V = r"\s*-?\s*V(?:olts?)?\b"
_A = r"\s*-?\s*A\b"


def _rx(pattern):
    return re.compile(pattern, re.IGNORECASE)


# Each: (claim keys produced, compiled pattern). Keys map onto fuse.py's vocabulary.
PATTERNS = [
    # "TPS55287 36-V, 4-A Buck-Boost Converter" — the title line, strongest single statement
    (("vin_max", "iout_max"),
     _rx(rf"{_N}{_V}\s*,\s*{_N}{_A}[^.]{{0,40}}(?:converter|regulator|buck|boost)")),

    # "has up to 36V input voltage capability" / "up to 36-V input voltage"
    (("vin_max",), _rx(rf"up to\s+{_N}{_V}\s+input voltage")),

    # "3.0V to 36V wide input voltage range" / "4.5-V to 28-V wide input voltage range"
    (("vin_min", "vin_max"),
     _rx(rf"{_N}{_V}\s+to\s+{_N}{_V}\s+(?:wide\s+)?input[- ]voltage")),

    # "Wide input voltage range: 2.4V to 22V" / "input voltage range of 1.6V to 5.5V"
    (("vin_min", "vin_max"),
     _rx(rf"input voltage range\s*(?:of|:)?\s*{_N}{_V}\s+to\s+{_N}{_V}")),

    # "1.6V to 5.5V input voltage range" (range before the label)
    (("vin_min", "vin_max"),
     _rx(rf"{_N}{_V}\s+to\s+{_N}{_V}\s+input voltage range")),

    # "programmed from 0.8V to 22V" / "outputting 0.8V to 22V voltage"
    (("vout_min", "vout_max"),
     _rx(rf"(?:programm(?:ed|able)|output(?:ting)?|regulat\w+)\s+(?:from\s+)?{_N}{_V}\s+to\s+{_N}{_V}")),

    # "Programmable output voltage range: 0.8V to 15V" / "output voltage range 1.0V to 5.5V"
    (("vout_min", "vout_max"),
     _rx(rf"output voltage range\s*(?:of|:)?\s*\(?\w*\)?\s*{_N}{_V}\s+to\s+{_N}{_V}")),

    # "0.8V to 22V with 10mV step programmable output voltage range"
    (("vout_min", "vout_max"),
     _rx(rf"{_N}{_V}\s+to\s+{_N}{_V}[^.]{{0,30}}output voltage")),

    # "with 10mV step" / "in 20-mV steps"
    (("vout_step_mv",), _rx(rf"{_N}\s*-?\s*mV\s+steps?")),

    # "2-A, continuous output current" / "8A output current"
    (("iout_max",), _rx(rf"{_N}{_A}[, ]{{0,3}}(?:continuous\s+)?output current")),

    # "switching frequency ... 200kHz to 2.2MHz"
    (("fsw_min_khz", "fsw_max_khz"),
     _rx(rf"{_N}\s*-?\s*kHz\s+to\s+{_N}\s*-?\s*MHz")),

    # "Maximum switching frequency up to 2.2MHz" / "Fixed 500-kHz switching frequency"
    (("fsw_max_khz",), _rx(rf"(?:up to\s+)?{_N}\s*-?\s*MHz\s+switching")),
    (("fsw_fixed_khz",), _rx(rf"fixed\s+{_N}\s*-?\s*kHz\s+switching")),

    # "can deliver 35W from a 12V input"
    (("boost_power_w", "boost_from_v"),
     _rx(rf"deliver(?:ing)?\s+{_N}\s*-?\s*W\s+from\s+(?:a\s+)?{_N}{_V}")),

    # "16-Bit" / "12-bit differential ADC"
    (("resolution_bits",), _rx(rf"{_N}\s*-?\s*bit\b")),

    # "45-A quiescent current" needs the micro sign; keep uA/µA explicit
    (("iq_typ_ua",), _rx(rf"{_N}\s*-?\s*[uµμ]A\s+quiescent")),
]

# MHz-valued keys need scaling to kHz to match fuse.py's units.
_SCALE = {"fsw_max_khz": 1e3, "fsw_min_khz": 1.0, "fsw_fixed_khz": 1.0}

# Lines that look like specs but describe the wrong thing.
_LINE_EXCLUDE = _rx(r"abs(olute)?\s*max|storage temp|junction temp|ESD|"
                    r"\border\b|package (?:code|drawing)|see section")

# ── topology, from the prose ──────────────────────────────────────────────────
# TI parts get this free from the parametric export's Topology column. Everything else — the
# Consonance CN3xxx solar chargers, Injoinic power-bank SoCs, Silvertel PoE bricks, SPV1040,
# the LDO clones — has no export, so the only place it is stated is the text.
TOPOLOGY_TERMS = [
    ("buck-boost", r"buck[-\s]?boost|step[-\s]?up/?\s?down|step[-\s]?down/?\s?up|"
                   r"four[-\s]switch|4[-\s]switch"),
    ("buck",       r"\bbuck\b|step[-\s]?down|synchronous step down"),
    ("boost",      r"\bboost\b|step[-\s]?up"),
    ("linear",     r"\bLDO\b|low[-\s]?dropout|linear (?:regulator|charger|mode)|"
                   r"linear.{0,12}charg"),
    ("flyback",    r"\bflyback\b|isolated (?:converter|dc[-/ ]?dc)"),
    ("sepic",      r"\bSEPIC\b"),
    ("charge-pump", r"charge[-\s]?pump|switched[-\s]?capacitor"),
    ("inverting",  r"\binverting\b.{0,20}(?:converter|regulator)"),
]

# A term only counts if it describes this device, not a comparison or an application list.
_TOPO_EXCLUDE = _rx(r"\bcompar\w+ to\b|\bversus\b|\bvs\.?\b|instead of|"
                    r"unlike|competitor|other (?:solutions|devices)")


def topology(fragments):
    """-> (derived topology or '', [terms found]).

    'buck' and 'boost' both appearing is read as buck-boost, since a datasheet that says the part
    steps both up and down is describing one converter, not two.
    """
    found = []
    for fragment in fragments:
        if _TOPO_EXCLUDE.search(fragment):
            continue
        for name, pattern in TOPOLOGY_TERMS:
            if name not in found and re.search(pattern, fragment, re.I):
                found.append(name)
    if "buck-boost" in found or ("buck" in found and "boost" in found):
        derived = "buck-boost"
    elif "buck" in found:
        derived = "buck"
    elif "boost" in found:
        derived = "boost"
    elif "linear" in found:
        derived = "linear"
    elif found:
        derived = found[0]
    else:
        derived = ""
    return derived, found


def columns(text, min_gap=3):
    """Split layout-preserved text into column streams so no pattern spans two columns.

    Returns a list of strings, one per detected column position. A line with no wide gap
    contributes to column 0, which is also where single-column pages end up.
    """
    streams = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        # Segment on runs of >= min_gap spaces, keeping each segment's start offset as its x-position.
        segments, pos = [], 0
        for chunk in re.split(rf"(\s{{{min_gap},}})", line):
            if chunk and not chunk.isspace():
                segments.append((pos, chunk.strip()))
            pos += len(chunk)
        if not segments:
            continue
        if len(segments) == 1:
            streams.setdefault(0, []).append(segments[0][1])
            continue
        for x, chunk in segments:
            # Bucket by rough x-position so a column stays a column down the page.
            key = round(x / 20)
            streams.setdefault(key, []).append(chunk)
    return [" ".join(lines) for _, lines in sorted(streams.items())]


def extract(pdf: Path, pages=2):
    """-> {key: {"value": float, "evidence": str, "pattern": int}} plus a "_claims" list.

    Where several patterns agree on a key the first (most authoritative) wins, but every hit is
    recorded in "_claims" so a conflict is visible rather than resolved silently.
    """
    try:
        raw = subprocess.run(
            ["pdftotext", "-f", "1", "-l", str(pages), "-layout", str(pdf), "-"],
            capture_output=True, timeout=60,
        ).stdout.decode("utf-8", "replace")
    except Exception as exc:                                   # noqa: BLE001
        if not vaultpath.have_tool("pdftotext"):
            vaultpath.require_tool("pdftotext", "reading datasheet prose")
        return {"_error": f"pdftotext failed: {exc}", "_claims": []}

    claims, best, all_fragments = [], {}, []
    for stream in columns(raw):
        # Work sentence-ish: split on sentence ends and bullet markers to bound each match.
        #
        # TI's bullet is a Symbol-font glyph that pdftotext emits as bytes which are not valid
        # UTF-8, so it decodes to U+FFFD. Without splitting on it, one "fragment" runs across
        # several Features bullets and a Vout pattern happily marries itself to the Vin bullet's
        # numbers — which is exactly how "Vout 3-36V" appears for a 0.8-22V part.
        for fragment in re.split(r"(?<=[.;])\s+|\s*[•▪�▪·]\s*", stream):
            fragment = " ".join(fragment.split())
            if len(fragment) < 8 or _LINE_EXCLUDE.search(fragment):
                continue
            all_fragments.append(fragment)
            for idx, (keys, pattern) in enumerate(PATTERNS):
                m = pattern.search(fragment)
                if not m:
                    continue
                groups = [g for g in m.groups() if g is not None]
                if len(groups) < len(keys):
                    continue
                for key, raw_value in zip(keys, groups):
                    try:
                        value = float(raw_value) * _SCALE.get(key, 1.0)
                    except ValueError:
                        continue
                    claims.append({"key": key, "value": value, "pattern": idx,
                                   "evidence": fragment[:180]})
                    if key not in best:
                        best[key] = {"value": value, "evidence": fragment[:180], "pattern": idx}

    derived, terms = topology(all_fragments)
    best["_claims"] = claims
    best["_topology"] = derived
    best["_topology_terms"] = terms
    best["_has_register_map"] = has_register_map(pdf)
    return best


# Older TI datasheets (INA209/219/220/226/230/231) summarise their registers in ONE "Register Set
# Summary" table — pointer address, name, function, reset value — and describe the bit fields in
# prose. `extractor/i2c_registers.py` looks for per-register bit-field tables (BITS / FIELD /
# ACCESS / RESET), which those documents do not contain, so it returns zero.
#
# Zero is then indistinguishable from "this part has no registers", which for an I2C power monitor
# is plainly wrong. This flags the difference so a twin note can say "not extracted" instead.
_REGISTER_HEADING = re.compile(
    r"register (?:set summary|maps?|descriptions?|summary)|"
    r"^\s*Table [\d-]+\.\s*Register|internal registers",
    re.I | re.M)


def has_register_map(pdf: Path):
    """True when the document advertises a register map anywhere, regardless of its table format."""
    try:
        raw = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                             capture_output=True, timeout=120).stdout.decode("utf-8", "replace")
    except Exception:                                          # noqa: BLE001
        return False
    return bool(_REGISTER_HEADING.search(raw))


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python textspec.py <pdf> [more.pdf ...]")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for arg in sys.argv[1:]:
        pdf = Path(arg)
        result = extract(pdf)
        print(f"\n=== {pdf.name} ===")
        if "_error" in result:
            print("  " + result["_error"])
            continue
        for key, hit in result.items():
            if key.startswith("_"):
                continue
            print(f"  {key:16} {hit['value']:>10g}   <- {hit['evidence'][:88]}")
        extra = len(result["_claims"]) - len([k for k in result if not k.startswith("_")])
        if extra > 0:
            print(f"  ({extra} additional corroborating/conflicting claim(s))")


if __name__ == "__main__":
    main()
