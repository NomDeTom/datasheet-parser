"""
verify_twins.py — check the generated twin notes are what we expect before trusting them.

Run after `twin_notes.py` and `enrich_ti.py`. Reports, and exits non-zero if a structural check
fails (missing notes, broken frontmatter, dangling PDF links, impossible numbers). Confidence and
flag distributions are informational — a low-confidence note is the system working, not a failure.
"""
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

VAULT = Path(r"D:\Clod\AutoNotes\Reference Material")
PARAMETRICS = VAULT / "TI Parametrics"
SUFFIX = " (datasheet)"
OVERRIDES_PATH = Path(__file__).resolve().parent / "ti_overrides.json"


def load_overrides():
    """Deliberate corrections to TI's export — a value differing from the export is expected here."""
    if not OVERRIDES_PATH.exists():
        return {}
    data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def frontmatter(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    front = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m:
            front[m.group(1)] = m.group(2).strip()
    return front


def as_num(raw):
    if raw in (None, "", "null"):
        return None
    try:
        return float(str(raw).strip('"'))
    except ValueError:
        return None


def main():
    seen, pdfs = set(), []
    for pattern in ("*.pdf", "*.PDF"):          # case-insensitive on Windows — dedupe by path
        for p in VAULT.rglob(pattern):
            if p.parent.name.lower() == "attachments" and str(p).lower() not in seen:
                seen.add(str(p).lower())
                pdfs.append(p)
    twins = sorted(VAULT.rglob(f"*{SUFFIX}.md"))
    problems = []

    print(f"PDFs in attachments : {len(pdfs)}")
    print(f"Twin notes          : {len(twins)}")

    # 1 — one twin per PDF
    twin_stems = {t.stem.replace(SUFFIX, "").lower() for t in twins}
    missing = sorted({p.stem.lower() for p in pdfs} - twin_stems)
    if missing:
        problems.append(f"{len(missing)} PDF(s) with no twin note")
        print(f"\n  MISSING TWINS ({len(missing)}): {', '.join(missing[:8])}"
              + (" ..." if len(missing) > 8 else ""))
    else:
        print("  [ok] every PDF has a twin")

    # 2 — no basename collision with the hand-written notes (would break [[wikilinks]])
    # Compare the actual note basenames: "bq24074 (datasheet)" vs "BQ24074" do NOT collide —
    # that suffix is the whole reason the twins are safe.
    curated = {p.stem.lower() for p in VAULT.rglob("*.md") if not p.stem.endswith(SUFFIX)}
    twin_names = {t.stem.lower() for t in twins}
    clashes = sorted(twin_names & curated)
    if clashes:
        problems.append(f"{len(clashes)} twin/curated basename collision(s)")
        print(f"  COLLISIONS: {', '.join(clashes[:8])}")
    else:
        print("  [ok] no basename collisions with curated notes")

    # 3 — frontmatter integrity and numeric sanity
    conf, flags, sources, no_front, bad_link = Counter(), Counter(), Counter(), [], []
    inverted, verified, parse_errors = [], 0, []
    for twin in twins:
        front = frontmatter(twin)
        if front is None:
            no_front.append(twin.name)
            continue
        conf[front.get("confidence", "«absent»").strip('"')] += 1
        sources[front.get("source", "pdf-fusion").strip('"')] += 1
        if front.get("verified") == "true":
            verified += 1
        if "parse_error" in front:
            parse_errors.append(twin.stem.replace(SUFFIX, ""))
        for f in re.findall(r'"([^"]+)"', front.get("flags", "")):
            flags[f] += 1

        src = front.get("source_pdf", "").strip('"')
        if src and not (twin.parent / src).exists():
            bad_link.append(twin.name)

        for lo_k, hi_k in (("vin_min", "vin_max"), ("vout_min", "vout_max")):
            lo, hi = as_num(front.get(lo_k)), as_num(front.get(hi_k))
            # lo == hi is a fixed-output part, not an error — only lo > hi is impossible
            if lo is not None and hi is not None and lo > hi:
                inverted.append(f"{twin.stem.replace(SUFFIX,'')}: {lo_k}={lo} >= {hi_k}={hi}")

    if no_front:
        problems.append(f"{len(no_front)} note(s) with unreadable frontmatter")
        print(f"  BROKEN FRONTMATTER: {', '.join(no_front[:5])}")
    else:
        print("  [ok] all frontmatter parses")

    if bad_link:
        problems.append(f"{len(bad_link)} note(s) pointing at a missing PDF")
        print(f"  DANGLING source_pdf: {', '.join(bad_link[:5])}")
    else:
        print("  [ok] every source_pdf resolves")

    if inverted:
        problems.append(f"{len(inverted)} inverted range(s) survived")
        print(f"  INVERTED RANGES: {inverted[:5]}")
    else:
        print("  [ok] no inverted voltage ranges")

    print(f"\nConfidence : " + ", ".join(f"{k}={v}" for k, v in conf.most_common()))
    print(f"Source     : " + ", ".join(f"{k}={v}" for k, v in sources.most_common()))
    print(f"Verified   : {verified}")
    if flags:
        print(f"Flags      : " + ", ".join(f"{k}={v}" for k, v in flags.most_common()))
    if parse_errors:
        print(f"Not parsed : {len(parse_errors)} — {', '.join(parse_errors[:6])}"
              + (" ..." if len(parse_errors) > 6 else ""))

    # 4 — spot-check enriched values against the TI exports they came from
    print("\nCross-check against TI exports:")
    overrides = load_overrides()
    checked = wrong = overridden = 0
    for csv_path in sorted(PARAMETRICS.glob("*.csv")):
        rows = list(csv.reader(csv_path.open(encoding="utf-8")))
        hi = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Product or Part number"),
                  None)
        if hi is None:
            continue
        header = [h.strip() for h in rows[hi]]
        norm = lambda s: re.sub(r"[^A-Z0-9]", "", (s or "").upper())
        table = {norm(r[0]): dict(zip(header, r)) for r in rows[hi + 1:] if r and r[0].strip()}
        for twin in twins:
            front = frontmatter(twin)
            if not front or front.get("source", "").strip('"') != "ti-parametric":
                continue
            rec = table.get(norm(twin.stem.replace(SUFFIX, "")))
            if not rec:
                continue
            for header_name, key in (("Vin (min) (V)", "vin_min"), ("Vin (max) (V)", "vin_max"),
                                     ("Vout (min) (V)", "vout_min"), ("Vout (max) (V)", "vout_max")):
                want, got = as_num(rec.get(header_name)), as_num(front.get(key))
                if want is None:
                    continue
                checked += 1
                if got is None or abs(got - want) > 1e-6:
                    part = twin.stem.replace(SUFFIX, "")
                    ovr = overrides.get(norm(part), {})
                    if key in ovr and got is not None and abs(float(ovr[key]) - got) < 1e-6:
                        overridden += 1      # deliberate correction, not a defect
                        continue
                    wrong += 1
                    print(f"  MISMATCH {part} {key}: note={got} TI={want}")
    if checked:
        print(f"  {checked - wrong - overridden}/{checked - overridden} enriched values match their "
              f"export exactly"
              + (f"; {overridden} vetted override(s) intentionally differ" if overridden else ""))
        if wrong:
            problems.append(f"{wrong} enriched value(s) disagree with the export")
    else:
        print("  (nothing enriched yet)")

    print("\n" + ("FAILED: " + "; ".join(problems) if problems else "All structural checks passed."))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
