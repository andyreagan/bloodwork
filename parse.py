#!/usr/bin/env python3
"""
Parse Andy's bloodwork.org file into SQLite database.
"""
import re
import sqlite3
import os

ORG_FILE = os.path.join(os.path.dirname(__file__), "bloodwork.org")
DB_FILE = os.path.join(os.path.dirname(__file__), "bloodwork.db")

# Biomarker name normalization map (raw name → canonical)
NORMALIZE = {
    # Hemoglobin variants
    "hgb": "Hemoglobin",
    "hemoglobin": "Hemoglobin",
    # Triglycerides
    "triglyceride": "Triglycerides",
    "triglycerides": "Triglycerides",
    # HbA1c
    "hba1c": "HbA1c",
    # Cholesterol
    "cholesterol, total": "Cholesterol",
    "cholesterol": "Cholesterol",
    # RBC
    "red blood cells": "RBC",
    "rbc": "RBC",
    # WBC
    "wbc": "WBC",
    # Hematocrit
    "hct": "Hematocrit",
    "hematocrit": "Hematocrit",
    # Platelets
    "plt": "Platelets",
    "platelet count": "Platelets",
    "platelets": "Platelets",
    # MCV
    "mcv": "MCV",
    "mean cell volume": "MCV",
    # MCH
    "mch": "MCH",
    "mean cell hemoglobin": "MCH",
    # MCHC
    "mchc": "MCHC",
    "mean cell hgb conc": "MCHC",
    # RDW
    "rdw": "RDW",
    "red cell dist width": "RDW",
    # LDL
    "ldl": "LDL",
    # HDL
    "hdl": "HDL",
    # AST
    "ast": "AST",
    # ALT
    "alt": "ALT",
    # GGT
    "ggt": "GGT",
    # Glucose
    "glucose": "Glucose",
    # hsCRP
    "hscrp": "hsCRP",
    "hsCRP": "hsCRP",
    # Testosterone
    "testosterone": "Testosterone",
    # Free testosterone
    "free testosterone": "Free Testosterone",
    "testosterone, free, calculated": "Free Testosterone",
    # Vitamin D
    "vitamin d": "Vitamin D",
    "vitamin d, 25-hydroxy": "Vitamin D",
    # Ferritin
    "ferritin": "Ferritin",
    # Vitamin B12
    "vitamin b12": "Vitamin B12",
    # Folate
    "folate": "Folate",
    # Albumin
    "albumin": "Albumin",
    # Calcium
    "calcium": "Calcium",
    # Creatinine
    "creatinine": "Creatinine",
    # BUN
    "bun": "BUN",
    # Potassium
    "potassium": "Potassium",
    # Sodium
    "sodium": "Sodium",
    # Chloride
    "chloride": "Chloride",
    # CO2
    "co2": "CO2",
    "carbon dioxide, total": "CO2",
    # SHBG
    "shbg": "SHBG",
    "sex hormone binding globulin": "SHBG",
    # Cortisol
    "cortisol": "Cortisol",
    # Iron
    "iron": "Iron",
    # TIBC
    "tibc": "TIBC",
    # Magnesium
    "magnesium": "Magnesium",
    # Alkaline Phosphatase
    "alkaline phosphatase": "Alkaline Phosphatase",
    # Bilirubin
    "bilirubin, total": "Total Bilirubin",
    "total bilirubin": "Total Bilirubin",
    # Total Protein
    "total protein": "Total Protein",
    "protein, total": "Total Protein",
    # eGFR
    "egfr": "eGFR",
    "glomerular filt rate": "eGFR",
    # Creatine Kinase
    "creatine kinase": "Creatine Kinase",
    # Neutrophils (percentage only - skip absolute to avoid duplicates)
    "neutrophils": "Neutrophils (%)",
    "neutrophil percentage": "Neutrophils (%)",
    # Lymphocytes
    "lymphocytes": "Lymphocytes (%)",
    "lymphocyte percentage": "Lymphocytes (%)",
    # Monocytes
    "monocytes": "Monocytes (%)",
    "monocyte percentage": "Monocytes (%)",
    # Eosinophils
    "eosinophils": "Eosinophils (%)",
    "eosinophil percentage": "Eosinophils (%)",
    # Basophils
    "basophils": "Basophils (%)",
    "basophil percentage": "Basophils (%)",
    # Hormones
    "estradiol": "Estradiol",
    "dhea-sulfate": "DHEA-S",
    "luteinizing hormone (lh)": "LH",
    "fsh": "FSH",
    "prostate specific antigen": "PSA",
    "tsh": "TSH",
    "insulin-like growth factor i": "IGF-1",
    # Others
    "homocyst(e)ine": "Homocysteine",
    "apolipoprotein b": "Apolipoprotein B",
    "lipoprotein (a)": "Lipoprotein(a)",
    "vldl": "VLDL",
    "transferrin saturation": "Transferrin Saturation",
    "transferrin saturation (ts)": "Transferrin Saturation",
    "globulin": "Globulin",
    "globulin, total": "Globulin",
    "anion gap": "Anion Gap",
    "mpv": "MPV",
    "cardiac risk ratio": "Cardiac Risk Ratio",
    "cardiac risk ratio:": "Cardiac Risk Ratio",
    "rbc magnesium": "RBC Magnesium",
    "a/g ratio": "A/G Ratio",
    "osmolality, cal": "Osmolality",
    "bun/cre ratio": "BUN/Creatinine Ratio",
    "testosterone:cortisol ratio": "Testosterone:Cortisol Ratio",
}

# Markers that appear as absolute counts (K/uL) and as percentages - keep only the percentage form
# We'll skip absolute differential counts with these names when unit is K/uL or cells/µL
SKIP_ABSOLUTE_DIFF = {"neutrophils", "lymphocytes", "monocytes", "eosinophils", "basophils", "immature granulocytes"}

# Reference ranges (normal_low, normal_high, optimal_low, optimal_high, unit)
# None means no lower/upper bound
REFERENCE_RANGES = {
    "Glucose":         (70, 99, 72, 85, "mg/dL"),
    "HbA1c":           (None, 5.7, None, 5.2, "%"),
    "Cholesterol":     (None, 200, None, 150, "mg/dL"),
    "HDL":             (40, None, 60, None, "mg/dL"),
    "LDL":             (None, 100, None, 70, "mg/dL"),
    "Triglycerides":   (None, 150, None, 80, "mg/dL"),
    "hsCRP":           (None, 3.0, None, 1.0, "mg/L"),
    "Testosterone":    (300, 1000, 500, 900, "ng/dL"),
    "Vitamin D":       (20, 50, 40, 60, "ng/mL"),
    "Ferritin":        (12, 300, 50, 150, "ng/mL"),
    "Hemoglobin":      (13.5, 17.5, 14.5, 16.5, "g/dL"),
    "AST":             (10, 40, 10, 30, "U/L"),
    "ALT":             (7, 56, 7, 30, "U/L"),
    "Cortisol":        (6, 23, 10, 18, "µg/dL"),
    "Vitamin B12":     (200, 900, 400, 800, "pg/mL"),
    "Magnesium":       (1.7, 2.2, 1.9, 2.1, "mg/dL"),
    "TSH":             (0.4, 4.0, 0.5, 2.5, "uIU/mL"),
    "Homocysteine":    (None, 15, None, 9, "umol/L"),
    "SHBG":            (10, 57, 20, 45, "nmol/L"),
    "Free Testosterone": (5, 21, 9, 18, "ng/dL"),
    "Creatinine":      (0.7, 1.2, 0.8, 1.1, "mg/dL"),
    "eGFR":            (60, None, 90, None, "mL/min/1.73m2"),
    "WBC":             (4.0, 11.0, 4.5, 8.0, "K/uL"),
    "RBC":             (4.5, 5.9, 4.7, 5.5, "M/uL"),
    "Hematocrit":      (40, 52, 42, 48, "%"),
    "Platelets":       (150, 400, 180, 350, "K/uL"),
}


def normalize_name(raw: str) -> str | None:
    """Return canonical biomarker name, or None to skip."""
    key = raw.strip().lower()
    return NORMALIZE.get(key, raw.strip())


def should_skip(raw_name: str, unit: str) -> bool:
    """Skip absolute differential cell counts (we keep only percentages)."""
    key = raw_name.strip().lower()
    if key in SKIP_ABSOLUTE_DIFF:
        # If unit is a percentage, keep it; otherwise skip
        if unit and "%" not in unit:
            return True
    # Skip rows with no numeric value possible
    return False


def parse_value(val_str: str):
    """Extract float from value string. Returns None if not parseable."""
    val_str = val_str.strip()
    # Handle "<X.X" format (e.g. "<9.0")
    val_str = re.sub(r'^[<>]\s*', '', val_str)
    try:
        return float(val_str)
    except ValueError:
        return None


def parse_org(filepath: str) -> list[dict]:
    """Parse bloodwork.org and return list of measurement dicts."""
    records = []
    current_date = None

    date_pattern = re.compile(r'^\*+\s+<(\d{4}-\d{2}-\d{2})>')
    # Matches: - BiomarkerName: value (unit)  OR  - BiomarkerName: value
    item_pattern = re.compile(
        r'^\s*-\s+([^:]+):\s+([\d<>.,\-]+)\s*(?:\(([^)]+)\))?'
    )

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            # Check for date header
            m = date_pattern.match(line)
            if m:
                current_date = m.group(1)
                continue

            # Check for measurement line
            m = item_pattern.match(line)
            if m and current_date:
                raw_name = m.group(1).strip()
                val_str = m.group(2).strip()
                unit = m.group(3).strip() if m.group(3) else ""

                # Skip non-numeric / non-health items
                skip_keywords = {"height", "weight", "bmi", "cocaine", "nicotine",
                                  "urinary", "urine", "blood pressure", "history of",
                                  "nrbc", "absolute nrbc"}
                if any(kw in raw_name.lower() for kw in skip_keywords):
                    continue

                # Skip absolute differential counts
                if should_skip(raw_name, unit):
                    continue

                value = parse_value(val_str)
                if value is None:
                    continue

                canonical = normalize_name(raw_name)
                if canonical is None:
                    continue

                records.append({
                    "date": current_date,
                    "biomarker": canonical,
                    "value": value,
                    "unit": unit,
                })

    return records


def create_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        DROP TABLE IF EXISTS measurements;
        DROP TABLE IF EXISTS reference_ranges;

        CREATE TABLE measurements (
            id INTEGER PRIMARY KEY,
            date TEXT,
            biomarker TEXT,
            value REAL,
            unit TEXT
        );

        CREATE TABLE reference_ranges (
            biomarker TEXT PRIMARY KEY,
            low REAL,
            high REAL,
            optimal_low REAL,
            optimal_high REAL,
            unit TEXT
        );
    """)
    conn.commit()
    return conn


def insert_records(conn: sqlite3.Connection, records: list[dict]):
    conn.executemany(
        "INSERT INTO measurements (date, biomarker, value, unit) VALUES (:date, :biomarker, :value, :unit)",
        records
    )
    conn.commit()


def insert_reference_ranges(conn: sqlite3.Connection):
    rows = [
        (biomarker, low, high, opt_low, opt_high, unit)
        for biomarker, (low, high, opt_low, opt_high, unit) in REFERENCE_RANGES.items()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO reference_ranges (biomarker, low, high, optimal_low, optimal_high, unit) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows
    )
    conn.commit()


def main():
    print(f"Parsing {ORG_FILE}...")
    records = parse_org(ORG_FILE)
    print(f"  → {len(records)} measurements parsed")

    print(f"Creating database {DB_FILE}...")
    conn = create_db(DB_FILE)
    insert_records(conn, records)
    insert_reference_ranges(conn)

    # Summary
    cur = conn.execute(
        "SELECT biomarker, COUNT(*) as n, MIN(date) as first, MAX(date) as last "
        "FROM measurements GROUP BY biomarker ORDER BY biomarker"
    )
    rows = cur.fetchall()
    print(f"\n{'Biomarker':<40} {'Count':>5}  {'First':>10}  {'Last':>10}")
    print("-" * 72)
    for row in rows:
        print(f"{row[0]:<40} {row[1]:>5}  {row[2]:>10}  {row[3]:>10}")

    conn.close()
    print(f"\nDone. {len(rows)} distinct biomarkers in database.")


if __name__ == "__main__":
    main()
