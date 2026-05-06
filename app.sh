#!/usr/bin/env bash
# app.sh — Streamlit web UI'yi başlatır.
# Tarayıcıda http://localhost:8501 otomatik açılır.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "HATA: .venv yok. Önce ./setup.sh çalıştır."
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

mkdir -p data/logs/screenshots chrome_profiles data/downloads

# Streamlit'i başlat. headless=false → tarayıcı otomatik açılsın
exec streamlit run app.py \
  --server.address=127.0.0.1 \
  --server.port=8501 \
  --browser.gatherUsageStats=false \
  --server.headless=false
