# Sunucu Deployment — AWS EC2 ile

NotebookLM Cinematic Studio'yu AWS EC2 üzerinde production'a almak için adım adım rehber. Tahmini süre: **30-45 dakika**.

## Önerilen instance

| Spec | Değer | Yorum |
|---|---|---|
| **Instance type** | **t3.medium** | 2 vCPU + 4 GB RAM. 2-3 paralel Chromium için yeterli. |
| **AMI** | Ubuntu Server 22.04 LTS | Apt-tabanlı, Playwright Chromium tam destekli. |
| **Storage** | 30 GB **gp3** EBS | Login state (~50MB/profil) + indirilen videolar (~50MB/video) + screenshots/log. gp2 değil gp3 daha ucuz + hızlı. |
| **Region** | **eu-central-1** (Frankfurt) | TR'den ~50ms latency. us-east-1 ucuz ama 130ms. |
| **Public IP** | Elastic IP (instance bağlıyken ücretsiz) | Restart sonrası IP değişmez, DNS sabit kalır. |

**Maliyet (credit yokken)**:
- t3.medium 24/7: ~$30/ay
- EBS 30GB gp3: ~$2.40/ay
- Outbound transfer (10GB): ~$0.90/ay
- **Toplam ~$33/ay**

Credit varsa rahatça absorb edilir. Kullanmadığın saatlerde `Stop` yaparsan EBS dışı ücret durur (~$3/ay).

## Adım 1 — EC2 instance launch

AWS Console → EC2 → **Launch instance**:

```
Name & tags:
  Name: notebooklm-studio

Application and OS Images (AMI):
  ✓ Ubuntu Server 22.04 LTS (HVM), SSD Volume Type
  Architecture: 64-bit (x86)

Instance type:
  ✓ t3.medium  (2 vCPU, 4 GiB RAM)

Key pair:
  Create new key pair → notebooklm-key
  Type: RSA, Format: .pem
  → Download (~/Downloads/notebooklm-key.pem)
  ⚠ chmod 400 ~/Downloads/notebooklm-key.pem

Network settings:
  VPC: default
  Auto-assign public IP: Enable
  Firewall (security groups): Create security group
    Allow SSH:    Source = My IP (CIDR /32)
    Allow HTTP:   Source = 0.0.0.0/0
    Allow HTTPS:  Source = 0.0.0.0/0

Configure storage:
  1x 30 GiB gp3 (root)
  Delete on termination: ❌ (Off — instance silinse bile state durur)

Advanced details: (default bırak)
```

**Launch** tıkla. ~30 saniye içinde `running` durumuna gelir.

## Adım 2 — Elastic IP

EC2 Console → **Elastic IPs** → **Allocate Elastic IP address** → Allocate.

Sonra instance'a bağla: **Actions → Associate Elastic IP address** → instance'ı seç.

Bu IP artık kalıcı (instance restart edilse de değişmez). DNS bunu hedefler.

## Adım 3 — Domain DNS

Domain provider'ında (Cloudflare, Namecheap, vs.) bir A record:

```
Type:   A
Name:   studio  (veya istediğin subdomain)
Value:  <Elastic IP>
TTL:    300 (Auto)
```

Sonuç: `studio.example.com` → Elastic IP.

## Adım 4 — SSH bağlan ve kur

```bash
chmod 400 ~/Downloads/notebooklm-key.pem
ssh -i ~/Downloads/notebooklm-key.pem ubuntu@<elastic-ip>
```

Sunucuda **tek komutla kurulum**:

```bash
curl -sSL https://raw.githubusercontent.com/esattavukcu/notebooklm-cinematic-studio/main/deploy/install-server.sh | bash
```

Bu script:
1. apt update + Chromium dependencies (libnss3, libatk-bridge2.0-0, vs.)
2. nginx + certbot (Let's Encrypt için)
3. Repo'yu `~/notebooklm-cinematic-studio` altına klonla
4. Python venv + setup.sh ile playwright kur
5. systemd service oluştur ve başlat
6. Talimatları yazdır

~5-8 dakika sürer (Chromium download'u dahil).

## Adım 5 — `.env` dosyasını gönder

Lokal makineden Azure conn + admin password'ı göndermek için:

```bash
# Lokal makinede:
rsync -avz -e "ssh -i ~/Downloads/notebooklm-key.pem" \
  .env ubuntu@<elastic-ip>:~/notebooklm-cinematic-studio/.env

ssh -i ~/Downloads/notebooklm-key.pem ubuntu@<elastic-ip> 'sudo systemctl restart notebooklm'
```

## Adım 6 — Login state'leri rsync et

Lokal'de zaten login olduğun profilleri sunucuya gönder. Repo'da hazır script var:

```bash
# Lokal makinede (repo root):
SERVER=ubuntu@<elastic-ip> KEY=~/Downloads/notebooklm-key.pem \
  ./deploy/sync-profiles.sh
```

Bu:
- `chrome_profiles/` klasörünün hepsini rsync eder
- `.env` dosyasını gönderir
- `data/profiles.json`'u gönderir
- Sunucuda servisi yeniden başlatır

**Cookie expiry**: Google session'lar ~14 günde bir yenilenir. Ayda 1-2 kez bu komutu tekrarla.

## Adım 7 — nginx + HTTPS

Sunucuda:

```bash
cd ~/notebooklm-cinematic-studio

# nginx config'i kopyala
sudo cp deploy/nginx.conf.template /etc/nginx/sites-available/notebooklm

# server_name'i kendi domain'ine çevir
sudo nano /etc/nginx/sites-available/notebooklm
# (studio.example.com'u değiştir)

# Basic auth — sayfaya kim girebilir?
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd esat
# (şifre belirle, başka kullanıcı için -c'siz tekrar et)

# Enable + reload
sudo ln -sf /etc/nginx/sites-available/notebooklm /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# HTTPS — Let's Encrypt sertifikası (DNS A record proper olmalı)
sudo certbot --nginx -d studio.example.com
# Auto-renew otomatik kurulur, certbot.timer çalışır
```

Tarayıcıda `https://studio.example.com` aç — basic auth ekranı gelmeli, login → user view.

## Test akışı

1. **User view**: `https://studio.example.com/?u=test` → senaryo gönderebilmelisin
2. **Admin view**: `https://studio.example.com/?admin=<ADMIN_PASSWORD>` → profil yönetimi, durum, log
3. Yeni bir senaryo gönder (eğer hesap kotada değilse)
4. ~60-90 dk sonra harvest cycle tetiklenir
5. Video Azure'a yüklenir, user view'da **☁️ Video aç** butonu çıkar

## Operasyon

### Logs
```bash
sudo journalctl -u notebooklm -f                          # canlı
sudo journalctl -u notebooklm --since "1 hour ago"        # son 1 saat
tail -f ~/notebooklm-cinematic-studio/data/logs/launcher.log  # uygulamanın kendi log'u
```

### Servisi yönetme
```bash
sudo systemctl restart notebooklm   # restart (config değiştikten sonra)
sudo systemctl stop notebooklm      # durdur
sudo systemctl start notebooklm     # başlat
sudo systemctl status notebooklm    # durum
```

### Code update (yeni versiyon push'landığında)
```bash
cd ~/notebooklm-cinematic-studio
git pull
.venv/bin/pip install -r requirements.txt   # yeni dep varsa
sudo systemctl restart notebooklm
```

### Disk kullanımı
```bash
du -sh ~/notebooklm-cinematic-studio/data/downloads/  # indirilmiş videolar
du -sh ~/notebooklm-cinematic-studio/chrome_profiles/ # login state'ler
```

50MB/video × 100 video = 5GB. 30GB diskte rahat sığar; bittiğinde EBS volume'u büyütebilirsin (downtime yok).

### Maliyet kontrolü

```bash
# CloudWatch billing alarm kur (AWS Console):
# Billing → Billing preferences → Receive Free Tier Usage Alerts ✓
# CloudWatch → Alarms → Create → Billing → Threshold $5
```

Kullanmadığın gece saatlerinde instance'ı `Stop` et:
```
EC2 Console → Instance state → Stop
```
RAM faturalanmaz, sadece EBS ~$3/ay devam eder. `Start` ile saniyeler içinde dönü.

## Sorun giderme

### "Streamlit açılıyor ama UI dondu kalıyor"
Websocket connection problemi. nginx config'inde `proxy_set_header Upgrade $http_upgrade` ve `Connection "upgrade"` var mı kontrol et — `nginx.conf.template`'de mevcut.

### "Profile login olmuş ama sunucuda otomasyon login redirect alıyor"
Cookie expire olmuş. Lokal'de re-login yap, `./deploy/sync-profiles.sh` ile tekrar gönder.

### "azure-storage-blob ImportError"
```bash
.venv/bin/pip install azure-storage-blob
sudo systemctl restart notebooklm
```

### "Chromium başlamıyor (libnss3 vs eksik)"
install-server.sh tüm dependencies kuruyor olmalı. Manuel:
```bash
sudo apt install -y libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
  libcairo2 libasound2t64
```

### nginx 502 Bad Gateway
Streamlit servisi düşmüş demektir.
```bash
sudo systemctl status notebooklm
sudo journalctl -u notebooklm -n 50
```

### Port 8501 blocked
nginx içeriden bağlanır, dış dünya için açma. Eğer böyle olduysa Security Group'tan 8501 kaldır.

## Güvenlik notları

- ✅ `ADMIN_PASSWORD` set edilmiş olmalı (32+ char random string)
- ✅ Basic auth nginx'te aktif
- ✅ HTTPS certbot ile zorunlu
- ✅ SSH sadece "My IP" → SG'de
- ✅ `.env` dosyası 0600 permissions: `chmod 600 .env`
- ⚠ Azure SAS token expire tarihi takip et (env'de yazıyor)
- ⚠ `data/`, `chrome_profiles/` backup'lı tut (snapshot al haftalık)

## Snapshot (yedekleme)

Haftalık otomatik snapshot:
```
EC2 → Lifecycle Manager → Create lifecycle policy
  → Schedule: Weekly
  → Retention: 4 snapshots
```

Disaster recovery: Snapshot'tan yeni instance ayağa kaldır, Elastic IP yeni instance'a transfer et.
