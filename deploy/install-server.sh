#!/usr/bin/env bash
# install-server.sh — Ubuntu 22.04 / 24.04 üzerinde NotebookLM Cinematic
# Studio'yu sıfırdan kuran tek-tuş script.
#
# Çalıştır:   curl -sSL https://raw.githubusercontent.com/esattavukcu/notebooklm-cinematic-studio/main/deploy/install-server.sh | bash
# Veya:       git clone ... && cd notebooklm-cinematic-studio && bash deploy/install-server.sh
#
# Gereksinim: Ubuntu 22.04+ veya Debian 12+ (apt-tabanlı), root veya sudo'lu kullanıcı.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/esattavukcu/notebooklm-cinematic-studio.git}"
APP_DIR="${APP_DIR:-/home/$(whoami)/notebooklm-cinematic-studio}"
SERVICE_USER="${SERVICE_USER:-$(whoami)}"

echo "════════════════════════════════════════════════════"
echo "  NotebookLM Cinematic Studio — server installer"
echo "════════════════════════════════════════════════════"
echo "  Repo:        $REPO_URL"
echo "  Hedef dizin: $APP_DIR"
echo "  User:        $SERVICE_USER"
echo

# Sudo kontrolü
if [ "$EUID" -eq 0 ]; then
  SUDO=""
else
  if ! command -v sudo >/dev/null 2>&1; then
    echo "HATA: sudo yok. Root olarak çalıştır veya sudo kur."
    exit 1
  fi
  SUDO="sudo"
fi

echo "==> [1/6] Sistem paketleri (universal — python, git, nginx, certbot)..."
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq \
  python3 python3-pip python3-venv git curl rsync \
  nginx certbot python3-certbot-nginx \
  ca-certificates

# Python 3.10+ kontrolü
PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "    Python $PY_VER tespit edildi."

echo "==> [2/6] Repo klonlanıyor..."
if [ -d "$APP_DIR/.git" ]; then
  echo "    Zaten var, pull ediliyor..."
  cd "$APP_DIR" && git pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

echo "==> [3/6] Shell scriptlere executable bit veriliyor..."
chmod +x setup.sh app.sh run.sh deploy/install-server.sh deploy/sync-profiles.sh 2>/dev/null || true

echo "==> [4/6] Python venv + bağımlılıklar..."
./setup.sh

echo "==> [4.5/6] Playwright Chromium OS dependencies (OS-aware install-deps)..."
# Playwright'ın kendi paketleyicisi — Ubuntu/Debian sürümünden bağımsız doğru
# libleri seçer (libasound2 vs libasound2t64, libatk-bridge2.0-0 vs ..t64).
# Bu adım Ubuntu 22.04, 24.04, 26.04+ hepsinde çalışır.
$SUDO ./.venv/bin/python -m playwright install-deps chromium || {
  echo "    ⚠ playwright install-deps başarısız. Manuel kurulum gerekebilir:"
  echo "      sudo apt install libnss3 libatk-bridge2.0-0* libasound2* libxkbcommon0"
}

echo "==> [5/6] systemd service kuruluyor..."
SERVICE_FILE="/etc/systemd/system/notebooklm.service"
$SUDO tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=NotebookLM Cinematic Studio
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/streamlit run app.py \\
  --server.address=127.0.0.1 \\
  --server.port=8501 \\
  --browser.gatherUsageStats=false \\
  --server.headless=true \\
  --server.fileWatcherType=none
Restart=always
RestartSec=5
Environment=PATH=$APP_DIR/.venv/bin:/usr/bin:/bin
# .env dosyasını otomatik yüklemek için (python-dotenv app.py'den okur).

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable notebooklm
$SUDO systemctl start notebooklm
sleep 2
if $SUDO systemctl is-active --quiet notebooklm; then
  echo "    ✓ Service running"
else
  echo "    ⚠ Service başlamadı, log:"
  $SUDO journalctl -u notebooklm -n 20 --no-pager || true
fi

echo "==> [6/6] Tamamlandı."
echo
echo "═════════════════════════════════════════════════════════════════"
echo "  ✓ Kurulum bitti."
echo
echo "  Sonraki adımlar:"
echo
echo "  1) .env dosyasını oluştur (Azure conn, ADMIN_PASSWORD):"
echo "       cp $APP_DIR/.env.example $APP_DIR/.env"
echo "       nano $APP_DIR/.env"
echo "       sudo systemctl restart notebooklm"
echo
echo "  2) chrome_profiles/'ı lokal'den rsync ile gönder (login state):"
echo "       (lokal makinede)"
echo "       rsync -avz chrome_profiles/ ubuntu@<server>:$APP_DIR/chrome_profiles/"
echo
echo "  3) nginx + HTTPS kur:"
echo "       sudo cp $APP_DIR/deploy/nginx.conf.template /etc/nginx/sites-available/notebooklm"
echo "       sudo nano /etc/nginx/sites-available/notebooklm  (server_name'i değiştir)"
echo "       sudo htpasswd -c /etc/nginx/.htpasswd <kullanici-adi>"
echo "       sudo ln -sf /etc/nginx/sites-available/notebooklm /etc/nginx/sites-enabled/"
echo "       sudo rm -f /etc/nginx/sites-enabled/default"
echo "       sudo nginx -t && sudo systemctl reload nginx"
echo "       sudo certbot --nginx -d studio.example.com"
echo
echo "  Streamlit şu an localhost:8501'de çalışıyor (sadece sunucudan görünür)."
echo "  Logs:  sudo journalctl -u notebooklm -f"
echo "═════════════════════════════════════════════════════════════════"
