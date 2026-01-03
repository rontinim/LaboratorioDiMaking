#!/usr/bin/env bash
set -euo pipefail

cd /opt/serra

LAT="${LAT:-45.4642}"
LON="${LON:-9.1900}"
LOC="${LOC:-Milano}"

# log su file
exec >> /var/log/serra-weather.log 2>&1

echo "=== WEATHER @ $(date -Is) LAT=$LAT LON=$LON LOC=$LOC ==="
LAT="$LAT" LON="$LON" LOC="$LOC" ./weather_pipeline.sh
echo
