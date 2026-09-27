# In Obsidian

**Cards.** `cards.py` owns the frontmatter keys it generates (`part`, `manufacturer`, `product_type`, `topology`, `vin_min` … `confidence_*`, `contested`, `verified_params`, `is_buck`, `converts_voltage`, `lcsc`, …) and the body above `## Notes`. Everything else — `category`, `verified`, `tags`, `aliases`, anything you add, and all of `## Notes` — is yours and survives every regeneration.

**Views.** `views/*.base` are Obsidian Bases views (Obsidian 1.9+, core plugin): Library, Needs review (low / flagged / verified), Power, LoRa. They filter on card properties, so they work whether Obsidian opens the whole vault or `AutoNotes/` alone. Embed one in a summary page with `![[LoRa.base]]`; Dataview queries work too.

**Search.** The `.text.txt` files are plain text, `.txt` rather than `.md` so Obsidian does not index them: multi-megabyte datasheets stalled the mobile app. Obsidian's own search therefore does not see them; `grep -n` gives page numbers, `build_db.py` puts every page in the full-text database, and LLM tools can read them directly. The card links each one as an attachment. Extracted text is escaped so it cannot become vault structure (`[[2:1]]` bit ranges, `#define`).
