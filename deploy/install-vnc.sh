#!/usr/bin/env bash
# install-vnc.sh — Sunucuda Xvfb + x11vnc + noVNC stack kurar.
# "Hesabı aktive et" butonuna tıklayınca açılan Chromium'u browser'dan
# görüp login olabilesin diye virtual display + VNC tüneli kurulur.
#
# Çalıştır:
#   curl -sSL https://raw.githubusercontent.com/esattavukcu/notebooklm-cinematic-studio/main/deploy/install-vnc.sh | bash
# veya repo içinde:
#   bash deploy/install-vnc.sh
#
# Sonrası:
#   1) .env'e şunu ekle:
#        HEADLESS_INIT_DISPLAY=:99
#   2) nginx.conf.template'i yeniden uygula (artık /vnc/ ve /websockify var)
#   3) sudo systemctl restart notebooklm nginx
#   4) Admin UI'da "Hesabı aktive et" → açılan VNC linkini tıkla → Chromium'u tarayıcıda gör

set -euo pipefail

SUDO=""
[ "$EUID" -eq 0 ] || SUDO="sudo"

echo "════════════════════════════════════════════════════"
echo "  noVNC stack installer"
echo "════════════════════════════════════════════════════"

echo "==> [1/4] Paketler kuruluyor (xvfb, x11vnc, novnc, websockify)..."
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq xvfb x11vnc novnc websockify

# noVNC web dosyaları farklı distrolarda farklı yerde olabilir
NOVNC_DIR=""
for candidate in /usr/share/novnc /usr/share/webapps/novnc /usr/local/share/novnc; do
  if [ -d "$candidate" ] && [ -f "$candidate/vnc.html" ]; then
    NOVNC_DIR="$candidate"
    break
  fi
done
if [ -z "$NOVNC_DIR" ]; then
  echo "HATA: noVNC web dosyaları bulunamadı. Manuel kurulum gerekebilir."
  exit 1
fi
echo "    noVNC dizini: $NOVNC_DIR"

# noVNC full client (vnc.html) default index.html olarak ayarla.
# vnc_lite.html'ten farkı: scaling, clipboard, settings panel, auto-reconnect,
# ping/keepalive desteği. Login UX'i için bunlar elzem.
# -f ile mevcut symlink üzerine yaz (eski kurulumlarda lite'a bağlı olabilir).
if [ -f "$NOVNC_DIR/vnc.html" ]; then
  $SUDO ln -sf "$NOVNC_DIR/vnc.html" "$NOVNC_DIR/index.html"
fi

echo "==> [2/4] systemd service'leri yazılıyor..."

# Xvfb — virtual display :99
$SUDO tee /etc/systemd/system/xvfb.service > /dev/null <<'EOF'
[Unit]
Description=Xvfb virtual display :99
After=network.target

[Service]
Type=simple
User=ubuntu
ExecStart=/usr/bin/Xvfb :99 -screen 0 1440x1024x24 -ac +extension GLX +extension RANDR +extension RENDER -noreset
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# x11vnc — VNC server reading from Xvfb :99
$SUDO tee /etc/systemd/system/x11vnc.service > /dev/null <<'EOF'
[Unit]
Description=x11vnc for Xvfb :99
After=xvfb.service
Requires=xvfb.service

[Service]
Type=simple
User=ubuntu
Environment=DISPLAY=:99
# -nopw: Şifre yok (nginx zaten basic auth ile koruyor, localhost-only zaten)
# -listen localhost: Sadece local interface'te dinler, dışa kapalı
# -forever: x11vnc bağlantı sonrası kapanmasın
# -shared: Birden fazla VNC client bağlanabilsin
# -noxdamage: Xvfb'de damage extension broken — polling kullan, yoksa frame
#   update'leri eksik gelir (yazarken karakterler kayboluyor gibi görünür)
# -defer 1 -wait 5: frame debounce — daha az ama daha tutarlı update
# -ping 10: 10sn'de bir keepalive RFB frame → idle WebSocket TCP düşmez
# -nocursor KALDIRILDI — cursor görünür, yazma pozisyonu takip edilebilir
# NOT: -ncache flag'i bilerek kullanılmıyor — framebuffer'ı 10x büyütüp altına
# offscreen cache alanı ekliyor. Native VNC viewer'lar bu cache'i kırpıyor ama
# noVNC bilmiyor → ekranın çoğu siyah cache alanı olarak görünüyor.
ExecStart=/usr/bin/x11vnc -display :99 -nopw -forever -shared -listen localhost -rfbport 5900 -noxdamage -defer 1 -wait 5 -ping 10
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# noVNC — websockify proxy (web client)
$SUDO tee /etc/systemd/system/novnc.service > /dev/null <<EOF
[Unit]
Description=noVNC websocket proxy
After=x11vnc.service
Requires=x11vnc.service

[Service]
Type=simple
User=ubuntu
ExecStart=/usr/bin/websockify --web=$NOVNC_DIR 6080 localhost:5900
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "==> [3/4] Servisleri başlat..."
$SUDO systemctl daemon-reload
$SUDO systemctl enable xvfb x11vnc novnc
# restart şart — unit dosyası değişmiş olabilir (yeni install veya update).
# enable --now sadece pasif servisleri başlatır, çalışanları restart etmez.
# Çalışan servisi yeni unit content'ine almak için explicit restart gerekir.
$SUDO systemctl restart xvfb x11vnc novnc
sleep 2

echo
echo "==> [4/4] Status:"
for svc in xvfb x11vnc novnc; do
  if $SUDO systemctl is-active --quiet $svc; then
    echo "    ✓ $svc: active"
  else
    echo "    ✗ $svc: NOT active — log: sudo journalctl -u $svc -n 20"
  fi
done

echo
echo "═══════════════════════════════════════════════════════════════"
echo "  ✓ noVNC stack kuruldu."
echo
echo "  Sonraki adımlar:"
echo
echo "  1) .env'e şu satırı ekle:"
echo "       HEADLESS_INIT_DISPLAY=:99"
echo
echo "  2) Eğer nginx eskiden kuruluysa, deploy/nginx.conf.template'i yeniden"
echo "     uygula (şimdi /vnc/ ve /websockify location'ları var):"
echo "       sudo cp deploy/nginx.conf.template /etc/nginx/sites-available/notebooklm"
echo "       sudo sed -i 's/studio.example.com/llm.yga.tr/g' /etc/nginx/sites-available/notebooklm"
echo "       sudo nginx -t && sudo systemctl reload nginx"
echo
echo "  3) Streamlit servisini restart et:"
echo "       sudo systemctl restart notebooklm"
echo
echo "  4) Admin UI'da yeni hesap ekle, 'Hesabı aktive et' butonuna tıkla."
echo "     Sayfada VNC link/iframe çıkar — Chromium'u tarayıcıda görüp"
echo "     login olabilirsin."
echo "═══════════════════════════════════════════════════════════════"
