#!/usr/bin/env python3
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")

def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def norm_uid(u: str) -> str:
    # rimuove ":" e mette maiuscolo
    return (u or "").strip().replace(":", "").upper()

def main() -> int:
    import sys
    if len(sys.argv) < 2:
        print("Uso: apply_nfc_tag.py <UID>")
        return 2

    uid_in = sys.argv[1].strip()
    uid_norm = norm_uid(uid_in)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    tag = cur.execute("""
        SELECT uid, crop_name, bed, mode
        FROM nfc_tag
        WHERE REPLACE(UPPER(uid),':','') = ?
        LIMIT 1
    """, (uid_norm,)).fetchone()

    if not tag:
        print(f"apply_nfc_tag: UID sconosciuto: {uid_in}")
        return 3

    crop = cur.execute("""
        SELECT name, soil_min, soil_max, light_hours_indoor, light_hours_outdoor
        FROM crop_profile
        WHERE name = ?
        LIMIT 1
    """, (tag["crop_name"],)).fetchone()

    if not crop:
        print(f"apply_nfc_tag: crop_profile mancante per {tag['crop_name']}")
        return 4

    if tag["mode"] == "indoor":
        light_target = float(crop["light_hours_indoor"])
    else:
        light_target = float(crop["light_hours_outdoor"])

    cur.execute("""
        INSERT INTO bed_state(bed, crop_name, mode, soil_min, soil_max, light_hours_target, motor_position, last_update)
        VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT motor_position FROM bed_state WHERE bed=?), 'unknown'), ?)
        ON CONFLICT(bed) DO UPDATE SET
          crop_name = excluded.crop_name,
          mode = excluded.mode,
          soil_min = excluded.soil_min,
          soil_max = excluded.soil_max,
          light_hours_target = excluded.light_hours_target,
          last_update = excluded.last_update;
    """, (
        tag["bed"],
        crop["name"],
        tag["mode"],
        float(crop["soil_min"]),
        float(crop["soil_max"]),
        float(light_target),
        tag["bed"],
        iso_now(),
    ))

    conn.commit()
    conn.close()

    print(f"apply_nfc_tag: OK bed={tag['bed']} crop={crop['name']} mode={tag['mode']} light_target={light_target}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
