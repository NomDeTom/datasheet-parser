"""
Extracts high-level device information from TI-style PDF datasheets.

Sources used:
  - Page 1 line 1        → part number
  - Page 1 line 2        → document ID (e.g. "SLUSDV2B")
  - Page 1 line 3+       → device title
  - Page 1 table 0       → package variants (type, pins, body size)
  - Page 1 features text → targeted parametric specs (VIN, VOUT, IMAX, freq …)
  - Pages 1–3 text       → interface type, temperature range (if not in ROC)
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class PackageVariant:
    part_number: str    # e.g. "BQ25798"
    package_code: str   # full code as printed, e.g. "QFN (29)"
    package_type: str   # normalised type, e.g. "QFN"
    pins: Optional[int] # pin count, None if not parseable
    body_size: str      # normalised, e.g. "4.0mm × 4.0mm"


@dataclass
class DeviceInfo:
    # ── Identity ──────────────────────────────────────────────────────────────
    part_number: str    # primary part number, e.g. "BQ25798"
    document_id: str    # TI doc code, e.g. "SLUSDV2B"
    title: str          # full device description line

    # ── Package ───────────────────────────────────────────────────────────────
    packages: list[PackageVariant] = field(default_factory=list)

    # ── Parametric specs (empty string = not found) ───────────────────────────
    vin_min: str = ""           # Minimum input voltage,  e.g. "3.6V"
    vin_max: str = ""           # Maximum input voltage,  e.g. "24V"
    vin_startup: str = ""       # Min voltage for device startup (UVLO rising), e.g. "3.0V"
    vout_min: str = ""          # Min programmable output voltage, e.g. "0.8V"
    vout_max: str = ""          # Max programmable output voltage, e.g. "15V"
    vsupply_min: str = ""       # Supply voltage min (monitors/ADCs), e.g. "2.7V"
    vsupply_max: str = ""       # Supply voltage max, e.g. "5.5V"
    iout_max: str = ""          # Max output or charge current, e.g. "5A"
    freq_min: str = ""          # Min switching frequency, e.g. "400kHz"
    freq_max: str = ""          # Max switching frequency, e.g. "2.1MHz"
    temp_min: str = ""          # Min operating temperature, e.g. "–40°C"
    temp_max: str = ""          # Max operating temperature, e.g. "125°C"
    interface: list[str] = field(default_factory=list)   # e.g. ["I2C", "SMBus"]

    # ── Content ───────────────────────────────────────────────────────────────
    features: list[str] = field(default_factory=list)
    applications: list[str] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────

# Normalise TI's "3.6-V" → "3.6V", "1.5-MHz" → "1.5MHz"
def _norm_val(s: str) -> str:
    return re.sub(r"(\d)\s*[-–]\s*([a-zA-ZµΩ°])", r"\1\2", s.strip())


def _norm_size(s: str) -> str:
    """Normalise package size strings to "Wmm × Hmm [× depth]" form."""
    s = s.strip()
    # Join multi-line cell text (e.g. "2.6 mm x 2.0 mm x\n1.2 mm\n(max height)")
    s = " ".join(s.splitlines())
    s = re.sub(r"\([^)]*\)", "", s).strip()               # strip "(max height)" etc.
    s = re.sub(r"(?<!\w)[xX×](?!\w)", " × ", s)          # unify × separators (not "max")
    s = re.sub(r"(\d)\s*mm", r"\1mm", s)                  # no space before mm
    s = re.sub(r"\s{2,}", " ", s).strip()                 # collapse extra whitespace
    return s


_PIN_IN_CODE = re.compile(r"\(.*?(\d{1,3}).*?\)")   # "(29)" or "(VQFN, 16)"
_SIZE_RE = re.compile(r"(\d+\.?\d*)\s*mm\s*[×xX]\s*(\d+\.?\d*)\s*mm", re.IGNORECASE)


def _parse_package(code: str, size_raw: str) -> tuple[str, Optional[int], str]:
    """
    Parse package code and size string.

    Returns (package_type, pins, body_size).
    Examples:
      "QFN (29)", "4.0 mm x 4.0 mm"  → ("QFN", 29, "4.0mm × 4.0mm")
      "RGV (VQFN, 16)", "4.00mm × 4.00mm" → ("VQFN", 16, "4.00mm × 4.00mm")
      "WQFN-HR", "2.5mm × 3.5mm"          → ("WQFN-HR", None, "2.5mm × 3.5mm")
    """
    # Extract pin count from parenthetical
    pins: Optional[int] = None
    m = _PIN_IN_CODE.search(code)
    if m:
        # Look for a bare number inside the parens
        nums = re.findall(r"\d+", m.group(0))
        # Prefer the largest number that is plausibly a pin count (2-4 digits)
        for n in nums:
            v = int(n)
            if 2 <= v <= 512:
                pins = v
                break

    # Derive package type: text before the first "(" or the whole code
    ptype = re.split(r"\s*\(", code)[0].strip()
    # For "RGV (VQFN, 16)" prefer the type inside the parens
    inner = re.search(r"\(([A-Za-z]+)[,\s]", code)
    if inner:
        ptype = inner.group(1)

    return ptype, pins, _norm_size(size_raw)


def _is_pkg_table(headers: list) -> bool:
    h = " ".join(str(x or "").upper() for x in headers)
    return "PART" in h and "PACKAGE" in h


# ── Feature / description text parsing ────────────────────────────────────────

# Voltage token: "3.6-V", "3.6V", "3.6 V"
# The negative lookbehind prevents matching a digit mid-number (e.g. "2.7V" → captures
# "2.7", not "7" when the preceding [^\n]{…} consumes the "2.").
_V = r"(?<![.\d])(\d+\.?\d*)\s*[-–]?\s*V"

# Frequency token: "750-kHz", "1.5-MHz", "400kHz", "2.1MHz"
_F = r"(\d+\.?\d*)\s*[-–]?\s*([kKmM]Hz)"

# Current token: "5-A", "5 A", "5A"
_A = r"(\d+\.?\d*)\s*[-–]?\s*A"

_SPEC_RES: list[tuple[str, re.Pattern]] = [
    # Startup / UVLO voltage — very specific phrase
    ("vin_startup",
     re.compile(
         rf"{_V}(?:\s+rising)?[^\n]{{0,20}}[Mm]inimum\s+input\s+voltage\s+for\s+start",
         re.IGNORECASE)),
    ("vin_startup",   # alternate: "X-V minimum input … start-up"
     re.compile(
         rf"{_V}\s+[Mm]inimum\s+input",
         re.IGNORECASE)),

    # Input voltage range — "X-V to Y-V … input" or "input/VIN … X-V to Y-V"
    # Pattern 1: range first, then the word "input" somewhere after it.
    # Deliberately excludes bare "VIN" labels (circuit diagrams) by requiring the
    # English word "input", which never appears in schematic node names.
    ("vin_range",
     re.compile(
         rf"{_V}\s+to\s+{_V}[^\n]{{0,40}}?input",
         re.IGNORECASE)),
    ("vin_range",
     re.compile(
         rf"(?:input|VIN)[^\n]{{0,40}}?{_V}\s+to\s+{_V}",
         re.IGNORECASE)),

    # Output voltage range — "X to Y … output/VOUT"
    ("vout_range",
     re.compile(
         rf"{_V}\s+to\s+{_V}[^\n]{{0,40}}?(?:output|VOUT)",
         re.IGNORECASE)),
    ("vout_range",
     re.compile(
         rf"(?:[Oo]utput|VOUT)[^\n]{{0,60}}?{_V}\s+to\s+{_V}",
         re.IGNORECASE)),

    # Power supply / supply voltage (for monitors)
    ("vsupply_range",
     re.compile(
         rf"(?:power[- ]supply|supply\s+voltage|V\s*S\b)[^\n]{{0,20}}{_V}\s+to\s+{_V}",
         re.IGNORECASE)),
    ("vsupply_range",   # "powered from a … X to Y supply"
     re.compile(
         rf"powered\s+from[^\n]{{0,30}}{_V}\s+to\s+{_V}",
         re.IGNORECASE)),

    # Max output / charge current
    ("iout_max",
     re.compile(
         rf"({_A}(?:\s+output|\s+charg|\s+fast|\s+peak|\s+average\s+inductor)[^\n]{{0,30}})",
         re.IGNORECASE)),

    # Frequency range  "X-kHz … to … Y-MHz"
    ("freq_range",
     re.compile(
         rf"{_F}\s+(?:or|to|-)\s+{_F}",
         re.IGNORECASE)),
    ("freq_range",   # single max frequency  "up to X MHz"
     re.compile(
         rf"(?:up\s+to|maximum)[^\n]{{0,20}}{_F}",
         re.IGNORECASE)),
    ("freq_range",   # single value "Y-MHz switch mode"
     re.compile(
         rf"{_F}\s+(?:switch|switching|frequency)",
         re.IGNORECASE)),

    # Operating temperature range
    ("temp_range",
     re.compile(
         r"([-–−]?\d+)\s*°C\s+to\s+\+?(\d+)\s*°C",
         re.IGNORECASE)),
]

_INTERFACE_RE = re.compile(r"\b(I2C|SPI|SMBus|SMBUS|UART|PMBus)\b", re.IGNORECASE)


def _extract_spec(text: str, name: str, pattern: re.Pattern) -> dict:
    """Run one pattern and return a partial spec dict or {}."""
    m = pattern.search(text)
    if not m:
        return {}

    g = [_norm_val(x) for x in m.groups() if x is not None]

    if name == "vin_startup":
        return {"vin_startup": g[0] + "V"} if g else {}

    if name in ("vin_range", "vout_range", "vsupply_range"):
        if len(g) >= 2:
            key = name.replace("_range", "")
            return {f"{key}_min": g[0] + "V", f"{key}_max": g[1] + "V"}

    if name == "iout_max":
        # Pull first bare number + A from the matched text
        cm = re.search(r"(\d+\.?\d*)\s*[-–]?\s*A", m.group(0))
        if cm:
            return {"iout_max": _norm_val(cm.group(0)).replace(" ", "")}

    if name == "freq_range":
        # May have 2 frequency tokens (range) or 1 (single max)
        freqs = re.findall(r"(\d+\.?\d*)\s*[-–]?\s*([kKmM]Hz)", m.group(0), re.IGNORECASE)
        if freqs:
            def _fmt(v, u):
                return _norm_val(v) + u.replace("k", "k").replace("K", "k")
            if len(freqs) >= 2:
                return {"freq_min": _fmt(*freqs[0]), "freq_max": _fmt(*freqs[-1])}
            return {"freq_max": _fmt(*freqs[0])}

    if name == "temp_range":
        lo, hi = g[0], g[1]
        # ensure proper sign on negative
        if not lo.startswith("–") and not lo.startswith("-"):
            lo = "–" + lo
        return {"temp_min": lo + "°C", "temp_max": "+" + hi + "°C"}

    return {}


def _parse_bullets(text: str, start_marker: str, stop_marker: str) -> list[str]:
    """
    Extract bullet points (marked with •) between start_marker and stop_marker.
    Returns cleaned strings, one per bullet.
    """
    m_start = re.search(re.escape(start_marker), text, re.IGNORECASE)
    m_stop  = re.search(re.escape(stop_marker),  text, re.IGNORECASE)
    if not m_start:
        return []
    section = text[m_start.end(): m_stop.start() if m_stop else m_start.end() + 3000]

    bullets = []
    for raw in section.split("•"):
        clean = re.sub(r"\s+", " ", raw).strip()
        if len(clean) > 8:   # skip noise
            bullets.append(clean)
    return bullets


# ── Extractor ──────────────────────────────────────────────────────────────────

class DeviceInfoExtractor:

    def __init__(self, pdf_path: Path, debug: bool = False):
        self.pdf_path = Path(pdf_path)
        self.debug = debug

    def extract(self) -> DeviceInfo:
        with pdfplumber.open(self.pdf_path) as pdf:
            # Collect text from pages 1–3 (enough for all key info)
            pages_text = []
            for i in range(min(3, len(pdf.pages))):
                pages_text.append(pdf.pages[i].extract_text() or "")

            p1_text  = pages_text[0]
            all_text = "\n".join(pages_text)

            # ── Identity ──────────────────────────────────────────────────────
            lines = [ln.strip() for ln in p1_text.splitlines() if ln.strip()]
            part_number = lines[0] if lines else ""
            doc_id      = ""
            title       = ""

            first_line = lines[0] if lines else ""
            if len(first_line) > 30:
                # Some datasheets put the full title on line 1 (e.g. TPSM83102).
                # Treat line 0 as the title; extract part number from leading word.
                pn_m = re.match(r"^([A-Za-z0-9\-]+)", first_line)
                part_number = pn_m.group(1) if pn_m else first_line.split()[0]
                title = first_line
                doc_id_line = lines[1] if len(lines) > 1 else ""
            else:
                # Normal format: line 0 = part number, line 1 = doc ID + date
                part_number = first_line
                doc_id_line = lines[1] if len(lines) > 1 else ""
                # Title: first line (among 2–8) that starts with the part number
                for ln in lines[2:8]:
                    if ln.upper().startswith(part_number.upper()) and len(ln) > len(part_number) + 10:
                        title = ln
                        break

            # Doc ID (SLVSCE0, SLUSDV2B, …): first all-caps token on the doc-ID line
            if doc_id_line:
                doc_m = re.search(r"\b([A-Z]{4,}[0-9A-Z]*)\b", doc_id_line)
                if doc_m:
                    doc_id = doc_m.group(1)

            # ── Package table ─────────────────────────────────────────────────
            packages: list[PackageVariant] = []
            p1_tables = pdf.pages[0].extract_tables()
            for tbl in p1_tables:
                if not tbl or not _is_pkg_table(tbl[0]):
                    continue
                headers = [str(h or "").upper() for h in tbl[0]]
                # Locate column indices
                def col(kw):
                    for j, h in enumerate(headers):
                        if kw in h:
                            return j
                    return None

                ci_pn   = col("PART")
                ci_pkg  = col("PACKAGE")
                ci_size = col("BODY") or col("SIZE")

                for row in tbl[1:]:
                    cells = [str(c or "").strip() for c in row]
                    if not any(cells):
                        continue
                    pn_raw   = cells[ci_pn]   if ci_pn   is not None and ci_pn   < len(cells) else ""
                    pkg_raw  = cells[ci_pkg]  if ci_pkg  is not None and ci_pkg  < len(cells) else ""
                    size_raw = cells[ci_size] if ci_size is not None and ci_size < len(cells) else ""

                    if not pn_raw or not pkg_raw:
                        continue
                    # Skip footnote rows like "(1) For all available packages…"
                    if pn_raw.startswith("("):
                        continue

                    ptype, pins, bsize = _parse_package(pkg_raw, size_raw)
                    packages.append(PackageVariant(
                        part_number=pn_raw,
                        package_code=pkg_raw,
                        package_type=ptype,
                        pins=pins,
                        body_size=bsize,
                    ))
                break  # only first matching table

            # ── Features and Applications ─────────────────────────────────────
            features     = _parse_bullets(p1_text, "1 Features", "3 Description")
            if not features:
                # Two-column PDFs (e.g. INA3221) merge "1 Features 3 Description" on
                # one line so the section window collapses to nothing.  Fall back to
                # collecting every •-delimited bullet from the whole first page.
                raw_bullets = [re.sub(r"\s+", " ", b).strip() for b in p1_text.split("•")]
                features = [b for b in raw_bullets if len(b) > 8][1:]  # [0] is pre-bullet text

            applications = _parse_bullets(p1_text, "2 Applications", "3 Description")
            # Fallback: some sheets have "2 Applications" before features section ends
            if not applications:
                applications = _parse_bullets(p1_text, "Applications", "Description")

            # ── Interface detection ───────────────────────────────────────────
            interface = sorted(set(
                m.upper() for m in _INTERFACE_RE.findall(all_text[:3000])
            ))

            # ── Targeted parametric specs ─────────────────────────────────────
            # Build a search corpus from title + features text + description
            corpus = title + "\n" + "\n".join(features) + "\n" + all_text[:4000]

            spec_vals: dict[str, str] = {}
            for spec_name, pattern in _SPEC_RES:
                if spec_name.replace("_range", "_min") in spec_vals:
                    continue   # already found this spec
                if spec_name in spec_vals:
                    continue
                result = _extract_spec(corpus, spec_name, pattern)
                # Merge, but don't overwrite already-found values
                for k, v in result.items():
                    if v and k not in spec_vals:
                        spec_vals[k] = v

            if self.debug:
                print(f"  part={part_number!r} doc={doc_id!r}")
                print(f"  packages: {packages}")
                print(f"  spec_vals: {spec_vals}")

            return DeviceInfo(
                part_number=part_number,
                document_id=doc_id,
                title=title,
                packages=packages,
                features=features,
                applications=applications,
                interface=interface,
                vin_min=spec_vals.get("vin_min", ""),
                vin_max=spec_vals.get("vin_max", ""),
                vin_startup=spec_vals.get("vin_startup", ""),
                vout_min=spec_vals.get("vout_min", ""),
                vout_max=spec_vals.get("vout_max", ""),
                vsupply_min=spec_vals.get("vsupply_min", ""),
                vsupply_max=spec_vals.get("vsupply_max", ""),
                iout_max=spec_vals.get("iout_max", ""),
                freq_min=spec_vals.get("freq_min", ""),
                freq_max=spec_vals.get("freq_max", ""),
                temp_min=spec_vals.get("temp_min", ""),
                temp_max=spec_vals.get("temp_max", ""),
            )
