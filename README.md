# NotebookLM Cinematic Studio

NotebookLM'de **birden fazla Google hesabı** üzerinden, **çoklu metin kuyruğu** ile **paralel** video üretimi tetikler. Streamlit tabanlı web UI'ı + CLI. Video indirme manuel (NotebookLM'in UI'ı bunu otomatize etmek için fazla volatile).

## Kurulum (3 komut)

```bash
git clone https://github.com/esattavukcu/notebooklm-cinematic-studio.git
cd notebooklm-cinematic-studio
./setup.sh
```

Setup ~2 dakika sürer (Python venv + Playwright + Chromium indirir).

## Çalıştırma

```bash
./app.sh
```

Tarayıcıda otomatik `http://localhost:8501` açılır.

## Kullanım

1. **Sol panelde** her Google hesabı için bir profil ekle. "Login başlat" butonuna basınca açılan Chromium'da o hesapla login ol → pencereyi kapat → "Login tamamlandı ✓".
2. **Hazırla sekmesinde** prompt'unu yapıştır (uzun system prompt'lar dahil), kuyruğa ekle.
3. **Durum sekmesinde** job'ları izle. Her biri bir notebook açar, metni yapıştırır, Cinematic seçer, Generate'e basar — sonra çıkar (~1-2 dk).
4. NotebookLM kendi tarafında 25-60 dk içinde video'yu üretir. Sen oturup beklemen gerekmez.
5. Video'yu indirmek için: **Durum sekmesinde** ilgili job'un yanındaki **🌐 Aç** linkine tıkla → notebook URL'in açılır → manuel olarak Download butonuna bas.

### Manuel indirme yolları

NotebookLM'de oluşturulmuş video'yu indirmek için iki yol:

**Yol A — Bizim Chromium'umuzda** (hesap başka yere giriş yapılmamışsa): Profilin yanındaki **"Tekrar login (re-auth)"** butonuna bas → açılan Chromium'da NotebookLM'e gir → video'yu indir → `~/Downloads`'a düşer.

**Yol B — Normal Chrome'da** (hesap normal Chrome'una da girilmişse): Sol panelde **🌐 NotebookLM'i normal Chrome'da aç** butonu → Mac'in default browser'ında NotebookLM açılır → manuel indir.

## Akış mimarisi

```
metin listesi → Streamlit UI → background worker → subprocess (Playwright)
                                                       │
                                                       ▼
                                Chromium (headless) → NotebookLM web → Generate tetikle
                                                       │
                                                       ▼
                              Notebook URL kaydedilir, NotebookLM video'yu üretir
                                                       │
                                                       ▼
                                Sen manuel indir (yukarıdaki Yol A veya B)
```

- `notebooklm_automator.py` — tek hesap için Playwright otomasyonu (CLI)
- `app.py` — Streamlit UI: profil yönetimi, kuyruk, durum tablosu, log
- `chrome_profiles/<id>/` — her hesabın Chromium profili
- `data/` — kalıcı state (jobs, profiles, drafts), log'lar

## CLI (UI olmadan)

```bash
.venv/bin/python notebooklm_automator.py \
  --profile-dir chrome_profiles/<id> \
  --headless \
  --no-wait-input \
  "metin..."
```

## Yeni sürüm yayınlama

```bash
git add -u && git commit -m "..." && git push
```

Mustafa bir sonraki `git pull` + `./app.sh`'da güncel kodla başlar.

## Sorun giderme

- **Video üretimi tetiklenmiyor:** NotebookLM UI değişmiş olabilir. Log sekmesinde job log'undaki `=== VIDEO OVERVIEW DEBUG ===` veya `=== DOWNLOAD ADAYLARI ===` çıktısını paylaş, selectorları güncelleriz.
- **Tarayıcı focus çalıyor:** Profile ayarlarında "Arka planda çalış (görünmez)" işaretli mi kontrol et.
- **Tekrar login açılmıyor:** Profile dir'deki SingletonLock dosyalarını temizle — script artık bunu otomatik yapıyor ama yine olursa: `rm -f chrome_profiles/<id>/SingletonLock`.

## Not

Bu araç bir tarayıcı otomasyonu — NotebookLM'in UI'ı değiştikçe selectorlar bozulur. Otomatik download akışı denedik ama UI çok değişken; o yüzden video üretimini tetikleyip sonra **manuel indirmek** en sağlam yol.
