#!/usr/bin/env bash
set -euo pipefail

project_root=/home/ubuntu/market-data
runtime_root=/home/ubuntu/market-data-runtime/weather

exec "$project_root/.venv/bin/python" "$project_root/04_scripts/weather/refresh_weather_from_sql.py" \
  --source "$runtime_root/current/weather_latest.sql" \
  --processed-root "$runtime_root/processed" \
  --contract-dir "$project_root/01_data/processed/weather" \
  --bootstrap-static-dir "$project_root/01_data/processed/weather" \
  --status-file "$runtime_root/status/weather_refresh_status.json" \
  --promote
