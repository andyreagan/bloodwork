#!/usr/bin/env python3
"""
Generate models.html — simple predictive models for each biomarker.

Philosophy / constraints
------------------------
With n=5–15 blood draws per biomarker, fitting multivariate models risks
severe overfitting.  This file does the minimum that is statistically
honest:

  1. Single-predictor OLS (age only, then age + best wearable)
     — gives slope, R², and a plain-English reading of the trend.

  2. Leave-One-Out (LOO) cross-validation
     — for each draw: fit model on the other n-1 draws, predict the
       held-out draw.  Reports LOO-R² and mean absolute error (MAE).
       LOO is the right CV scheme for very small n (no wasted data,
       no train/test split instability).

  3. "What would my next blood draw look like?"
     — applies the LOO-validated model to today's wearable values
       to give a directional prediction with uncertainty.

  Hard limits enforced:
  - max 2 predictors (age + 1 wearable) to avoid degrees-of-freedom abuse
  - minimum n=7 for any model (gives LOO folds of n≥6)
  - minimum n=5 for wearable predictor within the overlapping draw set
  - prediction intervals shown as ±1 LOO-MAE (honest, distribution-free)

The best wearable predictor for each biomarker is chosen by the highest
|partial r| (age-adjusted) from the correlations analysis — the metric
most likely to add signal beyond the age trend.
"""
import json
import math
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

import yaml

import shell
from ranges import resolve_active

BLOODWORK_FILE = os.path.join(os.path.dirname(__file__), "bloodwork_data.yaml")
FITNESS_FILE   = os.path.join(os.path.dirname(__file__), "fitness_data.yaml")
CORR_HTML      = os.path.join(os.path.dirname(__file__), "correlations.html")
HTML_FILE      = os.path.join(os.path.dirname(__file__), "models.html")

STRAVA_DB  = os.path.expanduser("~/projects/2026/strava-database/strava.db")
WHOOP_DB   = os.path.expanduser("~/projects/2026/whoop-database/whoop.db")
GARMIN_DB  = os.path.expanduser("~/projects/2026/garmin-database/garmin.db")

BIRTH_YEAR = 1989

LIFE_EVENTS = [
    {"date": "2017-10-01", "label": "Kid #1", "kids": 1},
    {"date": "2019-04-01", "label": "Kid #2", "kids": 2},
    {"date": "2021-07-01", "label": "Kid #3", "kids": 3},
]

BIOMARKERS_TO_MODEL = [
    "Glucose", "HbA1c", "Cholesterol", "LDL", "HDL", "Triglycerides",
    "Testosterone", "Free Testosterone", "SHBG",
    "Vitamin D", "Ferritin", "hsCRP",
    "AST", "ALT", "Hemoglobin", "Hematocrit",
]

# Wearable covariates — same as correlations page
WEARABLE_COVARIATES = [
    ("whoop_hrv_30d",         "HRV 30d avg (ms)",            "WHOOP"),
    ("whoop_recovery_30d",    "Recovery score 30d avg",       "WHOOP"),
    ("whoop_rhr_30d",         "Resting HR 30d avg (bpm)",     "WHOOP"),
    ("whoop_strain_30d",      "Day strain 30d avg",           "WHOOP"),
    ("whoop_sleep_hrs_30d",   "Sleep hours 30d avg",          "WHOOP"),
    ("whoop_sleep_perf_30d",  "Sleep performance 30d avg",    "WHOOP"),
    ("whoop_spo2_30d",        "SpO₂ 30d avg (%)",             "WHOOP"),
    ("whoop_resp_30d",        "Resp rate 30d avg (br/min)",   "WHOOP"),
    ("whoop_skin_temp_30d",   "Skin temp 30d avg (°C)",       "WHOOP"),
    ("whoop_hrv_90d",         "HRV 90d avg (ms)",             "WHOOP"),
    ("whoop_recovery_90d",    "Recovery score 90d avg",       "WHOOP"),
    ("whoop_rhr_90d",         "Resting HR 90d avg (bpm)",     "WHOOP"),
    ("whoop_strain_90d",      "Day strain 90d avg",           "WHOOP"),
    ("whoop_sleep_hrs_90d",   "Sleep hours 90d avg",          "WHOOP"),
    ("whoop_sleep_perf_90d",  "Sleep performance 90d avg",    "WHOOP"),
    ("strava_hrs_90d",        "Training hours 90d",           "Strava"),
    ("strava_run_miles_90d",  "Run miles 90d",                "Strava"),
    ("strava_ride_hrs_90d",   "Ride hours 90d",               "Strava"),
    ("strava_run_hrs_90d",    "Run hours 90d",                "Strava"),
    ("strava_avg_hr_90d",     "Avg workout HR 90d (bpm)",     "Strava"),
    ("strava_avg_watts_90d",  "Avg cycling watts 90d",        "Strava"),
    ("garmin_steps_90d",      "Steps 90d avg",                "Garmin"),
    ("garmin_stress_90d",     "Stress 90d avg",               "Garmin"),
    ("garmin_bb_hi_90d",      "Body battery hi 90d avg",      "Garmin"),
    ("garmin_intensity_90d",  "Intensity mins 90d avg",       "Garmin"),
    ("weight",                "Weight (lbs)",                 "Life"),
    ("vo2max",                "VO₂max (ml/kg/min)",           "Fitness"),
]


# ---------------------------------------------------------------------------
# Maths — pure Python, no numpy
# ---------------------------------------------------------------------------

def mean(xs):
    return sum(xs) / len(xs)


def ols1(xs, ys):
    """Simple OLS: y = a + b*x. Returns (intercept, slope) or None."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    b = num / den
    return my - b * mx, b          # (intercept, slope)


def ols2(x1s, x2s, ys):
    """OLS with two predictors: y = a + b1*x1 + b2*x2.
    Uses normal equations via 3×3 matrix inversion (Cramer's rule).
    Returns (intercept, b1, b2) or None.
    """
    n = len(ys)
    if n < 5:
        return None
    # Design matrix columns: [1, x1, x2]
    s1  = sum(x1s)
    s2  = sum(x2s)
    sy  = sum(ys)
    s11 = sum(a * a for a in x1s)
    s22 = sum(a * a for a in x2s)
    s12 = sum(a * b for a, b in zip(x1s, x2s))
    s1y = sum(a * y for a, y in zip(x1s, ys))
    s2y = sum(a * y for a, y in zip(x2s, ys))

    # Normal equations: [[n, s1, s2], [s1, s11, s12], [s2, s12, s22]] * [a, b1, b2] = [sy, s1y, s2y]
    A = [[n,  s1,  s2],
         [s1, s11, s12],
         [s2, s12, s22]]
    b_vec = [sy, s1y, s2y]

    def det3(m):
        return (m[0][0]*(m[1][1]*m[2][2]-m[1][2]*m[2][1])
               -m[0][1]*(m[1][0]*m[2][2]-m[1][2]*m[2][0])
               +m[0][2]*(m[1][0]*m[2][1]-m[1][1]*m[2][0]))

    dA = det3(A)
    if abs(dA) < 1e-12:
        return None

    def replace_col(M, col, v):
        import copy
        R = copy.deepcopy(M)
        for i in range(3):
            R[i][col] = v[i]
        return R

    a  = det3(replace_col(A, 0, b_vec)) / dA
    b1 = det3(replace_col(A, 1, b_vec)) / dA
    b2 = det3(replace_col(A, 2, b_vec)) / dA
    return a, b1, b2


def r_squared(ys, y_preds):
    my = mean(ys)
    ss_res = sum((y - yp) ** 2 for y, yp in zip(ys, y_preds))
    ss_tot = sum((y - my) ** 2 for y in ys)
    if ss_tot == 0:
        return None
    return 1.0 - ss_res / ss_tot


def mae(ys, y_preds):
    return mean([abs(y - yp) for y, yp in zip(ys, y_preds)])


def loo_cv_1(xs, ys):
    """LOO cross-validation for single-predictor OLS.
    Returns (loo_preds, loo_r2, loo_mae) or None."""
    n = len(xs)
    if n < 4:
        return None
    preds = []
    for i in range(n):
        xi = [xs[j] for j in range(n) if j != i]
        yi = [ys[j] for j in range(n) if j != i]
        fit = ols1(xi, yi)
        if fit is None:
            return None
        a, b = fit
        preds.append(a + b * xs[i])
    r2  = r_squared(ys, preds)
    err = mae(ys, preds)
    return preds, r2, err


def loo_cv_2(x1s, x2s, ys):
    """LOO cross-validation for two-predictor OLS.
    Returns (loo_preds, loo_r2, loo_mae) or None."""
    n = len(ys)
    if n < 6:
        return None
    preds = []
    for i in range(n):
        x1i = [x1s[j] for j in range(n) if j != i]
        x2i = [x2s[j] for j in range(n) if j != i]
        yi  = [ys[j]  for j in range(n) if j != i]
        fit = ols2(x1i, x2i, yi)
        if fit is None:
            return None
        a, b1, b2 = fit
        preds.append(a + b1 * x1s[i] + b2 * x2s[i])
    r2  = r_squared(ys, preds)
    err = mae(ys, preds)
    return preds, r2, err


def pearson_r(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx  = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy  = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def partial_r(xs, ys, control):
    """Partial r of xs and ys controlling for control."""
    n = len(xs)
    if n < 4:
        return None
    def resid(zs, cs):
        fit = ols1(cs, zs)
        if fit is None:
            return list(zs)
        a, b = fit
        return [z - (a + b * c) for z, c in zip(zs, cs)]
    rx = resid(xs, control)
    ry = resid(ys, control)
    return pearson_r(rx, ry)


# ---------------------------------------------------------------------------
# Data loading (same pattern as generate_correlations.py)
# ---------------------------------------------------------------------------

def load_bloodwork(path):
    with open(path, encoding="utf-8") as f:
        bw = yaml.safe_load(f)
    ref_ranges = resolve_active(bw["reference_ranges"])
    measurements = {}
    for draw in bw["draws"]:
        for bm, m in draw["measurements"].items():
            measurements.setdefault(bm, []).append({
                "date": draw["date"], "value": m["value"], "unit": m["unit"],
            })
    for k in measurements:
        measurements[k].sort(key=lambda x: x["date"])
    return measurements, ref_ranges


def load_fitness(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for k in list(data):
        if data[k] is None:
            data[k] = []
    return data


def make_prior_lookup(series, value_key="value"):
    series = sorted([e for e in series if e], key=lambda e: e["date"])
    def lookup(date_str):
        val = None
        for e in series:
            if e["date"] <= date_str:
                val = e[value_key]
            else:
                break
        return val
    return lookup


def kids_count_at(date_str):
    count = 0
    for ev in LIFE_EVENTS:
        if date_str >= ev["date"]:
            count = ev["kids"]
    return count


def load_whoop_windows(db_path, draw_dates):
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    result = {}
    for draw in draw_dates:
        row_data = {}
        for days, suffix in [(30, "30d"), (90, "90d")]:
            since = (datetime.strptime(draw, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
            cur.execute("""
                SELECT AVG(hrv_rmssd_ms) AS hrv, AVG(recovery_score) AS recovery,
                       AVG(resting_hr) AS rhr, AVG(strain) AS strain,
                       AVG(sleep_total_in_bed_ms)/3600000.0 AS sleep_hrs,
                       AVG(sleep_performance_pct) AS sleep_perf,
                       AVG(spo2_pct) AS spo2, AVG(respiratory_rate) AS resp,
                       AVG(skin_temp_celsius) AS skin_temp, COUNT(*) AS n_days
                FROM daily WHERE date >= ? AND date < ? AND hrv_rmssd_ms IS NOT NULL
            """, (since, draw))
            r = cur.fetchone()
            if r and r["n_days"] and r["n_days"] >= 7:
                row_data.update({
                    f"whoop_hrv_{suffix}":        r["hrv"],
                    f"whoop_recovery_{suffix}":   r["recovery"],
                    f"whoop_rhr_{suffix}":        r["rhr"],
                    f"whoop_strain_{suffix}":     r["strain"],
                    f"whoop_sleep_hrs_{suffix}":  r["sleep_hrs"],
                    f"whoop_sleep_perf_{suffix}": r["sleep_perf"],
                })
                if suffix == "30d":
                    row_data.update({
                        "whoop_spo2_30d":      r["spo2"],
                        "whoop_resp_30d":      r["resp"],
                        "whoop_skin_temp_30d": r["skin_temp"],
                    })
        result[draw] = row_data
    conn.close()
    return result


def load_strava_windows(db_path, draw_dates):
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    result = {}
    for draw in draw_dates:
        since = (datetime.strptime(draw, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
        cur.execute("""
            SELECT SUM(moving_time_s)/3600.0 AS total_hrs,
                   SUM(CASE WHEN sport_type IN ('Run','TrailRun','VirtualRun')
                            THEN distance_m*0.000621371 ELSE 0 END) AS run_miles,
                   SUM(CASE WHEN sport_type IN ('Run','TrailRun','VirtualRun')
                            THEN moving_time_s/3600.0 ELSE 0 END) AS run_hrs,
                   SUM(CASE WHEN sport_type IN ('Ride','VirtualRide','GravelRide','MountainBikeRide','EBikeRide')
                            THEN moving_time_s/3600.0 ELSE 0 END) AS ride_hrs,
                   AVG(CASE WHEN average_heartrate > 0 THEN average_heartrate END) AS avg_hr,
                   AVG(CASE WHEN average_watts > 0 AND device_watts=1 THEN average_watts END) AS avg_watts,
                   COUNT(*) AS n_act
            FROM activities WHERE date(start_date_local) >= ? AND date(start_date_local) < ?
        """, (since, draw))
        r = cur.fetchone()
        if r and r["n_act"] and r["n_act"] >= 3:
            result[draw] = {
                "strava_hrs_90d":       r["total_hrs"],
                "strava_run_miles_90d": r["run_miles"],
                "strava_run_hrs_90d":   r["run_hrs"],
                "strava_ride_hrs_90d":  r["ride_hrs"],
                "strava_avg_hr_90d":    r["avg_hr"],
                "strava_avg_watts_90d": r["avg_watts"],
            }
        else:
            result[draw] = {}
    conn.close()
    return result


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


def get_current_wearable_values(whoop_db, strava_db, garmin_db, weight_lookup, vo2_lookup):
    """Pull today's 30-day and 90-day wearable averages for the prediction card."""
    today = date.today().isoformat()
    whoop_now  = load_whoop_windows(whoop_db,   [today]).get(today, {})
    strava_now = load_strava_windows(strava_db, [today]).get(today, {})
    garmin_now = load_garmin_windows(garmin_db, [today]).get(today, {})
    return {
        **whoop_now,
        **strava_now,
        **garmin_now,
        "age":    date.today().year - BIRTH_YEAR,
        "kids":   kids_count_at(today),
        "weight": weight_lookup(today),
        "vo2max": vo2_lookup(today),
    }


# ---------------------------------------------------------------------------
# VO₂max estimation (abbreviated version from generate_correlations.py)
# ---------------------------------------------------------------------------

LAB_VO2MAX = [("2023-06-21", 55.6), ("2025-02-15", 60.6)]

def vdot_from_race(dist_m, time_s):
    if time_s <= 0 or dist_m <= 0:
        return None
    t = time_s / 60.0
    v = dist_m / t
    pct = 0.8 + 0.1894393*math.exp(-0.012778*t) + 0.2989558*math.exp(-0.1932605*t)
    vo2 = -4.60 + 0.182258*v + 0.000104*v**2
    return vo2 / pct if pct > 0 else None


def build_vo2max_lookup(fitness, strava_db):
    """Return a prior-value lookup for estimated VO₂max."""
    points = [{"date": d, "value": v, "method": "lab"} for d, v in LAB_VO2MAX]

    weight_lookup = make_prior_lookup(fitness.get("weight_lbs", []))
    for e in fitness.get("ftp_watts", []):
        w = weight_lookup(e["date"])
        if w:
            est = (e["value"] / (w * 0.453592)) * 10.8 + 7
            points.append({"date": e["date"], "value": est, "method": "ftp_coggan"})

    for e in fitness.get("marathon_times", []):
        secs = e.get("time_seconds", 0)
        if secs and secs < 5*3600:
            vd = vdot_from_race(42195, secs)
            if vd and 30 < vd < 80:
                points.append({"date": e["date"], "value": vd, "method": "vdot_marathon"})

    for e in fitness.get("5k_times", []):
        vd = vdot_from_race(5000, e.get("time_seconds", 0))
        if vd and 30 < vd < 80:
            points.append({"date": e["date"], "value": vd, "method": "vdot_5k"})

    # Strava best monthly 5K/10K
    if os.path.exists(strava_db):
        conn = sqlite3.connect(strava_db)
        cur = conn.cursor()
        cur.execute("""SELECT date(start_date_local) as dt, distance_m, moving_time_s
                       FROM activities
                       WHERE sport_type IN ('Run','VirtualRun','TrailRun')
                         AND moving_time_s > 0 AND distance_m BETWEEN 4600 AND 10500
                         AND manual=0 ORDER BY dt""")
        from collections import defaultdict
        monthly = defaultdict(lambda: {"5k": None, "10k": None})
        for dt, d, t in cur.fetchall():
            ym = dt[:7]
            if 4600 <= d <= 5400:
                if monthly[ym]["5k"] is None or t < monthly[ym]["5k"][1]:
                    monthly[ym]["5k"] = (dt, t, d)
            if 9500 <= d <= 10500:
                if monthly[ym]["10k"] is None or t < monthly[ym]["10k"][1]:
                    monthly[ym]["10k"] = (dt, t, d)
        conn.close()
        for ym, bests in monthly.items():
            for band, entry in bests.items():
                if entry:
                    dt, t, d = entry
                    vd = vdot_from_race(d, t)
                    if vd and 30 < vd < 80:
                        points.append({"date": dt, "value": vd, "method": f"vdot_{band}"})

    # Calibrate to lab anchors
    lab_dates = [p["date"] for p in points if p["method"] == "lab"]
    lab_vals  = {p["date"]: p["value"] for p in points if p["method"] == "lab"}
    if len(lab_dates) >= 2:
        lab_sorted = sorted(lab_dates)
        lab_dt = [datetime.strptime(d, "%Y-%m-%d") for d in lab_sorted]
        from collections import defaultdict as dd
        anchor_ratios = []
        for ld in lab_sorted:
            ld_dt = datetime.strptime(ld, "%Y-%m-%d")
            ratios = dd(list)
            for p in points:
                if p["method"] == "lab": continue
                gap = abs((datetime.strptime(p["date"], "%Y-%m-%d") - ld_dt).days)
                if gap <= 120:
                    ratios[p["method"]].append(lab_vals[ld] / p["value"])
            anchor_ratios.append({m: sum(v)/len(v) for m, v in ratios.items()})
        for p in points:
            if p["method"] == "lab": continue
            p_dt = datetime.strptime(p["date"], "%Y-%m-%d")
            span = (lab_dt[1] - lab_dt[0]).days
            t = max(0.0, min(1.0, (p_dt - lab_dt[0]).days / span if span > 0 else 0.5))
            r0 = anchor_ratios[0].get(p["method"])
            r1 = anchor_ratios[1].get(p["method"])
            if r0 and r1:
                scale = r0 + t * (r1 - r0)
            elif r0:
                scale = r0
            elif r1:
                scale = r1
            else:
                scale = 1.0
            p["value"] = round(p["value"] * scale, 1)

    TRUST = {"lab":0,"vdot_marathon":1,"vdot_5k":2,"vdot_hm":3,"vdot_fm":4,"vdot_10k":5,"ftp_coggan":6}
    best = {}
    for p in points:
        d = p["date"]
        if d not in best or TRUST.get(p["method"],9) < TRUST.get(best[d]["method"],9):
            best[d] = p
    return make_prior_lookup(sorted(best.values(), key=lambda p: p["date"]))


# ---------------------------------------------------------------------------
# Core model building
# ---------------------------------------------------------------------------

def build_models(measurements, ref_ranges, fitness,
                 whoop_windows, strava_windows, garmin_windows):

    weight_lookup = make_prior_lookup(fitness.get("weight_lbs", []))
    vo2_lookup    = build_vo2max_lookup(fitness, STRAVA_DB)

    all_dates = sorted({pt["date"] for pts in measurements.values() for pt in pts})

    def cov_row(d):
        row = {
            "age":    int(d[:4]) - BIRTH_YEAR,
            "kids":   kids_count_at(d),
            "weight": weight_lookup(d),
            "vo2max": vo2_lookup(d),
        }
        row.update(whoop_windows.get(d, {}))
        row.update(strava_windows.get(d, {}))
        row.update(garmin_windows.get(d, {}))
        return row

    cov_rows = {d: cov_row(d) for d in all_dates}
    today_covs = get_current_wearable_values(WHOOP_DB, STRAVA_DB, GARMIN_DB, weight_lookup, vo2_lookup)

    models = []

    for bm in BIOMARKERS_TO_MODEL:
        data = measurements.get(bm)
        if not data:
            continue

        bm_by_date = {pt["date"]: pt["value"] for pt in data}
        ref = ref_ranges.get(bm, {})

        # Dates with age available (all) vs with wearable data
        age_dates = sorted(d for d in all_dates if d in bm_by_date)
        if len(age_dates) < 5:
            continue

        ages  = [cov_rows[d]["age"] for d in age_dates]
        bm_y  = [bm_by_date[d]     for d in age_dates]

        # --- Model A: age only ---
        fit_a = ols1(ages, bm_y)
        if fit_a is None:
            continue
        a_intercept, a_slope = fit_a
        preds_a_full = [a_intercept + a_slope * x for x in ages]
        r2_a   = r_squared(bm_y, preds_a_full)
        loo_a  = loo_cv_1(ages, bm_y)

        # --- Find best wearable predictor (by partial r with age) ---
        best_wearable = None
        best_pr = 0.0
        for ckey, clabel, csource in WEARABLE_COVARIATES:
            w_dates = [d for d in age_dates if cov_rows[d].get(ckey) is not None]
            if len(w_dates) < 5:
                continue
            wx = [cov_rows[d][ckey]  for d in w_dates]
            wy = [bm_by_date[d]      for d in w_dates]
            wa = [cov_rows[d]["age"] for d in w_dates]
            pr = partial_r(wx, wy, wa)
            if pr is not None and abs(pr) > abs(best_pr):
                best_pr = pr
                best_wearable = (ckey, clabel, csource, w_dates, wx, wy, wa)

        # --- Model B: age + best wearable ---
        model_b = None
        if best_wearable and abs(best_pr) >= 0.3:
            ckey, clabel, csource, w_dates, wx, wy, wa = best_wearable
            if len(w_dates) >= 6:
                fit_b = ols2(wa, wx, wy)
                if fit_b:
                    b_int, b_age, b_w = fit_b
                    preds_b_full = [b_int + b_age * a + b_w * w
                                    for a, w in zip(wa, wx)]
                    r2_b  = r_squared(wy, preds_b_full)
                    loo_b = loo_cv_2(wa, wx, wy)

                    # Prediction for next draw
                    today_age = today_covs.get("age", date.today().year - BIRTH_YEAR)
                    today_w   = today_covs.get(ckey)
                    pred_next = None
                    pred_interval = None
                    if today_w is not None and loo_b:
                        pred_next = b_int + b_age * today_age + b_w * today_w
                        pred_interval = loo_b[2]  # LOO MAE as ±interval

                    # Direction of wearable coefficient
                    def sign_word(v):
                        return "increases" if v > 0 else "decreases"

                    model_b = {
                        "predictor_key":   ckey,
                        "predictor_label": clabel,
                        "predictor_source": csource,
                        "partial_r":       round(best_pr, 3),
                        "b_intercept":     round(b_int, 4),
                        "b_age":           round(b_age, 4),
                        "b_wearable":      round(b_w, 4),
                        "r2_insample":     round(r2_b, 3) if r2_b is not None else None,
                        "loo_r2":          round(loo_b[1], 3) if loo_b and loo_b[1] is not None else None,
                        "loo_mae":         round(loo_b[2], 3) if loo_b else None,
                        "n":               len(w_dates),
                        "pred_next":       round(pred_next, 2) if pred_next is not None else None,
                        "pred_interval":   round(pred_interval, 2) if pred_interval is not None else None,
                        "today_predictor": round(today_w, 2) if today_w is not None else None,
                        "wearable_sign_word": sign_word(b_w),
                        # LOO plot data
                        "loo_dates":       w_dates,
                        "loo_actual":      [round(v, 3) for v in wy],
                        "loo_predicted":   [round(v, 3) for v in loo_b[0]] if loo_b else [],
                    }

        # Age-only LOO plot data
        loo_a_preds = loo_a[0] if loo_a else [round(a_intercept + a_slope * x, 3) for x in ages]

        unit = data[0]["unit"] if data else ""
        models.append({
            "biomarker":     bm,
            "unit":          unit,
            "ref":           ref,
            "n":             len(age_dates),
            "dates":         age_dates,
            "actual":        [round(v, 3) for v in bm_y],
            # Model A
            "model_a": {
                "intercept":  round(a_intercept, 4),
                "slope_age":  round(a_slope, 4),
                "r2_insample": round(r2_a, 3) if r2_a is not None else None,
                "loo_r2":     round(loo_a[1], 3) if loo_a and loo_a[1] is not None else None,
                "loo_mae":    round(loo_a[2], 3) if loo_a else None,
                "loo_predicted": [round(v, 3) for v in loo_a_preds],
                "pred_next":  round(a_intercept + a_slope * today_covs.get("age", date.today().year - BIRTH_YEAR), 2),
            },
            # Model B (may be None)
            "model_b": model_b,
        })

    # Sort: models with the best LOO R² (model B if available, else A) first
    def sort_key(m):
        b = m.get("model_b")
        r2 = (b["loo_r2"] if b and b["loo_r2"] is not None else None) or m["model_a"].get("loo_r2") or -99
        return -r2

    models.sort(key=sort_key)
    return models, today_covs


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def status_color(value, ref):
    if not ref:
        return "#8892a4"
    lo  = ref.get("low")
    hi  = ref.get("high")
    olo = ref.get("optimal_low")
    ohi = ref.get("optimal_high")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        return "#ef4444"
    if (olo is None or value >= olo) and (ohi is None or value <= ohi):
        return "#22c55e"
    return "#eab308"


PAGE_STYLE = r"""<style>
.topbar .meta { font-size: 0.75rem; color: var(--muted); }
.note.warn { background: var(--warn-bg); border-color: var(--warn); color: var(--warn-ink); }
.metric-card.model, #overview { scroll-margin-top: calc(var(--bar-h) + 80px); }
.model-bm { font-size: 1rem; font-weight: 700; }
.model-unit { font-size: 0.75rem; color: var(--muted); font-weight: 400; }
.badge-row { display: flex; gap: 6px; flex-wrap: wrap; margin: 4px 0 12px; }
.badge .sw { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; }
.latest { text-align: right; flex-shrink: 0; }
.latest .metric-card-val { font-size: 1.4rem; }
.n-lbl { font-size: 0.7rem; color: var(--muted); white-space: nowrap; }
.model-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin-bottom: 14px; }
.sub-title { font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }
.metric-row { display: flex; justify-content: space-between; align-items: baseline; gap: 10px;
              padding: 3px 0; border-bottom: 1px solid var(--border); font-size: 0.8rem; }
.metric-row:last-child { border-bottom: none; }
.metric-label { color: var(--muted); }
.metric-val { font-weight: 600; font-variant-numeric: tabular-nums; text-align: right; }
.r2-track { height: 5px; background: var(--surface-2); border-radius: 3px; margin-top: 6px; }
.r2-fill { height: 5px; border-radius: 3px; }
.r2-lbl { font-size: 0.68rem; color: var(--muted); margin-top: 3px; }
.no-b { color: var(--muted); font-size: 0.8rem; }
.pred-box { background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
            padding: 12px 16px; margin-bottom: 16px; }
.pred-val { font-size: 1.5rem; font-weight: 800; line-height: 1.2; }
.pred-val span { font-size: 0.9rem; font-weight: 400; color: var(--muted); }
.pred-range { font-size: 0.75rem; color: var(--muted); margin-top: 2px; }
.pred-note { font-size: 0.72rem; color: var(--muted); margin-top: 4px; line-height: 1.5; }
</style>"""

PAGE_BODY = r"""<body>
__SIDEBAR__
<div class="main">
  <div class="topbar">
    <div>
      <h1>Predictive Models</h1>
      <div class="subtitle">Age-only and age + best wearable predictor · LOO cross-validated · __TODAY__</div>
    </div>
    <div class="meta" id="meta"></div>
  </div>

  <div class="content">
    <div class="note warn" id="overview">
      <strong>⚠️ Statistical disclaimer</strong> — These are simple OLS models with n=5–15 data points.
      Results are directional and exploratory, not clinical.
      In-sample R² is optimistic; <strong>LOO R²</strong> (leave-one-out cross-validated) is the honest
      estimate of predictive power. Where LOO R² is negative, the model predicts worse than the mean —
      treat it as noise. Max 2 predictors (age + 1 wearable) to avoid overfitting.
      Prediction intervals are ±1 LOO-MAE, not formal confidence intervals.
    </div>

    <div class="kpi-strip">
      <div class="kpi-card" style="--kpi-color:var(--accent)"><div class="kpi-label">biomarkers modelled</div><div class="kpi-value">__N_MODELS__</div></div>
      <div class="kpi-card" style="--kpi-color:var(--teal)"><div class="kpi-label">with wearable predictor</div><div class="kpi-value">__N_WITH_B__</div></div>
      <div class="kpi-card" style="--kpi-color:var(--gray)"><div class="kpi-label">Andy's age today</div><div class="kpi-value">__AGE__</div></div>
      <div class="kpi-card" style="--kpi-color:var(--blue)"><div class="kpi-label">HRV 30d avg today</div><div class="kpi-value" id="sv-hrv">—</div></div>
      <div class="kpi-card" style="--kpi-color:var(--purple)"><div class="kpi-label">Sleep hrs 30d today</div><div class="kpi-value" id="sv-sleep">—</div></div>
      <div class="kpi-card" style="--kpi-color:var(--orange)"><div class="kpi-label">Strain 30d today</div><div class="kpi-value" id="sv-strain">—</div></div>
    </div>

    <div class="metric-grid wide">
      <div class="metric-card">
        <div class="metric-card-header"><div class="metric-card-title">LOO R² by biomarker</div></div>
        <div class="metric-card-sub">Model B where a wearable predictor qualified, otherwise Model A. Bars are cut off at −1; below zero the model predicts worse than the mean.</div>
        <div class="legend" id="r2-legend"></div>
        <div class="chart-area" id="r2-chart"></div>
      </div>
    </div>

    <div class="metric-grid" id="models-container"></div>
  </div>
</div>

<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);

// series identity, used by badges, legends and charts alike
const C_ACTUAL = 'var(--blue)', C_A = 'var(--orange)', C_B = 'var(--teal)';
const sw = c => `<span class="sw" style="background:${c}"></span>`;

// Fill summary strip with today's wearable values
const t = DATA.today;
const fmt = (v, dp = 1) => v !== null && v !== undefined ? v.toFixed(dp) : '—';
$('sv-hrv').textContent    = fmt(t.whoop_hrv_30d, 1);
$('sv-sleep').textContent  = fmt(t.whoop_sleep_hrs_30d, 1);
$('sv-strain').textContent = fmt(t.whoop_strain_30d, 1);
$('meta').textContent = `${DATA.models.length} biomarkers · LOO cross-validated · max 2 predictors`;

// Status colour from ref ranges
function refColor(val, ref) {
  if (!ref || val === null || val === undefined) return 'var(--text)';
  const lo = ref.low ?? null, hi = ref.high ?? null, olo = ref.optimal_low ?? null, ohi = ref.optimal_high ?? null;
  if ((lo !== null && val < lo) || (hi !== null && val > hi)) return 'var(--bad)';
  if ((olo === null || val >= olo) && (ohi === null || val <= ohi)) return 'var(--good)';
  return 'var(--warn)';
}
function r2Color(r2) {
  if (r2 === null || r2 === undefined) return 'var(--muted)';
  if (r2 >= 0.6) return 'var(--good)';
  if (r2 >= 0.3) return 'var(--warn)';
  if (r2 >= 0)   return 'var(--gray)';
  return 'var(--bad)';
}
function r2Label(r2) {
  if (r2 === null || r2 === undefined) return '—';
  if (r2 >= 0.7) return 'strong';
  if (r2 >= 0.4) return 'moderate';
  if (r2 >= 0.1) return 'weak';
  if (r2 >= 0)   return 'near-zero';
  return 'worse than mean';
}
function metricRows(pairs) {
  return '<div>' + pairs.map(([l, v]) =>
    `<div class="metric-row"><span class="metric-label">${l}</span><span class="metric-val">${v}</span></div>`
  ).join('') + '</div>';
}
function r2Bar(r2) {
  if (r2 === null || r2 === undefined) return '';
  const pct = Math.max(0, Math.min(100, Math.round(r2 * 100)));
  return `<div title="LOO R² = ${r2}">
    <div class="r2-track"><div class="r2-fill" style="width:${pct}%;background:${r2Color(r2)}"></div></div>
    <div class="r2-lbl">LOO R² = ${r2.toFixed(2)} (${r2Label(r2)})</div>
  </div>`;
}
const slug = s => 'm-' + s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
const hasModelB = m => !!(m.model_b && m.model_b.loo_r2 !== null);

// ── overview: best-model LOO R² per biomarker ──────────────────────────────
(function () {
  const rows = DATA.models.map(m => {
    const b = hasModelB(m), r2 = b ? m.model_b.loo_r2 : m.model_a.loo_r2;
    return { m, b, r2 };
  }).filter(d => d.r2 !== null && d.r2 !== undefined);
  AR.legend('#r2-legend', [
    rows.some(d => !d.b) && { name: 'Model A (age only)', color: C_A },
    rows.some(d => d.b) && { name: 'Model B (age + wearable)', color: C_B }].filter(Boolean));
  AR.hbarChart('#r2-chart', {
    labels: rows.map(d => d.m.biomarker), values: rows.map(d => Math.max(-1, d.r2)),
    labelWidth: 130, rowHeight: 24,
    colors: (v, i) => rows[i].b ? C_B : C_A,
    fmt: v => v.toFixed(2),
    tip: i => {
      const d = rows[i], A = d.m.model_a, B = d.m.model_b;
      return `<b>${d.m.biomarker}</b><br>${sw(C_A)}<span class="dim">Model A: age only · LOO R²</span> ${A.loo_r2 !== null ? A.loo_r2.toFixed(2) : '—'}` +
        (d.b ? `<br>${sw(C_B)}<span class="dim">Model B: +${B.predictor_label} · LOO R²</span> ${B.loo_r2.toFixed(2)}` : '') +
        `<br><span class="dim">${r2Label(d.r2)}</span>`;
    },
  });
  // hbarChart prints the plotted (clamped) value; show the real one where it was cut off
  const fix = () => d3.select('#r2-chart').selectAll('.val-label').text((i) => rows[i].r2.toFixed(2));
  fix(); new ResizeObserver(() => requestAnimationFrame(fix)).observe($('r2-chart'));
})();

// ── one card per biomarker ─────────────────────────────────────────────────
const container = $('models-container');

DATA.models.forEach(m => {
  const A = m.model_a;
  const B = m.model_b;
  const hasB = hasModelB(m);

  const card = document.createElement('div');
  card.className = 'metric-card model';
  card.id = slug(m.biomarker);

  const latestActual = m.actual[m.actual.length - 1];
  const latestDate   = m.dates[m.dates.length - 1];

  const badges = [
    `<span class="badge">${sw(C_A)}Model A: age only · LOO-R²=${A.loo_r2 !== null ? A.loo_r2.toFixed(2) : '—'}</span>`,
    hasB ? `<span class="badge">${sw(C_B)}Model B: +${B.predictor_label} · LOO-R²=${B.loo_r2 !== null ? B.loo_r2.toFixed(2) : '—'}</span>` : '',
    (A.loo_r2 !== null && A.loo_r2 < 0 && (!hasB || B.loo_r2 < 0)) ? `<span class="badge warn">⚠ predictive power is noise</span>` : '',
  ].filter(Boolean).join('');

  // Model A column
  let colA = `<div><div class="sub-title">Model A — Age only (n=${m.n})</div>`;
  colA += metricRows([
    ['Slope (per year)', A.slope_age > 0 ? `+${A.slope_age.toFixed(3)}` : A.slope_age.toFixed(3)],
    ['In-sample R²',     A.r2_insample !== null ? A.r2_insample.toFixed(3) : '—'],
    ['LOO R²',           A.loo_r2 !== null ? A.loo_r2.toFixed(3) : '—'],
    ['LOO MAE',          A.loo_mae !== null ? `±${A.loo_mae.toFixed(2)} ${m.unit}` : '—'],
    ['Age-only prediction', `${A.pred_next !== undefined ? A.pred_next.toFixed(1) : '—'} ${m.unit}`],
  ]) + r2Bar(A.loo_r2) + '</div>';

  // Model B column
  let colB;
  if (hasB) {
    const srcBadge = {'WHOOP':'⌚','Strava':'🏃','Life':'⚖️','Fitness':'🫁'}[B.predictor_source] || '';
    const bSign = B.b_wearable > 0 ? '+' : '';
    colB = `<div><div class="sub-title">Model B — Age + ${srcBadge} ${B.predictor_label} (n=${B.n})</div>` +
      metricRows([
        ['Partial r (age-adj)',  B.partial_r > 0 ? `+${B.partial_r.toFixed(3)}` : B.partial_r.toFixed(3)],
        ['Coeff (wearable)',     `${bSign}${B.b_wearable.toFixed(4)} per unit`],
        ['In-sample R²',         B.r2_insample !== null ? B.r2_insample.toFixed(3) : '—'],
        ['LOO R²',               B.loo_r2 !== null ? B.loo_r2.toFixed(3) : '—'],
        ['LOO MAE',              B.loo_mae !== null ? `±${B.loo_mae.toFixed(2)} ${m.unit}` : '—'],
      ]) + r2Bar(B.loo_r2) + '</div>';
  } else {
    colB = `<div><div class="sub-title">Model B — no wearable predictor</div>
      <p class="no-b">
        Either no wearable covariate met the minimum threshold
        (|partial r| ≥ 0.3, n ≥ 5) or there were insufficient
        overlapping draws for LOO validation.
      </p></div>`;
  }

  // Next draw prediction
  const predVal = hasB ? B.pred_next : A.pred_next;
  const predMae = hasB ? B.loo_mae  : A.loo_mae;
  let pred = '';
  if (predVal !== null && predVal !== undefined) {
    pred = `<div class="pred-box">
      <div class="sub-title">Next draw prediction (based on today's covariates)</div>
      <div class="pred-val" style="color:${refColor(predVal, m.ref)}">${predVal.toFixed(1)} <span>${m.unit}</span></div>
      ${predMae ? `<div class="pred-range">±${predMae.toFixed(2)} ${m.unit} (±1 LOO-MAE)</div>` : ''}
      ${hasB && B.today_predictor !== null ? `
        <div class="pred-note">
          Using: age ${DATA.today.age}, ${B.predictor_label} = ${B.today_predictor}
          (${B.predictor_source}).
          Higher ${B.predictor_label} ${B.wearable_sign_word} ${m.biomarker}
          (coeff ${B.b_wearable > 0 ? '+':''}${B.b_wearable.toFixed(4)}).
        </div>` : `<div class="pred-note">Age-only prediction — no wearable data available today for this model.</div>`}
    </div>`;
  }

  card.innerHTML = `
    <div class="metric-card-header">
      <div>
        <div class="model-bm">${m.biomarker} <span class="model-unit">${m.unit}</span></div>
        <div class="badge-row">${badges}</div>
      </div>
      <div class="latest">
        <div class="metric-card-val" style="color:${refColor(latestActual, m.ref)}">${latestActual}</div>
        <div class="n-lbl">latest · ${latestDate}</div>
      </div>
    </div>
    <div class="model-cols">${colA}${colB}</div>
    ${pred}
    <div class="sub-title">LOO cross-validated: actual vs predicted</div>
    <div class="legend"></div>
    <div class="chart-area"></div>`;
  container.appendChild(card);

  // LOO chart: actual vs each model's held-out prediction
  const series = [
    { name: 'Actual', values: m.actual, color: C_ACTUAL, dots: true },
    { name: 'Model A (age)', values: m.dates.map((d, i) => A.loo_predicted[i] ?? null), color: C_A, dashed: true, width: 1.5, dots: true },
  ];
  if (hasB && B.loo_predicted && B.loo_predicted.length > 0) {
    // Model B only covers the wearable-overlap dates
    const byDate = Object.fromEntries(B.loo_dates.map((d, i) => [d, B.loo_predicted[i]]));
    series.push({ name: 'Model B (age + wearable)', values: m.dates.map(d => byDate[d] ?? null), color: C_B, dashed: true, width: 1.5, dots: true });
  }
  AR.legend(card.querySelector('.legend'), series);
  AR.lineChart(card.querySelector('.chart-area'), {
    x: m.dates, series, height: 180,
    tip: i => `<b>${AR.fmtDate(m.dates[i])}</b><br>` + series.filter(s => s.values[i] != null).map(s =>
      `${sw(s.color)}<span class="dim">${s.name}</span> ${s.values[i].toFixed(2)} ${m.unit}`).join('<br>'),
  });
});

// sidebar anchors: mark the biomarker being read
(function () {
  const links = [...document.querySelectorAll('.nav-item[href^="#"]')];
  const mark = id => links.forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + id));
  const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) mark(e.target.id); }),
                                      { rootMargin: '-25% 0px -65% 0px' });
  document.querySelectorAll('.metric-card.model, #overview').forEach(el => io.observe(el));
})();
</script>
</body>
</html>
"""


def _slug(name: str) -> str:
    """Card anchor for a biomarker; mirrors slug() in the page script."""
    return "m-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def build_html(models, today_covs, ref_ranges):
    today_str   = date.today().isoformat()
    today_age   = today_covs.get("age", date.today().year - BIRTH_YEAR)
    data_json   = json.dumps({"models": models, "today": today_covs,
                               "generated": today_str}, separators=(",", ":")).replace("</", "<\\/")

    n_with_b = sum(1 for m in models if m.get("model_b"))

    nav = (
        '  <div class="nav-section">\n'
        '    <div class="nav-section-title">Models</div>\n'
        '    <a class="nav-item" href="#overview"><span class="icon">📋</span> Overview</a>\n'
        + "".join(
            f'    <a class="nav-item" href="#{_slug(m["biomarker"])}">'
            f'<span class="icon">🧪</span> {m["biomarker"]}</a>\n'
            for m in models
        )
        + "  </div>"
    )

    return (
        shell.head("Andy — Predictive Models", today_str, PAGE_STYLE)
        + PAGE_BODY
        .replace("__SIDEBAR__", shell.sidebar("models.html", nav))
        .replace("__TODAY__", today_str)
        .replace("__N_MODELS__", str(len(models)))
        .replace("__N_WITH_B__", str(n_with_b))
        .replace("__AGE__", str(today_age))
        .replace("__DATA__", data_json)
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Loading bloodwork...")
    measurements, ref_ranges = load_bloodwork(BLOODWORK_FILE)
    all_dates = sorted({pt["date"] for pts in measurements.values() for pt in pts})
    print(f"  {len(measurements)} biomarkers · {len(all_dates)} draw dates")

    print("Loading fitness...")
    fitness = load_fitness(FITNESS_FILE)

    print("Loading WHOOP windows...")
    whoop_windows = load_whoop_windows(WHOOP_DB, all_dates)
    covered = sum(1 for d in all_dates if whoop_windows.get(d, {}).get("whoop_hrv_30d") is not None)
    print(f"  {covered}/{len(all_dates)} draws covered")

    print("Loading Strava windows...")
    strava_windows = load_strava_windows(STRAVA_DB, all_dates)
    scovered = sum(1 for d in all_dates if strava_windows.get(d, {}).get("strava_hrs_90d") is not None)
    print(f"  {scovered}/{len(all_dates)} draws covered")

    print("Loading Garmin windows...")
    garmin_windows = load_garmin_windows(GARMIN_DB, all_dates)
    gcovered = sum(1 for d in all_dates if garmin_windows.get(d, {}).get("garmin_steps_90d") is not None)
    print(f"  {gcovered}/{len(all_dates)} draws covered")

    print("Building models...")
    models, today_covs = build_models(
        measurements, ref_ranges, fitness, whoop_windows, strava_windows, garmin_windows)

    n_b = sum(1 for m in models if m.get("model_b"))
    print(f"  {len(models)} models built · {n_b} with wearable predictor")
    for m in models:
        A = m["model_a"]; B = m.get("model_b")
        loo_b_str = f"  B LOO-R²={B['loo_r2']:.2f}" if B and B.get("loo_r2") is not None else ""
        a_r2_str = f"{A['loo_r2']:.2f}" if A['loo_r2'] is not None else "—"
        print(f"  {m['biomarker']:20s}  n={m['n']:2d}  A LOO-R²={a_r2_str}{loo_b_str}")

    print(f"Writing {HTML_FILE}...")
    html = build_html(models, today_covs, ref_ranges)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(HTML_FILE)
    print(f"  Done! {HTML_FILE} ({size:,} bytes)")


if __name__ == "__main__":
    main()
