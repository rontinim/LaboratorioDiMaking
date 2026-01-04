from fastapi import APIRouter
import os, sqlite3, json

router = APIRouter()
DB_PATH = os.environ.get("SERRA_DB", "/opt/serra/serra.db")

@router.get("/weather/latest")
def weather_latest():
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT ts, json FROM weather ORDER BY ts DESC LIMIT 1")
    row = cur.fetchone(); con.close()
    if not row:
        return {"ok": False, "error": "no_weather"}
    return {"ok": True, "ts": row[0], "data": json.loads(row[1])}

@router.get("/weather/eto")
def weather_eto():
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT json FROM weather ORDER BY ts DESC LIMIT 1")
    row = cur.fetchone(); con.close()
    if not row: return {"ok": False, "eto_mm": None}
    data = json.loads(row[0])
    eto = None
    try:
        eto = float(data["daily"]["et0_fao_evapotranspiration"][0])
    except Exception:
        pass
    return {"ok": True, "eto_mm": eto}
