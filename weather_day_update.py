#!/usr/bin/env python3
import os
import sqlite3

DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # v_weather_latest: ultima riga ordinando per ts (che nel tuo schema è TEXT)
    row = cur.execute("""
        SELECT
          substr(sunrise, 1, 10) AS date,
          substr(sunrise, 12, 5) AS sunrise_hm,
          substr(sunset,  12, 5) AS sunset_hm,
          CAST((julianday(sunset) - julianday(sunrise)) * 24 * 60 AS INTEGER) AS day_length_min,
          ts,
          provider,
          loc_name
        FROM v_weather_latest
        ORDER BY ts DESC
        LIMIT 1;
    """).fetchone()

    if not row or not row["date"] or not row["sunrise_hm"] or not row["sunset_hm"]:
        print("weather_day_update: NO DATA in v_weather_latest")
        return 2

    cur.execute("""
        INSERT OR REPLACE INTO weather_day(date, sunrise, sunset, day_length_min)
        VALUES (?, ?, ?, ?);
    """, (row["date"], row["sunrise_hm"], row["sunset_hm"], row["day_length_min"]))

    conn.commit()

    print(
        f"weather_day_update: OK date={row['date']} sunrise={row['sunrise_hm']} "
        f"sunset={row['sunset_hm']} len_min={row['day_length_min']} "
        f"ts={row['ts']} loc={row['loc_name']} provider={row['provider']}"
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
