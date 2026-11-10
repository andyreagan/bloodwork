#!/usr/bin/env python3
"""
Generate static index.html dashboard from bloodwork_data.yaml.
"""
import json
import os
import sqlite3
from datetime import date

import yaml

import shell
from ranges import resolve_active

BLOODWORK_FILE = os.path.join(os.path.dirname(__file__), "bloodwork_data.yaml")
HTML_FILE = os.path.join(os.path.dirname(__file__), "index.html")
FITNESS_FILE = os.path.join(os.path.dirname(__file__), "fitness_data.yaml")
STRAVA_DB = os.path.expanduser("~/projects/2026/strava-database/strava.db")

BIRTH_YEAR = 1989

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


def load_data(yaml_path: str) -> tuple[dict, dict]:
    with open(yaml_path, "r", encoding="utf-8") as f:
        bw = yaml.safe_load(f)

    ref_ranges = resolve_active(bw["reference_ranges"])
    draws = bw["draws"]

    # Build measurements dict: {biomarker: [{date, value, unit}, ...]}
    measurements = {}
    for draw in draws:
        for biomarker, m in draw["measurements"].items():
            if biomarker not in measurements:
                measurements[biomarker] = []
            measurements[biomarker].append({
                "date": draw["date"],
                "value": m["value"],
                "unit": m["unit"],
            })

    # Sort each biomarker's measurements by date
    for k in measurements:
        measurements[k].sort(key=lambda x: x["date"])

    return measurements, ref_ranges


def load_fitness_data(yaml_path: str) -> dict:
    """Load fitness metrics from YAML file."""
    if not os.path.exists(yaml_path):
        return {}
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # Normalize: ensure None for empty keys
    for key in list(data.keys()):
        if data[key] is None:
            data[key] = []
    return data


def load_strava_data(db_path: str = STRAVA_DB) -> list[dict]:
    """Load weekly training volume from Strava SQLite DB.

    Returns a list of week dicts sorted by week ascending, only including
    weeks with total_hours > 0.  Returns empty list if db_path doesn't exist.
    """
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # strftime('%W') gives 00-53 (Monday-based ISO-ish week).
    # We reconstruct the ISO week label as YYYY-Www.
    query = """
    SELECT
        strftime('%Y', start_date_local) AS yr,
        strftime('%W', start_date_local) AS wk,
        sport_type,
        type,
        SUM(moving_time_s)   AS total_time_s,
        SUM(distance_m)      AS total_dist_m
    FROM activities
    WHERE start_date_local IS NOT NULL
    GROUP BY yr, wk, sport_type, type
    ORDER BY yr, wk
    """
    cur.execute(query)
    rows = cur.fetchall()
    conn.close()

    # Aggregate into week -> group buckets
    from collections import defaultdict
    weeks: dict[str, dict[str, float]] = defaultdict(lambda: {
        "run_hours": 0.0,
        "run_miles": 0.0,
        "ride_hours": 0.0,
        "swim_hours": 0.0,
        "strength_hours": 0.0,
        "other_hours": 0.0,
    })

    for row in rows:
        yr = row["yr"]
        wk = row["wk"]
        week_key = f"{yr}-W{int(wk):02d}"
        sport = row["sport_type"] or ""
        typ   = row["type"] or ""
        hours = (row["total_time_s"] or 0) / 3600.0
        dist_m = row["total_dist_m"] or 0.0

        # Classify
        if sport in ("Run", "TrailRun", "VirtualRun") or typ == "Run":
            weeks[week_key]["run_hours"] += hours
            weeks[week_key]["run_miles"] += dist_m * 0.000621371
        elif sport in ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide", "Velomobile") or typ == "Ride":
            weeks[week_key]["ride_hours"] += hours
        elif sport == "Swim" or typ == "Swim":
            weeks[week_key]["swim_hours"] += hours
        elif sport in ("WeightTraining", "Crossfit", "Workout") or typ in ("WeightTraining", "Workout"):
            weeks[week_key]["strength_hours"] += hours
        else:
            weeks[week_key]["other_hours"] += hours

    # Build sorted list, compute totals, apply rounding, filter zeros
    result = []
    for week_key in sorted(weeks.keys()):
        w = weeks[week_key]
        total = w["run_hours"] + w["ride_hours"] + w["swim_hours"] + w["strength_hours"] + w["other_hours"]
        if total <= 0:
            continue
        result.append({
            "week":           week_key,
            "run_miles":      round(w["run_miles"], 1),
            "run_hours":      round(w["run_hours"], 2),
            "ride_hours":     round(w["ride_hours"], 2),
            "swim_hours":     round(w["swim_hours"], 2),
            "strength_hours": round(w["strength_hours"], 2),
            "other_hours":    round(w["other_hours"], 2),
            "total_hours":    round(total, 2),
        })

    return result


def fmt_seconds(secs: int) -> str:
    """Convert seconds to H:MM:SS or MM:SS display string."""
    secs = int(secs)
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


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


def build_fitness_panel(fitness: dict) -> dict:
    """Build the fitness panel data structure for embedding in JSON."""
    cards = []
    charts = []
    placeholder_metrics = []

    # ---- VO2max ----
    vo2 = [e for e in fitness.get("vo2max", []) if e]
    if vo2:
        latest_vo2 = vo2[-1]
        v = latest_vo2["value"]
        if v > 58:
            status = "optimal"
        elif v >= 52:
            status = "normal"
        else:
            status = "unknown"
        cards.append({
            "name": "VO₂max",
            "value": v,
            "unit": "ml/kg/min",
            "date": latest_vo2["date"],
            "status": status,
            "source": latest_vo2.get("source", ""),
        })
        charts.append({
            "id": "vo2max",
            "title": "VO₂max",
            "unit": "ml/kg/min",
            "data": [{"date": e["date"], "value": e["value"], "source": e.get("source", "")} for e in vo2],
            "type": "vo2max",
            "latest_value": v,
            "latest_date": latest_vo2["date"],
            "status": status,
        })

    # ---- Weight ----
    wt = [e for e in fitness.get("weight_lbs", []) if e]
    if wt:
        latest_wt = wt[-1]
        cards.append({
            "name": "Weight",
            "value": latest_wt["value"],
            "unit": "lbs",
            "date": latest_wt["date"],
            "status": "unknown",
            "source": latest_wt.get("source", ""),
        })
        charts.append({
            "id": "weight_lbs",
            "title": "Weight",
            "unit": "lbs",
            "data": [{"date": e["date"], "value": e["value"], "source": e.get("source", "")} for e in wt],
            "type": "line",
            "latest_value": latest_wt["value"],
            "latest_date": latest_wt["date"],
            "status": "unknown",
        })

    # ---- FTP ----
    ftp = [e for e in fitness.get("ftp_watts", []) if e]
    if ftp:
        latest = ftp[-1]
        charts.append({
            "id": "ftp_watts",
            "title": "Cycling FTP",
            "unit": "watts",
            "data": [{"date": e["date"], "value": e["value"], "source": e.get("source", "")} for e in ftp],
            "type": "line",
            "latest_value": latest["value"],
            "latest_date": latest["date"],
            "status": "unknown",
        })
    else:
        placeholder_metrics.append("Cycling FTP (ftp_watts)")

    # ---- Marathon times ----
    mar = [e for e in fitness.get("marathon_times", []) if e]
    if mar:
        latest = mar[-1]
        charts.append({
            "id": "marathon_times",
            "title": "Marathon Time",
            "unit": "time",
            "data": [{"date": e["date"], "value": e["time_seconds"], "label": fmt_seconds(e["time_seconds"]), "race": e.get("race", "")} for e in mar],
            "type": "time",
            "latest_value": latest["time_seconds"],
            "latest_label": fmt_seconds(latest["time_seconds"]),
            "latest_date": latest["date"],
            "status": "unknown",
        })
    else:
        placeholder_metrics.append("Marathon Time (marathon_times)")

    # ---- 5K times ----
    fivek = [e for e in fitness.get("5k_times", []) if e]
    if fivek:
        latest = fivek[-1]
        charts.append({
            "id": "5k_times",
            "title": "5K Time",
            "unit": "time",
            "data": [{"date": e["date"], "value": e["time_seconds"], "label": fmt_seconds(e["time_seconds"]), "race": e.get("race", "")} for e in fivek],
            "type": "time",
            "latest_value": latest["time_seconds"],
            "latest_label": fmt_seconds(latest["time_seconds"]),
            "latest_date": latest["date"],
            "status": "unknown",
        })
    else:
        placeholder_metrics.append("5K Time (5k_times)")

    # ---- Strength lifts ----
    for key, label in [("squat_1rm_lbs", "Squat 1RM"), ("deadlift_1rm_lbs", "Deadlift 1RM"), ("bench_1rm_lbs", "Bench 1RM"), ("overhead_press_1rm_lbs", "OHP 1RM"), ("barbell_row_1rm_lbs", "Barbell Row 1RM")]:
        entries = [e for e in fitness.get(key, []) if e]
        if entries:
            latest = entries[-1]
            charts.append({
                "id": key,
                "title": label,
                "unit": "lbs",
                "data": [{"date": e["date"], "value": e["value"], "source": e.get("source", "")} for e in entries],
                "type": "line",
                "latest_value": latest["value"],
                "latest_date": latest["date"],
                "status": "unknown",
            })
        else:
            placeholder_metrics.append(f"{label} ({key})")

    return {
        "cards": cards,
        "charts": charts,
        "placeholders": placeholder_metrics,
    }


def build_html(measurements: dict, ref_ranges: dict, fitness: dict | None = None, strava_weeks: list | None = None) -> str:
    today = date.today()
    age = today.year - BIRTH_YEAR

    # Build summary card data
    cards = []

    # VO2max as first top card (from fitness data)
    fitness_data = fitness or {}
    vo2_entries = [e for e in fitness_data.get("vo2max", []) if e]
    if vo2_entries:
        latest_vo2 = vo2_entries[-1]
        v = latest_vo2["value"]
        if v > 58:
            vo2_status = "optimal"
        elif v >= 52:
            vo2_status = "normal"
        else:
            vo2_status = "unknown"
        cards.append({
            "name": "VO₂max",
            "value": v,
            "unit": "ml/kg/min",
            "date": latest_vo2["date"],
            "status": vo2_status,
            "trend": "↑" if len(vo2_entries) >= 2 and vo2_entries[-1]["value"] > vo2_entries[-2]["value"] else ("↓" if len(vo2_entries) >= 2 and vo2_entries[-1]["value"] < vo2_entries[-2]["value"] else "→"),
            "is_fitness": True,
        })

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

    # Build fitness panel data
    fitness_panel = build_fitness_panel(fitness or {})

    # In-page sections: overview, one per biomarker panel, fitness, training.
    # Each summary tile links to the section holding that marker's chart.
    section_of = {}
    sections = [("overview", "⚡", "Key Biomarkers", "Key Biomarkers — Latest Values")]
    for i, p in enumerate(panels_data):
        sid = f"panel-{i}"
        sections.append((sid, PANEL_ICONS.get(p["name"], "🧪"),
                         PANEL_NAV_LABELS.get(p["name"], p["name"]), p["name"]))
        for ch in p["charts"]:
            section_of[ch["name"]] = sid
    perf_sections = [("fitness", "💪", "Fitness", "Fitness & Performance")]
    if strava_weeks:
        perf_sections.append(("training", "📈", "Training", "Weekly Training Volume"))
    for c in cards:
        c["section"] = "fitness" if c.get("is_fitness") else section_of.get(c["name"])

    # JSON payload for embedding
    payload = json.dumps({
        "cards": cards,
        "panels": panels_data,
        "fitness": fitness_panel,
        "strava_weeks": strava_weeks or [],
        "generated": today.isoformat(),
        "age": age,
        "titles": {sid: title for sid, _, _, title in sections + perf_sections},
    }, separators=(',', ':'))

    def nav_items(items):
        return "\n".join(
            f'    <div class="nav-item{" active" if sid == "overview" else ""}" data-section="{sid}">'
            f'<span class="icon">{icon}</span> {label}</div>'
            for sid, icon, label, _ in items
        )

    nav = f"""  <div class="nav-section">
    <div class="nav-section-title">Bloodwork</div>
{nav_items(sections)}
  </div>
  <div class="nav-section">
    <div class="nav-section-title">Performance</div>
{nav_items(perf_sections)}
  </div>"""

    section_divs = "\n".join(
        f'    <div class="section{" active" if sid == "overview" else ""}" id="section-{sid}"></div>'
        for sid, _, _, _ in sections + perf_sections
    )

    html = shell.head("Andy's Health Dashboard", today.isoformat(), PAGE_CSS) + f"""<body>

{shell.sidebar("index.html", nav)}
<div class="main">
  <div class="topbar">
    <div>
      <h1 id="section-title">Key Biomarkers — Latest Values</h1>
      <div class="subtitle">Bloodwork · Fitness · Performance — personal health dashboard</div>
    </div>
    <div class="topbar-meta" id="header-meta"></div>
  </div>

  <div class="content">
{section_divs}
  </div>
</div>

<script>
""" + PAGE_JS.replace("__DATA__", payload) + """</script>
</body>
</html>
"""
    return html


# Sidebar icon / short label per biomarker panel.
PANEL_ICONS = {
    "Metabolic": "🍬",
    "Lipids": "🧈",
    "Liver & Enzymes": "🧪",
    "Blood Count": "🩸",
    "Hormones": "🧬",
    "Inflammation & Cardiovascular": "❤️",
    "Vitamins & Minerals": "💊",
    "Electrolytes": "🧂",
}
PANEL_NAV_LABELS = {"Inflammation & Cardiovascular": "Inflammation & CV"}

# Page-local styles: only what the kit (ar.css) lacks.
PAGE_CSS = """<style>
.topbar-meta { display: flex; gap: 4px 16px; flex-wrap: wrap; font-size: 0.8rem; color: var(--muted); }
.topbar-meta strong { color: var(--text); }
.unit { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
.trend { font-size: 0.8em; font-weight: 600; color: var(--muted); }
.kpi-card[data-goto] { cursor: pointer; }
.kpi-card .badge { margin-top: 8px; }
.kpi-strip.tiles { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
/* three-up chart grid steps down to two before the kit's single-column break */
@media (max-width: 1250px) { .metric-grid.three { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 900px)  { .metric-grid.three { grid-template-columns: minmax(0, 1fr); } }
.legend.below { margin: 8px 0 0; }
.legend-item .sw.band { background: var(--good); opacity: .3; }
.legend-item .sw.dash { height: 0; width: 14px; border-top: 1px dashed var(--muted); border-radius: 0; }
.legend-item .sw.dot  { border-radius: 50%; }
.ref-sources { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); font-size: 0.72rem; }
.ref-src-row { display: flex; justify-content: space-between; gap: 12px; color: var(--muted); padding: 1px 0; }
.ref-src-row.active { color: var(--text); }
.ref-src-row.active .ref-src-name { font-weight: 600; }
.ref-src-val { font-variant-numeric: tabular-nums; text-align: right; }
.placeholder { text-align: center; color: var(--muted); font-size: 0.75rem; }
.placeholder .ph-icon { font-size: 1.5rem; }
.placeholder .ph-name { font-weight: 700; color: var(--text); font-size: 0.85rem; margin: 4px 0; }
</style>"""

# Page script.  Plain string (not an f-string): __DATA__ is replaced with the
# JSON payload.  All charts are d3 via the kit's AR.* helpers.
PAGE_JS = r"""const DATA = __DATA__;
const $ = id => document.getElementById(id);

// Status → kit status colours.  The badge text carries the same meaning, so
// status never depends on colour alone.
const STATUS = {
  optimal:      { color: 'var(--good)', kpi: 'var(--good)', cls: 'good', label: 'Optimal' },
  normal:       { color: 'var(--warn)', kpi: 'var(--warn)', cls: 'warn', label: 'Normal' },
  out_of_range: { color: 'var(--bad)',  kpi: 'var(--bad)',  cls: 'bad',  label: 'Out of range' },
  unknown:      { color: 'var(--text)', kpi: 'var(--gray)', cls: '',     label: '' },
};
const VO2_LABELS = { optimal: 'Elite', normal: 'Good' };
const TREND_LABEL = { '↑': 'up', '↓': 'down', '→': 'stable' };

const statusLabel = (status, labels) => (labels || {})[status] || STATUS[status].label;
const badge = (status, labels) => {
  const t = statusLabel(status, labels);
  return t ? `<span class="badge ${STATUS[status].cls}">${t}</span>` : '';
};
const trendHtml = t => t ? ` <span class="trend" title="${TREND_LABEL[t]}">${t}</span>` : '';
const rangeTxt = (lo, hi) => {
  if (lo == null && hi == null) return null;
  if (lo != null && hi != null) return `${lo}–${hi}`;
  return hi != null ? `≤${hi}` : `≥${lo}`;
};

function getStatus(value, ref) {
  if (!ref) return 'unknown';
  if (ref.low != null && value < ref.low) return 'out_of_range';
  if (ref.high != null && value > ref.high) return 'out_of_range';
  const optOk = (ref.optimal_low == null || value >= ref.optimal_low) &&
                (ref.optimal_high == null || value <= ref.optimal_high);
  return optOk ? 'optimal' : 'normal';
}

function computeAge() {
  const birthYear = 1989;
  const today = new Date();
  return today.getFullYear() - birthYear;
}

function fmtSeconds(secs) {
  secs = Math.round(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  return `${m}:${String(s).padStart(2,'0')}`;
}

function getISOWeek(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
}

// ── HEADER META ───────────────────────────────────────────────────────────
const biomarkerCount = DATA.panels.reduce((s, p) => s + p.charts.length, 0);
const fitnessCount = (DATA.fitness?.charts?.length || 0);
$('header-meta').innerHTML = `
  <span><strong>Andy Reagan</strong></span>
  <span>Age <strong>${computeAge()}</strong></span>
  <span><strong>${biomarkerCount}</strong> biomarkers</span>
  <span><strong>${fitnessCount}</strong> fitness metrics</span>`;

// ── NAV ───────────────────────────────────────────────────────────────────
function show(id) {
  if (!$('section-' + id)) id = 'overview';
  document.querySelectorAll('.nav-item[data-section]').forEach(n =>
    n.classList.toggle('active', n.dataset.section === id));
  document.querySelectorAll('.section').forEach(s =>
    s.classList.toggle('active', s.id === 'section-' + id));
  $('section-title').textContent = DATA.titles[id];
  if (location.hash.slice(1) !== id) history.replaceState(null, '', '#' + id);
  window.scrollTo(0, 0);
}
document.querySelectorAll('.nav-item[data-section]').forEach(n =>
  n.addEventListener('click', () => show(n.dataset.section)));
window.addEventListener('hashchange', () => show(location.hash.slice(1)));

// ── TILES ─────────────────────────────────────────────────────────────────
function tile(c, labels) {
  const s = STATUS[c.status];
  return `<div class="kpi-card" style="--kpi-color:${s.kpi}"${c.section ? ` data-goto="${c.section}"` : ''}>
    <div class="kpi-label">${c.name}</div>
    <div class="kpi-value" style="color:${s.color}">${c.value}${trendHtml(c.trend)}</div>
    <div class="kpi-sub">${c.unit} · ${c.date}</div>
    ${c.source ? `<div class="kpi-sub"><i>${c.source}</i></div>` : ''}
    ${badge(c.status, labels)}
  </div>`;
}

const STATUS_LEGEND = `<div class="legend">
  <span class="legend-item"><span class="sw dot" style="background:var(--good)"></span>Optimal</span>
  <span class="legend-item"><span class="sw dot" style="background:var(--warn)"></span>Normal</span>
  <span class="legend-item"><span class="sw dot" style="background:var(--bad)"></span>Out of range</span>
  <span class="legend-item"><span class="sw band"></span>Optimal range</span>
  <span class="legend-item"><span class="sw dash"></span>Normal range limit</span>
</div>`;

// ── OVERVIEW ──────────────────────────────────────────────────────────────
$('section-overview').innerHTML =
  `<div class="kpi-strip tiles">${DATA.cards.map(c => tile(c, c.is_fitness ? VO2_LABELS : null)).join('')}</div>`;
$('section-overview').querySelectorAll('[data-goto]').forEach(el =>
  el.addEventListener('click', () => show(el.dataset.goto)));

// ── BIOMARKER PANELS ──────────────────────────────────────────────────────
// Card shell shared by the biomarker and fitness charts.
function chartCard(grid, { title, status, labels, val, unit, trend, sub }) {
  const card = document.createElement('div');
  card.className = 'metric-card';
  card.innerHTML = `
    <div class="metric-card-header">
      <div class="metric-card-title">${title}</div>
      ${badge(status, labels)}
    </div>
    <div class="metric-card-val" style="color:${STATUS[status].color}">${val}
      <span class="unit">${unit || ''}</span>${trendHtml(trend)}</div>
    <div class="metric-card-sub">${sub || ''}</div>
    <div class="chart-area"></div>`;
  grid.appendChild(card);
  return card;
}

// The y-range where a value counts as optimal: inside the normal limits and
// inside the optimal limits, whichever of them exist.
function optimalBand(ref) {
  const los = [ref.low, ref.optimal_low].filter(v => v != null);
  const his = [ref.high, ref.optimal_high].filter(v => v != null);
  if (!los.length && !his.length) return null;
  return { lo: los.length ? Math.max(...los) : null, hi: his.length ? Math.min(...his) : null };
}

function renderChart(card, bm) {
  const labels = bm.data.map(d => d.date);
  const values = bm.data.map(d => d.value);
  const ref = bm.ref;

  // Y range covers the data and every reference limit
  const allVals = [...values];
  if (ref) [ref.low, ref.high, ref.optimal_low, ref.optimal_high].forEach(v => { if (v != null) allVals.push(v); });
  const minVal = Math.min(...allVals);
  const maxVal = Math.max(...allVals);
  const pad = (maxVal - minVal) * 0.12 || 1;

  const band = ref ? optimalBand(ref) : null;
  const refLines = ref
    ? [ref.low, ref.high].filter(v => v != null && !(band && (v === band.lo || v === band.hi))).map(y => ({ y }))
    : [];

  AR.lineChart(card.querySelector('.chart-area'), {
    x: labels, y: values, color: 'var(--accent)', area: false, height: 150,
    dots: v => STATUS[getStatus(v, ref)].kpi,
    band, refLines,
    yDomain: [Math.max(0, minVal - pad), maxVal + pad],
    tip: i => {
      const st = statusLabel(getStatus(values[i], ref));
      return `<b>${AR.fmtDate(labels[i])}</b><br>${values[i]} ${bm.data[i].unit}` +
             (st ? `<br><span class="dim">${st}</span>` : '');
    },
  });
}

DATA.panels.forEach((panel, pi) => {
  const sec = $('section-panel-' + pi);
  sec.innerHTML = STATUS_LEGEND;
  const grid = document.createElement('div');
  grid.className = 'metric-grid three';
  sec.appendChild(grid);

  panel.charts.forEach(bm => {
    const card = chartCard(grid, {
      title: bm.name, status: bm.status, val: bm.latest_value,
      unit: bm.latest_unit, trend: bm.trend, sub: bm.latest_date,
    });

    // Range legend
    if (bm.ref) {
      const opt = rangeTxt(bm.ref.optimal_low, bm.ref.optimal_high);
      const norm = rangeTxt(bm.ref.low, bm.ref.high);
      const items = [];
      if (opt) items.push(`<span class="legend-item"><span class="sw band"></span>Optimal ${opt}</span>`);
      if (norm) items.push(`<span class="legend-item"><span class="sw ${opt ? 'dash' : 'band'}"></span>Normal ${norm}</span>`);
      if (items.length) card.insertAdjacentHTML('beforeend', `<div class="legend below">${items.join('')}</div>`);
    }

    // Per-source reference-range comparison (everyone has their own ranges)
    if (bm.ref && bm.ref.sources && Object.keys(bm.ref.sources).length > 1) {
      const SRC_LABEL = { curated: 'My ranges', insidetracker: 'InsideTracker', lab: 'Lab (Quest)' };
      const fmt = (b) => {
        const opt = rangeTxt(b.optimal_low, b.optimal_high);
        const norm = rangeTxt(b.low, b.high);
        const parts = [];
        if (opt) parts.push(`opt ${opt}`);
        if (norm) parts.push(`normal ${norm}`);
        return parts.join(' · ') || '—';
      };
      const box = document.createElement('div');
      box.className = 'ref-sources';
      ['curated', 'insidetracker', 'lab'].forEach(sk => {
        const b = bm.ref.sources[sk];
        if (!b) return;
        const isActive = sk === bm.ref.active_source;
        const row = document.createElement('div');
        row.className = 'ref-src-row' + (isActive ? ' active' : '');
        const name = SRC_LABEL[sk] + (isActive ? ' ●' : '');
        row.innerHTML = `<span class="ref-src-name">${name}</span><span class="ref-src-val">${fmt(b)}</span>`;
        box.appendChild(row);
      });
      card.appendChild(box);
    }

    renderChart(card, bm);
  });
});

// ── FITNESS ───────────────────────────────────────────────────────────────
function renderFitnessChart(card, fc) {
  const labels = fc.data.map(d => d.date);
  const values = fc.data.map(d => d.value);
  const minVal = Math.min(...values);
  const maxVal = Math.max(...values);
  const isTime = fc.type === 'time';
  const opts = {
    x: labels, y: values, color: 'var(--accent)', area: false, height: 150,
    // long series (monthly weight) draw as a plain line; sparse ones get a dot per test
    dots: values.length <= 40,
    tip: i => `<b>${AR.fmtDate(labels[i])}</b><br>` +
              (isTime ? fmtSeconds(values[i]) : `${values[i]} ${fc.unit}`),
  };

  if (fc.type === 'vo2max') {
    // Reference tiers: normal 42–52, good 52–58, elite >58
    opts.yDomain = [Math.min(38, minVal - 3), Math.max(65, maxVal + 3)];
    opts.band = { lo: 58, hi: null };
    opts.refLines = [{ y: 52 }, { y: 42 }];
    opts.dots = v => v > 58 ? 'var(--good)' : v >= 52 ? 'var(--warn)' : 'var(--bad)';
    opts.tip = i => `<b>${AR.fmtDate(labels[i])}</b><br>${values[i]} ${fc.unit}<br><span class="dim">` +
                    (values[i] > 58 ? 'Elite' : values[i] >= 52 ? 'Good' : 'Normal') + '</span>';
  } else if (isTime) {
    // For race times: lower is better; no reference bands
    // Plotted in hours (or minutes for short races) so the axis ticks land
    // on round clock times; labels and tooltip still read h:mm:ss.
    const per = maxVal >= 3600 ? 3600 : 60;
    const pad = (maxVal - minVal) * 0.15 || 60;
    opts.y = values.map(v => v / per);
    opts.yDomain = [Math.max(0, minVal - pad) / per, (maxVal + pad) / per];
    opts.yFmt = v => fmtSeconds(v * per);
  } else {
    const pad = (maxVal - minVal) * 0.2 || 5;
    opts.yDomain = [Math.max(0, minVal - pad), maxVal + pad];
  }
  AR.lineChart(card.querySelector('.chart-area'), opts);
}

(function () {
  const fitness = DATA.fitness;
  const sec = $('section-fitness');
  if (!fitness || !sec) return;

  // Fitness summary cards
  if (fitness.cards && fitness.cards.length > 0) {
    sec.insertAdjacentHTML('beforeend',
      `<div class="section-heading">Latest Values</div>
       <div class="kpi-strip tiles">${fitness.cards.map(c => tile(c, c.name === 'VO₂max' ? VO2_LABELS : null)).join('')}</div>`);
  }

  const grid = document.createElement('div');
  grid.className = 'metric-grid three';
  sec.appendChild(grid);

  (fitness.charts || []).forEach(fc => {
    const isTime = fc.type === 'time';
    const card = chartCard(grid, {
      title: fc.title, status: fc.status, labels: fc.type === 'vo2max' ? VO2_LABELS : null,
      val: isTime ? fc.latest_label : fc.latest_value,
      unit: isTime ? '' : fc.unit, sub: fc.latest_date,
    });
    if (fc.type === 'vo2max') {
      card.insertAdjacentHTML('beforeend', `<div class="legend below">
        <span class="legend-item"><span class="sw dot" style="background:var(--bad)"></span>Normal (42–52)</span>
        <span class="legend-item"><span class="sw dot" style="background:var(--warn)"></span>Good (52–58)</span>
        <span class="legend-item"><span class="sw dot" style="background:var(--good)"></span>Elite (&gt;58)</span>
        <span class="legend-item"><span class="sw band"></span>Elite range</span>
        <span class="legend-item"><span class="sw dash"></span>42 / 52</span>
      </div>`);
    }
    renderFitnessChart(card, fc);
  });

  // Placeholder cards for missing metrics
  if (fitness.placeholders && fitness.placeholders.length > 0) {
    sec.insertAdjacentHTML('beforeend',
      `<div class="section-heading">Not yet tracked</div>
       <div class="metric-grid three">${fitness.placeholders.map(name => `
         <div class="metric-card placeholder">
           <div class="ph-icon">📋</div>
           <div class="ph-name">${name.split(' (')[0]}</div>
           <div>No data yet — edit <code>fitness_data.yaml</code> to add</div>
         </div>`).join('')}</div>`);
  }
})();

// ── TRAINING (Strava weekly volumes) ──────────────────────────────────────
(function () {
  const weeks = DATA.strava_weeks;
  const sec = $('section-training');
  if (!weeks || weeks.length === 0 || !sec) return;

  // Filter to last 2 years by default
  const cutoff = (() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 2);
    return d.getFullYear() + '-W' + String(getISOWeek(d)).padStart(2, '0');
  })();
  const recent = weeks.filter(w => w.week >= cutoff);
  const display = recent.length >= 20 ? recent : weeks;
  const labels = display.map(w => w.week);
  const totalRunMiles = display.reduce((s, w) => s + w.run_miles, 0);

  const SPORTS = [
    ['Run',      'run_hours',      'var(--orange)'],
    ['Ride',     'ride_hours',     'var(--blue)'],
    ['Swim',     'swim_hours',     'var(--teal)'],
    ['Strength', 'strength_hours', 'var(--purple)'],
    ['Other',    'other_hours',    'var(--gray)'],
  ];

  sec.innerHTML = `
    <div class="metric-grid wide">
      <div class="metric-card">
        <div class="metric-card-header">
          <div class="metric-card-title">Weekly Volume (hours by sport)</div>
        </div>
        <div class="metric-card-val">${display.length} <span class="unit">weeks</span></div>
        <div class="metric-card-sub">${display[0].week} – ${display[display.length - 1].week}</div>
        <div class="legend" id="vol-legend"></div>
        <div class="chart-area" id="vol-chart"></div>
      </div>
      <div class="metric-card">
        <div class="metric-card-header">
          <div class="metric-card-title">Weekly Run Miles</div>
        </div>
        <div class="metric-card-val" style="color:var(--orange)">${totalRunMiles.toFixed(0)} <span class="unit">total mi shown</span></div>
        <div class="metric-card-sub">${display[0].week} – ${display[display.length - 1].week}</div>
        <div class="chart-area" id="miles-chart"></div>
      </div>
    </div>`;

  AR.legend('#vol-legend', SPORTS.map(([name, , color]) => ({ name, color })));
  AR.barChart('#vol-chart', {
    labels, height: 220,
    stack: SPORTS.map(([name, key, color]) => ({ name, color, values: display.map(w => w[key]) })),
    yFmt: v => v + 'h',
    tip: i => `<b>${labels[i]}</b><br>` +
      SPORTS.filter(([, key]) => display[i][key] > 0).map(([name, key, color]) =>
        `${AR.swatch(color)}<span class="dim">${name}</span> ${display[i][key].toFixed(2)} h`).join('<br>') +
      `<br><span class="dim">Total</span> ${display[i].total_hours.toFixed(2)} h`,
  });

  // Week labels are strftime('%Y-W%W') (Monday-based, week 00 = days before
  // the first Monday); plot each at its Monday so skipped weeks show as gaps
  // in time rather than being squeezed out.
  const monday = wk => {
    const [y, w] = wk.split('-W').map(Number);
    const jan1 = new Date(y, 0, 1);
    return new Date(y, 0, 1 + (8 - jan1.getDay()) % 7 + (w - 1) * 7);
  };
  AR.lineChart('#miles-chart', {
    x: labels.map(monday), y: display.map(w => w.run_miles),
    color: 'var(--orange)', height: 170, zero: true,
    yFmt: v => v + ' mi',
    tip: i => `<b>${labels[i]}</b><br>${display[i].run_miles.toFixed(1)} miles`,
  });
})();

show(location.hash.slice(1) || 'overview');
"""


def main():
    print(f"Loading data from {BLOODWORK_FILE}...")
    measurements, ref_ranges = load_data(BLOODWORK_FILE)
    print(f"  {len(measurements)} biomarkers loaded")

    print(f"Loading fitness data from {FITNESS_FILE}...")
    fitness = load_fitness_data(FITNESS_FILE)
    tracked = [k for k, v in fitness.items() if v]
    print(f"  {len(tracked)} fitness metrics with data: {', '.join(tracked)}")

    print(f"Loading Strava data from {STRAVA_DB}...")
    strava_weeks = load_strava_data(STRAVA_DB)
    if strava_weeks:
        print(f"  {len(strava_weeks)} weeks loaded ({strava_weeks[0]['week']} – {strava_weeks[-1]['week']})")
    else:
        print("  Strava DB not found or empty — Training tab will be skipped")

    print(f"Generating {HTML_FILE}...")
    html = build_html(measurements, ref_ranges, fitness, strava_weeks)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    size = os.path.getsize(HTML_FILE)
    print(f"  Done! {HTML_FILE} ({size:,} bytes)")


if __name__ == "__main__":
    main()
