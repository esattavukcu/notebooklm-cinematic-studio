#!/usr/bin/env bash
# Çift tıklayınca ya da terminalden çalıştırınca: input.txt'i NotebookLM'e gönderir
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
python notebooklm_automator.py "$@"
