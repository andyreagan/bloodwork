# Health Dashboard

Personal bloodwork, fitness, and recovery tracker.
Live at **[health.andyreagan.com](https://health.andyreagan.com)**.

## Pages

| Page | URL | What it shows |
|------|-----|---------------|
| **Dashboard** | `/` | Latest values for 89 biomarkers + VO₂max top card, trend arrows, panel charts |
| **Fitness Sources** | `/fitness.html` | Garmin vs WHOOP head-to-head comparison, combined monthly trends, source coverage |
| **Correlations** | `/correlations.html` | Raw + age-adjusted partial r between all biomarkers and 29 covariates (WHOOP/Strava/Garmin/Life) |
| **Models** | `/models.html` | LOO cross-validated OLS models: age-only + age + best wearable predictor, next-draw predictions |

## Data sources

| Source | Signal | Coverage |
|--------|--------|----------|
| `bloodwork_data.yaml` | 89 biomarkers across 17 blood draws | 2013–2026 |
| `fitness_data.yaml` | VO₂max (lab), FTP, weight, race times, lifts | 2011–2026 |
| `~/projects/2026/strava-database/strava.db` | Weekly training: run miles, ride/run/swim hours, avg HR, watts | 2011–2026 |
| `~/projects/2026/whoop-database/whoop.db` | Daily: HRV, recovery, RHR, strain, sleep, SpO₂, skin temp | Oct 2020–today |
| `~/projects/2026/connect-database/garmin-database/garmin.db` | Daily: steps, stress, body battery, intensity mins, sleep, RHR | 2015–today |

## Regenerating

```bash
cd ~/projects/2026/bloodwork
make          # rebuild everything
make update   # alias for make (rebuild all + push)
```

Or individually:

```bash
python generate.py              # → index.html
python generate_fitness.py      # → fitness.html
python generate_correlations.py # → correlations.html
python generate_models.py       # → models.html
```

## Adding a new blood draw

Edit `bloodwork_data.yaml` directly (or ask an LLM):

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

Then:

```bash
make update
git add -A && git commit -m "Add June 2026 bloodwork" && git push
```

## Upcoming tests

Planned draws and their hard deadlines. WHOOP and Function credits are use-by-a-date and **don't roll over**. Baseline for this cycle: Function Annual (100+) drawn 2026-04-15.

| Target date | Test | Provider | Constraint |
|-------------|------|----------|------------|
| 2026-08-26 | Annual physical | Primary care | Fixed (annual physical) |
| Mid–late Nov 2026 | WHOOP Advanced Labs (2nd of 2) | WHOOP → Quest | Test expires ~2026-12-04 (12 mo from purchase). Turn off Labs auto-renew *after* this draw. |
| Mid–late Jan 2027 | Function Mid-Year (60+) | Function → Quest | Use before annual renewal (~2027-04-15); 3–6 mo is only a recommendation |
| ~2027-04-15 | Function Annual (100+), renewal | Function → Quest | Membership renews; fresh 100+ panel |

## Files

| File | Purpose |
|------|---------|
| `bloodwork_data.yaml` | All bloodwork (draws + reference ranges) |
| `fitness_data.yaml` | VO₂max, FTP, weight, race times, lifts |
| `generate.py` | Main dashboard → `index.html` |
| `generate_fitness.py` | Fitness source comparison → `fitness.html` |
| `generate_correlations.py` | Correlation analysis → `correlations.html` |
| `generate_models.py` | Predictive models → `models.html` |
| `Makefile` | `make` rebuilds all four pages |

## Statistical approach

**Correlations page:**
- Raw Pearson *r* for every biomarker × covariate pair
- Age-adjusted partial *r*: regress both variables on age first, then correlate residuals — removes the shared aging trend before measuring the relationship
- Minimum n≥5 for wearable covariates to avoid spurious r=1 on tiny samples

**Models page:**
- Model A: age-only OLS (baseline)
- Model B: age + best wearable predictor, chosen by |partial r| ≥ 0.3
- Hard limit: max 2 predictors (n=5–15 doesn't support more)
- Leave-One-Out cross-validation throughout — LOO-R² is the headline metric
- Prediction intervals are ±1 LOO-MAE (honest, not formal confidence intervals)

## Privacy

This repo is public. The HTML embeds all data as JSON —
**do not commit sensitive non-health data here**.
Health data (bloodwork, fitness) is intentionally shared.
No full date of birth — only birth year (1989) for age calculation.
