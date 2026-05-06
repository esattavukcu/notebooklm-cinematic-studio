#!/usr/bin/env bash
# setup.sh — venv kurar, dependencies yükler, Playwright Chromium indirir.
# Tek komutluk first-run kurulum scripti. Mustafa: ./setup.sh çalıştırması yeterli.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Python sürümü kontrol ediliyor..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "HATA: python3 bulunamadı. https://www.python.org/downloads/ üzerinden Python 3.10+ kur."
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "    Python $PY_VER bulundu."

echo "==> Virtualenv (.venv) hazırlanıyor..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> pip yükseltiliyor..."
pip install --upgrade pip wheel >/dev/null

echo "==> Bağımlılıklar yükleniyor..."
pip install -r requirements.txt

echo "==> Playwright Chromium indiriliyor (ilk seferde 100-150MB)..."
python -m playwright install chromium

echo "==> Veri klasörleri hazırlanıyor..."
mkdir -p data/logs/screenshots chrome_profiles data/downloads

echo ""
echo "✓ Kurulum tamamlandı."
echo ""
echo "Çalıştırmak için:"
echo "    ./app.sh"
echo ""
echo "Ardından tarayıcıda http://localhost:8501 aç."
