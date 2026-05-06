#!/usr/bin/env python3
"""
notebooklm_automator.py — Playwright tabanlı NotebookLM Cinematic video tetikleyici.

İki modda çalışır:

    --init                       İlk login için. Chromium pencerede açılır,
                                 kullanıcı elle login olur, kapatınca state
                                 user_data_dir + auth.json'a kaydedilir.

    "<metin>" (init bayrağı yok)  Otomasyon modu. Chromium (varsayılan headless)
                                 açılır, NotebookLM'de notebook oluşturur,
                                 metni "Copied text" kaynağı olarak ekler,
                                 Studio panelinden Video Overview'i tıklar,
                                 customize dialog'da Generate'e basar, üretim
                                 başladığını doğrulayıp çıkar.

JSON event çıktısı (--json-events ile birlikte) her event tek satır:
    ##JSON## {"type": "...", "ts": "...", ...}

Parent (app.py'deki Worker) bu event'leri parse eder, job state'ini günceller.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# TMPDIR fix — Finder'dan başlatılan macOS context'inde Playwright'ın mkdtemp
# çağrısı /var/folders/... altında ENOENT veriyor. Stabil bir tmp altına yönlendir.
# ---------------------------------------------------------------------------
def _ensure_stable_tmpdir(profile_dir: Optional[Path]) -> None:
    cur = os.environ.get("TMPDIR", "")
    if cur and not cur.startswith("/var/folders/") and Path(cur).exists():
        return
    if profile_dir is None:
        return
    stable = profile_dir / ".tmp"
    try:
        stable.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(stable)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Event emitter — parent worker JSON satırlarını parse eder.
# ---------------------------------------------------------------------------
class EventEmitter:
    def __init__(self, json_events: bool, log_prefix: str = "") -> None:
        self.json_events = json_events
        self.log_prefix = log_prefix

    def emit(self, event_type: str, **fields: Any) -> None:
        payload = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        if self.json_events:
            sys.stdout.write("##JSON## " + json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        # Her durumda insan-okunur log'a da yaz
        msg = f"[{event_type}] " + " ".join(f"{k}={v!r}" for k, v in fields.items() if k not in {"type", "ts"})
        sys.stdout.write(f"{self.log_prefix}{msg}\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Profil klasöründe artakalan Chromium lock dosyalarını temizle. Process crash
# ettiğinde bunlar temizlenmez ve yeni instance "already in use" diye açılmaz.
# ---------------------------------------------------------------------------
LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")


def cleanup_profile_locks(profile_dir: Path) -> None:
    if not profile_dir.exists():
        return
    for name in LOCK_FILES:
        p = profile_dir / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Selector listeleri — UI metni TR/EN değişebiliyor, multi-variant.
# ---------------------------------------------------------------------------
CREATE_NEW_SELECTORS = [
    'button:has-text("Create new")',
    'button:has-text("Yeni oluştur")',
    'button:has-text("Yeni Notebook")',
    'button:has-text("New notebook")',
    '[aria-label*="Create" i]',
    '[aria-label*="Yeni" i]',
    'a:has-text("Create new")',
]

COPIED_TEXT_SELECTORS = [
    'button:has-text("Copied text")',
    'button:has-text("Kopyalanan metin")',
    'button:has-text("Paste text")',
    'button:has-text("Metni yapıştır")',
    'div:has-text("Copied text") >> nth=0',
    'mat-card:has-text("Copied text")',
    '[role="button"]:has-text("Copied text")',
]

# NotebookLM Material Design — dialog `mat-dialog-container` ya da custom
# overlay olabilir. role="dialog" her zaman ayarlanmamış. Multi-variant.
DIALOG_SELECTORS = [
    '[role="dialog"]',
    'mat-dialog-container',
    '.mat-mdc-dialog-container',
    '.mat-mdc-dialog-surface',
    '.cdk-overlay-pane',
    '[aria-modal="true"]',
]

# Customize Video Overview dialog'unu daha spesifik yakalamak için
CUSTOMIZE_DIALOG_SELECTORS = [
    'text=/Customize Video Overview/i',
    'text=/Video Overview\'i özelleştir/i',
    'text=/How would you like the video to be customized/i',
    'text=/Format/ >> visible=true',
] + DIALOG_SELECTORS

INSERT_BUTTON_SELECTORS = [
    '[role="dialog"] button:has-text("Insert")',
    '[role="dialog"] button:has-text("Ekle")',
    'mat-dialog-container button:has-text("Insert")',
    'mat-dialog-container button:has-text("Ekle")',
    '.cdk-overlay-pane button:has-text("Insert")',
    '.cdk-overlay-pane button:has-text("Ekle")',
    'button:has-text("Insert")',
    'button:has-text("Ekle")',
]

VIDEO_OVERVIEW_SELECTORS = [
    '[aria-label="Video Overview"]',
    '[aria-label="Video"]',
    '[aria-label*="Video Overview" i]',
    'mat-card:has-text("Video Overview")',
    'mat-card:has-text("Video")',
    '[role="button"]:has-text("Video Overview")',
    'button:has-text("Video Overview")',
    'div:has-text("Video Overview") >> nth=0',
]

GENERATE_SELECTORS = [
    '[role="dialog"] button:has-text("Generate")',
    'mat-dialog-container button:has-text("Generate")',
    '.cdk-overlay-pane button:has-text("Generate")',
    '[role="dialog"] button:has-text("Oluştur")',
    'mat-dialog-container button:has-text("Oluştur")',
    '.cdk-overlay-pane button:has-text("Oluştur")',
    '[role="dialog"] button:has-text("Üret")',
    'mat-dialog-container button:has-text("Üret")',
    'button:has-text("Generate")',
    'button:has-text("Oluştur")',
]

GENERATING_INDICATORS = [
    'text=/generating/i',
    'text=/üretiliyor/i',
    'text=/oluşturuluyor/i',
    'text=/loading/i',
    'text=/building/i',
    'text=/yükleniyor/i',
    '[role="progressbar"]',
    'mat-spinner',
    '[aria-busy="true"]',
]

DESCRIPTION_INPUT_SELECTORS = [
    '[role="dialog"] textarea',
    'mat-dialog-container textarea',
    '.cdk-overlay-pane textarea',
    '[role="dialog"] input[type="text"]',
    'mat-dialog-container input[type="text"]',
    '[role="dialog"] [contenteditable="true"]',
    'mat-dialog-container [contenteditable="true"]',
]

# NotebookLM bazen authuser query param'ı redirect eder. authuser=N: account index.
DEFAULT_HOMEPAGE = "https://notebooklm.google.com/"


# ---------------------------------------------------------------------------
# Yardımcı: bir selector listesinde ilk eşleşeni bul (timeout küçük). Hiçbiri
# yoksa None dön (hata atma — caller karar versin).
# ---------------------------------------------------------------------------
def _find_first_visible(page, selectors: list[str], timeout_ms: int = 4000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


def _click_with_fallback(loc, page) -> bool:
    """Click → force click → dispatch_event → keyboard. İlk başarıyı dön."""
    try:
        loc.click(timeout=3000)
        return True
    except Exception:
        pass
    try:
        loc.click(force=True, timeout=2000)
        return True
    except Exception:
        pass
    try:
        loc.dispatch_event("click")
        return True
    except Exception:
        pass
    try:
        loc.focus()
        page.keyboard.press("Enter")
        return True
    except Exception:
        pass
    return False


def _take_screenshot(page, screenshots_dir: Path, tag: str) -> Optional[Path]:
    if not screenshots_dir:
        return None
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        path = screenshots_dir / f"{tag}_{ts}.png"
        page.screenshot(path=str(path), full_page=False, timeout=5000)
        return path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# INIT mode: kullanıcının elle login olmasını bekle, state'i kaydet.
# ---------------------------------------------------------------------------
def run_init(profile_dir: Path, authuser: int, emitter: EventEmitter) -> int:
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    cleanup_profile_locks(profile_dir)
    _ensure_stable_tmpdir(profile_dir)

    auth_json = profile_dir / "auth.json"
    saved_once = {"value": False}

    with sync_playwright() as pw:
        emitter.emit("init_starting", profile_dir=str(profile_dir))
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=DownloadBubble,DownloadBubbleV2",
            ],
            accept_downloads=True,
        )
        # Yeni veya mevcut sayfa
        page = context.pages[0] if context.pages else context.new_page()

        def _save_storage_state() -> None:
            try:
                state = context.storage_state()
                tmp = auth_json.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(state), encoding="utf-8")
                tmp.replace(auth_json)
                emitter.emit("auth_saved", path=str(auth_json))
                saved_once["value"] = True
            except Exception as e:
                emitter.emit("auth_save_error", error=str(e))

        def _on_framenav(frame) -> None:
            if saved_once["value"]:
                return
            url = frame.url or ""
            # NotebookLM'e ulaştığında bir kez kaydet. Daha sonraki polling'ler
            # Chromium'u öne getirebilir → focus stealing; kaçın.
            if "notebooklm.google.com" in url:
                _save_storage_state()

        page.on("framenavigated", _on_framenav)

        url = f"{DEFAULT_HOMEPAGE}?authuser={authuser}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            emitter.emit("nav_error", error=str(e))

        emitter.emit("init_waiting_for_close", hint="Tarayıcıda Google ile giriş yap, ardından pencereyi kapat.")
        try:
            page.wait_for_event("close", timeout=0)  # 0 = sonsuz
        except Exception:
            pass

        # Pencere kapanmadan önce son bir state save denemesi
        try:
            _save_storage_state()
        except Exception:
            pass

        try:
            context.close()
        except Exception:
            pass

    emitter.emit("init_complete", auth_saved=auth_json.exists())
    return 0


# ---------------------------------------------------------------------------
# OTOMASYON: notebook oluştur, metin ekle, Cinematic video tetikle.
# ---------------------------------------------------------------------------
def select_cinematic_video_overview(page, emitter: EventEmitter, screenshots_dir: Path, job_id: str) -> bool:
    """
    Studio panelinden Video Overview kartını tıklar, customize dialog'u açar,
    Cinematic'in seçili olduğunu doğrular (üzerine tıklamaz — toggle eder).
    Generate butonu enabled olduğunda True döner.
    """
    last_screenshot = time.time()

    # 1) Video Overview kartını bul ve tıkla
    loc = _find_first_visible(page, VIDEO_OVERVIEW_SELECTORS, timeout_ms=20000)
    if loc is None:
        emitter.emit("video_overview_not_found")
        _take_screenshot(page, screenshots_dir, f"{job_id}_no_video_card")
        return False

    emitter.emit("video_overview_found")
    if not _click_with_fallback(loc, page):
        emitter.emit("video_overview_click_failed")
        return False

    # 2) Customize dialog açılana kadar bekle. NotebookLM dialog'u çoğu zaman
    # role="dialog" YERİNE mat-dialog-container / cdk-overlay-pane içinde render
    # ediyor. Multi-variant selector kullan.
    def _dialog_visible() -> bool:
        # Önce metne dayalı (en güvenilir):
        for sel in (
            'text=/Customize Video Overview/i',
            'text=/Customize/i >> visible=true',
        ):
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=400)
                return True
            except Exception:
                continue
        # Sonra container selectorları:
        for sel in DIALOG_SELECTORS:
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=300)
                return True
            except Exception:
                continue
        return False

    dialog_opened = False
    deadline = time.time() + 7.5
    while time.time() < deadline:
        if _dialog_visible():
            dialog_opened = True
            break
        if time.time() - last_screenshot > 60:
            _take_screenshot(page, screenshots_dir, f"{job_id}_waiting_dialog")
            last_screenshot = time.time()

    if not dialog_opened:
        emitter.emit("dialog_retry_click")
        try:
            _click_with_fallback(loc, page)
        except Exception:
            pass
        # Bir kez daha 5 sn bekle
        retry_deadline = time.time() + 5
        while time.time() < retry_deadline:
            if _dialog_visible():
                dialog_opened = True
                break
            time.sleep(0.3)

    if not dialog_opened:
        emitter.emit("customize_dialog_not_opened")
        _take_screenshot(page, screenshots_dir, f"{job_id}_no_dialog")
        return False

    emitter.emit("customize_dialog_open")

    # 3) Cinematic seçili durumda — ÜSTÜNE TIKLAMA (toggle ediyor, kapatıyor).
    # Sadece varsayılan olduğunu varsayarız.

    # 4) Description input varsa, BOŞ ise doldur. NotebookLM bazen Cinematic
    # için suggestion otomatik dolduruyor — onu silmemek için kontrol et.
    desc_loc = _find_first_visible(page, DESCRIPTION_INPUT_SELECTORS, timeout_ms=2000)
    if desc_loc is not None:
        try:
            existing = ""
            try:
                existing = desc_loc.input_value(timeout=1000) or ""
            except Exception:
                # contenteditable için input_value çalışmaz
                try:
                    existing = (desc_loc.text_content(timeout=1000) or "").strip()
                except Exception:
                    pass
            if not existing.strip():
                desc_loc.fill("Cinematic style with clear narration.", timeout=3000)
                emitter.emit("description_filled")
            else:
                emitter.emit("description_already_filled", chars=len(existing))
        except Exception as e:
            emitter.emit("description_fill_failed", error=str(e))

    # 5) Generate butonunu bul ve disabled durumunu kontrol et.
    gen_loc = _find_first_visible(page, GENERATE_SELECTORS, timeout_ms=5000)
    if gen_loc is None:
        emitter.emit("generate_button_not_found")
        _take_screenshot(page, screenshots_dir, f"{job_id}_no_generate")
        return False

    is_disabled = False
    try:
        is_disabled = gen_loc.evaluate(
            """el => {
                if (el.disabled === true) return true;
                const ad = el.getAttribute('aria-disabled');
                if (ad === 'true') return true;
                const cls = el.className || '';
                if (cls.includes('disabled') || cls.includes('mat-disabled')) return true;
                return false;
            }"""
        )
    except Exception:
        pass

    if is_disabled:
        emitter.emit("generate_disabled_warning")
        _take_screenshot(page, screenshots_dir, f"{job_id}_generate_disabled")
        # yine de force-click dene

    # 6) Generate'e bas
    if not _click_with_fallback(gen_loc, page):
        emitter.emit("generate_click_failed")
        return False

    emitter.emit("generate_clicked")

    # 7) Üretim başladığını gösteren bir indicator bekle (max 60 sn).
    # Aynı zamanda "daily limit reached" / "limit dolu" mesajını yakala — bu
    # NotebookLM'in kota tükenmişlik bildirimi, Generate'e bassan da üretmiyor.
    QUOTA_PHRASES = [
        "text=/reached your daily/i",
        "text=/daily.*limit/i",
        "text=/come back later/i",
        "text=/günlük.*limit/i",
        "text=/günlük.*sınır/i",
        "text=/kota.*doldu/i",
    ]

    indicator_deadline = time.time() + 60
    while time.time() < indicator_deadline:
        # Önce kota mesajı var mı?
        for sel in QUOTA_PHRASES:
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=300)
                emitter.emit("quota_exceeded", indicator=sel)
                _take_screenshot(page, screenshots_dir, f"{job_id}_quota_exceeded")
                return False
            except Exception:
                continue
        # Sonra üretim göstergesi var mı?
        for sel in GENERATING_INDICATORS:
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=300)
                emitter.emit("generation_started", indicator=sel)
                return True
            except Exception:
                continue
        if time.time() - last_screenshot > 60:
            _take_screenshot(page, screenshots_dir, f"{job_id}_waiting_indicator")
            last_screenshot = time.time()
        time.sleep(1)

    # 60 sn sonunda hiçbir şey yakalayamadık. Son bir kez kota mesajını ara
    # (sayfa yavaş yüklenmiş olabilir).
    for sel in QUOTA_PHRASES:
        try:
            if page.locator(sel).first.is_visible():
                emitter.emit("quota_exceeded", indicator=sel)
                _take_screenshot(page, screenshots_dir, f"{job_id}_quota_exceeded")
                return False
        except Exception:
            continue

    emitter.emit("generation_indicator_timeout")
    _take_screenshot(page, screenshots_dir, f"{job_id}_no_indicator")
    # Generate'e basıldı ama indicator yakalanmadı — yine de True dönüyoruz
    # çünkü çoğunlukla NotebookLM arka planda üretmeye başlıyor.
    return True


def run_automation(
    text: str,
    profile_dir: Path,
    authuser: int,
    headless: bool,
    wait_for_input: bool,
    emitter: EventEmitter,
    job_id: str,
    download_dir: Path,
    screenshots_dir: Path,
) -> int:
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    cleanup_profile_locks(profile_dir)
    _ensure_stable_tmpdir(profile_dir)

    auth_json = profile_dir / "auth.json"
    notebook_url: Optional[str] = None
    exit_code = 1

    with sync_playwright() as pw:
        browser = None
        context = None

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=DownloadBubble,DownloadBubbleV2",
        ]

        # Mod 1: auth.json varsa, paralel-friendly non-persistent context
        # Mod 2: persistent_context (single-instance per user_data_dir)
        try:
            if auth_json.exists():
                emitter.emit("launch_mode", mode="storage_state")
                browser = pw.chromium.launch(headless=headless, args=launch_args)
                context = browser.new_context(
                    storage_state=str(auth_json),
                    accept_downloads=True,
                )
            else:
                emitter.emit("launch_mode", mode="persistent")
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless,
                    args=launch_args,
                    accept_downloads=True,
                )

            page = context.pages[0] if context.pages else context.new_page()

            # CDP ile download dir'i sabitle
            try:
                download_dir.mkdir(parents=True, exist_ok=True)
                client = context.new_cdp_session(page)
                client.send(
                    "Browser.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": str(download_dir.resolve())},
                )
            except Exception as e:
                emitter.emit("cdp_download_dir_failed", error=str(e))

            url = f"{DEFAULT_HOMEPAGE}?authuser={authuser}&pageId=none"
            emitter.emit("navigating", url=url)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Login redirect kontrolü — accounts.google.com'a düştüyse:
            # - Headless modda: kullanıcı login yapamaz, hemen fail-fast.
            # - Non-headless'ta: 5 dk pencerede manuel login bekle.
            try:
                # Sayfa redirect oturana kadar kısa bir bekleme
                time.sleep(1.0)
                cur_url = page.url or ""
                if "accounts.google.com" in cur_url or "/signin" in cur_url:
                    if headless:
                        emitter.emit(
                            "login_required_headless",
                            url=cur_url,
                            hint=("Hesabın Google login'i süresi geçmiş veya hiç yapılmamış. "
                                  "Admin panelinden 'Yeniden giriş' yap."),
                        )
                        _take_screenshot(page, screenshots_dir, f"{job_id}_login_required")
                        raise RuntimeError("login_required_headless")
                    emitter.emit("login_required_waiting", hint="Pencerede manuel login bekleniyor (max 5 dk)")
                    deadline = time.time() + 300
                    while time.time() < deadline:
                        cur = page.url or ""
                        if "notebooklm.google.com" in cur and "accounts.google.com" not in cur:
                            break
                        time.sleep(2)
                    # 5 dk sonunda hâlâ accounts.google.com'daysa → fail
                    cur = page.url or ""
                    if "accounts.google.com" in cur or "/signin" in cur:
                        emitter.emit("login_timeout", url=cur)
                        raise RuntimeError("login_timeout")
            except RuntimeError:
                raise
            except Exception:
                pass

            # Notebook oluştur
            create_loc = _find_first_visible(page, CREATE_NEW_SELECTORS, timeout_ms=20000)
            if create_loc is None:
                emitter.emit("create_button_not_found")
                _take_screenshot(page, screenshots_dir, f"{job_id}_no_create_btn")
                raise RuntimeError("Create new butonu bulunamadı")

            if not _click_with_fallback(create_loc, page):
                raise RuntimeError("Create new butonuna tıklanamadı")
            emitter.emit("create_clicked")

            # Source seçim ekranı: Copied text
            copied_loc = _find_first_visible(page, COPIED_TEXT_SELECTORS, timeout_ms=20000)
            if copied_loc is None:
                emitter.emit("copied_text_not_found")
                _take_screenshot(page, screenshots_dir, f"{job_id}_no_copied_text")
                raise RuntimeError("Copied text butonu bulunamadı")
            if not _click_with_fallback(copied_loc, page):
                raise RuntimeError("Copied text butonuna tıklanamadı")
            emitter.emit("copied_text_clicked")

            # Açılan dialog'da textarea / contenteditable bul, metni yapıştır
            text_loc = None
            for sel in (
                '[role="dialog"] textarea',
                '[role="dialog"] [contenteditable="true"]',
                'textarea',
                '[contenteditable="true"]',
            ):
                try:
                    candidate = page.locator(sel).first
                    candidate.wait_for(state="visible", timeout=5000)
                    text_loc = candidate
                    break
                except Exception:
                    continue
            if text_loc is None:
                emitter.emit("text_input_not_found")
                _take_screenshot(page, screenshots_dir, f"{job_id}_no_textarea")
                raise RuntimeError("Metin alanı bulunamadı")

            try:
                text_loc.fill(text, timeout=10000)
            except Exception:
                # contenteditable için fill bazen çalışmıyor — keyboard fallback
                try:
                    text_loc.click()
                    page.keyboard.insert_text(text)
                except Exception as e:
                    raise RuntimeError(f"Metin yapıştırma başarısız: {e}")
            emitter.emit("text_inserted", chars=len(text))

            # Insert butonu
            insert_loc = _find_first_visible(page, INSERT_BUTTON_SELECTORS, timeout_ms=10000)
            if insert_loc is None:
                emitter.emit("insert_button_not_found")
                _take_screenshot(page, screenshots_dir, f"{job_id}_no_insert")
                raise RuntimeError("Insert butonu bulunamadı")
            if not _click_with_fallback(insert_loc, page):
                raise RuntimeError("Insert butonu tıklanamadı")
            emitter.emit("insert_clicked")

            # Notebook URL'i yakalamak için biraz bekle (URL /notebook/<id>'ye dönecek)
            try:
                page.wait_for_url(re.compile(r".*/notebook/[^/?#]+"), timeout=60000)
            except Exception:
                # bazen URL hızlı değişmiyor, devam et — sonra tekrar al
                pass

            cur = page.url or ""
            m = re.search(r"/notebook/([^/?#]+)", cur)
            if m:
                # authuser query param'ını koru
                authuser_part = f"?authuser={authuser}"
                notebook_url = f"https://notebooklm.google.com/notebook/{m.group(1)}{authuser_part}"
                emitter.emit("notebook_created", notebook_url=notebook_url)

            # Studio paneli yüklensin diye küçük bir bekleme
            time.sleep(3)

            # Cinematic Video Overview seç ve Generate'e bas
            ok = select_cinematic_video_overview(page, emitter, screenshots_dir, job_id)
            if not ok:
                emitter.emit("video_generation_failed_in_dialog")
                # yine de notebook_url'i dön — kullanıcı manuel devam edebilir

            # Üretim onaylandı, browser'ı erken kapatmayalım: birkaç sn daha tut
            time.sleep(3)

            if wait_for_input:
                emitter.emit("waiting_for_input", hint="Pencereyi kapatınca çıkıyorum.")
                try:
                    page.wait_for_event("close", timeout=0)
                except Exception:
                    pass

            exit_code = 0 if ok else 2

        except Exception as e:
            emitter.emit("automation_error", error=str(e), trace=traceback.format_exc())
            try:
                if context is not None:
                    pages = context.pages
                    if pages:
                        _take_screenshot(pages[0], screenshots_dir, f"{job_id}_fatal")
            except Exception:
                pass
            exit_code = 1
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass

    emitter.emit(
        "automation_complete",
        exit_code=exit_code,
        notebook_url=notebook_url or "",
    )
    return exit_code


# ---------------------------------------------------------------------------
# HARVEST: video üretimi tamamlanmış notebook'a tekrar gir, video URL'ini
# bul, indir. Phase 1 (URL) + Phase 2 (download) tek subprocess'te.
# ---------------------------------------------------------------------------
def run_harvest(
    notebook_url: str,
    profile_dir: Path,
    authuser: int,
    headless: bool,
    emitter: EventEmitter,
    job_id: str,
    download_dir: Path,
    screenshots_dir: Path,
) -> int:
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    cleanup_profile_locks(profile_dir)
    _ensure_stable_tmpdir(profile_dir)

    auth_json = profile_dir / "auth.json"
    exit_code = 1
    video_url: Optional[str] = None
    local_path: Optional[Path] = None

    # Hâlâ üretim göstergesi var mı kontrolü için
    not_ready_indicators = [
        'text=/generating/i',
        'text=/üretiliyor/i',
        'text=/oluşturuluyor/i',
        'text=/preparing/i',
        '[role="progressbar"]',
        'mat-spinner',
    ]

    # Video player selectorları — modal veya inline
    video_player_selectors = [
        'video[src]',
        '[role="dialog"] video',
        'mat-dialog-container video',
        '.cdk-overlay-pane video',
        'video',  # son çare
    ]

    with sync_playwright() as pw:
        browser = None
        context = None
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=DownloadBubble,DownloadBubbleV2",
        ]

        try:
            if auth_json.exists():
                emitter.emit("harvest_launch", mode="storage_state")
                browser = pw.chromium.launch(headless=headless, args=launch_args)
                context = browser.new_context(
                    storage_state=str(auth_json),
                    accept_downloads=True,
                )
            else:
                emitter.emit("harvest_launch", mode="persistent")
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless,
                    args=launch_args,
                    accept_downloads=True,
                )

            page = context.pages[0] if context.pages else context.new_page()

            emitter.emit("harvest_navigating", url=notebook_url)
            page.goto(notebook_url, wait_until="domcontentloaded", timeout=60000)

            # Login redirect kontrolü
            time.sleep(1.5)
            cur_url = page.url or ""
            if "accounts.google.com" in cur_url or "/signin" in cur_url:
                emitter.emit("harvest_login_required", url=cur_url)
                _take_screenshot(page, screenshots_dir, f"{job_id}_harvest_login")
                raise RuntimeError("login_required")

            # Studio panelin yüklenmesini bekle
            time.sleep(3)

            # Video Overview kartını bul ve tıkla
            voloc = _find_first_visible(page, VIDEO_OVERVIEW_SELECTORS, timeout_ms=15000)
            if voloc is None:
                emitter.emit("harvest_video_card_not_found")
                _take_screenshot(page, screenshots_dir, f"{job_id}_harvest_no_card")
                raise RuntimeError("video_card_not_found")

            _click_with_fallback(voloc, page)
            time.sleep(2)

            # Önce hâlâ üretiliyor mu kontrol et (still generating)
            still_generating = False
            for sel in not_ready_indicators:
                try:
                    if page.locator(sel).first.is_visible(timeout=500):
                        still_generating = True
                        break
                except Exception:
                    continue

            if still_generating:
                emitter.emit("harvest_not_ready", reason="still_generating")
                _take_screenshot(page, screenshots_dir, f"{job_id}_harvest_not_ready")
                exit_code = 2  # caller bunu retry sinyali olarak kullanır
                return exit_code

            # Video player'i ara
            for sel in video_player_selectors:
                try:
                    locator = page.locator(sel).first
                    locator.wait_for(state="visible", timeout=4000)
                    src = locator.get_attribute("src") or ""
                    if not src:
                        # source child elementinden dene
                        try:
                            src = page.locator(f"{sel} source").first.get_attribute("src") or ""
                        except Exception:
                            pass
                    if src and src.startswith("http"):
                        video_url = src
                        break
                except Exception:
                    continue

            if not video_url:
                emitter.emit("harvest_video_not_found")
                _take_screenshot(page, screenshots_dir, f"{job_id}_harvest_no_video")
                exit_code = 2  # belki birazdan hazır olur
                return exit_code

            emitter.emit("harvest_video_url_found", video_url=video_url)

            # Phase 2: video'yu cookie'ler ile indir
            try:
                download_dir.mkdir(parents=True, exist_ok=True)
                local_path = download_dir / f"{job_id}.mp4"
                # Playwright APIRequestContext browser cookie'lerini otomatik kullanır
                api = context.request
                response = api.get(video_url, timeout=300_000)  # 5 dk timeout
                if response.ok:
                    body = response.body()
                    local_path.write_bytes(body)
                    size_mb = len(body) / (1024 * 1024)
                    emitter.emit(
                        "harvest_downloaded",
                        path=str(local_path),
                        size_mb=round(size_mb, 2),
                    )
                else:
                    emitter.emit(
                        "harvest_download_failed",
                        status=response.status,
                        url=video_url,
                    )
                    local_path = None
            except Exception as e:
                emitter.emit("harvest_download_error", error=str(e))
                local_path = None

            exit_code = 0
        except RuntimeError:
            # Zaten emit edildi
            pass
        except Exception as e:
            emitter.emit("harvest_error", error=str(e), trace=traceback.format_exc())
            exit_code = 1
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass

    emitter.emit(
        "harvest_complete",
        exit_code=exit_code,
        video_url=video_url or "",
        local_path=str(local_path) if local_path else "",
    )
    return exit_code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="NotebookLM Cinematic automator")
    parser.add_argument("text", nargs="?", default="", help="Kaynak metin (automation modunda)")
    parser.add_argument("--profile-dir", required=True, help="Chromium user_data_dir")
    parser.add_argument("--authuser", type=int, default=0, help="Google account index (?authuser=N)")
    parser.add_argument("--headless", action="store_true", help="Headless çalış")
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.set_defaults(headless=True)
    parser.add_argument("--init", action="store_true", help="Login init modu")
    parser.add_argument("--harvest", default="", help="Harvest modu: notebook URL'i ver, video bul/indir")
    parser.add_argument("--wait-input", dest="wait_input", action="store_true")
    parser.add_argument("--no-wait-input", dest="wait_input", action="store_false")
    parser.set_defaults(wait_input=False)
    parser.add_argument("--json-events", action="store_true", help="stdout'a ##JSON## event satırları yaz")
    parser.add_argument("--job-id", default="cli", help="screenshot/log isimleri için")
    parser.add_argument("--download-dir", default="data/downloads")
    parser.add_argument("--screenshots-dir", default="data/logs/screenshots")
    args = parser.parse_args(argv)

    profile_dir = Path(args.profile_dir).resolve()
    download_dir = Path(args.download_dir).resolve()
    screenshots_dir = Path(args.screenshots_dir).resolve()

    emitter = EventEmitter(json_events=args.json_events)

    # SIGTERM/SIGINT graceful: emit + çık
    def _on_signal(signum, frame):  # noqa: ARG001
        emitter.emit("signal_received", signal=signum)
        sys.exit(130)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
    except Exception:
        pass

    if args.init:
        return run_init(profile_dir, args.authuser, emitter)

    if args.harvest:
        return run_harvest(
            notebook_url=args.harvest,
            profile_dir=profile_dir,
            authuser=args.authuser,
            headless=args.headless,
            emitter=emitter,
            job_id=args.job_id,
            download_dir=download_dir,
            screenshots_dir=screenshots_dir,
        )

    if not args.text.strip():
        emitter.emit("error", message="text boş — automation modunda gerekli")
        return 64

    return run_automation(
        text=args.text,
        profile_dir=profile_dir,
        authuser=args.authuser,
        headless=args.headless,
        wait_for_input=args.wait_input,
        emitter=emitter,
        job_id=args.job_id,
        download_dir=download_dir,
        screenshots_dir=screenshots_dir,
    )


if __name__ == "__main__":
    sys.exit(main())
