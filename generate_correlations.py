#!/usr/bin/env python3
"""
Generate correlations.html — standalone analysis page.

Data sources:
  - bloodwork_data.yaml  → biomarker values per draw
  - fitness_data.yaml    → VO₂max, weight
  - strava.db            → training load windows before each draw
  - whoop.db             → sleep / recovery / HRV windows before each draw

For each blood-draw date we build 30-day and 90-day rolling averages of
every wearable / training signal.

Statistics used:
  - Raw Pearson r (between covariate and biomarker)
  - Age-adjusted partial r (residuals after regressing both variables on age)

Partial correlation is the right tool here: with n=6–17 data points we
can't fit multivariate models without severe overfitting, but we *can*
remove the confounding linear trend of age from both sides before
computing r.  This answers "does sleep predict hsCRP beyond what aging
alone explains?" rather than "does everything that correlates with age
also correlate with each other?"
"""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

import yaml

import shell
from ranges import resolve_active

BLOODWORK_FILE = os.path.join(os.path.dirname(__file__), "bloodwork_data.yaml")
FITNESS_FILE   = os.path.join(os.path.dirname(__file__), "fitness_data.yaml")
HTML_FILE      = os.path.join(os.path.dirname(__file__), "correlations.html")

STRAVA_DB  = os.path.expanduser("~/projects/2026/strava-database/strava.db")
WHOOP_DB   = os.path.expanduser("~/projects/2026/whoop-database/whoop.db")
GARMIN_DB  = os.path.expanduser("~/projects/2026/garmin-database/garmin.db")

# Lab-measured VO₂max anchors (date, value)
LAB_VO2MAX = [
    ("2023-06-21", 55.6),  # UVM Lab indirect calorimetry
    ("2025-02-15", 60.6),  # UMass Empatica study
]

BIRTH_YEAR = 1989

LIFE_EVENTS = [
    {"date": "2017-10-01", "label": "Kid #1", "kids": 1},
    {"date": "2019-04-01", "label": "Kid #2", "kids": 2},
    {"date": "2021-07-01", "label": "Kid #3", "kids": 3},
]

BIOMARKERS_FOR_CORR = [
    "Glucose", "HbA1c", "Cholesterol", "LDL", "HDL", "Triglycerides",
    "Testosterone", "Free Testosterone", "SHBG", "Cortisol",
    "Vitamin D", "Ferritin", "hsCRP", "Homocysteine",
    "AST", "ALT", "GGT", "Hemoglobin", "Hematocrit",
    "TSH", "IGF-1", "DHEA-S", "Apolipoprotein B", "Lipoprotein(a)",
]

# Pairs shown as scatter plots
SCATTER_PAIRS = [
    ("Glucose",       "HbA1c"),
    ("Cholesterol",   "LDL"),
    ("HDL",           "Triglycerides"),
    ("Testosterone",  "SHBG"),
    ("AST",           "ALT"),
    ("Ferritin",      "Hemoglobin"),
    ("Testosterone",  "Cortisol"),
    ("Vitamin D",     "Testosterone"),
]

# Wearable / training covariates that we can correlate against biomarkers
# Each entry: (key, label, higher_is, source)
WEARABLE_COVARIATES = [
    # WHOOP 30-day
    ("whoop_hrv_30d",         "HRV 30d avg (ms)",           "+", "WHOOP"),
    ("whoop_recovery_30d",    "Recovery score 30d avg",      "+", "WHOOP"),
    ("whoop_rhr_30d",         "Resting HR 30d avg (bpm)",    "-", "WHOOP"),
    ("whoop_strain_30d",      "Day strain 30d avg",          "?", "WHOOP"),
    ("whoop_sleep_hrs_30d",   "Sleep hours 30d avg",         "+", "WHOOP"),
    ("whoop_sleep_perf_30d",  "Sleep performance 30d avg",   "+", "WHOOP"),
    ("whoop_spo2_30d",        "SpO₂ 30d avg (%)",            "+", "WHOOP"),
    ("whoop_resp_30d",        "Resp rate 30d avg (br/min)",  "?", "WHOOP"),
    ("whoop_skin_temp_30d",   "Skin temp 30d avg (°C)",      "?", "WHOOP"),
    # WHOOP 90-day
    ("whoop_hrv_90d",         "HRV 90d avg (ms)",            "+", "WHOOP"),
    ("whoop_recovery_90d",    "Recovery score 90d avg",      "+", "WHOOP"),
    ("whoop_rhr_90d",         "Resting HR 90d avg (bpm)",    "-", "WHOOP"),
    ("whoop_strain_90d",      "Day strain 90d avg",          "?", "WHOOP"),
    ("whoop_sleep_hrs_90d",   "Sleep hours 90d avg",         "+", "WHOOP"),
    ("whoop_sleep_perf_90d",  "Sleep performance 90d avg",   "+", "WHOOP"),
    # Strava 90-day
    ("strava_hrs_90d",        "Training hours 90d",          "?", "Strava"),
    ("strava_run_miles_90d",  "Run miles 90d",               "?", "Strava"),
    ("strava_ride_hrs_90d",   "Ride hours 90d",              "?", "Strava"),
    ("strava_run_hrs_90d",    "Run hours 90d",               "?", "Strava"),
    ("strava_avg_hr_90d",     "Avg workout HR 90d (bpm)",    "?", "Strava"),
    ("strava_avg_watts_90d",  "Avg cycling watts 90d",       "+", "Strava"),
    # Garmin 90-day
    ("garmin_steps_90d",      "Steps 90d avg",               "+", "Garmin"),
    ("garmin_stress_90d",     "Stress 90d avg",              "-", "Garmin"),
    ("garmin_bb_hi_90d",      "Body battery hi 90d avg",     "+", "Garmin"),
    ("garmin_intensity_90d",  "Intensity mins 90d avg",      "+", "Garmin"),
    # Classic covariates
    ("age",                   "Age (years)",                 "?", "Life"),
    ("kids",                  "# Kids",                      "?", "Life"),
    ("weight",                "Weight (lbs)",                "?", "Life"),
    ("vo2max",                "VO₂max (ml/kg/min)",          "+", "Fitness"),
]


# ---------------------------------------------------------------------------
# VO₂max estimation
# ---------------------------------------------------------------------------

def vdot_from_race(dist_m: float, time_s: float) -> float | None:
    """Jack Daniels VDOT formula. Returns VO₂max equivalent."""
    import math
    if time_s <= 0 or dist_m <= 0:
        return None
    t = time_s / 60.0          # minutes
    v = dist_m / t             # m/min
    pct_max = (0.8 + 0.1894393 * math.exp(-0.012778 * t)
                   + 0.2989558 * math.exp(-0.1932605 * t))
    vo2_at_pace = -4.60 + 0.182258 * v + 0.000104 * v ** 2
    if pct_max <= 0:
        return None
    return vo2_at_pace / pct_max


def build_vo2max_series(fitness: dict, strava_db: str) -> list[dict]:
    """
    Build a dense estimated VO₂max series combining:
      1. Lab measurements (exact anchors)
      2. VDOT from marathon / 5K times in fitness_data.yaml
      3. FTP w/kg via Coggan formula, calibrated to lab anchors

    Returns list of {date, value, source, method} sorted by date.
    """
    import math, sqlite3

    points: list[dict] = []

    # -- 1. Lab anchors (highest trust) --
    for date_str, val in LAB_VO2MAX:
        points.append({"date": date_str, "value": val,
                        "source": "Lab", "method": "lab"})

    # -- 2. VDOT from stored race times --
    MARATHON_M = 42195.0
    HALF_M     = 21097.5

    for entry in fitness.get("marathon_times", []):
        secs = entry.get("time_seconds", 0)
        dist = entry.get("dist_m", MARATHON_M)  # default marathon
        # skip ultras / very slow efforts (>5h for marathon distance)
        if dist == MARATHON_M and secs > 5 * 3600:
            continue
        if dist == HALF_M and secs > 2.5 * 3600:
            continue
        vd = vdot_from_race(dist, secs)
        if vd and 30 < vd < 80:
            points.append({"date": entry["date"], "value": round(vd, 1),
                            "source": entry.get("race", "Race"),
                            "method": "vdot_marathon"})

    for entry in fitness.get("5k_times", []):
        secs = entry.get("time_seconds", 0)
        vd = vdot_from_race(5000, secs)
        if vd and 30 < vd < 80:
            points.append({"date": entry["date"], "value": round(vd, 1),
                            "source": entry.get("race", "5K"),
                            "method": "vdot_5k"})

    # -- 3. Best race efforts from Strava (workout_type=1 race) --
    if os.path.exists(strava_db):
        conn = sqlite3.connect(strava_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Get best effort per year-month at classic distances
        cur.execute("""
            SELECT date(start_date_local) as dt,
                   distance_m, moving_time_s, name,
                   sport_type
            FROM activities
            WHERE sport_type IN ('Run','VirtualRun','TrailRun')
              AND moving_time_s > 0
              AND distance_m > 3000
              AND manual = 0
            ORDER BY dt
        """)
        rows = cur.fetchall()
        conn.close()

        # Per month keep the fastest pace effort at each standard distance band
        from collections import defaultdict
        monthly_best: dict[str, dict] = defaultdict(lambda: {
            "5k": None, "10k": None, "hm": None, "fm": None,
        })
        for r in rows:
            ym = r["dt"][:7]
            d, t = r["distance_m"], r["moving_time_s"]
            # 5K band
            if 4600 <= d <= 5400:
                cur_best = monthly_best[ym]["5k"]
                if cur_best is None or t < cur_best[1]:
                    monthly_best[ym]["5k"] = (r["dt"], t, d)
            # 10K band
            if 9500 <= d <= 10500:
                cur_best = monthly_best[ym]["10k"]
                if cur_best is None or t < cur_best[1]:
                    monthly_best[ym]["10k"] = (r["dt"], t, d)
            # Half marathon band
            if 20000 <= d <= 22000:
                cur_best = monthly_best[ym]["hm"]
                if cur_best is None or t < cur_best[1]:
                    monthly_best[ym]["hm"] = (r["dt"], t, d)
            # Marathon band (not ultra)
            if 41000 <= d <= 43500:
                cur_best = monthly_best[ym]["fm"]
                if cur_best is None or t < cur_best[1]:
                    monthly_best[ym]["fm"] = (r["dt"], t, d)

        for ym, bests in sorted(monthly_best.items()):
            for band, entry in bests.items():
                if entry is None:
                    continue
                dt, t, d = entry
                vd = vdot_from_race(d, t)
                if vd and 30 < vd < 80:
                    points.append({"date": dt, "value": round(vd, 1),
                                   "source": f"Strava {band.upper()} best",
                                   "method": f"vdot_{band}"})

    # -- 4. FTP-based estimates --
    weight_lookup = make_prior_lookup(fitness.get("weight_lbs", []))
    for entry in fitness.get("ftp_watts", []):
        d = entry["date"]
        w_lbs = weight_lookup(d)
        if w_lbs is None:
            continue
        w_kg   = w_lbs * 0.453592
        w_per_kg = entry["value"] / w_kg
        # Coggan: VO₂max ≈ (w/kg × 10.8) + 7
        ftp_est = w_per_kg * 10.8 + 7
        points.append({"date": d, "value": round(ftp_est, 1),
                        "source": entry.get("source", "FTP"),
                        "method": "ftp_coggan"})

    # -- Calibrate non-lab estimates against lab anchors --
    # Strategy: for each lab anchor, find the FTP/VDOT estimates within ±180 days,
    # compute a per-method bias correction, then apply globally.
    lab_dates  = [p["date"] for p in points if p["method"] == "lab"]
    lab_vals   = {p["date"]: p["value"] for p in points if p["method"] == "lab"}

    if len(lab_dates) >= 2:
        # Two lab anchors: compute per-method scale factor at each anchor date,
        # then linearly interpolate/extrapolate for points between/outside them.
        lab_sorted = sorted(lab_dates)
        lab_dt = [datetime.strptime(d, "%Y-%m-%d") for d in lab_sorted]

        from collections import defaultdict
        # For each anchor, compute per-method ratio using points within ±120 days
        anchor_ratios: list[dict[str, float]] = []  # one dict per anchor
        for ld in lab_sorted:
            ld_dt = datetime.strptime(ld, "%Y-%m-%d")
            ratios: dict[str, list] = defaultdict(list)
            for p in points:
                if p["method"] == "lab":
                    continue
                gap = abs((datetime.strptime(p["date"], "%Y-%m-%d") - ld_dt).days)
                if gap <= 120:
                    ratios[p["method"]].append(lab_vals[ld] / p["value"])
            anchor_ratios.append({m: sum(v)/len(v) for m, v in ratios.items()})

        def interp_scale(method: str, date_str: str) -> float:
            """Linearly interpolate per-method scale between the two lab anchors."""
            p_dt = datetime.strptime(date_str, "%Y-%m-%d")
            r0 = anchor_ratios[0].get(method)
            r1 = anchor_ratios[1].get(method)
            if r0 is None and r1 is None:
                return 1.0
            if r0 is None:
                return r1
            if r1 is None:
                return r0
            # Linear interpolation
            span = (lab_dt[1] - lab_dt[0]).days
            t = (p_dt - lab_dt[0]).days / span if span > 0 else 0.5
            t = max(0.0, min(1.0, t))  # clamp — don't extrapolate wildly
            return r0 + t * (r1 - r0)

        for p in points:
            if p["method"] != "lab":
                scale = interp_scale(p["method"], p["date"])
                p["value"] = round(p["value"] * scale, 1)
                p["calibrated"] = True

    elif len(lab_dates) == 1:
        # Single anchor: compute per-method ratio within ±180 days
        ld = lab_dates[0]
        ld_dt = datetime.strptime(ld, "%Y-%m-%d")
        from collections import defaultdict
        method_ratios: dict[str, list] = defaultdict(list)
        for p in points:
            if p["method"] == "lab":
                continue
            gap = abs((datetime.strptime(p["date"], "%Y-%m-%d") - ld_dt).days)
            if gap <= 180:
                method_ratios[p["method"]].append(lab_vals[ld] / p["value"])
        method_scale = {m: sum(v)/len(v) for m, v in method_ratios.items()}
        for p in points:
            if p["method"] != "lab":
                scale = method_scale.get(p["method"], 1.0)
                p["value"] = round(p["value"] * scale, 1)
                p["calibrated"] = True

    # Sort and deduplicate: per date keep highest-trust value
    # Trust order: lab > vdot_marathon > vdot_5k > vdot_hm > vdot_fm > vdot_10k > ftp_coggan
    METHOD_TRUST = {
        "lab": 0, "vdot_marathon": 1, "vdot_5k": 2,
        "vdot_hm": 3, "vdot_fm": 4, "vdot_10k": 5, "ftp_coggan": 6,
    }
    points.sort(key=lambda p: (p["date"], METHOD_TRUST.get(p["method"], 9)))

    # Keep all points but flag them — the caller can choose how to use them
    return points


# ---------------------------------------------------------------------------
# Maths helpers
# ---------------------------------------------------------------------------

def pearson_r(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx  = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy  = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def residuals(xs: list[float], ys: list[float]) -> list[float]:
    """OLS residuals of ys regressed on xs. Returns ys unchanged if regression fails."""
    reg = ols(xs, ys)
    if reg is None:
        return list(ys)
    slope, intercept = reg
    return [y - (slope * x + intercept) for x, y in zip(xs, ys)]


def partial_r(xs: list[float], ys: list[float],
              control: list[float]) -> float | None:
    """
    Partial Pearson r between xs and ys after linearly controlling for `control`.

    Method: regress both xs and ys on `control`, then correlate the residuals.
    This removes the shared linear trend in `control` from both variables before
    computing their correlation — equivalent to the textbook partial-r formula.

    Returns None if n < 4 or variance is zero.
    """
    n = len(xs)
    if n < 4 or len(ys) != n or len(control) != n:
        return None
    rx = residuals(control, xs)
    ry = residuals(control, ys)
    return pearson_r(rx, ry)


def ols(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = num / den
    return slope, my - slope * mx


def make_prior_lookup(series: list[dict], value_key: str = "value"):
    """Return fn(date_str) -> float | None, last known value on or before date."""
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
# Data loading
# ---------------------------------------------------------------------------

def load_bloodwork(path: str) -> tuple[dict, dict]:
    with open(path, "r", encoding="utf-8") as f:
        bw = yaml.safe_load(f)
    ref_ranges = resolve_active(bw["reference_ranges"])
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


def load_whoop_windows(db_path: str, draw_dates: list[str]) -> dict[str, dict]:
    """
    For each draw date compute 30-day and 90-day averages of WHOOP metrics.
    Returns {draw_date: {feature: value|None, ...}, ...}
    """
    if not os.path.exists(db_path):
        return {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    result = {}
    for draw in draw_dates:
        row_data: dict[str, float | None] = {}
        for days, suffix in [(30, "30d"), (90, "90d")]:
            since = (datetime.strptime(draw, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
            cur.execute("""
                SELECT
                    AVG(hrv_rmssd_ms)                          AS hrv,
                    AVG(recovery_score)                        AS recovery,
                    AVG(resting_hr)                            AS rhr,
                    AVG(strain)                                AS strain,
                    AVG(sleep_total_in_bed_ms) / 3600000.0     AS sleep_hrs,
                    AVG(sleep_performance_pct)                 AS sleep_perf,
                    AVG(spo2_pct)                              AS spo2,
                    AVG(respiratory_rate)                      AS resp,
                    AVG(skin_temp_celsius)                     AS skin_temp,
                    COUNT(*)                                   AS n_days
                FROM daily
                WHERE date >= ? AND date < ?
                  AND hrv_rmssd_ms IS NOT NULL
            """, (since, draw))
            r = cur.fetchone()
            if r and r["n_days"] and r["n_days"] >= 7:
                row_data[f"whoop_hrv_{suffix}"]        = r["hrv"]
                row_data[f"whoop_recovery_{suffix}"]   = r["recovery"]
                row_data[f"whoop_rhr_{suffix}"]        = r["rhr"]
                row_data[f"whoop_strain_{suffix}"]     = r["strain"]
                row_data[f"whoop_sleep_hrs_{suffix}"]  = r["sleep_hrs"]
                row_data[f"whoop_sleep_perf_{suffix}"] = r["sleep_perf"]
                if suffix == "30d":
                    row_data["whoop_spo2_30d"]       = r["spo2"]
                    row_data["whoop_resp_30d"]        = r["resp"]
                    row_data["whoop_skin_temp_30d"]   = r["skin_temp"]
            else:
                for key in [f"whoop_hrv_{suffix}", f"whoop_recovery_{suffix}",
                            f"whoop_rhr_{suffix}", f"whoop_strain_{suffix}",
                            f"whoop_sleep_hrs_{suffix}", f"whoop_sleep_perf_{suffix}"]:
                    row_data[key] = None
                if suffix == "30d":
                    row_data["whoop_spo2_30d"]      = None
                    row_data["whoop_resp_30d"]       = None
                    row_data["whoop_skin_temp_30d"]  = None
        result[draw] = row_data

    conn.close()
    return result


def load_strava_windows(db_path: str, draw_dates: list[str]) -> dict[str, dict]:
    """
    For each draw date compute 90-day Strava training load features.
    Returns {draw_date: {feature: value, ...}, ...}
    """
    if not os.path.exists(db_path):
        return {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    result = {}
    for draw in draw_dates:
        since = (datetime.strptime(draw, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
        cur.execute("""
            SELECT
                SUM(moving_time_s) / 3600.0                                   AS total_hrs,
                SUM(CASE WHEN sport_type IN ('Run','TrailRun','VirtualRun')
                         THEN distance_m * 0.000621371 ELSE 0 END)            AS run_miles,
                SUM(CASE WHEN sport_type IN ('Run','TrailRun','VirtualRun')
                         THEN moving_time_s / 3600.0 ELSE 0 END)              AS run_hrs,
                SUM(CASE WHEN sport_type IN ('Ride','VirtualRide','GravelRide',
                         'MountainBikeRide','EBikeRide')
                         THEN moving_time_s / 3600.0 ELSE 0 END)              AS ride_hrs,
                AVG(CASE WHEN average_heartrate > 0
                         THEN average_heartrate END)                           AS avg_hr,
                AVG(CASE WHEN average_watts > 0 AND device_watts = 1
                         THEN average_watts END)                               AS avg_watts,
                COUNT(*)                                                        AS n_activities
            FROM activities
            WHERE date(start_date_local) >= ?
              AND date(start_date_local) <  ?
        """, (since, draw))
        r = cur.fetchone()
        if r and r["n_activities"] and r["n_activities"] >= 3:
            result[draw] = {
                "strava_hrs_90d":       r["total_hrs"],
                "strava_run_miles_90d": r["run_miles"],
                "strava_run_hrs_90d":   r["run_hrs"],
                "strava_ride_hrs_90d":  r["ride_hrs"],
                "strava_avg_hr_90d":    r["avg_hr"],
                "strava_avg_watts_90d": r["avg_watts"],
            }
        else:
            result[draw] = {k: None for k in [
                "strava_hrs_90d", "strava_run_miles_90d", "strava_run_hrs_90d",
                "strava_ride_hrs_90d", "strava_avg_hr_90d", "strava_avg_watts_90d",
            ]}

    conn.close()
    return result


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def load_garmin_windows(db_path: str, draw_dates: list[str]) -> dict[str, dict]:
    """90-day averages of Garmin daily metrics before each blood draw."""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()
    result = {}
    for draw in draw_dates:
        since = (datetime.strptime(draw, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
        cur.execute("""
            SELECT AVG(total_steps)                                          AS steps,
                   AVG(avg_stress_level)                                     AS stress,
                   AVG(body_battery_highest)                                 AS bb_hi,
                   AVG(moderate_intensity_mins + vigorous_intensity_mins*2)  AS intensity,
                   COUNT(*)                                                   AS n_days
            FROM daily
            WHERE date >= ? AND date < ?
              AND total_steps IS NOT NULL AND total_steps > 0
        """, (since, draw))
        r = cur.fetchone()
        if r and r["n_days"] and r["n_days"] >= 7:
            result[draw] = {
                "garmin_steps_90d":     r["steps"],
                "garmin_stress_90d":    r["stress"],
                "garmin_bb_hi_90d":     r["bb_hi"],
                "garmin_intensity_90d": r["intensity"],
            }
        else:
            result[draw] = {k: None for k in [
                "garmin_steps_90d", "garmin_stress_90d",
                "garmin_bb_hi_90d", "garmin_intensity_90d",
            ]}
    conn.close()
    return result


def build_payload(measurements: dict, fitness: dict,
                  whoop_windows: dict, strava_windows: dict, garmin_windows: dict,
                  vo2max_series: list[dict]) -> dict:

    weight_lookup = make_prior_lookup(fitness.get("weight_lbs", []))

    # Build a best-estimate VO₂max lookup from the rich series.
    # Per date keep the highest-trust (lowest METHOD_TRUST) value; then
    # make_prior_lookup will give the nearest prior value for any draw date.
    METHOD_TRUST = {
        "lab": 0, "vdot_marathon": 1, "vdot_5k": 2,
        "vdot_hm": 3, "vdot_fm": 4, "vdot_10k": 5, "ftp_coggan": 6,
    }
    # Collapse to one best point per date
    best_by_date: dict[str, dict] = {}
    for p in vo2max_series:
        d = p["date"]
        if d not in best_by_date or (METHOD_TRUST.get(p["method"], 9) <
                                      METHOD_TRUST.get(best_by_date[d]["method"], 9)):
            best_by_date[d] = p
    vo2_dense = sorted(best_by_date.values(), key=lambda p: p["date"])
    vo2_lookup = make_prior_lookup(vo2_dense)

    # All blood-draw dates
    all_dates = sorted({pt["date"] for pts in measurements.values() for pt in pts})

    # date → biomarker value dicts
    bm_date_val: dict[str, dict[str, float]] = {
        bm: {pt["date"]: pt["value"] for pt in measurements[bm]}
        for bm in BIOMARKERS_FOR_CORR
        if bm in measurements
    }

    # Build full covariate row per draw date
    def covariate_row(d: str) -> dict:
        row: dict[str, float | None] = {
            "date":   d,
            "age":    int(d[:4]) - BIRTH_YEAR,
            "kids":   kids_count_at(d),
            "weight": weight_lookup(d),
            "vo2max": vo2_lookup(d),
        }
        row.update(whoop_windows.get(d, {}))
        row.update(strava_windows.get(d, {}))
        row.update(garmin_windows.get(d, {}))
        return row

    cov_rows = {d: covariate_row(d) for d in all_dates}

    covariate_keys = [c[0] for c in WEARABLE_COVARIATES]

    # ------------------------------------------------------------------
    # 1. Biomarker × covariate correlation matrix
    #    For each pair: raw r AND age-adjusted partial r
    # ------------------------------------------------------------------
    biomarker_matrix = []
    for bm in BIOMARKERS_FOR_CORR:
        data = measurements.get(bm)
        if not data or len(data) < 3:
            continue

        cov_rs:      dict[str, float | None] = {}  # raw r
        cov_partial: dict[str, float | None] = {}  # age-adjusted partial r
        bm_vals_by_date = {pt["date"]: pt["value"] for pt in data}

        for ckey in covariate_keys:
            min_n = 4 if ckey in ("age", "kids", "weight", "vo2max") else 5
            # Shared dates where we have both the covariate AND the biomarker
            shared = [
                d for d in all_dates
                if d in bm_vals_by_date and cov_rows[d].get(ckey) is not None
            ]
            if len(shared) < min_n:
                cov_rs[ckey]      = None
                cov_partial[ckey] = None
                continue

            cov_vals = [cov_rows[d][ckey]      for d in shared]
            bm_vals  = [bm_vals_by_date[d]     for d in shared]
            age_vals = [cov_rows[d]["age"]      for d in shared]

            r = pearson_r(cov_vals, bm_vals)
            cov_rs[ckey] = round(r, 3) if r is not None else None

            # Age-adjusted: skip if ckey IS age, or if n < 5 (need df for residuals)
            if ckey == "age" or len(shared) < 5:
                cov_partial[ckey] = None
            else:
                pr = partial_r(cov_vals, bm_vals, age_vals)
                cov_partial[ckey] = round(pr, 3) if pr is not None else None

        biomarker_matrix.append({
            "name":    bm,
            "n":       len(data),
            "r":       cov_rs,       # {ckey: raw_r}
            "partial": cov_partial,  # {ckey: age_adj_r}
            # keep flat copies of classic covariates for backward compat in JS
            **{k: cov_rs.get(k) for k in ["age", "kids", "weight", "vo2max"]},
        })

    # Sort by max |raw r| across classic covariates
    biomarker_matrix.sort(
        key=lambda x: max(abs(x.get(k) or 0) for k in ["age", "kids", "weight", "vo2max"]),
        reverse=True,
    )

    # ------------------------------------------------------------------
    # 2. Top wearable × biomarker correlations — raw AND age-adjusted
    # ------------------------------------------------------------------
    all_cov_bm_pairs = []
    for bm in BIOMARKERS_FOR_CORR:
        if bm not in bm_date_val:
            continue
        bm_vals_by_date = bm_date_val[bm]
        for ckey, clabel, _, csource in WEARABLE_COVARIATES:
            min_n = 4 if ckey in ("age", "kids", "weight", "vo2max") else 5
            shared = [
                d for d in all_dates
                if bm_vals_by_date.get(d) is not None and cov_rows[d].get(ckey) is not None
            ]
            if len(shared) < min_n:
                continue

            cov_vals = [cov_rows[d][ckey]  for d in shared]
            bm_vals  = [bm_vals_by_date[d] for d in shared]
            age_vals = [cov_rows[d]["age"]  for d in shared]

            r = pearson_r(cov_vals, bm_vals)
            if r is None:
                continue

            # Age-adjusted partial r (skip for age itself, and need n>=5)
            pr = None
            if ckey != "age" and len(shared) >= 5:
                pr = partial_r(cov_vals, bm_vals, age_vals)

            all_cov_bm_pairs.append({
                "biomarker":   bm,
                "covariate":   clabel,
                "cov_key":     ckey,
                "source":      csource,
                "r":           round(r, 3),
                "partial_r":   round(pr, 3) if pr is not None else None,
                "n":           len(shared),
            })

    # Raw ranking
    all_cov_bm_pairs.sort(key=lambda x: abs(x["r"]), reverse=True)
    top_cov_bm = all_cov_bm_pairs[:30]

    # Age-adjusted ranking (only pairs where partial_r exists)
    top_cov_bm_adj = sorted(
        [x for x in all_cov_bm_pairs if x["partial_r"] is not None],
        key=lambda x: abs(x["partial_r"]),
        reverse=True,
    )[:30]

    # ------------------------------------------------------------------
    # 3. Top pairwise biomarker–biomarker correlations
    # ------------------------------------------------------------------
    frequent = [bm for bm in BIOMARKERS_FOR_CORR
                if bm in bm_date_val and len(bm_date_val[bm]) >= 6]

    top_bm_pairs = []
    for i in range(len(frequent)):
        for j in range(i + 1, len(frequent)):
            a, b = frequent[i], frequent[j]
            shared = sorted(bm_date_val[a].keys() & bm_date_val[b].keys())
            if len(shared) < 4:
                continue
            r = pearson_r([bm_date_val[a][d] for d in shared],
                          [bm_date_val[b][d] for d in shared])
            if r is None:
                continue
            top_bm_pairs.append({
                "pair": f"{a} × {b}", "bm_a": a, "bm_b": b,
                "r": round(r, 3), "n": len(shared),
            })

    top_bm_pairs.sort(key=lambda x: abs(x["r"]), reverse=True)
    top_bm_pairs = top_bm_pairs[:20]

    # ------------------------------------------------------------------
    # 4. Scatter data for selected biomarker pairs
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
        r   = pearson_r(xs, ys)
        reg = ols(xs, ys)
        x_min, x_max = min(xs), max(xs)
        reg_line = None
        if reg:
            s, ic = reg
            reg_line = [{"x": x_min, "y": round(s * x_min + ic, 4)},
                        {"x": x_max, "y": round(s * x_max + ic, 4)}]
        scatter_pairs.append({
            "x_label": bm_a, "y_label": bm_b,
            "r": round(r, 3) if r is not None else None,
            "n": len(shared),
            "points":   [{"x": x, "y": y, "date": d} for x, y, d in zip(xs, ys, shared)],
            "reg_line": reg_line,
        })

    # ------------------------------------------------------------------
    # 5. Wearable scatter: pick the 6 strongest wearable × biomarker pairs
    #    that have enough data and plot them
    # ------------------------------------------------------------------
    wearable_scatters = []
    seen_pairs: set[tuple] = set()
    for item in top_cov_bm:
        pair_key = (item["cov_key"], item["biomarker"])
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        bm = item["biomarker"]
        ckey = item["cov_key"]
        pts = [
            {"x": cov_rows[d].get(ckey), "y": bm_date_val[bm].get(d), "date": d}
            for d in all_dates
            if bm_date_val.get(bm, {}).get(d) is not None and cov_rows[d].get(ckey) is not None
        ]
        if len(pts) < 3:
            continue
        xs = [p["x"] for p in pts]
        ys = [p["y"] for p in pts]
        reg = ols(xs, ys)
        reg_line = None
        if reg:
            s, ic = reg
            x_min, x_max = min(xs), max(xs)
            reg_line = [{"x": x_min, "y": round(s * x_min + ic, 4)},
                        {"x": x_max, "y": round(s * x_max + ic, 4)}]
        wearable_scatters.append({
            "x_label": item["covariate"],
            "y_label": bm,
            "source":  item["source"],
            "r":       item["r"],
            "n":       len(pts),
            "points":  pts,
            "reg_line": reg_line,
        })
        if len(wearable_scatters) >= 8:
            break

    # ------------------------------------------------------------------
    # 6. Timeline rows for charts
    # ------------------------------------------------------------------
    timeline = []
    for d in all_dates:
        row = dict(cov_rows[d])
        for bm in BIOMARKERS_FOR_CORR:
            v = bm_date_val.get(bm, {}).get(d)
            if v is not None:
                row[bm] = v
        timeline.append(row)

    # ------------------------------------------------------------------
    # 7. Auto-generated insights
    # ------------------------------------------------------------------
    insights = _build_insights(biomarker_matrix, top_cov_bm_adj, top_cov_bm, top_bm_pairs)

    return {
        "biomarker_matrix":   biomarker_matrix,
        "covariate_meta":     [{"key": c[0], "label": c[1], "higher": c[2], "source": c[3]}
                               for c in WEARABLE_COVARIATES],
        "top_cov_bm":         top_cov_bm,
        "top_cov_bm_adj":     top_cov_bm_adj,
        "top_bm_pairs":       top_bm_pairs,
        "scatter_pairs":      scatter_pairs,
        "wearable_scatters":  wearable_scatters,
        "timeline":           timeline,
        "life_events":        LIFE_EVENTS,
        "insights":           insights,
        "vo2max_series":      vo2max_series,
        "generated":          date.today().isoformat(),
        "n_draws":            len(all_dates),
    }


def _build_insights(matrix: list, top_adj: list, top_raw: list, top_bm_pairs: list) -> list:
    """
    top_adj = age-adjusted ranked list (partial r)
    top_raw = raw r ranked list
    """
    insights = []

    def strongest(rows, key):
        valid = [r for r in rows if r.get(key) is not None]
        return max(valid, key=lambda r: abs(r[key]), default=None)

    # Best wearable predictor after age-adjustment
    if top_adj:
        item = top_adj[0]
        source_emoji = {"WHOOP": "⌚", "Strava": "🏃", "Life": "👶", "Fitness": "🫁"}.get(item["source"], "📊")
        insights.append({
            "emoji": source_emoji, "color": "var(--accent)",
            "title": (f"Strongest age-adjusted predictor: {item['covariate']} → {item['biomarker']} "
                      f"(partial r = {item['partial_r']:+.3f})"),
            "body":  (f"After removing the shared linear trend with age, {item['covariate']} "
                      f"({'WHOOP' if item['source']=='WHOOP' else item['source']}) still "
                      f"{'positively' if item['partial_r'] > 0 else 'negatively'} correlates with "
                      f"{item['biomarker']} across {item['n']} draws — this isn't just an age effect."),
        })

    # HRV insight — prefer age-adjusted
    hrv_adj = [x for x in top_adj if "hrv" in x["cov_key"].lower()]
    hrv_raw = [x for x in top_raw if "hrv" in x["cov_key"].lower()]
    if hrv_adj or hrv_raw:
        item = hrv_adj[0] if hrv_adj else hrv_raw[0]
        pr = item.get("partial_r")
        r  = item["r"]
        insights.append({
            "emoji": "💓", "color": "var(--blue)" if (pr or r) > 0 else "var(--red)",
            "title": (f"HRV → {item['biomarker']}  "
                      f"(r = {r:+.3f}" + (f", age-adj r = {pr:+.3f}" if pr else "") + ")"),
            "body":  (f"Higher HRV tracks with {'higher' if r > 0 else 'lower'} {item['biomarker']} "
                      f"across {item['n']} draws."
                      + (f" The relationship holds after controlling for age (partial r = {pr:+.3f})."
                         if pr and abs(pr) > 0.3 else
                         " But after controlling for age the signal weakens — some of this may be age-driven."
                         if pr else "")),
        })

    # Sleep insight — prefer age-adjusted
    sleep_adj = [x for x in top_adj if "sleep" in x["cov_key"].lower()]
    sleep_raw = [x for x in top_raw if "sleep" in x["cov_key"].lower()]
    if sleep_adj or sleep_raw:
        item = sleep_adj[0] if sleep_adj else sleep_raw[0]
        pr = item.get("partial_r")
        r  = item["r"]
        insights.append({
            "emoji": "😴", "color": "var(--teal)",
            "title": (f"Sleep → {item['biomarker']}  "
                      f"(r = {r:+.3f}" + (f", age-adj r = {pr:+.3f}" if pr else "") + ")"),
            "body":  (f"{item['covariate']} correlates with {'higher' if r > 0 else 'lower'} "
                      f"{item['biomarker']}."
                      + (f" Age-adjusted partial r = {pr:+.3f} — sleep has an independent effect."
                         if pr and abs(pr) > 0.3 else "")),
        })

    # Training load — prefer age-adjusted
    strava_adj = [x for x in top_adj if x["source"] == "Strava"]
    strava_raw = [x for x in top_raw if x["source"] == "Strava"]
    if strava_adj or strava_raw:
        item = strava_adj[0] if strava_adj else strava_raw[0]
        pr = item.get("partial_r")
        r  = item["r"]
        insights.append({
            "emoji": "🏃", "color": "var(--orange)",
            "title": (f"Training → {item['biomarker']}  "
                      f"(r = {r:+.3f}" + (f", age-adj r = {pr:+.3f}" if pr else "") + ")"),
            "body":  (f"{item['covariate']} (90-day window) "
                      f"{'positively' if r > 0 else 'negatively'} correlates with "
                      f"{item['biomarker']} across {item['n']} draws."
                      + (f" Age-adjusted: partial r = {pr:+.3f}." if pr else "")),
        })

    # Age trend — strongest raw-r with age
    row = strongest(matrix, "age")
    if row:
        r = row["age"]
        insights.append({
            "emoji": "📈", "color": "var(--blue)" if r > 0 else "var(--red)",
            "title": f"{row['name']} tracks age most strongly (r = {r:+.2f})",
            "body":  (f"{row['name']} {'rises' if r > 0 else 'falls'} with age. "
                      f"This is used as the control variable in age-adjusted correlations — "
                      f"so other findings above are already independent of this trend."),
        })

    # Strongest biomarker-biomarker pair
    if top_bm_pairs:
        tp = top_bm_pairs[0]
        insights.append({
            "emoji": "🔗", "color": "var(--accent)",
            "title": f"Strongest biomarker–biomarker link: {tp['pair']} (r = {tp['r']:+.3f})",
            "body":  (f"These two markers move {'together' if tp['r'] > 0 else 'in opposite directions'} "
                      f"across {tp['n']} shared blood draws. Note: biomarker pairs are not yet age-adjusted."),
        })

    return insights


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

PAGE_STYLE = r"""<style>
/* diverging r scale for the matrix: two hues, midpoint = the card surface.
   Cells mix towards these with color-mix(), so a theme switch needs no redraw. */
:root { --heat-pos: #3b82d9; --heat-neg: #e0524f; }
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) { --heat-pos: #2c6cc0; --heat-neg: #c2413f; }
}
:root[data-theme="dark"] { --heat-pos: #2c6cc0; --heat-neg: #c2413f; }

.topbar .meta { font-size: 0.75rem; color: var(--muted); }
.desc { color: var(--muted); font-size: 0.82rem; margin: -4px 0 14px; line-height: 1.6; max-width: 100ch; }
.metric-card.insight { border-top: 3px solid var(--kpi-color, var(--border)); }
.ic-emoji { font-size: 1.4rem; margin-bottom: 4px; }
.ic-title { font-size: 0.85rem; font-weight: 700; margin-bottom: 4px; }
.ic-body  { font-size: 0.8rem; color: var(--muted); line-height: 1.55; }
.metric-card-title .vs { color: var(--muted); font-weight: 400; text-transform: none; }
.metric-card-title .badge { margin-left: 4px; text-transform: none; letter-spacing: 0; }
.r-meta { text-align: right; flex-shrink: 0; }
.r-big  { font-size: 1.1rem; font-weight: 800; white-space: nowrap; }
.n-lbl  { font-size: 0.68rem; color: var(--muted); }
/* ranked bars keep a readable label column; narrow screens scroll sideways */
.rank-narrow { display: none; }
@media (max-width: 640px) { .rank-wide { display: none; } .rank-narrow { display: block; } }
.legend-item .sw.dot { border-radius: 50%; }
.legend-item .sw.dash { width: 0; height: 12px; border-left: 1px dashed var(--muted); border-radius: 0; }

/* heatmap */
table.heat { width: auto; border-collapse: separate; border-spacing: 1px; font-size: 0.72rem; }
table.heat thead th { background: var(--surface); border-bottom: 0; padding: 4px 2px; font-size: 0.7rem; }
table.heat thead th.grp { text-align: center; border-bottom: 2px solid var(--border); color: var(--text); }
table.heat thead th.vh { height: 150px; vertical-align: bottom; text-align: center; font-weight: 500; }
table.heat thead th.vh span { writing-mode: vertical-rl; transform: rotate(180deg); white-space: nowrap; }
table.heat tbody tr { border: 0; }
table.heat tbody tr:hover { background: none; }
table.heat tbody td { padding: 0; }
table.heat td.bm-name, table.heat th.bm-name {
  position: sticky; left: 0; z-index: 1; background: var(--surface);
  font-weight: 600; padding: 0 10px 0 0; text-align: left; font-size: 0.78rem;
}
table.heat td.n-col { color: var(--muted); text-align: right; padding: 0 8px 0 0; font-variant-numeric: tabular-nums; }
table.heat td.hc {
  width: 44px; min-width: 44px; height: 26px; text-align: center; color: var(--text);
  font-variant-numeric: tabular-nums; border-radius: 2px; cursor: default;
  background: color-mix(in srgb, var(--c, var(--surface)) var(--m, 0%), var(--surface));
}
table.heat td.hc.na { color: var(--border); }
table.heat td.hc:hover { outline: 2px solid var(--text); outline-offset: -1px; }
.heat-key { display: flex; align-items: center; gap: 8px; font-size: 0.72rem; color: var(--muted); margin-bottom: 12px; }
.heat-key .bar {
  width: 180px; height: 10px; border-radius: 2px; border: 1px solid var(--border);
  background: linear-gradient(to right,
    color-mix(in srgb, var(--heat-neg) 85%, var(--surface)), var(--surface),
    color-mix(in srgb, var(--heat-pos) 85%, var(--surface)));
}
</style>"""

PAGE_BODY = r"""<body>
__SIDEBAR__
<div class="main">
  <div class="topbar">
    <div>
      <h1>Correlations &amp; Analysis</h1>
      <div class="subtitle">Biomarkers × WHOOP sleep/recovery · Strava training load · life covariates — 2013–2026</div>
    </div>
    <div class="meta" id="meta"></div>
  </div>

  <div class="content">

    <!-- INSIGHTS -->
    <div class="section active" id="section-insights">
      <h2 class="section-heading">Key Insights</h2>
      <div class="metric-grid three" id="insights"></div>
    </div>

    <!-- WEARABLE × BIOMARKER -->
    <div class="section" id="section-wearable">
      <div class="metric-grid">
        <div class="metric-card">
          <div class="metric-card-header"><div class="metric-card-title">Top Correlations — Age-Adjusted (partial r)</div></div>
          <div class="metric-card-sub">Ranked by <strong>partial r</strong> after removing the shared linear trend with age from both
            the covariate and the biomarker. This answers "does this metric predict the biomarker
            <em>beyond what aging alone explains</em>?" — the more meaningful question given that
            many things change with age simultaneously. n ≥ 5 required.</div>
          <div class="legend" data-legend="sign"></div>
          <div class="chart-area rank-wide" id="rank-adj"></div><div class="chart-area rank-narrow" id="rank-adj-n"></div>
        </div>
        <div class="metric-card">
          <div class="metric-card-header"><div class="metric-card-title">Raw Correlations (unadjusted, for reference)</div></div>
          <div class="metric-card-sub">Pearson r without age-adjustment. Ranked by |r|. Anything that also has a large
            age trend may be partially or fully confounded.</div>
          <div class="legend" data-legend="sign"></div>
          <div class="chart-area rank-wide" id="rank-raw"></div><div class="chart-area rank-narrow" id="rank-raw-n"></div>
        </div>
      </div>
      <h2 class="section-heading">Scatter Plots — Top Pairs (raw)</h2>
      <p class="desc">Each point is one blood draw (faint = early, solid = recent). Regression line shown. Age not removed from axes.</p>
      <div class="metric-grid three" id="wearable-scatters"></div>
    </div>

    <!-- FULL MATRIX -->
    <div class="section" id="section-matrix">
      <div class="metric-grid wide">
        <div class="metric-card">
          <div class="metric-card-header">
            <div class="metric-card-title">Biomarker × All Covariates</div>
            <div class="roll-btns" id="matrix-toggle">
              <button class="roll-btn active" data-mode="r">Raw r</button>
              <button class="roll-btn" data-mode="partial">Age-adjusted partial r</button>
            </div>
          </div>
          <div class="metric-card-sub">Every biomarker row vs. all covariates.
            <strong>Raw r</strong>: plain Pearson correlation.
            <strong>Age-adjusted partial r</strong>: residuals after regressing both variables on age —
            measures the relationship <em>independent of aging</em>.
            Age-adjusted is blank for the age column itself and where n &lt; 5.
            Scroll right for all columns.</div>
          <div class="heat-key"><span>−1</span><span class="bar"></span><span>+1</span><span>· blank = not enough shared draws</span></div>
          <div class="table-wrap"><table class="heat" id="heat"></table></div>
        </div>
      </div>
    </div>

    <!-- BIOMARKER PAIRS -->
    <div class="section" id="section-pairs">
      <div class="metric-grid wide">
        <div class="metric-card">
          <div class="metric-card-header"><div class="metric-card-title">Top Biomarker–Biomarker Correlations</div></div>
          <div class="metric-card-sub">Pearson <em>r</em> between all biomarker pairs on shared blood-draw dates (≥4 required). Top 20 by |r|.</div>
          <div class="legend" data-legend="sign"></div>
          <div class="chart-area rank-wide" id="rank-pairs"></div><div class="chart-area rank-narrow" id="rank-pairs-n"></div>
        </div>
      </div>
      <h2 class="section-heading">Scatter Plots — Key Biomarker Pairs</h2>
      <p class="desc">Each point = one blood draw. Faint (early) → solid (recent).</p>
      <div class="metric-grid three" id="pair-scatters"></div>
    </div>

    <!-- TIMELINE -->
    <div class="section" id="section-timeline">
      <h2 class="section-heading">Wearable Metrics Over Blood-Draw Dates</h2>
      <p class="desc">Rolling averages of WHOOP and Strava signals plotted at each blood-draw date. Life-event markers shown as dashed vertical lines where they fall in range.</p>
      <div class="metric-grid" id="timeline"></div>
    </div>

    <!-- VO2MAX -->
    <div class="section" id="section-vo2max">
      <h2 class="section-heading">Estimated VO₂max — Full History (2011–2026)</h2>
      <div class="metric-grid wide">
        <div class="metric-card">
          <div class="metric-card-header"><div class="metric-card-title" id="vo2-title">VO₂max estimated series</div></div>
          <div class="metric-card-sub">Lab measurements (2 points) anchored to estimated series from:
            <strong>VDOT</strong> (Jack Daniels formula from race times) and
            <strong>FTP w/kg</strong> (Coggan formula, calibrated to lab values).
            Non-lab estimates are scaled so they match the lab anchors within ±180 days.
            Each method shown in a different colour. Blood-draw dates marked.</div>
          <div class="legend" id="vo2-legend"></div>
          <div class="chart-area" id="vo2-chart"></div>
        </div>
      </div>
      <h2 class="section-heading">Coverage Summary</h2>
      <div class="kpi-strip" id="vo2-kpis" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))"></div>
    </div>

  </div>
</div>

<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);

// ── helpers ────────────────────────────────────────────────────────────────
const fmtR = (r, dp = 3) => r == null ? '—' : (r > 0 ? '+' : r < 0 ? '−' : '') + Math.abs(r).toFixed(dp);
const signColor = r => r >= 0 ? 'var(--blue)' : 'var(--red)';
// headline r: coloured by sign once it is at least moderate, muted when weak
const rColor = r => r == null || Math.abs(r) < 0.4 ? 'var(--muted)' : signColor(r);
function rStrength(r) {
  if (r == null) return '';
  const a = Math.abs(r);
  return a >= 0.7 ? 'strong' : a >= 0.4 ? 'moderate' : 'weak';
}
const num = v => d3.format(',')(+(+v).toPrecision(4));
const badge = s => `<span class="badge">${s}</span>`;

// draw now and whenever the container's width changes (same contract as ar.js)
function mountChart(el, height, draw) {
  let lastW = -1;
  const run = () => {
    const w = Math.round(el.clientWidth);
    if (!w || w === lastW) return;
    lastW = w;
    el.innerHTML = '';
    const svg = d3.select(el).append('svg').attr('class', 'ar-chart')
      .attr('viewBox', `0 0 ${w} ${height}`).attr('height', height);
    draw(svg, w, height);
  };
  run();
  new ResizeObserver(run).observe(el);
}

document.querySelectorAll('[data-legend="sign"]').forEach(el => AR.legend(el, [
  { name: 'Positive r', color: 'var(--blue)' }, { name: 'Negative r', color: 'var(--red)' }]));

$('meta').textContent =
  `${DATA.n_draws} blood draws · ${(DATA.biomarker_matrix || []).length} biomarkers · ` +
  `${(DATA.covariate_meta || []).length} covariates (WHOOP + Strava + Garmin + Life)`;

// ── section nav ────────────────────────────────────────────────────────────
function showSection(id) {
  if (!$('section-' + id)) return;
  document.querySelectorAll('.nav-item[data-section]').forEach(i => i.classList.toggle('active', i.dataset.section === id));
  document.querySelectorAll('.section').forEach(s => s.classList.toggle('active', s.id === 'section-' + id));
}
document.querySelectorAll('.nav-item[data-section]').forEach(item => {
  item.addEventListener('click', () => {
    showSection(item.dataset.section);
    history.replaceState(null, '', '#' + item.dataset.section);
    window.scrollTo(0, 0);
  });
});
if (location.hash) showSection(location.hash.slice(1));

// ── Insights ───────────────────────────────────────────────────────────────
$('insights').innerHTML = (DATA.insights || []).map(ins => `
  <div class="metric-card insight" style="--kpi-color:${ins.color}">
    <div class="ic-emoji">${ins.emoji}</div>
    <div class="ic-title">${ins.title}</div>
    <div class="ic-body">${ins.body}</div>
  </div>`).join('');

// ── ranked correlation bars ────────────────────────────────────────────────
function rankChart(id, items, value, label, tip) {
  AR.hbarChart('#' + id, {
    labels: items.map(label), values: items.map(value),
    fmt: v => fmtR(v), labelWidth: 290, rowHeight: 24,
    colors: v => signColor(v), tip: i => tip(items[i]),
  });
  // phone layout: label on its own line above a bar on a fixed −1…+1 scale
  const rowH = 38;
  mountChart($(id + '-n'), rowH * items.length, (svg, W) => {
    const x = d3.scaleLinear().domain([-1, 1]).range([0, W - 52]);
    const g = svg.selectAll('g').data(items).join('g').attr('transform', (d, i) => `translate(0,${i * rowH})`);
    g.append('text').attr('class', 'cat-label').attr('x', 0).attr('y', 13).style('font-size', '11px').text(label);
    g.append('line').attr('x1', x(0)).attr('x2', x(0)).attr('y1', 18).attr('y2', 32).attr('stroke', 'var(--border)');
    g.append('rect').attr('x', d => Math.min(x(0), x(value(d)))).attr('y', 20)
      .attr('width', d => Math.max(1, Math.abs(x(value(d)) - x(0)))).attr('height', 10).attr('rx', 3)
      .attr('fill', d => signColor(value(d)));
    g.append('text').attr('class', 'val-label').attr('x', W).attr('y', 29).attr('text-anchor', 'end').text(d => fmtR(value(d)));
    g.append('rect').attr('width', W).attr('height', rowH).attr('fill', 'transparent')
      .on('mousemove', (ev, d) => AR.tip.show(tip(d), ev)).on('mouseleave', AR.tip.hide);
  });
}
const covTip = usePartial => item => {
  const r = usePartial ? (item.partial_r ?? item.r) : item.r;
  return `<b>${item.covariate} → ${item.biomarker}</b><br>` +
    `<span class="dim">source</span> ${item.source}<br>` +
    `<span class="dim">raw r</span> ${fmtR(item.r)}` +
    (item.partial_r != null ? `<br><span class="dim">age-adj r</span> ${fmtR(item.partial_r)}` : '') +
    `<br><span class="dim">${rStrength(r)} ${r > 0 ? 'positive' : 'negative'} · n=${item.n}</span>`;
};
rankChart('rank-adj', DATA.top_cov_bm_adj || [], i => i.partial_r ?? i.r,
          i => `${i.covariate} → ${i.biomarker}`, covTip(true));
rankChart('rank-raw', DATA.top_cov_bm || [], i => i.r,
          i => `${i.covariate} → ${i.biomarker}`, covTip(false));
rankChart('rank-pairs', DATA.top_bm_pairs || [], i => i.r, i => i.pair,
          i => `<b>${i.pair}</b><br><span class="dim">r</span> ${fmtR(i.r)}<br>` +
               `<span class="dim">${rStrength(i.r)} ${i.r > 0 ? 'positive' : 'negative'} · n=${i.n}</span>`);

// ── scatter cards ──────────────────────────────────────────────────────────
function scatterCard(container, sp) {
  const card = document.createElement('div');
  card.className = 'metric-card';
  card.innerHTML = `
    <div class="metric-card-header">
      <div class="metric-card-title">${sp.x_label} <span class="vs">vs</span> ${sp.y_label}${sp.source ? ' ' + badge(sp.source) : ''}</div>
      <div class="r-meta">
        <div class="r-big" style="color:${rColor(sp.r)}">r = ${fmtR(sp.r)}</div>
        <div class="n-lbl">n = ${sp.n}</div>
      </div>
    </div>
    <div class="chart-area"></div>`;
  container.appendChild(card);

  // early → recent as faint → solid, one hue; mixes with the surface so it follows the theme
  const years = sp.points.map(p => +p.date.slice(0, 4));
  const y0 = Math.min(...years), y1 = Math.max(...years);
  let fit = null;
  if (sp.reg_line && sp.reg_line[1].x !== sp.reg_line[0].x) {
    const [a, b] = sp.reg_line, slope = (b.y - a.y) / (b.x - a.x);
    fit = { slope, intercept: a.y - slope * a.x };
  }
  AR.scatter(card.querySelector('.chart-area'), {
    points: sp.points.map((p, i) => {
      const t = y1 === y0 ? 1 : (years[i] - y0) / (y1 - y0);
      return { x: p.x, y: p.y, date: p.date,
               color: `color-mix(in srgb, var(--accent) ${Math.round(30 + 70 * t)}%, var(--surface))` };
    }),
    height: 240, xLabel: sp.x_label, yLabel: sp.y_label, fit, r: 5, margin: { t: 26 },
    tip: p => `<b>${AR.fmtDate(p.date)}</b><br><span class="dim">${sp.x_label}</span> ${num(p.x)}` +
              `<br><span class="dim">${sp.y_label}</span> ${num(p.y)}`,
  });
}
(DATA.wearable_scatters || []).forEach(sp => scatterCard($('wearable-scatters'), sp));
(DATA.scatter_pairs || []).forEach(sp => scatterCard($('pair-scatters'), sp));

// ── Full matrix heatmap ────────────────────────────────────────────────────
(function () {
  const covMeta = DATA.covariate_meta || [];
  const groups = ['Life', 'Fitness', 'WHOOP', 'Strava', 'Garmin']
    .map(src => ({ src, cols: covMeta.filter(c => c.source === src) })).filter(g => g.cols.length);
  const cols = groups.flatMap(g => g.cols);
  const short = l => l.replace(' avg', '');
  const rows = DATA.biomarker_matrix || [];
  let mode = 'r';
  const tbl = d3.select('#heat');

  const thead = tbl.append('thead');
  const g = thead.append('tr');
  g.append('th').attr('class', 'bm-name');
  g.append('th');
  g.selectAll('th.grp').data(groups).join('th').attr('class', 'grp')
    .attr('colspan', d => d.cols.length).text(d => d.src);
  const h = thead.append('tr');
  h.append('th').attr('class', 'bm-name').text('Biomarker');
  h.append('th').attr('class', 'num').text('n');
  h.selectAll('th.vh').data(cols).join('th').attr('class', 'vh').append('span').text(d => short(d.label));

  const tr = tbl.append('tbody').selectAll('tr').data(rows).join('tr');
  tr.append('td').attr('class', 'bm-name').text(d => d.name);
  tr.append('td').attr('class', 'n-col').text(d => d.n);
  const cell = tr.selectAll('td.hc').data(row => cols.map(col => ({ row, col }))).join('td').attr('class', 'hc');
  const val = d => { const v = (d.row[mode] || {})[d.col.key]; return v === undefined ? null : v; };

  function paint() {
    cell.classed('na', d => val(d) == null)
      .style('--c', d => { const v = val(d); return v == null ? null : v >= 0 ? 'var(--heat-pos)' : 'var(--heat-neg)'; })
      .style('--m', d => { const v = val(d); return v == null ? null : Math.round(Math.min(1, Math.abs(v)) * 85) + '%'; })
      .text(d => { const v = val(d); return v == null ? '·' : fmtR(v, 2).replace('+', ''); });
  }
  paint();
  cell.on('mousemove', (ev, d) => {
      const raw = (d.row.r || {})[d.col.key], adj = (d.row.partial || {})[d.col.key];
      AR.tip.show(`<b>${d.row.name} × ${d.col.label}</b><br>` +
        `<span class="dim">${d.col.source} · raw r</span> ${fmtR(raw)}<br>` +
        `<span class="dim">age-adj partial r</span> ${fmtR(adj)}`, ev);
    })
    .on('mouseleave', AR.tip.hide);

  document.querySelectorAll('#matrix-toggle .roll-btn').forEach(btn => btn.addEventListener('click', () => {
    mode = btn.dataset.mode;
    document.querySelectorAll('#matrix-toggle .roll-btn').forEach(b => b.classList.toggle('active', b === btn));
    paint();
  }));
})();

// ── Timeline: one metric per chart (no dual axes) ──────────────────────────
(function () {
  const tl = DATA.timeline || [];
  const METRICS = [
    ['WHOOP: HRV rMSSD (30-day avg)',          'whoop_hrv_30d',        'ms',      'var(--teal)',   1],
    ['WHOOP: Recovery Score (30-day avg)',     'whoop_recovery_30d',   '0–100',   'var(--blue)',   1],
    ['WHOOP: Resting HR (30-day avg)',         'whoop_rhr_30d',        'bpm',     'var(--red)',    1],
    ['WHOOP: Day Strain (30-day avg)',         'whoop_strain_30d',     '0–21',    'var(--orange)', 1],
    ['WHOOP: Sleep Hours (30-day avg)',        'whoop_sleep_hrs_30d',  'hours',   'var(--purple)', 2],
    ['WHOOP: Sleep Performance (30-day avg)',  'whoop_sleep_perf_30d', '%',       'var(--pink)',   1],
    ['Strava: Training Hours (90-day window)', 'strava_hrs_90d',       'hours',   'var(--orange)', 1],
    ['Strava: Run Miles (90-day window)',      'strava_run_miles_90d', 'miles',   'var(--green)',  1],
    ['WHOOP: SpO₂ (30-day avg)',               'whoop_spo2_30d',       '%',       'var(--blue)',   1],
    ['WHOOP: Respiratory Rate (30-day avg)',   'whoop_resp_30d',       'breaths/min', 'var(--yellow)', 1],
  ];
  METRICS.forEach(([title, key, unit, color, dp]) => {
    const pts = tl.filter(r => r[key] != null);
    const card = document.createElement('div');
    card.className = 'metric-card';
    const last = pts[pts.length - 1];
    card.innerHTML = `
      <div class="metric-card-header"><div class="metric-card-title">${title}</div></div>
      <div class="metric-card-val" style="color:${color}">${last ? last[key].toFixed(dp) : '—'}</div>
      <div class="metric-card-sub">${unit}${last ? ' · before the ' + AR.fmtDate(last.date) + ' draw' : ''}</div>
      <div class="chart-area"></div>`;
    $('timeline').appendChild(card);
    AR.lineChart(card.querySelector('.chart-area'), {
      x: pts.map(r => r.date), y: pts.map(r => r[key]), color, dots: true, height: 160,
      yFmt: v => d3.format('~g')(+v.toFixed(dp)),
      markers: (DATA.life_events || []).map(e => ({ x: e.date, label: e.label })),
      tip: i => `<b>${AR.fmtDate(pts[i].date)}</b><br>${pts[i][key].toFixed(dp)} <span class="dim">${unit}</span>`,
    });
  });
})();

// ── VO₂max estimated series ────────────────────────────────────────────────
(function () {
  const series = (DATA.vo2max_series || []).map(p => Object.assign({ t: AR.parseDate(p.date) }, p));
  if (!series.length) return;
  const METHOD_COLOR = {
    lab: 'var(--text)', vdot_marathon: 'var(--green)', vdot_5k: 'var(--purple)', vdot_hm: 'var(--yellow)',
    vdot_fm: 'var(--orange)', vdot_10k: 'var(--pink)', ftp_coggan: 'var(--blue)',
  };
  const METHOD_LABEL = {
    lab: 'Lab (measured)', vdot_marathon: 'VDOT from marathon', vdot_5k: 'VDOT from 5K',
    vdot_hm: 'VDOT from half marathon', vdot_fm: 'VDOT from full marathon (Strava)',
    vdot_10k: 'VDOT from 10K', ftp_coggan: 'FTP-based (Coggan, calibrated)',
  };
  const col = p => METHOD_COLOR[p.method] || 'var(--accent)';
  const methods = [...new Set(series.map(p => p.method))];
  const allDates = [...new Set(series.map(p => p.date))].sort();
  const draws = (DATA.timeline || []).map(r => AR.parseDate(r.date));

  $('vo2-title').textContent = `VO₂max estimated series — ${series.length} data points`;
  $('vo2-legend').innerHTML = methods.map(m =>
    `<span class="legend-item"><span class="sw dot" style="background:${METHOD_COLOR[m] || 'var(--accent)'}"></span>${METHOD_LABEL[m] || m}</span>`).join('') +
    '<span class="legend-item"><span class="sw dash"></span>Blood draw date</span>';

  mountChart($('vo2-chart'), 320, (svg, W, H) => {
    const m = { t: 22, r: 14, b: 26, l: 44 };
    const [d0, d1] = d3.extent(series, p => p.t);
    const x = d3.scaleTime().domain([d3.timeDay.offset(d0, -60), d3.timeDay.offset(d1, 60)]).range([m.l, W - m.r]);
    const [lo, hi] = d3.extent(series, p => p.value);
    const y = d3.scaleLinear().domain([Math.min(35, lo - 1), Math.max(70, hi + 1)]).range([H - m.b, m.t]);

    svg.append('g').attr('class', 'grid').attr('transform', `translate(${m.l},0)`)
      .call(d3.axisLeft(y).ticks(6).tickSize(-(W - m.l - m.r)).tickPadding(6));
    svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.b})`)
      .call(d3.axisBottom(x).ticks(Math.max(2, Math.floor((W - m.l - m.r) / 80))).tickFormat(d3.timeFormat('%Y')).tickSize(0).tickPadding(8));
    svg.append('text').attr('class', 'axis-title').attr('x', m.l).attr('y', m.t - 8).text('VO₂max (ml/kg/min)');

    svg.append('g').selectAll('line').data(draws.filter(d => d >= x.domain()[0] && d <= x.domain()[1])).join('line')
      .attr('class', 'ref-line').attr('x1', d => x(d)).attr('x2', d => x(d)).attr('y1', m.t).attr('y2', H - m.b);

    svg.append('g').selectAll('circle').data(series.filter(p => p.method !== 'lab')).join('circle')
      .attr('cx', p => x(p.t)).attr('cy', p => y(p.value)).attr('r', 3.5)
      .attr('fill', col).attr('stroke', 'var(--surface)').attr('stroke-width', 1);
    // lab anchors on top, as stars
    svg.append('g').selectAll('path').data(series.filter(p => p.method === 'lab')).join('path').attr('class', 'ring')
      .attr('transform', p => `translate(${x(p.t)},${y(p.value)})`)
      .attr('d', d3.symbol().type(d3.symbolStar).size(190)).attr('fill', col);

    const focus = svg.append('circle').attr('r', 7).attr('fill', 'none').attr('stroke', 'var(--text)')
      .attr('stroke-width', 1.5).style('display', 'none').style('pointer-events', 'none');
    svg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', Math.max(0, W - m.l - m.r)).attr('height', H - m.t - m.b)
      .attr('fill', 'transparent')
      .on('mousemove', ev => {
        const [px, py] = d3.pointer(ev, svg.node());
        let best = null, bd = 30;
        series.forEach(p => { const d = Math.hypot(x(p.t) - px, y(p.value) - py) - (p.method === 'lab' ? 4 : 0); if (d < bd) { bd = d; best = p; } });
        if (!best) { focus.style('display', 'none'); AR.tip.hide(); return; }
        focus.style('display', null).attr('cx', x(best.t)).attr('cy', y(best.value));
        AR.tip.show(`<b>${AR.fmtDate(best.date)}</b><br>${AR.swatch(col(best))}` +
          `<span class="dim">${METHOD_LABEL[best.method] || best.method}</span> ${best.value.toFixed(1)} ml/kg/min` +
          (best.source ? `<br><span class="dim">${best.source}</span>` : ''), ev);
      })
      .on('mouseleave', () => { focus.style('display', 'none'); AR.tip.hide(); });
  });

  const n = f => series.filter(f).length;
  $('vo2-kpis').innerHTML = [
    ['🔬 Lab measurements', n(p => p.method === 'lab') + ' actual VO₂max tests', 'var(--text)'],
    ['🏃 VDOT from races', n(p => p.method.startsWith('vdot')) + ' race-derived estimates', 'var(--green)'],
    ['🚴 FTP-based (calibrated)', n(p => p.method === 'ftp_coggan') + ' cycling estimates', 'var(--blue)'],
    ['📅 Total span', (allDates[0] || '?') + ' → ' + (allDates[allDates.length - 1] || '?'), 'var(--accent)'],
  ].map(([name, val, c]) => `
    <div class="kpi-card" style="--kpi-color:${c}">
      <div class="kpi-label">${name}</div>
      <div class="kpi-value" style="font-size:1rem">${val}</div>
    </div>`).join('');
})();
</script>
</body>
</html>
"""

SECTIONS_NAV = """  <div class="nav-section">
    <div class="nav-section-title">Analysis</div>
    <div class="nav-item active" data-section="insights"><span class="icon">💡</span> Insights</div>
    <div class="nav-item" data-section="wearable"><span class="icon">⌚</span> Wearable × Biomarker</div>
    <div class="nav-item" data-section="matrix"><span class="icon">🧬</span> Full Matrix</div>
    <div class="nav-item" data-section="pairs"><span class="icon">🔗</span> Biomarker Pairs</div>
    <div class="nav-item" data-section="timeline"><span class="icon">📅</span> Timeline</div>
    <div class="nav-item" data-section="vo2max"><span class="icon">🫁</span> VO₂max</div>
  </div>"""


def build_html(payload: dict) -> str:
    data_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return (
        shell.head("Andy — Correlations &amp; Analysis", payload["generated"], PAGE_STYLE)
        + PAGE_BODY
        .replace("__SIDEBAR__", shell.sidebar("correlations.html", SECTIONS_NAV))
        .replace("__DATA__", data_json)
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Loading bloodwork from {BLOODWORK_FILE}...")
    measurements, ref_ranges = load_bloodwork(BLOODWORK_FILE)
    all_dates = sorted({pt["date"] for pts in measurements.values() for pt in pts})
    print(f"  {len(measurements)} biomarkers · {len(all_dates)} draw dates")

    print(f"Loading fitness from {FITNESS_FILE}...")
    fitness = load_fitness(FITNESS_FILE)

    print(f"Loading WHOOP windows from {WHOOP_DB}...")
    whoop_windows = load_whoop_windows(WHOOP_DB, all_dates)
    covered = sum(1 for d in all_dates if whoop_windows.get(d, {}).get("whoop_hrv_30d") is not None)
    print(f"  {covered}/{len(all_dates)} draw dates have WHOOP 30-day data")

    print(f"Loading Strava windows from {STRAVA_DB}...")
    strava_windows = load_strava_windows(STRAVA_DB, all_dates)
    scovered = sum(1 for d in all_dates if strava_windows.get(d, {}).get("strava_hrs_90d") is not None)
    print(f"  {scovered}/{len(all_dates)} draw dates have Strava 90-day data")

    print(f"Loading Garmin windows from {GARMIN_DB}...")
    garmin_windows = load_garmin_windows(GARMIN_DB, all_dates)
    gcovered = sum(1 for d in all_dates if garmin_windows.get(d, {}).get("garmin_steps_90d") is not None)
    print(f"  {gcovered}/{len(all_dates)} draw dates have Garmin 90-day data")

    print("Building estimated VO₂max series...")
    vo2max_series = build_vo2max_series(fitness, STRAVA_DB)
    methods = {}
    for p in vo2max_series:
        methods[p["method"]] = methods.get(p["method"], 0) + 1
    print(f"  {len(vo2max_series)} points: " + ", ".join(f"{k}×{v}" for k, v in sorted(methods.items())))

    print("Building correlation payload...")
    payload = build_payload(measurements, fitness, whoop_windows, strava_windows, garmin_windows, vo2max_series)
    print(f"  {len(payload['biomarker_matrix'])} biomarkers × {len(payload['covariate_meta'])} covariates")
    print(f"  {len(payload['top_cov_bm'])} top wearable→biomarker pairs")
    print(f"  {len(payload['top_bm_pairs'])} top biomarker–biomarker pairs")
    print(f"  {len(payload['wearable_scatters'])} wearable scatter plots")
    print(f"  {len(payload['insights'])} insights")

    print(f"Writing {HTML_FILE}...")
    html = build_html(payload)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(HTML_FILE)
    print(f"  Done! {HTML_FILE} ({size:,} bytes)")


if __name__ == "__main__":
    main()
