#!/usr/bin/env bash
# Streamlit UI'ını başlat
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
exec streamlit run app.py --server.headless false
