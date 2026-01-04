import os, json, re, time, threading, sqlite3, requests, datetime as dt
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import paho.mqtt.client as mqtt
from anthropic import Anthropic

app = FastAPI(title="Serra MCP Bridge", version="0.5.0")

# ===== ENV =====
MQTT_BROKER = os.getenv("MQTT_BROKER","localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT","1883"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL","claude-3-5-haiku-latest")
GPS_LAT = float(os.getenv("GPS_LAT","0") or 0)
GPS_LON = float(os.getenv("GPS_LON","0") or 0)
PLAN_INTERVAL_MIN = int(os.getenv("PLAN_INTERVAL_MIN","30"))
ZONES = [z.strip() for z in (os.getenv("ZONES","bed1").split(",")) if z.strip()]
DB_PATH = os.getenv("DB_PATH","/var/lib/serra/serra.db")

# LIMITI (default da ENV; sovrascrivibili via /config/limits)
DEFAULT_LIMITS = {
  "max_ml_per_call": int(os.getenv("MAX_IRRIGATE_ML_PER_CALL","300")),
  "max_ml_per_hour": int(os.getenv("MAX_IRRIGATE_ML_PER_HOUR","600")),
  "max_ml_per_day":  int(os.getenv("MAX_IRRIGATE_ML_PER_DAY","1500")),
  "min_interval_min": int(os.getenv("MIN_INTERVAL_BETWEEN_IRRIGATIONS_MIN","20")),
  "irrig_start": os.getenv("IRRIGATION_ALLOWED_START","06:00"),
  "irrig_end":   os.getenv("IRRIGATION_ALLOWED_END","22:00"),
  "lights_max_hours_per_day": int(os.getenv("LIGHTS_MAX_HOURS_PER_DAY","16")),
  "lights_night_only": os.getenv("LIGHTS_NIGHT_ONLY","true").lower()=="true",
}
LIMITS = DEFAULT_LIMITS.copy()

# ===== DB =====
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn
DB = db_conn()
def db_exec(q, params=()):
    cur = DB.cursor()
    cur.execute(q, params)
    DB.commit()
    return cur

# load limits from config table if present
def load_limits():
    global LIMITS
    try:
        cur = db_exec("SELECT value FROM config WHERE key='limits'")
        row = cur.fetchone()
        if row:
            LIMITS.update(json.loads(row[0]))
            print("[LIMITS] loaded from DB:", LIMITS)
        else:
            print("[LIMITS] using defaults:", LIMITS)
    except Exception as e:
        print("[LIMITS] load error:", e)
load_limits()

# ===== MQTT =====
mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="serra-mcp")
_mqtt_ready = False
LAST = {}  # cache ultimi valori per zone

def _mqtt_connect_with_retry(max_tries=30, delay_s=1.0):
    global _mqtt_ready
    for i in range(1, max_tries+1):
        try:
            mqttc.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqttc.loop_start()
            mqttc.subscribe("serra/telemetry/#", qos=0)
            print(f"[MQTT] connected {MQTT_BROKER}:{MQTT_PORT} (try {i})")
            _mqtt_ready = True
            return
        except Exception as e:
            print(f"[MQTT] connect failed ({i}/{max_tries}): {e}")
            time.sleep(delay_s)
    _mqtt_ready = False

def on_message(client, userdata, msg):
    try:
        parts = msg.topic.split("/")
        if len(parts) >= 3:
            zone = parts[2]
        else:
            return
        payload = msg.payload.decode("utf-8")
        db_exec("INSERT INTO telemetry(zone, payload) VALUES(?,?)", (zone, payload))
        data = json.loads(payload)
        LAST[zone] = data
    except Exception as e:
        print(f"[TEL ERROR] {e}")
mqttc.on_message = on_message

# ===== CLAUDE =====
client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
if not ANTHROPIC_API_KEY:
    print("[WARN] ANTHROPIC_API_KEY non impostata: /ai/* darà errore")

# ===== Schemi API =====
class PubReq(BaseModel):
    topic: str
    payload: object
    qos: int = 0
    retain: bool = False

class AiPingReq(BaseModel):
    q: str = "2+2?"

class PlanReq(BaseModel):
    zone: str
    crop: str
    soil_cap: float | None = None
    soil_wc: float | None = None
    air_temp_c: float | None = None
    air_rh: float | None = None
    vpd_kpa: float | None = None
    light_lux: float | None = None
    eto_mm: float | None = None
    forecast: dict | None = None
    constraints: dict | None = None
    publish: bool = True

# ===== Utils =====
def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, re.S)
        if not m:
            raise ValueError("Nessun JSON trovato")
        return json.loads(m.group(0))

def _within_window(now: dt.datetime, start_str: str, end_str: str) -> bool:
    h1, m1 = map(int, start_str.split(":"))
    h2, m2 = map(int, end_str.split(":"))
    t = now.time()
    start_t = dt.time(h1, m1); end_t = dt.time(h2, m2)
    if start_t <= end_t:
        return start_t <= t <= end_t
    else:
        # finestra che attraversa la mezzanotte
        return t >= start_t or t <= end_t

def _sum_irrig(zone: str, hours: int):
    # somma irrigazioni negli ultimi N hours
    cur = db_exec("SELECT ts, plan_json FROM setpoints WHERE zone=? AND ts >= datetime('now','-%d hours')" % hours, (zone,))
    rows = cur.fetchall()
    s = 0; last_ts = None
    for ts, pj in rows:
        try:
            p = json.loads(pj)
            ml = int(p.get("irrigate_ml", 0) or 0)
            s += ml
            if ml > 0:
                last_ts = ts
        except: pass
    return s, last_ts

def _lights_used_today(zone: str):
    today = dt.date.today().isoformat()
    cur = db_exec("SELECT ts, plan_json FROM setpoints WHERE zone=? AND date(ts)=date('now','localtime')", (zone,))
    rows = cur.fetchall()
    used = 0
    for ts, pj in rows:
        try:
            p = json.loads(pj)
            L = p.get("lights") or {}
            if L.get("on"):
                used += int(L.get("hours",0) or 0)
        except: pass
    return used

def _apply_safety(plan: dict, zone: str) -> dict:
    notes = plan.get("notes","")
    now = dt.datetime.now()
    # irrigate_ml normalizza e clamp
    irrig = int(plan.get("irrigate_ml", 0) or 0)
    if irrig < 0: irrig = 0
    irrig = min(irrig, LIMITS["max_ml_per_call"])

    # finestra oraria
    if not _within_window(now, LIMITS["irrig_start"], LIMITS["irrig_end"]):
        irrig = 0
        notes += " | fuori finestra"

    # tetti orario/giorno
    hour_sum, last_ts = _sum_irrig(zone, 1)
    day_sum, _ = _sum_irrig(zone, 24)
    irrig = max(0, min(irrig, max(0, LIMITS["max_ml_per_hour"] - hour_sum)))
    irrig = max(0, min(irrig, max(0, LIMITS["max_ml_per_day"]  - day_sum)))

    # cooldown minimo
    if last_ts:
        try:
            last_dt = dt.datetime.fromisoformat(last_ts.replace("Z",""))
            delta_min = (now - last_dt).total_seconds()/60.0
            if delta_min < LIMITS["min_interval_min"]:
                irrig = 0
                notes += " | cooldown attivo"
        except: pass

    plan["irrigate_ml"] = irrig
    plan["notes"] = notes.strip(" |")

    # luci: cap ore/giorno
    L = plan.get("lights") or {}
    if L.get("on"):
        used = _lights_used_today(zone)
        remaining = max(0, LIMITS["lights_max_hours_per_day"] - used)
        L["hours"] = max(0, min(int(L.get("hours",0) or 0), remaining))
        plan["lights"] = L
    return plan

# ===== Open-Meteo =====
def fetch_open_meteo(lat: float, lon: float, days: int = 5) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon, "forecast_days": days,
        "hourly": "temperature_2m,relative_humidity_2m,et0_fao_evapotranspiration,vapor_pressure_deficit",
        "timezone": "auto"
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    db_exec("INSERT INTO meteo_cache(source,lat,lon,raw_json) VALUES(?,?,?,?)",
            ("open-meteo", lat, lon, json.dumps(data)))
    return data

# ===== FastAPI =====
@app.on_event("startup")
def on_startup():
    _mqtt_connect_with_retry()

@app.get("/health")
def health():
    return {"ok": True, "zones": ZONES, "mqtt": _mqtt_ready, "limits": LIMITS}

@app.post("/publish")
def publish(req: PubReq):
    global _mqtt_ready
    if not _mqtt_ready:
        _mqtt_connect_with_retry(max_tries=5, delay_s=0.5)
    payload = req.payload if isinstance(req.payload, (str, bytes)) else json.dumps(req.payload)
    info = mqttc.publish(req.topic, payload, qos=req.qos, retain=req.retain)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise HTTPException(500, f"MQTT publish failed: rc={info.rc}")
    return {"published": True, "topic": req.topic}

@app.get("/config/limits")
def get_limits():
    return LIMITS

@app.post("/config/limits")
def set_limits(upd: dict):
    global LIMITS
    LIMITS.update({k:v for k,v in upd.items() if k in DEFAULT_LIMITS})
    db_exec("INSERT OR REPLACE INTO config(key, value) VALUES('limits', ?)", (json.dumps(LIMITS),))
    return LIMITS

@app.post("/meteo/update")
def meteo_update():
    if not GPS_LAT or not GPS_LON:
        raise HTTPException(400, "GPS_LAT/LON non impostati")
    data = fetch_open_meteo(GPS_LAT, GPS_LON, days=5)
    return {"saved": True, "hours": len(data.get("hourly", {}).get("time", []))}

@app.post("/ai/ping")
def ai_ping(req: AiPingReq):
    if not client:
        raise HTTPException(500, "ANTHROPIC_API_KEY mancante")
    msg = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=128, temperature=0,
        system="Sei un test agent. Rispondi in una riga, conciso.",
        messages=[{"role":"user","content": req.q}]
    )
    text = msg.content[0].text if msg.content else ""
    return {"answer": text, "model": CLAUDE_MODEL}

@app.post("/ai/plan")
def ai_plan(req: PlanReq):
    if not client:
        raise HTTPException(500, "ANTHROPIC_API_KEY mancante")
    user_payload = {
        "zone": req.zone, "crop": req.crop,
        "sensors": {
            "soil_cap": req.soil_cap, "soil_wc": req.soil_wc,
            "air_temp_c": req.air_temp_c, "air_rh": req.air_rh,
            "vpd_kpa": req.vpd_kpa, "light_lux": req.light_lux,
        },
        "environment": {"eto_mm": req.eto_mm, "forecast": req.forecast},
        "constraints": req.constraints or {}
    }
    system = ("Sei il planner di una serra.\n"
              "Rispondi SOLO con JSON valido:\n"
              "{ \"zone\": str, \"irrigate_ml\": int, \"soil_target\": [min,max],"
              "  \"lights\": {\"on\": bool, \"hours\": int}, \"notes\": str }\n"
              "Se i dati sono incerti: irrigate_ml=0 e spiega in notes.")
    msg = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=400, temperature=0,
        system=system, messages=[{"role":"user","content": json.dumps(user_payload)}]
    )
    text = msg.content[0].text if msg.content else "{}"
    plan = _extract_json(text)

    # --- SAFETY GUARDS ---
    plan = _apply_safety(plan, req.zone)

    # publish e log
    topic = f"serra/setpoints/{plan.get('zone', req.zone)}"
    mqttc.publish(topic, json.dumps(plan), qos=0, retain=True) if req.publish else None
    db_exec("INSERT INTO setpoints(zone, plan_json) VALUES(?,?)", (plan.get("zone", req.zone), json.dumps(plan)))
    db_exec("INSERT INTO decision_log(zone, model, notes) VALUES(?,?,?)",
            (plan.get("zone", req.zone), CLAUDE_MODEL, plan.get("notes","")))
    plan["_published_to"] = topic if req.publish else None
    return plan

# ===== Planner loop (thread) =====
_stop = threading.Event()

def _plan_once_for_zone(zone: str, crop: str = "basilico"):
    sens = LAST.get(zone, {})
    req = PlanReq(
        zone=zone, crop=crop,
        soil_cap=sens.get("soil_cap"), soil_wc=sens.get("soil_wc"),
        air_temp_c=sens.get("air_temp_c"), air_rh=sens.get("air_rh"),
        vpd_kpa=sens.get("vpd_kpa"), light_lux=sens.get("light_lux"),
        eto_mm=sens.get("eto_mm"), forecast=None, publish=True
    )
    try:
        ai_plan(req)
    except Exception as e:
        print(f"[PLAN ERROR] zone={zone}: {e}")

def _planner_loop():
    print(f"[PLANNER] loop ogni {PLAN_INTERVAL_MIN} min, zones={ZONES}")
    while not _stop.is_set():
        for z in ZONES:
            _plan_once_for_zone(z)
        _stop.wait(PLAN_INTERVAL_MIN * 60)

def start_planner_thread():
    t = threading.Thread(target=_planner_loop, daemon=True)
    t.start()

# ===== Startup =====
@app.on_event("startup")
def _startup_all():
    _mqtt_connect_with_retry()
    start_planner_thread()

from weather_endpoints import router as weather_router
app.include_router(weather_router)

from weather_endpoints import router as weather_router
app.include_router(weather_router)
from fastapi import Request
from fastapi.responses import JSONResponse

@app.middleware("http")
async def catch_exceptions_middleware(request: Request, call_next):
    try:
        resp = await call_next(request)
        return resp
    except Exception as e:
        # Log su stdout per journalctl
        print("[GLOBAL ERROR]", repr(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
