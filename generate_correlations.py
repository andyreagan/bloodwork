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

BLOODWORK_FILE = os.path.join(os.path.dirname(__file__), "bloodwork_data.yaml")
FITNESS_FILE   = os.path.join(os.path.dirname(__file__), "fitness_data.yaml")
HTML_FILE      = os.path.join(os.path.dirname(__file__), "correlations.html")

STRAVA_DB  = os.path.expanduser("~/projects/2026/strava-database/strava.db")
WHOOP_DB   = os.path.expanduser("~/projects/2026/whoop-database/whoop.db")
GARMIN_DB  = os.path.expanduser("~/projects/2026/connect-database/garmin-database/garmin.db")

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
            "emoji": source_emoji, "color": "#818cf8",
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
            "emoji": "💓", "color": "#22c55e" if (pr or r) > 0 else "#ef4444",
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
            "emoji": "😴", "color": "#60a5fa",
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
            "emoji": "🏃", "color": "#f97316",
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
            "emoji": "📈", "color": "#ef4444" if r > 0 else "#22c55e",
            "title": f"{row['name']} tracks age most strongly (r = {r:+.2f})",
            "body":  (f"{row['name']} {'rises' if r > 0 else 'falls'} with age. "
                      f"This is used as the control variable in age-adjusted correlations — "
                      f"so other findings above are already independent of this trend."),
        })

    # Strongest biomarker-biomarker pair
    if top_bm_pairs:
        tp = top_bm_pairs[0]
        insights.append({
            "emoji": "🔗", "color": "#818cf8",
            "title": f"Strongest biomarker–biomarker link: {tp['pair']} (r = {tp['r']:+.3f})",
            "body":  (f"These two markers move {'together' if tp['r'] > 0 else 'in opposite directions'} "
                      f"across {tp['n']} shared blood draws. Note: biomarker pairs are not yet age-adjusted."),
        })

    return insights


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def build_html(payload: dict) -> str:
    data_json = json.dumps(payload, separators=(",", ":"))

    # Build a lookup label → key for the matrix table header
    cov_meta_by_key = {c["key"]: c for c in payload["covariate_meta"]}
    classic_keys  = ["age", "kids", "weight", "vo2max"]
    whoop_keys    = [c["key"] for c in payload["covariate_meta"] if c["source"] == "WHOOP"]
    strava_keys   = [c["key"] for c in payload["covariate_meta"] if c["source"] == "Strava"]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Andy — Correlations &amp; Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f1117; --surface: #1a1d27; --surface2: #22263a;
    --border: #2e3250; --text: #e2e8f0; --muted: #8892a4;
    --green: #22c55e; --yellow: #eab308; --red: #ef4444;
    --blue: #60a5fa; --accent: #818cf8; --orange: #f97316;
    --radius: 12px;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px; line-height: 1.5; padding: 1rem;
  }}

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

  /* Nav tabs */
  .tabs {{
    display: flex; gap: 0.5rem; margin-bottom: 1.5rem; flex-wrap: wrap;
  }}
  .tab-btn {{
    background: var(--surface); border: 1px solid var(--border);
    color: var(--muted); border-radius: 6px;
    padding: 0.4rem 0.85rem; font-size: 0.8rem; cursor: pointer; transition: all 0.15s;
  }}
  .tab-btn:hover, .tab-btn.active {{
    background: var(--accent); border-color: var(--accent); color: white;
  }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}

  /* Section */
  .section {{ margin-bottom: 2rem; }}
  .section > h2 {{
    font-size: 1rem; font-weight: 700; color: var(--accent);
    margin-bottom: 0.75rem; padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
  }}
  .section > p.desc {{
    color: var(--muted); font-size: 0.82rem; margin-bottom: 1rem; line-height: 1.6;
  }}

  /* Insight cards */
  .insights-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 0.85rem;
  }}
  .insight-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 1rem;
  }}
  .ic-emoji  {{ font-size: 1.4rem; margin-bottom: 0.35rem; }}
  .ic-title  {{ font-size: 0.85rem; font-weight: 700; color: var(--text); margin-bottom: 0.3rem; }}
  .ic-body   {{ font-size: 0.8rem; color: var(--muted); line-height: 1.55; }}

  /* Table */
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; white-space: nowrap; }}
  thead tr {{ border-bottom: 1px solid var(--border); color: var(--muted); text-align: left; }}
  th {{ padding: 5px 8px; font-weight: 600; }}
  tbody tr {{ border-bottom: 1px solid rgba(46,50,80,0.4); }}
  tbody tr:nth-child(even) {{ background: rgba(255,255,255,0.02); }}
  td {{ padding: 5px 8px; vertical-align: middle; }}
  td.bm-name {{ font-weight: 600; color: var(--text); position: sticky; left: 0; background: var(--bg); z-index:1; }}
  td.n-col {{ text-align: center; color: var(--muted); }}

  .r-val {{ font-weight: 700; font-size: 0.8rem; }}
  .r-bar-track {{ height: 3px; background: rgba(255,255,255,0.08); border-radius: 2px; margin-top: 2px; }}
  .r-bar-fill  {{ height: 3px; border-radius: 2px; }}
  th.group-whoop {{ background: rgba(34,197,94,0.08); }}
  th.group-strava {{ background: rgba(249,115,22,0.08); }}
  th.group-classic {{ background: rgba(96,165,250,0.08); }}

  /* Source badge */
  .source-badge {{
    display: inline-block; font-size: 0.65rem; font-weight: 700;
    padding: 1px 5px; border-radius: 4px; margin-left: 4px; vertical-align: middle;
  }}
  .badge-whoop  {{ background: rgba(34,197,94,0.2);  color: #22c55e; }}
  .badge-strava {{ background: rgba(249,115,22,0.2); color: #f97316; }}
  .badge-life   {{ background: rgba(234,179,8,0.2);  color: #eab308; }}
  .badge-fitness{{ background: rgba(129,140,248,0.2);color: #818cf8; }}

  /* Pair/top cards */
  .pair-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 0.7rem;
  }}
  .pair-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 0.85rem 1rem;
  }}
  .pair-name {{ font-size: 0.78rem; font-weight: 700; color: var(--text); margin-bottom: 3px; }}
  .pair-r    {{ font-size: 1.15rem; font-weight: 700; }}
  .pair-meta {{ font-size: 0.68rem; color: var(--muted); margin-top: 2px; }}
  .pair-bar-track {{ height: 3px; background: rgba(255,255,255,0.08); border-radius: 2px; margin-top: 6px; }}
  .pair-bar-fill  {{ height: 3px; border-radius: 2px; }}

  /* Charts */
  .charts-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem;
  }}
  .chart-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 1rem 1rem 0.75rem;
  }}
  .chart-header {{ display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 0.5rem; }}
  .chart-title  {{ font-size: 0.82rem; font-weight: 700; color: var(--text); line-height: 1.3; }}
  .chart-meta   {{ text-align: right; flex-shrink: 0; margin-left: 0.5rem; }}
  .r-big  {{ font-size: 1.1rem; font-weight: 700; }}
  .n-lbl  {{ font-size: 0.65rem; color: var(--muted); }}
  .chart-wrap {{ height: 160px; position: relative; }}
  .full-card {{ grid-column: 1 / -1; }}
  .timeline-wrap {{ height: 220px; position: relative; }}

  .chart-legend {{ display: flex; gap: 0.75rem; margin-top: 0.4rem; flex-wrap: wrap; }}
  .legend-item {{ display: flex; align-items: center; gap: 4px; font-size: 0.65rem; color: var(--muted); }}
  .legend-swatch {{ width: 12px; height: 8px; border-radius: 2px; }}

  footer {{
    text-align: center; color: var(--muted); font-size: 0.75rem;
    padding: 1rem 0; border-top: 1px solid var(--border); margin-top: 2rem;
  }}
  @media (max-width: 600px) {{
    .charts-grid {{ grid-template-columns: 1fr; }}
    .pair-grid   {{ grid-template-columns: 1fr; }}
    .insights-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<header>
  <div>
    <h1>🔬 Correlations &amp; Analysis</h1>
    <p>Biomarkers × WHOOP sleep/recovery · Strava training load · life covariates — 2013–2026</p>
  </div>
  <a href="index.html" class="back-link">← Dashboard</a>
</header>

<div class="tabs" id="tabs"></div>
<div id="tab-panels"></div>

<footer id="footer"></footer>

<script>
const DATA = {data_json};

// ---------------------------------------------------------------------------
// Helpers
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
  const pct = Math.round(Math.abs(r) * 100);
  const col = rColor(r);
  const dir = r >= 0 ? '▲' : '▼';
  return `<div class="r-val" style="color:${{col}};">${{r.toFixed(3)}} ${{dir}}</div>
          <div class="r-bar-track"><div class="r-bar-fill" style="width:${{pct}}%;background:${{col}};"></div></div>`;
}}
function badgeHtml(source) {{
  const cls = {{'WHOOP':'whoop','Strava':'strava','Life':'life','Fitness':'fitness'}}[source] || 'life';
  return `<span class="source-badge badge-${{cls}}">${{source}}</span>`;
}}
function scatterPointColors(points) {{
  const years = points.map(p => parseInt(p.date.substring(0,4)));
  const minYr = Math.min(...years), maxYr = Math.max(...years);
  return points.map(p => {{
    const t = maxYr === minYr ? 0.5 : (parseInt(p.date.substring(0,4)) - minYr) / (maxYr - minYr);
    return `rgba(${{Math.round(96+t*(249-96))}},${{Math.round(165+t*(115-165))}},${{Math.round(250+t*(22-250))}},0.9)`;
  }});
}}

// ---------------------------------------------------------------------------
// Tab system
// ---------------------------------------------------------------------------
const tabsEl   = document.getElementById('tabs');
const panelsEl = document.getElementById('tab-panels');
let firstTab   = true;

function addTab(label, id, buildFn) {{
  const btn = document.createElement('button');
  btn.className = 'tab-btn' + (firstTab ? ' active' : '');
  btn.textContent = label;
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('panel-' + id).classList.add('active');
  }});
  tabsEl.appendChild(btn);

  const panel = document.createElement('div');
  panel.className = 'tab-panel' + (firstTab ? ' active' : '');
  panel.id = 'panel-' + id;
  buildFn(panel);
  panelsEl.appendChild(panel);
  firstTab = false;
}}

// ---------------------------------------------------------------------------
// Tab 1: Insights
// ---------------------------------------------------------------------------
addTab('💡 Insights', 'insights', panel => {{
  const sec = mkSection('Key Insights', null);
  const grid = document.createElement('div');
  grid.className = 'insights-grid';
  (DATA.insights || []).forEach(ins => {{
    const card = document.createElement('div');
    card.className = 'insight-card';
    card.style.borderLeft = `3px solid ${{ins.color}}`;
    card.innerHTML = `<div class="ic-emoji">${{ins.emoji}}</div>
                      <div class="ic-title">${{ins.title}}</div>
                      <div class="ic-body">${{ins.body}}</div>`;
    grid.appendChild(card);
  }});
  sec.appendChild(grid);
  panel.appendChild(sec);
}});

// ---------------------------------------------------------------------------
// Tab 2: Wearable × Biomarker top correlations
// ---------------------------------------------------------------------------
addTab('⌚ Wearable × Biomarker', 'wearable', panel => {{

  // Helper: render a ranked pair-card grid
  function pairGrid(items, usePartial) {{
    const grid = document.createElement('div');
    grid.className = 'pair-grid';
    items.forEach(item => {{
      const r   = usePartial ? (item.partial_r ?? item.r) : item.r;
      const col = rColor(r);
      const card = document.createElement('div');
      card.className = 'pair-card';
      card.style.borderLeft = `3px solid ${{col}}`;
      // Show both values when available
      const rawPart  = `<span style="color:var(--muted);font-size:0.7rem;">raw r = ${{item.r.toFixed(3)}}</span>`;
      const adjPart  = item.partial_r !== null && item.partial_r !== undefined
        ? `<span style="color:${{rColor(item.partial_r)}};font-size:0.7rem;">age-adj r = ${{item.partial_r.toFixed(3)}}</span>`
        : '';
      card.innerHTML = `
        <div class="pair-name">${{item.covariate}} ${{badgeHtml(item.source)}}<br>→ ${{item.biomarker}}</div>
        <div class="pair-r" style="color:${{col}};">${{usePartial ? 'partial ' : ''}}r = ${{r.toFixed(3)}}</div>
        <div class="pair-meta" style="display:flex;gap:0.5rem;flex-wrap:wrap;">${{rawPart}}${{adjPart}}</div>
        <div class="pair-meta" style="margin-top:2px;">${{rStrength(r)}} ${{r > 0 ? 'positive' : 'negative'}} · n=${{item.n}}</div>
        <div class="pair-bar-track"><div class="pair-bar-fill" style="width:${{Math.round(Math.abs(r)*100)}}%;background:${{col}};"></div></div>`;
      grid.appendChild(card);
    }});
    return grid;
  }}

  // Age-adjusted section (primary)
  const adjItems = DATA.top_cov_bm_adj || [];
  const sec = mkSection(
    'Top Correlations — Age-Adjusted (partial r)',
    `Ranked by <strong>partial r</strong> after removing the shared linear trend with age from both
     the covariate and the biomarker. This answers "does this metric predict the biomarker
     <em>beyond what aging alone explains</em>?" — the more meaningful question given that
     many things change with age simultaneously. n ≥ 5 required.`
  );
  sec.appendChild(pairGrid(adjItems, true));
  panel.appendChild(sec);

  // Raw section (secondary, collapsible)
  const rawSec = mkSection(
    'Raw Correlations (unadjusted, for reference)',
    `Pearson r without age-adjustment. Ranked by |r|. Anything that also has a large
     age trend may be partially or fully confounded.`
  );
  rawSec.appendChild(pairGrid(DATA.top_cov_bm || [], false));
  panel.appendChild(rawSec);

  // Scatter plots for top age-adjusted pairs
  if (DATA.wearable_scatters && DATA.wearable_scatters.length > 0) {{
    const sec2 = mkSection('Scatter Plots — Top Pairs (raw)',
      'Each point is one blood draw (blue = early, orange = recent). Regression line shown. Age not removed from axes.');
    const sgrid = document.createElement('div');
    sgrid.className = 'charts-grid';
    DATA.wearable_scatters.forEach(sp => renderScatterCard(sgrid, sp));
    sec2.appendChild(sgrid);
    panel.appendChild(sec2);
  }}
}});

// ---------------------------------------------------------------------------
// Tab 3: Full matrix table
// ---------------------------------------------------------------------------
addTab('🧬 Full Matrix', 'matrix', panel => {{
  const covMeta     = DATA.covariate_meta || [];
  const classicKeys = ['age','kids','weight','vo2max'];
  const whoopKeys   = covMeta.filter(c => c.source === 'WHOOP').map(c => c.key);
  const stravaKeys  = covMeta.filter(c => c.source === 'Strava').map(c => c.key);
  const allKeys     = [...classicKeys, ...whoopKeys, ...stravaKeys];
  const keyToLabel  = Object.fromEntries(covMeta.map(c => [c.key, c.label]));

  // Toggle: raw vs age-adjusted
  let showPartial = false;
  const toggleBar = document.createElement('div');
  toggleBar.style.cssText = 'display:flex;gap:0.5rem;margin-bottom:1rem;align-items:center;';
  toggleBar.innerHTML = `<span style="font-size:0.8rem;color:var(--muted);">Show:</span>`;
  ['Raw r', 'Age-adjusted partial r'].forEach((label, i) => {{
    const btn = document.createElement('button');
    btn.className = 'tab-btn' + (i === 0 ? ' active' : '');
    btn.textContent = label;
    btn.addEventListener('click', () => {{
      showPartial = i === 1;
      toggleBar.querySelectorAll('.tab-btn').forEach((b,j) => b.classList.toggle('active', j===i));
      rebuildBody();
    }});
    toggleBar.appendChild(btn);
  }});

  const sec = mkSection(
    'Biomarker × All Covariates',
    `Every biomarker row vs. all covariates.
     <strong>Raw r</strong>: plain Pearson correlation.
     <strong>Age-adjusted partial r</strong>: residuals after regressing both variables on age —
     measures the relationship <em>independent of aging</em>.
     Age-adjusted is blank for the age column itself and where n &lt; 5.
     Scroll right for all columns.`
  );
  sec.appendChild(toggleBar);

  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  const tbl = document.createElement('table');

  // Header
  const thead = document.createElement('thead');
  let htr = '<tr><th>Biomarker</th><th class="n-col">n</th>';
  classicKeys.forEach(k => {{ htr += `<th class="group-classic">${{keyToLabel[k] || k}}</th>`; }});
  whoopKeys.forEach(k   => {{ htr += `<th class="group-whoop">${{(keyToLabel[k]||k).replace(' avg','').replace(' (ms)','').replace(' (bpm)','')}}</th>`; }});
  stravaKeys.forEach(k  => {{ htr += `<th class="group-strava">${{(keyToLabel[k]||k)}}</th>`; }});
  htr += '</tr>';
  thead.innerHTML = htr;
  tbl.appendChild(thead);

  const tbody = document.createElement('tbody');
  tbl.appendChild(tbody);

  function rebuildBody() {{
    tbody.innerHTML = '';
    (DATA.biomarker_matrix || []).forEach(row => {{
      const tr = document.createElement('tr');
      let cells = `<td class="bm-name">${{row.name}}</td><td class="n-col">${{row.n}}</td>`;
      allKeys.forEach(k => {{
        let v;
        if (showPartial) {{
          v = (row.partial && row.partial[k] !== undefined) ? row.partial[k] : null;
        }} else {{
          v = (row.r && row.r[k] !== undefined) ? row.r[k] : (row[k] !== undefined ? row[k] : null);
        }}
        cells += `<td style="min-width:90px">${{rBarHtml(v)}}</td>`;
      }});
      tr.innerHTML = cells;
      tbody.appendChild(tr);
    }});
  }}
  rebuildBody();

  wrap.appendChild(tbl);
  sec.appendChild(wrap);
  panel.appendChild(sec);
}});

// ---------------------------------------------------------------------------
// Tab 4: Biomarker pairs
// ---------------------------------------------------------------------------
addTab('🔗 Biomarker Pairs', 'pairs', panel => {{
  const sec = mkSection(
    'Top Biomarker–Biomarker Correlations',
    'Pearson <em>r</em> between all biomarker pairs on shared blood-draw dates (≥4 required). Top 20 by |r|.'
  );
  const grid = document.createElement('div');
  grid.className = 'pair-grid';
  (DATA.top_bm_pairs || []).forEach(item => {{
    const col = rColor(item.r);
    const card = document.createElement('div');
    card.className = 'pair-card';
    card.style.borderLeft = `3px solid ${{col}}`;
    card.innerHTML = `
      <div class="pair-name">${{item.pair}}</div>
      <div class="pair-r" style="color:${{col}};">r = ${{item.r.toFixed(3)}}</div>
      <div class="pair-meta">${{rStrength(item.r)}} ${{item.r > 0 ? 'positive' : 'negative'}} · n=${{item.n}}</div>
      <div class="pair-bar-track"><div class="pair-bar-fill" style="width:${{Math.round(Math.abs(item.r)*100)}}%;background:${{col}};"></div></div>`;
    grid.appendChild(card);
  }});
  sec.appendChild(grid);
  panel.appendChild(sec);

  // Biomarker scatter plots
  const sec2 = mkSection('Scatter Plots — Key Biomarker Pairs',
    'Each point = one blood draw. Blue (early) → orange (recent).');
  const sgrid = document.createElement('div');
  sgrid.className = 'charts-grid';
  (DATA.scatter_pairs || []).forEach(sp => renderScatterCard(sgrid, sp));
  sec2.appendChild(sgrid);
  panel.appendChild(sec2);
}});

// ---------------------------------------------------------------------------
// Tab 5: Timeline
// ---------------------------------------------------------------------------
addTab('📅 Timeline', 'timeline', panel => {{
  const sec = mkSection(
    'Wearable Metrics Over Blood-Draw Dates',
    'Rolling averages of WHOOP and Strava signals plotted at each blood-draw date. Life-event markers shown.'
  );

  const tl = DATA.timeline || [];
  const labels = tl.map(r => r.date);

  // Chart builder helper
  function tlCard(title, datasets, yLabel, yLabel2, height) {{
    const card = document.createElement('div');
    card.className = 'chart-card full-card';
    card.innerHTML = `<div class="chart-header"><div class="chart-title">${{title}}</div></div>`;
    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    wrap.style.height = (height || 180) + 'px';
    const canvas = document.createElement('canvas');
    wrap.appendChild(canvas);
    card.appendChild(wrap);

    // Life-event plugin
    const lifePlugin = {{
      id: 'lifeEvents',
      afterDraw(chart) {{
        const {{ ctx, chartArea: {{ top, bottom }}, scales: {{ x }} }} = chart;
        if (!x) return;
        (DATA.life_events || []).forEach(ev => {{
          const idx = labels.indexOf(ev.date);
          if (idx < 0) return;
          const xPx = x.getPixelForValue(idx);
          ctx.save();
          ctx.beginPath(); ctx.moveTo(xPx, top); ctx.lineTo(xPx, bottom);
          ctx.strokeStyle = 'rgba(234,179,8,0.45)'; ctx.lineWidth = 1.5;
          ctx.setLineDash([4,3]); ctx.stroke();
          ctx.fillStyle = '#eab308'; ctx.font = '9px sans-serif';
          ctx.textAlign = 'center'; ctx.fillText(ev.label, xPx, top - 3);
          ctx.restore();
        }});
      }},
    }};

    const scales = {{
      x: {{ grid: {{ color: 'rgba(46,50,80,0.6)' }}, ticks: {{ color: '#8892a4', maxRotation: 45, maxTicksLimit: 20, font: {{ size: 9 }} }} }},
      y: {{ title: {{ display: !!yLabel, text: yLabel||'', color:'#8892a4', font:{{size:9}} }}, grid: {{ color: 'rgba(46,50,80,0.6)' }}, ticks: {{ color: '#8892a4', font: {{ size: 9 }} }} }},
    }};
    if (yLabel2) {{
      scales.y2 = {{ position: 'right', title: {{ display: true, text: yLabel2, color:'#8892a4', font:{{size:9}} }}, grid: {{ display: false }}, ticks: {{ color: '#8892a4', font: {{ size: 9 }} }} }};
    }}

    new Chart(canvas, {{
      type: 'line', plugins: [lifePlugin],
      data: {{ labels, datasets }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{ backgroundColor: '#1a1d27', borderColor: '#2e3250', borderWidth: 1, titleColor: '#e2e8f0', bodyColor: '#8892a4' }},
        }},
        scales,
      }},
    }});
    return card;
  }}

  const grid = document.createElement('div');
  grid.className = 'charts-grid';
  grid.style.gridTemplateColumns = '1fr';

  // Chart 1: HRV + Recovery (30d)
  grid.appendChild(tlCard('WHOOP: HRV & Recovery Score (30-day avg)', [
    {{ label: 'HRV rMSSD (ms)', data: tl.map(r => r.whoop_hrv_30d), borderColor: '#22c55e', backgroundColor:'transparent', borderWidth:2.5, pointRadius:5, tension:0.3, fill:false, yAxisID:'y', spanGaps:true }},
    {{ label: 'Recovery score', data: tl.map(r => r.whoop_recovery_30d), borderColor: '#60a5fa', backgroundColor:'rgba(96,165,250,0.1)', borderWidth:2, pointRadius:4, tension:0.3, fill:true, yAxisID:'y2', spanGaps:true }},
  ], 'HRV (ms)', 'Recovery (0–100)'));

  // Chart 2: RHR + Strain (30d)
  grid.appendChild(tlCard('WHOOP: Resting HR & Day Strain (30-day avg)', [
    {{ label: 'Resting HR (bpm)', data: tl.map(r => r.whoop_rhr_30d), borderColor: '#ef4444', backgroundColor:'transparent', borderWidth:2.5, pointRadius:5, tension:0.3, fill:false, yAxisID:'y', spanGaps:true }},
    {{ label: 'Day strain', data: tl.map(r => r.whoop_strain_30d), borderColor: '#f97316', backgroundColor:'rgba(249,115,22,0.1)', borderWidth:2, pointRadius:4, tension:0.3, fill:true, yAxisID:'y2', spanGaps:true }},
  ], 'RHR (bpm)', 'Strain (0–21)'));

  // Chart 3: Sleep
  grid.appendChild(tlCard('WHOOP: Sleep Hours & Performance (30-day avg)', [
    {{ label: 'Sleep hrs', data: tl.map(r => r.whoop_sleep_hrs_30d), borderColor: '#a855f7', backgroundColor:'rgba(168,85,247,0.1)', borderWidth:2.5, pointRadius:5, tension:0.3, fill:true, yAxisID:'y', spanGaps:true }},
    {{ label: 'Sleep performance %', data: tl.map(r => r.whoop_sleep_perf_30d), borderColor: '#818cf8', backgroundColor:'transparent', borderWidth:2, pointRadius:4, tension:0.3, fill:false, yAxisID:'y2', spanGaps:true }},
  ], 'Hours', 'Performance (%)'));

  // Chart 4: Strava load
  grid.appendChild(tlCard('Strava: Training Load (90-day window)', [
    {{ label: 'Total hours', data: tl.map(r => r.strava_hrs_90d), borderColor: '#f97316', backgroundColor:'rgba(249,115,22,0.1)', borderWidth:2.5, pointRadius:5, tension:0.3, fill:true, yAxisID:'y', spanGaps:true }},
    {{ label: 'Run miles', data: tl.map(r => r.strava_run_miles_90d), borderColor: '#22c55e', backgroundColor:'transparent', borderWidth:2, pointRadius:4, tension:0.3, fill:false, yAxisID:'y2', spanGaps:true }},
  ], 'Training hrs', 'Run miles'));

  // Chart 5: SpO2 + resp rate
  grid.appendChild(tlCard('WHOOP: SpO₂ & Respiratory Rate (30-day avg)', [
    {{ label: 'SpO₂ (%)', data: tl.map(r => r.whoop_spo2_30d), borderColor: '#60a5fa', backgroundColor:'rgba(96,165,250,0.1)', borderWidth:2.5, pointRadius:5, tension:0.3, fill:true, yAxisID:'y', spanGaps:true }},
    {{ label: 'Resp rate (br/min)', data: tl.map(r => r.whoop_resp_30d), borderColor: '#eab308', backgroundColor:'transparent', borderWidth:2, pointRadius:4, tension:0.3, fill:false, yAxisID:'y2', spanGaps:true }},
  ], 'SpO₂ (%)', 'Breaths/min'));

  sec.appendChild(grid);
  panel.appendChild(sec);
}});

// ---------------------------------------------------------------------------
// Tab 6: VO₂max estimated series
// ---------------------------------------------------------------------------
addTab('🫁 VO₂max', 'vo2max', panel => {{
  const sec = mkSection(
    'Estimated VO₂max — Full History (2011–2026)',
    `Lab measurements (2 points) anchored to estimated series from:
     <strong>VDOT</strong> (Jack Daniels formula from race times) and
     <strong>FTP w/kg</strong> (Coggan formula, calibrated to lab values).
     Non-lab estimates are scaled so they match the lab anchors within ±180 days.
     Each method shown in a different colour. Blood-draw dates marked.`
  );

  const series = DATA.vo2max_series || [];
  if (series.length === 0) {{ panel.appendChild(sec); return; }}

  // Group by method for separate datasets
  const METHOD_COLOR = {{
    'lab':            '#ffffff',
    'vdot_marathon':  '#22c55e',
    'vdot_5k':        '#86efac',
    'vdot_hm':        '#4ade80',
    'vdot_fm':        '#34d399',
    'vdot_10k':       '#6ee7b7',
    'ftp_coggan':     '#60a5fa',
  }};
  const METHOD_LABEL = {{
    'lab':           'Lab (measured)',
    'vdot_marathon': 'VDOT from marathon',
    'vdot_5k':       'VDOT from 5K',
    'vdot_hm':       'VDOT from half marathon',
    'vdot_fm':       'VDOT from full marathon (Strava)',
    'vdot_10k':      'VDOT from 10K',
    'ftp_coggan':    'FTP-based (Coggan, calibrated)',
  }};

  // All unique dates sorted for x-axis
  const allDates = [...new Set(series.map(p => p.date))].sort();

  // Build per-method datasets
  const methods = [...new Set(series.map(p => p.method))];
  const datasets = methods.map(method => {{
    const pts = series.filter(p => p.method === method);
    const ptMap = Object.fromEntries(pts.map(p => [p.date, p.value]));
    const isLab = method === 'lab';
    return {{
      label: METHOD_LABEL[method] || method,
      data: allDates.map(d => ptMap[d] ?? null),
      borderColor: METHOD_COLOR[method] || '#818cf8',
      backgroundColor: isLab ? 'white' : 'transparent',
      borderWidth: isLab ? 0 : 1.5,
      pointRadius: isLab ? 10 : (method.startsWith('vdot') ? 4 : 3),
      pointStyle: isLab ? 'star' : 'circle',
      pointHoverRadius: 7,
      tension: 0.3,
      fill: false,
      spanGaps: false,
    }};
  }});

  // Blood-draw date annotations plugin
  const drawDates = (DATA.timeline || []).map(r => r.date);
  const drawPlugin = {{
    id: 'drawDates',
    afterDraw(chart) {{
      const {{ ctx, chartArea: {{ top, bottom }}, scales: {{ x }} }} = chart;
      if (!x) return;
      drawDates.forEach(dd => {{
        const idx = allDates.indexOf(dd);
        if (idx < 0) return;
        const xPx = x.getPixelForValue(idx);
        ctx.save();
        ctx.beginPath(); ctx.moveTo(xPx, top); ctx.lineTo(xPx, bottom);
        ctx.strokeStyle = 'rgba(234,179,8,0.35)'; ctx.lineWidth = 1;
        ctx.setLineDash([3,3]); ctx.stroke();
        ctx.restore();
      }});
    }},
  }};

  const card = document.createElement('div');
  card.className = 'chart-card full-card';
  const hdr = document.createElement('div');
  hdr.className = 'chart-header';
  hdr.innerHTML = `<div class="chart-title">VO₂max estimated series — ${{series.length}} data points</div>`;
  card.appendChild(hdr);

  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  wrap.style.height = '280px';
  const canvas = document.createElement('canvas');
  wrap.appendChild(canvas);
  card.appendChild(wrap);

  // Legend
  const leg = document.createElement('div');
  leg.className = 'chart-legend';
  leg.style.marginTop = '0.6rem';
  methods.forEach(m => {{
    leg.innerHTML += `<div class="legend-item">
      <div class="legend-swatch" style="background:${{METHOD_COLOR[m]||'#818cf8'}};border-radius:50%;width:10px;height:10px;"></div>
      ${{METHOD_LABEL[m]||m}}
    </div>`;
  }});
  leg.innerHTML += `<div class="legend-item">
    <div style="width:12px;height:2px;background:rgba(234,179,8,0.5);display:inline-block;margin-right:4px;"></div>Blood draw date
  </div>`;
  card.appendChild(leg);

  const grid = document.createElement('div');
  grid.className = 'charts-grid';
  grid.style.gridTemplateColumns = '1fr';
  grid.appendChild(card);
  sec.appendChild(grid);
  panel.appendChild(sec);

  // Summary stats card
  const labPts = series.filter(p => p.method === 'lab');
  const ftpPts = series.filter(p => p.method === 'ftp_coggan');
  const vdotPts = series.filter(p => p.method.startsWith('vdot'));
  const statSec = mkSection('Coverage Summary', null);
  const statGrid = document.createElement('div');
  statGrid.className = 'pair-grid';
  [
    ['🔬 Lab measurements', labPts.length + ' actual VO₂max tests', '#ffffff'],
    ['🏃 VDOT from races', vdotPts.length + ' race-derived estimates', '#22c55e'],
    ['🚴 FTP-based (calibrated)', ftpPts.length + ' cycling estimates', '#60a5fa'],
    ['📅 Total span', (allDates[0]||'?') + ' → ' + (allDates[allDates.length-1]||'?'), '#818cf8'],
  ].forEach(([name, val, col]) => {{
    const card2 = document.createElement('div');
    card2.className = 'pair-card';
    card2.style.borderLeft = `3px solid ${{col}}`;
    card2.innerHTML = `<div class="pair-name">${{name}}</div><div class="pair-r" style="color:${{col}};font-size:0.95rem;">${{val}}</div>`;
    statGrid.appendChild(card2);
  }});
  statSec.appendChild(statGrid);
  panel.appendChild(statSec);

  new Chart(canvas, {{
    type: 'line',
    plugins: [drawPlugin],
    data: {{ labels: allDates, datasets }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1a1d27', borderColor: '#2e3250', borderWidth: 1,
          titleColor: '#e2e8f0', bodyColor: '#8892a4',
          callbacks: {{
            label: ctx => ctx.parsed.y !== null
              ? ` ${{ctx.dataset.label}}: ${{ctx.parsed.y.toFixed(1)}} ml/kg/min`
              : null,
          }},
          filter: item => item.parsed.y !== null,
        }},
      }},
      scales: {{
        x: {{
          grid: {{ color: 'rgba(46,50,80,0.6)' }},
          ticks: {{ color: '#8892a4', maxRotation: 45, maxTicksLimit: 24, font: {{ size: 9 }} }},
        }},
        y: {{
          title: {{ display: true, text: 'VO₂max (ml/kg/min)', color: '#8892a4', font: {{ size: 10 }} }},
          min: 35, max: 70,
          grid: {{ color: 'rgba(46,50,80,0.6)' }},
          ticks: {{ color: '#8892a4', font: {{ size: 10 }} }},
        }},
      }},
    }},
  }});
}});

// ---------------------------------------------------------------------------
// Shared: render scatter card
// ---------------------------------------------------------------------------
function renderScatterCard(container, sp) {{
  const card = document.createElement('div');
  card.className = 'chart-card';
  const col = rColor(sp.r);
  const hdr = document.createElement('div');
  hdr.className = 'chart-header';
  hdr.innerHTML = `
    <div class="chart-title">${{sp.x_label}}<br><span style="color:var(--muted);font-weight:400">vs</span> ${{sp.y_label}}</div>
    <div class="chart-meta">
      <div class="r-big" style="color:${{col}};">r = ${{sp.r !== null ? sp.r.toFixed(3) : '—'}}</div>
      <div class="n-lbl">n = ${{sp.n}}</div>
    </div>`;
  card.appendChild(hdr);
  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  const canvas = document.createElement('canvas');
  wrap.appendChild(canvas);
  card.appendChild(wrap);
  container.appendChild(card);

  const ptColors = scatterPointColors(sp.points);
  const datasets = [{{
    label: `${{sp.x_label}} vs ${{sp.y_label}}`,
    data: sp.points.map(p => ({{ x: p.x, y: p.y }})),
    backgroundColor: ptColors, borderColor: ptColors.map(c => c.replace('0.9','1')),
    borderWidth: 1.5, pointRadius: 6, pointHoverRadius: 8, type: 'scatter',
  }}];
  if (sp.reg_line) {{
    datasets.push({{
      label: 'Trend', data: sp.reg_line, type: 'line',
      borderColor: 'rgba(129,140,248,0.55)', borderWidth: 1.5,
      borderDash: [4,3], pointRadius: 0, fill: false,
    }});
  }}
  new Chart(canvas, {{
    type: 'scatter', data: {{ datasets }},
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
        x: {{ title: {{ display: true, text: sp.x_label, color:'#8892a4', font:{{size:9}} }}, grid: {{ color: 'rgba(46,50,80,0.6)' }}, ticks: {{ color:'#8892a4', font:{{size:9}} }} }},
        y: {{ title: {{ display: true, text: sp.y_label, color:'#8892a4', font:{{size:9}} }}, grid: {{ color: 'rgba(46,50,80,0.6)' }}, ticks: {{ color:'#8892a4', font:{{size:9}} }} }},
      }},
    }},
  }});
}}

// ---------------------------------------------------------------------------
// Section helper
// ---------------------------------------------------------------------------
function mkSection(title, descHTML) {{
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
// Footer
// ---------------------------------------------------------------------------
document.getElementById('footer').textContent =
  `Generated ${{DATA.generated}} · ${{DATA.n_draws}} blood draws · `+
  `${{(DATA.biomarker_matrix||[]).length}} biomarkers · `+
  `${{(DATA.covariate_meta||[]).length}} covariates (WHOOP + Strava + Life)`;
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
