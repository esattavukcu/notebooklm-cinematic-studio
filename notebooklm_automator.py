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
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# .env auto-load — direkt CLI çağrısında da PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
# vs. okunabilsin. app.py'den subprocess olarak çağrıldığında env zaten parent'tan
# inherit edilir, ama standalone test için bu lazım.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# TMPDIR fix — Finder'dan başlatılan macOS context'inde Playwright'ın mkdtemp
# çağrısı /var/folders/... altında ENOENT veriyor. Stabil bir tmp altına yönlendir.
# Linux'ta /tmp zaten stabil; ayrıca TMPDIR'i profile_dir/.tmp'a çekersek
# Chrome'un crashpad/IPC soketleri başarısız olup process spawn'da ölüyor.
# Bu yüzden override yalnızca macOS'ta (darwin) yapılır.
# ---------------------------------------------------------------------------
def _ensure_stable_tmpdir(profile_dir: Optional[Path]) -> None:
    if sys.platform != "darwin":
        return
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

# Notebook içinde "+ Add sources" butonu (sources panel header'ında)
ADD_SOURCES_SELECTORS = [
    'button:has-text("Add sources")',
    'button:has-text("Add source")',
    'button:has-text("Kaynak ekle")',
    'button:has-text("Kaynakları ekle")',
    '[aria-label="Add sources"]',
    '[aria-label="Add source"]',
    '[aria-label*="Add sources" i]',
    '[aria-label*="kaynak ekle" i]',
    'button[mattooltip="Add source"]',
    'button[mattooltip*="Add" i]',
]

# Cinematic Customize dialog'unda "Custom prompt" button/toggle.
# NotebookLM bunu bazen ikon, bazen text olarak gösterir. Multi-variant.
# İlk modal'da (Create new sonrası) ya da "+ Add sources" modal'ında
# "Upload files" butonu — tıklanınca native file picker açıyor (hidden
# input lazy-attached oluyor, doğrudan input[type=file] aramak fail ediyor).
# Bu yüzden expect_file_chooser ile butona basıp picker'ı yakalıyoruz.
UPLOAD_FILES_BUTTON_SELECTORS = [
    'button:has-text("Upload files")',
    'button:has-text("Upload Files")',
    'button:has-text("Dosya yükle")',
    'button:has-text("Dosyaları yükle")',
    '[role="button"]:has-text("Upload files")',
    'mat-card:has-text("Upload files")',
    '[aria-label*="upload" i][aria-label*="file" i]',
]


CUSTOM_PROMPT_BUTTON_SELECTORS = [
    'button:has-text("Custom prompt")',
    'button:has-text("Custom Prompt")',
    'button:has-text("Özel istem")',
    'button:has-text("Özel Prompt")',
    '[aria-label*="custom prompt" i]',
    '[aria-label*="özel istem" i]',
    'button:has-text("Customize")',  # bazen "Customize" → custom prompt'a açar
]

# Custom prompt dialog/modal — pasted text alanı
CUSTOM_PROMPT_INPUT_SELECTORS = [
    # Custom Prompt dialog özelinde
    'text=/Custom Prompt/i >> .. >> textarea',
    '[role="dialog"]:has-text("Custom Prompt") textarea',
    'mat-dialog-container:has-text("Custom Prompt") textarea',
    '.cdk-overlay-pane:has-text("Custom Prompt") textarea',
    # Genel — dialog içindeki en uzun textarea genelde custom prompt
    '[role="dialog"] textarea',
    'mat-dialog-container textarea',
    '.cdk-overlay-pane textarea',
]

# Custom Prompt dialog'undaki Save / Apply / OK / Done butonu (modal'i kapat)
CUSTOM_PROMPT_SAVE_SELECTORS = [
    '[role="dialog"] button:has-text("Save")',
    '[role="dialog"] button:has-text("Apply")',
    '[role="dialog"] button:has-text("Done")',
    '[role="dialog"] button:has-text("OK")',
    '[role="dialog"] button:has-text("Kaydet")',
    '[role="dialog"] button:has-text("Uygula")',
    'mat-dialog-container button:has-text("Save")',
    'mat-dialog-container button:has-text("Apply")',
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

# Eğer Playwright'ın bundled Chromium'u o OS için yoksa (ör. Ubuntu 26.04
# henüz Playwright tarafından desteklenmiyor), env var ile sistem Chrome'una
# yönlendirilebilir. Tipik değer: /usr/bin/google-chrome-stable
_SYSTEM_BROWSER_EXEC = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()


def _launch_kwargs_extra() -> dict:
    """Chromium launch kwargs — env'e göre dinamik."""
    kw: dict = {}
    if _SYSTEM_BROWSER_EXEC:
        kw["executable_path"] = _SYSTEM_BROWSER_EXEC
    # Xvfb (DISPLAY=:N) context'te Playwright'ın default --remote-debugging-pipe
    # Chrome'u SIGTRAP ile crash ettiriyor. Pipe'ı disable et, Playwright otomatik
    # --remote-debugging-port=0 (random TCP) fallback'e düşer.
    if os.environ.get("DISPLAY", "").startswith(":"):
        kw["ignore_default_args"] = ["--remote-debugging-pipe"]
    return kw


def _xvfb_args() -> list[str]:
    """Xvfb context'inde Chromium için ek argümanlar:
    - ozone=x11: Xvfb display'ini açıkça hedefle
    - disable-gpu: Xvfb'de GPU yok
    - remote-debugging-port=0: Pipe yerine TCP port (Playwright pipe handshake
      Xvfb context'inde kuramıyor → Chrome SIGTRAP ile kill ediliyor)
    - no-first-run: ilk çalıştırma sihirbazı atla
    - no-default-browser-check: default browser uyarısı atla
    """
    if os.environ.get("DISPLAY", "").startswith(":"):
        return [
            "--ozone-platform=x11",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=0",
        ]
    return []


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
#
# Strateji: Playwright'ın launch_persistent_context'i Xvfb context'inde
# Chromium'u SIGTRAP ile kıran flag'ler ekliyor. Workaround:
#   1) Chrome'u manuel `subprocess.Popen` ile başlat (manuel test çalışıyor)
#   2) Chrome --remote-debugging-port=9222 ile listenleyince Playwright'ı
#      connect_over_cdp ile bağla
#   3) Frame navigation'ı izle, NotebookLM'e ulaşınca storage_state kaydet
#   4) Chrome kapanınca finalize et
# ---------------------------------------------------------------------------
def _find_chrome_binary() -> Optional[str]:
    """Önce env var'dan, sonra Playwright bundled (Linux/macOS), sonra system Chrome."""
    env = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
    if env and Path(env).exists():
        return env

    # Playwright bundled chromium — platform-spesifik cache yolları
    # Linux: ~/.cache/ms-playwright/chromium-XXXX/chrome-linux64/chrome
    # macOS: ~/Library/Caches/ms-playwright/chromium-XXXX/chrome-mac{-arm64}/
    #         Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing
    cache_dirs: list[Path] = [
        Path.home() / ".cache" / "ms-playwright",                 # Linux/CI
        Path.home() / "Library" / "Caches" / "ms-playwright",     # macOS
        Path.home() / "AppData" / "Local" / "ms-playwright",      # Windows
    ]
    # Adaylar — her platform için olası binary yolu pattern'ları
    bin_patterns = [
        # Linux
        "chrome-linux64/chrome",
        "chrome-linux/chrome",
        # macOS (yeni: 'Google Chrome for Testing', eski: 'Chromium')
        "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
        "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        # Windows
        "chrome-win/chrome.exe",
    ]
    for cache in cache_dirs:
        if not cache.exists():
            continue
        for d in sorted(cache.glob("chromium-*"), reverse=True):
            for pat in bin_patterns:
                candidate = d / pat
                if candidate.exists():
                    return str(candidate)

    # System Google Chrome (Linux + macOS yolları)
    for p in (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if Path(p).exists():
            return p
    return None


def run_init(profile_dir: Path, authuser: int, emitter: EventEmitter) -> int:
    """Manuel Chrome subprocess + Playwright CDP connect.
    launch_persistent_context Xvfb'de SIGTRAP veriyor — bypass."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    cleanup_profile_locks(profile_dir)
    _ensure_stable_tmpdir(profile_dir)

    auth_json = profile_dir / "auth.json"
    saved_once = {"value": False}

    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        emitter.emit("init_error", error="Chrome binary bulunamadı")
        return 1

    # Random TCP port — birden fazla init paralel çalışırsa çakışmasın
    import random
    port = random.randint(9300, 9899)

    # Manuel test'te çalışan minimal flag set'i — Playwright'ın 40+ flag'i
    # Xvfb'de SIGTRAP'a sebep oluyor. Burada about:blank'a açıp navigation'ı
    # CDP üzerinden Playwright'a yaptırıyoruz.
    chrome_args = [
        chrome_bin,
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--enable-unsafe-swiftshader",  # Xvfb'de software GL fallback
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "about:blank",
    ]
    if os.environ.get("DISPLAY", "").startswith(":"):
        chrome_args.insert(1, "--ozone-platform=x11")
        chrome_args.insert(2, "--disable-gpu")

    emitter.emit("init_starting", profile_dir=str(profile_dir), port=port)

    # Chrome'u direkt subprocess.Popen ile başlat.
    # start_new_session=True → Chrome ayrı session/process group'ta başlar,
    # Python parent'ından bağımsız, sinyaller izole. Eskiden shell=True + "&"
    # kullanılıyordu ama sh wrapper'ı içinde Chrome backgrounding kararlı
    # değildi (ps tree: python─sh, Chrome hiç doğmuyor, port'a bind etmiyor).
    # stdout/stderr inherit (parent TTY veya journald'a gider).
    try:
        chrome_proc = subprocess.Popen(
            chrome_args,
            start_new_session=True,
        )
    except Exception as e:
        emitter.emit("init_error", error=f"Chrome spawn fail: {e}")
        return 1

    # chrome_proc.pid = gerçek Chrome PID'i (artık shell pid'i değil).
    emitter.emit("init_chrome_pid", pid=chrome_proc.pid)

    # Chrome'un port'ta listenlemesini bekle (max 30 sn)
    import urllib.request
    import urllib.error
    cdp_url = f"http://127.0.0.1:{port}/json/version"
    chrome_ready = False
    # Chrome `&` ile arka plana atıldı — Popen'ın gördüğü shell zaten çıktı.
    # Yaşam takibini sadece /json/version polling ile yapıyoruz.
    for _ in range(30):
        try:
            urllib.request.urlopen(cdp_url, timeout=1)
            chrome_ready = True
            break
        except (urllib.error.URLError, OSError):
            time.sleep(1)

    if not chrome_ready:
        emitter.emit("init_error", error="Chrome 30 sn içinde port'ta listenleyemedi")
        return 1

    emitter.emit("init_chrome_ready", port=port)

    # Playwright'ı CDP ile bağla
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
            contexts = browser.contexts
            if not contexts:
                emitter.emit("init_error", error="Chrome context'i bulunamadı")
                chrome_proc.terminate()
                return 1
            context = contexts[0]
            page = context.pages[0] if context.pages else context.new_page()

            # Cookies count'una göre "en zengin" save'i koruyalım — login
            # tamamlanmadan önce save edilirse cookies az, sonra daha çok olur.
            # Daha çok cookie'li save var ise üzerine yazmamalıyız.
            best_save = {"n_cookies": 0}

            def _save_storage_state(reason: str = "framenav") -> None:
                try:
                    state = context.storage_state()
                    n = len(state.get("cookies") or [])
                    # Daha az cookie'li bir state üzerine yazma (regression koruması)
                    if n < best_save["n_cookies"]:
                        return
                    tmp = auth_json.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(state), encoding="utf-8")
                    tmp.replace(auth_json)
                    best_save["n_cookies"] = n
                    emitter.emit(
                        "auth_saved", path=str(auth_json),
                        cookies=n, reason=reason,
                    )
                    saved_once["value"] = True
                except Exception as e:
                    emitter.emit("auth_save_error", error=str(e), reason=reason)

            def _on_framenav(frame) -> None:
                url = (frame.url or "").lower()
                # Login sayfalarında save etme (cookies henüz eksik)
                if not url or "accounts.google.com" in url:
                    return
                if "notebooklm.google.com" in url:
                    _save_storage_state(reason="framenav")

            try:
                page.on("framenavigated", _on_framenav)
            except Exception:
                pass

            # Chrome about:blank'te açıldı; şimdi Google login'e yönlendir
            try:
                page.goto(
                    f"{DEFAULT_HOMEPAGE}?authuser={authuser}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                emitter.emit("init_nav_warning", error=str(e)[:200])

            emitter.emit(
                "init_waiting_for_close",
                hint="Tarayıcıda Google ile giriş yap, ardından pencereyi kapat.",
            )

            # Chrome'un ölümünü port-polling ile izle + periyodik save.
            # Her 5 sn'de bir state'i yakala — login tamamlandıkça cookies
            # birikecek, son successful save final state'i tutar.
            _last_periodic = 0.0
            while True:
                now = time.time()
                if now - _last_periodic >= 5.0:
                    try:
                        _save_storage_state(reason="periodic")
                    except Exception:
                        pass
                    _last_periodic = now
                try:
                    urllib.request.urlopen(cdp_url, timeout=2)
                    time.sleep(2)
                except (urllib.error.URLError, OSError):
                    # Port cevap vermiyor → chrome öldü
                    break

            # Final save denemesi (genelde context kapanmış olur, periyodik
            # save zaten son state'i yakalamış olmalı)
            try:
                _save_storage_state(reason="final")
            except Exception:
                pass

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        emitter.emit("init_cdp_error", error=str(e))
        try:
            chrome_proc.terminate()
            chrome_proc.wait(timeout=5)
        except Exception:
            try:
                chrome_proc.kill()
            except Exception:
                pass
        return 1

    emitter.emit("init_complete", auth_saved=auth_json.exists())
    return 0


# ---------------------------------------------------------------------------
# OTOMASYON: notebook oluştur, metin ekle, Cinematic video tetikle.
# ---------------------------------------------------------------------------
def select_cinematic_video_overview(
    page,
    emitter: EventEmitter,
    screenshots_dir: Path,
    job_id: str,
    custom_prompt: str = "",
) -> bool:
    """
    Studio panelinden Video Overview kartını tıklar, customize dialog'u açar,
    Cinematic'in seçili olduğunu doğrular (üzerine tıklamaz — toggle eder).
    Eğer custom_prompt verilmişse Custom Prompt alanını doldurur.
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

    # 4) Custom Prompt varsa onu kullan (description'ı override eder).
    # NotebookLM Customize dialog'u tek textarea'lı; custom prompt'umuz role +
    # constraints + source listing içerdiği için "description" yerine bu girer.
    if custom_prompt and custom_prompt.strip():
        cp_ok = apply_custom_prompt_to_cinematic_dialog(
            page=page,
            custom_prompt=custom_prompt,
            emitter=emitter,
            screenshots_dir=screenshots_dir,
            job_id=job_id,
        )
        emitter.emit("custom_prompt_applied", success=cp_ok)
    else:
        # Custom prompt yoksa eski davranış: BOŞ description'ı doldur.
        desc_loc = _find_first_visible(page, DESCRIPTION_INPUT_SELECTORS, timeout_ms=2000)
        if desc_loc is not None:
            try:
                existing = ""
                try:
                    existing = desc_loc.input_value(timeout=1000) or ""
                except Exception:
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


def upload_files_to_notebook(
    page,
    file_paths: list[Path],
    emitter: EventEmitter,
    screenshots_dir: Path,
    job_id: str,
    label: str = "files",
) -> bool:
    """Notebook içinde "+ Add sources" → file input set akışıyla dosyaları yükler.

    Tek toplu upload (multiple file kabul). Style guides + images karışık olabilir.
    Returns True if upload succeeded (modal closed), False on failure.
    """
    if not file_paths:
        return True

    # 1) "+ Add sources" butonuna bas
    add_loc = _find_first_visible(page, ADD_SOURCES_SELECTORS, timeout_ms=20000)
    if add_loc is None:
        emitter.emit("add_sources_button_not_found", label=label)
        _take_screenshot(page, screenshots_dir, f"{job_id}_no_add_sources_{label}")
        return False
    if not _click_with_fallback(add_loc, page):
        emitter.emit("add_sources_click_failed", label=label)
        return False
    emitter.emit("add_sources_clicked", label=label)

    # 2) Modal açılsın diye küçük bekleme
    time.sleep(1.5)

    # 3) Dosyaları yükle — iki stratejiyle (NotebookLM lazy-attach yapıyor)
    paths_str = [str(p.resolve()) for p in file_paths if p.exists()]
    if not paths_str:
        emitter.emit("no_valid_file_paths", label=label)
        return False

    upload_done = False

    # Strateji 1: direkt input[type=file] aramak (bazen DOM'da hidden var)
    file_input_selectors = [
        '[role="dialog"] input[type="file"]',
        'mat-dialog-container input[type="file"]',
        '.cdk-overlay-pane input[type="file"]',
        'input[type="file"]',
    ]
    file_input = None
    for sel in file_input_selectors:
        try:
            cand = page.locator(sel).first
            cand.wait_for(state="attached", timeout=2500)
            file_input = cand
            emitter.emit("file_input_found", selector=sel, label=label,
                         strategy="direct")
            break
        except Exception:
            continue
    if file_input is not None:
        try:
            file_input.set_input_files(paths_str)
            emitter.emit("files_set", count=len(paths_str), label=label,
                         strategy="direct")
            upload_done = True
        except Exception as e:
            emitter.emit("direct_set_input_files_failed", error=str(e), label=label)

    # Strateji 2: "Upload files" butonu + expect_file_chooser
    if not upload_done:
        upload_btn = _find_first_visible(
            page, UPLOAD_FILES_BUTTON_SELECTORS, timeout_ms=8000
        )
        if upload_btn is None:
            emitter.emit("upload_files_button_not_found", label=label)
            _take_screenshot(page, screenshots_dir,
                             f"{job_id}_no_upload_btn_{label}")
            return False
        try:
            with page.expect_file_chooser(timeout=15000) as fc_info:
                try:
                    upload_btn.click(timeout=5000)
                except Exception:
                    upload_btn.evaluate("el => el.click()")
            file_chooser = fc_info.value
            file_chooser.set_files(paths_str)
            emitter.emit("files_set", count=len(paths_str), label=label,
                         strategy="file_chooser")
        except Exception as e:
            emitter.emit("file_chooser_failed", error=str(e), label=label)
            _take_screenshot(page, screenshots_dir,
                             f"{job_id}_filechooser_fail_{label}")
            return False

    # 5) Upload tamamlanmasını bekle (modal kapanması signal)
    time.sleep(5)
    deadline = time.time() + 180  # max 3 dk (PDF/image karışık olabilir)
    last_emit = 0
    while time.time() < deadline:
        modal_visible = False
        for sel in ('[role="dialog"]', 'mat-dialog-container', '.mat-mdc-dialog-container'):
            try:
                if page.locator(sel).first.is_visible(timeout=300):
                    modal_visible = True
                    break
            except Exception:
                continue
        if not modal_visible:
            emitter.emit("upload_modal_closed", label=label)
            return True
        if time.time() - last_emit > 15:
            emitter.emit("upload_in_progress", label=label,
                         elapsed=int(time.time() - (deadline - 180)))
            last_emit = time.time()
        time.sleep(2)

    emitter.emit("upload_timeout_modal_still_open", label=label)
    _take_screenshot(page, screenshots_dir, f"{job_id}_upload_timeout_{label}")
    try:
        page.keyboard.press("Escape")
        time.sleep(1)
    except Exception:
        pass
    return True


# Backward-compat alias — önceki adıyla çağrılan varsa
def upload_image_sources(page, image_paths, emitter, screenshots_dir, job_id):
    return upload_files_to_notebook(
        page, image_paths, emitter, screenshots_dir, job_id, label="images"
    )


def apply_custom_prompt_to_cinematic_dialog(
    page,
    custom_prompt: str,
    emitter: EventEmitter,
    screenshots_dir: Path,
    job_id: str,
) -> bool:
    """Customize Video Overview dialog AÇIKKEN, Custom Prompt alanını doldurur.

    NotebookLM 2 farklı UI'a sahip olabilir:
    (a) Customize dialog içinde ayrı bir "Custom prompt" textarea hazır
    (b) "Custom prompt" butonu var → ona basınca yeni dialog açılıyor

    Önce (a)'yı dene, sonra (b)'yi.
    Returns True if applied, False otherwise (caller fallback yapabilir).
    """
    if not custom_prompt.strip():
        return True

    # Strategy A: Mevcut dialog'da textarea bul ve doldur
    # NotebookLM Customize dialog'u genelde tek textarea'lı, oraya custom prompt
    # girilebiliyor. Description input ile aynı element olabilir.
    direct_loc = None
    for sel in CUSTOM_PROMPT_INPUT_SELECTORS:
        try:
            cand = page.locator(sel).first
            cand.wait_for(state="visible", timeout=2000)
            direct_loc = cand
            emitter.emit("custom_prompt_input_found_direct", selector=sel)
            break
        except Exception:
            continue

    # Strategy B: Eğer A çalışmadıysa "Custom prompt" butonuna bas
    if direct_loc is None:
        btn_loc = _find_first_visible(
            page, CUSTOM_PROMPT_BUTTON_SELECTORS, timeout_ms=3000
        )
        if btn_loc is not None and _click_with_fallback(btn_loc, page):
            emitter.emit("custom_prompt_button_clicked")
            time.sleep(1.0)
            for sel in CUSTOM_PROMPT_INPUT_SELECTORS:
                try:
                    cand = page.locator(sel).first
                    cand.wait_for(state="visible", timeout=3000)
                    direct_loc = cand
                    emitter.emit("custom_prompt_input_found_via_button", selector=sel)
                    break
                except Exception:
                    continue

    if direct_loc is None:
        emitter.emit("custom_prompt_input_not_found")
        _take_screenshot(page, screenshots_dir, f"{job_id}_no_custom_prompt_input")
        return False

    # Doldur — clear + fill
    try:
        try:
            direct_loc.fill("", timeout=2000)
        except Exception:
            pass
        # Çok uzun olabilir, timeout'u uzat
        direct_loc.fill(custom_prompt, timeout=15000)
        emitter.emit("custom_prompt_filled", chars=len(custom_prompt))
    except Exception as e:
        # contenteditable fallback
        try:
            direct_loc.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.insert_text(custom_prompt)
            emitter.emit("custom_prompt_filled_kb", chars=len(custom_prompt))
        except Exception as e2:
            emitter.emit("custom_prompt_fill_failed", error=str(e), error2=str(e2))
            _take_screenshot(page, screenshots_dir, f"{job_id}_prompt_fill_fail")
            return False

    # Eğer ayrı bir custom prompt dialog açıldıysa onu kaydedip kapat
    save_loc = _find_first_visible(
        page, CUSTOM_PROMPT_SAVE_SELECTORS, timeout_ms=1500
    )
    if save_loc is not None:
        try:
            _click_with_fallback(save_loc, page)
            emitter.emit("custom_prompt_saved")
            time.sleep(0.8)
        except Exception:
            pass

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
    image_paths: Optional[list[Path]] = None,
    script_file: Optional[Path] = None,
    style_guide_paths: Optional[list[Path]] = None,
    custom_prompt: str = "",
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
            *_xvfb_args(),
        ]

        # Mod 1: auth.json varsa, paralel-friendly non-persistent context
        # Mod 2: persistent_context (single-instance per user_data_dir)
        try:
            if auth_json.exists():
                emitter.emit("launch_mode", mode="storage_state")
                browser = pw.chromium.launch(headless=headless, args=launch_args, **_launch_kwargs_extra())
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
                    **_launch_kwargs_extra(),
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

            # ===== Phase E: 2 farklı akış =====
            # (a) script_file verilmişse: ilk source'u "Upload files"'tan yükle
            #     (script + style guides + images bir arada). Custom prompt için
            #     source isimleri filename'den geliyor, "Copied text" auto-name
            #     verir (kontrol edemeyiz). Bu yüzden file upload tercih.
            # (b) Yoksa eski akış: "Copied text" → text yapıştır (legacy / dev test)

            if script_file is not None and script_file.exists():
                # YENİ AKIŞ: ilk modal'da "Upload files" → script.txt + (varsa)
                # style guides + images hep birlikte yükle. NotebookLM dosya
                # ismini source ismi olarak kullanıyor, custom prompt'tan ref edilebilir.
                emitter.emit("flow_mode", mode="upload_files_pack")

                # İlk modal otomatik açılıyor (Create new sonrası source seçim).
                # Bu modal'da hidden input[type=file] mevcut.
                first_files: list[Path] = [script_file]
                if style_guide_paths:
                    first_files.extend([p for p in style_guide_paths if p.exists()])
                if image_paths:
                    first_files.extend([p for p in image_paths if p.exists()])

                # NotebookLM modal'ında file input lazy-attached — direkt
                # aramak fail ediyor. İki strateji:
                # 1) Doğrudan input[type=file] var mı? (bazen DOM'da hidden duruyor)
                # 2) Yoksa "Upload files" butonuna bas + expect_file_chooser
                #    ile native picker'ı yakala.
                paths_str = [str(p.resolve()) for p in first_files if p.exists()]
                emitter.emit("first_modal_uploading",
                             count=len(paths_str),
                             files=[Path(p).name for p in paths_str])

                upload_done = False

                # Strateji 1: direkt input[type=file] — kısa timeout, varsa kullan
                file_input = None
                for sel in (
                    '[role="dialog"] input[type="file"]',
                    'mat-dialog-container input[type="file"]',
                    '.cdk-overlay-pane input[type="file"]',
                    'input[type="file"]',
                ):
                    try:
                        cand = page.locator(sel).first
                        cand.wait_for(state="attached", timeout=2500)
                        file_input = cand
                        emitter.emit("first_modal_file_input_found",
                                     selector=sel, strategy="direct")
                        break
                    except Exception:
                        continue
                if file_input is not None:
                    try:
                        file_input.set_input_files(paths_str)
                        emitter.emit("first_modal_files_set",
                                     count=len(paths_str), strategy="direct")
                        upload_done = True
                    except Exception as e:
                        emitter.emit("direct_set_input_files_failed", error=str(e))

                # Strateji 2: "Upload files" butonu + expect_file_chooser
                if not upload_done:
                    upload_btn = _find_first_visible(
                        page, UPLOAD_FILES_BUTTON_SELECTORS, timeout_ms=10000
                    )
                    if upload_btn is None:
                        emitter.emit("upload_files_button_not_found")
                        _take_screenshot(page, screenshots_dir,
                                         f"{job_id}_no_upload_btn")
                        raise RuntimeError(
                            "İlk modal'da Upload files butonu bulunamadı"
                        )
                    emitter.emit("upload_files_button_found")
                    try:
                        with page.expect_file_chooser(timeout=15000) as fc_info:
                            try:
                                upload_btn.click(timeout=5000)
                            except Exception:
                                # JS click fallback
                                upload_btn.evaluate("el => el.click()")
                        file_chooser = fc_info.value
                        file_chooser.set_files(paths_str)
                        emitter.emit("first_modal_files_set",
                                     count=len(paths_str),
                                     strategy="file_chooser")
                        upload_done = True
                    except Exception as e:
                        emitter.emit("file_chooser_failed", error=str(e))
                        _take_screenshot(page, screenshots_dir,
                                         f"{job_id}_filechooser_fail")
                        raise RuntimeError(f"File chooser ile upload başarısız: {e}")

                # Notebook URL'i yakala (upload tamamlanınca redirect olur)
                try:
                    page.wait_for_url(re.compile(r".*/notebook/[^/?#]+"), timeout=180000)
                except Exception:
                    pass

                cur = page.url or ""
                m = re.search(r"/notebook/([^/?#]+)", cur)
                if m:
                    authuser_part = f"?authuser={authuser}"
                    notebook_url = f"https://notebooklm.google.com/notebook/{m.group(1)}{authuser_part}"
                    emitter.emit("notebook_created", notebook_url=notebook_url)

                # Source'ların processing'i bitsin (PDF/image ingest)
                # Source listesi büyük ise 30-60 sn alabilir
                wait_secs = 12 + 3 * len(paths_str)
                emitter.emit("waiting_source_ingest", expected_secs=wait_secs)
                time.sleep(min(wait_secs, 60))

            else:
                # ESKI AKIŞ (legacy): Copied text → text yapıştır
                emitter.emit("flow_mode", mode="copied_text_legacy")

                copied_loc = _find_first_visible(page, COPIED_TEXT_SELECTORS, timeout_ms=20000)
                if copied_loc is None:
                    emitter.emit("copied_text_not_found")
                    _take_screenshot(page, screenshots_dir, f"{job_id}_no_copied_text")
                    raise RuntimeError("Copied text butonu bulunamadı")
                if not _click_with_fallback(copied_loc, page):
                    raise RuntimeError("Copied text butonuna tıklanamadı")
                emitter.emit("copied_text_clicked")

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
                    try:
                        text_loc.click()
                        page.keyboard.insert_text(text)
                    except Exception as e:
                        raise RuntimeError(f"Metin yapıştırma başarısız: {e}")
                emitter.emit("text_inserted", chars=len(text))

                insert_loc = _find_first_visible(page, INSERT_BUTTON_SELECTORS, timeout_ms=10000)
                if insert_loc is None:
                    emitter.emit("insert_button_not_found")
                    _take_screenshot(page, screenshots_dir, f"{job_id}_no_insert")
                    raise RuntimeError("Insert butonu bulunamadı")
                if not _click_with_fallback(insert_loc, page):
                    raise RuntimeError("Insert butonu tıklanamadı")
                emitter.emit("insert_clicked")

                try:
                    page.wait_for_url(re.compile(r".*/notebook/[^/?#]+"), timeout=60000)
                except Exception:
                    pass

                cur = page.url or ""
                m = re.search(r"/notebook/([^/?#]+)", cur)
                if m:
                    authuser_part = f"?authuser={authuser}"
                    notebook_url = f"https://notebooklm.google.com/notebook/{m.group(1)}{authuser_part}"
                    emitter.emit("notebook_created", notebook_url=notebook_url)

                # Legacy mode: image_paths varsa şimdi "+ Add sources" ile ekle
                if image_paths:
                    emitter.emit("legacy_image_upload_starting", count=len(image_paths))
                    upload_files_to_notebook(
                        page=page, file_paths=image_paths,
                        emitter=emitter, screenshots_dir=screenshots_dir,
                        job_id=job_id, label="images",
                    )
                    time.sleep(4)

            # Studio paneli yüklensin diye küçük bir bekleme
            time.sleep(3)

            # Cinematic Video Overview seç ve Generate'e bas
            ok = select_cinematic_video_overview(
                page, emitter, screenshots_dir, job_id,
                custom_prompt=custom_prompt or "",
            )
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
            *_xvfb_args(),
        ]

        try:
            if auth_json.exists():
                emitter.emit("harvest_launch", mode="storage_state")
                browser = pw.chromium.launch(headless=headless, args=launch_args, **_launch_kwargs_extra())
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
                    **_launch_kwargs_extra(),
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
            time.sleep(4)

            # Strateji: NotebookLM Studio panelinin alt kısmında "üretilmiş içerikler"
            # listesi var. Video tamamlandıysa orada bir kart olarak gözüküyor —
            # üzerinde ▶ play butonu, ⋮ menü, başlık, "20h" gibi yaş bilgisi.
            #
            # Akış (sırayla dene, biri tutarsa diğerlerini atla):
            #   0) ⋮ menü → Download seçeneği — en temiz path, doğrudan dosyaya
            #      iner, page.expect_download() ile yakalanır.
            #   1) Sayfada veya Studio panelinde direkt video[src] var mı?
            #   2) Produced-video ▶ play butonunu bul, tıkla, modal/inline
            #      player'da video[src] çıkar, cookie ile fetch et.
            #   3) Hiçbiri tutmadıysa: Video Overview tool kartına tıkla → ya
            #      customize dialog (= hiç üretilmemiş) ya da generating
            #      indicator (= hâlâ üretiliyor) gör — ikisi de "not_ready".

            # === Strateji 0: ⋮ menü → Download ===
            menu_button_selectors = [
                # Studio panelinde produced video item üzerinde "More" butonu
                '[role="complementary"] [aria-label*="More" i]',
                'aside [aria-label*="More" i]',
                '[role="complementary"] button[aria-label*="more" i]',
                'aside button[aria-label*="more" i]',
                # Material Design icon-only "more_vert" butonu
                'button:has(mat-icon:has-text("more_vert"))',
                '[role="button"]:has(mat-icon:has-text("more_vert"))',
                # Generic
                'button[aria-label="More options"]',
                'button[aria-label="More"]',
                'button[aria-label*="seçenek" i]',  # TR: "daha fazla seçenek"
            ]

            download_menu_selectors = [
                '[role="menuitem"]:has-text("Download")',
                '[role="menuitem"]:has-text("İndir")',
                'button:has-text("Download")',
                'button:has-text("İndir")',
                'a:has-text("Download")',
                'a:has-text("İndir")',
                # Material menu
                'mat-menu-item:has-text("Download")',
                'mat-menu-item:has-text("İndir")',
                # Icon + text
                '[role="menuitem"]:has(mat-icon:has-text("download"))',
            ]

            for menu_sel in menu_button_selectors:
                try:
                    menu_loc = page.locator(menu_sel).first
                    menu_loc.wait_for(state="visible", timeout=1500)
                    if not _click_with_fallback(menu_loc, page):
                        continue
                    emitter.emit("harvest_menu_clicked", selector=menu_sel)
                    time.sleep(1)

                    # Menu açıldı — Download seçeneğini bul ve tıklarken
                    # download event'ini yakala
                    for dl_sel in download_menu_selectors:
                        try:
                            dl_loc = page.locator(dl_sel).first
                            dl_loc.wait_for(state="visible", timeout=2000)
                            download_dir.mkdir(parents=True, exist_ok=True)
                            target = download_dir / f"{job_id}.mp4"
                            with page.expect_download(timeout=300_000) as dl_info:
                                _click_with_fallback(dl_loc, page)
                            dl = dl_info.value
                            dl.save_as(str(target))
                            size_mb = target.stat().st_size / (1024 * 1024) if target.exists() else 0
                            emitter.emit(
                                "harvest_downloaded",
                                path=str(target),
                                size_mb=round(size_mb, 2),
                                via="menu_download",
                            )
                            local_path = target
                            # Üretilen dosya adını da kayıt
                            try:
                                emitter.emit(
                                    "harvest_download_meta",
                                    suggested_filename=dl.suggested_filename,
                                    url=dl.url,
                                )
                                # Eğer download URL'i HTTP ise video_url olarak da kaydet
                                if dl.url and dl.url.startswith("http"):
                                    video_url = dl.url
                            except Exception:
                                pass
                            break
                        except Exception:
                            continue

                    if local_path:
                        break
                    # Menü açıldı ama download bulunamadı — escape ile kapat, devam et
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                except Exception:
                    continue

            # === Strateji 1: video[src] direkt sayfada var mı? ===
            # 1) Sayfada zaten <video> var mı? (sadece menü/download başaramazsa)
            if not local_path:
                for sel in ('video[src]', 'video source[src]'):
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="attached", timeout=1500)
                        src = loc.get_attribute("src") or ""
                        if src and (src.startswith("http") or src.startswith("blob:")):
                            video_url = src
                            emitter.emit("harvest_video_inline", src=src[:120])
                            break
                    except Exception:
                        continue

            # 2) Yoksa produced-video play butonunu ara ve tıkla
            if not video_url and not local_path:
                produced_play_selectors = [
                    # Studio panel üretilmiş öğe play butonları
                    '[role="complementary"] [aria-label="Play"]',
                    '[role="complementary"] [aria-label*="Play" i]',
                    '[role="complementary"] button[aria-label*="play" i]',
                    'aside [aria-label="Play"]',
                    'aside button[aria-label*="play" i]',
                    # Material icon play_arrow olan butonlar
                    'button:has(mat-icon:has-text("play_arrow"))',
                    '[role="button"]:has(mat-icon:has-text("play_arrow"))',
                    # Generic
                    'button[aria-label="Play"]',
                    'button[aria-label*="Play video" i]',
                    # SVG play icon
                    'button:has(svg[d*="M8 5v14"])',  # play_arrow SVG path
                ]
                play_clicked = False
                for sel in produced_play_selectors:
                    try:
                        play_loc = page.locator(sel).first
                        play_loc.wait_for(state="visible", timeout=1500)
                        if _click_with_fallback(play_loc, page):
                            emitter.emit("harvest_play_clicked", selector=sel)
                            play_clicked = True
                            break
                    except Exception:
                        continue

                if play_clicked:
                    # Video player açılana kadar bekle, src çıkar
                    time.sleep(2)
                    for sel in ('video[src]', '[role="dialog"] video', 'mat-dialog-container video',
                                '.cdk-overlay-pane video', 'video'):
                        try:
                            vloc = page.locator(sel).first
                            vloc.wait_for(state="visible", timeout=4000)
                            src = vloc.get_attribute("src") or ""
                            if not src:
                                try:
                                    src = page.locator(f"{sel} source").first.get_attribute("src") or ""
                                except Exception:
                                    pass
                            if src and (src.startswith("http") or src.startswith("blob:")):
                                video_url = src
                                emitter.emit("harvest_video_after_play", src=src[:120])
                                break
                        except Exception:
                            continue

            # 3) Hâlâ video yok — Video Overview kartına tıklayarak durumu anla
            #    (still_generating mı, hiç üretilmemiş mi)
            if not video_url and not local_path:
                voloc = _find_first_visible(page, VIDEO_OVERVIEW_SELECTORS, timeout_ms=8000)
                if voloc is None:
                    emitter.emit("harvest_video_card_not_found")
                    _take_screenshot(page, screenshots_dir, f"{job_id}_harvest_no_card")
                    raise RuntimeError("video_card_not_found")

                _click_with_fallback(voloc, page)
                time.sleep(2)

                # Generating indicator?
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
                    exit_code = 2
                    return exit_code

                # Customize dialog açıldıysa = bu notebook'ta hiç video üretilmemiş
                dialog_open = False
                for sel in ('[role="dialog"]', 'mat-dialog-container', '.cdk-overlay-pane'):
                    try:
                        if page.locator(sel).first.is_visible(timeout=500):
                            dialog_open = True
                            break
                    except Exception:
                        continue

                if dialog_open:
                    emitter.emit("harvest_no_video_produced", reason="customize_dialog_opened")
                    _take_screenshot(page, screenshots_dir, f"{job_id}_harvest_no_produced")
                    exit_code = 2  # retry — belki ilerde üretilir
                    return exit_code

                # Bir kez daha video[src] dene (kartla tetiklenmiş olabilir)
                for sel in video_player_selectors:
                    try:
                        locator = page.locator(sel).first
                        locator.wait_for(state="visible", timeout=4000)
                        src = locator.get_attribute("src") or ""
                        if src and (src.startswith("http") or src.startswith("blob:")):
                            video_url = src
                            break
                    except Exception:
                        continue

            # Hâlâ ne local_path ne video_url varsa: ya hiç üretilmemiş ya
            # da still generating — retry.
            if not video_url and not local_path:
                emitter.emit("harvest_video_not_found")
                _take_screenshot(page, screenshots_dir, f"{job_id}_harvest_no_video")
                exit_code = 2
                return exit_code

            # video_url var ama henüz indirmedik (Strateji 0 başarısız oldu,
            # Strateji 1 veya 2 URL buldu) → Phase 2: cookie ile fetch
            if video_url and not local_path:
                emitter.emit("harvest_video_url_found", video_url=video_url)
                try:
                    download_dir.mkdir(parents=True, exist_ok=True)
                    target = download_dir / f"{job_id}.mp4"
                    api = context.request
                    response = api.get(video_url, timeout=300_000)  # 5 dk timeout
                    if response.ok:
                        body = response.body()
                        target.write_bytes(body)
                        size_mb = len(body) / (1024 * 1024)
                        local_path = target
                        emitter.emit(
                            "harvest_downloaded",
                            path=str(target),
                            size_mb=round(size_mb, 2),
                            via="cookie_fetch",
                        )
                    else:
                        emitter.emit(
                            "harvest_download_failed",
                            status=response.status,
                            url=video_url[:120],
                        )
                except Exception as e:
                    emitter.emit("harvest_download_error", error=str(e))

            exit_code = 0 if local_path else 2
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
    # Phase E: image source paths (notebook'a upload edilecek)
    parser.add_argument(
        "--images",
        nargs="*",
        default=[],
        help="Notebook'a Add Sources → Upload akışıyla yüklenecek görsel dosya path'leri",
    )
    # Phase E: script'i .txt dosyasından yükle (Copied text yerine)
    parser.add_argument(
        "--script-file",
        default="",
        help="Script .txt dosyası — varsa Copied text yerine ilk source olarak upload edilir.",
    )
    # Phase E: admin'in style guide kütüphanesi (PDF/MD/DOCX vs.)
    parser.add_argument(
        "--style-guides",
        nargs="*",
        default=[],
        help="Notebook'a attach edilecek reusable style guide dosyaları",
    )
    # Phase E: Cinematic Customize → Custom Prompt'a yapışacak metin
    parser.add_argument(
        "--custom-prompt-file",
        default="",
        help="Custom prompt'u dosyadan oku (CLI escape sorunlarını ortadan kaldırır)",
    )
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

    # Phase E: text boş olabilir (script_file ile yüklenecekse). En az birinden
    # script gelmeli.
    if not args.text.strip() and not args.script_file:
        emitter.emit("error",
                     message="text ya da --script-file gerekli (automation modunda)")
        return 64

    image_paths: list[Path] = []
    for ip in (args.images or []):
        p = Path(ip).resolve()
        if p.exists() and p.is_file():
            image_paths.append(p)
        else:
            emitter.emit("image_path_skipped", path=str(p), reason="not_found")

    style_guide_paths: list[Path] = []
    for gp in (args.style_guides or []):
        p = Path(gp).resolve()
        if p.exists() and p.is_file():
            style_guide_paths.append(p)
        else:
            emitter.emit("style_guide_skipped", path=str(p), reason="not_found")

    script_file: Optional[Path] = None
    if args.script_file:
        p = Path(args.script_file).resolve()
        if p.exists() and p.is_file():
            script_file = p
        else:
            emitter.emit("script_file_skipped", path=str(p), reason="not_found")

    custom_prompt = ""
    if args.custom_prompt_file:
        p = Path(args.custom_prompt_file).resolve()
        if p.exists() and p.is_file():
            try:
                custom_prompt = p.read_text(encoding="utf-8")
            except OSError as e:
                emitter.emit("custom_prompt_read_failed", error=str(e))

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
        image_paths=image_paths or None,
        script_file=script_file,
        style_guide_paths=style_guide_paths or None,
        custom_prompt=custom_prompt,
    )


if __name__ == "__main__":
    sys.exit(main())
