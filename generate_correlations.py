#!/usr/bin/env python3
"""
Generate correlations.html — standalone analysis page.

Reads bloodwork_data.yaml + fitness_data.yaml and produces a static
correlations.html with:
  - Biomarker × covariate (age, #kids, weight, VO₂max) Pearson-r table
  - Top 20 pairwise biomarker–biomarker correlations
  - Scatter plots for selected interesting pairs
  - Covariate timeline chart
  - Auto-generated insight cards
"""
import json
import os
from datetime import date

import yaml

BLOODWORK_FILE = os.path.join(os.path.dirname(__file__), "bloodwork_data.yaml")
FITNESS_FILE   = os.path.join(os.path.dirname(__file__), "fitness_data.yaml")
HTML_FILE      = os.path.join(os.path.dirname(__file__), "correlations.html")

BIRTH_YEAR = 1989

# Kids milestones
LIFE_EVENTS = [
    {"date": "2017-10-01", "label": "Kid #1", "kids": 1},
    {"date": "2019-04-01", "label": "Kid #2", "kids": 2},
    {"date": "2021-07-01", "label": "Kid #3", "kids": 3},
]

# Biomarkers to include in the analysis
BIOMARKERS_FOR_CORR = [
    "Glucose", "HbA1c", "Cholesterol", "LDL", "HDL", "Triglycerides",
    "Testosterone", "Free Testosterone", "SHBG", "Cortisol",
    "Vitamin D", "Ferritin", "hsCRP", "Homocysteine",
    "AST", "ALT", "GGT", "Hemoglobin", "Hematocrit",
    "TSH", "IGF-1", "DHEA-S", "Apolipoprotein B", "Lipoprotein(a)",
]

# Scatter pairs to visualise explicitly
SCATTER_PAIRS = [
    ("Glucose",      "HbA1c"),
    ("Cholesterol",  "LDL"),
    ("HDL",          "Triglycerides"),
    ("Testosterone", "SHBG"),
    ("AST",          "ALT"),
    ("Ferritin",     "Hemoglobin"),
    ("Testosterone", "Cortisol"),
    ("Vitamin D",    "Testosterone"),
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_bloodwork(path: str) -> tuple[dict, dict]:
    with open(path, "r", encoding="utf-8") as f:
        bw = yaml.safe_load(f)
    ref_ranges = bw["reference_ranges"]
    measurements: dict[str, list] = {}
    for draw in bw["draws"]:
        for bm, m in draw["measurements"].items():
            measurements.setdefault(bm, []).append({
                "date":  draw["date"],
                "value": m["value"],
                "unit":  m["unit"],
            })
    for k in measurements:
        measurements[k].sort(key=lambda x: x["date"])
    return measurements, ref_ranges


def load_fitness(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for k in list(data):
        if data[k] is None:
            data[k] = []
    return data


# ---------------------------------------------------------------------------
# Maths helpers
# ---------------------------------------------------------------------------

def pearson_r(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num   = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx    = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy    = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Return (slope, intercept) for simple OLS, or None."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num  = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den  = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope     = num / den
    intercept = my - slope * mx
    return slope, intercept


# ---------------------------------------------------------------------------
# Lookup helpers (nearest-prior value)
# ---------------------------------------------------------------------------

def make_prior_lookup(series: list[dict], value_key: str = "value"):
    """Return a function date_str -> float | None giving last known value."""
    series = sorted([e for e in series if e], key=lambda e: e["date"])
    def lookup(date_str: str) -> float | None:
        val = None
        for e in series:
            if e["date"] <= date_str:
                val = e[value_key]
            else:
                break
        return val
    return lookup


def kids_count_at(date_str: str) -> int:
    count = 0
    for ev in LIFE_EVENTS:
        if date_str >= ev["date"]:
            count = ev["kids"]
    return count


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def build_payload(measurements: dict, fitness: dict) -> dict:
    weight_lookup = make_prior_lookup(fitness.get("weight_lbs", []))
    vo2_lookup    = make_prior_lookup(fitness.get("vo2max", []))

    # date → value dicts for fast lookup in pairwise section
    bm_date_val: dict[str, dict[str, float]] = {
        bm: {pt["date"]: pt["value"] for pt in measurements[bm]}
        for bm in BIOMARKERS_FOR_CORR
        if bm in measurements
    }

    # ------------------------------------------------------------------
    # 1. Biomarker × covariate matrix
    # ------------------------------------------------------------------
    biomarker_matrix = []
    for bm in BIOMARKERS_FOR_CORR:
        data = measurements.get(bm)
        if not data or len(data) < 3:
            continue

        ages, kids_list, weights, vo2s, vals = [], [], [], [], []
        for pt in data:
            d   = pt["date"]
            age = int(d[:4]) - BIRTH_YEAR
            w   = weight_lookup(d)
            v2  = vo2_lookup(d)
            ages.append(age)
            kids_list.append(kids_count_at(d))
            weights.append(w)
            vo2s.append(v2)
            vals.append(pt["value"])

        r_age  = pearson_r(ages, vals)
        r_kids = pearson_r(kids_list, vals)

        w_valid  = [(w, v) for w, v in zip(weights, vals) if w  is not None]
        v2_valid = [(v2, v) for v2, v in zip(vo2s, vals)   if v2 is not None]

        r_weight = pearson_r([p[0] for p in w_valid],  [p[1] for p in w_valid])  if len(w_valid)  >= 3 else None
        r_vo2    = pearson_r([p[0] for p in v2_valid], [p[1] for p in v2_valid]) if len(v2_valid) >= 2 else None

        biomarker_matrix.append({
            "name":     bm,
            "n":        len(vals),
            "r_age":    round(r_age,    3) if r_age    is not None else None,
            "r_kids":   round(r_kids,   3) if r_kids   is not None else None,
            "r_weight": round(r_weight, 3) if r_weight is not None else None,
            "r_vo2":    round(r_vo2,    3) if r_vo2    is not None else None,
        })

    biomarker_matrix.sort(key=lambda x: abs(x["r_age"] or 0), reverse=True)

    # ------------------------------------------------------------------
    # 2. Top pairwise biomarker–biomarker correlations
    # ------------------------------------------------------------------
    frequent = [bm for bm in BIOMARKERS_FOR_CORR
                if bm in bm_date_val and len(bm_date_val[bm]) >= 6]

    top_corrs = []
    for i in range(len(frequent)):
        for j in range(i + 1, len(frequent)):
            a, b = frequent[i], frequent[j]
            shared = sorted(bm_date_val[a].keys() & bm_date_val[b].keys())
            if len(shared) < 4:
                continue
            xs = [bm_date_val[a][d] for d in shared]
            ys = [bm_date_val[b][d] for d in shared]
            r  = pearson_r(xs, ys)
            if r is None:
                continue
            top_corrs.append({
                "pair": f"{a} × {b}",
                "bm_a": a,
                "bm_b": b,
                "r":    round(r, 3),
                "n":    len(shared),
            })

    top_corrs.sort(key=lambda x: abs(x["r"]), reverse=True)
    top_corrs = top_corrs[:20]

    # ------------------------------------------------------------------
    # 3. Scatter data (with regression line endpoints)
    # ------------------------------------------------------------------
    scatter_pairs = []
    for bm_a, bm_b in SCATTER_PAIRS:
        dv_a = bm_date_val.get(bm_a, {})
        dv_b = bm_date_val.get(bm_b, {})
        shared = sorted(dv_a.keys() & dv_b.keys())
        if len(shared) < 3:
            continue
        xs = [dv_a[d] for d in shared]
        ys = [dv_b[d] for d in shared]
        r  = pearson_r(xs, ys)
        reg = linear_regression(xs, ys)
        x_min, x_max = min(xs), max(xs)
        reg_line = None
        if reg:
            slope, intercept = reg
            reg_line = [
                {"x": x_min, "y": round(slope * x_min + intercept, 4)},
                {"x": x_max, "y": round(slope * x_max + intercept, 4)},
            ]
        scatter_pairs.append({
            "x_label":  bm_a,
            "y_label":  bm_b,
            "r":        round(r, 3) if r is not None else None,
            "n":        len(shared),
            "points":   [{"x": x, "y": y, "date": d} for x, y, d in zip(xs, ys, shared)],
            "reg_line": reg_line,
        })

    # ------------------------------------------------------------------
    # 4. Timeline (one row per blood-draw date)
    # ------------------------------------------------------------------
    all_dates = sorted({pt["date"] for pts in measurements.values() for pt in pts})
    timeline = []
    for d in all_dates:
        row = {
            "date":   d,
            "age":    int(d[:4]) - BIRTH_YEAR,
            "kids":   kids_count_at(d),
            "weight": weight_lookup(d),
            "vo2max": vo2_lookup(d),
        }
        for bm in BIOMARKERS_FOR_CORR:
            v = bm_date_val.get(bm, {}).get(d)
            if v is not None:
                row[bm] = v
        timeline.append(row)

    # ------------------------------------------------------------------
    # 5. Auto-generated insights
    # ------------------------------------------------------------------
    insights = []

    def strongest(rows, key):
        valid = [r for r in rows if r.get(key) is not None]
        if not valid:
            return None
        return max(valid, key=lambda r: abs(r[key]))

    row = strongest(biomarker_matrix, "r_age")
    if row:
        r = row["r_age"]
        insights.append({
            "emoji": "📈", "color": "#ef4444" if r > 0 else "#22c55e",
            "title": f"{row['name']} changes most with age (r = {r:+.2f})",
            "body":  (f"{row['name']} rises steadily as Andy ages — worth monitoring."
                      if r > 0 else
                      f"{row['name']} declines with age — common pattern; check optimal ranges."),
        })

    row = strongest(biomarker_matrix, "r_kids")
    if row:
        r = row["r_kids"]
        strength = "strongly" if abs(r) >= 0.6 else "moderately"
        insights.append({
            "emoji": "👶", "color": "#eab308",
            "title": f"{row['name']} correlates with # kids (r = {r:+.2f})",
            "body":  (f"The arrival of children (2017, 2019, 2021) {strength} correlates with "
                      f"changes in {row['name']} — could reflect lifestyle, sleep, or stress."),
        })

    row = strongest(biomarker_matrix, "r_weight")
    if row and row["r_weight"] is not None:
        r = row["r_weight"]
        insights.append({
            "emoji": "⚖️", "color": "#60a5fa",
            "title": f"{row['name']} tracks weight most closely (r = {r:+.2f})",
            "body":  (f"Body weight explains meaningful variation in {row['name']}. "
                      f"Diet and body-composition improvements could directly move this marker."),
        })

    row = strongest(biomarker_matrix, "r_vo2")
    if row and row["r_vo2"] is not None:
        r = row["r_vo2"]
        direction = "higher" if r > 0 else "lower"
        insights.append({
            "emoji": "🫁", "color": "#22c55e",
            "title": f"{row['name']} correlates with VO₂max (r = {r:+.2f})",
            "body":  (f"Better aerobic fitness (VO₂max) tracks with {direction} {row['name']} "
                      f"— aerobic training may be a meaningful lever."),
        })

    if top_corrs:
        tp = top_corrs[0]
        direction = "together" if tp["r"] > 0 else "in opposite directions"
        insights.append({
            "emoji": "🔗", "color": "#818cf8",
            "title": f"Strongest biomarker link: {tp['pair']} (r = {tp['r']:+.3f})",
            "body":  (f"These two markers move {direction} across {tp['n']} blood draws — "
                      f"{'extremely' if abs(tp['r']) >= 0.85 else 'highly'} correlated."),
        })

    chol = next((r for r in biomarker_matrix if r["name"] == "Cholesterol"), None)
    if chol and chol.get("r_age") is not None and abs(chol["r_age"]) >= 0.4:
        r = chol["r_age"]
        insights.append({
            "emoji": "🫀", "color": "#ef4444" if r > 0 else "#22c55e",
            "title": f"Cholesterol trend with age (r = {r:+.2f})",
            "body":  ("Total Cholesterol has trended upward with age. "
                      "Lifestyle interventions (diet, cardio) are worth discussing with a doctor."
                      if r > 0 else
                      "Good news — Cholesterol has trended downward with age, possibly reflecting "
                      "improved diet or fitness."),
        })

    return {
        "biomarker_matrix": biomarker_matrix,
        "top_correlations": top_corrs,
        "scatter_pairs":    scatter_pairs,
        "timeline":         timeline,
        "life_events":      LIFE_EVENTS,
        "insights":         insights,
        "generated":        date.today().isoformat(),
        "n_draws":          len(all_dates),
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def build_html(payload: dict) -> str:
    data_json = json.dumps(payload, separators=(",", ":"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Andy — Correlations &amp; Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3250;
    --text: #e2e8f0;
    --muted: #8892a4;
    --green: #22c55e;
    --yellow: #eab308;
    --red: #ef4444;
    --blue: #60a5fa;
    --accent: #818cf8;
    --radius: 12px;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px; line-height: 1.5; padding: 1rem;
  }}

  /* ---- Header ---- */
  header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 1.25rem 1.5rem; background: var(--surface);
    border-radius: var(--radius); border: 1px solid var(--border);
    margin-bottom: 1.5rem; flex-wrap: wrap; gap: 0.75rem;
  }}
  header h1 {{ font-size: 1.3rem; font-weight: 700; }}
  header p  {{ color: var(--muted); font-size: 0.85rem; margin-top: 2px; }}
  .back-link {{
    display: inline-flex; align-items: center; gap: 0.4rem;
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--accent); border-radius: 6px;
    padding: 0.35rem 0.8rem; font-size: 0.8rem; font-weight: 600;
    text-decoration: none; transition: all 0.15s;
  }}
  .back-link:hover {{ background: var(--accent); border-color: var(--accent); color: white; }}

  /* ---- Section headings ---- */
  .section {{ margin-bottom: 2.5rem; }}
  .section > h2 {{
    font-size: 1rem; font-weight: 700; color: var(--accent);
    margin-bottom: 1rem; padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
  }}
  .section > p.desc {{
    color: var(--muted); font-size: 0.82rem; margin-bottom: 1rem; line-height: 1.6;
  }}

  /* ---- Insight cards ---- */
  .insights-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 0.85rem;
  }}
  .insight-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 1rem;
  }}
  .insight-card .ic-emoji {{ font-size: 1.4rem; margin-bottom: 0.35rem; }}
  .insight-card .ic-title {{ font-size: 0.85rem; font-weight: 700; color: var(--text); margin-bottom: 0.3rem; }}
  .insight-card .ic-body  {{ font-size: 0.8rem; color: var(--muted); line-height: 1.55; }}

  /* ---- Covariate matrix table ---- */
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
  thead tr {{ border-bottom: 1px solid var(--border); color: var(--muted); text-align: left; }}
  th {{ padding: 6px 10px; font-weight: 600; white-space: nowrap; }}
  tbody tr {{ border-bottom: 1px solid rgba(46,50,80,0.45); }}
  tbody tr:nth-child(even) {{ background: rgba(255,255,255,0.02); }}
  td {{ padding: 6px 10px; vertical-align: middle; }}
  td.bm-name {{ font-weight: 600; color: var(--text); white-space: nowrap; }}
  td.n-col {{ text-align: center; color: var(--muted); }}
  .r-cell {{ min-width: 110px; }}
  .r-val {{ font-weight: 700; font-size: 0.85rem; }}
  .r-bar-track {{
    height: 4px; background: rgba(255,255,255,0.08);
    border-radius: 2px; margin-top: 3px;
  }}
  .r-bar-fill {{ height: 4px; border-radius: 2px; }}

  /* ---- Top pairwise cards ---- */
  .pair-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 0.75rem;
  }}
  .pair-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 0.85rem 1rem;
  }}
  .pair-card .pair-name {{ font-size: 0.8rem; font-weight: 700; color: var(--text); margin-bottom: 4px; }}
  .pair-card .pair-r    {{ font-size: 1.2rem; font-weight: 700; }}
  .pair-card .pair-meta {{ font-size: 0.7rem; color: var(--muted); margin-top: 3px; }}
  .pair-bar-track {{ height: 4px; background: rgba(255,255,255,0.08); border-radius: 2px; margin-top: 8px; }}
  .pair-bar-fill  {{ height: 4px; border-radius: 2px; }}

  /* ---- Chart cards ---- */
  .charts-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 1.25rem;
  }}
  .chart-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 1rem 1rem 0.75rem;
  }}
  .chart-header {{ display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 0.6rem; }}
  .chart-title {{ font-size: 0.85rem; font-weight: 700; color: var(--text); }}
  .chart-meta  {{ text-align: right; }}
  .chart-meta .r-big {{ font-size: 1.15rem; font-weight: 700; }}
  .chart-meta .n-lbl {{ font-size: 0.65rem; color: var(--muted); }}
  .chart-wrap {{ height: 160px; position: relative; }}

  .full-card {{ grid-column: 1 / -1; }}
  .timeline-wrap {{ height: 220px; position: relative; }}

  .chart-legend {{
    display: flex; gap: 0.75rem; margin-top: 0.4rem; flex-wrap: wrap;
  }}
  .legend-item {{ display: flex; align-items: center; gap: 4px; font-size: 0.65rem; color: var(--muted); }}
  .legend-swatch {{ width: 12px; height: 8px; border-radius: 2px; }}

  footer {{
    text-align: center; color: var(--muted); font-size: 0.75rem;
    padding: 1rem 0; border-top: 1px solid var(--border); margin-top: 2rem;
  }}

  @media (max-width: 600px) {{
    .charts-grid {{ grid-template-columns: 1fr; }}
    .pair-grid   {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<header>
  <div>
    <h1>🔬 Correlations &amp; Analysis</h1>
    <p>Pearson <em>r</em> · biomarkers vs age, kids, weight, VO₂max · 2013–2026</p>
  </div>
  <a href="index.html" class="back-link">← Dashboard</a>
</header>

<div id="app"></div>

<footer id="footer"></footer>

<script>
const DATA = {data_json};

// ---------------------------------------------------------------------------
// Colour helpers
// ---------------------------------------------------------------------------
function rColor(r) {{
  if (r === null || r === undefined) return 'var(--muted)';
  const a = Math.abs(r);
  if (a >= 0.7) return r > 0 ? '#22c55e' : '#ef4444';
  if (a >= 0.4) return r > 0 ? '#86efac' : '#fca5a5';
  return 'var(--muted)';
}}

function rStrength(r) {{
  if (r === null) return '';
  const a = Math.abs(r);
  if (a >= 0.7) return 'strong';
  if (a >= 0.4) return 'moderate';
  return 'weak';
}}

function rBarHtml(r) {{
  if (r === null || r === undefined) return '<span style="color:var(--muted)">—</span>';
  const pct  = Math.round(Math.abs(r) * 100);
  const col  = rColor(r);
  const dir  = r >= 0 ? '▲' : '▼';
  return `<div class="r-val" style="color:${{col}};">${{r.toFixed(3)}} ${{dir}}</div>
          <div class="r-bar-track"><div class="r-bar-fill" style="width:${{pct}}%;background:${{col}};"></div></div>`;
}}

// ---------------------------------------------------------------------------
// App root
// ---------------------------------------------------------------------------
const app = document.getElementById('app');

// ---------------------------------------------------------------------------
// Section helper
// ---------------------------------------------------------------------------
function makeSection(title, descHTML) {{
  const sec = document.createElement('div');
  sec.className = 'section';
  const h2 = document.createElement('h2');
  h2.textContent = title;
  sec.appendChild(h2);
  if (descHTML) {{
    const p = document.createElement('p');
    p.className = 'desc';
    p.innerHTML = descHTML;
    sec.appendChild(p);
  }}
  return sec;
}}

// ---------------------------------------------------------------------------
// 1. Insights
// ---------------------------------------------------------------------------
(function() {{
  const sec = makeSection('Key Insights', null);
  const grid = document.createElement('div');
  grid.className = 'insights-grid';
  (DATA.insights || []).forEach(ins => {{
    const card = document.createElement('div');
    card.className = 'insight-card';
    card.style.borderLeft = `3px solid ${{ins.color}}`;
    card.innerHTML = `
      <div class="ic-emoji">${{ins.emoji}}</div>
      <div class="ic-title">${{ins.title}}</div>
      <div class="ic-body">${{ins.body}}</div>
    `;
    grid.appendChild(card);
  }});
  sec.appendChild(grid);
  app.appendChild(sec);
}})();

// ---------------------------------------------------------------------------
// 2. Biomarker × covariate matrix
// ---------------------------------------------------------------------------
(function() {{
  const sec = makeSection(
    'Biomarker vs. Age / # Kids / Weight / VO₂max',
    `Pearson <em>r</em> for each biomarker against four life covariates across all blood-draw dates.
     Sorted by |r with age|. ▲ = positive correlation, ▼ = negative.`
  );

  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  const table = document.createElement('table');
  table.innerHTML = `
    <thead>
      <tr>
        <th>Biomarker</th>
        <th class="n-col">n</th>
        <th>r · Age</th>
        <th>r · # Kids</th>
        <th>r · Weight</th>
        <th>r · VO₂max</th>
      </tr>
    </thead>
  `;
  const tbody = document.createElement('tbody');
  (DATA.biomarker_matrix || []).forEach(row => {{
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="bm-name">${{row.name}}</td>
      <td class="n-col">${{row.n}}</td>
      <td class="r-cell">${{rBarHtml(row.r_age)}}</td>
      <td class="r-cell">${{rBarHtml(row.r_kids)}}</td>
      <td class="r-cell">${{row.r_weight !== null ? rBarHtml(row.r_weight) : '<span style="color:var(--muted)">—</span>'}}</td>
      <td class="r-cell">${{row.r_vo2 !== null ? rBarHtml(row.r_vo2) : '<span style="color:var(--muted)">—</span>'}}</td>
    `;
    tbody.appendChild(tr);
  }});
  table.appendChild(tbody);
  wrap.appendChild(table);
  sec.appendChild(wrap);
  app.appendChild(sec);
}})();

// ---------------------------------------------------------------------------
// 3. Top pairwise biomarker–biomarker correlations
// ---------------------------------------------------------------------------
(function() {{
  const sec = makeSection(
    'Top Biomarker–Biomarker Correlations',
    'Pearson <em>r</em> between all biomarker pairs on shared blood-draw dates (≥4 shared draws required). Top 20 by |r|.'
  );
  const grid = document.createElement('div');
  grid.className = 'pair-grid';
  (DATA.top_correlations || []).forEach(item => {{
    const a   = Math.abs(item.r);
    const col = rColor(item.r);
    const card = document.createElement('div');
    card.className = 'pair-card';
    card.style.borderLeft = `3px solid ${{col}}`;
    card.innerHTML = `
      <div class="pair-name">${{item.pair}}</div>
      <div class="pair-r" style="color:${{col}};">r = ${{item.r.toFixed(3)}}</div>
      <div class="pair-meta">${{rStrength(item.r)}} ${{item.r > 0 ? 'positive' : 'negative'}} · n=${{item.n}}</div>
      <div class="pair-bar-track"><div class="pair-bar-fill" style="width:${{Math.round(a*100)}}%;background:${{col}};"></div></div>
    `;
    grid.appendChild(card);
  }});
  sec.appendChild(grid);
  app.appendChild(sec);
}})();

// ---------------------------------------------------------------------------
// 4. Scatter plots
// ---------------------------------------------------------------------------
(function() {{
  const sec = makeSection(
    'Scatter Plots — Key Pairs',
    'Each point is one blood draw. Colour fades from blue (early) to orange (recent). Regression line shown.'
  );
  const grid = document.createElement('div');
  grid.className = 'charts-grid';

  (DATA.scatter_pairs || []).forEach(sp => {{
    const card = document.createElement('div');
    card.className = 'chart-card';

    const hdr = document.createElement('div');
    hdr.className = 'chart-header';
    const col = rColor(sp.r);
    hdr.innerHTML = `
      <div class="chart-title">${{sp.x_label}} vs ${{sp.y_label}}</div>
      <div class="chart-meta">
        <div class="r-big" style="color:${{col}};">r = ${{sp.r !== null ? sp.r.toFixed(3) : '—'}}</div>
        <div class="n-lbl">n = ${{sp.n}} draws</div>
      </div>
    `;
    card.appendChild(hdr);

    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    const canvas = document.createElement('canvas');
    wrap.appendChild(canvas);
    card.appendChild(wrap);
    grid.appendChild(card);

    // Point colours: blue (early) → orange (recent)
    const years  = sp.points.map(p => parseInt(p.date.substring(0,4)));
    const minYr  = Math.min(...years);
    const maxYr  = Math.max(...years);
    const ptColors = sp.points.map(p => {{
      const t = maxYr === minYr ? 0.5 : (parseInt(p.date.substring(0,4)) - minYr) / (maxYr - minYr);
      const r = Math.round(96  + t * (249 - 96));
      const g = Math.round(165 + t * (115 - 165));
      const b = Math.round(250 + t * (22  - 250));
      return `rgba(${{r}},${{g}},${{b}},0.9)`;
    }});

    const datasets = [{{
      label: `${{sp.x_label}} vs ${{sp.y_label}}`,
      data:  sp.points.map(p => ({{ x: p.x, y: p.y }})),
      backgroundColor:  ptColors,
      borderColor:      ptColors.map(c => c.replace('0.9','1')),
      borderWidth: 1.5,
      pointRadius: 6,
      pointHoverRadius: 8,
      type: 'scatter',
    }}];

    if (sp.reg_line) {{
      datasets.push({{
        label: 'Trend',
        data:  sp.reg_line,
        type:  'line',
        borderColor: 'rgba(129,140,248,0.6)',
        borderWidth: 1.5,
        borderDash: [4,3],
        pointRadius: 0,
        fill: false,
      }});
    }}

    new Chart(canvas, {{
      type: 'scatter',
      data: {{ datasets }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            backgroundColor: '#1a1d27', borderColor: '#2e3250', borderWidth: 1,
            titleColor: '#e2e8f0', bodyColor: '#8892a4',
            callbacks: {{
              title:  items => sp.points[items[0].dataIndex]?.date || '',
              label:  ctx  => `${{sp.x_label}}: ${{ctx.parsed.x}},  ${{sp.y_label}}: ${{ctx.parsed.y}}`,
            }},
            filter: item => item.dataset.label !== 'Trend',
          }},
        }},
        scales: {{
          x: {{
            title: {{ display: true, text: sp.x_label, color: '#8892a4', font: {{ size: 10 }} }},
            grid: {{ color: 'rgba(46,50,80,0.6)' }}, ticks: {{ color: '#8892a4', font: {{ size: 10 }} }},
          }},
          y: {{
            title: {{ display: true, text: sp.y_label, color: '#8892a4', font: {{ size: 10 }} }},
            grid: {{ color: 'rgba(46,50,80,0.6)' }}, ticks: {{ color: '#8892a4', font: {{ size: 10 }} }},
          }},
        }},
      }},
    }});
  }});

  sec.appendChild(grid);
  app.appendChild(sec);
}})();

// ---------------------------------------------------------------------------
// 5. Timeline chart (covariates over time)
// ---------------------------------------------------------------------------
(function() {{
  const sec = makeSection(
    'Life Covariates Over Blood-Draw Dates',
    'Weight and VO₂max (right axis) plotted against every blood-draw date, with # kids shaded (left axis). Life-event markers in grey.'
  );

  const card = document.createElement('div');
  card.className = 'chart-card full-card';

  const hdr = document.createElement('div');
  hdr.className = 'chart-header';
  hdr.innerHTML = `
    <div class="chart-title">Weight · VO₂max · # Kids — across ${{DATA.n_draws}} blood draws</div>
  `;
  card.appendChild(hdr);

  const wrap = document.createElement('div');
  wrap.className = 'timeline-wrap';
  const canvas = document.createElement('canvas');
  wrap.appendChild(canvas);
  card.appendChild(wrap);

  const leg = document.createElement('div');
  leg.className = 'chart-legend';
  leg.innerHTML = `
    <div class="legend-item"><div class="legend-swatch" style="background:#a855f7"></div># Kids (left)</div>
    <div class="legend-item"><div class="legend-swatch" style="background:#60a5fa"></div>Weight lbs (right)</div>
    <div class="legend-item"><div class="legend-swatch" style="background:#22c55e"></div>VO₂max ml/kg/min (right)</div>
  `;
  card.appendChild(leg);

  const grid = document.createElement('div');
  grid.className = 'charts-grid';
  grid.style.gridTemplateColumns = '1fr';
  grid.appendChild(card);
  sec.appendChild(grid);
  app.appendChild(sec);

  const tl     = DATA.timeline || [];
  const labels = tl.map(r => r.date);

  // Vertical annotation lines for life events
  const lifeEventPlugin = {{
    id: 'lifeEvents',
    afterDraw(chart) {{
      const {{ ctx, chartArea: {{ top, bottom }}, scales: {{ x }} }} = chart;
      if (!x) return;
      (DATA.life_events || []).forEach(ev => {{
        const idx = labels.indexOf(ev.date);
        if (idx < 0) return;
        const xPx = x.getPixelForValue(idx);
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(xPx, top);
        ctx.lineTo(xPx, bottom);
        ctx.strokeStyle = 'rgba(234,179,8,0.45)';
        ctx.lineWidth   = 1.5;
        ctx.setLineDash([4,3]);
        ctx.stroke();
        ctx.fillStyle   = '#eab308';
        ctx.font        = '10px sans-serif';
        ctx.textAlign   = 'center';
        ctx.fillText(ev.label, xPx, top - 4);
        ctx.restore();
      }});
    }},
  }};

  new Chart(canvas, {{
    type: 'line',
    plugins: [lifeEventPlugin],
    data: {{
      labels,
      datasets: [
        {{
          label: '# Kids',
          data:  tl.map(r => r.kids),
          borderColor: '#a855f7', backgroundColor: 'rgba(168,85,247,0.12)',
          borderWidth: 2.5, pointRadius: 3, tension: 0.1, fill: true, yAxisID: 'y',
        }},
        {{
          label: 'Weight (lbs)',
          data:  tl.map(r => r.weight),
          borderColor: '#60a5fa', backgroundColor: 'transparent',
          borderWidth: 2, pointRadius: 3, tension: 0.3, fill: false,
          yAxisID: 'y2', spanGaps: true,
        }},
        {{
          label: 'VO₂max',
          data:  tl.map(r => r.vo2max),
          borderColor: '#22c55e', backgroundColor: 'transparent',
          borderWidth: 2.5, pointRadius: 6, tension: 0.3, fill: false,
          yAxisID: 'y2', spanGaps: true,
        }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1a1d27', borderColor: '#2e3250', borderWidth: 1,
          titleColor: '#e2e8f0', bodyColor: '#8892a4',
        }},
      }},
      scales: {{
        x: {{
          grid: {{ color: 'rgba(46,50,80,0.6)' }},
          ticks: {{ color: '#8892a4', maxRotation: 45, maxTicksLimit: 20, font: {{ size: 9 }} }},
        }},
        y: {{
          title: {{ display: true, text: '# Kids', color: '#a855f7', font: {{ size: 10 }} }},
          min: 0, max: 4, stepSize: 1,
          grid: {{ color: 'rgba(46,50,80,0.6)' }},
          ticks: {{ color: '#8892a4', font: {{ size: 10 }}, stepSize: 1 }},
        }},
        y2: {{
          position: 'right',
          title: {{ display: true, text: 'Weight / VO₂max', color: '#60a5fa', font: {{ size: 10 }} }},
          grid: {{ display: false }},
          ticks: {{ color: '#8892a4', font: {{ size: 10 }} }},
        }},
      }},
    }},
  }});
}})();

// ---------------------------------------------------------------------------
// Footer
// ---------------------------------------------------------------------------
document.getElementById('footer').textContent =
  `Generated ${{DATA.generated}} · ${{DATA.n_draws}} blood draws · ${{(DATA.biomarker_matrix||[]).length}} biomarkers analysed`;
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Loading bloodwork from {BLOODWORK_FILE}...")
    measurements, ref_ranges = load_bloodwork(BLOODWORK_FILE)
    print(f"  {len(measurements)} biomarkers")

    print(f"Loading fitness from {FITNESS_FILE}...")
    fitness = load_fitness(FITNESS_FILE)

    print("Building correlation payload...")
    payload = build_payload(measurements, fitness)
    print(f"  {len(payload['biomarker_matrix'])} biomarkers × 4 covariates")
    print(f"  {len(payload['top_correlations'])} top pairwise correlations")
    print(f"  {len(payload['scatter_pairs'])} scatter pairs")
    print(f"  {len(payload['timeline'])} blood-draw dates in timeline")
    print(f"  {len(payload['insights'])} insights generated")

    print(f"Writing {HTML_FILE}...")
    html = build_html(payload)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(HTML_FILE)
    print(f"  Done! {HTML_FILE} ({size:,} bytes)")


if __name__ == "__main__":
    main()
