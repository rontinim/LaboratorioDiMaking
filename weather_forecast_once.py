#!/usr/bin/env python3
import os, json, sqlite3, urllib.request
from datetime import datetime

DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")
LAT = float(os.environ.get("GPS_LAT", "44.4949"))
LON = float(os.environ.get("GPS_LON", "11.3426"))
HOURS = int(os.environ.get("FORECAST_HOURS", "48"))

def now_local():
    return datetime.now()

def fetch():
    # Open-Meteo hourly: temperature_2m + precipitation
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&hourly=temperature_2m,precipitation"
        "&timezone=Europe%2FRome"
    )
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    print(f"=== WEATHER_FORECAST_ONCE @ {now_local().strftime('%Y-%m-%dT%H:%M:%S')} ===", flush=True)
    data = fetch()
    h = data.get("hourly") or {}
    times = h.get("time") or []
    temps = h.get("temperature_2m") or []
    precs = h.get("precipitation") or []

    n = min(len(times), len(temps), len(precs), HOURS)
    if n == 0:
        print("NO hourly data", flush=True)
        return 0

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=5000;")
    cur = con.cursor()
    cur.execute("BEGIN;")
    for i in range(n):
        # Open-Meteo returns "YYYY-MM-DDTHH:00"
        ts = times[i].replace("T", " ") + ":00"
        cur.execute("""
          INSERT INTO weather_forecast_hourly (ts, temp_c, precip_mm)
          VALUES (?, ?, ?)
          ON CONFLICT(ts) DO UPDATE SET temp_c=excluded.temp_c, precip_mm=excluded.precip_mm
        """, (ts, float(temps[i]) if temps[i] is not None else None,
                   float(precs[i]) if precs[i] is not None else None))
    con.commit()
    print(f"OK saved {n} hours", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
