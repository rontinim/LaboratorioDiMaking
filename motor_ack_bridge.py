#!/usr/bin/env python3
import os
import json
import sqlite3
import time
import signal
import sys
import paho.mqtt.client as mqtt

DB_PATH   = os.getenv("SERRA_DB", "/opt/serra/serra.db")
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
SUB_TOPIC = os.getenv("SUB_TOPIC", "serra/ack/+/motor")  # serra/ack/<bed>/motor

def log(*a):
    print("[MOTOR_ACK]", *a, flush=True)

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con

def parse_ack(topic: str, payload: str):
    parts = topic.split("/")
    bed = parts[2] if len(parts) >= 4 else None

    try:
        data = json.loads(payload) if payload.strip().startswith("{") else {"raw": payload}
    except Exception:
        data = {"raw": payload}

    direction = (data.get("dir") or data.get("direction") or "").strip().lower()
    position  = (data.get("position") or data.get("motor_position") or "").strip().lower()
    raw       = (data.get("raw") or "").strip().lower()

    pos = None
    for cand in (position, direction, raw):
        if cand in ("up", "down"):
            pos = cand
            break

    ok = data.get("ok")
    return bed, pos, ok, data

def update_bed_state(bed: str, pos: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        UPDATE bed_state
        SET motor_position = ?,
            last_update = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
        WHERE bed = ?
    """, (pos, bed))
    con.commit()
    con.close()

def on_connect(cli, userdata, flags, rc, props=None):
    log("connected rc=", rc)
    cli.subscribe(SUB_TOPIC, qos=0)

def on_message(cli, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8", "ignore").strip()
    log("msg", topic, payload)

    bed, pos, ok, data = parse_ack(topic, payload)

    if not bed:
        log("skip: cannot parse bed from topic")
        return
    if pos not in ("up", "down"):
        log("skip: cannot parse motor position (need up/down)")
        return

    try:
        update_bed_state(bed, pos)
        log(f"updated bed_state: bed={bed} motor_position={pos}")
    except Exception as e:
        log("db update error:", e)

def main() -> int:
    cli = mqtt.Client(
        client_id=f"serra-motor-ack-{int(time.time())}",
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    cli.on_connect = on_connect
    cli.on_message = on_message

    cli.connect(MQTT_HOST, MQTT_PORT, keepalive=30)

    def _sig(*_):
        try:
            cli.disconnect()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    cli.loop_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
