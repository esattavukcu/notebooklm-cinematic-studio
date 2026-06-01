#!/usr/bin/env bash
# deploy/push_auth.sh — Tek profile auth.json'u sunucuya gönder (mkdir + rsync + smoke + init flip).
#
# Manuel rsync döngüsünü kısaltır:
#   - Sunucuda hedef dizini yarat (yoksa)
#   - auth.json'u rsync ile gönder
#   - Sunucudan smoke_test çağır
#   - Başarılıysa profiles.json'da initialized=True flip
#
# Kullanım:
#   ./deploy/push_auth.sh <profile_id>
#
# Örnek:
#   ./deploy/push_auth.sh aeda6ea93676

set -euo pipefail

SSH_KEY="${NLM_SSH_KEY:-$HOME/.ssh/dev-internal-00.pem}"
SSH_HOST="${NLM_HOST:-ubuntu@ec2-35-156-158-86.eu-central-1.compute.amazonaws.com}"
REMOTE_REPO="${NLM_REMOTE_REPO:-/home/ubuntu/notebooklm-cinematic-studio}"

PROFILE_ID="${1:-}"
if [ -z "$PROFILE_ID" ]; then
  echo "Kullanım: $0 <profile_id>"
  echo "Örnek: $0 aeda6ea93676"
  exit 1
fi

LOCAL_AUTH="chrome_profiles/$PROFILE_ID/auth.json"
if [ ! -f "$LOCAL_AUTH" ]; then
  echo "✗ Lokal auth.json yok: $LOCAL_AUTH"
  echo "  Önce notebooklm_automator.py --init ile login yap."
  exit 1
fi

REMOTE_DIR="$REMOTE_REPO/chrome_profiles/$PROFILE_ID"
REMOTE_AUTH="$REMOTE_DIR/auth.json"

echo "→ Sunucuda klasör hazırlanıyor: $REMOTE_DIR"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_HOST" "mkdir -p '$REMOTE_DIR'"

echo "→ rsync: $LOCAL_AUTH → $SSH_HOST:$REMOTE_AUTH"
rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  "$LOCAL_AUTH" "$SSH_HOST:$REMOTE_AUTH"

echo "→ Smoke test çalıştırılıyor..."
SMOKE_OUTPUT=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_HOST" \
  "cd $REMOTE_REPO && source .venv/bin/activate && python3 -c \"
from notebooklm_client import smoke_test
ok, msg = smoke_test('$PROFILE_ID')
print('OK' if ok else 'FAIL', '|', msg[:200])
\"" 2>&1 | tail -5)

echo "  $SMOKE_OUTPUT"

if echo "$SMOKE_OUTPUT" | grep -q "^OK"; then
  echo "→ Profile initialized=True yapılıyor..."
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_HOST" \
    "cd $REMOTE_REPO && python3 -c \"
import json
profs = json.load(open('data/profiles.json'))
for p in profs:
    if p['id'] == '$PROFILE_ID':
        p['initialized'] = True
        break
json.dump(profs, open('data/profiles.json','w'), indent=4, ensure_ascii=False)
print('init=True flip OK')
\""
  echo "✅ $PROFILE_ID hazır — dispatch'e girebilir."
else
  echo "⚠ Smoke FAIL — auth.json gönderildi ama session geçerli değil."
  echo "  Login flow'unda 'NotebookLM ana sayfa'ya ulaştığından emin ol."
  exit 1
fi
