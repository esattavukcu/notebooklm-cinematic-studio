#!/usr/bin/env bash
# deploy/deploy.sh — sunucuda çalışan post-pull deploy script'i.
#
# Akış:
#   1) Mevcut HEAD'i hatırla (rollback için)
#   2) git pull --ff-only
#   3) Önceki deploy edilen HEAD ile karşılaştır → değişen dosyalara göre
#      sadece gerekli adımları çalıştır (pip install, install-vnc, restart)
#   4) Başarılıysa yeni HEAD'i .last-deployed-head'e yaz
#   5) Hata olursa otomatik rollback (git reset + systemctl restart)
#
# Kullanım (sunucuda):
#   cd ~/notebooklm-cinematic-studio
#   bash deploy/deploy.sh
#
# İlk deploy (.last-deployed-head yoksa): tüm adımları çalıştırır (full).

set -euo pipefail

cd "$(dirname "$0")/.."

LAST_DEPLOY_FILE=".last-deployed-head"
PREV_HEAD=$(git rev-parse HEAD)

rollback() {
  echo ""
  echo "✗ Deploy failed — rolling back to $PREV_HEAD"
  git reset --hard "$PREV_HEAD" || true
  sudo systemctl restart notebooklm 2>/dev/null || true
  echo "✗ Rolled back. Investigate before retrying."
  exit 1
}
trap rollback ERR

echo "==> Mevcut HEAD: $PREV_HEAD"
echo "==> git pull --ff-only..."
git fetch --quiet
git pull --ff-only
NEW_HEAD=$(git rev-parse HEAD)

if [ "$PREV_HEAD" = "$NEW_HEAD" ] && [ -f "$LAST_DEPLOY_FILE" ]; then
  if [ "$(cat "$LAST_DEPLOY_FILE")" = "$NEW_HEAD" ]; then
    echo "✓ Already at $NEW_HEAD, no changes to deploy."
    trap - ERR
    exit 0
  fi
fi

# Hangi dosyalar değişti? İlk deploy ise ALL, sonrasında diff.
if [ -f "$LAST_DEPLOY_FILE" ]; then
  OLD_HEAD=$(cat "$LAST_DEPLOY_FILE")
  CHANGED=$(git diff --name-only "$OLD_HEAD" "$NEW_HEAD" || echo "ALL")
  echo "==> Değişen dosyalar ($OLD_HEAD..$NEW_HEAD):"
  echo "$CHANGED" | sed 's/^/    /'
else
  CHANGED="ALL"
  echo "==> İlk deploy — tüm adımlar çalıştırılacak."
fi

needs() {
  [ "$CHANGED" = "ALL" ] || echo "$CHANGED" | grep -qE "$1"
}

if needs '^requirements\.txt$'; then
  echo "==> pip install -r requirements.txt..."
  .venv/bin/pip install -r requirements.txt
fi

if needs '^deploy/install-vnc\.sh$'; then
  echo "==> bash deploy/install-vnc.sh (VNC stack rebuild)..."
  bash deploy/install-vnc.sh
fi

if needs '^deploy/notebooklm\.service$'; then
  echo "==> systemd unit değişti — daemon-reload..."
  sudo cp deploy/notebooklm.service /etc/systemd/system/notebooklm.service
  sudo systemctl daemon-reload
fi

# Python dosyaları değiştiyse Streamlit restart şart
if needs '\.py$|^deploy/notebooklm\.service$'; then
  echo "==> sudo systemctl restart notebooklm..."
  sudo systemctl restart notebooklm
  sleep 2
  if ! systemctl is-active --quiet notebooklm; then
    echo "✗ notebooklm.service active değil:"
    sudo systemctl status notebooklm --no-pager | head -20
    false  # trigger rollback
  fi
fi

# Başarılı — yeni HEAD'i kaydet
echo "$NEW_HEAD" > "$LAST_DEPLOY_FILE"
trap - ERR

echo ""
echo "✓ Deployed: $PREV_HEAD..$NEW_HEAD"
echo "  Services: $(systemctl is-active notebooklm xvfb x11vnc novnc | tr '\n' ' ')"
