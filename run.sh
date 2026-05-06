#!/usr/bin/env bash
# run.sh — notebooklm_automator.py'yi CLI olarak çalıştırmak için kestirme.
# Örnek: ./run.sh "Kahve nasıl yapılır?" --profile-dir chrome_profiles/abc123
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "HATA: .venv yok. Önce ./setup.sh çalıştır."
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

exec python notebooklm_automator.py "$@"
