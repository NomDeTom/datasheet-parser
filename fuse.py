"""
fuse.py — reconcile the two parameter sources inside a TI datasheet into one best estimate.

`parse.py` yields two independent views of the same numbers:

  A. device_info.json  — scraped from page 1. TI's two-column layout interleaves the Features and
     Description columns, so numbers routinely land in the wrong field. Measured on this vault it
     echoed Vin into Vout on 5 of 5 TPS552xx parts.

  B. elec_chars.json "Recommended Operating Conditions" — a real table, read by pdfplumber from
     actual cell structure. Correct on all 5 of those, but has its own failure modes: it picks up
     abs-max rows (TPS55160 -> "0 36"), echoes Vin itself on some parts (TPS55285), and occasionally
     returns prose ("Vin 20" on TPS61382-Q1).

Neither is trustworthy alone, but they fail *differently*. Agreement between them is strong evidence;
disagreement is exactly the row a human should look at. This module encodes that:

    both agree            -> confidence "high"
    disagree              -> prefer ROC (empirically better), confidence "low", both recorded
    ROC only              -> confidence "medium"
    page-1 only           -> confidence "low"
    neither               -> None, confidence "none"

plus sanity flags for the known failure modes, so a bad extraction is labelled rather than silently
believed.
"""
import re

# ── numeric parsing ──────────────────────────────────────────────────────────
_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
           "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}

# '-0.3', '3.0', '2.2M', '500m' — a bare number with an optional SI prefix directly attached
_NUM_RE = re.compile(r"([-+−–]?\d*\.?\d+)\s*([pnuµμmkKMG])?(?=[\s–−]|$|[VAWH])")

_UNIT_SCALE = {"V": 1.0, "A": 1.0, "W": 1.0, "HZ": 1e-3, "KHZ": 1.0, "MHZ": 1e3, "GHZ": 1e6}


def numbers_in(*cells, unit=""):
    """Pull every number out of one or more table cells, SI-scaled, in document order.

    The parser frequently mashes a row's MIN/TYP/MAX into a single cell ('3.0 36'), so the caller
    gets a list and decides how to read it.
    """
    out = []
    for cell in cells:
        if not cell:
            continue
        text = str(cell).replace("−", "-").replace("–", " ")
        for raw, prefix in _NUM_RE.findall(text):
            try:
                val = float(raw.replace("−", "-").replace("–", "-"))
            except ValueError:
                continue
            val *= _PREFIX.get(prefix or "", 1.0)
            out.append(val)
    scale = _UNIT_SCALE.get((unit or "").strip().upper(), 1.0)
    return [round(v * scale, 6) for v in out]


def value_of(raw, unit_hint=""):
    """Single number out of a device_info string like '3.0V' / '2.2MHz' / '500mA'."""
    if not raw:
        return None
    text = str(raw)
    m = re.search(r"([-+−]?\d*\.?\d+)\s*([pnuµμmkKMG])?\s*(V|A|W|Hz|kHz|MHz|GHz)?", text, re.I)
    if not m:
        return None
    try:
        val = float(m.group(1).replace("−", "-"))
    except ValueError:
        return None
    val *= _PREFIX.get(m.group(2) or "", 1.0)
    unit = (m.group(3) or unit_hint or "").upper()
    return round(val * _UNIT_SCALE.get(unit, 1.0), 6)


# ── Recommended Operating Conditions lookup ──────────────────────────────────
_ROW_PATTERNS = {
    "vin":  (re.compile(r"\bv\s*_?in\b|input voltage", re.I), "V"),
    "vout": (re.compile(r"\bv\s*_?out\b|output voltage", re.I), "V"),
    "iout": (re.compile(r"\bi\s*_?out\b|output current", re.I), "A"),
    "fsw":  (re.compile(r"switching frequency|\bf\s*_?sw\b", re.I), "kHz"),
}

# rows that look like the right parameter but are actually the wrong table
_ROW_EXCLUDE = re.compile(r"abs(olute)?\s*max|storage|junction|ripple|accuracy|"
                          r"uvlo|undervoltage|threshold|logic|leakage", re.I)


def roc_range(elec, key):
    """(lo, hi) for `key` from the Recommended Operating Conditions section, or (None, None)."""
    pattern, unit = _ROW_PATTERNS[key]
    for section in elec or []:
        if "recommended operating" not in (section.get("name") or "").lower():
            continue
        for spec in section.get("specs") or []:
            blob = f"{spec.get('symbol','')} {spec.get('parameter','')}"
            if not pattern.search(blob) or _ROW_EXCLUDE.search(blob):
                continue
            nums = numbers_in(spec.get("min"), spec.get("typ"), spec.get("max"),
                              unit=spec.get("unit") or unit)
            if len(nums) >= 3:
                return nums[0], nums[-1]      # min .. max, ignoring typ in between
            if len(nums) == 2:
                return nums[0], nums[1]
            if len(nums) == 1:
                return nums[0], None
    return None, None


# ── fusion ───────────────────────────────────────────────────────────────────
def _close(a, b, tol=0.05):
    if a is None or b is None:
        return False
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / scale <= tol


def fuse_pair(page1, roc):
    """Reconcile one (lo, hi) pair. Returns (lo, hi, confidence, disagreement-or-None)."""
    p_lo, p_hi = page1
    r_lo, r_hi = roc
    has_p = p_lo is not None or p_hi is not None
    has_r = r_lo is not None or r_hi is not None

    if not has_p and not has_r:
        return None, None, "none", None
    if has_r and not has_p:
        return r_lo, r_hi, "medium", None
    if has_p and not has_r:
        return p_lo, p_hi, "low", None

    if _close(p_lo, r_lo) and _close(p_hi, r_hi):
        return r_lo, r_hi, "high", None
    return (r_lo, r_hi, "low",
            f"page1={fmt(p_lo)}–{fmt(p_hi)} vs ROC={fmt(r_lo)}–{fmt(r_hi)}")


def fmt(v):
    if v is None:
        return "?"
    return f"{v:g}"


def _end_agrees(x, y):
    """One end of a range. Two absent values agree; one absent does not."""
    if x is None and y is None:
        return True
    if x is None or y is None:
        return False
    return _close(x, y)


def _pair_agrees(a, b):
    """Two (lo, hi) pairs say the same thing, treating absent-on-both-sides as agreement.

    Requiring both ends to be present rejected iout, which only ever has a max — so a genuine
    agreement was reported as a disagreement ("prose=?-4 vs page1=?-4").
    """
    if not any(v is not None for v in (*a, *b)):
        return False
    return _end_agrees(a[0], b[0]) and _end_agrees(a[1], b[1])


def fuse_triple(page1, roc, prose):
    """Reconcile three views of one (lo, hi) pair.

    Measured precision on a hand-verified set: prose 97%, ROC better than page-1, page-1 worst.
    So the tie-break order is prose -> ROC -> page-1, but agreement between any two outranks a
    single source's opinion.
    """
    present = [(name, pair) for name, pair in
               (("prose", prose), ("roc", roc), ("page1", page1))
               if pair and any(v is not None for v in pair)]
    if not present:
        return None, None, "none", None
    if len(present) == 1:
        name, pair = present[0]
        return pair[0], pair[1], ("medium" if name in ("prose", "roc") else "low"), None

    # Any two in agreement wins outright.
    for i in range(len(present)):
        for j in range(i + 1, len(present)):
            if _pair_agrees(present[i][1], present[j][1]):
                lo, hi = present[i][1]
                # Only a source that actually differs is a dissenter — a third source that agrees
                # is corroboration, and reporting it as "outvoted" was simply wrong.
                dissenters = [(n, pr) for n, pr in present
                              if n not in (present[i][0], present[j][0])
                              and any(v is not None for v in pr)
                              and not _pair_agrees(pr, (lo, hi))]
                if dissenters:
                    n, pr = dissenters[0]
                    return (lo, hi, "high",
                            f"{present[i][0]}+{present[j][0]}={fmt(lo)}–{fmt(hi)} "
                            f"outvoted {n}={fmt(pr[0])}–{fmt(pr[1])}")
                return lo, hi, "high", None

    # No two agree — take the most reliable source and say so.
    name, pair = present[0]
    others = " vs ".join(f"{n}={fmt(pr[0])}–{fmt(pr[1])}" for n, pr in present[1:])
    return pair[0], pair[1], "low", f"{name}={fmt(pair[0])}–{fmt(pair[1])} vs {others}"


def fuse(info, elec, text=None):
    """Fuse device_info + elec_chars (+ optional textspec claims) into one estimate.

    With the third source present the sources vote: any two agreeing beats a lone dissenter, which
    is what fixes TPS55287 (page-1 Vout echoes Vin, ROC and the prose both say 0.8-22V) without
    needing a hand-written override.
    """
    info = info or {}
    p = {
        "vin":  (value_of(info.get("vin_min"), "V"),  value_of(info.get("vin_max"), "V")),
        "vout": (value_of(info.get("vout_min"), "V"), value_of(info.get("vout_max"), "V")),
        "iout": (None,                                 value_of(info.get("iout_max"), "A")),
        "fsw":  (value_of(info.get("freq_min"), "kHz"), value_of(info.get("freq_max"), "kHz")),
    }
    r = {k: roc_range(elec, k) for k in _ROW_PATTERNS}

    # Third view: claims made in the datasheet's own prose (see textspec.py).
    t = {}
    if text:
        g = lambda k: (text.get(k) or {}).get("value")
        t = {
            "vin":  (g("vin_min"), g("vin_max")),
            "vout": (g("vout_min"), g("vout_max")),
            "iout": (None, g("iout_max")),
            "fsw":  (g("fsw_min_khz"), g("fsw_max_khz") or g("fsw_fixed_khz")),
        }

    values, confidence, disagreements = {}, {}, {}
    for key in ("vin", "vout", "iout", "fsw"):
        lo, hi, conf, clash = fuse_triple(p[key], r[key], t.get(key, (None, None)))
        values[key] = (lo, hi)
        confidence[key] = conf
        if clash:
            disagreements[key] = clash

    flags = []
    vin_lo, vin_hi = values["vin"]
    vout_lo, vout_hi = values["vout"]

    # The signature failure: Vout identical to Vin means the extractor copied the wrong column.
    # Before discarding, try the other source — when ROC echoes Vin, page-1 is often intact
    # (TPS55285: ROC said 2.4–22, page-1 correctly said 0.8–15).
    if (vout_lo is not None and vout_hi is not None
            and _close(vout_lo, vin_lo, 0.01) and _close(vout_hi, vin_hi, 0.01)):
        flags.append("vout_echoes_vin")
        p_lo, p_hi = p["vout"]
        recoverable = (p_lo is not None and p_hi is not None
                       and not (_close(p_lo, vin_lo, 0.01) and _close(p_hi, vin_hi, 0.01)))
        if recoverable:
            flags.append("vout_from_page1_fallback")
            values["vout"] = (p_lo, p_hi)
            confidence["vout"] = "low"
        else:
            values["vout"] = (None, None)
            confidence["vout"] = "none"

    # A 0V minimum on a converter input is an abs-max row that leaked in.
    if vin_lo == 0:
        flags.append("vin_min_zero_suspect")
        confidence["vin"] = "low"

    for key in ("vin", "vout"):
        lo, hi = values[key]
        # lo == hi is legitimate: fixed-output parts (TPS61097A-33 is 3.3V only, TPS63805 5V only)
        # report an identical min and max. Only lo > hi is impossible.
        if lo is not None and hi is not None and lo > hi:
            flags.append(f"{key}_range_inverted")
            values[key] = (None, None)
            confidence[key] = "none"

    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    rated = [confidence[k] for k in ("vin", "vout") if confidence[k] != "none"]
    overall = min(rated, key=lambda c: order[c]) if rated else "none"
    # A missing Vout or any anomaly flag means the extraction is not clean, whatever Vin scored.
    if confidence["vout"] == "none" or flags:
        overall = "low" if overall != "none" else "none"

    return {"values": values, "confidence": confidence,
            "disagreements": disagreements, "flags": flags, "overall": overall}
