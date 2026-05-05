# NotebookLM Cinematic Studio

NotebookLM'de **birden fazla Google hesabı** üzerinden, **çoklu metin kuyruğu** ile **paralel** Cinematic videolar üretir, **otomatik indirir**. Native macOS uygulaması olarak veya CLI/web olarak çalıştırılabilir.

## Özellikler

- 🪪 Çoklu hesap profili — her hesap için ayrı Chromium oturumu, şifre saklanmıyor
- 🔁 Round-robin dağıtım — hesaplar arasında adil paylaşım
- 🚦 Günlük limit — hesap başına "max X video/gün", NotebookLM kotasını aşmaz
- ⚡ Paralel mode — `auth.json` kaydedilmiş profillerde aynı hesapta 2-3 paralel job
- 📥 Otomatik download — video hazır olunca .mp4 `data/downloads/`'a iner
- ⏱ Follow-up worker — uzun süren job'ları (60+ dk) arka planda 10 dk'da bir kontrol eder, hazır olanı indirir
- 📝 Drafts/Compose — uzun system prompt'ları kart kart ekle, checkbox ile bulk seç
- 🎬 Videolar sekmesi — indirilenleri tek tıkla download
- 📊 CSV export — tüm job geçmişini Excel-uyumlu CSV olarak indir

## Kurulum (3 yöntem)

### Yöntem 1: macOS .app — en kolay (önerilen)

```bash
cd <bu klasör>
chmod +x build_app.sh
./build_app.sh
```

`NotebookLM Cinematic Studio.app` oluşur. **Çift tıkla aç.**

İlk açılışta:
- Source `~/Library/Application Support/NotebookLM Cinematic Studio/` klasörüne kopyalanır
- Python venv kurulur (~2 dakika)
- Playwright + Chromium indirilir
- Native macOS penceresi açılır (browser tabı yok)

Sonraki açılışlarda doğrudan ~3 saniyede başlar.

**Mustafa'ya dağıtmak için:**

```bash
zip -ry "NotebookLM Cinematic Studio.zip" "NotebookLM Cinematic Studio.app"
```

İlk açılışta macOS Gatekeeper uyarısı çıkabilir (imzasız .app olduğu için). Çözüm: `Sağ tık → Aç → Aç` (sadece bir kere). Veya `xattr -cr "NotebookLM Cinematic Studio.app"` komutu ile karantinayı kaldır.

### Yöntem 2: Web UI (Streamlit, browser tabı)

```bash
chmod +x setup.sh app.sh run.sh
./setup.sh
./app.sh        # http://localhost:8501 açılır
```

### Yöntem 3: CLI (tek prompt, tek hesap)

```bash
./run.sh "örümcekler nasıl yürür"
```

veya doğrudan:

```bash
.venv/bin/python notebooklm_automator.py \
  --profile-dir chrome_profiles/<id> \
  "metin"
```

## Kullanım akışı (UI)

1. **Sol panel — Profil ekle:** Her hesap için bir profil. Etiket + günlük limit (default 3) + paralel slot sayısı.
2. **Login başlat** butonuna bas. Açılan Chromium'da Google ile login ol → pencereyi kapat → **Login tamamlandı ✓**.
3. **Hazırla sekmesi:** Yeni içerik formuna prompt'unu yapıştır (uzun system prompt'lar dahil). "İçerik ekle" → kart oluşur.
4. Birden fazla içerik ekledikten sonra checkbox ile seç → **Seçilenleri kuyruğa ekle**.
5. **Durum sekmesi:** Job'lar canlı: queued → running → done (veya submitted/failed). 3 sn'de bir yenilenir.
6. **Videolar sekmesi:** İndirilen .mp4'ler. Tek tıkla download.

## Auto-update (git pull)

Repo: **https://github.com/esattavukcu/notebooklm-cinematic-studio**

`.app` her açılışta `~/Library/Application Support/NotebookLM Cinematic Studio/` klasöründe `git pull` çalıştırır. Yeni sürüm yayınlamak için sen ana repo'ya `git push` yaparsın; ekibin uygulamayı bir sonraki açtığında otomatik güncel kod gelir.

### Mustafa için — tek seferlik kurulum

`.app`'i bir kere açıp kapattıktan sonra (Application Support klasörü oluşmuş olur):

```bash
cd "$HOME/Library/Application Support/NotebookLM Cinematic Studio"

# Mevcut data ve profilleri yedekle
mv data data.backup 2>/dev/null || true
mv chrome_profiles chrome_profiles.backup 2>/dev/null || true

# Repo'yu clone et
git init
git remote add origin https://github.com/esattavukcu/notebooklm-cinematic-studio.git
git fetch origin main
git reset --hard origin/main

# Yedekleri geri koy
mv data.backup data 2>/dev/null || true
mv chrome_profiles.backup chrome_profiles 2>/dev/null || true
```

Bu kuruluyu yaptıktan sonra her `.app` açılışında `git pull` ile otomatik günceller.

### Sen — yeni sürüm yayınlamak

Geliştirme yaptığın klasörde:

```bash
git add -u
git commit -m "fix: ..."
git push
```

Git repo'sunda commit yoksa `.app` bundle source'undan çalışır (auto-update sessizce atlanır).

## Mimari

```
NotebookLM Cinematic Studio.app
└── Contents/
    ├── Info.plist
    ├── MacOS/notebooklm-launcher  ← shell script (PATH setup, venv, git pull, Streamlit başlat)
    └── Resources/source/          ← Python kaynak (ilk açılışta App Support'a kopyalanır)

~/Library/Application Support/NotebookLM Cinematic Studio/
├── app.py, notebooklm_automator.py, desktop.py
├── .venv/                         ← Python venv (Playwright + Streamlit + pywebview)
├── chrome_profiles/<id>/          ← her hesap için Chromium profili (login state, auth.json)
├── data/
│   ├── jobs.json, drafts.json, profiles.json
│   ├── downloads/*.mp4            ← indirilen videolar
│   └── logs/<job_id>.log          ← her job'ın canlı log'u
└── launcher.log                   ← .app launcher debug
```

Veri akışı:
- `app.py` Streamlit UI — 8501'den (rastgele port) yayın yapar, `desktop.py` pywebview ile gösterir
- Worker thread'i her 2 sn'de queued job'ları boş profile slot'larına atar (round-robin + günlük limit)
- Her job ayrı subprocess: `notebooklm_automator.py --profile-dir X --json-events <text>`
- Subprocess JSON event'leri stdout'a basar, parent thread parse edip job state'i günceller
- Follow-up worker her 10 dk'da submitted (timeout) job'ları açıp video hazırsa indirir

## Sorun giderme

- **`.app` açılmıyor / Gatekeeper uyarısı:** `xattr -cr "NotebookLM Cinematic Studio.app"` ile karantinayı kaldır.
- **launcher.log'a bak:** `~/Library/Application Support/NotebookLM Cinematic Studio/launcher.log`
- **`'Cinematic' seçeneği bulunamadı`** → NotebookLM UI değişti. Log sekmesindeki `=== VIDEO OVERVIEW DEBUG ===` çıktısını paylaş.
- **Otomatik download çalışmadı** → Log'da `Download butonu bulunamadı` görürsen, NotebookLM'in download akışı değişmiş. Status sekmesinde "Submitted'ları yeniden dene" ile follow-up worker'a tekrar denetebilirsin.
- **Worker job'ı almıyor** → Profilin "Login tamamlandı ✓" işaretli olduğundan ve günlük limitinin dolmadığından emin ol.
- **Paralel mod açılmıyor** → Profil "Tekrar login" → yeni login'de auth.json oluşur. Sonra ayarlardan paralel slot'u 2-3 yap.

## Güvenlik

Bu uygulama Google **şifrelerini saklamıyor** — sadece Chromium oturum cookie'leri ve `auth.json` (storage state) profile klasöründe duruyor. Bunlar makinene özel; başkasıyla paylaşma. Hesap sahipleri istedikleri an [Google → Güvenlik → Cihazlar](https://myaccount.google.com/device-activity)'tan oturumu sonlandırabilir.

```bash
chmod -R go-rwx "$HOME/Library/Application Support/NotebookLM Cinematic Studio"
```

## Sürüm

v0.4.0 — desktop .app, pywebview native window, auto-update via git pull, drafts/compose UI, follow-up worker
