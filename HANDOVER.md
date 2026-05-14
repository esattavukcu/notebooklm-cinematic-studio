# NotebookLM Cinematic Studio — Devir Notu

**Devir tarihi:** 2026-05-14
**Repo:** https://github.com/esattavukcu/notebooklm-cinematic-studio
**Production:** https://llm.yga.tr/

Bu döküman projenin nasıl çalıştığını, sunucu erişimini, deploy akışını ve sık karşılaşılan operasyonları kapsar. Eski Claude/Mehmet kafa karışıklığına gerek kalmadan devam edebilirsin.

---

## 1. Proje Ne Yapıyor?

**Tek cümlede:** Kullanıcı bir senaryo (.docx veya Streamlit text area) verir → sistem NotebookLM hesaplarından birini kullanarak otomatik **Cinematic video** üretir (30-40dk gen), MP4'ü indirir, Azure Blob'a yükler, paylaşılabilir link döner.

**Akış:**
```
[Kullanıcı senaryo gir] / [Drive klasörü topluca yükle]
        ↓
[Worker dispatcher kuyruktan iş alır → uygun profile dağıtır]
        ↓
[notebooklm-py async pipeline]
  • Notebook yarat
  • Source upload (script + opsiyonel görseller)
  • Cinematic gen tetikle (Veo 3)
  • 30-40dk bekle
  • MP4 native indir
        ↓
[Azure Blob upload → kullanıcıya paylaşılabilir SAS URL]
```

---

## 2. Sunucu Erişimi

**EC2:** `ubuntu@ec2-35-156-158-86.eu-central-1.compute.amazonaws.com`
**SSH key:** `~/.ssh/dev-internal-00.pem` (Mehmet'te var, ondan al)
**Servis:** `notebooklm.service` (systemd, autostart enabled)
**Repo path:** `/home/ubuntu/notebooklm-cinematic-studio`
**Virtualenv:** `.venv/` (Python 3.12)
**Nginx:** `llm.yga.tr` → 127.0.0.1:8501

### Hızlı bağlantı:
```bash
ssh -i ~/.ssh/dev-internal-00.pem ubuntu@ec2-35-156-158-86.eu-central-1.compute.amazonaws.com
```

### Servis kontrol:
```bash
sudo systemctl status notebooklm    # durum
sudo systemctl restart notebooklm   # restart (her deploy'dan sonra)
sudo journalctl -u notebooklm -f    # canlı log
sudo journalctl -u notebooklm --since "10 min ago" | grep -iE "error|exception"
```

---

## 3. Deploy Akışı

**Her zaman aynı 4 komut:**

```bash
# 1. Local'de değişikliği test et
cd /Users/...path.../notebooklm-cinematic-studio
python3 -m py_compile app.py  # syntax check

# 2. Commit + push
git add . && git commit -m "..." && git push origin main

# 3. Server'a pull
ssh -i ~/.ssh/dev-internal-00.pem ubuntu@ec2-... 'cd notebooklm-cinematic-studio && git pull --ff-only'

# 4. Restart
ssh -i ~/.ssh/dev-internal-00.pem ubuntu@ec2-... 'sudo systemctl restart notebooklm && sleep 4 && sudo systemctl is-active notebooklm'
```

**Yeni dependency eklediysen:**
```bash
ssh ec2 'cd notebooklm-cinematic-studio && source .venv/bin/activate && pip install -r requirements.txt'
```

---

## 4. Mimari + Önemli Dosyalar

| Dosya | Amaç |
|---|---|
| `app.py` | Streamlit UI + Worker thread + dispatcher (~5500 satır, büyük ama tek dosya) |
| `notebooklm_client.py` | NotebookLM submit pipeline (teng-lin/notebooklm-py wrapper, async) |
| `gemini_client.py` | Gemini CLI subprocess wrapper — text gen (script iter + asset extract) |
| `bulk_import.py` | Drive klasöründen toplu docx import (gdown + python-docx) |
| `notebooklm_automator.py` | **Legacy** Playwright init flow (sadece auth.json yenileme için) |
| `nlm_client.py` | **Legacy** tmc/nlm Go CLI wrapper — `USE_LEGACY_SUBMIT=1` ile aktif olur |
| `data/jobs.json` | Tüm job state'leri (queued, running, generating, done, failed) |
| `data/profiles.json` | NotebookLM hesap profilleri (id, daily_limit, initialized) |
| `chrome_profiles/<id>/auth.json` | Her hesap için Playwright storage_state (cookies) |
| `data/downloads/<nb_id>.mp4` | İndirilen Cinematic videoları |
| `data/logs/launcher.log` | Worker dispatcher log'u |
| `data/logs/<job_id>.log` | Per-job pipeline log'u |

**Worker thread mimarisi (app.py içinde):**
- `class Worker` singleton — `@st.cache_resource` ile module load'da başlar
- `_loop()` her 2sn'de bir: `_auto_init_check` → `_dispatch_round` → `_reap_finished` → (her 60sn) `_harvest_round`
- Job dispatch: `_run_job_via_notebooklm` thread spawn — full async pipeline blocking olarak

---

## 5. Aktif Hesaplar (Profiles)

| Profil ID | Hesap | Daily Limit | Notlar |
|---|---|---|---|
| `1ee0e5c16713` | privacy@twinscience.com | 3 (free) | Login OK |
| `9b3d6c0b806f` | esat@twinscience.com | 3 (free) | Login OK |
| `450572a58b0e` | erdem.gelenbe@twinscience.com | 3 (free) | Login OK |

**Toplam kapasite:** 9 video/gün.

Ultra/Pro hesap eklenirse: admin → profil → Gelişmiş Ayarlar → "Günlük max video" → 20 (Ultra) veya 10 (AI Pro). Dispatcher otomatik dağıtır.

### Yeni hesap eklemek:

1. Admin panel → Sidebar "Yeni hesap ekle" → Email + authuser=0
2. Profilin **`💻 Lokal makineden yenile`** expander'ı aç
3. Mac'te ilk komutu çalıştır → Chromium açılır → o hesapla login
4. İkinci komutu çalıştır (rsync) → auth.json server'a kopyalanır
5. Admin'de **`✅ Auth.json'um hazır, kontrol et`** butonuna bas → otomatik aktive olur

---

## 6. Üç Önemli Kullanıcı Akışı

### A) Tek script submit (normal kullanıcı)

User https://llm.yga.tr → login → senaryo yapıştır → 3-step UI:
1. **Step 1 — Senaryo**: Yapıştır + AI ile düzenle (Gemini)
2. **Step 2 — Görseller**: AI asset çıkar + Wikimedia/Openverse/Pixabay/Pexels'ten image bul + manuel URL paste seçeneği
3. **Step 3 — Submit**: Custom prompt + "Kuyruğa gönder"

Worker thread alır → 30-40dk sonra MP4 hazır.

### B) Drive Toplu (40+ docx tek seferde)

1. Drive klasörünü **"Anyone with the link"** yap
2. Link kopyala
3. Admin tab `🗂️ Drive Toplu` veya user view'daki **`🗂️ Drive klasöründen toplu video üret`** expander
4. Drive URL yapıştır → "👁 Önizle"
5. Her dosya için checkbox + modified date + author göründü → istemediklerini de-select et
6. (Opsiyonel) Custom prompt template düzenle (default educational template hazır)
7. "🚀 Hepsini queue'ya at"
8. Dispatcher 9/gün dağıtır (3 profil × 3) → ~5 günde biter

### C) Revize (önceki videodan iteration)

Mevcut video kartından **"Revize et"** → custom prompt yaz → submit → eski video MP4'ü ilk source olarak NotebookLM'e yüklenir + revize talimatı ile yeniden gen.

---

## 7. Önemli Env Variables (`.env`)

```bash
# Admin gate (.env, asla commit edilmez)
ADMIN_PASSWORD=<random hex>

# Azure Blob (videoların paylaşım URL'i)
AZURE_STORAGE_CONNECTION_STRING=<sas connection string>
AZURE_CONTAINER=cinematic-videos
AZURE_BLOB_PREFIX=videos/

# Gemini text gen — TWO modes (env'e bağlı otomatik dispatch):
# Mode 1: API key (AKTİF, önerilen) — google-genai SDK direkt
GEMINI_API_KEY=AIzaSy...           # https://aistudio.google.com/apikey
# Mode 2: CLI fallback (key yoksa devreye girer)
GEMINI_BIN_PATH=/usr/local/bin/gemini

# Playwright sistem Chromium (Ubuntu 26.04 için)
# PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/google-chrome-stable

# Server-side login UI (xvfb + noVNC, ek özellik)
HEADLESS_INIT_DISPLAY=:99

# Lokal init flow için (admin UI'da rsync komutu hazır gelsin diye)
LOCAL_INIT_SSH_HOST=ubuntu@ec2-35-156-158-86.eu-central-1.compute.amazonaws.com
LOCAL_INIT_SSH_KEY=~/.ssh/dev-internal-00.pem
LOCAL_INIT_REMOTE_PATH=/home/ubuntu/notebooklm-cinematic-studio

# Quota block default 8h (NotebookLM Pacific reset ile uyumlu)
# QUOTA_BLOCK_HOURS=8

# Image search free tier (opsiyonel)
PIXABAY_API_KEY=<key>
PEXELS_API_KEY=<key>

# Legacy submit path'leri (artık kullanılmıyor, ama acil durum için var)
# USE_LEGACY_SUBMIT=1  → tmc/nlm Go CLI yolunu aktif eder
# USE_PLAYWRIGHT_SUBMIT=1  → eski Playwright cookie-fetch harvest cycle
```

---

## 8. Sık Karşılaşılan Operasyonlar

### Stuck "generating" job kurtarma

Server restart sırasında thread öldüyse, job `generating`'de kalır. Kurtar:

```python
# Server'da çalıştır:
ssh ec2 'cd notebooklm-cinematic-studio && source .venv/bin/activate && python3 -c "
from notebooklm_client import resume_download, notebook_id_from_url
from pathlib import Path
nb_id = notebook_id_from_url(\"https://notebooklm.google.com/notebook/<UUID>\")
out = Path(f\"data/downloads/{nb_id}.mp4\")
result = resume_download(profile_id=\"<PROFILE_ID>\", notebook_id=nb_id, out_path=out)
print(result)
"'
```

Sonra `data/jobs.json`'da o job'un `status="done"`, `harvest_status="downloaded"`, `video_local_path` set et — script bunu manuel yapıyor.

### Auth expired (profil "Yeniden giriş")

Profile için kotalar düşmesin diye admin panelden re-init:
1. Admin → Profil → `💻 Lokal makineden yenile` expander
2. Komutu Mac'te çalıştır → login
3. rsync komutu çalıştır
4. `✅ Kontrol et` butonu

VNC yolu da var ama VNC'siz lokal yol daha hızlı.

### Job duplicate / requeue

Failed job'u tekrar denemek için manuel script:

```python
ssh ec2 'cd notebooklm-cinematic-studio && python3 -c "
import json
jobs = json.load(open(\"data/jobs.json\"))
for j in jobs:
    if j.get(\"id\") == \"<JOB_ID>\":
        j[\"status\"] = \"queued\"
        j[\"started_at\"] = 0.0
        j[\"profile_id\"] = \"\"
        j[\"profile_name\"] = \"\"
        j[\"error\"] = \"\"
        j[\"notebook_url\"] = \"\"
        j[\"harvest_status\"] = \"pending\"
        break
json.dump(jobs, open(\"data/jobs.json\", \"w\"), indent=2, ensure_ascii=False)
"'
```

Worker dispatcher otomatik kuyruktan alır.

### Tüm job'lar / profillerin durumunu görme

Admin panelinde:
- **📊 Durum** sekmesi: tüm job'lar (queued/running/generating/done/failed counts)
- **🎬 Videolar**: indirilebilir MP4 listesi
- **📜 Log**: launcher.log canlı görüntü

Komutla:
```bash
ssh ec2 'cd notebooklm-cinematic-studio && python3 -c "
import json
jobs = json.load(open(\"data/jobs.json\"))
from collections import Counter
print(Counter(j[\"status\"] for j in jobs))
"'
```

---

## 9. Tier Detection / Quota (Önemli)

**Otomatik detection YOK** — NotebookLM tier API'si yok. Manuel set:
- Free: 3/gün
- AI Pro: ~10/gün
- AI Ultra: ~20/gün

**Self-correct:** Yanlış limit set edersen sistem `quota_exceeded` hatasını yakalar → o profili `QUOTA_BLOCK_HOURS=8` saat block eder, sonra otomatik retry. Google'ın gerçek reset zamanı Pacific time (~07-08:00 UTC) olduğu için 8h block doğru reset penceresini bulur.

**`today_count` reset:** Her gün 00:00 UTC (= 03:00 Türkiye) `date.today()` rollover ile sıfırlanır.

---

## 10. Bilinen Riskler / İzlenecek Şeyler

1. **OAuth refresh token expire** (~6 ay): privacy/erdem/esat hesapları için `notebooklm-py` ve `gemini-cli` auth'ları periyodik refresh ister. Admin'de "Yeniden giriş" akışı hazır.

2. **NotebookLM API değişiklikleri:** `teng-lin/notebooklm-py` library'si toplum projesi, Google bazen UI/API'sini değiştirip kütüphaneyi bozuyor. `pip install --upgrade notebooklm-py` ile takip et.

3. **Server reboot:** xvfb/x11vnc/novnc systemd unit'leri auto-restart yapmıyor olabilir — admin paneldeki VNC tabanlı init "Hesabı aktive et" çalışmıyorsa: `sudo systemctl start xvfb x11vnc novnc` (lokal init zaten alternatif).

4. **Drive klasör paylaşım kısıtı:** Twinscience Workspace admin "public sharing" kapatabilir. Bu durumda Drive Toplu çalışmaz → OAuth Drive scope'lu kurulum gerekir (~30dk iş, hazır plan var ama implement edilmedi).

5. **Long-running thread + restart:** Cinematic gen 30-40dk sürer. Bu süre içinde `systemctl restart` yaparsan thread ölür, job stuck kalır → `resume_download` ile kurtar (yukarıda).
   - **TODO:** Worker thread'e otomatik "stale generating sweeper" eklenebilir — her 5 dakikada bir 90+ dk geçmiş `generating` job'ları otomatik resume etmeye dener.

---

## 11. Son 2 Hafta Yapılan Migration'lar

- **OpenRouter → Gemini CLI (OAuth) → Gemini API key**: Text gen 3 evrim
  geçirdi. Şu an `google-genai` SDK + API key (AKTİF). CLI yolu fallback
  olarak duruyor (`GEMINI_API_KEY` yoksa devreye girer).
- **tmc/nlm Go CLI + Playwright harvest → teng-lin/notebooklm-py**: Python-native, native MP4 download, harvest cycle yok.
- **Pollinations image gen**: Hâlâ aktif (nano-banana OAuth tier'da yok). User isterse AI Studio API key path'i açılabilir.
- **Drive Toplu özelliği**: Public Drive klasör → 40 docx → 9/gün job dispatch.

---

## 12. Bana Soru Sorman Gereken Şeyler (Mehmet'e)

- `.env` ve `~/.ssh/dev-internal-00.pem` (server SSH key) — bu dökümana koymadım, güvenlik.
- Azure SAS token (~`.env` içinde). 2027-08-16'da expire oluyor — yenilemek için Azure Portal → erpgeneralstorage → SAS regenerate.
- Workspace hesapları (privacy/esat/erdem) şifreleri.
- GitHub repo write access.

---

## 13. Hızlı Smoke Test (devraldıktan sonra)

```bash
# 1. Server canlı mı?
curl -I https://llm.yga.tr/

# 2. Servis çalışıyor mu?
ssh ec2 'sudo systemctl is-active notebooklm'

# 3. Profil auth'ları sağlıklı mı?
ssh ec2 'cd notebooklm-cinematic-studio && source .venv/bin/activate && python3 -c "
from notebooklm_client import smoke_test
for pid in (\"1ee0e5c16713\", \"9b3d6c0b806f\", \"450572a58b0e\"):
    ok, msg = smoke_test(pid)
    print(pid, \"OK\" if ok else \"FAIL:\", msg[:100])
"'

# 4. Bulk import çalışıyor mu?
ssh ec2 'cd notebooklm-cinematic-studio && source .venv/bin/activate && python3 -c "
from bulk_import import is_available
print(is_available())
"'
```

Hepsi yeşil → sistem sağlıklı.

---

## 14. İletişim

- **Mehmet** (proje sahibi, dev): `mehmetesattavukcu@...` — kritik durumda
- **Serdar** (sık kullanıcı, içerik gönderir)
- **GitHub Issues:** `esattavukcu/notebooklm-cinematic-studio/issues` — bug + feature request

---

**Son commit (devir günü):** `e449513 resume_download: artifact.status int enum support`

İyi çalışmalar 👋
