#!/usr/bin/env python3
"""
Generate fitness.html — unified Garmin + WHOOP + Strava comparison and
combined signal dashboard.

Three sections:
  1. Source comparison  — side-by-side Garmin vs WHOOP for overlapping
                          signals (sleep, RHR, respiration) so you can
                          see where they agree and where they diverge.
  2. Combined signals   — per-day unified series choosing the best
                          available source for each metric:
                            HRV:          WHOOP (Garmin has none)
                            Sleep hrs:    WHOOP (more consistent sensor)
                            RHR:          WHOOP (lower bias vs Garmin)
                            Steps:        Garmin (back to 2015, WHOOP has none)
                            Stress:       Garmin (unique signal)
                            Body battery: Garmin (unique signal)
                            Intensity:    Garmin moderate+vigorous mins
                            Training hrs: Strava (sport breakdown)
  3. Trends             — monthly rolling means for all combined signals,
                          with blood-draw markers.

The combined daily signal is also exported as a JSON blob so
generate_correlations.py and generate_models.py can import it directly
(currently they query the DBs themselves; this can replace that logic).
"""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from collections import defaultdict

HTML_FILE  = os.path.join(os.path.dirname(__file__), "fitness.html")

GARMIN_DB = os.path.expanduser("~/projects/2026/connect-database/garmin-database/garmin.db")
WHOOP_DB  = os.path.expanduser("~/projects/2026/whoop-database/whoop.db")
STRAVA_DB = os.path.expanduser("~/projects/2026/strava-database/strava.db")

BIRTH_YEAR = 1989

BLOOD_DRAW_DATES = [
    "2013-01-10","2018-09-27","2018-10-10","2019-01-14",
    "2021-07-22","2021-08-10","2021-10-29","2022-09-30","2022-12-23",
    "2023-01-03","2023-07-10","2023-11-02","2023-11-16",
    "2024-11-27","2025-03-06","2025-06-05","2025-12-04",
]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_garmin(db_path: str) -> dict[str, dict]:
    """Returns {date: {field: value, ...}}"""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT date,
               total_steps, total_distance_m,
               sleep_total_seconds, sleep_deep_seconds,
               sleep_rem_seconds, sleep_awake_seconds,
               sleep_score,
               resting_heart_rate,
               avg_stress_level, max_stress_level,
               body_battery_highest, body_battery_lowest,
               body_battery_charged, body_battery_drained,
               moderate_intensity_mins, vigorous_intensity_mins,
               respiration_avg,
               active_kilocalories, total_kilocalories,
               weight_kg
        FROM daily
        WHERE date >= '2015-01-01'
        ORDER BY date
    """).fetchall()
    conn.close()
    result = {}
    for r in rows:
        result[r["date"]] = dict(r)
    return result


def load_whoop(db_path: str) -> dict[str, dict]:
    """Returns {date: {field: value, ...}}"""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT date,
               recovery_score, hrv_rmssd_ms, resting_hr,
               spo2_pct, skin_temp_celsius, respiratory_rate,
               sleep_performance_pct,
               sleep_total_in_bed_ms, sleep_light_ms,
               sleep_rem_ms, sleep_slow_wave_ms, sleep_awake_ms,
               sleep_disturbances,
               strain, workout_strain, workout_count,
               kilojoules
        FROM daily
        ORDER BY date
    """).fetchall()
    conn.close()
    return {r["date"]: dict(r) for r in rows}


def load_strava_daily(db_path: str) -> dict[str, dict]:
    """Aggregate Strava activities to daily level."""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT date(start_date_local) as dt,
               SUM(moving_time_s) / 3600.0            AS total_hrs,
               SUM(CASE WHEN sport_type IN ('Run','TrailRun','VirtualRun')
                        THEN distance_m*0.000621371 ELSE 0 END) AS run_miles,
               SUM(CASE WHEN sport_type IN ('Run','TrailRun','VirtualRun')
                        THEN moving_time_s/3600.0 ELSE 0 END)   AS run_hrs,
               SUM(CASE WHEN sport_type IN ('Ride','VirtualRide','GravelRide',
                        'MountainBikeRide','EBikeRide')
                        THEN moving_time_s/3600.0 ELSE 0 END)   AS ride_hrs,
               SUM(CASE WHEN sport_type IN ('WeightTraining','Crossfit','Workout')
                        THEN moving_time_s/3600.0 ELSE 0 END)   AS strength_hrs,
               AVG(CASE WHEN average_heartrate > 0
                        THEN average_heartrate END)              AS avg_hr,
               AVG(CASE WHEN average_watts > 0 AND device_watts=1
                        THEN average_watts END)                  AS avg_watts,
               COUNT(*) AS n_activities
        FROM activities
        WHERE start_date_local IS NOT NULL
        GROUP BY dt
        ORDER BY dt
    """).fetchall()
    conn.close()
    return {r["dt"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# Build combined signal
# ---------------------------------------------------------------------------

def build_combined(garmin: dict, whoop: dict, strava: dict) -> list[dict]:
    """
    Merge all three sources into a single per-day record, choosing the
    best source for each metric.  Fills zeros/None where unavailable.
    """
    all_dates = sorted(
        set(garmin.keys()) | set(whoop.keys()) | set(strava.keys())
    )
    # Restrict to 2015+ (first Garmin steps data)
    all_dates = [d for d in all_dates if d >= "2015-01-01"]

    combined = []
    for d in all_dates:
        g = garmin.get(d, {})
        w = whoop.get(d, {})
        s = strava.get(d, {})

        def gv(key, scale=1):
            v = g.get(key)
            return round(v * scale, 3) if v is not None else None

        def wv(key, scale=1):
            v = w.get(key)
            return round(v * scale, 3) if v is not None else None

        row = {
            "date": d,
            # Steps — Garmin only
            "steps":              gv("total_steps"),
            # Sleep — prefer WHOOP, fall back to Garmin
            "sleep_hrs":          wv("sleep_total_in_bed_ms", 1/3600000) or gv("sleep_total_seconds", 1/3600),
            "sleep_rem_hrs":      wv("sleep_rem_ms", 1/3600000)          or gv("sleep_rem_seconds", 1/3600),
            "sleep_deep_hrs":     wv("sleep_slow_wave_ms", 1/3600000)    or gv("sleep_deep_seconds", 1/3600),
            "sleep_score":        wv("sleep_performance_pct")            or gv("sleep_score"),
            "sleep_src":          "whoop" if w.get("sleep_total_in_bed_ms") else ("garmin" if g.get("sleep_total_seconds") else None),
            # HRV — WHOOP only (Garmin doesn't have it in this DB)
            "hrv_ms":             wv("hrv_rmssd_ms"),
            # RHR — prefer WHOOP (more consistent wrist measurement)
            "rhr":                wv("resting_hr")                       or gv("resting_heart_rate"),
            "rhr_src":            "whoop" if w.get("resting_hr") else ("garmin" if g.get("resting_heart_rate") else None),
            # Recovery / stress
            "whoop_recovery":     wv("recovery_score"),
            "whoop_strain":       wv("strain"),
            "garmin_stress":      gv("avg_stress_level"),
            "body_battery_hi":    gv("body_battery_highest"),
            "body_battery_lo":    gv("body_battery_lowest"),
            # Intensity — Garmin
            "intensity_mins":     (
                (g.get("moderate_intensity_mins") or 0) +
                2 * (g.get("vigorous_intensity_mins") or 0)
                ) or None,
            "vigorous_mins":      gv("vigorous_intensity_mins"),
            # SpO2 / respiration — prefer WHOOP, fall back Garmin
            "spo2":               wv("spo2_pct")          or gv("spo2_avg"),
            "resp_rate":          wv("respiratory_rate")  or gv("respiration_avg"),
            "skin_temp":          wv("skin_temp_celsius"),
            # Strava training
            "train_hrs":          s.get("total_hrs"),
            "run_miles":          s.get("run_miles"),
            "run_hrs":            s.get("run_hrs"),
            "ride_hrs":           s.get("ride_hrs"),
            "strength_hrs":       s.get("strength_hrs"),
            "strava_avg_hr":      s.get("avg_hr"),
            "strava_avg_watts":   s.get("avg_watts"),
            "n_activities":       s.get("n_activities"),
            # Calories — Garmin
            "active_kcal":        gv("active_kilocalories"),
            # Weight — Garmin (sparse)
            "weight_kg":          gv("weight_kg"),
        }
        combined.append(row)

    return combined


# ---------------------------------------------------------------------------
# Monthly aggregation for trend charts
# ---------------------------------------------------------------------------

def monthly_means(combined: list[dict], fields: list[str]) -> list[dict]:
    """Group combined daily data by month, return means for requested fields."""
    buckets: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in combined:
        ym = row["date"][:7]
        for f in fields:
            v = row.get(f)
            if v is not None:
                buckets[ym][f].append(v)
    result = []
    for ym in sorted(buckets.keys()):
        r = {"month": ym}
        for f in fields:
            vals = buckets[ym][f]
            r[f] = round(sum(vals) / len(vals), 2) if vals else None
            r[f"_n_{f}"] = len(vals)
        result.append(r)
    return result


# ---------------------------------------------------------------------------
# Comparison stats (Garmin vs WHOOP overlap)
# ---------------------------------------------------------------------------

def build_comparison_stats(garmin: dict, whoop: dict) -> dict:
    """Compute head-to-head stats for overlapping signals."""
    overlap = sorted(set(garmin.keys()) & set(whoop.keys()))
    overlap = [d for d in overlap if d >= "2020-10-22"]  # WHOOP start

    def corr(xs, ys):
        n = len(xs)
        if n < 3:
            return None
        mx, my = sum(xs)/n, sum(ys)/n
        num = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
        dx  = sum((x-mx)**2 for x in xs)**0.5
        dy  = sum((y-my)**2 for y in ys)**0.5
        return num/(dx*dy) if dx*dy else None

    # Sleep
    sleep_pairs = [(garmin[d]["sleep_total_seconds"]/3600,
                    whoop[d]["sleep_total_in_bed_ms"]/3600000)
                   for d in overlap
                   if garmin[d].get("sleep_total_seconds") and whoop[d].get("sleep_total_in_bed_ms")]
    gs = [p[0] for p in sleep_pairs]; ws = [p[1] for p in sleep_pairs]
    sleep_diffs = [abs(g-w) for g,w in sleep_pairs]

    # RHR
    rhr_pairs = [(garmin[d]["resting_heart_rate"], whoop[d]["resting_hr"])
                 for d in overlap
                 if garmin[d].get("resting_heart_rate") and whoop[d].get("resting_hr")]
    gr = [p[0] for p in rhr_pairs]; wr_v = [p[1] for p in rhr_pairs]

    # Respiration (Garmin: respiration_avg, WHOOP: respiratory_rate)
    resp_pairs = [(garmin[d]["respiration_avg"], whoop[d]["respiratory_rate"])
                  for d in overlap
                  if garmin[d].get("respiration_avg") and whoop[d].get("respiratory_rate")]
    grs = [p[0] for p in resp_pairs]; wrs = [p[1] for p in resp_pairs]

    return {
        "overlap_days": len(overlap),
        "overlap_start": overlap[0] if overlap else None,
        "overlap_end":   overlap[-1] if overlap else None,
        "sleep": {
            "n":            len(sleep_pairs),
            "garmin_avg":   round(sum(gs)/len(gs), 2)  if gs else None,
            "whoop_avg":    round(sum(ws)/len(ws), 2)  if ws else None,
            "avg_abs_diff": round(sum(sleep_diffs)/len(sleep_diffs), 2) if sleep_diffs else None,
            "pearson_r":    round(corr(gs, ws), 3) if gs else None,
        },
        "rhr": {
            "n":            len(rhr_pairs),
            "garmin_avg":   round(sum(gr)/len(gr), 1)   if gr else None,
            "whoop_avg":    round(sum(wr_v)/len(wr_v), 1) if wr_v else None,
            "avg_abs_diff": round(sum(abs(g-w) for g,w in rhr_pairs)/len(rhr_pairs), 1) if rhr_pairs else None,
            "pearson_r":    round(corr(gr, wr_v), 3) if gr else None,
        },
        "respiration": {
            "n":            len(resp_pairs),
            "garmin_avg":   round(sum(grs)/len(grs), 2) if grs else None,
            "whoop_avg":    round(sum(wrs)/len(wrs), 2) if wrs else None,
            "pearson_r":    round(corr(grs, wrs), 3) if grs else None,
        },
    }


# ---------------------------------------------------------------------------
# Sample scatter data for comparison charts (daily, recent 2 years)
# ---------------------------------------------------------------------------

def build_scatter_data(garmin: dict, whoop: dict, strava: dict) -> dict:
    """Build scatter datasets using weekly averages to keep payload small."""
    cutoff = (date.today() - timedelta(days=730)).isoformat()

    def iso_week(d: str) -> str:
        dt = datetime.strptime(d, "%Y-%m-%d")
        # Monday-based week key
        mon = dt - timedelta(days=dt.weekday())
        return mon.strftime("%Y-%m-%d")

    def weekly_scatter(dates, x_fn, y_fn) -> list[dict]:
        """Aggregate to weekly means before returning scatter points."""
        buckets: dict[str, dict[str, list]] = defaultdict(lambda: {"x": [], "y": []})
        for d in dates:
            g = garmin.get(d, {}); w = whoop.get(d, {})
            x = x_fn(g, w); y = y_fn(g, w)
            if x is not None and y is not None:
                wk = iso_week(d)
                buckets[wk]["x"].append(x)
                buckets[wk]["y"].append(y)
        return [
            {"date": wk,
             "x": round(sum(v["x"])/len(v["x"]), 2),
             "y": round(sum(v["y"])/len(v["y"]), 2)}
            for wk, v in sorted(buckets.items())
            if v["x"]
        ]

    overlap_gw = sorted(d for d in set(garmin.keys()) & set(whoop.keys()) if d >= cutoff)

    # Strava vs WHOOP
    strava_whoop = sorted(d for d in set(whoop.keys()) & set(strava.keys()) if d >= cutoff)
    strain_vs_hrs_buckets: dict[str, dict] = defaultdict(lambda: {"x": [], "y": []})
    for d in strava_whoop:
        s = whoop[d].get("strain")
        h = strava.get(d, {}).get("total_hrs")
        if s is not None and h is not None:
            wk = iso_week(d)
            strain_vs_hrs_buckets[wk]["x"].append(h)
            strain_vs_hrs_buckets[wk]["y"].append(s)
    strain_vs_hrs = [
        {"date": wk, "x": round(sum(v["x"])/len(v["x"]), 2),
         "y": round(sum(v["y"])/len(v["y"]), 2)}
        for wk, v in sorted(strain_vs_hrs_buckets.items()) if v["x"]
    ]

    # Steps vs strain (Garmin + WHOOP)
    steps_buckets: dict[str, dict] = defaultdict(lambda: {"x": [], "y": []})
    for d in overlap_gw:
        st = garmin[d].get("total_steps")
        s  = whoop[d].get("strain")
        if st and s is not None:
            wk = iso_week(d)
            steps_buckets[wk]["x"].append(st)
            steps_buckets[wk]["y"].append(s)
    steps_vs_strain = [
        {"date": wk, "x": round(sum(v["x"])/len(v["x"])),
         "y": round(sum(v["y"])/len(v["y"]), 2)}
        for wk, v in sorted(steps_buckets.items()) if v["x"]
    ]

    return {
        "sleep_garmin_vs_whoop": weekly_scatter(overlap_gw,
            lambda g, w: g.get("sleep_total_seconds", 0)/3600 if g.get("sleep_total_seconds") else None,
            lambda g, w: w.get("sleep_total_in_bed_ms", 0)/3600000 if w.get("sleep_total_in_bed_ms") else None),
        "rhr_garmin_vs_whoop": weekly_scatter(overlap_gw,
            lambda g, w: g.get("resting_heart_rate"),
            lambda g, w: w.get("resting_hr")),
        "resp_garmin_vs_whoop": weekly_scatter(overlap_gw,
            lambda g, w: g.get("respiration_avg"),
            lambda g, w: w.get("respiratory_rate")),
        "strain_vs_strava_hrs": strain_vs_hrs,
        "steps_vs_strain":      steps_vs_strain,
        "bb_vs_recovery": weekly_scatter(overlap_gw,
            lambda g, w: g.get("body_battery_highest"),
            lambda g, w: w.get("recovery_score")),
        "garmin_stress_vs_whoop_recovery": weekly_scatter(overlap_gw,
            lambda g, w: g.get("avg_stress_level"),
            lambda g, w: w.get("recovery_score")),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def build_html(stats: dict, monthly: list[dict], scatter: dict,
               combined_summary: dict) -> str:

    payload = json.dumps({
        "stats":    stats,
        "monthly":  monthly,
        "scatter":  scatter,
        "summary":  combined_summary,
        "blood_draws": BLOOD_DRAW_DATES,
        "generated": date.today().isoformat(),
    }, separators=(",", ":"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Andy — Fitness Sources</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
  :root{{
    --bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;
    --border:#2e3250;--text:#e2e8f0;--muted:#8892a4;
    --green:#22c55e;--yellow:#eab308;--red:#ef4444;
    --blue:#60a5fa;--accent:#818cf8;--orange:#f97316;
    --radius:12px;
  }}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{background:var(--bg);color:var(--text);
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       font-size:14px;line-height:1.5;padding:1rem;}}

  header{{display:flex;align-items:center;justify-content:space-between;
          padding:1.25rem 1.5rem;background:var(--surface);
          border-radius:var(--radius);border:1px solid var(--border);
          margin-bottom:1.5rem;flex-wrap:wrap;gap:0.75rem;}}
  header h1{{font-size:1.3rem;font-weight:700;}}
  header p{{color:var(--muted);font-size:0.85rem;margin-top:2px;}}
  .nav-link{{display:inline-flex;align-items:center;gap:0.4rem;
             background:var(--surface2);border:1px solid var(--border);
             color:var(--accent);border-radius:6px;padding:0.35rem 0.8rem;
             font-size:0.8rem;font-weight:600;text-decoration:none;transition:all 0.15s;}}
  .nav-link:hover{{background:var(--accent);border-color:var(--accent);color:white;}}

  .tabs{{display:flex;gap:0.5rem;margin-bottom:1.5rem;flex-wrap:wrap;}}
  .tab-btn{{background:var(--surface);border:1px solid var(--border);
            color:var(--muted);border-radius:6px;padding:0.4rem 0.85rem;
            font-size:0.8rem;cursor:pointer;transition:all 0.15s;}}
  .tab-btn:hover,.tab-btn.active{{background:var(--accent);border-color:var(--accent);color:white;}}
  .tab-panel{{display:none;}}.tab-panel.active{{display:block;}}

  .section{{margin-bottom:2rem;}}
  .section>h2{{font-size:1rem;font-weight:700;color:var(--accent);
               margin-bottom:0.75rem;padding-bottom:0.5rem;
               border-bottom:1px solid var(--border);}}
  .section>p.desc{{color:var(--muted);font-size:0.82rem;margin-bottom:1rem;line-height:1.6;}}

  /* Stat grid */
  .stat-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:0.85rem;margin-bottom:1.5rem;}}
  .stat-card{{background:var(--surface);border:1px solid var(--border);
              border-radius:var(--radius);padding:0.85rem 1rem;}}
  .stat-card .label{{font-size:0.72rem;color:var(--muted);text-transform:uppercase;
                     letter-spacing:0.04em;margin-bottom:0.3rem;}}
  .stat-card .value{{font-size:1.25rem;font-weight:700;}}
  .stat-card .sub{{font-size:0.72rem;color:var(--muted);margin-top:2px;}}

  /* Source comparison table */
  .compare-table{{width:100%;border-collapse:collapse;font-size:0.82rem;}}
  .compare-table th{{padding:7px 10px;font-weight:600;color:var(--muted);
                     text-align:left;border-bottom:1px solid var(--border);}}
  .compare-table td{{padding:7px 10px;border-bottom:1px solid rgba(46,50,80,0.4);}}
  .compare-table tr:nth-child(even){{background:rgba(255,255,255,0.02);}}
  .verdict-good{{color:var(--green);font-weight:700;}}
  .verdict-ok{{color:var(--yellow);font-weight:700;}}
  .verdict-poor{{color:var(--red);font-weight:700;}}

  /* Source legend pills */
  .src-pill{{display:inline-block;font-size:0.65rem;font-weight:700;
             padding:1px 5px;border-radius:4px;margin-left:4px;}}
  .src-whoop{{background:rgba(34,197,94,0.2);color:#22c55e;}}
  .src-garmin{{background:rgba(96,165,250,0.2);color:#60a5fa;}}
  .src-strava{{background:rgba(249,115,22,0.2);color:#f97316;}}

  /* Charts */
  .charts-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem;}}
  .chart-card{{background:var(--surface);border:1px solid var(--border);
               border-radius:var(--radius);padding:1rem;}}
  .chart-header{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:0.5rem;}}
  .chart-title{{font-size:0.82rem;font-weight:700;}}
  .chart-meta{{text-align:right;font-size:0.7rem;color:var(--muted);}}
  .chart-wrap{{height:160px;position:relative;}}
  .full-card{{grid-column:1/-1;}}
  .tall-wrap{{height:220px;position:relative;}}

  .legend-row{{display:flex;gap:0.75rem;flex-wrap:wrap;margin-top:0.4rem;}}
  .legend-item{{display:flex;align-items:center;gap:4px;font-size:0.65rem;color:var(--muted);}}
  .legend-swatch{{width:12px;height:8px;border-radius:2px;}}

  /* Coverage summary */
  .coverage-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:0.75rem;}}
  .cov-card{{background:var(--surface);border:1px solid var(--border);
             border-radius:var(--radius);padding:0.85rem 1rem;}}
  .cov-metric{{font-size:0.82rem;font-weight:700;margin-bottom:0.4rem;}}
  .cov-bar-track{{height:6px;background:rgba(255,255,255,0.08);border-radius:3px;margin:3px 0;}}
  .cov-bar-fill{{height:6px;border-radius:3px;}}
  .cov-note{{font-size:0.72rem;color:var(--muted);line-height:1.5;margin-top:4px;}}

  footer{{text-align:center;color:var(--muted);font-size:0.75rem;
          padding:1rem 0;border-top:1px solid var(--border);margin-top:2rem;}}
  @media(max-width:600px){{.charts-grid{{grid-template-columns:1fr;}}}}
</style>
</head>
<body>

<header>
  <div>
    <h1>📊 Fitness Sources</h1>
    <p>Garmin Connect · WHOOP · Strava — comparison, coverage &amp; combined signals</p>
  </div>
  <div style="display:flex;gap:0.5rem;flex-wrap:wrap;">
    <a href="correlations.html" class="nav-link">🔬 Correlations</a>
    <a href="models.html"       class="nav-link">📐 Models</a>
    <a href="index.html"        class="nav-link">← Dashboard</a>
  </div>
</header>

<div class="tabs" id="tabs"></div>
<div id="panels"></div>
<footer id="footer"></footer>

<script>
const DATA = {payload};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const tabsEl   = document.getElementById('tabs');
const panelsEl = document.getElementById('panels');
let firstTab   = true;

function addTab(label, id, buildFn) {{
  const btn = document.createElement('button');
  btn.className = 'tab-btn' + (firstTab ? ' active' : '');
  btn.textContent = label;
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('panel-'+id).classList.add('active');
  }});
  tabsEl.appendChild(btn);
  const panel = document.createElement('div');
  panel.className = 'tab-panel' + (firstTab ? ' active' : '');
  panel.id = 'panel-'+id;
  buildFn(panel);
  panelsEl.appendChild(panel);
  firstTab = false;
}}

function mkSection(title, desc) {{
  const sec = document.createElement('div');
  sec.className = 'section';
  sec.innerHTML = `<h2>${{title}}</h2>` + (desc ? `<p class="desc">${{desc}}</p>` : '');
  return sec;
}}

function chartCard(title, meta, height, full) {{
  const card = document.createElement('div');
  card.className = 'chart-card' + (full ? ' full-card' : '');
  card.innerHTML = `<div class="chart-header">
    <div class="chart-title">${{title}}</div>
    <div class="chart-meta">${{meta||''}}</div>
  </div>`;
  const wrap = document.createElement('div');
  wrap.className = height > 160 ? 'tall-wrap' : 'chart-wrap';
  wrap.style.height = height + 'px';
  const canvas = document.createElement('canvas');
  wrap.appendChild(canvas);
  card.appendChild(wrap);
  return {{card, canvas}};
}}

function scatterChart(canvas, pts, xLabel, yLabel, color) {{
  if (!pts || pts.length === 0) return;
  const years = pts.map(p => parseInt(p.date.substring(0,4)));
  const minYr = Math.min(...years), maxYr = Math.max(...years);
  const colors = pts.map(p => {{
    const t = maxYr===minYr ? 0.5 : (parseInt(p.date.substring(0,4))-minYr)/(maxYr-minYr);
    return `rgba(${{Math.round(96+t*(249-96))}},${{Math.round(165+t*(115-165))}},${{Math.round(250+t*(22-250))}},0.8)`;
  }});
  // regression line
  const n = pts.length, xs = pts.map(p=>p.x), ys = pts.map(p=>p.y);
  const mx=xs.reduce((a,b)=>a+b,0)/n, my=ys.reduce((a,b)=>a+b,0)/n;
  const num=xs.reduce((s,x,i)=>s+(x-mx)*(ys[i]-my),0);
  const den=xs.reduce((s,x)=>s+(x-mx)**2,0);
  const b = den>0 ? num/den : 0, a = my-b*mx;
  const xMin=Math.min(...xs), xMax=Math.max(...xs);
  const r = den>0 ? num/(den**0.5 * ys.reduce((s,y)=>s+(y-my)**2,0)**0.5) : 0;

  new Chart(canvas, {{
    type: 'scatter',
    data: {{datasets:[
      {{label:'data', data:pts.map(p=>({{x:p.x,y:p.y}})),
        backgroundColor:colors, borderColor:colors, borderWidth:1.5,
        pointRadius:4, pointHoverRadius:6}},
      {{label:'trend', data:[{{x:xMin,y:a+b*xMin}},{{x:xMax,y:a+b*xMax}}],
        type:'line', borderColor:'rgba(129,140,248,0.5)', borderWidth:1.5,
        borderDash:[4,3], pointRadius:0, fill:false}},
    ]}},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{
        legend:{{display:false}},
        tooltip:{{backgroundColor:'#1a1d27',borderColor:'#2e3250',borderWidth:1,
                 titleColor:'#e2e8f0',bodyColor:'#8892a4',
                 callbacks:{{
                   title:items=>pts[items[0].dataIndex]?.date||'',
                   label:ctx=>`${{xLabel}}: ${{ctx.parsed.x}}, ${{yLabel}}: ${{ctx.parsed.y}}`,
                 }},
                 filter:item=>item.dataset.label==='data'}},
      }},
      scales:{{
        x:{{title:{{display:true,text:xLabel,color:'#8892a4',font:{{size:9}}}},
            grid:{{color:'rgba(46,50,80,0.6)'}},ticks:{{color:'#8892a4',font:{{size:9}}}}}},
        y:{{title:{{display:true,text:yLabel,color:'#8892a4',font:{{size:9}}}},
            grid:{{color:'rgba(46,50,80,0.6)'}},ticks:{{color:'#8892a4',font:{{size:9}}}}}},
      }},
    }},
  }});
  // annotate r on card title area
  const titleEl = canvas.closest('.chart-card')?.querySelector('.chart-meta');
  if (titleEl) titleEl.textContent = `r = ${{r.toFixed(3)}} · n=${{n}}`;
}}

function bloodDrawPlugin(labels) {{
  return {{
    id:'bloodDraws',
    afterDraw(chart) {{
      const {{ctx, chartArea:{{top,bottom}}, scales:{{x}}}} = chart;
      if (!x) return;
      DATA.blood_draws.forEach(dd => {{
        const idx = labels.indexOf(dd);
        if (idx < 0) return;
        const xPx = x.getPixelForValue(idx);
        ctx.save();
        ctx.beginPath(); ctx.moveTo(xPx,top); ctx.lineTo(xPx,bottom);
        ctx.strokeStyle='rgba(234,179,8,0.4)';ctx.lineWidth=1;
        ctx.setLineDash([3,3]);ctx.stroke();ctx.restore();
      }});
    }},
  }};
}}

const CHARTCFG = {{
  responsive:true, maintainAspectRatio:false,
  interaction:{{mode:'index',intersect:false}},
  plugins:{{
    legend:{{display:false}},
    tooltip:{{backgroundColor:'#1a1d27',borderColor:'#2e3250',borderWidth:1,
             titleColor:'#e2e8f0',bodyColor:'#8892a4'}},
  }},
  scales:{{
    x:{{grid:{{color:'rgba(46,50,80,0.6)'}},ticks:{{color:'#8892a4',maxRotation:45,maxTicksLimit:24,font:{{size:9}}}}}},
    y:{{grid:{{color:'rgba(46,50,80,0.6)'}},ticks:{{color:'#8892a4',font:{{size:10}}}}}},
  }},
}};

// ---------------------------------------------------------------------------
// Tab 1: Source Comparison
// ---------------------------------------------------------------------------
addTab('⚖️ Source Comparison', 'compare', panel => {{
  const s = DATA.stats;

  // Summary stat cards
  const secStats = mkSection('Head-to-Head: Garmin vs WHOOP Overlap',
    `${{s.overlap_days}} days with both devices worn (${{s.overlap_start}} – ${{s.overlap_end}}).
     Pearson r and mean absolute difference computed per daily value.`);

  const cards = [
    ['Sleep hours', `r = ${{s.sleep.pearson_r}}`, `Avg diff: ${{s.sleep.avg_abs_diff}}h · Garmin: ${{s.sleep.garmin_avg}}h vs WHOOP: ${{s.sleep.whoop_avg}}h`, s.sleep.pearson_r >= 0.7 ? 'good' : s.sleep.pearson_r >= 0.4 ? 'ok' : 'poor'],
    ['Resting HR', `r = ${{s.rhr.pearson_r}}`, `Avg diff: ${{s.rhr.avg_abs_diff}} bpm · Garmin: ${{s.rhr.garmin_avg}} vs WHOOP: ${{s.rhr.whoop_avg}}`, s.rhr.pearson_r >= 0.7 ? 'good' : s.rhr.pearson_r >= 0.4 ? 'ok' : 'poor'],
    ['Respiration', `r = ${{s.respiration.pearson_r}}`, `Garmin: ${{s.respiration.garmin_avg}} vs WHOOP: ${{s.respiration.whoop_avg}} br/min`, s.respiration.pearson_r >= 0.7 ? 'good' : s.respiration.pearson_r >= 0.4 ? 'ok' : 'poor'],
  ];
  const statGrid = document.createElement('div');
  statGrid.className = 'stat-grid';
  cards.forEach(([label, val, sub, verdict]) => {{
    const c = document.createElement('div');
    c.className = 'stat-card';
    const col = {{good:'var(--green)',ok:'var(--yellow)',poor:'var(--red)'}}[verdict];
    c.innerHTML = `<div class="label">${{label}}</div>
                   <div class="value" style="color:${{col}}">${{val}}</div>
                   <div class="sub">${{sub}}</div>`;
    statGrid.appendChild(c);
  }});
  secStats.appendChild(statGrid);
  panel.appendChild(secStats);

  // Coverage table
  const secCov = mkSection('Signal Coverage by Source', null);
  const cGrid = document.createElement('div');
  cGrid.className = 'coverage-grid';
  const coverage = [
    {{metric:'HRV (rMSSD)', garmin:false, whoop:true,  strava:false, note:'WHOOP only — no Garmin HRV in this DB', winner:'whoop'}},
    {{metric:'Sleep hours', garmin:true,  whoop:true,  strava:false, note:'Both; WHOOP preferred (r='+s.sleep.pearson_r+' agreement, WHOOP avoids watch-on-wrist artifact)', winner:'whoop'}},
    {{metric:'Resting HR',  garmin:true,  whoop:true,  strava:false, note:'Both; WHOOP preferred (dedicated overnight measurement)', winner:'whoop'}},
    {{metric:'SpO₂',        garmin:true,  whoop:true,  strava:false, note:'Both (sparse on Garmin); WHOOP preferred', winner:'whoop'}},
    {{metric:'Respiration', garmin:true,  whoop:true,  strava:false, note:'Both agree well (r='+s.respiration.pearson_r+')', winner:'either'}},
    {{metric:'Recovery',    garmin:false, whoop:true,  strava:false, note:'WHOOP only (0–100 score)', winner:'whoop'}},
    {{metric:'Strain',      garmin:false, whoop:true,  strava:false, note:'WHOOP only (0–21 score)', winner:'whoop'}},
    {{metric:'Steps',       garmin:true,  whoop:false, strava:false, note:'Garmin only — back to 2015, WHOOP has none', winner:'garmin'}},
    {{metric:'Stress',      garmin:true,  whoop:false, strava:false, note:'Garmin only (0–100, HRV-derived)', winner:'garmin'}},
    {{metric:'Body battery',garmin:true,  whoop:false, strava:false, note:'Garmin only (0–100 energy reserve)', winner:'garmin'}},
    {{metric:'Training hrs',garmin:false, whoop:false, strava:true,  note:'Strava: sport breakdown, power, pace', winner:'strava'}},
    {{metric:'Skin temp',   garmin:false, whoop:true,  strava:false, note:'WHOOP only', winner:'whoop'}},
  ];
  coverage.forEach(c => {{
    const card = document.createElement('div');
    card.className = 'cov-card';
    const srcBadge = src => `<span class="src-pill src-${{src}}">${{src}}</span>`;
    const avail = [c.garmin?'garmin':null, c.whoop?'whoop':null, c.strava?'strava':null].filter(Boolean);
    card.innerHTML = `
      <div class="cov-metric">${{c.metric}} — winner: ${{srcBadge(c.winner)}}</div>
      <div style="display:flex;gap:0.3rem;flex-wrap:wrap;margin-bottom:4px;">
        ${{avail.map(s=>srcBadge(s)).join('')}}${{avail.length===0?'<span style="color:var(--muted);font-size:0.72rem;">none</span>':''}}
      </div>
      <div class="cov-note">${{c.note}}</div>
    `;
    cGrid.appendChild(card);
  }});
  secCov.appendChild(cGrid);
  panel.appendChild(secCov);

  // Scatter plots: Garmin vs WHOOP
  const secScatter = mkSection('Daily Scatter: Garmin vs WHOOP (last 2 years)',
    'Each point = one day. Blue (early) → orange (recent). Regression line shows agreement direction.');
  const sg = document.createElement('div');
  sg.className = 'charts-grid';

  [
    ['sleep_garmin_vs_whoop', 'Garmin sleep (hrs)', 'WHOOP sleep (hrs)'],
    ['rhr_garmin_vs_whoop',   'Garmin RHR (bpm)',   'WHOOP RHR (bpm)'],
    ['resp_garmin_vs_whoop',  'Garmin resp (br/min)','WHOOP resp (br/min)'],
    ['bb_vs_recovery',        'Garmin body battery hi', 'WHOOP recovery score'],
    ['garmin_stress_vs_whoop_recovery', 'Garmin stress', 'WHOOP recovery score'],
    ['steps_vs_strain',       'Garmin steps',       'WHOOP strain'],
  ].forEach(([key, xl, yl]) => {{
    const pts = DATA.scatter[key] || [];
    const {{card, canvas}} = chartCard(`${{xl}} vs ${{yl}}`, '', 160);
    sg.appendChild(card);
    scatterChart(canvas, pts, xl, yl);
  }});
  secScatter.appendChild(sg);
  panel.appendChild(secScatter);

  // Strava vs WHOOP strain
  const secStrain = mkSection('WHOOP Strain vs Strava Training Hours', null);
  const sg2 = document.createElement('div');
  sg2.className = 'charts-grid';
  const {{card:sc, canvas:sCanvas}} = chartCard('Strava training hrs vs WHOOP strain', '', 160);
  sg2.appendChild(sc);
  scatterChart(sCanvas, DATA.scatter['strain_vs_strava_hrs']||[], 'Training hours', 'WHOOP strain');
  secStrain.appendChild(sg2);
  panel.appendChild(secStrain);
}});

// ---------------------------------------------------------------------------
// Tab 2: Combined Monthly Trends
// ---------------------------------------------------------------------------
addTab('📈 Combined Trends', 'trends', panel => {{
  const months = DATA.monthly;
  const labels = months.map(m => m.month);
  const bdPlugin = bloodDrawPlugin(labels);

  function trendCard(title, datasets, yLabel, yLabel2, height, full) {{
    const {{card, canvas}} = chartCard(title, '', height||180, full);
    const scales = {{
      x: CHARTCFG.scales.x,
      y: {{...CHARTCFG.scales.y, title:{{display:!!yLabel, text:yLabel||'', color:'#8892a4', font:{{size:9}}}}}},
    }};
    if (yLabel2) scales.y2 = {{position:'right', grid:{{display:false}},
      title:{{display:true,text:yLabel2,color:'#8892a4',font:{{size:9}}}},
      ticks:{{color:'#8892a4',font:{{size:10}}}}}};
    new Chart(canvas, {{
      type:'line', plugins:[bdPlugin],
      data:{{labels, datasets}},
      options:{{...CHARTCFG, scales}},
    }});
    return card;
  }}

  function ds(label, field, color, yAxisID, fill) {{
    return {{
      label, yAxisID: yAxisID||'y',
      data: months.map(m => m[field]),
      borderColor: color, backgroundColor: fill ? color.replace(')',',0.12)').replace('rgb','rgba') : 'transparent',
      borderWidth:2, pointRadius:2, tension:0.3, fill:!!fill, spanGaps:true,
    }};
  }}

  const grid = document.createElement('div');
  grid.className = 'charts-grid';
  grid.style.gridTemplateColumns = '1fr';

  // 1. Steps + training hours
  grid.appendChild(trendCard('Daily Steps (monthly avg) + Training Hours',
    [ds('Steps', 'steps', '#60a5fa', 'y', true),
     ds('Training hrs', 'train_hrs', '#f97316', 'y2')],
    'Steps', 'Training hrs/day', 200, true));

  // 2. Sleep: WHOOP vs Garmin
  grid.appendChild(trendCard('Sleep Hours — combined (WHOOP preferred, Garmin fallback)',
    [ds('Sleep hrs', 'sleep_hrs', '#a855f7', 'y', true),
     ds('Sleep score', 'sleep_score', '#818cf8', 'y2')],
    'Hours', 'Score (0–100)', 180, true));

  // 3. HRV + Recovery
  grid.appendChild(trendCard('WHOOP: HRV & Recovery Score',
    [ds('HRV (ms)', 'hrv_ms', '#22c55e', 'y'),
     ds('Recovery', 'whoop_recovery', '#60a5fa', 'y2')],
    'HRV rMSSD (ms)', 'Recovery (0–100)', 180, true));

  // 4. RHR + Strain
  grid.appendChild(trendCard('Resting HR (WHOOP) + WHOOP Strain',
    [ds('RHR (bpm)', 'rhr', '#ef4444', 'y'),
     ds('Strain', 'whoop_strain', '#f97316', 'y2')],
    'RHR (bpm)', 'Strain (0–21)', 180, true));

  // 5. Garmin stress + body battery
  grid.appendChild(trendCard('Garmin: Stress + Body Battery',
    [ds('Stress (0–100)', 'garmin_stress', '#eab308', 'y'),
     ds('Body battery high', 'body_battery_hi', '#22c55e', 'y2')],
    'Stress', 'Body battery', 180, true));

  // 6. SpO2 + resp rate
  grid.appendChild(trendCard('SpO₂ & Respiration Rate',
    [ds('SpO₂ (%)', 'spo2', '#60a5fa', 'y', true),
     ds('Resp (br/min)', 'resp_rate', '#eab308', 'y2')],
    'SpO₂ (%)', 'Breaths/min', 180, true));

  panel.appendChild(grid);

  // Legend note
  const note = document.createElement('p');
  note.style.cssText = 'color:var(--muted);font-size:0.75rem;margin-top:0.5rem;';
  note.innerHTML = '⚡ Dashed yellow vertical lines = blood draw dates.';
  panel.appendChild(note);
}});

// ---------------------------------------------------------------------------
// Tab 3: Source Coverage
// ---------------------------------------------------------------------------
addTab('📅 Coverage', 'coverage', panel => {{
  const months = DATA.monthly;
  const labels = months.map(m => m.month);

  const sec = mkSection('Data Coverage by Source Over Time',
    'Shows how many days per month have data from each source. Identifies gaps and source transitions.');

  const grid = document.createElement('div');
  grid.className = 'charts-grid';
  grid.style.gridTemplateColumns = '1fr';

  const {{card, canvas}} = chartCard('Monthly data coverage (days/month with data)', '', 250, true);
  grid.appendChild(card);

  new Chart(canvas, {{
    type: 'bar',
    data: {{
      labels,
      datasets: [
        {{label:'WHOOP (HRV)',     data:months.map(m=>m['_n_hrv_ms']||0),     backgroundColor:'rgba(34,197,94,0.7)', stack:'s'}},
        {{label:'WHOOP (sleep)',   data:months.map(m=>m['_n_sleep_hrs']||0),   backgroundColor:'rgba(34,197,94,0.35)',stack:'s2'}},
        {{label:'Garmin (steps)', data:months.map(m=>m['_n_steps']||0),       backgroundColor:'rgba(96,165,250,0.6)', stack:'s3'}},
        {{label:'Strava',         data:months.map(m=>m['_n_train_hrs']||0),   backgroundColor:'rgba(249,115,22,0.6)', stack:'s4'}},
      ],
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      interaction:{{mode:'index',intersect:false}},
      plugins:{{
        legend:{{display:true,labels:{{color:'#8892a4',font:{{size:10}}}}}},
        tooltip:{{backgroundColor:'#1a1d27',borderColor:'#2e3250',borderWidth:1,
                 titleColor:'#e2e8f0',bodyColor:'#8892a4'}},
      }},
      scales:{{
        x:{{grid:{{color:'rgba(46,50,80,0.6)'}},ticks:{{color:'#8892a4',maxRotation:45,maxTicksLimit:24,font:{{size:9}}}}}},
        y:{{grid:{{color:'rgba(46,50,80,0.6)'}},ticks:{{color:'#8892a4',font:{{size:10}}}},title:{{display:true,text:'Days with data',color:'#8892a4',font:{{size:9}}}}}},
      }},
    }},
  }});

  sec.appendChild(grid);
  panel.appendChild(sec);

  // Summary stats
  const sumSec = mkSection('Summary', null);
  const sg = document.createElement('div');
  sg.className = 'stat-grid';
  const sm = DATA.summary;
  [
    ['Garmin steps', sm.garmin_step_days + ' days', '2015 – today'],
    ['WHOOP HRV',    sm.whoop_hrv_days + ' days',   '2020-10 – today'],
    ['Strava',       sm.strava_days + ' days',       '2011 – today'],
    ['Blood draws',  DATA.blood_draws.length + ' draws', '2013 – today'],
    ['Full overlap (all 3)', sm.full_overlap_days + ' days', 'WHOOP + Garmin + Strava'],
  ].forEach(([l,v,s]) => {{
    const c = document.createElement('div');
    c.className = 'stat-card';
    c.innerHTML = `<div class="label">${{l}}</div><div class="value" style="color:var(--accent)">${{v}}</div><div class="sub">${{s}}</div>`;
    sg.appendChild(c);
  }});
  sumSec.appendChild(sg);
  panel.appendChild(sumSec);
}});

document.getElementById('footer').textContent =
  `Generated ${{DATA.generated}} · Garmin + WHOOP + Strava unified fitness dashboard`;
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Loading Garmin...")
    garmin = load_garmin(GARMIN_DB)
    print(f"  {len(garmin)} daily rows")

    print("Loading WHOOP...")
    whoop = load_whoop(WHOOP_DB)
    print(f"  {len(whoop)} daily rows")

    print("Loading Strava daily aggregates...")
    strava = load_strava_daily(STRAVA_DB)
    print(f"  {len(strava)} days with activities")

    print("Building comparison stats...")
    stats = build_comparison_stats(garmin, whoop)
    print(f"  {stats['overlap_days']} overlap days")
    print(f"  Sleep: r={stats['sleep']['pearson_r']}, avg_diff={stats['sleep']['avg_abs_diff']}h")
    print(f"  RHR:   r={stats['rhr']['pearson_r']}, avg_diff={stats['rhr']['avg_abs_diff']} bpm")
    print(f"  Resp:  r={stats['respiration']['pearson_r']}")

    print("Building combined daily signal...")
    combined = build_combined(garmin, whoop, strava)
    print(f"  {len(combined)} combined days")

    print("Building monthly trends...")
    MONTHLY_FIELDS = [
        "steps", "sleep_hrs", "sleep_rem_hrs", "sleep_deep_hrs", "sleep_score",
        "hrv_ms", "rhr", "whoop_recovery", "whoop_strain",
        "garmin_stress", "body_battery_hi", "body_battery_lo",
        "spo2", "resp_rate", "skin_temp",
        "train_hrs", "run_miles", "run_hrs", "ride_hrs",
        "active_kcal", "intensity_mins",
    ]
    monthly = monthly_means(combined, MONTHLY_FIELDS)
    print(f"  {len(monthly)} months")

    print("Building scatter data...")
    scatter = build_scatter_data(garmin, whoop, strava)

    # Summary counts for coverage tab
    full_overlap = sum(1 for d in combined
                       if garmin.get(d["date"], {}).get("total_steps")
                       and whoop.get(d["date"], {}).get("hrv_rmssd_ms")
                       and strava.get(d["date"]))
    summary = {
        "garmin_step_days": sum(1 for d in garmin.values() if d.get("total_steps") and d["total_steps"] > 0),
        "whoop_hrv_days":   sum(1 for d in whoop.values()  if d.get("hrv_rmssd_ms")),
        "strava_days":      len(strava),
        "full_overlap_days": full_overlap,
    }

    print(f"Writing {HTML_FILE}...")
    html = build_html(stats, monthly, scatter, summary)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(HTML_FILE)
    print(f"  Done! {HTML_FILE} ({size:,} bytes)")


if __name__ == "__main__":
    main()
