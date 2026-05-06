#!/usr/bin/env bash
# sync-profiles.sh — Lokal'deki chrome_profiles/ klasörünü sunucuya rsync eder.
# Login state (Google cookies) sunucuda olmadığı için her hesap için bu adım
# bir kez yapılır. Cookie'ler ~14 günde bir expire olabilir, ayda 1-2 kez tekrarla.
#
# Kullanım:
#   SERVER=ubuntu@studio.example.com KEY=~/.ssh/notebooklm.pem ./deploy/sync-profiles.sh
#   veya:
#   SERVER=esat@1.2.3.4 ./deploy/sync-profiles.sh
#
# Env vars:
#   SERVER     — kullanıcı@host (zorunlu)
#   KEY        — SSH private key (default: ~/.ssh/notebooklm.pem)
#   REMOTE_DIR — sunucudaki uygulama klasörü (default: ~/notebooklm-cinematic-studio)
#   PROFILES   — virgülle ayrılmış belirli profiller (default: hepsi)

set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

SERVER="${SERVER:-}"
KEY="${KEY:-$HOME/.ssh/notebooklm.pem}"
REMOTE_DIR="${REMOTE_DIR:-notebooklm-cinematic-studio}"
PROFILES="${PROFILES:-}"

if [ -z "$SERVER" ]; then
  echo "HATA: SERVER env var lazım."
  echo "Örnek: SERVER=ubuntu@studio.example.com ./deploy/sync-profiles.sh"
  exit 1
fi

if [ ! -d "chrome_profiles" ]; then
  echo "HATA: chrome_profiles/ klasörü yok. Lokal'de en az bir profil ile login olmuş olman gerek."
  exit 1
fi

SSH_OPTS="-o StrictHostKeyChecking=accept-new"
if [ -f "$KEY" ]; then
  SSH_OPTS="-i $KEY $SSH_OPTS"
fi

echo "════════════════════════════════════════════════════"
echo "  Login state senkronu"
echo "════════════════════════════════════════════════════"
echo "  Hedef sunucu:   $SERVER"
echo "  Remote dizin:   ~/$REMOTE_DIR"
echo "  SSH key:        $KEY"
echo

# Belirli profiller
if [ -n "$PROFILES" ]; then
  IFS=',' read -ra ARR <<< "$PROFILES"
  for p in "${ARR[@]}"; do
    src="chrome_profiles/$p/"
    if [ ! -d "$src" ]; then
      echo "  ⚠ $src yok, atlandı"
      continue
    fi
    echo "==> Senkronlanıyor: $p"
    rsync -avz --mkpath --progress -e "ssh $SSH_OPTS" "$src" "$SERVER:$REMOTE_DIR/chrome_profiles/$p/"
  done
else
  echo "==> Tüm chrome_profiles/ rsync ediliyor..."
  rsync -avz --mkpath --progress -e "ssh $SSH_OPTS" chrome_profiles/ "$SERVER:$REMOTE_DIR/chrome_profiles/"
fi

# .env de gönder (varsa)
if [ -f ".env" ]; then
  echo "==> .env de senkronlanıyor (Azure conn, ADMIN_PASSWORD)..."
  rsync -avz --mkpath -e "ssh $SSH_OPTS" .env "$SERVER:$REMOTE_DIR/.env"
fi

# data/profiles.json de gönder ki sunucu hangi profilin hangi isimle olduğunu bilsin
if [ -f "data/profiles.json" ]; then
  echo "==> data/profiles.json senkronlanıyor..."
  rsync -avz --mkpath -e "ssh $SSH_OPTS" data/profiles.json "$SERVER:$REMOTE_DIR/data/profiles.json"
fi

echo
echo "==> Sunucuda servisi yeniden başlatılıyor..."
ssh $SSH_OPTS "$SERVER" "sudo systemctl restart notebooklm && sudo systemctl status notebooklm --no-pager -n 5"

echo
echo "✓ Senkron tamamlandı."
