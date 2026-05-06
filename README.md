# NotebookLM Cinematic Studio

Birden fazla Google hesabı üzerinden NotebookLM'de **toplu Cinematic video üretimi** tetikleyen Streamlit aracı. 50–100 metni elden geçirip her biri için tek tek notebook açmak yerine, hepsini kuyruğa atıp arka planda dağıtmaya yarar.

> Not: Bu araç videoları **otomatik indirmez**. NotebookLM 25–60 dk içinde videoyu üretir; sen sonra notebook URL'ini açıp manuel indirirsin. (NotebookLM'in indirme UI'ı çok değişken — otomatize etmek manuel akıştan daha kırılgan.)

## Özellikler

- 🔁 **Round-robin profil dispatch** — N farklı Google hesabı paralel kullanılır
- 📊 **Günlük limit takibi** — her hesap için ayrı (default: 3 video/gün)
- 🧵 **Paralel slot** — aynı hesapla birden fazla browser instance (auth.json ile)
- 📝 **Toplu metin kuyruğu** — checkbox ile seç, "kuyruğa ekle"
- 🎬 **Cinematic varsayılan** — Video Overview kartı + Generate butonu
- 🪟 **Headless** — varsayılan görünmez çalışır, focus çalmaz
- 📜 **Job logu** — her job için ayrı .log dosyası, Streamlit'te görünür
- 🌐 **Notebook URL'leri** kayıt altında — manuel indirme için tek tık

## Kurulum (3 komut)

```bash
git clone <repo-url> notebooklm-cinematic-studio
cd notebooklm-cinematic-studio
chmod +x setup.sh app.sh run.sh && ./setup.sh && ./app.sh
```

`setup.sh`:
- Python venv oluşturur (`.venv/`)
- `streamlit`, `playwright`, `python-dotenv` yükler
- Playwright Chromium indirir (~150MB, ilk seferde)

`app.sh`:
- Streamlit'i `http://localhost:8501` üzerinde başlatır
- Tarayıcı otomatik açılır

> macOS Gatekeeper, AppTranslocation, vs. sorunu yok — bu pure Python + Streamlit, .app bundle değil.

## İki mod: Kullanıcı vs Yönetim

Araç iki ayrı arayüz sunar:

### 👤 Kullanıcı görünümü (varsayılan, Mustafa-tier)
- URL: `http://your-domain/` (parametresiz)
- **Tek sayfa, tek textarea, tek button.** Senaryonu yapıştır → "Video üret" → bekle → notebook'u aç.
- İlk girişte ismini ister (Mustafa, Ahmet, vb.), sonraki ziyaretlerde sadece kendi gönderilerini görür.
- Hesap yönetimi, log, profil ayarları **görünmez**.

### ⚙️ Yönetim (admin) görünümü
- URL: `http://your-domain/?admin=<şifre>` (env var: `ADMIN_PASSWORD`)
- Lokal kullanım için: `?admin=1` (env var boşsa)
- Hesap (Google profil) ekleme, login başlatma, tüm job'lar, loglar, kuyruk yönetimi.

### Admin şifresi tanımla (sunucu dağıtımı için)

```bash
export ADMIN_PASSWORD="senin-secret-string"
./app.sh
```

`ADMIN_PASSWORD` set değilse `?admin=1` ile herkes admin olur (lokal geliştirme). Sunucuda mutlaka bir şifre belirle.

## İlk kullanım (yöneticinin yapacakları)

1. Tarayıcıda `http://localhost:8501/?admin=1` aç (veya sunucuda `?admin=<şifre>`)
2. Sol panelde **+ Yeni hesap ekle**:
   - Hesap adı: ayırt etmen için (örn. `baran-yga`, `editor-1`, vb.)
   - Diğer ayarlar opsiyonel — varsayılanlar iyi (3 video/gün, 1 paralel slot)
3. **🔓 Hesabı aktive et** → açılan Chromium'da Google'a giriş yap → pencereyi kapat
4. ✨ **Otomatik aktive olur** — auth.json yazılır yazılmaz hesap "🟢 hazır" olur. Manuel buton yok.
5. Kullanıcılara `http://your-domain/` URL'ini ver — onlar `?admin=` görmüyor, sadece submit ekranını.

## Kullanıcı için kullanım (Mustafa)

1. Yönetici verdiği URL'e git: `http://your-domain/`
2. İlk girişte adını yaz → "Devam"
3. Senaryonu (uzun metin) yapıştır → **🚀 Video üret**
4. Aşağıdaki listede durumu izle:
   - ⏳ KUYRUKTA → ▶ ÇALIŞIYOR → ✓ TAMAMLANDI
5. ✓ Tamamlandı olunca **🌐 Notebook'u aç** → NotebookLM'de Studio panelden 25-60 dk içinde video hazır olur.

## Mimari

```
metin listesi (UI)
    │
    ▼
data/jobs.json + data/profiles.json + data/drafts.json
    │
    ▼
Worker thread (her 2 sn dispatch round)
    │
    ├─ free profile (round-robin, daily_limit, max_concurrent)
    │
    ▼
subprocess → notebooklm_automator.py
    │
    ▼
Playwright Chromium (headless varsayılan)
    │
    └─ NotebookLM: Create → Copied text → Studio → Video Overview → Generate
```

## Dosya yapısı

```
notebooklm-cinematic-studio/
├── app.py                      # Streamlit UI + Worker thread
├── notebooklm_automator.py     # Playwright otomasyonu (CLI olarak da çalışır)
├── requirements.txt
├── setup.sh                    # ./setup.sh — kurulum
├── app.sh                      # ./app.sh   — UI başlat
├── run.sh                      # ./run.sh "<metin>" --profile-dir ... — CLI test
├── chrome_profiles/<id>/       # her profil için Chromium user_data_dir + auth.json
└── data/
    ├── jobs.json
    ├── profiles.json
    ├── drafts.json
    ├── downloads/              # manuel indirdiğin videolar
    └── logs/
        ├── launcher.log
        ├── <job_id>.log
        └── screenshots/
```

## CLI kullanımı

UI'sız direkt komut satırından da test edilebilir:

```bash
# Login init
./run.sh --init --profile-dir chrome_profiles/abc123 --authuser 0

# Otomatik tetikleme (headless)
./run.sh "Kahve nasıl yapılır?" \
  --profile-dir chrome_profiles/abc123 \
  --authuser 0 \
  --json-events
```

## Önemli design kararları

- ❌ **macOS .app bundle YOK.** Streamlit web UI yeterli; pywebview/Gatekeeper/TMPDIR'in hepsi kırılgan.
- ❌ **Otomatik video download YOK.** NotebookLM'in indirme UI'ı çok değişken; manuel daha sağlam.
- ❌ **Cinematic'e tıklanmaz.** Customize dialog'da varsayılan seçili gelir; üstüne tıklamak toggle eder ve Generate disabled olur.
- ✅ **Headless varsayılan.** Profile.headless=True; Chromium pop-up yapmaz, focus çalmaz.
- ✅ **Generate sonrası early-exit yok.** "Generating" göstergesi yakalanana kadar bekler; erken kapanma → API isteği iptal.
- ✅ **JSON file write atomic + thread-safe.** `RLock` + unique tmp adı (PID + thread_id + ts).
- ✅ **Stale state cleanup.** Streamlit her başladığında crash sonrası "running" job'ları `failed`'a düşürür.

## Sorun giderme

### "Chromium açılmıyor" / "already in use"
Profil klasöründe artakalan lock dosyaları var. `notebooklm_automator.py` başlangıçta `SingletonLock`/`SingletonCookie`/`SingletonSocket`'ı temizler ama yine de açılmazsa:
```bash
rm -f chrome_profiles/<id>/Singleton*
```

### "Generate disabled" hatası
Genelde Cinematic'in seçimi kalkmış demektir. NotebookLM'in customize dialog'unda Cinematic varsayılan seçili gelir; otomasyon ÜZERİNE TIKLAMAZ. Manuel kontrol için:
- `data/logs/screenshots/<job_id>_generate_disabled_*.png` ekran görüntülerine bak
- Headless'i kapat (`Arka planda çalış` toggle'ını kapat) ve gözünle takip et

### "Login redirect"
Stored credentials kick in etmediyse Playwright accounts.google.com'a düşer ve 5 dk bekler. Bu sürede manuel login olabilirsin (headless=False ile çalıştırırsan görünür).

### macOS: `mkdtemp ENOENT` hatası
TMPDIR'in `/var/folders/...` altında olması Playwright'ı kırıyor. Otomator başlangıçta TMPDIR'i profile dir altındaki stabil bir konuma yönlendirir. Yine de hata alıyorsan:
```bash
export TMPDIR=/tmp
```

### NotebookLM UI değişti, selectorlar çalışmıyor
NotebookLM Material Design — `mat-card`, `aria-label`'lar değişebilir. `notebooklm_automator.py` içinde `*_SELECTORS` listelerini güncelle. Multiple variant kullanılıyor, biri tutar.

## Geliştirme

- Python 3.10+ gerekli
- Stack: Streamlit + Playwright + standard lib (JSON dosyaları, threading)
- DB yok — 2-15 kişilik ekip aracı, JSON yeterli
- Test: `./run.sh "test" --profile-dir chrome_profiles/<id> --no-headless --no-wait-input` ile manuel adım adım izleyebilirsin
