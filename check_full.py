"""Print all registers with field counts and addresses."""
import json, sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"Total: {len(data)}")
for r in data:
    print(f"  p{r['source_page']:3d}  {r['address']:6s}  {r['name']!r:50s}  {len(r['fields'])} field(s)")
