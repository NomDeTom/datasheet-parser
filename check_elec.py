"""Quick quality check of elec_chars.json output."""
import json, sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for sec in data:
    print(f"\n{'='*60}")
    print(f"Section : {sec['name']}")
    print(f"Page    : {sec['source_page']}")
    print(f"Conds   : {sec['conditions'][:80]}")
    print(f"Specs   : {len(sec['specs'])}")
    group = None
    for sp in sec["specs"][:12]:
        if sp["group"] != group:
            group = sp["group"]
            if group:
                print(f"  --- {group} ---")
        sym  = sp["symbol"][:22].ljust(22)
        par  = sp["parameter"][:35].ljust(35)
        vals = f"min={sp['min'][:8]:<8} typ={sp['typ'][:8]:<8} max={sp['max'][:8]:<8}"
        unit = sp["unit"]
        print(f"  {sym}  {par}  {vals}  {unit}")
    if len(sec["specs"]) > 12:
        print(f"  … ({len(sec['specs']) - 12} more)")
