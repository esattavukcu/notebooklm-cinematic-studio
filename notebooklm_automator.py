#!/usr/bin/env python3
"""
NotebookLM Automator
====================
NotebookLM'de yeni bir notebook oluşturur, "Copied Text" ile metin yapıştırır,
sağdaki Video Overview'dan Cinematic seçer, Generate'e basar ve videonun
hazır olmasını bekler.

Kullanım:
    python notebooklm_automator.py "yapıştırılacak metin"
    python notebooklm_automator.py --file input.txt
    python notebooklm_automator.py            # input.txt'i otomatik okur

İlk çalıştırmada açılan tarayıcıda Google hesabınla giriş yap.
Profil ./chrome_profile/ klasörüne kaydedilir; sonraki çalıştırmalarda
otomatik login olunmuş olur.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
load_dotenv()

NOTEBOOKLM_URL = "https://notebooklm.google.com/?authuser=3&pageId=none"
DEFAULT_PROFILE_DIR = Path(__file__).parent / "chrome_profile"
DEFAULT_DOWNLOAD_DIR = Path(__file__).parent / "data" / "downloads"


# TMPDIR fix: Finder'dan açılan .app'lerde TMPDIR /var/folders/... olur ve
# Playwright bazen 'ENOENT: mkdtemp' hatası verir. Stabil bir yere yönlendir.
def _ensure_stable_tmpdir() -> None:
    current = os.environ.get("TMPDIR", "")
    if not current or current.startswith("/var/folders/"):
        stable = Path(__file__).parent / "data" / "tmp"
        stable.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(stable)


_ensure_stable_tmpdir()
GOOGLE_EMAIL = os.getenv("GOOGLE_EMAIL", "").strip()
GOOGLE_PASSWORD = os.getenv("GOOGLE_PASSWORD", "").strip()
DEFAULT_TIMEOUT_MIN = int(os.getenv("GENERATION_TIMEOUT_MIN", "25"))

# Run-time toggles, set by parse_args / run()
EMIT_JSON = False  # ebeveyn proses parse edebilsin diye stdout'a JSON event basar


# ------------------------------------------------------------------
# Yardımcılar
# ------------------------------------------------------------------
def emit(event: str, **fields) -> None:
    """JSON event satırı (parent process tarafından parse edilir)."""
    if not EMIT_JSON:
        return
    payload = {"ts": time.time(), "event": event, **fields}
    print("##JSON## " + json.dumps(payload, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
    emit("log", message=msg)


def notify(title: str, message: str) -> None:
    """macOS bildirim + terminal bell."""
    print("\a", end="", flush=True)
    try:
        os.system(
            f'''osascript -e 'display notification "{message}" with title "{title}"' '''
        )
    except Exception:
        pass
    log(f"BİLDİRİM: {title} — {message}")


def click_first_visible(page: Page, selectors: list[str], timeout: int = 8000) -> bool:
    """Verilen selector listesinde gözüken ilk öğeye tıkla."""
    end = time.time() + timeout / 1000
    while time.time() < end:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.click()
                    return True
            except Exception:
                continue
        time.sleep(0.3)
    return False


# ------------------------------------------------------------------
# Adımlar
# ------------------------------------------------------------------
def google_login_if_needed(page: Page) -> None:
    """Eğer login sayfasındaysak ve .env'de kimlik varsa otomatik dener."""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass

    if "accounts.google.com" not in page.url:
        return

    if not GOOGLE_EMAIL or not GOOGLE_PASSWORD:
        log("Google login sayfası açıldı. Lütfen elle giriş yap, script bekliyor...")
        # 5 dakika boyunca kullanıcının login olmasını bekle
        page.wait_for_url("**/notebooklm.google.com/**", timeout=300_000)
        return

    log("Google'a otomatik giriş deneniyor...")
    try:
        # E-posta
        page.fill('input[type="email"]', GOOGLE_EMAIL)
        page.click('button:has-text("Next"), button:has-text("İleri")')
        page.wait_for_timeout(2000)

        # Şifre
        page.wait_for_selector('input[type="password"]', timeout=15000)
        page.fill('input[type="password"]', GOOGLE_PASSWORD)
        page.click('button:has-text("Next"), button:has-text("İleri")')

        # NotebookLM'e dönmesini bekle (2FA/captcha varsa elle tamamla)
        log("Login submit edildi. NotebookLM'e dönmesi bekleniyor (2FA varsa elle tamamla)...")
        page.wait_for_url("**/notebooklm.google.com/**", timeout=300_000)
    except Exception as e:
        log(f"Otomatik login başarısız ({e}). Lütfen elle tamamla, bekliyorum...")
        page.wait_for_url("**/notebooklm.google.com/**", timeout=300_000)


def create_new_notebook(page: Page) -> None:
    log("Yeni notebook oluşturuluyor...")
    selectors = [
        'button:has-text("Create new")',
        'button:has-text("Yeni oluştur")',
        'button:has-text("New notebook")',
        '[aria-label*="Create" i]',
        'div[role="button"]:has-text("Create new")',
    ]
    if not click_first_visible(page, selectors, timeout=20000):
        raise RuntimeError("'Create new' butonu bulunamadı.")
    page.wait_for_timeout(1500)


def choose_copied_text(page: Page) -> None:
    log("Source seçim diyaloğunda 'Copied text' seçiliyor...")
    selectors = [
        'text="Copied text"',
        'text="Kopyalanan metin"',
        '[aria-label*="copied text" i]',
        'div:has-text("Copied text")',
        'button:has-text("Paste text")',
    ]
    if not click_first_visible(page, selectors, timeout=15000):
        raise RuntimeError("'Copied text' seçeneği bulunamadı.")
    page.wait_for_timeout(1000)


def _dump_dom_for_debug(page: Page) -> None:
    """Hata anında sayfadaki textarea/contenteditable adaylarını dump et."""
    try:
        info = page.evaluate(
            """() => {
              const out = [];
              const sels = [
                'textarea', 'input[type="text"]', '[contenteditable="true"]',
                'div[role="textbox"]', '[role="dialog"] *',
                'mat-dialog-container *', 'mwc-textarea', 'md-input'
              ];
              const seen = new Set();
              for (const s of sels) {
                document.querySelectorAll(s).forEach(el => {
                  if (seen.has(el)) return;
                  seen.add(el);
                  const r = el.getBoundingClientRect();
                  if (r.width === 0 || r.height === 0) return;
                  const tag = el.tagName.toLowerCase();
                  out.push({
                    tag,
                    role: el.getAttribute('role') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    aria: el.getAttribute('aria-label') || '',
                    id: el.id || '',
                    cls: (el.className || '').toString().slice(0, 80),
                    contentEditable: el.isContentEditable || false,
                  });
                });
              }
              return out.slice(0, 40);
            }"""
        )
        log("=== DOM DEBUG (gözüken text input adayları) ===")
        for i, el in enumerate(info):
            log(f"  [{i}] {el}")
        log("=== /DOM DEBUG ===")
    except Exception as e:
        log(f"DOM dump alınamadı: {e}")


def paste_text_and_insert(page: Page, text: str) -> None:
    log(f"Metin yapıştırılıyor ({len(text)} karakter)...")

    # Diyaloğun yüklenmesi için biraz bekle
    page.wait_for_timeout(2500)
    try:
        page.wait_for_selector(
            '[role="dialog"], mat-dialog-container, [aria-modal="true"]',
            timeout=8000,
            state="visible",
        )
    except PWTimeout:
        log("UYARI: dialog elementi göremedim, yine de denemeye devam.")

    # Geniş selector listesi — placeholder/aria/dialog scoped + göze çarpan boyut
    textarea_selectors = [
        '[role="dialog"] textarea',
        'mat-dialog-container textarea',
        '[aria-modal="true"] textarea',
        'textarea[placeholder*="aste" i]',     # Paste / paste
        'textarea[placeholder*="metin" i]',
        'textarea[aria-label*="aste" i]',
        'textarea[aria-label*="text" i]',
        '[role="dialog"] [contenteditable="true"]',
        'mat-dialog-container [contenteditable="true"]',
        '[contenteditable="true"]',
        'div[role="textbox"]',
        'textarea',
    ]
    pasted = False
    last_err: Exception | None = None
    for sel in textarea_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=1500):
                continue
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=3000)
            # textarea/input ise fill, contenteditable ise type
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("textarea", "input"):
                loc.fill(text)
            else:
                loc.evaluate("(el) => { el.focus(); el.innerText = ''; }")
                page.keyboard.type(text, delay=10)
            pasted = True
            log(f"OK: metin '{sel}' selector'üne yazıldı.")
            break
        except Exception as e:
            last_err = e
            continue

    if not pasted:
        _dump_dom_for_debug(page)
        raise RuntimeError(
            f"Metin alanı bulunamadı. Son hata: {last_err}. "
            "Yukarıdaki DOM dump'ı bana yapıştır, selector'ı düzeltirim."
        )

    page.wait_for_timeout(800)

    log("Insert/Ekle butonuna basılıyor...")
    insert_selectors = [
        '[role="dialog"] button:has-text("Insert")',
        '[role="dialog"] button:has-text("Ekle")',
        'mat-dialog-container button:has-text("Insert")',
        'button:has-text("Insert")',
        'button:has-text("Ekle")',
        'button:has-text("Add")',
        'button:has-text("Ekleme")',
        '[aria-label*="insert" i]',
        '[role="dialog"] button[type="submit"]',
    ]
    if not click_first_visible(page, insert_selectors, timeout=10000):
        _dump_dom_for_debug(page)
        raise RuntimeError("'Insert' butonu bulunamadı.")
    log("Metin eklendi, source işleniyor...")
    page.wait_for_timeout(5000)


def _dump_video_overview_debug(page: Page) -> None:
    """Video Overview alanındaki butonları/menüleri dump et."""
    try:
        info = page.evaluate(
            """() => {
              const out = [];
              const all = document.querySelectorAll('button, [role="button"], [role="menuitem"], [role="radio"], [role="option"], a, mat-card, mat-list-item');
              for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                const txt = (el.innerText || el.textContent || '').trim().slice(0, 60);
                const aria = el.getAttribute('aria-label') || '';
                if (!txt && !aria) continue;
                // Sadece "video"/"cinematic"/"overview"/"customize" geçenleri filtrele
                const blob = (txt + ' ' + aria).toLowerCase();
                if (!/(video|cinematic|sinematik|overview|customize|özelleş|generate|oluştur|brief|explainer|persona)/i.test(blob)) continue;
                out.push({
                  tag: el.tagName.toLowerCase(),
                  role: el.getAttribute('role') || '',
                  text: txt,
                  aria,
                  cls: (el.className || '').toString().slice(0, 60),
                });
              }
              return out.slice(0, 60);
            }"""
        )
        log("=== VIDEO OVERVIEW DEBUG (ilgili görünür elementler) ===")
        for i, el in enumerate(info):
            log(f"  [{i}] {el}")
        log("=== /VIDEO OVERVIEW DEBUG ===")
    except Exception as e:
        log(f"Video Overview dump alınamadı: {e}")


def select_cinematic_video_overview(page: Page) -> None:
    log("Sağ panelde Video Overview > Cinematic aranıyor...")

    # Source işlendikten sonra studio panelinin gelmesi için bekle
    page.wait_for_timeout(3000)

    # 1) Video Overview kartını/customize butonunu aç
    # NotebookLM'de Video Overview kartında bir "Customize"/ayar ikonu var.
    # Önce customize'ı dene, olmazsa kartın kendisine tıkla.
    customize_selectors = [
        'button[aria-label*="customize" i][aria-label*="video" i]',
        'button[aria-label*="video overview options" i]',
        'button[aria-label*="more" i][aria-label*="video" i]',
        'mat-card:has-text("Video Overview") button[aria-label*="customize" i]',
        'mat-card:has-text("Video Overview") button[aria-label*="more" i]',
        'mat-card:has-text("Video Overview") button[aria-label*="settings" i]',
        ':text("Video Overview") >> xpath=ancestor::*[self::mat-card or self::div][1]//button[contains(@aria-label,"ustomize") or contains(@aria-label,"ettings") or contains(@aria-label,"ore")]',
    ]
    opened = click_first_visible(page, customize_selectors, timeout=4000)
    if not opened:
        # Kart üstündeki herhangi bir tıklanabilir hedefe (başlık dahil) tıkla
        fallback_selectors = [
            'mat-card:has-text("Video Overview")',
            'button:has-text("Video Overview")',
            'div[role="button"]:has-text("Video Overview")',
            ':text("Video Overview")',
        ]
        click_first_visible(page, fallback_selectors, timeout=5000)

    page.wait_for_timeout(2000)

    # 2) Açılan menü/dialog/expanded panelde Cinematic seçeneğini bul
    cinematic_selectors = [
        '[role="menuitem"]:has-text("Cinematic")',
        '[role="option"]:has-text("Cinematic")',
        '[role="radio"]:has-text("Cinematic")',
        'mat-option:has-text("Cinematic")',
        '[role="dialog"] :text("Cinematic")',
        'button:has-text("Cinematic")',
        'div[role="button"]:has-text("Cinematic")',
        'text="Cinematic"',
        'text="Sinematik"',
        '[aria-label*="cinematic" i]',
    ]
    if not click_first_visible(page, cinematic_selectors, timeout=12000):
        # Belki Cinematic bir alt-menüde — "Format" ya da "Style" butonunu dene
        for menu_label in ["Format", "Style", "Stil", "Biçim", "Type"]:
            try:
                page.locator(f'button:has-text("{menu_label}")').first.click(timeout=2000)
                page.wait_for_timeout(1000)
                if click_first_visible(page, cinematic_selectors, timeout=5000):
                    log("Cinematic seçildi (alt-menü üzerinden).")
                    page.wait_for_timeout(1000)
                    return
            except Exception:
                continue
        _dump_video_overview_debug(page)
        raise RuntimeError(
            "'Cinematic' seçeneği bulunamadı. Yukarıdaki VIDEO OVERVIEW DEBUG "
            "çıktısını bana yapıştır, doğru selector'ı eklerim."
        )
    log("Cinematic seçildi.")
    page.wait_for_timeout(1000)


def click_generate(page: Page) -> None:
    log("Generate butonuna basılıyor...")
    generate_selectors = [
        'button:has-text("Generate")',
        'button:has-text("Oluştur")',
        'button:has-text("Create")',
        '[aria-label*="generate" i]',
    ]
    if not click_first_visible(page, generate_selectors, timeout=10000):
        raise RuntimeError("'Generate' butonu bulunamadı.")
    log("Video üretimi başlatıldı.")


def wait_for_video_ready(
    page: Page,
    timeout_min: int,
    fail_on_timeout: bool = True,
    screenshot_dir: Optional[Path] = None,
    job_id: str = "job",
) -> bool:
    """Video hazır olana kadar bekle. Hazırsa True döner.

    Üç sinyal kullanıyor:
    - Selector match (Download, video[src], vs.)
    - Heuristic: 'Generating' metni bir kez görünmüş ama artık 30 sn yok
    - Periyodik screenshot (debug için)
    """
    log(f"Video üretiminin tamamlanması bekleniyor (max {timeout_min} dk)...")
    started = time.time()
    deadline = started + timeout_min * 60

    # Genişletilmiş ready selectorları
    ready_selectors_strict = [
        'video[src]',
        'video source[src]',
        'a[href*=".mp4"]',
        'a[download]',
        'button:has-text("Download")',
        'button:has-text("İndir")',
        'button[aria-label*="download" i]',
        'button[aria-label*="indir" i]',
        '[role="menuitem"]:has-text("Download")',
        # NotebookLM'in yeni UI'ında video player wrapper olabilir
        '[aria-label*="play video" i]',
        '[data-test*="video-player"]',
    ]
    in_progress_texts = [
        "Generating", "Üretiliyor", "Yükleniyor", "Loading",
        "Creating video", "Video oluşturuluyor", "Hazırlanıyor",
        "Working on it", "İşleniyor",
    ]

    if screenshot_dir is not None:
        screenshot_dir.mkdir(parents=True, exist_ok=True)

    last_progress_log = started
    last_screenshot = started
    in_progress_seen_at_least_once = False
    in_progress_last_seen = 0.0

    while time.time() < deadline:
        # 1) Hâlâ üretiliyor mu?
        in_progress = False
        for txt in in_progress_texts:
            try:
                if page.locator(f'text="{txt}"').first.is_visible(timeout=300):
                    in_progress = True
                    in_progress_seen_at_least_once = True
                    in_progress_last_seen = time.time()
                    break
            except Exception:
                pass

        # 2) Selector tabanlı ready
        if not in_progress:
            for sel in ready_selectors_strict:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=400):
                        log(f"Video hazır! (selector: {sel})")
                        return True
                except Exception:
                    pass

        # 3) Heuristic: 'Generating' bir kez görünmüş ama 30 sn'dir yok → hazır say
        if (
            in_progress_seen_at_least_once
            and not in_progress
            and time.time() - in_progress_last_seen > 30
            and time.time() - started > 60  # en az 1 dk geçmiş olsun
        ):
            log("Video hazır! (heuristic: 'Generating' metni 30 sn'dir yok)")
            return True

        # 4) Periyodik ilerleme log'u
        now = time.time()
        if now - last_progress_log > 60:
            elapsed = int(now - started)
            seen = "evet" if in_progress_seen_at_least_once else "henüz hayır"
            log(
                f"…hâlâ bekliyor ({elapsed // 60} dk {elapsed % 60} sn geçti) "
                f"| in_progress şu an: {in_progress} | bir kez görüldü: {seen}"
            )
            last_progress_log = now

        # 5) Periyodik screenshot (debug için)
        if screenshot_dir is not None and now - last_screenshot > 90:
            try:
                ts = time.strftime("%H%M%S")
                shot = screenshot_dir / f"{job_id}_{ts}.png"
                page.screenshot(path=str(shot), full_page=False)
                log(f"Screenshot: {shot}")
                last_screenshot = now
            except Exception as e:
                log(f"Screenshot alınamadı: {e}")

        time.sleep(10)

    msg = (
        f"Video {timeout_min} dakika içinde tamamlanmadı. "
        "Follow-up worker 10 dk'da bir kontrol edip indirmeyi deneyecek."
    )
    if fail_on_timeout:
        raise TimeoutError(msg)
    log(f"UYARI: {msg}")
    emit("timeout_soft", message=msg)
    return False


def _safe_filename(text: str, max_len: int = 60) -> str:
    """Prompt metnini dosya adı için temizle."""
    cleaned = re.sub(r"[^\w\s\-çğıöşüÇĞİÖŞÜ]", "", text, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("_")
    return cleaned[:max_len] or "video"


def _dump_download_candidates(page: Page) -> None:
    """Sayfada download'la ilgili olabilecek tüm element/buton/link'leri logla."""
    try:
        info = page.evaluate(
            """() => {
              const out = [];
              const sels = ['button', 'a', '[role="button"]', '[role="menuitem"]', 'video', 'source'];
              const seen = new Set();
              for (const s of sels) {
                document.querySelectorAll(s).forEach(el => {
                  if (seen.has(el)) return;
                  seen.add(el);
                  const r = el.getBoundingClientRect();
                  const txt = (el.innerText || el.textContent || '').trim().slice(0, 40);
                  const aria = el.getAttribute('aria-label') || '';
                  const href = el.getAttribute('href') || '';
                  const dl = el.getAttribute('download');
                  const src = el.getAttribute('src') || '';
                  const blob = (txt + ' ' + aria).toLowerCase();
                  const isVideoEl = el.tagName === 'VIDEO' || el.tagName === 'SOURCE';
                  if (!/(download|indir|save|kaydet|export|.mp4)/i.test(blob + ' ' + href + ' ' + src) && !isVideoEl) return;
                  out.push({
                    tag: el.tagName.toLowerCase(),
                    text: txt,
                    aria,
                    href: href.slice(0, 120),
                    src: src.slice(0, 120),
                    download: dl,
                    visible: r.width > 0 && r.height > 0,
                  });
                });
              }
              return out.slice(0, 50);
            }"""
        )
        log("=== DOWNLOAD ADAYLARI ===")
        for i, el in enumerate(info):
            log(f"  [{i}] {el}")
        log("=== /DOWNLOAD ADAYLARI ===")
    except Exception as e:
        log(f"Download adayları dump alınamadı: {e}")


def _try_download_video_element(page: Page, dest_dir: Path, filename_hint: str) -> Optional[Path]:
    """Sayfadaki <video> elementinin src'sini bul ve direkt HTTP ile indir."""
    try:
        video_url = page.evaluate(
            """() => {
              const v = document.querySelector('video');
              if (!v) return null;
              if (v.src) return v.src;
              const s = v.querySelector('source[src]');
              return s ? s.src : null;
            }"""
        )
        if not video_url:
            log("Sayfada <video> elementi veya src bulunamadı.")
            return None
        if video_url.startswith("blob:"):
            log(f"Video URL'i blob: ({video_url[:80]}...) — HTTP fetch çalışmaz.")
            return None
        log(f"Video src: {video_url[:120]}")
        # Playwright context.request ile çek (cookies inherit eder)
        try:
            response = page.context.request.get(video_url)
            if response.ok:
                safe = _safe_filename(filename_hint)
                ts = time.strftime("%Y%m%d_%H%M%S")
                final_path = dest_dir / f"{ts}_{safe}.mp4"
                final_path.write_bytes(response.body())
                log(f"Video <video> elementinden indirildi: {final_path}")
                emit("video_downloaded", path=str(final_path), trigger="video_element")
                return final_path
            else:
                log(f"Video src HTTP yanıtı: {response.status}")
        except Exception as e:
            log(f"context.request.get hatası: {e}")
    except Exception as e:
        log(f"_try_download_video_element hatası: {e}")
    return None


def download_video(
    page: Page,
    dest_dir: Path,
    filename_hint: str,
    timeout_ms: int = 90000,
) -> Optional[Path]:
    """Hazır video için Download butonunu bul, dosyayı dest_dir'e kaydet."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    log("Download butonu aranıyor...")
    log(f"Sayfa URL: {page.url}")

    direct_selectors = [
        'button:has-text("Download")',
        'button:has-text("İndir")',
        'a[download]',
        '[aria-label*="download" i]',
        'button[aria-label*="download" i]',
    ]
    kebab_selectors = [
        'button[aria-label*="more" i]',
        'button[aria-label*="options" i]',
        'button[aria-label*="ayarlar" i]',
        'button:has-text("⋮")',
    ]
    menu_download_selectors = [
        '[role="menuitem"]:has-text("Download")',
        '[role="menuitem"]:has-text("İndir")',
        'text="Download"',
        'text="İndir"',
    ]

    download = None
    try:
        with page.expect_download(timeout=timeout_ms) as dl_info:
            clicked = False
            # 1) Direkt Download butonu
            for sel in direct_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() == 0:
                        continue
                    if loc.is_visible(timeout=1500):
                        loc.scroll_into_view_if_needed(timeout=1500)
                        loc.click()
                        clicked = True
                        log(f"Download tıklandı: {sel}")
                        break
                except Exception:
                    continue

            # 2) Kebab/More menüsü deneme
            if not clicked:
                log("Direkt Download bulunamadı, kebab/more menü deneniyor...")
                for kebab in kebab_selectors:
                    try:
                        kloc = page.locator(kebab).first
                        if kloc.count() == 0:
                            continue
                        if kloc.is_visible(timeout=1000):
                            kloc.click()
                            page.wait_for_timeout(700)
                            for msel in menu_download_selectors:
                                try:
                                    mloc = page.locator(msel).first
                                    if mloc.count() > 0 and mloc.is_visible(timeout=1500):
                                        mloc.click()
                                        clicked = True
                                        break
                                except Exception:
                                    continue
                            if clicked:
                                break
                    except Exception:
                        continue

            if not clicked:
                _dump_download_candidates(page)
                raise RuntimeError(
                    "Download butonu hiçbir yerde bulunamadı. "
                    "Yukarıdaki DOWNLOAD ADAYLARI listesini paylaşırsan selector'ı eklerim."
                )

        download = dl_info.value
    except PWTimeout:
        log("expect_download timeout — Download butonu tıklandı ama browser download event tetiklemedi.")
        log("Fallback: <video> elementinden src bulup HTTP ile indirme deneniyor...")
        video_path = _try_download_video_element(page, dest_dir, filename_hint)
        if video_path:
            return video_path
        _dump_download_candidates(page)
        raise RuntimeError(
            "Download tıklandı ama dosya gelmedi ve <video> fallback de işe yaramadı. "
            "DOWNLOAD ADAYLARI log'unu paylaş."
        )

    safe = _safe_filename(filename_hint)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    final_path = dest_dir / f"{timestamp}_{safe}.mp4"
    download.save_as(str(final_path))
    log(f"Video indirildi: {final_path}")
    emit("video_downloaded", path=str(final_path))
    return final_path


# ------------------------------------------------------------------
# Init modu — sadece login için tarayıcı aç
# ------------------------------------------------------------------
def _save_storage_state(context, profile_dir: Path) -> bool:
    """Kullanıcının login state'ini auth.json olarak kaydet (paralel mod için)."""
    try:
        state = context.storage_state()
        (profile_dir / "auth.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except Exception as e:
        log(f"auth.json kaydedilemedi: {e}")
        return False


def run_init(profile_dir: Path) -> None:
    """Profili login için aç. Kullanıcı login olunca pencereyi kapatır.
    Login state hem persistent profile'a hem auth.json'a kaydedilir.
    Bu modda download'lar otomatik ~/Downloads'a kaydedilir (manuel kullanım için)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    log(f"Login modu — profil: {profile_dir}")
    # Önceki çalışmadan kalan lock'ları temizle
    _cleanup_profile_locks(profile_dir)
    log("Açılan tarayıcıda Google hesabınla giriş yap, sonra pencereyi kapat.")
    log("Bu modda indirdiğin her dosya ~/Downloads klasörüne otomatik kaydedilecek.")
    emit("init_started", profile_dir=str(profile_dir))

    user_downloads = Path.home() / "Downloads"

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 850},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=DownloadBubble,DownloadBubbleV2",
            ],
            accept_downloads=True,
        )
        # Login modunda download'lar ~/Downloads'a otomatik kaydedilsin
        _attach_download_handler(context, user_downloads)

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(NOTEBOOKLM_URL, wait_until="domcontentloaded", timeout=60000)
        log("Tarayıcı açık. Login olduktan sonra pencereyi kapatın.")

        # Login tespitinde periyodik olarak auth.json'a kaydet.
        # Önemli: time.sleep yerine page.wait_for_timeout kullanıyoruz —
        # böylece Playwright event handler'ları (özellikle download) bekleme
        # sırasında işlenebilir. time.sleep main thread'i bloklayıp event'leri
        # kuyruğa atıyordu.
        last_save = 0.0
        try:
            while True:
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                try:
                    if "notebooklm.google.com" in page.url:
                        if time.time() - last_save > 8:
                            if _save_storage_state(context, profile_dir):
                                last_save = time.time()
                except Exception:
                    pass
                # 2 saniye bekle — bu sırada Playwright event'leri pump edilir
                try:
                    page.wait_for_timeout(2000)
                except Exception:
                    # Page kapatıldıysa wait_for_timeout hata verebilir
                    break
        except Exception:
            pass

        # Son bir kez kaydetmeye çalış
        try:
            _save_storage_state(context, profile_dir)
        except Exception:
            pass

        try:
            context.close()
        except Exception:
            pass

    auth_ok = (profile_dir / "auth.json").exists()
    emit("init_done", profile_dir=str(profile_dir), auth_saved=auth_ok)
    log(f"Profil kaydedildi (paralel mod: {'aktif' if auth_ok else 'pasif'}).")


# ------------------------------------------------------------------
# Ana akış
# ------------------------------------------------------------------
def _setup_cdp_download_behavior(context, dest_dir: Path) -> None:
    """CDP ile Chromium'a 'tüm download'ları şu klasöre kaydet' der.

    Playwright'ın Python event handler'ı tetiklenmese bile dosya iner.
    Native Chromium download mekanizması direkt yazar."""

    def configure_page(page):
        try:
            client = context.new_cdp_session(page)
            client.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(dest_dir),
                    "eventsEnabled": True,
                },
            )
            log(f"[cdp] page için download path ayarlandı: {dest_dir}")
        except Exception as e:
            log(f"[cdp] page config hatası: {e}")

    # Her yeni page için ayarla
    try:
        context.on("page", configure_page)
        # Mevcut page'lere de uygula
        for p in context.pages:
            configure_page(p)
        log(f"[cdp] download behavior kuruldu, klasör: {dest_dir}")
    except Exception as e:
        log(f"[cdp] kurulum hatası: {e}")


def _attach_download_handler(context, dest_dir: Path) -> None:
    """Chromium'da herhangi bir download başlarsa otomatik dest_dir'e kaydet.

    İki katmanlı:
    1. CDP setDownloadBehavior — Chromium native olarak dest_dir'e indirir
    2. Playwright context.on('download') — yedek katman, Playwright API'sinden
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    log(f"[download-handler] kuruluyor — hedef klasör: {dest_dir}")

    # CDP-tabanlı download path config (en güvenilir yol)
    _setup_cdp_download_behavior(context, dest_dir)

    def on_download(download):
        log(f"[download-handler] EVENT TETİKLENDİ! url={download.url[:80]}")
        try:
            suggested = download.suggested_filename or "download.bin"
            log(f"[download-handler] suggested_filename: {suggested}")
            # Sanitize + prefix timestamp (eğer aynı isimden varsa)
            safe = re.sub(r"[^\w\s.\-çğıöşüÇĞİÖŞÜ]", "", suggested, flags=re.UNICODE)
            target = dest_dir / safe
            if target.exists():
                ts = time.strftime("%Y%m%d_%H%M%S")
                target = dest_dir / f"{ts}_{safe}"
            log(f"[download-handler] save_as başlıyor → {target}")
            download.save_as(str(target))
            log(f"[download-handler] ✓ KAYDEDİLDİ: {target}")
            log(f"[download-handler] dosya boyutu: {target.stat().st_size} byte")
            emit("video_downloaded", path=str(target), trigger="auto_handler")
            # macOS bildirimi
            try:
                os.system(
                    f'''osascript -e 'display notification "{target.name}" with title "İndirme tamamlandı"' '''
                )
            except Exception:
                pass
        except Exception as e:
            log(f"[download-handler] ✗ HATA: {type(e).__name__}: {e}")
            import traceback
            log(traceback.format_exc())

    try:
        context.on("download", on_download)
        log(f"[download-handler] event listener kayıt edildi")
    except Exception as e:
        log(f"[download-handler] kuruluş hatası: {e}")


def _cleanup_profile_locks(profile_dir: Path) -> None:
    """Önceki çalışmadan kalan Chromium lock dosyalarını temizle.

    Chromium çakılırsa SingletonLock/SingletonCookie/SingletonSocket dosyaları
    profil klasöründe kalır. Yeni Chromium instance bunları görüp 'profil
    kullanılıyor' diye reddeder. Burada zorla temizleyelim.
    """
    if not profile_dir.exists():
        return
    lock_names = [
        "SingletonLock", "SingletonCookie", "SingletonSocket",
        "lockfile", ".org.chromium.Chromium.lock",
    ]
    for name in lock_names:
        lock_file = profile_dir / name
        try:
            # Symlink olabilir, hem unlink hem rm dene
            if lock_file.is_symlink() or lock_file.exists():
                lock_file.unlink(missing_ok=True)
                log(f"Stale lock temizlendi: {name}")
        except Exception as e:
            log(f"Lock silinemedi ({name}): {e}")


def _open_browser(pw, profile_dir: Path, headless: bool, download_dir: Optional[Path] = None):
    """Profile uygun şekilde browser/context aç.

    auth.json varsa: non-persistent (paralel-friendly).
    Yoksa: persistent context (tek instance).
    """
    auth_path = profile_dir / "auth.json"
    parallel_mode = auth_path.exists()

    # Persistent context kullanırken profile dir'deki stale lock'ları temizle
    if not parallel_mode:
        _cleanup_profile_locks(profile_dir)

    if parallel_mode:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=DownloadBubble,DownloadBubbleV2",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            storage_state=str(auth_path),
            accept_downloads=True,
        )
    else:
        browser = None
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=DownloadBubble,DownloadBubbleV2",
            ],
            accept_downloads=True,
        )

    if download_dir is not None:
        _attach_download_handler(context, download_dir)

    return browser, context, parallel_mode


def run(
    text: str,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    headless: bool = False,
    timeout_min: int = DEFAULT_TIMEOUT_MIN,
    keep_open: bool = True,
    soft_timeout: bool = True,
    download_dir: Optional[Path] = None,
    auto_download: bool = True,
) -> None:
    if not text.strip():
        raise ValueError("Yapıştırılacak metin boş olamaz.")

    profile_dir.mkdir(parents=True, exist_ok=True)
    if download_dir is None:
        download_dir = DEFAULT_DOWNLOAD_DIR
    download_dir.mkdir(parents=True, exist_ok=True)

    parallel_mode = (profile_dir / "auth.json").exists()
    log(f"Chrome profili: {profile_dir}")
    log(f"Mod: {'paralel (storage_state)' if parallel_mode else 'persistent (tekli)'}")
    emit(
        "job_started",
        profile_dir=str(profile_dir),
        parallel_mode=parallel_mode,
        text_preview=text[:80],
    )

    with sync_playwright() as pw:
        browser, context, _ = _open_browser(pw, profile_dir, headless, download_dir=download_dir)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            log(f"Açılıyor: {NOTEBOOKLM_URL}")
            page.goto(NOTEBOOKLM_URL, wait_until="domcontentloaded", timeout=60000)
            google_login_if_needed(page)

            emit("step", name="create_new_notebook")
            create_new_notebook(page)
            emit("step", name="choose_copied_text")
            choose_copied_text(page)
            emit("step", name="paste_text")
            paste_text_and_insert(page, text)
            emit("step", name="select_cinematic")
            select_cinematic_video_overview(page)
            emit("step", name="click_generate")
            click_generate(page)
            emit("step", name="wait_for_video")
            screenshot_dir = (Path(__file__).parent / "data" / "logs" / "screenshots")
            job_id = profile_dir.name  # profile_id'yi job_id olarak kullan
            ready = wait_for_video_ready(
                page,
                timeout_min,
                fail_on_timeout=not soft_timeout,
                screenshot_dir=screenshot_dir,
                job_id=job_id,
            )

            notebook_url = page.url
            video_path: Optional[Path] = None
            if ready and auto_download:
                try:
                    emit("step", name="download_video")
                    video_path = download_video(page, download_dir, text)
                except Exception as e:
                    log(f"Otomatik download başarısız: {e}")
                    emit("download_failed", error=str(e))

            emit(
                "job_done",
                notebook_url=notebook_url,
                video_ready=ready,
                video_path=str(video_path) if video_path else None,
            )
            if ready:
                msg = "Cinematic video hazır!" + (
                    f" İndirildi: {video_path.name}" if video_path else ""
                )
                notify("NotebookLM", msg)
                log(f"Tamamlandı. Notebook URL: {notebook_url}")
            else:
                notify(
                    "NotebookLM",
                    f"Süre doldu ama üretim devam ediyor olabilir. URL: {notebook_url}",
                )
                log(
                    f"Süre doldu ama hata atılmadı (soft timeout). "
                    f"Notebook URL: {notebook_url}"
                )
            if keep_open:
                log("Tarayıcıyı kapatmak için Enter'a bas...")
                try:
                    input()
                except EOFError:
                    pass
        except Exception as e:
            emit("job_failed", error=str(e))
            log(f"HATA: {e}")
            notify("NotebookLM HATA", str(e)[:120])
            if keep_open:
                log("Tarayıcı açık bırakıldı, elle inceleyebilirsin. Çıkmak için Enter...")
                try:
                    input()
                except EOFError:
                    pass
            raise
        finally:
            try:
                context.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def run_check_download(
    notebook_url: str,
    profile_dir: Path,
    download_dir: Optional[Path] = None,
    filename_hint: str = "video",
    headless: bool = False,
) -> bool:
    """Var olan notebook URL'ini aç, video hazırsa indir, kapat.
    Follow-up worker bu fonksiyonu çağırır."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    if download_dir is None:
        download_dir = DEFAULT_DOWNLOAD_DIR
    download_dir.mkdir(parents=True, exist_ok=True)

    log(f"Follow-up: {notebook_url} kontrol ediliyor...")
    emit("followup_started", notebook_url=notebook_url)

    with sync_playwright() as pw:
        browser, context, _ = _open_browser(pw, profile_dir, headless, download_dir=download_dir)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(notebook_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            google_login_if_needed(page)
            page.wait_for_timeout(2000)

            # Sadece kısa bir süre kontrol — hazır mı?
            ready = wait_for_video_ready(page, timeout_min=2, fail_on_timeout=False)
            if not ready:
                emit("followup_not_ready", notebook_url=notebook_url)
                log("Follow-up: video hâlâ hazır değil.")
                return False

            try:
                video_path = download_video(page, download_dir, filename_hint)
                emit(
                    "followup_done",
                    video_path=str(video_path) if video_path else None,
                )
                return True
            except Exception as e:
                log(f"Follow-up download başarısız: {e}")
                emit("followup_download_failed", error=str(e))
                return False
        finally:
            try:
                context.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NotebookLM otomasyonu")
    p.add_argument("text", nargs="?", help="Yapıştırılacak metin")
    p.add_argument("--file", "-f", help="Metni okuyacağı dosya yolu")
    p.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help="Chromium profil klasörü (her hesaba ayrı klasör)",
    )
    p.add_argument(
        "--init",
        action="store_true",
        help="Sadece login için tarayıcı aç, profili kaydet ve çık",
    )
    p.add_argument("--headless", action="store_true", help="Görünmez tarayıcı")
    p.add_argument(
        "--timeout-min",
        type=int,
        default=DEFAULT_TIMEOUT_MIN,
        help="Video üretimi için max bekleme süresi (dk)",
    )
    p.add_argument(
        "--no-wait-input",
        action="store_true",
        help="Bittiğinde Enter beklemeden çık (subprocess kullanımı için)",
    )
    p.add_argument(
        "--strict-timeout",
        action="store_true",
        help="Süre dolarsa exception at (default: soft timeout, sadece uyarı)",
    )
    p.add_argument(
        "--no-download",
        action="store_true",
        help="Video hazır olunca otomatik indirme yapma",
    )
    p.add_argument(
        "--check-download",
        metavar="URL",
        help="Mevcut notebook URL'ini aç, video hazırsa indir (follow-up modu)",
    )
    p.add_argument(
        "--filename-hint",
        default="video",
        help="--check-download ile birlikte: indirilen dosya için isim ipucu",
    )
    p.add_argument(
        "--download-dir",
        default=str(DEFAULT_DOWNLOAD_DIR),
        help="Video indirme klasörü",
    )
    p.add_argument(
        "--json-events",
        action="store_true",
        help="stdout'a ##JSON## prefixli event satırları bas",
    )
    return p.parse_args()


def main() -> None:
    global EMIT_JSON
    args = parse_args()
    EMIT_JSON = args.json_events
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    download_dir = Path(args.download_dir).expanduser().resolve()

    if args.init:
        run_init(profile_dir)
        return

    if args.check_download:
        ok = run_check_download(
            notebook_url=args.check_download,
            profile_dir=profile_dir,
            download_dir=download_dir,
            filename_hint=args.filename_hint,
            headless=args.headless,
        )
        sys.exit(0 if ok else 2)

    if args.text:
        text = args.text
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        default_file = Path(__file__).parent / "input.txt"
        if default_file.exists():
            log(f"Argüman verilmedi, {default_file} okunuyor.")
            text = default_file.read_text(encoding="utf-8")
        else:
            print(
                "Kullanım: python notebooklm_automator.py \"metin\" "
                "[--profile-dir PATH] [--headless] [--no-wait-input]"
            )
            sys.exit(1)

    run(
        text,
        profile_dir=profile_dir,
        headless=args.headless,
        timeout_min=args.timeout_min,
        keep_open=not args.no_wait_input,
        soft_timeout=not args.strict_timeout,
        download_dir=download_dir,
        auto_download=not args.no_download,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Kullanıcı iptal etti.")
        sys.exit(130)
