#!/usr/bin/env python3
"""
Generate static index.html dashboard from bloodwork.db.
"""
import json
import sqlite3
import os
from datetime import date

DB_FILE = os.path.join(os.path.dirname(__file__), "bloodwork.db")
HTML_FILE = os.path.join(os.path.dirname(__file__), "index.html")

BIRTH_DATE = date(1989, 7, 25)

# Dashboard panels: (panel_name, [biomarker_list])
PANELS = [
    ("Metabolic", ["Glucose", "HbA1c", "eGFR", "Creatinine", "BUN", "Albumin"]),
    ("Lipids", ["Cholesterol", "LDL", "HDL", "Triglycerides", "VLDL", "Apolipoprotein B"]),
    ("Liver & Enzymes", ["AST", "ALT", "GGT", "Alkaline Phosphatase", "Total Bilirubin"]),
    ("Blood Count", ["Hemoglobin", "Hematocrit", "RBC", "WBC", "Platelets", "MCV", "MCH", "MCHC", "RDW"]),
    ("Hormones", ["Testosterone", "Free Testosterone", "SHBG", "Cortisol", "Estradiol", "DHEA-S", "LH", "FSH", "IGF-1", "TSH"]),
    ("Inflammation & Cardiovascular", ["hsCRP", "Homocysteine", "Lipoprotein(a)", "Cardiac Risk Ratio"]),
    ("Vitamins & Minerals", ["Vitamin D", "Vitamin B12", "Folate", "Ferritin", "Iron", "Magnesium", "Calcium", "TIBC", "Transferrin Saturation"]),
    ("Electrolytes", ["Sodium", "Potassium", "Chloride", "CO2", "Anion Gap"]),
]

# Key biomarkers for the top summary cards
KEY_BIOMARKERS = [
    "Glucose", "HbA1c", "Cholesterol", "LDL", "HDL", "Triglycerides",
    "Testosterone", "Vitamin D", "Ferritin", "Hemoglobin", "hsCRP",
    "AST", "ALT", "Cortisol", "Free Testosterone",
]


def load_data(db_path: str) -> tuple[dict, dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Load all measurements
    cur = conn.execute(
        "SELECT date, biomarker, value, unit FROM measurements ORDER BY date"
    )
    measurements = {}
    for row in cur:
        b = row["biomarker"]
        if b not in measurements:
            measurements[b] = []
        measurements[b].append({
            "date": row["date"],
            "value": row["value"],
            "unit": row["unit"],
        })

    # Load reference ranges
    cur = conn.execute("SELECT * FROM reference_ranges")
    ref_ranges = {}
    for row in cur:
        ref_ranges[row["biomarker"]] = {
            "low": row["low"],
            "high": row["high"],
            "optimal_low": row["optimal_low"],
            "optimal_high": row["optimal_high"],
            "unit": row["unit"],
        }

    conn.close()
    return measurements, ref_ranges


def get_status(value: float, ref: dict | None) -> str:
    """Return 'optimal', 'normal', or 'out_of_range'."""
    if not ref:
        return "unknown"

    lo = ref.get("low")
    hi = ref.get("high")
    olo = ref.get("optimal_low")
    ohi = ref.get("optimal_high")

    # Check out of range first
    if lo is not None and value < lo:
        return "out_of_range"
    if hi is not None and value > hi:
        return "out_of_range"

    # Check optimal
    opt_lo_ok = (olo is None) or (value >= olo)
    opt_hi_ok = (ohi is None) or (value <= ohi)
    if opt_lo_ok and opt_hi_ok:
        return "optimal"

    return "normal"


def get_trend(data_points: list) -> str:
    """Return '↑', '↓', or '→' based on last two values."""
    if len(data_points) < 2:
        return "→"
    prev = data_points[-2]["value"]
    last = data_points[-1]["value"]
    diff = abs(last - prev)
    pct = diff / prev if prev != 0 else 0
    if pct < 0.03:
        return "→"
    return "↑" if last > prev else "↓"


def build_html(measurements: dict, ref_ranges: dict) -> str:
    today = date.today()
    age = today.year - BIRTH_DATE.year - (
        (today.month, today.day) < (BIRTH_DATE.month, BIRTH_DATE.day)
    )

    # Build summary card data
    cards = []
    for bm in KEY_BIOMARKERS:
        data = measurements.get(bm)
        if not data:
            continue
        latest = data[-1]
        ref = ref_ranges.get(bm)
        status = get_status(latest["value"], ref)
        trend = get_trend(data)
        cards.append({
            "name": bm,
            "value": latest["value"],
            "unit": latest["unit"],
            "date": latest["date"],
            "status": status,
            "trend": trend,
        })

    # Build panel data
    panels_data = []
    for panel_name, biomarkers in PANELS:
        panel_charts = []
        for bm in biomarkers:
            data = measurements.get(bm)
            if not data:
                continue
            ref = ref_ranges.get(bm)
            latest = data[-1]
            status = get_status(latest["value"], ref)
            trend = get_trend(data)
            panel_charts.append({
                "name": bm,
                "data": data,
                "ref": ref,
                "latest_value": latest["value"],
                "latest_unit": latest["unit"],
                "latest_date": latest["date"],
                "status": status,
                "trend": trend,
            })
        if panel_charts:
            panels_data.append({"name": panel_name, "charts": panel_charts})

    # JSON payload for embedding
    payload = json.dumps({
        "cards": cards,
        "panels": panels_data,
        "generated": today.isoformat(),
        "age": age,
    }, separators=(',', ':'))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Andy's Bloodwork Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3250;
    --text: #e2e8f0;
    --text-muted: #8892a4;
    --green: #22c55e;
    --yellow: #eab308;
    --red: #ef4444;
    --blue: #60a5fa;
    --accent: #818cf8;
    --card-radius: 12px;
    --gap: 1rem;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    padding: 1rem;
  }}

  header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1.25rem 1.5rem;
    background: var(--surface);
    border-radius: var(--card-radius);
    border: 1px solid var(--border);
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
    gap: 0.75rem;
  }}

  .header-title h1 {{
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--text);
  }}

  .header-title p {{
    color: var(--text-muted);
    font-size: 0.85rem;
    margin-top: 2px;
  }}

  .header-meta {{
    display: flex;
    gap: 1.5rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }}

  .header-meta span strong {{ color: var(--text); }}

  /* Summary cards */
  .summary-section {{ margin-bottom: 2rem; }}
  .summary-section h2 {{ font-size: 1rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.75rem; }}

  .cards-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: var(--gap);
  }}

  .bm-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: 1rem;
    position: relative;
    transition: transform 0.15s, box-shadow 0.15s;
    cursor: default;
  }}

  .bm-card:hover {{
    transform: translateY(-2px);
    box-shadow: 0 4px 16px rgba(0,0,0,0.3);
  }}

  .bm-card.optimal {{ border-left: 3px solid var(--green); }}
  .bm-card.normal {{ border-left: 3px solid var(--yellow); }}
  .bm-card.out_of_range {{ border-left: 3px solid var(--red); }}
  .bm-card.unknown {{ border-left: 3px solid var(--text-muted); }}

  .bm-card .bm-name {{
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 0.4rem;
  }}

  .bm-card .bm-value {{
    font-size: 1.6rem;
    font-weight: 700;
    line-height: 1;
  }}

  .bm-card.optimal .bm-value {{ color: var(--green); }}
  .bm-card.normal .bm-value {{ color: var(--yellow); }}
  .bm-card.out_of_range .bm-value {{ color: var(--red); }}
  .bm-card.unknown .bm-value {{ color: var(--text); }}

  .bm-card .bm-unit {{
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-top: 2px;
  }}

  .bm-card .bm-trend {{
    position: absolute;
    top: 0.75rem;
    right: 0.9rem;
    font-size: 1.1rem;
    opacity: 0.7;
  }}

  .bm-card .bm-date {{
    font-size: 0.7rem;
    color: var(--text-muted);
    margin-top: 0.4rem;
  }}

  .status-dot {{
    display: inline-block;
    width: 6px; height: 6px;
    border-radius: 50%;
    margin-right: 4px;
    vertical-align: middle;
  }}
  .dot-optimal {{ background: var(--green); }}
  .dot-normal {{ background: var(--yellow); }}
  .dot-out {{ background: var(--red); }}

  /* Panels */
  .panel {{ margin-bottom: 2.5rem; }}

  .panel h2 {{
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--accent);
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
  }}

  .charts-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 1.25rem;
  }}

  .chart-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: 1rem 1rem 0.75rem;
  }}

  .chart-card .chart-header {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 0.6rem;
  }}

  .chart-card .chart-title {{
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--text);
  }}

  .chart-card .chart-latest {{
    text-align: right;
  }}

  .chart-card .chart-latest .val {{
    font-size: 1.15rem;
    font-weight: 700;
  }}

  .chart-card .chart-latest.optimal .val {{ color: var(--green); }}
  .chart-card .chart-latest.normal .val {{ color: var(--yellow); }}
  .chart-card .chart-latest.out_of_range .val {{ color: var(--red); }}
  .chart-card .chart-latest.unknown .val {{ color: var(--text); }}

  .chart-card .chart-latest .val-unit {{
    font-size: 0.7rem;
    color: var(--text-muted);
  }}

  .chart-card .chart-latest .val-date {{
    font-size: 0.65rem;
    color: var(--text-muted);
  }}

  .chart-wrap {{
    height: 140px;
    position: relative;
  }}

  /* Legend for chart bands */
  .chart-legend {{
    display: flex;
    gap: 0.75rem;
    margin-top: 0.4rem;
    flex-wrap: wrap;
  }}

  .legend-item {{
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 0.65rem;
    color: var(--text-muted);
  }}

  .legend-swatch {{
    width: 12px; height: 8px;
    border-radius: 2px;
  }}

  /* Nav tabs */
  .tabs {{
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
  }}

  .tab-btn {{
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text-muted);
    border-radius: 6px;
    padding: 0.4rem 0.85rem;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all 0.15s;
  }}

  .tab-btn:hover, .tab-btn.active {{
    background: var(--accent);
    border-color: var(--accent);
    color: white;
  }}

  .panel {{ display: none; }}
  .panel.active {{ display: block; }}

  footer {{
    text-align: center;
    color: var(--text-muted);
    font-size: 0.75rem;
    padding: 1rem 0;
    border-top: 1px solid var(--border);
    margin-top: 2rem;
  }}

  @media (max-width: 600px) {{
    .cards-grid {{ grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }}
    .charts-grid {{ grid-template-columns: 1fr; }}
    header {{ flex-direction: column; }}
  }}
</style>
</head>
<body>

<header>
  <div class="header-title">
    <h1>🩸 Andy's Bloodwork</h1>
    <p>Personal health dashboard — private & self-hosted</p>
  </div>
  <div class="header-meta" id="header-meta"></div>
</header>

<div class="summary-section">
  <h2>Key Biomarkers — Latest Values</h2>
  <div class="cards-grid" id="cards-grid"></div>
</div>

<div class="tabs" id="tabs"></div>

<div id="panels"></div>

<footer id="footer"></footer>

<script>
const DATA = {payload};

const STATUS_COLOR = {{ optimal: '#22c55e', normal: '#eab308', out_of_range: '#ef4444', unknown: '#8892a4' }};
const TREND_LABEL = {{ '↑': 'up', '↓': 'down', '→': 'stable' }};

// Compute age
function computeAge() {{
  const birth = new Date(1989, 6, 25); // July = month 6 (0-indexed)
  const today = new Date();
  let age = today.getFullYear() - birth.getFullYear();
  if (today < new Date(today.getFullYear(), birth.getMonth(), birth.getDate())) age--;
  return age;
}}

// Render header meta
document.getElementById('header-meta').innerHTML = `
  <span><strong>Andy Reagan</strong></span>
  <span>Age <strong>${{computeAge()}}</strong></span>
  <span>Generated <strong>${{DATA.generated}}</strong></span>
  <span>${{DATA.cards.length}} biomarkers tracked</span>
`;

// Footer
document.getElementById('footer').textContent =
  `Generated ${{DATA.generated}} · ${{DATA.panels.reduce((s,p)=>s+p.charts.length,0)}} biomarkers · private`;

// Summary cards
const cardsEl = document.getElementById('cards-grid');
DATA.cards.forEach(c => {{
  const el = document.createElement('div');
  el.className = `bm-card ${{c.status}}`;
  el.innerHTML = `
    <div class="bm-name">${{c.name}}</div>
    <div class="bm-trend">${{c.trend}}</div>
    <div class="bm-value">${{c.value}}</div>
    <div class="bm-unit">${{c.unit}}</div>
    <div class="bm-date">${{c.date}}</div>
  `;
  cardsEl.appendChild(el);
}});

// Tabs + panels
const tabsEl = document.getElementById('tabs');
const panelsEl = document.getElementById('panels');
let firstActive = true;

DATA.panels.forEach((panel, pi) => {{
  // Tab button
  const btn = document.createElement('button');
  btn.className = 'tab-btn' + (firstActive ? ' active' : '');
  btn.textContent = panel.name;
  btn.dataset.panel = pi;
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('panel-' + pi).classList.add('active');
  }});
  tabsEl.appendChild(btn);

  // Panel div
  const panelDiv = document.createElement('div');
  panelDiv.className = 'panel' + (firstActive ? ' active' : '');
  panelDiv.id = 'panel-' + pi;

  const h2 = document.createElement('h2');
  h2.textContent = panel.name;
  panelDiv.appendChild(h2);

  const grid = document.createElement('div');
  grid.className = 'charts-grid';
  panelDiv.appendChild(grid);

  panel.charts.forEach(bm => {{
    const card = document.createElement('div');
    card.className = 'chart-card';

    const header = document.createElement('div');
    header.className = 'chart-header';

    const titleEl = document.createElement('div');
    titleEl.className = 'chart-title';
    titleEl.textContent = bm.name;

    const latestEl = document.createElement('div');
    latestEl.className = `chart-latest ${{bm.status}}`;
    latestEl.innerHTML = `
      <div class="val">${{bm.latest_value}} <span class="val-unit">${{bm.latest_unit}}</span> ${{bm.trend}}</div>
      <div class="val-date">${{bm.latest_date}}</div>
    `;

    header.appendChild(titleEl);
    header.appendChild(latestEl);
    card.appendChild(header);

    // Chart canvas
    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    const canvas = document.createElement('canvas');
    wrap.appendChild(canvas);
    card.appendChild(wrap);

    // Legend
    if (bm.ref) {{
      const leg = document.createElement('div');
      leg.className = 'chart-legend';
      if (bm.ref.optimal_low !== null || bm.ref.optimal_high !== null) {{
        leg.innerHTML += `<div class="legend-item"><div class="legend-swatch" style="background:rgba(99,102,241,0.35)"></div>Optimal</div>`;
      }}
      if (bm.ref.low !== null || bm.ref.high !== null) {{
        leg.innerHTML += `<div class="legend-item"><div class="legend-swatch" style="background:rgba(99,102,241,0.15)"></div>Normal</div>`;
      }}
      card.appendChild(leg);
    }}

    grid.appendChild(card);

    // Draw chart
    renderChart(canvas, bm);
  }});

  panelsEl.appendChild(panelDiv);
  firstActive = false;
}});

function renderChart(canvas, bm) {{
  const labels = bm.data.map(d => d.date);
  const values = bm.data.map(d => d.value);
  const ref = bm.ref;

  // Determine Y range
  const allVals = [...values];
  if (ref) {{
    if (ref.low !== null) allVals.push(ref.low);
    if (ref.high !== null) allVals.push(ref.high);
    if (ref.optimal_low !== null) allVals.push(ref.optimal_low);
    if (ref.optimal_high !== null) allVals.push(ref.optimal_high);
  }}
  const minVal = Math.min(...allVals);
  const maxVal = Math.max(...allVals);
  const pad = (maxVal - minVal) * 0.2 || 1;
  const yMin = Math.max(0, minVal - pad);
  const yMax = maxVal + pad;

  // Build annotation bands via dataset trick (fill between)
  const datasets = [];

  // Normal range band (if exists)
  if (ref && (ref.low !== null || ref.high !== null)) {{
    const lo = ref.low !== null ? ref.low : yMin;
    const hi = ref.high !== null ? ref.high : yMax;
    datasets.push({{
      label: '_normal_hi',
      data: labels.map(() => hi),
      borderWidth: 0,
      pointRadius: 0,
      fill: '+1',
      backgroundColor: 'rgba(99,102,241,0.10)',
      tension: 0,
      order: 3,
    }});
    datasets.push({{
      label: '_normal_lo',
      data: labels.map(() => lo),
      borderWidth: 0,
      pointRadius: 0,
      fill: false,
      backgroundColor: 'transparent',
      tension: 0,
      order: 3,
    }});
  }}

  // Optimal range band
  if (ref && (ref.optimal_low !== null || ref.optimal_high !== null)) {{
    const olo = ref.optimal_low !== null ? ref.optimal_low : yMin;
    const ohi = ref.optimal_high !== null ? ref.optimal_high : yMax;
    datasets.push({{
      label: '_opt_hi',
      data: labels.map(() => ohi),
      borderWidth: 0,
      pointRadius: 0,
      fill: '+1',
      backgroundColor: 'rgba(99,102,241,0.25)',
      tension: 0,
      order: 2,
    }});
    datasets.push({{
      label: '_opt_lo',
      data: labels.map(() => olo),
      borderWidth: 0,
      pointRadius: 0,
      fill: false,
      backgroundColor: 'transparent',
      tension: 0,
      order: 2,
    }});
  }}

  // Actual values line
  const pointColors = values.map(v => STATUS_COLOR[getStatus(v, ref)]);
  datasets.push({{
    label: bm.name,
    data: values,
    borderColor: '#818cf8',
    backgroundColor: pointColors,
    borderWidth: 2,
    pointRadius: 5,
    pointHoverRadius: 7,
    pointBorderColor: '#818cf8',
    pointBorderWidth: 1.5,
    tension: 0.3,
    fill: false,
    order: 1,
  }});

  new Chart(canvas, {{
    type: 'line',
    data: {{ labels, datasets }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1a1d27',
          borderColor: '#2e3250',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#8892a4',
          callbacks: {{
            label: ctx => {{
              if (ctx.dataset.label.startsWith('_')) return null;
              return ` ${{ctx.parsed.y}} ${{bm.latest_unit}}`;
            }},
          }},
          filter: item => !item.dataset.label.startsWith('_'),
        }},
      }},
      scales: {{
        x: {{
          grid: {{ color: 'rgba(46,50,80,0.6)' }},
          ticks: {{ color: '#8892a4', maxRotation: 45, font: {{ size: 10 }} }},
        }},
        y: {{
          min: yMin,
          max: yMax,
          grid: {{ color: 'rgba(46,50,80,0.6)' }},
          ticks: {{ color: '#8892a4', font: {{ size: 10 }} }},
        }},
      }},
    }},
  }});
}}

function getStatus(value, ref) {{
  if (!ref) return 'unknown';
  if (ref.low !== null && value < ref.low) return 'out_of_range';
  if (ref.high !== null && value > ref.high) return 'out_of_range';
  const optOk = (ref.optimal_low === null || value >= ref.optimal_low) &&
                (ref.optimal_high === null || value <= ref.optimal_high);
  return optOk ? 'optimal' : 'normal';
}}
</script>
</body>
</html>
"""
    return html


def main():
    print(f"Loading data from {DB_FILE}...")
    measurements, ref_ranges = load_data(DB_FILE)

    print(f"  {len(measurements)} biomarkers loaded")
    print(f"Generating {HTML_FILE}...")

    html = build_html(measurements, ref_ranges)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    size = os.path.getsize(HTML_FILE)
    print(f"  Done! {HTML_FILE} ({size:,} bytes)")


if __name__ == "__main__":
    main()
