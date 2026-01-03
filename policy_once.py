#!/usr/bin/env python3
import os, json, sqlite3, subprocess, re
from datetime import datetime, timedelta

DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")
BED     = os.environ.get("BED", "bed2")

# Claude / Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-3-5-haiku-latest")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "400"))

# Quanto spesso aggiornare policy (minuti); cron può essere ogni 5 min ma policy la aggiorniamo quando serve.
POLICY_MIN_INTERVAL_MIN = int(os.environ.get("POLICY_MIN_INTERVAL_MIN", "5"))

# Safety LED (riusiamo i tuoi limiti)
LIGHTS_MAX_HOURS_PER_DAY = float(os.environ.get("LIGHTS_MAX_HOURS_PER_DAY", "16"))
LIGHTS_NIGHT_ONLY        = os.environ.get("LIGHTS_NIGHT_ONLY", "true").lower() == "true"

# Pub MQTT (per chunk LED)
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MOSQUITTO_PUB = os.environ.get("MOSQUITTO_PUB", "mosquitto_pub")
LED_CHUNK_SEC = int(os.environ.get("LED_CHUNK_SEC", "1800"))  # 30 min

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con

def now_local():
    return datetime.now()

def today_ymd():
    return now_local().strftime("%Y-%m-%d")

def hhmm():
    return now_local().strftime("%H:%M")

def parse_hhmm(s: str):
    if not s or not re.match(r"^\d{2}:\d{2}$", s): return None
    h, m = s.split(":")
    h = int(h); m = int(m)
    if h < 0 or h > 23 or m < 0 or m > 59: return None
    return f"{h:02d}:{m:02d}"

def clamp(x, a, b):
    return a if x < a else (b if x > b else x)

def get_row(con, sql, args=()):
    cur = con.execute(sql, args)
    return cur.fetchone()

def get_policy_today(con):
    return get_row(con, "SELECT * FROM bed_policy WHERE bed=? AND date=?", (BED, today_ymd()))

def get_last_policy_update(con):
    r = get_policy_today(con)
    return r["updated_at"] if r else None

def recent_enough(last_iso: str, minutes: int) -> bool:
    if not last_iso: return False
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
    # prossime 24h: min temp e pioggia totale
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

    ctx = {
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
    return ctx

def call_claude(ctx):
    """
    Ritorna dict con:
      irrig_window: {"from":"HH:MM","to":"HH:MM"} oppure null
      allow_day_led: bool
      extra_light_h: float (0..N)
      reason: str
    """
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

    user = {
        "task": "Choose best irrigation window for today and whether to add extra artificial light.",
        "context": ctx
    }

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": json.dumps(user, ensure_ascii=False)}]
    }

    # chiamata via curl (minimo impatto, niente dipendenze python extra)
    cmd = [
        "curl","-sS","-m","20","https://api.anthropic.com/v1/messages",
        "-H", f"x-api-key: {ANTHROPIC_API_KEY}",
        "-H", "anthropic-version: 2023-06-01",
        "-H", "content-type: application/json",
        "-d", json.dumps(payload)
    ]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)

    # estrai testo
    text = ""
    for part in data.get("content", []):
        if part.get("type") == "text":
            text += part.get("text","")

    # deve essere JSON puro
    try:
        res = json.loads(text)
    except Exception:
        # fallback: prova a cercare la prima {...}
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return {
                "irrig_window": None,
                "allow_day_led": False,
                "extra_light_h": 0.0,
                "reason": "PARSE_FAIL",
                "raw": {"text": text, "api": data}
            }
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

    return {
        "irrig_window": irrig,
        "allow_day_led": allow_day_led,
        "extra_light_h": extra_light_h,
        "reason": reason if reason else "OK",
        "raw": res
    }

def upsert_policy(con, pol):
    # validità: oggi tutto il giorno (minimo impatto)
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

def is_night_now(con):
    """
    Ritorna True se adesso è notte (fuori da [sunrise, sunset]).
    Usa weather_day se disponibile, altrimenti fallback conservativo: considera "giorno" 08:00-16:30.
    """
    now = now_local()
    t = now.strftime("%H:%M")

    wd = con.execute(
        "SELECT sunrise, sunset FROM weather_day WHERE date=date('now','localtime')"
    ).fetchone()

    if wd is not None:
        sunrise = wd["sunrise"]
        sunset  = wd["sunset"]
        if sunrise and sunset:
            return not (sunrise <= t <= sunset)

    return not ("08:00" <= t <= "16:30")

def daily_light_done_sec(con):
    r = get_row(con, "SELECT seconds_done FROM daily_light WHERE bed=? AND date=date('now','localtime')", (BED,))
    return int(r["seconds_done"]) if r and r["seconds_done"] is not None else 0

def maybe_led_chunk(con, pol):
    """
    Se Claude vuole extra luce e consente LED di giorno, pubblichiamo un chunk ON da 30 min.
    Safety:
      - non superare LIGHTS_MAX_HOURS_PER_DAY
      - se LIGHTS_NIGHT_ONLY e non allow_day_led -> non fare nulla
    """
    extra_h = float(pol.get("extra_light_h", 0.0) or 0.0)
    if extra_h <= 0.0:
        return

    allow_day = bool(pol.get("allow_day_led", False))
    night = is_night_now(con)

    if LIGHTS_NIGHT_ONLY and (not night) and (not allow_day):
        return

    # budget giornaliero
    done = daily_light_done_sec(con)
    max_sec = int(LIGHTS_MAX_HOURS_PER_DAY * 3600)
    if done >= max_sec:
        return

    # se è giorno e allow_day_led è true, ok.
    # se è notte e lights_night_only true, ok.
    remain_sec = max_sec - done
    secs = min(LED_CHUNK_SEC, remain_sec)

    topic = f"serra/cmd/{BED}/relay/led"
    cmd = {"on": True, "secs": int(secs)}
    subprocess.call([MOSQUITTO_PUB, "-h", MQTT_HOST, "-t", topic, "-m", json.dumps(cmd)])
    print(f"[POLICY] LED chunk pub {topic} {cmd} (done={done}s max={max_sec}s)", flush=True)

def main():
    con = db()

    last = get_last_policy_update(con)
    if recent_enough(last, POLICY_MIN_INTERVAL_MIN):
        # policy aggiornata di recente: esegui solo eventuale LED chunk (così reagisce anche senza nuova call)
        polrow = get_policy_today(con)
        if polrow:
            pol = {
                "extra_light_h": float(polrow["extra_light_h"] or 0.0),
                "allow_day_led": bool(int(polrow["allow_day_led"] or 0))
            }
            maybe_led_chunk(con, pol)
        return 0

    ctx = load_context(con)
    pol = call_claude(ctx)

    upsert_policy(con, pol)
    print(f"[POLICY] saved bed={BED} irrig={pol['irrig_window']} allow_day_led={pol['allow_day_led']} extra_light_h={pol['extra_light_h']} reason={pol['reason']}", flush=True)

    # LED chunk (se serve)
    maybe_led_chunk(con, pol)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
