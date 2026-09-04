"""
classify.py — tag each twin note with what kind of product it is.

Without this, every table filters on "has a voltage range", which is why an ATtiny (supply 1.8-5.5V)
turns up beside buck-boost converters, and an RJ45 connector beside chargers. This writes a single
canonical `product_type` plus boolean `is_*` fields so a query can say what it actually means:

    WHERE is_converter AND vin_max >= 12

Signals are combined in descending order of trust, and `classified_by` records which one decided:

  1. TI export category/subcategory/function  — curated by the vendor, present on 58 notes
  2. The vault folder the PDF sits in          — curated by the user (Power/, MCUs/, Sensors/, ...)
  3. Title and description keywords            — everything else

Folder beats keywords because the folder is a human decision; TI's category beats folder because it
distinguishes a charger from a converter inside Power/, which the folder cannot.

Run last: it reads `ti_category` and `topology`, which `enrich_ti.py` adds.

Usage:
    python classify.py                # classify every twin note
    python classify.py --dry-run      # report the distribution, write nothing
"""
import argparse
import re
from collections import Counter
from pathlib import Path

DEFAULT_VAULT = Path(r"D:\Clod\AutoNotes\Reference Material")
SUFFIX = " (datasheet)"

# The canonical set. Order matters only for reporting.
TYPES = [
    "converter", "charger", "power-monitor", "protection", "ldo", "load-switch",
    "power-bank-soc", "amplifier", "mcu", "soc", "comms", "discrete", "connector",
    "rf", "solar-mppt", "sensor", "rtc", "app-note", "reference-manual", "unknown",
]

# ── signal 1: TI export fields ───────────────────────────────────────────────
TI_CATEGORY = {
    "dc/dc converters": "converter",
    "battery management ics": "charger",
    "digital power monitors": "power-monitor",
    "amplifiers": "amplifier",
}
TI_SUBCATEGORY = {
    "battery charger ics": "charger",
    "battery monitors": "power-monitor",
    "battery fuel gauges": "power-monitor",
    "battery protectors": "protection",
    "comparators": "amplifier",
    "current sense amplifiers": "power-monitor",
    "operational amplifiers": "amplifier",
    "linear regulators": "ldo",
}

# ── signal 2: the curated section index ──────────────────────────────────────
# `#Datasheets.md` already sorts ~200 datasheets into hand-written sections. That is a human
# classification of exactly this corpus, so it outranks folder and keywords — and it carries
# topology too, since the converter sections are split Buck / Boost / Buck-Boost.
#
# It is what correctly identifies XB3306D as a protection IC: its garbled extracted title reads
# "Protection of Charger Reverse", which a keyword match reads as "charger".
CURATED_INDEX = "#Datasheets.md"
CURATED_SECTIONS = {
    "buck converters":                      ("converter", "buck"),
    "boost converters":                     ("converter", "boost"),
    "buck-boost converters":                ("converter", "buck-boost"),
    "battery chargers (bq series)":         ("charger", ""),
    "battery chargers (cn / tp / other)":   ("charger", ""),
    "protection, supervisors & efuses":     ("protection", ""),
    "ldos":                                 ("ldo", "linear"),
    "mosfets":                              ("discrete", ""),
    "current sensors":                      ("power-monitor", ""),
    "real time clocks":                     ("rtc", ""),
    "mcus":                                 ("mcu", ""),
    "usb / comms":                          ("comms", ""),
    "solar / mppt":                         ("solar-mppt", ""),
    "application notes & general reference": ("app-note", ""),
}


def load_curated(vault: Path):
    """-> {pdf filename lowercased: (product_type, topology)} from the curated section index."""
    path = vault / CURATED_INDEX
    if not path.exists():
        return {}
    mapping, current = {}, None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        heading = re.match(r"^##\s+(.*?)\s*$", line)
        if heading:
            current = CURATED_SECTIONS.get(heading.group(1).strip().lower())
            continue
        if not current:
            continue
        for target in re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]", line):
            name = target.strip().lower()
            mapping[name] = current
            if name.endswith(".pdf"):
                mapping[name[:-4]] = current
    return mapping


# ── signal 3: vault folder ───────────────────────────────────────────────────
FOLDER = {
    "mcus": "mcu",
    "socs": "soc",
    "comms": "comms",
    "discretes": "discrete",
    "connectors and switches": "connector",
    "rf": "rf",
    "lora": "rf",
    "solar and mppt": "solar-mppt",
    "sensors": "sensor",
    "application notes": "app-note",
    "poe": "converter",
}

# ── signal 3: title / description keywords, first match wins ────────────────
KEYWORDS = [
    ("reference-manual", r"\bTRM\b|technical reference manual"),
    ("app-note",         r"\bapplication (?:note|report)\b|\bhow to use\b|technical brief"),
    ("power-bank-soc",   r"power bank|\bIP5[0-9]{3}\b|\bIP2[0-9]{3}\b"),
    # Protection first, and charger tightened: a bare "charger" appears in protection-IC prose too
    # (XB3306D's extracted title reads "Protection of Charger Reverse"), which mislabelled every
    # XySemi protection part as a charger.
    ("protection",       r"protection ic|battery protect\w*|\bprotector\b"),
    ("charger",          r"battery charger|charger ic|charging (?:ic|controller)|"
                         r"charge (?:management|controller)|li-?ion charg\w+|"
                         r"(?:linear|switch[- ]?mode|buck|boost)\s+charg\w+"),
    ("power-monitor",    r"power monitor|current[- ]sense|current/power monitor|shunt monitor|"
                         r"fuel gauge|coulomb count"),
    ("protection",       r"\bprotection\b|\bsupervisor\b|\beFuse\b|over[- ]?voltage protect|"
                         r"battery protect|\bOVP\b|voltage detector|\breset ic\b"),
    ("load-switch",      r"load switch|ideal diode|power mux|power path|reverse (?:current )?block"),
    ("ldo",              r"\bLDO\b|low[- ]dropout|linear regulator"),
    ("converter",        r"buck[- ]?boost|step[- ]?down|step[- ]?up|\bbuck\b|\bboost\b|"
                         r"switching regulator|dc[-/ ]?dc|power module|\bSEPIC\b|\bflyback\b"),
    ("mcu",              r"\bmicrocontroller\b|\bMCU\b|\btinyAVR\b|\bATtiny\b|\bAVR\b|\bUPDI\b"),
    ("soc",              r"\bSoC\b|application processor|vision processor|Cortex-A"),
    ("comms",            r"USB[- ]to[- ]serial|usb.{0,12}bridge|\bUART\b|\bUSB PD\b|type-c|"
                         r"\bUSB\b.{0,12}hub|\bI2C\b.{0,6}bridge|ethernet"),
    ("rf",              r"\bLoRa\b|transceiver|\bBLE\b|sub-?GHz|\bRF\b\b"),
    ("discrete",         r"\bMOSFET\b|\bN-ch\b|\bP-ch\b|\bdiode\b|\btransistor\b"),
    ("connector",        r"\bRJ45\b|\bconnector\b|magjack|\bheader\b"),
    ("amplifier",        r"\bop[- ]?amp\b|operational amplifier|\bcomparator\b|instrumentation amp"),
    ("solar-mppt",       r"\bMPPT\b|solar"),
]

# Which product types legitimately carry a Vin -> Vout conversion range.
CONVERTS = {"converter", "charger", "ldo", "power-bank-soc", "solar-mppt"}

# ── manufacturer ─────────────────────────────────────────────────────────────
# TI literature numbers: SLVS/SLUS/SBOS/SNVS/SLLS/SNAS/SPRU... all begin SL, SB, SN or SP.
_TI_DOC = re.compile(r"^(SL|SB|SN|SP)[A-Z]", re.I)
# Microchip: DS40002204A / DS90003257A
_MCHP_DOC = re.compile(r"^DS\d{5,}", re.I)

# LCSC/JLCPCB filenames embed the vendor: 2410121637_XySemi-XB3306D_C2759992
#
# The vendor field itself contains hyphens ("XI-AN-Aerosemi-Tech", "HANRUN-Zhongshan-HanRun-Elec"),
# so splitting on the first hyphen truncates it to "XI-AN" / "HANRUN". Capture the whole middle
# segment between the timestamp and the trailing _C<id>, then prefix-match it against known vendors.
_LCSC_NAME = re.compile(r"^\d{8,}_(.+?)_C\d+", re.I)

# Prefix of the middle segment -> manufacturer. "" means "not a vendor, keep looking" —
# TECH-PUBLIC is LCSC's marker for a generic public datasheet, not a company.
_LCSC_VENDORS = [
    ("tech-public", ""),
    ("xi-an-aerosemi-tech", "Aerosemi"),
    ("hanrun-zhongshan-hanrun-elec", "HanRun"),
    ("hxy-mosfet", "HXY MOSFET"),
    ("terminus-tech", "Terminus Technology"),
    ("texas-instruments", "Texas Instruments"),
    ("xysemi", "XySemi"),
    ("puolop", "PUOLOP"),
]

# Part-number prefix -> manufacturer. Longest prefix wins, so order is irrelevant.
MFR_PREFIX = {
    "TPS": "Texas Instruments", "TPSM": "Texas Instruments", "TLV": "Texas Instruments",
    "TLC": "Texas Instruments", "LM": "Texas Instruments", "LMR": "Texas Instruments",
    "BQ": "Texas Instruments", "INA": "Texas Instruments", "UCC": "Texas Instruments",
    "CSD": "Texas Instruments", "SN74": "Texas Instruments", "TB": "Microchip Technology",
    "ATTINY": "Microchip Technology", "ATMEGA": "Microchip Technology",
    "MCP": "Microchip Technology", "PIC": "Microchip Technology", "MIC": "Microchip Technology",
    "LTC": "Analog Devices", "LT": "Analog Devices", "ADP": "Analog Devices",
    "MAX": "Analog Devices", "AD": "Analog Devices",
    "CH": "WCH", "IP": "Injoinic Technology",
    "RV1": "Rockchip", "RK3": "Rockchip",
    "SX12": "Semtech", "LR11": "Semtech", "LR20": "Semtech", "LLCC": "Semtech",
    "XB": "XySemi", "TP40": "NanJing Top Power", "CN3": "Consonance",
    "MT36": "Aerosemi", "HR9": "HanRun", "HY9": "HanRun", "AG5": "Silvertel",
    "AG9": "Silvertel", "FS8": "Fortune Semiconductor", "FS3": "Fortune Semiconductor",
    "HT78": "Holtek", "XC62": "Torex", "RT92": "Richtek", "AO": "Alpha & Omega",
    "HL": "Hongjiacheng", "AP21": "Diodes Incorporated", "DW01": "PUOLOP",
    "SPV": "STMicroelectronics", "FE1": "Terminus Technology", "E73": "Ebyte",
    "IRF": "Infineon", "FM21": "Fine Made Electronics", "RX81": "Epson",
    "RV-8": "Epson", "AND9": "onsemi", "LS12": "Lightstar",
}
_MFR_TEXT = [
    ("Texas Instruments", r"texas instruments|\bti\.com\b"),
    ("Rockchip", r"rockchip"), ("WCH", r"\bWCH\b|wch-ic\.com|qinheng"),
    ("Semtech", r"semtech"), ("Microchip Technology", r"microchip|\bAtmel\b"),
    ("Analog Devices", r"analog devices|\bMaxim\b|linear technology"),
    ("Injoinic Technology", r"injoinic"), ("Holtek", r"holtek"),
    ("STMicroelectronics", r"stmicroelectronics"), ("HanRun", r"hanrun"),
    ("Silvertel", r"silvertel"), ("Consonance", r"consonance"),
    ("XySemi", r"xysemi"), ("Aerosemi", r"aerosemi"), ("Epson", r"epson"),
]


def manufacturer(note: Path, front):
    """-> (manufacturer, decided_by). Empty string when nothing is confident."""
    if read_field(front, "ti_category"):
        return "Texas Instruments", "ti-export"

    doc_id = read_field(front, "doc_id")
    if _TI_DOC.match(doc_id):
        return "Texas Instruments", "doc-id"
    if _MCHP_DOC.match(doc_id):
        return "Microchip Technology", "doc-id"

    m = _LCSC_NAME.match(note.stem)
    if m:
        middle = m.group(1).strip().lower()
        for prefix, mfr in _LCSC_VENDORS:
            if middle.startswith(prefix):
                if mfr:
                    return mfr, "lcsc-filename"
                break       # a known non-vendor marker: fall through to the part-number prefix

    part = (read_field(front, "part") or note.stem).upper().replace("_", "")
    best = ""
    for prefix, mfr in MFR_PREFIX.items():
        if part.startswith(prefix) and len(prefix) > len(best):
            best, best_mfr = prefix, mfr
    if best:
        return best_mfr, "part-prefix"

    blob = " ".join([read_field(front, "title"), read_field(front, "ti_description"), note.stem])
    for mfr, pattern in _MFR_TEXT:
        if re.search(pattern, blob, re.I):
            return mfr, "text"
    return "", "none"


def split_frontmatter(text):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5:]


def read_field(front, key):
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", front, re.M)
    if not m:
        return ""
    raw = m.group(1).strip()
    return "" if raw == "null" else raw.strip('"')


def set_field(front, key, value):
    pattern = rf"^{re.escape(key)}:.*$"
    if re.search(pattern, front, re.M):
        return re.sub(pattern, f"{key}: {value}", front, count=1, flags=re.M)
    return front.rstrip("\n") + f"\n{key}: {value}"


def _from_blob(blob):
    if re.search(r"buck[-/ ]?boost|four[- ]switch", blob, re.I):
        return "buck-boost"
    has_buck = bool(re.search(r"\bbuck\b|step[- ]?down", blob, re.I))
    has_boost = bool(re.search(r"\bboost\b|step[- ]?up", blob, re.I))
    if has_buck and has_boost:
        return "buck-boost"
    if has_buck:
        return "buck"
    if has_boost:
        return "boost"
    if re.search(r"\blinear\b|\bLDO\b|low[- ]?dropout", blob, re.I):
        return "linear"
    return ""


def topology_class(front, curated_topology=""):
    """-> (buck / boost / buck-boost / linear / '', decided_by).

    TI parts get topology free from the export's Topology column. Everything else — the Consonance
    solar chargers, Injoinic power-bank SoCs, the LDO clones — is only described in prose, so the
    cached regex reading (`text_topology`) is the fallback that covers them.
    """
    ti = _from_blob(read_field(front, "topology"))
    if ti:
        return ti, "ti-topology"

    if curated_topology:
        return curated_topology, "curated-index"

    text = read_field(front, "text_topology")
    if text:
        return text, "prose"

    blob = " ".join([read_field(front, "title"), read_field(front, "ti_description")])
    from_title = _from_blob(blob)
    if from_title:
        return from_title, "title"

    # Deliberately NOT inferring "buck" from vin_min > vout_max. A linear regulator steps down too,
    # so the range alone cannot tell a buck from an LDO — that guess would mislabel every linear
    # part in the vault. The range fact is exposed as `steps_down` instead, which claims only what
    # it knows.
    return "", "none"


def steps_down(front):
    """True when the input never reaches the output — buck *or* linear, deliberately not saying."""
    try:
        return float(read_field(front, "vin_min")) > float(read_field(front, "vout_max")) > 0
    except (TypeError, ValueError):
        return False


def classify(note: Path, front, curated=None):
    """-> (product_type, decided_by)"""
    category = read_field(front, "ti_category").lower()
    if category in TI_CATEGORY:
        # A DC/DC-category part that TI itself calls a charger is a charger.
        sub = read_field(front, "ti_subcategory").lower()
        function = read_field(front, "ti_function").lower()
        if sub in TI_SUBCATEGORY:
            return TI_SUBCATEGORY[sub], "ti-subcategory"
        if "charger" in function:
            return "charger", "ti-function"
        return TI_CATEGORY[category], "ti-category"

    # The curated section index: a human already sorted this exact corpus.
    hit = ((curated or {}).get(read_field(front, "source_pdf").lower())
           or (curated or {}).get(note.stem.replace(SUFFIX, "").lower()))
    if hit:
        return hit[0], "curated-index"

    # Folder: walk up from attachments/ to the owning topical folder.
    parts = [p.lower() for p in note.parts]
    for name in reversed(parts):
        if name in FOLDER:
            return FOLDER[name], "folder"

    blob = " ".join([read_field(front, "part"), read_field(front, "title"),
                     read_field(front, "ti_description"), note.stem])
    for product_type, pattern in KEYWORDS:
        if re.search(pattern, blob, re.I):
            return product_type, "keyword"

    if read_field(front, "parse_error"):
        return "reference-manual", "parse-error"
    return "unknown", "none"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    curated = load_curated(args.vault)
    print(f"curated index: {len(curated)} datasheet name(s) already sorted into sections")
    counts, by_signal, examples = Counter(), Counter(), {}
    mfrs, mfr_signal = Counter(), Counter()
    for note in sorted(args.vault.rglob(f"*{SUFFIX}.md")):
        text = note.read_text(encoding="utf-8")
        front, body = split_frontmatter(text)
        if front is None:
            continue

        product_type, decided_by = classify(note, front, curated)
        curated_hit = (curated.get(read_field(front, "source_pdf").lower())
                       or curated.get(note.stem.replace(SUFFIX, "").lower()) or ("", ""))
        counts[product_type] += 1
        by_signal[decided_by] += 1
        examples.setdefault(product_type, []).append(note.stem.replace(SUFFIX, ""))

        mfr, mfr_by = manufacturer(note, front)
        mfrs[mfr or "«unknown»"] += 1
        mfr_signal[mfr_by] += 1

        topo, topo_by = topology_class(front, curated_hit[1])
        front = set_field(front, "product_type", f'"{product_type}"')
        front = set_field(front, "classified_by", f'"{decided_by}"')
        front = set_field(front, "manufacturer", f'"{mfr}"' if mfr else "null")
        front = set_field(front, "manufacturer_by", f'"{mfr_by}"')
        front = set_field(front, "topology_class", f'"{topo}"' if topo else "null")
        front = set_field(front, "topology_by", f'"{topo_by}"')

        # Topology booleans. A buck-boost genuinely *is* both, so is_buck and is_boost are both
        # true for it — "can this part step my rail down?" must catch buck-boosts. is_buck_boost
        # picks out the ones that do both. Linear is a topology in its own right, not the absence
        # of one, so it gets its own flag rather than falling through to "unknown".
        front = set_field(front, "is_buck",
                          "true" if topo in ("buck", "buck-boost") else "false")
        front = set_field(front, "is_boost",
                          "true" if topo in ("boost", "buck-boost") else "false")
        front = set_field(front, "is_buck_boost", "true" if topo == "buck-boost" else "false")
        front = set_field(front, "is_linear", "true" if topo == "linear" else "false")
        front = set_field(front, "is_switching",
                          "true" if topo in ("buck", "boost", "buck-boost", "flyback",
                                             "sepic", "inverting") else "false")
        front = set_field(front, "steps_down", "true" if steps_down(front) else "false")

        # Booleans, so a Dataview WHERE clause reads like a sentence.
        for t in TYPES:
            if t == "unknown":
                continue
            key = "is_" + t.replace("-", "_")
            front = set_field(front, key, "true" if product_type == t else "false")
        # Umbrella: does a Vin -> Vout range mean anything for this part?
        front = set_field(front, "converts_voltage",
                          "true" if product_type in CONVERTS else "false")

        if not args.dry_run:
            note.write_text("---\n" + front.rstrip("\n") + "\n---\n" + body, encoding="utf-8")

    print(f"{sum(counts.values())} note(s) classified"
          + (" (dry run — nothing written)" if args.dry_run else ""))
    print("\nBy type:")
    for t, n in counts.most_common():
        sample = ", ".join(examples[t][:3])
        print(f"  {n:>4}  {t:<18} e.g. {sample[:64]}")
    print("\nDecided by: " + ", ".join(f"{k}={v}" for k, v in by_signal.most_common()))
    print("\nBy manufacturer:")
    for mfr, n in mfrs.most_common():
        print(f"  {n:>4}  {mfr}")
    print("Manufacturer from: " + ", ".join(f"{k}={v}" for k, v in mfr_signal.most_common()))
    if counts.get("unknown"):
        print(f"\nUnclassified ({counts['unknown']}): {', '.join(examples['unknown'][:12])}")


if __name__ == "__main__":
    main()
