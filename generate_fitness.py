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

import shell

HTML_FILE  = os.path.join(os.path.dirname(__file__), "fitness.html")

GARMIN_DB = os.path.expanduser("~/projects/2026/garmin-database/garmin.db")
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

    generated = date.today().isoformat()
    payload = json.dumps({
        "stats":    stats,
        "monthly":  monthly,
        "scatter":  scatter,
        "summary":  combined_summary,
        "blood_draws": BLOOD_DRAW_DATES,
        "generated": generated,
    }, separators=(",", ":"))

    nav = """  <div class="nav-section">
    <div class="nav-section-title">Fitness Sources</div>
    <div class="nav-item active" data-section="compare"><span class="icon">⚖️</span> Source Comparison</div>
    <div class="nav-item" data-section="trends"><span class="icon">📈</span> Combined Trends</div>
    <div class="nav-item" data-section="coverage"><span class="icon">📅</span> Coverage</div>
  </div>"""

    return shell.head("Andy — Fitness Sources", generated, PAGE_CSS) + f"""<body>

{shell.sidebar("fitness.html", nav)}
<div class="main">
  <div class="topbar">
    <div>
      <h1 id="section-title">Source Comparison</h1>
      <div class="subtitle">Garmin Connect · WHOOP · Strava — comparison, coverage &amp; combined signals</div>
    </div>
  </div>

  <div class="content">
    <div class="section active" id="section-compare"></div>
    <div class="section" id="section-trends"></div>
    <div class="section" id="section-coverage"></div>
  </div>
</div>

<script>
""" + PAGE_JS.replace("__DATA__", payload) + """</script>
</body>
</html>
"""


# Page-local styles: only what the kit (ar.css) lacks.
PAGE_CSS = """<style>
.desc { color: var(--muted); font-size: 0.82rem; line-height: 1.6; margin: -4px 0 14px; max-width: 80ch; }
.block { margin-bottom: 28px; }
.kpi-strip.wide-tiles { grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
.kpi-card .badge { margin-top: 8px; }
/* three-up chart grid steps down to two before the kit's single-column break */
@media (max-width: 1250px) { .metric-grid.three { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 900px)  { .metric-grid.three { grid-template-columns: minmax(0, 1fr); } }
/* source pill: neutral badge + identity-colour dot */
.src-pill { display: inline-flex; align-items: center; gap: 5px; margin-right: 4px; }
.src-pill .sw { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
td.wrap { white-space: normal; min-width: 240px; color: var(--muted); }
.legend-item .sw.dash { height: 0; width: 14px; border-top: 1px dashed var(--muted); border-radius: 0; }
.legend-item .sw.ramp { width: 44px; background: linear-gradient(90deg, var(--blue), var(--orange)); }
.cov-row + .cov-row { margin-top: 10px; }
.cov-label { font-size: 0.75rem; font-weight: 600; color: var(--text); display: flex; align-items: center; gap: 6px; margin-bottom: 2px; }
.cov-label .sw { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
</style>"""

# Page script.  Plain string (not an f-string): __DATA__ is replaced with the
# JSON payload.  All charts are d3 via the kit's AR.* helpers.
PAGE_JS = r"""const DATA = __DATA__;
const $ = id => document.getElementById(id);

// Source identity colours
const SRC = {
  garmin: { name: 'Garmin', color: 'var(--blue)' },
  whoop:  { name: 'WHOOP',  color: 'var(--pink)' },
  strava: { name: 'Strava', color: 'var(--orange)' },
  either: { name: 'either', color: null },
};
const srcPill = k => `<span class="badge src-pill">${SRC[k].color ? `<span class="sw" style="background:${SRC[k].color}"></span>` : ''}${SRC[k].name}</span>`;

// ── NAV ───────────────────────────────────────────────────────────────────
const TITLES = { compare: 'Source Comparison', trends: 'Combined Trends', coverage: 'Coverage' };
function show(id) {
  if (!TITLES[id]) id = 'compare';
  document.querySelectorAll('.nav-item[data-section]').forEach(n =>
    n.classList.toggle('active', n.dataset.section === id));
  document.querySelectorAll('.section').forEach(s =>
    s.classList.toggle('active', s.id === 'section-' + id));
  $('section-title').textContent = TITLES[id];
  if (location.hash.slice(1) !== id) history.replaceState(null, '', '#' + id);
  window.scrollTo(0, 0);
}
document.querySelectorAll('.nav-item[data-section]').forEach(n =>
  n.addEventListener('click', () => show(n.dataset.section)));
window.addEventListener('hashchange', () => show(location.hash.slice(1)));

// ── HELPERS ───────────────────────────────────────────────────────────────
function mkSection(parent, title, desc) {
  const sec = document.createElement('div');
  sec.className = 'block';
  sec.innerHTML = `<div class="section-heading">${title}</div>` + (desc ? `<p class="desc">${desc}</p>` : '');
  parent.appendChild(sec);
  return sec;
}

function chartCard(grid, title, sub) {
  const card = document.createElement('div');
  card.className = 'metric-card';
  card.innerHTML = `
    <div class="metric-card-header"><div class="metric-card-title">${title}</div></div>
    <div class="metric-card-sub">${sub || ''}</div>
    <div class="chart-area"></div>`;
  grid.appendChild(card);
  return card;
}

// Scatter of weekly means, coloured blue (early) → orange (recent) by date,
// with a least-squares line.  The ramp is a CSS color-mix of two kit tokens,
// so it follows the theme without a redraw.
function scatterChart(card, pts, xLabel, yLabel) {
  if (!pts || pts.length === 0) {
    card.querySelector('.chart-area').innerHTML = '<div class="empty">No data</div>';
    return;
  }
  const t0 = AR.parseDate(pts[0].date), t1 = AR.parseDate(pts[pts.length - 1].date);
  const ramp = p => {
    const t = t1 > t0 ? (AR.parseDate(p.date) - t0) / (t1 - t0) : 0.5;
    return `color-mix(in srgb, var(--orange) ${Math.round(t * 100)}%, var(--blue))`;
  };
  // regression line
  const n = pts.length, xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  const mx = xs.reduce((a, b) => a + b, 0) / n, my = ys.reduce((a, b) => a + b, 0) / n;
  const num = xs.reduce((s, x, i) => s + (x - mx) * (ys[i] - my), 0);
  const den = xs.reduce((s, x) => s + (x - mx) ** 2, 0);
  const b = den > 0 ? num / den : 0, a = my - b * mx;
  const r = den > 0 ? num / (den ** 0.5 * ys.reduce((s, y) => s + (y - my) ** 2, 0) ** 0.5) : 0;

  AR.scatter(card.querySelector('.chart-area'), {
    points: pts.map(p => ({ x: p.x, y: p.y, label: 'Week of ' + AR.fmtDate(p.date), color: ramp(p) })),
    height: 230, r: 4, xLabel, yLabel,
    fit: { slope: b, intercept: a },
    tip: p => `<b>${p.label}</b><br><span class="dim">${xLabel}</span> ${p.x}<br><span class="dim">${yLabel}</span> ${p.y}`,
  });
  card.querySelector('.metric-card-sub').textContent = `r = ${r.toFixed(3)} · n=${n}`;
}

const SCATTER_LEGEND = `<div class="legend">
  <span class="legend-item">earlier <span class="sw ramp"></span> recent</span>
  <span class="legend-item"><span class="sw dash"></span>least-squares fit</span>
</div>`;

// ── SOURCE COMPARISON ─────────────────────────────────────────────────────
(function () {
  const panel = $('section-compare');
  const s = DATA.stats;

  // Summary stat cards
  const secStats = mkSection(panel, 'Head-to-Head: Garmin vs WHOOP Overlap',
    `${s.overlap_days} days with both devices worn (${s.overlap_start} – ${s.overlap_end}).
     Pearson r and mean absolute difference computed per daily value.`);

  const verdict = r => r >= 0.7 ? 'good' : r >= 0.4 ? 'ok' : 'poor';
  const VERDICT = {
    good: { color: 'var(--good)', cls: 'good', label: 'Good agreement' },
    ok:   { color: 'var(--warn)', cls: 'warn', label: 'OK agreement' },
    poor: { color: 'var(--bad)',  cls: 'bad',  label: 'Poor agreement' },
  };
  const cards = [
    ['Sleep hours', `r = ${s.sleep.pearson_r}`, `Avg diff: ${s.sleep.avg_abs_diff}h · Garmin: ${s.sleep.garmin_avg}h vs WHOOP: ${s.sleep.whoop_avg}h`, verdict(s.sleep.pearson_r)],
    ['Resting HR', `r = ${s.rhr.pearson_r}`, `Avg diff: ${s.rhr.avg_abs_diff} bpm · Garmin: ${s.rhr.garmin_avg} vs WHOOP: ${s.rhr.whoop_avg}`, verdict(s.rhr.pearson_r)],
    ['Respiration', `r = ${s.respiration.pearson_r}`, `Garmin: ${s.respiration.garmin_avg} vs WHOOP: ${s.respiration.whoop_avg} br/min`, verdict(s.respiration.pearson_r)],
  ];
  secStats.insertAdjacentHTML('beforeend', `<div class="kpi-strip wide-tiles">${cards.map(([label, val, sub, v]) => `
    <div class="kpi-card" style="--kpi-color:${VERDICT[v].color}">
      <div class="kpi-label">${label}</div>
      <div class="kpi-value" style="color:${VERDICT[v].color}">${val}</div>
      <div class="kpi-sub">${sub}</div>
      <span class="badge ${VERDICT[v].cls}">${VERDICT[v].label}</span>
    </div>`).join('')}</div>`);

  // Coverage table
  const secCov = mkSection(panel, 'Signal Coverage by Source', null);
  const coverage = [
    {metric:'HRV (rMSSD)', garmin:false, whoop:true,  strava:false, note:'WHOOP only — no Garmin HRV in this DB', winner:'whoop'},
    {metric:'Sleep hours', garmin:true,  whoop:true,  strava:false, note:'Both; WHOOP preferred (r='+s.sleep.pearson_r+' agreement, WHOOP avoids watch-on-wrist artifact)', winner:'whoop'},
    {metric:'Resting HR',  garmin:true,  whoop:true,  strava:false, note:'Both; WHOOP preferred (dedicated overnight measurement)', winner:'whoop'},
    {metric:'SpO₂',        garmin:true,  whoop:true,  strava:false, note:'Both (sparse on Garmin); WHOOP preferred', winner:'whoop'},
    {metric:'Respiration', garmin:true,  whoop:true,  strava:false, note:'Both agree well (r='+s.respiration.pearson_r+')', winner:'either'},
    {metric:'Recovery',    garmin:false, whoop:true,  strava:false, note:'WHOOP only (0–100 score)', winner:'whoop'},
    {metric:'Strain',      garmin:false, whoop:true,  strava:false, note:'WHOOP only (0–21 score)', winner:'whoop'},
    {metric:'Steps',       garmin:true,  whoop:false, strava:false, note:'Garmin only — back to 2015, WHOOP has none', winner:'garmin'},
    {metric:'Stress',      garmin:true,  whoop:false, strava:false, note:'Garmin only (0–100, HRV-derived)', winner:'garmin'},
    {metric:'Body battery',garmin:true,  whoop:false, strava:false, note:'Garmin only (0–100 energy reserve)', winner:'garmin'},
    {metric:'Training hrs',garmin:false, whoop:false, strava:true,  note:'Strava: sport breakdown, power, pace', winner:'strava'},
    {metric:'Skin temp',   garmin:false, whoop:true,  strava:false, note:'WHOOP only', winner:'whoop'},
  ];
  secCov.insertAdjacentHTML('beforeend', `<div class="metric-card"><div class="table-wrap"><table>
    <thead><tr><th>Metric</th><th>Available from</th><th>Winner</th><th>Note</th></tr></thead>
    <tbody>${coverage.map(c => {
      const avail = ['garmin', 'whoop', 'strava'].filter(k => c[k]);
      return `<tr><td><b>${c.metric}</b></td>
        <td>${avail.map(srcPill).join('') || '<span class="dim">none</span>'}</td>
        <td>${srcPill(c.winner)}</td>
        <td class="wrap">${c.note}</td></tr>`;
    }).join('')}</tbody></table></div></div>`);

  // Scatter plots: Garmin vs WHOOP
  const secScatter = mkSection(panel, 'Weekly Scatter: Garmin vs WHOOP (last 2 years)',
    'Each point = one week (mean of the daily values). Blue (early) → orange (recent). Regression line shows agreement direction.');
  secScatter.insertAdjacentHTML('beforeend', SCATTER_LEGEND);
  const sg = document.createElement('div');
  sg.className = 'metric-grid three';
  secScatter.appendChild(sg);

  [
    ['sleep_garmin_vs_whoop', 'Garmin sleep (hrs)', 'WHOOP sleep (hrs)'],
    ['rhr_garmin_vs_whoop',   'Garmin RHR (bpm)',   'WHOOP RHR (bpm)'],
    ['resp_garmin_vs_whoop',  'Garmin resp (br/min)','WHOOP resp (br/min)'],
    ['bb_vs_recovery',        'Garmin body battery hi', 'WHOOP recovery score'],
    ['garmin_stress_vs_whoop_recovery', 'Garmin stress', 'WHOOP recovery score'],
    ['steps_vs_strain',       'Garmin steps',       'WHOOP strain'],
  ].forEach(([key, xl, yl]) => {
    scatterChart(chartCard(sg, `${xl} vs ${yl}`, ''), DATA.scatter[key] || [], xl, yl);
  });

  // Strava vs WHOOP strain
  const secStrain = mkSection(panel, 'WHOOP Strain vs Strava Training Hours', null);
  const sg2 = document.createElement('div');
  sg2.className = 'metric-grid three';
  secStrain.appendChild(sg2);
  scatterChart(chartCard(sg2, 'Strava training hrs vs WHOOP strain', ''),
    DATA.scatter['strain_vs_strava_hrs'] || [], 'Training hours', 'WHOOP strain');
})();

// ── COMBINED MONTHLY TRENDS ───────────────────────────────────────────────
// Each pair of related signals sits side by side as two charts on a shared
// time axis (one y-scale per chart), with blood-draw dates as dashed markers.
(function () {
  const panel = $('section-trends');
  const months = DATA.monthly;
  const labels = months.map(m => m.month);
  const markers = DATA.blood_draws.map(d => ({ x: d, label: 'Blood draw ' + d }));

  panel.innerHTML = `<div class="note">Monthly averages of the combined daily signal.
    Dashed vertical lines = blood draw dates.</div>`;
  const grid = document.createElement('div');
  grid.className = 'metric-grid';
  panel.appendChild(grid);

  function trendCard(title, field, color, unit, decimals) {
    const values = months.map(m => m[field]);
    let last = values.length - 1;
    while (last >= 0 && values[last] == null) last--;
    const fmt = v => d3.format(',')(+v.toFixed(decimals));
    const card = document.createElement('div');
    card.className = 'metric-card';
    card.innerHTML = `
      <div class="metric-card-header"><div class="metric-card-title">${title}</div></div>
      <div class="metric-card-val" style="color:${color}">${last >= 0 ? fmt(values[last]) : '—'}</div>
      <div class="metric-card-sub">${unit}${last >= 0 ? ' · ' + AR.fmtDate(labels[last]) + ' avg' : ''}</div>
      <div class="chart-area"></div>`;
    grid.appendChild(card);
    AR.lineChart(card.querySelector('.chart-area'), {
      x: labels, y: values, color, height: 170, markers,
      yFmt: v => d3.format('~s')(+v.toFixed(2)),
      tip: i => `<b>${AR.fmtDate(labels[i])}</b><br>${fmt(values[i])} <span class="dim">${unit}</span>`,
    });
  }

  // 1. Steps + training hours
  trendCard('Daily Steps (monthly avg)', 'steps', 'var(--blue)', 'steps/day', 0);
  trendCard('Training Hours', 'train_hrs', 'var(--orange)', 'training hrs/day', 2);
  // 2. Sleep
  trendCard('Sleep Hours — combined (WHOOP preferred, Garmin fallback)', 'sleep_hrs', 'var(--purple)', 'hours', 2);
  trendCard('Sleep Score', 'sleep_score', 'var(--pink)', 'score (0–100)', 1);
  // 3. HRV + Recovery
  trendCard('WHOOP: HRV', 'hrv_ms', 'var(--teal)', 'HRV rMSSD (ms)', 1);
  trendCard('WHOOP: Recovery Score', 'whoop_recovery', 'var(--blue)', 'recovery (0–100)', 1);
  // 4. RHR + Strain
  trendCard('Resting HR (WHOOP)', 'rhr', 'var(--red)', 'RHR (bpm)', 1);
  trendCard('WHOOP Strain', 'whoop_strain', 'var(--orange)', 'strain (0–21)', 1);
  // 5. Garmin stress + body battery
  trendCard('Garmin: Stress', 'garmin_stress', 'var(--yellow)', 'stress (0–100)', 1);
  trendCard('Garmin: Body Battery', 'body_battery_hi', 'var(--green)', 'body battery high', 1);
  // 6. SpO2 + resp rate
  trendCard('SpO₂', 'spo2', 'var(--blue)', 'SpO₂ (%)', 1);
  trendCard('Respiration Rate', 'resp_rate', 'var(--yellow)', 'breaths/min', 1);
})();

// ── SOURCE COVERAGE ───────────────────────────────────────────────────────
(function () {
  const panel = $('section-coverage');
  const months = DATA.monthly;
  const labels = months.map(m => m.month);

  const sec = mkSection(panel, 'Data Coverage by Source Over Time',
    'Shows how many days per month have data from each source. Identifies gaps and source transitions.');

  // One row per source on the same month axis (small multiples).
  const rows = [
    ['WHOOP (HRV)',            '_n_hrv_ms',    'var(--pink)'],
    ['Sleep (WHOOP or Garmin)', '_n_sleep_hrs', 'var(--purple)'],
    ['Garmin (steps)',         '_n_steps',     'var(--blue)'],
    ['Strava',                 '_n_train_hrs', 'var(--orange)'],
  ];
  sec.insertAdjacentHTML('beforeend', `<div class="metric-grid wide"><div class="metric-card">
    <div class="metric-card-header"><div class="metric-card-title">Monthly data coverage (days/month with data)</div></div>
    ${rows.map(([name, , color], i) => `<div class="cov-row">
      <div class="cov-label"><span class="sw" style="background:${color}"></span>${name}</div>
      <div class="chart-area" id="cov-chart-${i}"></div></div>`).join('')}
  </div></div>`);
  rows.forEach(([name, key, color], i) => {
    const values = months.map(m => m[key] || 0);
    AR.barChart('#cov-chart-' + i, {
      labels, values, color, height: 96, yTicks: 3,
      yFmt: v => v,
      tip: j => `<b>${AR.fmtDate(labels[j])}</b><br><span class="dim">${name}</span> ${values[j]} days`,
    });
  });

  // Summary stats
  const sumSec = mkSection(panel, 'Summary', null);
  const sm = DATA.summary;
  sumSec.insertAdjacentHTML('beforeend', `<div class="kpi-strip wide-tiles">${[
    ['Garmin steps', sm.garmin_step_days + ' days', '2015 – today'],
    ['WHOOP HRV',    sm.whoop_hrv_days + ' days',   '2020-10 – today'],
    ['Strava',       sm.strava_days + ' days',       '2011 – today'],
    ['Blood draws',  DATA.blood_draws.length + ' draws', '2013 – today'],
    ['Full overlap (all 3)', sm.full_overlap_days + ' days', 'WHOOP + Garmin + Strava'],
  ].map(([l, v, sub]) => `
    <div class="kpi-card" style="--kpi-color:var(--accent)">
      <div class="kpi-label">${l}</div>
      <div class="kpi-value">${v}</div>
      <div class="kpi-sub">${sub}</div>
    </div>`).join('')}</div>`);
})();

show(location.hash.slice(1) || 'compare');
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
