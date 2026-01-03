#!/usr/bin/env python3
import os
import sys
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta

# ===================== CONFIG =====================

DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")
BED = os.environ.get("BED", "bed2")

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MOSQUITTO_PUB = os.environ.get("MOSQUITTO_PUB", "mosquitto_pub")

MOTOR_REVS = float(os.environ.get("MOTOR_REVS", "2"))
EVENT_WINDOW_MIN = int(os.environ.get("EVENT_WINDOW_MIN", "3"))

LED_PRE_SUNSET_MIN = int(os.environ.get("LED_PRE_SUNSET_MIN", "10"))
LED_INDOOR_START_HHMM = os.environ.get("LED_INDOOR_START_HHMM", "08:00")

# chunk LED (fail-safe: niente 10h in un colpo)
MAX_LED_CHUNK_SEC = int(os.environ.get("MAX_LED_CHUNK_SEC", "1800"))  # 30 min

# ===================== TIME =====================

def now_local() -> datetime:
    return datetime.now()

def iso_now() -> str:
    return now_local().strftime("%Y-%m-%dT%H:%M:%S")

def today_ymd() -> str:
    return now_local().strftime("%Y-%m-%d")

def parse_hhmm(ymd: str, hhmm: str) -> datetime:
    return datetime.strptime(f"{ymd} {hhmm}", "%Y-%m-%d %H:%M")

# ===================== DB =====================

def open_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con

def get_weather_day(con):
    row = con.execute(
        "SELECT date,sunrise,sunset,day_length_min "
        "FROM weather_day ORDER BY date DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None

def get_bed_state(con, bed):
    row = con.execute(
        "SELECT bed,crop_name,mode,soil_min,soil_max,light_hours_target,motor_position,last_update "
        "FROM bed_state WHERE bed=?",
        (bed,)
    ).fetchone()
    return dict(row) if row else None

def get_crop(con, name):
    if not name:
        return None
    row = con.execute("SELECT * FROM crops WHERE name=? LIMIT 1", (name,)).fetchone()
    return dict(row) if row else None

def get_telemetry_last(con, zone):
    row = con.execute("SELECT * FROM telemetry_last WHERE zone=?", (zone,)).fetchone()
    return dict(row) if row else None

def get_daily_light(con, bed, date):
    row = con.execute(
        "SELECT bed,date,seconds_done,last_ts,last_led_on,updated_at "
        "FROM daily_light WHERE bed=? AND date=?",
        (bed, date)
    ).fetchone()
    return dict(row) if row else None

def ensure_daily_light_row(con, bed, date):
    con.execute("""
      INSERT INTO daily_light (bed, date, seconds_done, last_ts, last_led_on, updated_at)
      VALUES (?, ?, 0, NULL, 0, strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
      ON CONFLICT(bed, date) DO NOTHING
    """, (bed, date))

def sync_daily_light_led(con, bed: str, date: str, is_on: int):
    con.execute("""
        UPDATE daily_light
        SET last_led_on = ?,
            last_ts = strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),
            updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
        WHERE bed = ? AND date = ?
    """, (int(is_on), bed, date))

# ===================== MQTT =====================

def mqtt_pub(topic: str, payload: dict):
    msg = json.dumps(payload, separators=(",", ":"))
    cmd = [MOSQUITTO_PUB, "-h", MQTT_HOST, "-t", topic, "-m", msg]
    subprocess.check_call(cmd)
    print(f"[MQTT] PUB {topic} {msg}", flush=True)

def motor_cmd(bed: str, direction: str, revs: float, reason: str):
    topic = f"serra/cmd/{bed}/motor"
    cmd = {"revs": float(revs), "dir": direction}
    mqtt_pub(topic, cmd)
    return {"topic": topic, "cmd": cmd, "reason": reason}

def led_cmd(bed: str, on: bool, secs: int, reason: str):
    topic = f"serra/cmd/{bed}/relay/led"
    cmd = {"on": bool(on), "secs": int(secs)}
    mqtt_pub(topic, cmd)
    return {"topic": topic, "cmd": cmd, "reason": reason}

def publish_setpoint(bed: str, soil_min: float, soil_max: float, lights_on: bool, hours: float, notes: str):
    topic = f"serra/setpoints/{bed}"
    sp = {
        "zone": bed,
        "irrigate_ml": 0,
        "soil_target": [float(soil_min), float(soil_max)],
        "lights": {"on": bool(lights_on), "hours": float(hours)},
        "notes": notes
    }
    mqtt_pub(topic, sp)
    return sp

# ===================== PLANNER =====================

def main():
    print(f"=== PLAN_ONCE_V2 bed={BED} @ {iso_now()} ===", flush=True)

    with open_db() as con:
        weather = get_weather_day(con)
        bed_state = get_bed_state(con, BED)
        if not bed_state:
            print("ERR: missing bed_state", file=sys.stderr, flush=True)
            return 2

        crop = get_crop(con, bed_state.get("crop_name"))
        telem = get_telemetry_last(con, BED)

        print("weather_day:", weather, flush=True)
        print("bed_state  :", bed_state, flush=True)
        print("crop       :", crop, flush=True)
        print("telemetry  :", telem, flush=True)

        # meteo deve essere di oggi per usare sunset/sunrise
        if not weather or weather.get("date") != today_ymd():
            print("SKIP: weather not for today", flush=True)
            return 0

        ymd = today_ymd()
        sunset_hhmm = weather["sunset"]

        mode = (bed_state.get("mode") or "").strip().lower()
        motor_pos = (bed_state.get("motor_position") or "").strip().lower()

        # setpoint sempre: in OUTDOOR forziamo lights_on=false (coerente)
        soil_min = float(bed_state.get("soil_min") or 40.0)
        soil_max = float(bed_state.get("soil_max") or 60.0)
        light_hours_target = float(bed_state.get("light_hours_target") or 0.0)

        publish_setpoint(
            BED, soil_min, soil_max,
            lights_on=(mode == "indoor" and light_hours_target > 0),
            hours=(light_hours_target if mode == "indoor" else 0.0),
            notes="Planner v2: setpoint published (light budget enforced via daily_light)."
        )

        # 1) enforce motore
        motor_action = None
        if mode == "outdoor" and motor_pos != "up":
            motor_action = motor_cmd(BED, "up", MOTOR_REVS, "mode_enforce:outdoor")
        elif mode == "indoor" and motor_pos != "down":
            motor_action = motor_cmd(BED, "down", MOTOR_REVS, "mode_enforce:indoor")

        led_action = None

        # stato reale LED (preferisci telemetria, fallback daily_light)
        ensure_daily_light_row(con, BED, ymd)
        dl = get_daily_light(con, BED, ymd) or {}
        db_last_led_on = int(dl.get("last_led_on") or 0)
        telem_led_on = int(telem.get("relay_led") or 0) if telem else None
        physical_led_on = telem_led_on if telem_led_on is not None else db_last_led_on

        # 2A) OUTDOOR: forza spegnimento se accesi
        if mode == "outdoor":
            if physical_led_on == 1:
                led_action = led_cmd(BED, False, 1, "mode_outdoor")
                sync_daily_light_led(con, BED, ymd, 0)
                con.commit()

            print("MOTOR_CMD:", motor_action, flush=True)
            print("LED_CMD  :", led_action, flush=True)
            return 0

        # 2B) INDOOR: budget + finestra
        if mode == "indoor" and light_hours_target > 0:
            dl = get_daily_light(con, BED, ymd) or {}
            seconds_done = int(dl.get("seconds_done") or 0)
            last_led_on = int(dl.get("last_led_on") or 0)

            target_sec = int(round(light_hours_target * 3600))
            remaining = max(0, target_sec - seconds_done)

            print(f"LIGHT_DONE_S: {seconds_done} ({seconds_done/3600:.2f}h)", flush=True)
            print(f"LIGHT_TARGET_S: {target_sec} ({light_hours_target:.2f}h) REMAINING_S: {remaining}", flush=True)

            start_dt = parse_hhmm(ymd, LED_INDOOR_START_HHMM)
            end_dt = parse_hhmm(ymd, sunset_hhmm) - timedelta(minutes=LED_PRE_SUNSET_MIN)
            now = now_local()
            in_window = (start_dt <= now <= end_dt)

            if not in_window:
                if physical_led_on == 1 or last_led_on == 1:
                    led_action = led_cmd(BED, False, 1, "outside_window")
                    sync_daily_light_led(con, BED, ymd, 0)
                    con.commit()
            else:
                if remaining <= 0:
                    if physical_led_on == 1 or last_led_on == 1:
                        led_action = led_cmd(BED, False, 1, "budget_done")
                        sync_daily_light_led(con, BED, ymd, 0)
                        con.commit()
                else:
                    # accendi solo se risulta spento (db/telem)
                    if physical_led_on == 0 and last_led_on == 0:
                        secs = min(remaining, MAX_LED_CHUNK_SEC)
                        led_action = led_cmd(BED, True, secs, "indoor_budget_chunk")
                        sync_daily_light_led(con, BED, ymd, 1)
                        con.commit()

        print("MOTOR_CMD:", motor_action, flush=True)
        print("LED_CMD  :", led_action, flush=True)
        return 0

if __name__ == "__main__":
    raise SystemExit(main())

