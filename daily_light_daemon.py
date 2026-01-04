#!/usr/bin/env python3
import os, json, sqlite3, time, signal
from datetime import datetime
import paho.mqtt.client as mqtt

DB_PATH   = os.getenv("SERRA_DB", "/opt/serra/serra.db")
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

# serra/cmd/<bed>/relay/led  payload: {"on":true,"secs":10}
# serra/ack/<bed>/relay/led  (se esiste) payload: {"ok":true,"on":true}
SUB_CMD = os.getenv("SUB_CMD", "serra/cmd/+/relay/led")
SUB_ACK = os.getenv("SUB_ACK", "serra/ack/+/relay/led")

TICK_SEC = int(os.getenv("TICK_SEC", "30"))

_running = True

def log(*a):
    print("[DAILY_LIGHT]", *a, flush=True)

def now_local():
    return datetime.now()

def iso_now():
    return now_local().strftime("%Y-%m-%dT%H:%M:%S")

def today_ymd():
    return now_local().strftime("%Y-%m-%d")

def parse_iso(s: str):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con

def ensure_row(con, bed: str, date: str):
    con.execute("""
      INSERT INTO daily_light (bed, date, seconds_done, last_ts, last_led_on, updated_at)
      VALUES (?, ?, 0, NULL, 0, strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
      ON CONFLICT(bed, date) DO NOTHING
    """, (bed, date))

def get_row(con, bed: str, date: str):
    return con.execute("""
      SELECT bed,date,seconds_done,last_ts,last_led_on,updated_at
      FROM daily_light
      WHERE bed=? AND date=?
    """, (bed, date)).fetchone()

def add_seconds(con, bed: str, date: str, delta_s: int):
    if delta_s <= 0:
        return
    con.execute("""
      UPDATE daily_light
      SET seconds_done = seconds_done + ?,
          last_ts = ?,
          updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
      WHERE bed=? AND date=?
    """, (int(delta_s), iso_now(), bed, date))

def set_led_state(con, bed: str, on: bool):
    date = today_ymd()
    ensure_row(con, bed, date)
    row = get_row(con, bed, date)

    prev_on = int(row["last_led_on"] or 0) if row else 0
    prev_ts = row["last_ts"] if row else None

    # Se arriva ON ma eravamo gia ON, ignora (evita spam)
    if on and prev_on == 1:
        return


    # Se arriva OFF e prima era ON -> accumula subito dal last_ts
    if (not on) and prev_on == 1 and prev_ts:
        try:
            dt_last = parse_iso(prev_ts)
            dt_now = now_local()
            delta = int((dt_now - dt_last).total_seconds())
            if delta < 0:
                delta = 0
            # clamp: evita salti enormi se last_ts è vecchio o sporco
            if delta > 6 * TICK_SEC:
                delta = 6 * TICK_SEC
            add_seconds(con, bed, date, delta)
        except Exception:
            pass

    # Aggiorna stato e timestamp evento
    con.execute("""
      UPDATE daily_light
      SET last_led_on = ?,
          last_ts = ?,
          updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
      WHERE bed=? AND date=?
    """, (1 if on else 0, iso_now(), bed, date))

def tick_one(con, bed: str):
    date = today_ymd()
    row = get_row(con, bed, date)
    if not row:
        return
    if int(row["last_led_on"] or 0) != 1:
        return
    last_ts = row["last_ts"]
    if not last_ts:
        # se manca, inizializza senza contare
        con.execute("""
          UPDATE daily_light
          SET last_ts = ?,
              updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
          WHERE bed=? AND date=?
        """, (iso_now(), bed, date))
        return

    try:
        dt_last = parse_iso(last_ts)
        dt_now = now_local()
        delta = int((dt_now - dt_last).total_seconds())
        if delta < 0:
            delta = 0
        # clamp ragionevole al tick
        if delta > 2 * TICK_SEC:
            delta = 2 * TICK_SEC
        add_seconds(con, bed, date, delta)
    except Exception:
        # se parse fallisce, resetta last_ts
        con.execute("""
          UPDATE daily_light
          SET last_ts = ?,
              updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
          WHERE bed=? AND date=?
        """, (iso_now(), bed, date))

def parse_on(payload: str):
    try:
        data = json.loads(payload)
    except Exception:
        return None
    v = data.get("on")
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "on", "yes"):
            return True
        if s in ("0", "false", "off", "no"):
            return False
    return None

def bed_from_topic(topic: str):
    # serra/cmd/<bed>/relay/led  or serra/ack/<bed>/relay/led
    parts = topic.split("/")
    return parts[2] if len(parts) >= 5 else None

def on_connect(cli, userdata, flags, rc):
    log("connected rc=", rc)
    cli.subscribe([(SUB_CMD, 0), (SUB_ACK, 0)])
    log("sub", SUB_CMD, "and", SUB_ACK)

def on_message(cli, userdata, msg):
    bed = bed_from_topic(msg.topic)
    if not bed:
        return
    on = parse_on(msg.payload.decode("utf-8", "ignore"))
    if on is None:
        return
    with db() as con:
        set_led_state(con, bed, on)
        con.commit()
    log("event", msg.topic, "->", "ON" if on else "OFF")

def _sig(signum, frame):
    global _running
    _running = False
    log("SIGTERM")

def main():
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    cli = mqtt.Client(client_id=f"serra-daily-light-{int(time.time())}", protocol=mqtt.MQTTv311)
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    cli.loop_start()

    while _running:
        try:
            with db() as con:
                date = today_ymd()
                beds = [r["bed"] for r in con.execute(
                    "SELECT bed FROM daily_light WHERE date=? AND last_led_on=1",
                    (date,)
                ).fetchall()]
                for b in beds:
                    tick_one(con, b)
                con.commit()
        except Exception as e:
            log("tick error:", repr(e))
        time.sleep(TICK_SEC)

    cli.loop_stop()
    cli.disconnect()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
