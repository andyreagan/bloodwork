# Bloodwork Dashboard

Private personal bloodwork tracker for Andy Reagan.

## What it does

Parses `bloodwork.org` (17 blood draws, 2013–2025) → SQLite → static HTML dashboard.

- **88 distinct biomarkers** tracked
- InsideTracker-inspired design with dark theme
- Color-coded status: 🟢 optimal / 🟡 normal / 🔴 out of range
- Trend arrows per biomarker (↑↓→)
- Line charts with reference range bands
- Grouped into panels: Metabolic, Lipids, Liver, Blood Count, Hormones, Inflammation, Vitamins, Electrolytes
- Mobile-friendly
- Self-contained HTML, no server required

## Usage

```bash
# Full rebuild from Synology
make all

# Or step by step:
make fetch    # rsync bloodwork.org from Synology
make parse    # parse.py → bloodwork.db
make generate # generate.py → index.html

# Rebuild and push to GitHub
make deploy
```

## Files

| File | Purpose |
|------|---------|
| `bloodwork.org` | Source data (not committed — pull from Synology) |
| `parse.py` | Parses org file → SQLite DB |
| `generate.py` | Generates index.html from DB |
| `bloodwork.db` | SQLite database (not committed) |
| `index.html` | Static dashboard (committed for GitHub Pages) |
| `Makefile` | Build automation |

## Schema

```sql
measurements(id, date, biomarker, value, unit)
reference_ranges(biomarker, low, high, optimal_low, optimal_high, unit)
```

## Privacy

All private. The source `.org` file and `.db` are gitignored. The HTML embeds the data as JSON — keep this repo private.
