#!/usr/bin/env python3
import os, json, sqlite3, subprocess, re
from datetime import datetime, timedelta

DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")
BED     = os.environ.get("BED", "bed2")

# If set, only this BED instance will run global indoor/outdoor actions.
# Useful if you have multiple cron jobs (one per bed).
MODE_MASTER_BED = os.environ.get("MODE_MASTER_BED", "").strip()

# Claude / Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-3-5-haiku-latest")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "400"))

# Policy update interval (minutes)
POLICY_MIN_INTERVAL_MIN = int(os.environ.get("POLICY_MIN_INTERVAL_MIN", "5"))

# LED safety
LIGHTS_MAX_HOURS_PER_DAY = float(os.environ.get("LIGHTS_MAX_HOURS_PER_DAY", "16"))
LIGHTS_NIGHT_ONLY        = os.environ.get("LIGHTS_NIGHT_ONLY", "true").lower() == "true"

# MQTT
MQTT_HOST     = os.environ.get("MQTT_HOST", "127.0.0.1")
MOSQUITTO_PUB = os.environ.get("MOSQUITTO_PUB", "mosquitto_pub")

# Per-bed topics
def led_topic(bed: str) -> str:
    return f"serra/cmd/{bed}/relay/led"

def motor_topic(bed: str) -> str:
    return f"serra/cmd/{bed}/motor"

# Chunk LED (default 1800)
LED_CHUNK_SEC = int(os.environ.get("LED_CHUNK_SEC", "1800"))

# Motor defaults (requested)
MOTOR_REVS_UP   = float(os.environ.get("MOTOR_REVS_UP", "2"))
MOTOR_REVS_DOWN = float(os.environ.get("MOTOR_REVS_DOWN", "2"))

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con

def now_local():
    return datetime.now()

def today_ymd():
    return now_local().strftime("%Y-%m-%d")

def parse_hhmm(s: str):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", s):
        return None
    h, m = s.split(":")
    h = int(h); m = int(m)
    if h < 0 or h > 23 or m < 0 or m > 59:
        return None
    return f"{h:02d}:{m:02d}"

def hhmm_to_min(s: str):
    s = parse_hhmm(s)
    if not s:
        return None
    h, m = s.split(":")
    return int(h) * 60 + int(m)

def clamp(x, a, b):
    return a if x < a else (b if x > b else x)

def get_row(con, sql, args=()):
    cur = con.execute(sql, args)
    return cur.fetchone()

def mqtt_pub(topic: str, payload: dict):
    subprocess.call([MOSQUITTO_PUB, "-h", MQTT_HOST, "-t", topic, "-m", json.dumps(payload)])

# ---------------------------
# GLOBAL MODE STATE MACHINE
# ---------------------------

def should_run_mode_actions() -> bool:
    if MODE_MASTER_BED:
        return BED == MODE_MASTER_BED
    return True

def ensure_bed_runtime(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS bed_runtime (
      bed TEXT NOT NULL,
      date TEXT NOT NULL,
      last_effective_mode TEXT,
      motor_raised INTEGER DEFAULT 0,
      motor_lowered INTEGER DEFAULT 0,
      updated_at TEXT,
      PRIMARY KEY (bed, date)
    )
    """)
    con.commit()

def get_runtime_today(con, bed: str):
    return get_row(con, "SELECT * FROM bed_runtime WHERE bed=? AND date=?", (bed, today_ymd()))

def upsert_runtime(con, bed: str, *, last_effective_mode=None, motor_raised=None, motor_lowered=None):
    rt = get_runtime_today(con, bed)
    ts = now_local().strftime("%Y-%m-%dT%H:%M:%S")

    if rt is None:
        con.execute("""
          INSERT INTO bed_runtime(bed,date,last_effective_mode,motor_raised,motor_lowered,updated_at)
          VALUES (?,?,?,?,?,?)
        """, (
            bed, today_ymd(),
            last_effective_mode,
            int(motor_raised) if motor_raised is not None else 0,
            int(motor_lowered) if motor_lowered is not None else 0,
            ts
        ))
    else:
        new_mode    = rt["last_effective_mode"] if last_effective_mode is None else last_effective_mode
        new_raised  = rt["motor_raised"]        if motor_raised is None else int(motor_raised)
        new_lowered = rt["motor_lowered"]       if motor_lowered is None else int(motor_lowered)

        con.execute("""
          UPDATE bed_runtime
          SET last_effective_mode=?, motor_raised=?, motor_lowered=?, updated_at=?
          WHERE bed=? AND date=?
        """, (new_mode, new_raised, new_lowered, ts, bed, today_ymd()))

    con.commit()

def list_beds(con):
    rows = con.execute("SELECT bed FROM bed_state ORDER BY bed").fetchall()
    beds = [r["bed"] for r in rows] if rows else []
    return beds if beds else [BED]

def is_night_now(con) -> bool:
    now = now_local()
    now_min = now.hour * 60 + now.minute

    wd = con.execute(
        "SELECT sunrise, sunset FROM weather_day WHERE date=date('now','localtime')"
    ).fetchone()

    if wd is not None and "sunrise" in wd.keys() and "sunset" in wd.keys():
        sr = hhmm_to_min(wd["sunrise"])
        ss = hhmm_to_min(wd["sunset"])
        if sr is not None and ss is not None:
            return not (sr <= now_min <= ss)

    return not (hhmm_to_min("08:00") <= now_min <= hhmm_to_min("16:30"))

def get_raw_mode(con) -> str:
    bs = con.execute("SELECT mode FROM bed_state WHERE bed=?", (BED,)).fetchone()
    if bs and bs["mode"]:
        v = str(bs["mode"]).strip().lower()
        if v in ("indoor", "outdoor"):
            return v
    return "outdoor"

def compute_effective_mode(con, raw_mode: str) -> str:
    raw_mode = (raw_mode or "outdoor").strip().lower()
    if raw_mode == "indoor":
        return "indoor"
    # outdoor after sunset behaves as indoor
    return "indoor" if is_night_now(con) else "outdoor"

def pub_led_for_bed(bed: str, on: bool, secs: int):
    # Never infinite. For mode ON we always use 1800.
    if on:
        secs = LED_CHUNK_SEC
    else:
        secs = 0
    cmd = {"on": bool(on), "secs": int(secs)}
    mqtt_pub(led_topic(bed), cmd)
    print(f"[MODE] LED {bed} {'ON' if on else 'OFF'} topic={led_topic(bed)} cmd={cmd}", flush=True)

def pub_motor_for_bed(bed: str, dir_: str, revs: float):
    cmd = {"revs": float(revs), "dir": dir_}
    mqtt_pub(motor_topic(bed), cmd)
    print(f"[MODE] MOTOR {bed} topic={motor_topic(bed)} cmd={cmd}", flush=True)

def apply_mode_transition_for_bed(con, bed: str, new_mode: str, rt):
    motor_raised  = bool(rt["motor_raised"])  if rt else False
    motor_lowered = bool(rt["motor_lowered"]) if rt else False

    if new_mode == "indoor":
        pub_led_for_bed(bed, True, LED_CHUNK_SEC)

        # motor DOWN only once after an UP in the same day
        if motor_raised and not motor_lowered:
            pub_motor_for_bed(bed, "down", MOTOR_REVS_DOWN)
            upsert_runtime(con, bed, motor_lowered=1)
            print(f"[MODE] {bed} indoor -> motor DOWN (once after last UP)", flush=True)
        else:
            print(f"[MODE] {bed} indoor -> motor unchanged (raised={motor_raised}, lowered={motor_lowered})", flush=True)

    else:
        # outdoor (day): LEDs OFF and motor UP on transition (so if it was down in indoor, it rises)
        pub_led_for_bed(bed, False, 0)

        pub_motor_for_bed(bed, "up", MOTOR_REVS_UP)
        upsert_runtime(con, bed, motor_raised=1, motor_lowered=0)
        print(f"[MODE] {bed} outdoor(day) -> motor UP (on transition)", flush=True)

def run_mode_state_machine(con) -> str:
    ensure_bed_runtime(con)

    raw = get_raw_mode(con)
    night = is_night_now(con)
    eff = compute_effective_mode(con, raw)

    beds = list_beds(con)
    print(f"[MODE] loop raw={raw} night={night} effective={eff} beds={beds} master={BED}", flush=True)

    for b in beds:
        rt = get_runtime_today(con, b)
        last = rt["last_effective_mode"] if rt and rt["last_effective_mode"] else None

        if eff != last:
            print(f"[MODE] TRANSITION {b} {last} -> {eff}", flush=True)
            upsert_runtime(con, b, last_effective_mode=eff)
            apply_mode_transition_for_bed(con, b, eff, rt)
        else:
            print(f"[MODE] stable {b} {eff}: no actions", flush=True)

    return eff

# ---------------------------
# POLICY (per BED) + LED CHUNK (FIXED)
# ---------------------------

def get_policy_today(con):
    return get_row(con, "SELECT * FROM bed_policy WHERE bed=? AND date=?", (BED, today_ymd()))

def get_last_policy_update(con):
    r = get_policy_today(con)
    return r["updated_at"] if r else None

def recent_enough(last_iso: str, minutes: int) -> bool:
    if not last_iso:
        return False
    try:
        t = datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%S")
        return (now_local() - t) < timedelta(minutes=minutes)
    except Exception:
        return False

def load_context(con):
    bed_state = get_row(con, "SELECT * FROM bed_state WHERE bed=?", (BED,))
    crop = None
    if bed_state and bed_state["crop_name"]:
        crop = get_row(con, "SELECT * FROM crops WHERE name=?", (bed_state["crop_name"],))

    telem = get_row(con, "SELECT * FROM telemetry_last WHERE zone=?", (BED,))

    weather_day = get_row(con, "SELECT * FROM weather_day WHERE date=date('now','localtime')")
    weather_h = con.execute("""
      SELECT ts,temp_c,precip_mm
      FROM weather_forecast_hourly
      WHERE ts >= strftime('%Y-%m-%d %H:00:00','now','localtime')
        AND ts <  strftime('%Y-%m-%d %H:00:00','now','localtime','+24 hours')
      ORDER BY ts
    """).fetchall()

    daily_light = get_row(con, "SELECT * FROM daily_light WHERE bed=? AND date=date('now','localtime')", (BED,))
    daily_irrig = get_row(con, "SELECT * FROM daily_irrig WHERE bed=? AND date=date('now','localtime')", (BED,))

    def rows_to_list(rs):
        return [dict(r) for r in rs] if rs else []

    return {
        "now_local": now_local().strftime("%Y-%m-%dT%H:%M:%S"),
        "bed": BED,
        "bed_state": dict(bed_state) if bed_state else None,
        "crop": dict(crop) if crop else None,
        "telemetry_last": dict(telem) if telem else None,
        "weather_day": dict(weather_day) if weather_day else None,
        "weather_next_24h": rows_to_list(weather_h),
        "daily_light": dict(daily_light) if daily_light else None,
        "daily_irrig": dict(daily_irrig) if daily_irrig else None,
        "limits": {
            "lights_max_hours_per_day": LIGHTS_MAX_HOURS_PER_DAY,
            "lights_night_only": LIGHTS_NIGHT_ONLY
        }
    }

def call_claude(ctx):
    if not ANTHROPIC_API_KEY:
        return {
            "irrig_window": None,
            "allow_day_led": False,
            "extra_light_h": 0.0,
            "reason": "NO_API_KEY",
            "raw": {"error": "ANTHROPIC_API_KEY missing"}
        }

    system = (
      "You are an agronomy strategy assistant for a greenhouse.\n"
      "Return ONLY valid JSON. Do not include markdown.\n"
      "You decide strategy ONLY (when is best to irrigate today, and whether to add extra artificial light).\n"
      "Never command relays directly. The controller enforces safety limits.\n"
      "JSON schema:\n"
      "{"
      "\"irrig_window\":{\"from\":\"HH:MM\",\"to\":\"HH:MM\"}|null,"
      "\"allow_day_led\":true|false,"
      "\"extra_light_h\":number,"
      "\"reason\":\"string\""
      "}\n"
      "Guidelines:\n"
      "- If a freeze risk exists (near or below 0-1C) prefer to avoid irrigation or choose warmest part of day.\n"
      "- If cistern not ok, still provide strategy but expect controller may block.\n"
      "- For outdoor cloudy: you may set allow_day_led=true and extra_light_h > 0.\n"
      "- Keep extra_light_h between 0 and 6.\n"
      "- Irrigation window should be inside 06:00-22:00 if possible.\n"
    )

    user = {"task": "Choose best irrigation window for today and whether to add extra artificial light.", "context": ctx}
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": json.dumps(user, ensure_ascii=False)}]
    }

    cmd = [
        "curl","-sS","-m","20","https://api.anthropic.com/v1/messages",
        "-H", f"x-api-key: {ANTHROPIC_API_KEY}",
        "-H", "anthropic-version: 2023-06-01",
        "-H", "content-type: application/json",
        "-d", json.dumps(payload)
    ]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)

    text = ""
    for part in data.get("content", []):
        if part.get("type") == "text":
            text += part.get("text","")

    try:
        res = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return {"irrig_window": None, "allow_day_led": False, "extra_light_h": 0.0, "reason": "PARSE_FAIL", "raw": {"text": text, "api": data}}
        res = json.loads(m.group(0))

    irrig = res.get("irrig_window", None)
    if irrig:
        f = parse_hhmm(irrig.get("from",""))
        t = parse_hhmm(irrig.get("to",""))
        irrig = {"from": f, "to": t} if (f and t) else None

    allow_day_led = bool(res.get("allow_day_led", False))
    extra_light_h = float(res.get("extra_light_h", 0.0) or 0.0)
    extra_light_h = clamp(extra_light_h, 0.0, 6.0)
    reason = str(res.get("reason","")).strip()[:240]

    return {"irrig_window": irrig, "allow_day_led": allow_day_led, "extra_light_h": extra_light_h, "reason": reason if reason else "OK", "raw": res}

def upsert_policy(con, pol):
    vf, vt = "00:00", "23:59"
    irrig_from = pol["irrig_window"]["from"] if pol["irrig_window"] else None
    irrig_to   = pol["irrig_window"]["to"]   if pol["irrig_window"] else None

    con.execute("""
      INSERT INTO bed_policy(bed,date,valid_from,valid_to,irrig_from,irrig_to,allow_day_led,extra_light_h,reason,raw_json,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
      ON CONFLICT(bed,date) DO UPDATE SET
        valid_from=excluded.valid_from,
        valid_to=excluded.valid_to,
        irrig_from=excluded.irrig_from,
        irrig_to=excluded.irrig_to,
        allow_day_led=excluded.allow_day_led,
        extra_light_h=excluded.extra_light_h,
        reason=excluded.reason,
        raw_json=excluded.raw_json,
        updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
    """, (
        BED, today_ymd(), vf, vt,
        irrig_from, irrig_to,
        1 if pol["allow_day_led"] else 0,
        pol["extra_light_h"],
        pol["reason"],
        json.dumps(pol["raw"], ensure_ascii=False)
    ))
    con.commit()

def daily_light_done_sec(con):
    r = get_row(con, "SELECT seconds_done FROM daily_light WHERE bed=? AND date=date('now','localtime')", (BED,))
    return int(r["seconds_done"]) if r and r["seconds_done"] is not None else 0

def is_led_currently_on(con) -> bool:
    # Avoid resetting LED timer on ESP: do not send new chunk if telemetry says it's already ON.
    t = get_row(con, "SELECT relay_led FROM telemetry_last WHERE zone=?", (BED,))
    if not t:
        return False
    try:
        return int(t["relay_led"] or 0) == 1
    except Exception:
        return False

def maybe_led_chunk(con, pol, effective_mode: str):
    """
    Fixed behavior:
      - outdoor(day): LEDs must stay OFF => policy cannot turn them ON
      - respect LIGHTS_NIGHT_ONLY (unless allow_day_led)
      - respect MAX daily hours
      - respect extra_light_h quota (do not exceed target seconds for today)
      - do not send chunk if telemetry says LED is already ON (prevents timer reset)
      - never infinite: always send {"on": true, "secs": N}; if remaining < 1800, send remainder
    """
    if effective_mode == "outdoor":
        return

    extra_h = float(pol.get("extra_light_h", 0.0) or 0.0)
    if extra_h <= 0.0:
        return

    allow_day = bool(pol.get("allow_day_led", False))
    night = is_night_now(con)
    if LIGHTS_NIGHT_ONLY and (not night) and (not allow_day):
        return

    # If LED already ON, don't publish a new chunk (avoid resetting timer)
    if is_led_currently_on(con):
        print("[POLICY] LED already ON (telemetry), skip chunk to avoid timer reset", flush=True)
        return

    done = daily_light_done_sec(con)
    max_sec = int(LIGHTS_MAX_HOURS_PER_DAY * 3600)

    # Target quota from Claude (cap at max daily safety)
    target_sec = int(round(extra_h * 3600))
    if target_sec < 0:
        target_sec = 0
    if target_sec > max_sec:
        target_sec = max_sec

    if done >= max_sec:
        return
    if done >= target_sec:
        print(f"[POLICY] extra target reached: done={done}s target={target_sec}s", flush=True)
        return

    remain_to_target = target_sec - done
    remain_to_max = max_sec - done
    secs = min(LED_CHUNK_SEC, remain_to_target, remain_to_max)
    if secs <= 0:
        return

    cmd = {"on": True, "secs": int(secs)}
    mqtt_pub(led_topic(BED), cmd)
    print(f"[POLICY] LED chunk pub {led_topic(BED)} {cmd} (done={done}s target={target_sec}s max={max_sec}s)", flush=True)

def main():
    con = db()

    raw = get_raw_mode(con)
    eff_for_policy = compute_effective_mode(con, raw)

    # 1) Global mode actions (LED+motor for all beds) only if master
    if should_run_mode_actions():
        eff_for_policy = run_mode_state_machine(con)
    else:
        print(f"[MODE] skipped global actions (MODE_MASTER_BED={MODE_MASTER_BED}, this BED={BED})", flush=True)

    # 2) Policy update + possible LED chunk (blocked in outdoor day)
    last = get_last_policy_update(con)
    if recent_enough(last, POLICY_MIN_INTERVAL_MIN):
        polrow = get_policy_today(con)
        if polrow:
            pol = {
                "extra_light_h": float(polrow["extra_light_h"] or 0.0),
                "allow_day_led": bool(int(polrow["allow_day_led"] or 0))
            }
            maybe_led_chunk(con, pol, eff_for_policy)
        return 0

    ctx = load_context(con)
    pol = call_claude(ctx)

    upsert_policy(con, pol)
    print(f"[POLICY] saved bed={BED} irrig={pol['irrig_window']} allow_day_led={pol['allow_day_led']} extra_light_h={pol['extra_light_h']} reason={pol['reason']}", flush=True)

    maybe_led_chunk(con, pol, eff_for_policy)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
