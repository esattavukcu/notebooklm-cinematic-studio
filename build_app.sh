#!/usr/bin/env bash
# .app bundle inşa eder.
# Kullanım:
#   ./build_app.sh
# Üretilen: "NotebookLM Cinematic Studio.app" bu klasörde
set -e

cd "$(dirname "$0")"
SRC_DIR="$(pwd)"

APP_NAME="NotebookLM Cinematic Studio"
APP_DIR="${APP_NAME}.app"
VERSION="0.4.0"
BUNDLE_ID="org.yga.notebooklm-cinematic"

echo "==> Eski .app temizleniyor..."
rm -rf "$APP_DIR"

mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources/source"

echo "==> Kaynak dosyalar kopyalanıyor..."
# Önemli: chrome_profiles, data, .venv kopyalanmaz
SRC_FILES=(
  app.py
  notebooklm_automator.py
  desktop.py
  requirements.txt
  setup.sh
  app.sh
  run.sh
  README.md
  .env.example
  input.txt
)
for f in "${SRC_FILES[@]}"; do
  if [ -f "$f" ]; then
    cp "$f" "$APP_DIR/Contents/Resources/source/"
  fi
done

# Info.plist
echo "==> Info.plist yazılıyor..."
cat > "$APP_DIR/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundleDisplayName</key>
  <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key>
  <string>$VERSION</string>
  <key>CFBundleShortVersionString</key>
  <string>$VERSION</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleExecutable</key>
  <string>notebooklm-launcher</string>
  <key>LSMinimumSystemVersion</key>
  <string>10.15</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>NotebookLM otomasyonu için Chromium kontrolü gerekli.</string>
</dict>
</plist>
EOF

# Launcher script
echo "==> Launcher script yazılıyor..."
cat > "$APP_DIR/Contents/MacOS/notebooklm-launcher" <<'LAUNCHER_EOF'
#!/bin/bash
# NotebookLM Cinematic Studio — App Launcher
# Application Support'ta venv kurar, git pull ile günceller, desktop.py'ı başlatır.

set -u

# Finder'dan açılınca PATH eksik olabilir — yaygın yerleri ekle
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

APP_NAME="NotebookLM Cinematic Studio"
SUPPORT="$HOME/Library/Application Support/$APP_NAME"
LOG="$SUPPORT/launcher.log"
BUNDLE_SRC="$(cd "$(dirname "$0")/../Resources/source" && pwd)"

mkdir -p "$SUPPORT"
exec >> "$LOG" 2>&1

echo ""
echo "===== $(date) ====="
echo "Bundle source: $BUNDLE_SRC"
echo "Support dir: $SUPPORT"

cd "$SUPPORT"

# 1) Source senkronu (versiyon güncellemesi için)
# Eğer Application Support'ta git repo varsa, ona dokunma — git pull halleder.
# Yoksa bundle'dan source'u kopyala.
if [ -d ".git" ]; then
  echo "Git repo bulundu, bundle source'u atlanıyor."
else
  echo "Bundle source senkronlanıyor..."
  rsync -a --exclude='__pycache__' --exclude='.venv' --exclude='data' \
        --exclude='chrome_profile' --exclude='chrome_profiles' \
        --exclude='*.zip' --exclude='*.app' \
        "$BUNDLE_SRC/" "$SUPPORT/"
fi

# 2) Auto-update (varsa)
if [ -d ".git" ]; then
  echo "git pull deneniyor..."
  if git pull --quiet 2>>"$LOG"; then
    echo "Güncelleme tamam."
  else
    echo "git pull başarısız (offline veya çakışma olabilir), devam ediliyor."
  fi
fi

# 3) Python bul
PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  osascript -e 'display alert "Python 3 bulunamadı" message "Lütfen Python 3.10+ yükle (python.org veya brew install python)" as critical'
  exit 1
fi
echo "Python: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"

# 4) venv kurulum (yoksa)
if [ ! -x ".venv/bin/python" ]; then
  echo "İlk kurulum, ~2 dakika sürer..."
  osascript -e 'display notification "İlk kurulum başlıyor (~2 dk)..." with title "NotebookLM Cinematic"' &
  "$PYTHON_BIN" -m venv .venv
  ./.venv/bin/pip install --upgrade pip --quiet
  ./.venv/bin/pip install -r requirements.txt --quiet
  ./.venv/bin/playwright install chromium --quiet
  osascript -e 'display notification "Kurulum tamam, başlatılıyor..." with title "NotebookLM Cinematic"' &
fi

# 5) requirements.txt değiştiyse pip install -r tekrar çalıştır
REQ_HASH_FILE=".req.hash"
CURRENT_HASH="$(shasum requirements.txt | awk '{print $1}')"
PREV_HASH="$([ -f "$REQ_HASH_FILE" ] && cat "$REQ_HASH_FILE" || echo "")"
if [ "$CURRENT_HASH" != "$PREV_HASH" ]; then
  echo "requirements.txt değişti, paketler güncelleniyor..."
  ./.venv/bin/pip install -r requirements.txt --quiet
  echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
fi

# 6) Desktop launcher'ı başlat
echo "desktop.py başlatılıyor..."
exec ./.venv/bin/python desktop.py
LAUNCHER_EOF
chmod +x "$APP_DIR/Contents/MacOS/notebooklm-launcher"

# Optional icon
if [ -f icon.icns ]; then
  cp icon.icns "$APP_DIR/Contents/Resources/AppIcon.icns"
fi

echo ""
echo "✓ '$APP_DIR' inşa edildi."
echo ""
echo "Test:"
echo "  open \"$APP_DIR\""
echo ""
echo "Dağıtmak için:"
echo "  zip -ry \"${APP_NAME}.zip\" \"$APP_DIR\""
echo ""
echo "İlk açılışta ~2 dk kurulum yapar (Python venv + Playwright Chromium)."
echo "Sonraki açılışlarda direkt başlar."
echo "Loglar: ~/Library/Application Support/$APP_NAME/launcher.log"
