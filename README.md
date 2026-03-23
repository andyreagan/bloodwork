# Bloodwork Dashboard

Personal bloodwork and fitness tracker. Live at **[health.andyreagan.com](https://health.andyreagan.com)**.

## What it does

Tracks 19 blood draws (2013–2026) across 89 biomarkers, plus fitness metrics (VO₂max, FTP, weight).

- InsideTracker-inspired design with dark theme
- Color-coded status: 🟢 optimal / 🟡 normal / 🔴 out of range
- Trend arrows per biomarker (↑↓→)
- Line charts with reference range bands
- Panels: Metabolic, Lipids, Liver, Blood Count, Hormones, Inflammation, Vitamins, Electrolytes, Fitness
- Mobile-friendly, self-contained HTML

## Adding new data

All data lives in two human-editable YAML files. To add a new blood draw, edit `bloodwork_data.yaml` directly (or ask an LLM to do it):

```yaml
draws:
  - date: "2026-06-01"
    source: "Labcorp / LifeForce"
    notes: "Annual LifeForce panel."
    measurements:
      Glucose: {value: 95, unit: mg/dL}
      HbA1c: {value: 5.5, unit: '%'}
      # ... etc
```

Then regenerate:

```bash
cd ~/projects/bloodwork
python3 generate.py
git add -A && git commit -m "Add June 2026 bloodwork" && git push
```

## Files

| File | Purpose |
|------|---------|
| `bloodwork_data.yaml` | All bloodwork data (draws + reference ranges) |
| `fitness_data.yaml` | Fitness metrics (VO₂max, FTP, weight, race times, lifts) |
| `generate.py` | Reads both YAMLs → generates `index.html` |
| `index.html` | Static dashboard (committed for GitHub Pages) |

## Privacy

This repo is public. The HTML embeds all data as JSON — **do not commit sensitive non-health data here**. Health data (bloodwork, fitness) is intentionally shared. No full date of birth is included — only birth year (1989) for age calculation.
