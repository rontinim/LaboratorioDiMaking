#!/bin/bash
set -euo pipefail
cd /opt/serra

LAT="${LAT:-45.4642}"
LON="${LON:-9.1900}"
LOC="${LOC:-Milano}"

# 1) scarica/aggiorna meteo in tabella weather (via weather_once.py)
./venv/bin/python weather_once.py "$LAT" "$LON" "$LOC"

# 2) aggiorna tabella weather_day a partire da v_weather_latest
./venv/bin/python weather_day_update.py
