#!/usr/bin/env python3
import os, json, time, signal, sys, subprocess
import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
SUB_TOPIC = "serra/nfc/#"   # es: serra/nfc/bed2 {"uid":"04:AB:23:FE"} oppure payload="04AB23FE"

PYTHON = os.getenv("PYTHON", "/opt/serra/venv/bin/python")
APPLY_SCRIPT = os.getenv("APPLY_SCRIPT", "/opt/serra/apply_nfc_tag.py")

def log(*a): print("[NFC]", *a, flush=True)

def norm_uid(u: str) -> str:
    return (u or "").strip().replace(":", "").upper()

def parse_zone_uid(topic: str, payload: str):
    try:
        data = json.loads(payload) if payload.startswith("{") else {"uid": payload}
    except Exception as e:
        log("bad json:", e)
        return None, None

    parts = topic.split("/")
    zone = data.get("zone") or (parts[2] if len(parts) >= 3 else None)
    uid  = (data.get("uid") or "").strip()
    return zone, uid

def blink_led(cli, zone: str):
    # 1 secondo di blink: stesso schema del planner
    try:
        cli.publish(f"serra/cmd/{zone}/relay/led", json.dumps({"on": True, "secs": 1}), qos=0, retain=False)
    except Exception:
        pass

def on_connect(cli, userdata, flags, rc, props=None):
    log("connected rc=", rc)
    cli.subscribe(SUB_TOPIC, qos=0)

def on_message(cli, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8","ignore").strip()
    zone, uid_in = parse_zone_uid(topic, payload)

    if not zone or not uid_in:
        log("missing zone/uid", topic, payload)
        return

    uid_norm = norm_uid(uid_in)

    # Chiama apply_nfc_tag.py passando l'UID normalizzato
    try:
        p = subprocess.run(
            [PYTHON, APPLY_SCRIPT, uid_norm],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()

        if p.returncode == 0:
            ev = {"ok": True, "zone": zone, "uid": uid_in, "uid_norm": uid_norm, "msg": out}
            cli.publish(f"serra/events/nfc/{zone}", json.dumps(ev), qos=0, retain=False)
            blink_led(cli, zone)
            log("OK", zone, uid_in, "->", uid_norm, out)
        else:
            ev = {"ok": False, "zone": zone, "uid": uid_in, "uid_norm": uid_norm, "error": out or err or f"rc={p.returncode}"}
            cli.publish(f"serra/events/nfc/{zone}", json.dumps(ev), qos=0, retain=False)
            log("FAIL", zone, uid_in, "->", uid_norm, ev["error"])
    except Exception as e:
        ev = {"ok": False, "zone": zone, "uid": uid_in, "uid_norm": uid_norm, "error": str(e)}
        cli.publish(f"serra/events/nfc/{zone}", json.dumps(ev), qos=0, retain=False)
        log("EXC", e)

def main():
    cli = mqtt.Client(client_id=f"serra-nfc-bridge-{int(time.time())}", clean_session=True, protocol=mqtt.MQTTv311)
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(MQTT_HOST, MQTT_PORT, keepalive=30)

    def _sig(*_):
        cli.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    cli.loop_forever()

if __name__ == "__main__":
    main()
