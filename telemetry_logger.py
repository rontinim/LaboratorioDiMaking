#!/usr/bin/env python3
import json, sqlite3, os, time, signal
import paho.mqtt.client as mqtt

DB_PATH   = os.environ.get("SERRA_DB", "/opt/serra/serra.db")
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TOPIC     = os.environ.get("TOPIC", "serra/telemetry/#")

TABLE = "telemetry"   # <-- si scrive nella TABELLA, non nella VIEW

conn = None
COLUMNS = set()

def ensure_conn():
    global conn
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def get_columns():
    c = ensure_conn().execute(f"PRAGMA table_info({TABLE})")
    return {r["name"] for r in c.fetchall()}

def now_sql():
    return "strftime('%Y-%m-%d %H:%M:%S','now','localtime')"

def on_connect(client, userdata, flags, rc, properties=None):
    client.subscribe(TOPIC)
    print(f"[telemetry_logger] connected rc={rc}, sub={TOPIC}", flush=True)

def on_message(client, userdata, msg):
    global COLUMNS
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"[WARN] bad json on {msg.topic}: {e}", flush=True)
        return

    parts = msg.topic.split("/")
    zone = parts[2] if len(parts) >= 3 else None
    if not zone:
        print(f"[WARN] cannot parse zone from {msg.topic}", flush=True)
        return

    if (not COLUMNS) or (int(time.time()) % 60 == 0):
        try:
            COLUMNS = get_columns()
        except Exception as e:
            print(f"[WARN] get_columns failed: {e}", flush=True)

    # Normalizzazione: soil_pct (0..100) -> soil_cap (0..1)
    if "soil_pct" in payload and "soil_cap" not in payload:
        try:
            payload["soil_cap"] = float(payload["soil_pct"]) / 100.0
        except Exception:
            pass

    payload_cols = [k for k in payload.keys() if k in COLUMNS and k not in ("id","ts","zone")]
    cols = ["ts","zone"] + payload_cols
    vals = [now_sql(),"?" ] + ["?"]*len(payload_cols)
    args = [zone] + [payload[k] for k in payload_cols]

    sql  = f"INSERT INTO {TABLE} ({','.join(cols)}) VALUES ({','.join(vals)})"

    try:
        ensure_conn().execute(sql, args)
    except sqlite3.OperationalError as e:
        print(f"[SQL] {e} -> refreshing schema and retry", flush=True)
        try:
            COLUMNS = get_columns()
            payload_cols = [k for k in payload.keys() if k in COLUMNS and k not in ("id","ts","zone")]
            cols = ["ts","zone"] + payload_cols
            vals = [now_sql(),"?" ] + ["?"]*len(payload_cols)
            args = [zone] + [payload[k] for k in payload_cols]
            sql  = f"INSERT INTO {TABLE} ({','.join(cols)}) VALUES ({','.join(vals)})"
            ensure_conn().execute(sql, args)
        except Exception as e2:
            print(f"[SQL-FAIL] {e2}", flush=True)
    except Exception as e:
        print(f"[FAIL] {e}", flush=True)

def handle_sigterm(signum, frame):
    global conn
    try:
        if conn:
            conn.close()
    finally:
        os._exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

client = mqtt.Client(client_id=f"serra-telemetry-logger-{int(time.time())}", protocol=mqtt.MQTTv311)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
