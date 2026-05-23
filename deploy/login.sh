#!/usr/bin/env bash
# deploy/login.sh — Mac'ten profil login + otomatik sunucu sync
#
# Akış:
#   1) Sunucudan profil listesini oku, hangileri ⚪ (login bekliyor) gör
#   2) Listeyi numaralandır, user tek tek seçer ya da "a" ile hepsi
#   3) Seçilen her profil için: Chrome aç → user login olsun → kapatsın →
#      auth.json'u sunucuya rsync et → smoke_test ile doğrula
#   4) Sonunda özet: kaç başarılı, kaç fail
#
# Kullanım:
#   ./deploy/login.sh                # interaktif menü
#   ./deploy/login.sh --all          # tüm ⚪'lara peşpeşe
#   ./deploy/login.sh <profile_id>   # spesifik bir profil
#
# Config (env var override edilebilir):
#   NLM_SSH_KEY     — SSH key path (default: ~/Downloads/dev-internal-00.pem)
#   NLM_HOST        — ubuntu@host (default: prod EC2)
#   NLM_REMOTE_REPO — sunucudaki repo path (default: /home/ubuntu/notebooklm-cinematic-studio)

set -euo pipefail

# ---- Config ----------------------------------------------------------------
SSH_KEY="${NLM_SSH_KEY:-$HOME/Downloads/dev-internal-00.pem}"
SSH_HOST="${NLM_HOST:-ubuntu@ec2-35-156-158-86.eu-central-1.compute.amazonaws.com}"
REMOTE_REPO="${NLM_REMOTE_REPO:-/home/ubuntu/notebooklm-cinematic-studio}"

# ---- Pre-flight ------------------------------------------------------------
cd "$(dirname "$0")/.."

if [ ! -f "$SSH_KEY" ]; then
  echo "✗ SSH key bulunamadı: $SSH_KEY"
  echo "  Override için: export NLM_SSH_KEY=/path/to/key.pem"
  exit 1
fi
chmod 600 "$SSH_KEY" 2>/dev/null || true

if [ ! -d .venv ]; then
  echo "✗ .venv yok. Önce ./setup.sh çalıştır."
  exit 1
fi

ssh_run() { ssh -i "$SSH_KEY" -o ConnectTimeout=10 "$SSH_HOST" "$@"; }

# ---- Profilleri sunucudan oku ---------------------------------------------
echo "==> Sunucudan profil listesi alınıyor..."
PROFILES_JSON=$(ssh_run "cat $REMOTE_REPO/data/profiles.json")

# Profilleri parse et: id|name|env|initialized
# NOT: mapfile bash 4+ olduğu için (macOS'ta bash 3.2) kullanmıyoruz.
# JSON'u stdin'den geçiriyoruz ki single-quote/special char güvenli olsun.
PROFILE_LINES=()
while IFS= read -r line; do
  [ -n "$line" ] && PROFILE_LINES+=("$line")
done < <(printf '%s' "$PROFILES_JSON" | python3 -c "
import json, sys
profiles = json.load(sys.stdin)
for p in profiles:
    pid = p.get('id', '')
    name = p.get('name', '?').replace('|', '/')
    env = p.get('environment', 'prod')
    init = 'Y' if p.get('initialized', False) else 'N'
    print(f'{pid}|{name}|{env}|{init}')
")

if [ ${#PROFILE_LINES[@]} -eq 0 ]; then
  echo "✗ Hiç profil yok."
  exit 1
fi

# ---- Seçim mantığı ---------------------------------------------------------
MODE="${1:-}"
SELECTED_IDS=()

if [ "$MODE" = "--all" ]; then
  # Tüm ⚪'lar
  for line in "${PROFILE_LINES[@]}"; do
    IFS='|' read -r pid name env init <<<"$line"
    if [ "$init" = "N" ]; then
      SELECTED_IDS+=("$pid")
    fi
  done
  if [ ${#SELECTED_IDS[@]} -eq 0 ]; then
    echo "✓ Login gereken profil yok — hepsi 🟢."
    exit 0
  fi
elif [ -n "$MODE" ]; then
  # Spesifik id veya isim match
  for line in "${PROFILE_LINES[@]}"; do
    IFS='|' read -r pid name env init <<<"$line"
    if [ "$pid" = "$MODE" ] || [ "$name" = "$MODE" ]; then
      SELECTED_IDS+=("$pid")
      break
    fi
  done
  if [ ${#SELECTED_IDS[@]} -eq 0 ]; then
    echo "✗ Profil bulunamadı: $MODE"
    exit 1
  fi
else
  # İnteraktif menü
  echo
  echo "Profiller:"
  i=0
  for line in "${PROFILE_LINES[@]}"; do
    IFS='|' read -r pid name env init <<<"$line"
    i=$((i+1))
    marker="⚪"
    [ "$init" = "Y" ] && marker="🟢"
    printf "  %2d) %s %-30s [%s]\n" "$i" "$marker" "$name" "$env"
  done
  echo
  echo "Seçim:  '1 3 5' (boşluklarla), 'a' (tüm ⚪), 'q' (çık)"
  read -rp "→ " choice

  case "$choice" in
    q|Q) echo "Çıkıldı."; exit 0 ;;
    a|A)
      for line in "${PROFILE_LINES[@]}"; do
        IFS='|' read -r pid name env init <<<"$line"
        [ "$init" = "N" ] && SELECTED_IDS+=("$pid")
      done
      ;;
    *)
      for n in $choice; do
        idx=$((n-1))
        if [ "$idx" -lt 0 ] || [ "$idx" -ge ${#PROFILE_LINES[@]} ]; then
          echo "✗ Geçersiz numara: $n"
          continue
        fi
        IFS='|' read -r pid name env init <<<"${PROFILE_LINES[$idx]}"
        SELECTED_IDS+=("$pid")
      done
      ;;
  esac
fi

if [ ${#SELECTED_IDS[@]} -eq 0 ]; then
  echo "✗ Seçilen profil yok."
  exit 1
fi

# Lookup helper
profile_name() {
  for line in "${PROFILE_LINES[@]}"; do
    IFS='|' read -r pid name env init <<<"$line"
    if [ "$pid" = "$1" ]; then echo "$name [$env]"; return; fi
  done
  echo "$1"
}

# ---- Login akışı (her profil için) ----------------------------------------
SUCCESS=()
FAILED=()
TOTAL=${#SELECTED_IDS[@]}
CURRENT=0

for pid in "${SELECTED_IDS[@]}"; do
  CURRENT=$((CURRENT+1))
  pname=$(profile_name "$pid")
  echo
  echo "═══════════════════════════════════════════════════════════════════"
  echo "  [$CURRENT/$TOTAL] Login: $pname"
  echo "  Profile id: $pid"
  echo "═══════════════════════════════════════════════════════════════════"

  # Lokal profile dir'i temizle (eski state varsa)
  local_dir="chrome_profiles/$pid"
  if [ -d "$local_dir" ]; then
    echo "→ Eski lokal state temizleniyor: $local_dir"
    rm -rf "$local_dir"
  fi
  mkdir -p "$local_dir"

  # Init başlat (foreground — user Chrome'da login olur, kapatır)
  echo "→ Chromium açılıyor. Login ol → notebooklm.google.com ana sayfaya ulaş → pencereyi KAPAT."
  echo "  (Touch ID/passkey çalışmaz — email + şifre kullan)"
  echo
  if ! .venv/bin/python notebooklm_automator.py --init \
        --profile-dir "$local_dir" --authuser 0; then
    echo "✗ Init script hata verdi."
    FAILED+=("$pname (init fail)")
    continue
  fi

  # auth.json yazıldı mı?
  auth_file="$local_dir/auth.json"
  if [ ! -f "$auth_file" ]; then
    echo "✗ auth.json oluşmadı — login tamamlanmadı veya notebooklm.google.com'a ulaşılmadı."
    FAILED+=("$pname (auth.json yok)")
    continue
  fi
  size=$(stat -f%z "$auth_file" 2>/dev/null || stat -c%s "$auth_file")
  echo "→ auth.json hazır ($size bytes)"

  # rsync to server
  # Hedef klasör yoksa rsync fail eder (eski profil → sunucuda hiç login
  # olmamış). Önce mkdir -p ile garantile.
  echo "→ Sunucuya rsync..."
  if ! ssh_run "mkdir -p $REMOTE_REPO/$local_dir"; then
    echo "✗ remote mkdir fail"
    FAILED+=("$pname (mkdir fail)")
    continue
  fi
  if ! rsync -az -e "ssh -i $SSH_KEY -o ConnectTimeout=10" \
        "$auth_file" \
        "$SSH_HOST:$REMOTE_REPO/$local_dir/auth.json"; then
    echo "✗ rsync fail"
    FAILED+=("$pname (rsync fail)")
    continue
  fi

  # smoke_test sunucuda
  echo "→ Sunucuda smoke_test..."
  smoke_out=$(ssh_run "cd $REMOTE_REPO && .venv/bin/python -c \"
from notebooklm_client import smoke_test
ok, msg = smoke_test('$pid')
print(('OK: ' if ok else 'FAIL: ') + msg[:200])
\"" 2>&1)
  echo "  $smoke_out"

  if [[ "$smoke_out" == OK:* ]]; then
    # initialized=True yap
    ssh_run "cd $REMOTE_REPO && .venv/bin/python -c \"
import json
from pathlib import Path
p = Path('data/profiles.json')
profiles = json.loads(p.read_text())
for x in profiles:
    if x.get('id') == '$pid':
        x['initialized'] = True
        break
p.write_text(json.dumps(profiles, indent=2))
print('initialized=True set on server')
\""
    SUCCESS+=("$pname")
  else
    FAILED+=("$pname (smoke fail)")
  fi
done

# ---- Özet -----------------------------------------------------------------
echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  Özet"
echo "═══════════════════════════════════════════════════════════════════"
if [ ${#SUCCESS[@]} -gt 0 ]; then
  echo "✅ Başarılı (${#SUCCESS[@]}):"
  printf "    • %s\n" "${SUCCESS[@]}"
fi
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "❌ Başarısız (${#FAILED[@]}):"
  printf "    • %s\n" "${FAILED[@]}"
  exit 1
fi
echo
