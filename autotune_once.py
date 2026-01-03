#!/usr/bin/env python3
import os, sqlite3
from datetime import datetime, timedelta

DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")
BED     = os.environ.get("BED", "bed2")

FREEZE_C = float(os.environ.get("FREEZE_C", "1.0"))
LOOKAHEAD_H = int(os.environ.get("LOOKAHEAD_H", "24"))

# "siccità" semplice: poche precipitazioni previste nelle prossime 24h
DRY_MAX_RAIN_MM = float(os.environ.get("DRY_MAX_RAIN_MM", "0.5"))
BOOST_MIN = float(os.environ.get("BOOST_SOIL_MIN", "2.0"))
BOOST_MAX = float(os.environ.get("BOOST_SOIL_MAX", "2.0"))

CLAMP_MIN = float(os.environ.get("CLAMP_SOIL_MIN_MAX", "70"))  # max soil_min
CLAMP_MAX = float(os.environ.get("CLAMP_SOIL_MAX_MAX", "85"))  # max soil_max

def today():
    return datetime.now().strftime("%Y-%m-%d")

def clamp(x, a, b):
    return a if x < a else (b if x > b else x)

def main():
    date = today()
    print(f"=== AUTOTUNE bed={BED} date={date} ===", flush=True)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000;")

    bs = con.execute("SELECT soil_min, soil_max FROM bed_state WHERE bed=? LIMIT 1", (BED,)).fetchone()
    if not bs:
        print("NO bed_state", flush=True); return 0

    soil_min = float(bs["soil_min"] or 0.0)
    soil_max = float(bs["soil_max"] or 100.0)

    # meteo prossime LOOKAHEAD_H ore
    start = datetime.now().replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=LOOKAHEAD_H)

    rows = con.execute("""
      SELECT ts, temp_c, precip_mm
      FROM weather_forecast_hourly
      WHERE ts >= ? AND ts < ?
      ORDER BY ts
    """, (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()

    if not rows:
        print("NO forecast rows -> keep policy neutral", flush=True)
        freeze_block = 0
        drought_boost = 0
        note = "no forecast"
        mn_ov = None; mx_ov = None
    else:
        min_temp = min([r["temp_c"] for r in rows if r["temp_c"] is not None] or [999])
        sum_rain = sum([r["precip_mm"] for r in rows if r["precip_mm"] is not None] or [0.0])

        freeze_block = 1 if (min_temp <= FREEZE_C) else 0
        drought_boost = 1 if (sum_rain <= DRY_MAX_RAIN_MM) else 0

        mn_ov = None; mx_ov = None
        note_parts = [f"min_temp={min_temp:.1f}C", f"rain={sum_rain:.1f}mm"]

        if drought_boost and not freeze_block:
            mn_ov = clamp(soil_min + BOOST_MIN, 0, CLAMP_MIN)
            mx_ov = clamp(soil_max + BOOST_MAX, 0, CLAMP_MAX)
            note_parts.append(f"boost +{BOOST_MIN}/+{BOOST_MAX}")

        if freeze_block:
            note_parts.append("freeze_block=1")

        note = " ".join(note_parts)

    con.execute("""
      INSERT INTO irrig_policy (bed, date, freeze_block, drought_boost, soil_min_override, soil_max_override, note)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(bed, date) DO UPDATE SET
        freeze_block=excluded.freeze_block,
        drought_boost=excluded.drought_boost,
        soil_min_override=excluded.soil_min_override,
        soil_max_override=excluded.soil_max_override,
        note=excluded.note,
        updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
    """, (BED, date, int(freeze_block), int(drought_boost), mn_ov, mx_ov, note))
    con.commit()

    print("OK policy:", note, flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
