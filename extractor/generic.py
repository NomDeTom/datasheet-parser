"""
Generic extractor for non-TI datasheets.

Uses pdfplumber text extraction from the first N pages, then broad regex
patterns to pull key specs.  Returns the same DeviceInfo type as the TI
extractor so parse.py can treat both paths uniformly.

Handles:
  - Garbled timestamp filenames  (2410121637_XySemi-XB3306D_C2759992.pdf → XB3306D)
  - Spaced part numbers          (M T 3 6 08 → MT3608)
  - (cid:N) encoded bullet chars (some Type-1 font PDFs)
  - Voltage ranges in several notations: "X to Y V", "X~YV", "X–YV"
  - Package abbreviations: SOT-23, SOP-8, TSSOP-8, QFN-16, etc.
  - Multi-manufacturer bullet styles: •  ·  ●  ■  ◆  –  *  n<space>
"""
import re
from pathlib import Path
from typing import Optional

import pdfplumber

from extractor.device_info import DeviceInfo, PackageVariant


# ── Text cleaning ─────────────────────────────────────────────────────────────

_CID_RE  = re.compile(r"\(cid:\d+\)")          # encoded ligature/bullet chars
_WS_RE   = re.compile(r"[ \t]+")
_NL_RE   = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    text = _CID_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


# ── Part number from filename ─────────────────────────────────────────────────
#
# Covers three filename conventions seen in the import folder:
#   1. "2410121637_XySemi-XB3306D_C2759992"  → XB3306D
#   2. "C84817 MT3608_plain"                  → MT3608
#   3. "HT7833-Holtek Semiconductor"          → HT7833
#   4. "XC6219B332MR"                         → XC6219B332MR  (already clean)

_TIMESTAMP_STEM   = re.compile(r"^\d{10,}_(.+?)(?:_C\d+)?$")  # "2410..._body_C123" → "body"
_LCSC_NUM         = re.compile(r"^C\d{5,}$", re.IGNORECASE)   # e.g. C2759992, C84817
_PART_SHAPE       = re.compile(r"^[A-Za-z]{1,6}\d[A-Za-z0-9\-]{1,20}$")  # "XB3306D", "IRF9358PBF"
_PART_WORD        = re.compile(r"\b([A-Za-z]{2,6}\d[A-Za-z0-9\-]{1,20})\b")  # no underscores
_GENERIC_TOKEN    = re.compile(r"^(MOSFET|IC|MODULE|CHIP|ELEC|TECH|PUBLIC|ASIC|SEMI)$",
                                re.IGNORECASE)


def _part_from_stem(stem: str) -> str:
    """Best-effort part number guess from a PDF filename stem."""

    def _best_from_tokens(tokens: list[str]) -> str:
        """Pick the longest part-shaped, non-generic token from a list."""
        candidates = [
            t for t in tokens
            if _PART_SHAPE.match(t) and not _LCSC_NUM.match(t) and not _GENERIC_TOKEN.match(t)
        ]
        return max(candidates, key=len) if candidates else ""

    # Convention 1: timestamp_body_Cdigits  (body may contain hyphens)
    m = _TIMESTAMP_STEM.match(stem)
    if m:
        body = m.group(1)   # e.g. "HXY-MOSFET-IRF9358PBF-HXY" or "XySemi-XB3306D"
        result = _best_from_tokens(body.split("-"))
        if result:
            return result

    # Convention 3: "HT7833-Holtek Semiconductor" — first hyphen/space token is the part
    first = re.split(r"[\s-]", stem)[0]
    if _PART_SHAPE.match(first) and not _LCSC_NUM.match(first):
        return first

    # Convention 2: "C84817 MT3608_plain" — split on whitespace/underscores and check tokens
    for token in re.split(r"[\s_]+", stem):
        if _PART_SHAPE.match(token) and not _LCSC_NUM.match(token) and not _GENERIC_TOKEN.match(token):
            return token

    # Last resort: regex scan (handles embedded part numbers without clean delimiters)
    for m2 in _PART_WORD.finditer(stem):
        candidate = m2.group(1)
        if not _LCSC_NUM.match(candidate) and not _GENERIC_TOKEN.match(candidate):
            return candidate

    return stem


# ── Part number from page-1 text ──────────────────────────────────────────────
#
# Typical layouts seen across manufacturers:
#   • First non-blank line is the part number  (XB3306D, FS8205A, MT3608…)
#   • First line is manufacturer, second is part  (UMW / XC6219)
#   • Spaced-out part: "M T 3 6 0 8" — collapse spaces between single chars

_SPACED_PN = re.compile(r"^([A-Za-z0-9])(\s[A-Za-z0-9]){3,}$")   # "M T 3 6 0 8"
_PN_SHAPE  = re.compile(r"^[A-Za-z]{1,6}\d[\w\-]{1,20}$")         # "XB3306D", "IRF9358PBF"


def _part_from_text(lines: list[str], hint: str) -> str:
    """
    Scan the first few page-1 lines for a part-number-shaped token.
    Uses hint (from filename) to confirm a match or fall back to it.
    """
    for ln in lines[:8]:
        ln = ln.strip()
        if not ln:
            continue
        # Collapse spaced part numbers
        if _SPACED_PN.match(ln):
            candidate = ln.replace(" ", "")
            if _PN_SHAPE.match(candidate):
                return candidate
        # Single-token line that looks like a part number
        if _PN_SHAPE.match(ln):
            return ln
        # Line contains the hint token prominently
        if hint and hint.upper() in ln.upper():
            # Extract the token that matches the hint
            for tok in ln.split():
                if hint.upper() in tok.upper() and _PN_SHAPE.match(tok):
                    return tok

    return hint   # fall back to filename-derived hint


# ── Package detection ─────────────────────────────────────────────────────────

_PKG_RE = re.compile(
    r"\b("
    r"SOT-?23(?:-\d+)?|"
    r"SOT-?89(?:-\d+)?|"
    r"SOT-?363|"
    r"SC-?70(?:-\d+)?|"
    r"SOP-?\d+|"
    r"SSOP-?\d+|"
    r"TSSOP-?\d+|"
    r"MSOP-?\d+|"
    r"HSOP-?\d+|"
    r"QFN-?\d*|"
    r"WQFN-?\d*|"
    r"DFN-?\d+|"
    r"LGA-?\d+|"
    r"WLCSP-?\d*|"
    r"TO-?92|TO-?252|TO-?263|TO-?220(?:F)?|"
    r"SOD-?\d+|"
    r"DO-?\d+[A-Z]?|"
    r"DIP-?\d+"
    r")\b",
    re.IGNORECASE,
)

_PIN_AFTER_PKG = re.compile(r"(\d{1,3})[\s-]?(?:pin|lead|ld)?", re.IGNORECASE)


def _find_packages(text: str) -> list[PackageVariant]:
    seen: set[str] = set()
    pkgs: list[PackageVariant] = []
    for m in _PKG_RE.finditer(text):
        raw = m.group(0)
        key = raw.upper().replace("-", "").replace(" ", "")
        if key in seen:
            continue
        seen.add(key)

        # Try to extract pin count from the package token itself
        pins: Optional[int] = None
        pin_m = re.search(r"-(\d+)$", raw)
        if pin_m:
            pins = int(pin_m.group(1))

        pkgs.append(PackageVariant(
            part_number="",
            package_code=raw,
            package_type=re.split(r"[-]?\d", raw)[0].upper(),
            pins=pins,
            body_size="",
        ))
    return pkgs


# ── Voltage extraction ────────────────────────────────────────────────────────
#
# Patterns ordered from most-specific to least.  We stop once we find vin_range.
#
# Notations covered:
#   "4.5V to 35V"  "4.5 to 35V"  "4.5~35V"  "4.5 – 35 V"
#   "2V to 24V Input"  "Input: 2V to 24V"  "VIN = 2 to 5.5V"

_V   = r"(\d+\.?\d*)\s*V?"      # capture a voltage number (V optional, added back)
_SEP = r"\s*(?:to|~|–|-|\.\.\.)\s*"  # range separator

# Full range with units on both sides or just the second
_VRANGE_RE = re.compile(
    rf"{_V}\s*V?\s*{_SEP}{_V}\s*V",
    re.IGNORECASE,
)

# Input voltage keyword context
_VIN_CTX = re.compile(
    rf"(?:input|VIN|supply)[^\n]{{0,40}}{_V}\s*V?\s*{_SEP}{_V}\s*V"
    rf"|{_V}\s*V?\s*{_SEP}{_V}\s*V[^\n]{{0,40}}(?:input|VIN|supply)",
    re.IGNORECASE,
)

# Output voltage keyword context
_VOUT_CTX = re.compile(
    rf"(?:output|VOUT)[^\n]{{0,40}}{_V}\s*V?\s*{_SEP}{_V}\s*V"
    rf"|{_V}\s*V?\s*{_SEP}{_V}\s*V[^\n]{{0,40}}(?:output|VOUT)",
    re.IGNORECASE,
)

# Single "up to X V" / "max X V" for supply rails
_VSINGLE_RE = re.compile(
    rf"(?:up\s+to|maximum|max\.?)\s*{_V}\s*V",
    re.IGNORECASE,
)


def _find_voltages(text: str) -> dict:
    out: dict = {}

    # Input range
    m = _VIN_CTX.search(text)
    if m:
        nums = re.findall(r"\d+\.?\d*", m.group(0))
        if len(nums) >= 2:
            out["vin_min"] = nums[0] + "V"
            out["vin_max"] = nums[1] + "V"

    # Output range
    m = _VOUT_CTX.search(text)
    if m:
        nums = re.findall(r"\d+\.?\d*", m.group(0))
        if len(nums) >= 2:
            out["vout_min"] = nums[0] + "V"
            out["vout_max"] = nums[1] + "V"

    # First plain voltage range as fallback for VIN if not found yet
    if "vin_min" not in out:
        m = _VRANGE_RE.search(text)
        if m:
            nums = re.findall(r"\d+\.?\d*", m.group(0))
            if len(nums) >= 2:
                out["vin_min"] = nums[0] + "V"
                out["vin_max"] = nums[1] + "V"

    # Single max supply
    if "vin_max" not in out:
        m = _VSINGLE_RE.search(text)
        if m:
            nums = re.findall(r"\d+\.?\d*", m.group(0))
            if nums:
                out["vin_max"] = nums[0] + "V"

    return out


# ── Current extraction ────────────────────────────────────────────────────────

_IOUT_RE = re.compile(
    r"(\d+\.?\d*)\s*[-–]?\s*[Aa]\b[^\n]{0,40}?"
    r"(?:output|charg|inductor|switch|peak|continuous|drain|current)",
    re.IGNORECASE,
)
_IOUT_RE2 = re.compile(
    r"(?:output|charg|peak|continuous|drain|max)[^\n]{0,40}?"
    r"(\d+\.?\d*)\s*[-–]?\s*A\b",
    re.IGNORECASE,
)


def _find_current(text: str) -> str:
    for pattern in (_IOUT_RE, _IOUT_RE2):
        m = pattern.search(text)
        if m:
            nums = re.findall(r"\d+\.?\d*", m.group(0))
            if nums:
                return nums[0] + "A"
    return ""


# ── Frequency extraction ──────────────────────────────────────────────────────

_FREQ_RE = re.compile(
    r"(\d+\.?\d*)\s*[-–]?\s*([kKmM]Hz)\b",
    re.IGNORECASE,
)


def _find_frequency(text: str) -> dict:
    hits = _FREQ_RE.findall(text[:3000])
    if not hits:
        return {}
    # Take the last hit (often in the features summary)
    val, unit = hits[-1]
    return {"freq_max": val + unit}


# ── Feature bullet extraction ─────────────────────────────────────────────────
#
# Bullet chars used across the datasheets in this batch:
#   •  ·  ●  ■  ◆  □  ▪  –  *  n<space> (ADI style)

_BULLET_RE = re.compile(
    r"(?:^|\n)\s*[•·●■◆□▪\*]\s*(.+)",
)
_ADI_BULLET_RE = re.compile(
    r"(?:^|\n)n\s{1,3}([A-Z].{5,80})",   # ADI "n VIN Range: …"
)
_DASH_BULLET_RE = re.compile(
    r"(?:^|\n)\s{0,4}[–\-]\s{1,3}([A-Z].{5,80})",
)

# Marker that likely starts the features section
_FEAT_SECTION = re.compile(
    r"(?:features?|key\s+features?|product\s+features?|highlights?)\s*\n",
    re.IGNORECASE,
)
_FEAT_STOP = re.compile(
    r"(?:applications?|description|general\s+desc|pin\s+desc|absolute\s+max|ordering)",
    re.IGNORECASE,
)


def _find_features(text: str) -> list[str]:
    # Try to narrow to the features section first
    sec_m = _FEAT_SECTION.search(text)
    if sec_m:
        after = text[sec_m.end():]
        stop_m = _FEAT_STOP.search(after)
        corpus = after[: stop_m.start()] if stop_m else after[:2000]
    else:
        corpus = text[:3000]

    bullets: list[str] = []
    for pattern in (_BULLET_RE, _ADI_BULLET_RE, _DASH_BULLET_RE):
        hits = pattern.findall(corpus)
        if hits:
            bullets.extend(h.strip() for h in hits if len(h.strip()) > 8)
        if len(bullets) >= 3:
            break

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for b in bullets:
        key = b[:60].lower()
        if key not in seen:
            seen.add(key)
            result.append(b)
    return result[:20]


# ── Interface detection ───────────────────────────────────────────────────────

_IFACE_RE = re.compile(r"\b(I2C|SPI|SMBus|SMBUS|UART|PMBus)\b", re.IGNORECASE)


def _find_interface(text: str) -> list[str]:
    return sorted(set(m.upper() for m in _IFACE_RE.findall(text[:4000])))


# ── Title line ────────────────────────────────────────────────────────────────

def _find_title(lines: list[str], part_number: str) -> str:
    """Return the first line that contains the part number AND more text."""
    for ln in lines[:15]:
        ln = ln.strip()
        if part_number.upper() in ln.upper() and len(ln) > len(part_number) + 5:
            return ln
    # Fallback: return any long descriptive line from the first page
    for ln in lines[:15]:
        ln = ln.strip()
        if len(ln) > 20 and not re.match(r"^https?://|^www\.", ln, re.IGNORECASE):
            return ln
    return part_number


# ── Manufacturer detection ────────────────────────────────────────────────────
#
# Not reliable enough to auto-detect generally, so we just look for known names
# in the first page.

_KNOWN_MFRS = [
    ("Texas Instruments", re.compile(r"\b(Texas Instruments|^TI\b)", re.IGNORECASE)),
    ("Analog Devices", re.compile(r"\b(Analog Devices|Linear Technology|LTC)\b", re.IGNORECASE)),
    ("Holtek", re.compile(r"\bHoltek\b", re.IGNORECASE)),
    ("UMW", re.compile(r"\bUMW\b")),
    ("XySemi", re.compile(r"\bXy\s*Semi\b", re.IGNORECASE)),
    ("Torex", re.compile(r"\bTorex\b", re.IGNORECASE)),
    ("Aerosemi", re.compile(r"\bAerosemi\b", re.IGNORECASE)),
    ("WCH", re.compile(r"\b(WCH|Qinheng)\b", re.IGNORECASE)),
    ("HXY", re.compile(r"\bHXY\b|HuaXuanYang", re.IGNORECASE)),
    ("CanSheng", re.compile(r"\bCanSheng\b", re.IGNORECASE)),
    ("Consonance", re.compile(r"\bConsonance\b", re.IGNORECASE)),
    ("NanJing Top Power", re.compile(r"\b(NanJing Top Power|Top Power)\b", re.IGNORECASE)),
    ("Silvertel", re.compile(r"\bSilvertel\b", re.IGNORECASE)),
    ("HanRun", re.compile(r"\bHan\s*Run\b", re.IGNORECASE)),
]


def _find_manufacturer(text: str) -> str:
    for name, pattern in _KNOWN_MFRS:
        if pattern.search(text):
            return name
    return ""


# ── Main extractor ────────────────────────────────────────────────────────────

class GenericExtractor:
    """
    Fallback extractor for non-TI datasheets.  Reads the first `n_pages` pages,
    cleans the text, and applies broad regex patterns to populate DeviceInfo.
    """

    def __init__(self, pdf_path: Path, n_pages: int = 3, debug: bool = False):
        self.pdf_path  = Path(pdf_path)
        self.n_pages   = n_pages
        self.debug     = debug

    def extract(self) -> DeviceInfo:
        stem = self.pdf_path.stem
        hint = _part_from_stem(stem)

        with pdfplumber.open(self.pdf_path) as pdf:
            pages = pdf.pages[: min(self.n_pages, len(pdf.pages))]
            page_texts = [_clean(p.extract_text() or "") for p in pages]

        p1_text   = page_texts[0]
        all_text  = "\n\n".join(page_texts)
        p1_lines  = [ln.strip() for ln in p1_text.splitlines() if ln.strip()]

        part_number  = _part_from_text(p1_lines, hint)
        title        = _find_title(p1_lines, part_number)
        packages     = _find_packages(p1_text)
        manufacturer = _find_manufacturer(all_text)
        features     = _find_features(all_text)
        interface    = _find_interface(all_text)
        voltages     = _find_voltages(all_text)
        iout_max     = _find_current(all_text)
        freq         = _find_frequency(all_text)

        if self.debug:
            print(f"  [generic] stem={stem!r}")
            print(f"  hint={hint!r}  part={part_number!r}")
            print(f"  packages: {[p.package_code for p in packages]}")
            print(f"  voltages: {voltages}")
            print(f"  iout_max: {iout_max!r}  freq: {freq}")
            print(f"  features ({len(features)}): {features[:3]}")

        return DeviceInfo(
            part_number=part_number,
            document_id="",
            title=title,
            packages=packages,
            features=features,
            applications=[],
            interface=interface,
            vin_min=voltages.get("vin_min", ""),
            vin_max=voltages.get("vin_max", ""),
            vin_startup="",
            vout_min=voltages.get("vout_min", ""),
            vout_max=voltages.get("vout_max", ""),
            vsupply_min="",
            vsupply_max="",
            iout_max=iout_max,
            freq_min="",
            freq_max=freq.get("freq_max", ""),
            temp_min="",
            temp_max="",
        )
