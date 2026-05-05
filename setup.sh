#!/usr/bin/env bash
# Tek seferlik kurulum
set -e

cd "$(dirname "$0")"

echo "==> Python venv oluşturuluyor..."
python3 -m venv .venv
source .venv/bin/activate

echo "==> Bağımlılıklar yükleniyor..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Playwright Chromium indiriliyor..."
playwright install chromium

if [ ! -f .env ]; then
  cp .env.example .env
fi

mkdir -p chrome_profiles data data/logs

echo ""
echo "✓ Kurulum tamam."
echo ""
echo "Web UI'ı başlatmak için:"
echo "  ./app.sh"
echo ""
echo "Tek metin (CLI) için:"
echo "  ./run.sh \"örümcekler nasıl yürür\""
