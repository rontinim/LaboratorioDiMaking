CREATE TABLE weather (
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        lat REAL,
        lon REAL,
        source TEXT,
        json TEXT NOT NULL
    );
CREATE INDEX idx_weather_ts ON weather(ts DESC);
CREATE TABLE setpoints (
  zone TEXT NOT NULL,
  ts   DATETIME DEFAULT CURRENT_TIMESTAMP,
  irrigate_ml INTEGER DEFAULT 0,
  soil_tgt_min REAL,
  soil_tgt_max REAL,
  lights_on INTEGER DEFAULT 0,
  lights_hours INTEGER DEFAULT 0,
  source TEXT,
  notes TEXT,
  published_topic TEXT
, plan_json TEXT);
CREATE INDEX idx_sp_zone_ts ON setpoints(zone, ts DESC);
CREATE TABLE config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE decision_log (
  ts              DATETIME DEFAULT CURRENT_TIMESTAMP,
  zone            TEXT,
  plan_json       TEXT,         -- JSON completo ritornato dal planner (dopo clamp/normalize)
  inputs_json     TEXT,         -- payload richiesta /ai/plan (sensori, eto, ecc.)
  limits_json     TEXT,         -- limiti attivi al momento del plan
  published_topic TEXT,         -- es. serra/setpoints/bed1
  eto_mm          REAL,         -- comodo se il bridge lo logga separatamente
  forecast_json   TEXT,         -- eventuale forecast passato al planner
  error           TEXT,         -- eventuale msg di errore non-bloccante
  duration_ms     REAL          -- tempo di esecuzione del plan
, result_json TEXT, request_json TEXT, topic TEXT, model TEXT, notes TEXT, request_id TEXT, provider TEXT, api_latency_ms REAL, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, cost_usd REAL);
CREATE INDEX idx_decision_zone_ts ON decision_log(zone, ts DESC);
CREATE TRIGGER trg_sp_dedupe
BEFORE INSERT ON setpoints
WHEN NEW.source IS NULL AND EXISTS (
  SELECT 1 FROM setpoints s
  WHERE s.zone = NEW.zone AND s.ts = NEW.ts AND s.source = 'mqtt'
)
BEGIN
  SELECT RAISE(IGNORE);
END;
CREATE TRIGGER trg_decision_dedupe
BEFORE INSERT ON decision_log
WHEN NEW.plan_json IS NULL AND EXISTS (
  SELECT 1 FROM decision_log d
  WHERE d.zone = NEW.zone AND d.ts = NEW.ts AND d.plan_json IS NOT NULL
)
BEGIN
  SELECT RAISE(IGNORE);
END;
CREATE VIEW setpoints_last AS
WITH ranked AS (
  SELECT s.*,
         ROW_NUMBER() OVER (
           PARTITION BY zone
           ORDER BY ts DESC,
                    CASE WHEN COALESCE(source,'')='mqtt' THEN 0 ELSE 1 END,
                    rowid DESC
         ) AS rn
  FROM setpoints s
)
SELECT * FROM ranked WHERE rn=1
/* setpoints_last(zone,ts,irrigate_ml,soil_tgt_min,soil_tgt_max,lights_on,lights_hours,source,notes,published_topic,plan_json,rn) */;
CREATE TABLE crops (
  id               INTEGER PRIMARY KEY,
  name             TEXT UNIQUE NOT NULL,      -- es: basilico, rosmarino
  soil_target_min  REAL,                      -- % o frazione (coerente ai tuoi setpoint)
  soil_target_max  REAL,
  photoperiod_h    REAL,                      -- ore luce desiderate/giorno (es 16)
  lights_night_only INTEGER DEFAULT 1,        -- 1 = fai supplemento solo fuori ore di sole
  notes            TEXT
, crop TEXT);
CREATE TABLE beds (
  zone       TEXT PRIMARY KEY,                -- es: bed1, bed2
  crop_id    INTEGER REFERENCES crops(id),
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE nfc_tags (
  uid     TEXT PRIMARY KEY,                   -- UID del tag (es in esadecimale)
  crop_id INTEGER REFERENCES crops(id),
  label   TEXT
);
CREATE TRIGGER trg_beds_updated
AFTER UPDATE ON beds
BEGIN
  UPDATE beds SET updated_at=CURRENT_TIMESTAMP WHERE zone=NEW.zone;
END;
CREATE VIEW v_bed_config AS
SELECT b.zone,
       c.name AS crop,
       c.soil_target_min,
       c.soil_target_max,
       c.photoperiod_h,
       c.lights_night_only
FROM beds b
JOIN crops c ON c.id=b.crop_id
/* v_bed_config(zone,crop,soil_target_min,soil_target_max,photoperiod_h,lights_night_only) */;
CREATE TABLE zones(
  zone TEXT PRIMARY KEY,
  description TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT
);
CREATE TABLE zone_assignment(
  id INTEGER PRIMARY KEY,
  zone TEXT NOT NULL REFERENCES zones(zone),
  crop TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN('nfc','manual','api')),
  tag_uid TEXT,
  assigned_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_zone_assignment_zone_time ON zone_assignment(zone,assigned_at DESC);
CREATE TABLE devices_status(
  zone TEXT PRIMARY KEY REFERENCES zones(zone),
  esp_mac TEXT,
  mqtt_client_id TEXT,
  ip_addr TEXT,
  fw_version TEXT,
  status TEXT CHECK(status IN('online','offline')) DEFAULT 'offline',
  last_seen TEXT
);
CREATE TABLE weather_obs(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  provider TEXT,
  loc_name TEXT,
  sunrise TEXT,
  sunset TEXT,
  temp_c REAL,
  feels_like_c REAL,
  dewpoint_c REAL,
  humidity_pct REAL,
  pressure_hpa REAL,
  wind_mps REAL,
  wind_gust_mps REAL,
  wind_deg REAL,
  cloud_pct REAL,
  solar_w_m2 REAL,
  precip_now_mmph REAL,
  precip_1h_mm REAL,
  precip_24h_mm REAL,
  precip_prob_pct REAL,
  is_raining INTEGER,
  eto_mm REAL,
  condition TEXT
);
CREATE TABLE setpoint_history(
  id INTEGER PRIMARY KEY,
  ts TEXT DEFAULT (datetime('now')),
  zone TEXT,
  publish_topic TEXT,
  payload_json TEXT,
  model TEXT,
  ok INTEGER,
  error TEXT
);
CREATE INDEX idx_setpoint_hist_zone_ts ON setpoint_history(zone,ts DESC);
CREATE TABLE commands_log(
  id INTEGER PRIMARY KEY,
  ts TEXT DEFAULT (datetime('now')),
  zone TEXT,
  topic TEXT,
  payload_json TEXT,
  ack_ts TEXT,
  ack_payload TEXT,
  ok INTEGER,
  error TEXT
);
CREATE TABLE irrigation_events(
  id INTEGER PRIMARY KEY,
  ts TEXT DEFAULT (datetime('now')),
  zone TEXT,
  ml INTEGER,
  seconds INTEGER,
  result TEXT,
  reason TEXT
);
CREATE INDEX idx_irrigation_zone_ts ON irrigation_events(zone,ts DESC);
CREATE TABLE alerts(
  id INTEGER PRIMARY KEY,
  ts TEXT DEFAULT (datetime('now')),
  zone TEXT,
  type TEXT,
  severity TEXT,
  message TEXT,
  resolved_at TEXT
);
CREATE INDEX idx_alerts_zone_ts ON alerts(zone,ts DESC);
CREATE TABLE settings(
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE VIEW v_weather_latest AS SELECT * FROM weather_obs ORDER BY ts DESC LIMIT 1
/* v_weather_latest(id,ts,provider,loc_name,sunrise,sunset,temp_c,feels_like_c,dewpoint_c,humidity_pct,pressure_hpa,wind_mps,wind_gust_mps,wind_deg,cloud_pct,solar_w_m2,precip_now_mmph,precip_1h_mm,precip_24h_mm,precip_prob_pct,is_raining,eto_mm,condition) */;
CREATE TABLE light_events (
  id       INTEGER PRIMARY KEY,
  ts       TEXT DEFAULT (datetime('now')),
  zone     TEXT REFERENCES zones(zone),
  light_on INTEGER,            -- 0/1
  until_ts TEXT,
  reason   TEXT,
  result   TEXT
);
CREATE INDEX idx_light_zone_ts ON light_events(zone, ts DESC);
CREATE VIEW v_zone_current_crop AS
WITH last_ass AS (
  SELECT zone, MAX(assigned_at) AS assigned_at
  FROM zone_assignment
  GROUP BY zone
)
SELECT za.zone, za.crop, za.source, za.tag_uid, za.assigned_at
FROM zone_assignment za
JOIN last_ass la ON la.zone = za.zone AND la.assigned_at = za.assigned_at
/* v_zone_current_crop(zone,crop,source,tag_uid,assigned_at) */;
CREATE INDEX idx_commands_zone_ts ON commands_log(zone, ts DESC);
CREATE VIEW v_setpoints_last_normalized AS
SELECT
  zone,
  ts,
  irrigate_ml,
  soil_tgt_min  AS soil_target_min,
  soil_tgt_max  AS soil_target_max,
  lights_on,
  lights_hours,
  source,
  notes,
  published_topic AS published_to,
  plan_json
FROM setpoints_last
/* v_setpoints_last_normalized(zone,ts,irrigate_ml,soil_target_min,soil_target_max,lights_on,lights_hours,source,notes,published_to,plan_json) */;
CREATE UNIQUE INDEX idx_crops_crop ON crops(crop);
CREATE TABLE telemetry(
  id INTEGER PRIMARY KEY,
  ts   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
  zone TEXT NOT NULL,
  soil_cap REAL,
  soil_temp_c REAL,
  air_temp_c REAL,
  air_rh REAL,
  light_lux REAL,
  vbat REAL,
  rssi INTEGER,
  soil_mv INTEGER, soil_raw INTEGER, weight_g REAL,
  relay_led INTEGER, relay_pump INTEGER,
  soil1_raw INTEGER, soil1_mv INTEGER, soil1_pct REAL,
  soil2_raw INTEGER, soil2_mv INTEGER, soil2_pct REAL
);
CREATE INDEX idx_telemetry_zone_ts ON telemetry(zone, ts DESC);
CREATE INDEX idx_telemetry_weight  ON telemetry(weight_g);
CREATE VIEW telemetry_last AS
WITH last AS (SELECT zone, MAX(ts) ts FROM telemetry GROUP BY zone)
SELECT t.* FROM telemetry t JOIN last l USING(zone,ts)
/* telemetry_last(id,ts,zone,soil_cap,soil_temp_c,air_temp_c,air_rh,light_lux,vbat,rssi,soil_mv,soil_raw,weight_g,relay_led,relay_pump,soil1_raw,soil1_mv,soil1_pct,soil2_raw,soil2_mv,soil2_pct) */;
CREATE VIEW v_telemetry_last_by_zone AS
WITH last AS (SELECT zone, MAX(ts) ts FROM telemetry GROUP BY zone)
SELECT t.ts, t.zone, t.weight_g, t.soil1_pct, t.soil2_pct, t.soil_mv, t.soil_raw
FROM telemetry t JOIN last l USING(zone,ts)
/* v_telemetry_last_by_zone(ts,zone,weight_g,soil1_pct,soil2_pct,soil_mv,soil_raw) */;
CREATE TABLE weather_day (
  date            TEXT PRIMARY KEY,   -- "2025-12-01"
  sunrise         TEXT NOT NULL,      -- "07:34"
  sunset          TEXT NOT NULL,      -- "16:49"
  day_length_min  INTEGER NOT NULL    -- lunghezza in minuti
);
CREATE TABLE crop_profile (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  name                TEXT UNIQUE NOT NULL,   -- "basilico", "prezzemolo"
  soil_min            REAL NOT NULL,          -- umidità % minima
  soil_max            REAL NOT NULL,          -- umidità % massima
  light_hours_indoor  REAL NOT NULL,          -- ore consigliate in interno (solo LED)
  light_hours_outdoor REAL NOT NULL,          -- ore minime totali (sole + LED)
  notes               TEXT
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE nfc_tag (
  uid       TEXT PRIMARY KEY,                -- "04:AB:23:FE"
  crop_name TEXT NOT NULL,                   -- "basilico", "prezzemolo"
  bed       TEXT NOT NULL,                   -- "bed2"
  mode      TEXT NOT NULL CHECK (
               mode IN ('indoor','outdoor','manual')
             )                               -- modalità di gestione
);
CREATE TABLE bed_state (
  bed               TEXT PRIMARY KEY,        -- "bed2"
  crop_name         TEXT,
  mode              TEXT,                    -- "indoor","outdoor","manual"
  soil_min          REAL,
  soil_max          REAL,
  light_hours_target REAL,                   -- ore target per OGGI
  motor_position    TEXT,                    -- "up","down","unknown"
  last_update       TEXT                     -- ISO8601: "2025-12-01T15:30:00"
);
CREATE TABLE motor_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  bed       TEXT NOT NULL,
  date      TEXT NOT NULL,               -- YYYY-MM-DD
  event     TEXT NOT NULL CHECK(event IN ('sunrise','sunset')),
  ts        TEXT NOT NULL,               -- ISO8601 now
  revs      REAL NOT NULL,
  dir       TEXT NOT NULL CHECK(dir IN ('up','down')),
  mqtt_topic TEXT NOT NULL,
  mqtt_payload TEXT NOT NULL,
  UNIQUE(bed, date, event)
);
