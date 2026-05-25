import json, sys
from pathlib import Path

path = sys.argv[1]
data = json.loads(Path(path).read_text(encoding="utf-8"))
print(f"Total registers: {len(data)}")
for r in data[:5]:
    print(f"  {r['address']}  {r['name']!r}  reset={r['reset']!r}")
    for f in r["fields"][:3]:
        print(f"    [{f['bits']}] {f['name']!r}  {f['access']}  reset={f['reset']!r}")
