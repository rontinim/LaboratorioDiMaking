#!/usr/bin/env python3
import os, json, sqlite3, subprocess, math, re
from datetime import datetime, timedelta

DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")
BED     = os.environ.get("BED", "bed2")

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MOSQUITTO_PUB = os.environ.get("MOSQUITTO_PUB", "mosquitto_pub")

# --- Pompa ---
PUMP_FLOW_ML_PER_MIN = float(os.environ.get("PUMP_FLOW_ML_PER_MIN", "1200"))
PUMP_PULSE_SEC       = int(os.environ.get("PUMP_PULSE_SEC", "2"))
MIN_PULSE_GAP_SEC    = int(os.environ.get("MIN_PULSE_GAP_SEC", "60"))

# --- Sicurezze irrigazione ---
MAX_ML_PER_CALL = int(os.environ.get("MAX_IRRIGATE_ML_PER_CALL", "300"))
MAX_ML_PER_HOUR = int(os.environ.get("MAX_IRRIGATE_ML_PER_HOUR", "600"))
MAX_ML_PER_DAY  = int(os.environ.get("MAX_IRRIGATE_ML_PER_DAY", "1500"))

MIN_INTERVAL_MIN = int(os.environ.get("MIN_INTERVAL_BETWEEN_IRRIGATIONS_MIN", "2"))  # ora 2 min

# --- Dose adattiva ---
MAX_PULSES_PER_RUN = int(os.environ.get("MAX_PULSES_PER_RUN", "3"))
DEFICIT_STEP_PCT   = float(os.environ.get("DEFICIT_STEP_PCT", "10"))

# --- Anti-gelo / telemetria ---
FREEZE_C = float(os.environ.get("FREEZE_C", "1.0"))
FREEZE_LOOKAHEAD_H = int(os.environ.get("FREEZE_LOOKAHEAD_H", "12"))
TELEMETRY_MAX_AGE_SEC = int(os.environ.get("TELEMETRY_MAX_AGE_SEC", "180"))

# --- Fasce orarie hard safety ---
IRRIG_START = os.environ.get("IRRIGATION_ALLOWED_START", "06:00")
IRRIG_END   = os.environ.get("IRRIGATION_ALLOWED_END", "22:00")

def now_local():
    return datetime.now()

def iso_now():
    return now_local().strftime("%Y-%m-%dT%H:%M:%S")

def today_ymd():
    return now_local().strftime("%Y-%m-%d")

def now_hhmm():
    return now_local().strftime("%H:%M")

def parse_iso(s: str):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")

def parse_ts_sql(s: str):
    # telemetry ts: "YYYY-MM-DD HH:MM:SS"
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

def clamp(x,a,b):
    return a if x < a else (b if x > b else x)

def hhmm_ok(s):
    return bool(s) and re.match(r"^\d{2}:\d{2}$", s)

def in_window(hm, start, end):
    # assume start<end same day
    return (hm >= start) and (hm <= end)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con

def q1(con, sql, args=()):
    cur = con.execute(sql, args)
    return cur.fetchone()

def policy_window(con):
    r = q1(con, "SELECT irrig_from, irrig_to FROM bed_policy WHERE bed=? AND date=?", (BED, today_ymd()))
    if not r: return None
    f = r["irrig_from"]; t = r["irrig_to"]
    if not (hhmm_ok(f) and hhmm_ok(t)): return None
    if t < f:  # se Claude sbaglia, ignora
        return None
    return (f, t)

def telemetry_last(con):
    return q1(con, "SELECT * FROM telemetry_last WHERE zone=?", (BED,))

def bed_targets(con):
    r = q1(con, "SELECT soil_min, soil_max FROM bed_state WHERE bed=?", (BED,))
    if not r: return (45.0, 65.0)
    return (float(r["soil_min"]), float(r["soil_max"]))

def telem_soil_pct(telem):
    # telem.soil_cap è 0..1 -> %
    if telem is None: return None
    if telem["soil_pct"] is not None:
        return float(telem["soil_pct"])
    if telem["soil_cap"] is not None:
        return float(telem["soil_cap"]) * 100.0
    return None

def cistern_ok(telem):
    if not telem: return None
    if "soil_cistern_ok" in telem.keys() and telem["soil_cistern_ok"] is not None:
        return int(telem["soil_cistern_ok"]) == 1
    return None

def telemetry_fresh(telem):
    if not telem or not telem["ts"]:
        return False
    try:
        ts = parse_ts_sql(telem["ts"])
        return (now_local() - ts).total_seconds() <= TELEMETRY_MAX_AGE_SEC
    except Exception:
        return False

def forecast_freeze_block(con):
    # min temp prossime N ore
    rows = con.execute("""
      SELECT MIN(temp_c) AS min_t
      FROM weather_forecast_hourly
      WHERE ts >= strftime('%Y-%m-%d %H:00:00','now','localtime')
        AND ts <  strftime('%Y-%m-%d %H:00:00','now','localtime', ?)
    """, (f"+{FREEZE_LOOKAHEAD_H} hours",)).fetchone()
    if rows and rows["min_t"] is not None:
        mn = float(rows["min_t"])
        if mn <= FREEZE_C:
            return (True, mn)
    return (False, None)

def ml_per_pulse():
    return int(round(PUMP_FLOW_ML_PER_MIN * (PUMP_PULSE_SEC / 60.0)))

def publish_pump(secs):
    topic = f"serra/cmd/{BED}/relay/pump"
    cmd = {"on": True, "secs": int(secs)}
    subprocess.check_call([MOSQUITTO_PUB, "-h", MQTT_HOST, "-t", topic, "-m", json.dumps(cmd)])
    print(f"[MQTT] PUB {topic} {cmd} (~{ml_per_pulse()} ml)", flush=True)

def ensure_tables(con):
    con.execute("""
      CREATE TABLE IF NOT EXISTS daily_irrig (
        bed          TEXT NOT NULL,
        date         TEXT NOT NULL,
        ml_done      INTEGER NOT NULL DEFAULT 0,
        last_pump_ts TEXT NULL,
        updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
        PRIMARY KEY (bed, date)
      );
    """)
    con.execute("""
      CREATE TABLE IF NOT EXISTS irrig_events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now','localtime')),
        bed      TEXT NOT NULL,
        secs     INTEGER NOT NULL,
        ml       INTEGER NOT NULL,
        soil_pct REAL NULL,
        reason   TEXT NULL
      );
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_irrig_events_bed_ts ON irrig_events(bed, ts);")
    con.commit()

def get_daily_irrig(con):
    r = q1(con, "SELECT * FROM daily_irrig WHERE bed=? AND date=date('now','localtime')", (BED,))
    if not r:
        con.execute("INSERT OR IGNORE INTO daily_irrig(bed,date,ml_done,last_pump_ts) VALUES (?,date('now','localtime'),0,NULL)", (BED,))
        con.commit()
        r = q1(con, "SELECT * FROM daily_irrig WHERE bed=? AND date=date('now','localtime')", (BED,))
    return r

def update_daily_irrig(con, ml_add):
    con.execute("""
      UPDATE daily_irrig
      SET ml_done = ml_done + ?,
          last_pump_ts = strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),
          updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
      WHERE bed=? AND date=date('now','localtime')
    """, (int(ml_add), BED))
    con.commit()

def log_event(con, secs, ml, soil_pct, reason):
    con.execute("INSERT INTO irrig_events(bed,secs,ml,soil_pct,reason) VALUES (?,?,?,?,?)",
                (BED, int(secs), int(ml), float(soil_pct) if soil_pct is not None else None, reason))
    con.commit()

def main():
    print(f"=== IRRIG_ONCE bed={BED} @ {iso_now()} ===", flush=True)
    con = db()
    ensure_tables(con)

    # hard safety: allowed window
    hm = now_hhmm()
    if IRRIG_START and IRRIG_END and (not in_window(hm, IRRIG_START, IRRIG_END)):
        print("BLOCKED: outside allowed irrigation window", flush=True)
        return 0

    # strategy window from Claude (optional)
    pw = policy_window(con)
    if pw:
        pf, pt = pw
        if not in_window(hm, pf, pt):
            print(f"BLOCKED: outside policy window {pf}-{pt}", flush=True)
            return 0

    telem = telemetry_last(con)
    if not telemetry_fresh(telem):
        print("BLOCKED: telemetry stale/missing", flush=True)
        return 0

    cis_ok = cistern_ok(telem)
    if cis_ok is False:
        print("BLOCKED: cistern_ok=0", flush=True)
        return 0

    # freeze check (forecast)
    frz, mn = forecast_freeze_block(con)
    if frz:
        print(f"BLOCKED: forecast freeze min_temp={mn}C <= {FREEZE_C}C", flush=True)
        return 0

    soil_pct = telem_soil_pct(telem)
    if soil_pct is None:
        print("BLOCKED: no soil_pct/soil_cap in telemetry", flush=True)
        return 0

    soil_min, soil_max = bed_targets(con)
    print(f"soil_pct={soil_pct:.2f} target=[{soil_min:.1f},{soil_max:.1f}]", flush=True)

    if soil_pct >= soil_min:
        print("OK: soil above min, no irrigation", flush=True)
        return 0

    daily = get_daily_irrig(con)
    ml_done = int(daily["ml_done"] or 0)
    if ml_done >= MAX_ML_PER_DAY:
        print(f"BLOCKED: day cap reached ml_done={ml_done} >= {MAX_ML_PER_DAY}", flush=True)
        return 0

    # cooldown: pulse gap OR min interval
    now_iso = iso_now()
    last_ts = daily["last_pump_ts"]
    if last_ts:
        try:
            last = parse_iso(last_ts)
            elapsed = (now_local() - last).total_seconds()
            need = max(MIN_PULSE_GAP_SEC, MIN_INTERVAL_MIN * 60)
            if elapsed < need:
                remain = int(need - elapsed)
                print(f"BLOCKED: cooldown {need}s not elapsed (last {last_ts}) remain={remain}s", flush=True)
                return 0
        except Exception:
            pass

    # pulse planning
    deficit = max(0.0, soil_min - soil_pct)
    extra_pulses = int(math.floor(deficit / max(1e-6, DEFICIT_STEP_PCT)))
    pulses = clamp(1 + extra_pulses, 1, MAX_PULSES_PER_RUN)

    ml_pulse = ml_per_pulse()
    ml_total = pulses * ml_pulse
    ml_total = min(ml_total, MAX_ML_PER_CALL)

    # recompute pulses to match caps
    pulses = max(1, min(MAX_PULSES_PER_RUN, int(math.floor(ml_total / ml_pulse)) if ml_pulse > 0 else 1))
    ml_total = pulses * ml_pulse

    print(f"need pulses={pulses} (pulse={PUMP_PULSE_SEC}s ~{ml_pulse}ml) deficit_step={DEFICIT_STEP_PCT}", flush=True)

    # hour cap (semplice: somma ultima ora dagli eventi)
    row = q1(con, """
      SELECT COALESCE(SUM(ml),0) AS ml1h
      FROM irrig_events
      WHERE bed=?
        AND ts >= strftime('%Y-%m-%d %H:%M:%S','now','localtime','-1 hour')
    """, (BED,))
    ml1h = int(row["ml1h"] or 0)
    if ml1h >= MAX_ML_PER_HOUR:
        print(f"BLOCKED: hour cap reached ml1h={ml1h} >= {MAX_ML_PER_HOUR}", flush=True)
        return 0
    if ml1h + ml_total > MAX_ML_PER_HOUR:
        ml_total = max(ml_pulse, MAX_ML_PER_HOUR - ml1h)
        pulses = max(1, min(MAX_PULSES_PER_RUN, int(math.floor(ml_total / ml_pulse))))
        ml_total = pulses * ml_pulse

    # day cap trim
    if ml_done + ml_total > MAX_ML_PER_DAY:
        ml_total = max(ml_pulse, MAX_ML_PER_DAY - ml_done)
        pulses = max(1, min(MAX_PULSES_PER_RUN, int(math.floor(ml_total / ml_pulse))))
        ml_total = pulses * ml_pulse

    # execute ONLY ONE pulse per run (minimo impatto, cron ripete)
    # -> così resti sempre “gentile”: 2s e stop; poi prossimo giro se serve.
    publish_pump(PUMP_PULSE_SEC)
    update_daily_irrig(con, ml_pulse)
    log_event(con, PUMP_PULSE_SEC, ml_pulse, soil_pct, "auto_pulse_policy_window")

    print("DONE", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
