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
  <key>LSMultipleInstancesProhibited</key>
  <true/>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.productivity</string>
</dict>
</plist>
EOF

# Launcher script
echo "==> Launcher script yazılıyor..."
cat > "$APP_DIR/Contents/MacOS/notebooklm-launcher" <<'LAUNCHER_EOF'
#!/bin/bash
# NotebookLM Cinematic Studio — App Launcher
# Application Support'ta venv kurar, GitHub Releases'tan otomatik günceller,
# desktop.py'ı başlatır. Git GEREKMEZ.

set -u

# Finder'dan açılınca PATH eksik olabilir
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

APP_NAME="NotebookLM Cinematic Studio"
GITHUB_REPO="esattavukcu/notebooklm-cinematic-studio"
SUPPORT="$HOME/Library/Application Support/$APP_NAME"
LOG="$SUPPORT/launcher.log"
BUNDLE_SRC="$(cd "$(dirname "$0")/../Resources/source" && pwd)"
VERSION_FILE="$SUPPORT/.installed_version"
LOCK_FILE="$SUPPORT/.app.pid"

mkdir -p "$SUPPORT"
exec >> "$LOG" 2>&1

echo ""
echo "===== $(date) ====="
echo "Bundle source: $BUNDLE_SRC"
echo "Support dir: $SUPPORT"

# ---- Singleton kontrolü ----
# Mevcut instance varsa pencereyi öne getirip çık
if [ -f "$LOCK_FILE" ]; then
  EXISTING_PID="$(cat "$LOCK_FILE" 2>/dev/null || echo '')"
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "Zaten çalışıyor (pid=$EXISTING_PID), pencere öne getiriliyor."
    # NotebookLM penceresini öne getirmeye çalış (pywebview window adı)
    osascript <<'OSAEOF' 2>/dev/null || true
tell application "System Events"
    set procs to (every process whose name contains "Python" or name contains "notebooklm")
    repeat with p in procs
        try
            set frontmost of p to true
            exit repeat
        end try
    end repeat
end tell
OSAEOF
    exit 0
  else
    echo "Eski lock bulundu ama process yok, temizleniyor."
    rm -f "$LOCK_FILE"
  fi
fi

# Bu instance'ın PID'ini lock dosyasına yaz, çıkışta temizle
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"; echo "Çıkış: lock temizlendi."' EXIT

cd "$SUPPORT"

# 1) İlk kurulum: bundle source'u Application Support'a aç
if [ ! -f "app.py" ]; then
  echo "İlk kurulum: bundle source kopyalanıyor..."
  rsync -a --exclude='__pycache__' --exclude='.venv' --exclude='data' \
        --exclude='chrome_profile' --exclude='chrome_profiles' \
        --exclude='*.zip' --exclude='*.app' \
        "$BUNDLE_SRC/" "$SUPPORT/"
fi

# 2) HTTP-tabanlı auto-update — GitHub Releases API
# Online kontrol, offline ise sessizce atla
echo "Güncelleme kontrolü..."
INSTALLED_VERSION="$([ -f "$VERSION_FILE" ] && cat "$VERSION_FILE" || echo '')"
LATEST_JSON="$(curl -fsS --max-time 8 "https://api.github.com/repos/$GITHUB_REPO/releases/latest" 2>/dev/null || true)"

if [ -n "$LATEST_JSON" ]; then
  LATEST_VERSION="$(echo "$LATEST_JSON" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("tag_name", ""))
except Exception:
    print("")' 2>/dev/null)"

  if [ -n "$LATEST_VERSION" ] && [ "$LATEST_VERSION" != "$INSTALLED_VERSION" ]; then
    echo "Yeni sürüm tespit edildi: $LATEST_VERSION (yüklü: ${INSTALLED_VERSION:-yok})"
    osascript -e "display notification \"Güncelleme indiriliyor: $LATEST_VERSION\" with title \"NotebookLM Cinematic\"" 2>/dev/null &

    ZIP_URL="$(echo "$LATEST_JSON" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    for a in d.get("assets", []):
        if a.get("name", "").endswith(".zip"):
            print(a.get("browser_download_url", ""))
            break
except Exception:
    pass' 2>/dev/null)"

    if [ -n "$ZIP_URL" ]; then
      TMPDIR="$(mktemp -d)"
      if curl -fsSL --max-time 120 "$ZIP_URL" -o "$TMPDIR/update.zip" \
         && unzip -qq "$TMPDIR/update.zip" -d "$TMPDIR"; then
        # .app içindeki source/'u bul
        NEW_SRC="$(find "$TMPDIR" -type d -name 'source' -path '*/Resources/source' | head -1)"
        if [ -n "$NEW_SRC" ] && [ -d "$NEW_SRC" ]; then
          echo "Source güncelleniyor: $NEW_SRC -> $SUPPORT"
          rsync -a --delete-excluded \
                --exclude='__pycache__' --exclude='.venv' --exclude='data' \
                --exclude='chrome_profile' --exclude='chrome_profiles' \
                --exclude='*.zip' --exclude='*.app' \
                --exclude='.installed_version' --exclude='launcher.log' \
                --exclude='.req.hash' \
                "$NEW_SRC/" "$SUPPORT/"
          echo "$LATEST_VERSION" > "$VERSION_FILE"
          osascript -e "display notification \"Güncellendi: $LATEST_VERSION\" with title \"NotebookLM Cinematic\"" 2>/dev/null &
        else
          echo "Yeni source bulunamadı zip içinde, atlanıyor."
        fi
      else
        echo "Zip indirme/açma başarısız (offline?), atlanıyor."
      fi
      rm -rf "$TMPDIR"
    else
      echo "Release zip URL'i bulunamadı."
    fi
  else
    echo "Güncel: $INSTALLED_VERSION"
  fi
else
  echo "GitHub'a erişilemedi (offline?), güncelleme atlanıyor."
fi

# 3) İlk kez kurulduysa ve version dosyası yoksa, bundle version'ı kaydet
if [ ! -f "$VERSION_FILE" ]; then
  echo "bundle" > "$VERSION_FILE"
fi

# 4) Python bul
PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  osascript -e 'display alert "Python 3 bulunamadı" message "Lütfen Python 3.10+ yükle (python.org veya brew install python)" as critical'
  exit 1
fi
echo "Python: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"

# 5) venv kurulum (yoksa)
if [ ! -x ".venv/bin/python" ]; then
  echo "İlk kurulum, ~2 dakika sürer..."
  osascript -e 'display notification "İlk kurulum başlıyor (~2 dk)..." with title "NotebookLM Cinematic"' 2>/dev/null &
  "$PYTHON_BIN" -m venv .venv
  ./.venv/bin/pip install --upgrade pip --quiet
  ./.venv/bin/pip install -r requirements.txt --quiet
  ./.venv/bin/playwright install chromium --quiet
  osascript -e 'display notification "Kurulum tamam, başlatılıyor..." with title "NotebookLM Cinematic"' 2>/dev/null &
fi

# 6) requirements.txt değiştiyse pip install -r tekrar çalıştır
REQ_HASH_FILE=".req.hash"
CURRENT_HASH="$(shasum requirements.txt | awk '{print $1}')"
PREV_HASH="$([ -f "$REQ_HASH_FILE" ] && cat "$REQ_HASH_FILE" || echo "")"
if [ "$CURRENT_HASH" != "$PREV_HASH" ]; then
  echo "requirements.txt değişti, paketler güncelleniyor..."
  ./.venv/bin/pip install -r requirements.txt --quiet
  echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
fi

# 7) Desktop launcher'ı başlat
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
