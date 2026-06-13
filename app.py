"""
app.py — NotebookLM Cinematic Studio Streamlit UI + Worker thread.

Çalıştırmak için:
    ./app.sh
veya:
    streamlit run app.py

Architecture özeti:
- @st.cache_resource ile Worker singleton (modül yüklemesinde başlar)
- Worker thread her 2 sn'de _dispatch_round çağırır
- Her job için subprocess.Popen → notebooklm_automator.py
- subprocess stdout'unu parse eder (##JSON## event satırları), state'i günceller
- JSON dosyalarına atomic + thread-safe yazar
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any, Optional

# .env auto-load — secret'lar repo'ya gitmesin diye dosyadan okuyoruz.
# python-dotenv requirements.txt'te var. Yüklenmemişse sessizce devam et.
try:
    from dotenv import load_dotenv
    # APP_DIR henüz tanımlanmadığı için göreli path kullanıyoruz; load_dotenv()
    # default olarak çalışma dizininden başlayıp parent'lara doğru .env arar.
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

import streamlit as st

# Phase 6: teng-lin/notebooklm-py — Python-native NotebookLM API.
# Notebook create + source upload + Cinematic generate + MP4 download tek
# kütüphanede. Önceki tmc/nlm (Go subprocess) + Playwright harvest cycle'i
# tamamen değiştirir. Auth: mevcut chrome_profiles/<id>/auth.json kullanılır.
try:
    from notebooklm_client import (
        NotebookLMClientError,
        submit_job as notebooklm_submit_job,
        smoke_test as notebooklm_smoke_test,
        is_available as notebooklm_is_available,
        auth_path_for,
    )
    _NOTEBOOKLM_AVAILABLE = True
except ImportError as _nb_imp_err:
    _NOTEBOOKLM_AVAILABLE = False
    _nb_imp_err_msg = str(_nb_imp_err)

# Legacy: tmc/nlm Go CLI wrapper. notebooklm-py'a geçiş tamamlandı, fallback
# için kalır. USE_LEGACY_SUBMIT=1 env ile zorla aktif edilebilir.
try:
    from nlm_client import (
        NlmError,
        extract_nlm_cookies,
        fetch_nlm_auth_token,
        nlm_create_notebook,
        nlm_source_add,
        nlm_create_video,
        notebook_web_url,
        nlm_smoke_test,
    )
    _NLM_AVAILABLE = True
except ImportError as _nlm_imp_err:
    _NLM_AVAILABLE = False
    _nlm_imp_err_msg = str(_nlm_imp_err)

# Gemini wrapper — text gen (asset extraction) + görsel üretim (API mode).
try:
    from gemini_client import (
        GeminiError,
        gemini_chat,
        gemini_smoke_test,
        generate_image as gemini_generate_image,
        GEMINI_MODELS,
        GEMINI_DEFAULT_MODEL,
        GEMINI_IMAGE_MODELS,
        GEMINI_IMAGE_DEFAULT,
    )
    _GEMINI_AVAILABLE = True
except ImportError as _gemini_imp_err:
    _GEMINI_AVAILABLE = False
    _gemini_imp_err_msg = str(_gemini_imp_err)

# Bulk import: Drive klasöründen toplu docx → Job (gdown + python-docx).
try:
    from bulk_import import (
        bulk_import_from_drive,
        is_available as bulk_is_available,
        extract_folder_id as bulk_extract_folder_id,
    )
    _BULK_AVAILABLE = True
except ImportError as _bulk_imp_err:
    _BULK_AVAILABLE = False
    _bulk_imp_err_msg = str(_bulk_imp_err)

# ---------------------------------------------------------------------------
# Sabitler ve yollar
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = APP_DIR / "data"
LOGS_DIR = DATA_DIR / "logs"
SCREENSHOTS_DIR = LOGS_DIR / "screenshots"
DOWNLOADS_DIR = DATA_DIR / "downloads"
PROFILES_DIR = APP_DIR / "chrome_profiles"

JOBS_FILE = DATA_DIR / "jobs.json"
BATCHES_FILE = DATA_DIR / "batches.json"
PROFILES_FILE = DATA_DIR / "profiles.json"
DRAFTS_FILE = DATA_DIR / "drafts.json"
USERS_FILE = DATA_DIR / "users.json"
SCRIPT_DRAFTS_FILE = DATA_DIR / "script_drafts.json"  # Phase A in-progress drafts
JOB_ASSETS_DIR = DATA_DIR / "job_assets"  # Phase E: per-job indirilen görseller
STYLE_GUIDES_DIR = DATA_DIR / "style_guides"  # Phase E: admin-managed reusable source dosyaları
GEN_IMAGES_DIR = DATA_DIR / "gen_images"  # Gemini ile üretilen görseller (job'a kopyalanana kadar)
LAUNCHER_LOG = LOGS_DIR / "launcher.log"

# Bugünkü kullanım sayımına dahil olan job durumları. Failed da sayılır —
# yoksa kullanıcı sürekli aynı profili spam'leyip limit aşabilir.
COUNTED_STATUSES = {"running", "generating", "done", "submitted", "failed"}
TERMINAL_STATUSES = {"done", "failed", "submitted", "stopped"}
# generating = automator bitti, NotebookLM Cinematic video üretiyor; harvest bekliyoruz.
# done       = video harvest edildi (+ Azure'a yüklendi); gerçekten tamamlandı.
HARVEST_PICKUP_STATUSES = {"generating", "done"}  # geriye dönük uyum için "done" da

# Environment routing — URL ?env=dev|prod. Worker thread'i bunu module-load
# sırasında okuduğu için ÇOK ERKEN tanımlı olması şart (race condition fix).
ALLOWED_ENVS = ("dev", "prod")
DEFAULT_ENV = os.environ.get("DEFAULT_ENV", "dev").strip().lower()
if DEFAULT_ENV not in ALLOWED_ENVS:
    DEFAULT_ENV = "dev"

DISPATCH_INTERVAL_SEC = 2.0
# GLOBAL eşzamanlı-submission limiti (TÜM hesaplar toplamı). 2-core sunucu
# aynı anda çok "submission" (Chromium aç + notebook+source upload + create-video)
# kaldırmıyor — 8 paralel submit load'u 38'e çıkarmıştı. "running" statüsü =
# aktif submission fazı; generating (Google'da render, yük ~0) sayılmaz. Yani
# çok video AYNI ANDA generating olabilir ama AYNI ANDA ≤N tanesi submit edilir.
GLOBAL_MAX_SUBMITTING = int(os.environ.get("GLOBAL_MAX_SUBMITTING", "2"))
# Aynı anda kaç video TOPLAM in-flight olabilir (running+generating+submitted).
# GLOBAL_MAX_SUBMITTING sadece Chromium-submit fazını sınırlar; submit bitince iş
# "generating"e geçip slotu boşaltıyordu → 11 hesap birden Google'a yüklenip tek
# proxy IP'den burst → "artifact removed". Bu sınır eşzamanlı ÜRETİMİ kısar:
# az-paralel = burst yok. (default 4)
GLOBAL_MAX_INFLIGHT = int(os.environ.get("GLOBAL_MAX_INFLIGHT", "4"))
# "artifact removed" gibi GEÇİCİ Google hatalarında işi kaç kez TAZE (farklı
# hesapta) yeniden deneyeceğiz. Hata olasılıksal (~%24; aynı script bir hesapta
# oluyor, başka denemede olmuyor) → 8 denemede 0.25^8 ≈ %0.001 kalıcı-fail
# (pratikte her iş eninde sonunda üretilir). tried_profiles her denemeyi farklı
# hesaba yönlendirir.
TRANSIENT_RETRY_MAX = int(os.environ.get("TRANSIENT_RETRY_MAX", "8"))
# "artifact removed" retry'larında ARTAN backoff (saniye). İlk 1 deneme HEMEN
# (bağımsız flake'i hızlı yakala), sonrakiler aralıklı → SÜREKLİ kötü pencereyi
# (Google'ın dakikalarca reddettiği anlar; tek proxy IP burst) atlatır. Kanıt:
# 8 hemen-retry 3 dakikada aynı kötü pencerede tükendi → pencere geçince bütçe
# bitmişti. Bu schedule 8 denemeyi ~1.5 saate yayar (pencereyi bekler).
TRANSIENT_BACKOFF_SEC = [0, 60, 180, 600, 1200, 1800, 2700, 3600]
# Profil NotebookLM kota hatası yedikten sonra kaç saat block kalır?
# Google'ın gerçek reset zamanı Pacific time (~07-08:00 UTC) — bizim UTC date
# rollover ile uyumsuz. 8h block + self-correct retry: max 8h overshoot,
# gerçek reset zamanını otomatik bulur.
QUOTA_BLOCK_HOURS = float(os.environ.get("QUOTA_BLOCK_HOURS", "8"))

# Stale "generating" job auto-resume parametreleri (notebooklm-py path için).
# Server restart → thread öldü → MP4 indirilemedi senaryosu için sweeper.
STALE_RESUME_CHECK_INTERVAL_SEC = 5 * 60   # Sweeper her 5dk'da bir tarar
STALE_RESUME_MIN_AGE_SEC = 90 * 60         # 90dk+ "generating" → stuck kabul
STALE_RESUME_MAX_ATTEMPTS = 3              # Job başına max attempt
STALE_RESUME_BACKOFF_SEC = [10*60, 30*60, 60*60]  # 10dk → 30dk → 60dk

# Auth health check parametreleri (proaktif expire tespiti).
# Plan A reactive akışındaki "1. job ziyan" sorununu çözer — periyodik
# smoke_test ile profil expire'larını önceden yakalayıp Slack alert atar.
AUTH_HEALTH_CHECK_INTERVAL_SEC = int(os.environ.get(
    "AUTH_HEALTH_CHECK_INTERVAL_SEC", str(4 * 3600)  # 4 saat default
))
AUTH_HEALTH_PROBE_TIMEOUT_SEC = 25  # smoke_test per profile timeout
JOB_LOG_TAIL_LINES = 400

# Daily özet (günsonu Slack raporu) — günde 1×, belirtilen UTC saatte.
# Default 17:00 UTC = 20:00 İstanbul (gün sonu). Son gönderim tarihi
# data/.last_daily_summary dosyasında tutulur (restart-safe, double-send yok).
DAILY_SUMMARY_HOUR_UTC = int(os.environ.get("DAILY_SUMMARY_HOUR_UTC", "17"))
DAILY_SUMMARY_STATE_FILE = DATA_DIR / ".last_daily_summary"

# Harvest module config — env var ile override edilebilir.
# NotebookLM video üretimi gerçekte 60-90 dk sürüyor (READMEdeki "25-60dk"
# bilgi yanıltıcı). Default'ları gerçek deneyimle hizaladık:
#   - İlk deneme: 60 dk (video minimum bu kadar sürer)
#   - Retry: 10 dk arayla, max 8 deneme → toplam pencere 60 + 80 = 140 dk
#   - Yani ~2.3 saatlik harvest penceresi
HARVEST_FIRST_DELAY_SEC = int(os.environ.get("HARVEST_FIRST_DELAY_MIN", "60")) * 60
HARVEST_RETRY_INTERVAL_SEC = int(os.environ.get("HARVEST_RETRY_INTERVAL_MIN", "10")) * 60
HARVEST_MAX_ATTEMPTS = int(os.environ.get("HARVEST_MAX_ATTEMPTS", "8"))
HARVEST_CHECK_INTERVAL_SEC = 60         # Worker kaç sn'de bir harvest round yapsın

# Azure Blob upload config (env-var gated)
AZURE_CONN = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
AZURE_CONTAINER = os.environ.get("AZURE_CONTAINER", "cinematic-videos").strip()
AZURE_BLOB_PREFIX = os.environ.get("AZURE_BLOB_PREFIX", "videos/").strip()
AZURE_ENABLED = bool(AZURE_CONN)

# Server-side login UI (xvfb + noVNC). Set ise Chromium init virtual display'de
# açılır, admin /vnc/ üzerinden tarayıcıdan görüp login olur — Mac gerekmez.
# Lokal deployment'ta boş bırak (Chromium native pencerede açılır).
HEADLESS_INIT_DISPLAY = os.environ.get("HEADLESS_INIT_DISPLAY", "").strip()
VNC_ENABLED = bool(HEADLESS_INIT_DISPLAY)

# Slack webhook bildirimleri (opsiyonel).
# .env'e SLACK_WEBHOOK_URL=https://hooks.slack.com/services/... ekle.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
SLACK_ENABLED = bool(SLACK_WEBHOOK_URL)

# Lokal init flow için SSH bilgileri (admin panelde "Lokal komut göster" widget'ı
# bu bilgileri kullanarak hazır rsync komutu üretir). Boş bırakırsan rsync
# satırı UI'da gösterilmez, sadece Playwright init komutu çıkar.
LOCAL_INIT_SSH_HOST = os.environ.get("LOCAL_INIT_SSH_HOST", "").strip()
LOCAL_INIT_SSH_KEY = os.environ.get("LOCAL_INIT_SSH_KEY", "~/.ssh/dev-internal-00.pem").strip()
LOCAL_INIT_REMOTE_PATH = os.environ.get(
    "LOCAL_INIT_REMOTE_PATH",
    "/home/ubuntu/notebooklm-cinematic-studio",
).strip()

# Image search: opsiyonel free-tier API keyleri.
# Wikimedia + Openverse key gerektirmez. Pixabay + Pexels free tier ama
# kayıt + key alımı gerekir. Set edilmezse o kaynak skip edilir.
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "").strip()
# Shutterstock: lisanslı stok arama + lisansla/indir. SHUTTERSTOCK_TOKEN =
# licenses.create + purchases.view scope'lu OAuth access token. Set edilmezse
# Shutterstock kaynağı UI'da görünmez (graceful).
SHUTTERSTOCK_TOKEN = os.environ.get("SHUTTERSTOCK_TOKEN", "").strip()
SHUTTERSTOCK_ENABLED = bool(SHUTTERSTOCK_TOKEN)

# Text gen: Gemini CLI üzerinden (Sign-in with Google OAuth).
# UI'da AI özellikleri (script iteration, asset extraction) sadece
# _GEMINI_AVAILABLE True ise gösterilir. Smoke test: admin sidebar widget.
# Eski OpenRouter free-tier listesi kaldırıldı — GEMINI_MODELS gemini_client'te.
LLM_ENABLED = _GEMINI_AVAILABLE  # backward-compat alias (eski koddaki check'ler bozulmasın)

PYTHON_BIN = sys.executable  # venv'in içindeki python

# ---------------------------------------------------------------------------
# Klasörleri ve dosyaları hazırla
# ---------------------------------------------------------------------------
for d in (DATA_DIR, LOGS_DIR, SCREENSHOTS_DIR, DOWNLOADS_DIR, PROFILES_DIR,
          JOB_ASSETS_DIR, STYLE_GUIDES_DIR, GEN_IMAGES_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Atomic, thread-safe JSON I/O
# ---------------------------------------------------------------------------
_FILE_LOCK = threading.RLock()


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Aynı isim tmp file ile race condition oluyor — PID + thread + ts ile unique."""
    with _FILE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = f".{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}.tmp"
        tmp = path.with_suffix(path.suffix + suffix)
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def _read_json(path: Path, default: Any) -> Any:
    with _FILE_LOCK:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default


# ---------------------------------------------------------------------------
# Veri modelleri
# ---------------------------------------------------------------------------
@dataclass
class Profile:
    id: str
    name: str
    authuser: int = 0
    daily_limit: int = 3              # 0 = sınırsız
    max_concurrent: int = 1
    headless: bool = True
    initialized: bool = False
    last_used: float = 0.0
    created_at: float = field(default_factory=time.time)
    # "dev" | "prod" — dispatcher job.environment ile profile.environment eşler.
    # Backward compat: eski profillerde alan yok → load_profiles default "prod"
    # set eder (varolan sistem prod sayılır).
    environment: str = "prod"


@dataclass
class Draft:
    id: str
    title: str
    content: str
    created_at: float = field(default_factory=time.time)


@dataclass
class Job:
    id: str
    title: str
    text: str                         # Final script — NotebookLM'e gönderilen
    profile_id: str = ""
    profile_name: str = ""
    status: str = "queued"           # queued | running | done | failed | submitted | stopped
    notebook_url: str = ""
    error: str = ""
    pid: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    submitted_by: str = ""           # Kullanıcının kendi adı (user view session_state'inden)
    batch_id: str = ""               # Toplu import batch'i (boş = tek job)
    version: int = 0                 # Bulk import versiyon no (1..N); 0 = legacy/tekil
    priority: float = 0.0            # Dispatch önceliği — yüksek önce. Öncelikli batch=time.time() (yeni=üstte); 0=normal FIFO
    auth_retry_count: int = 0        # Auth-fail blip sonrası otomatik re-queue sayısı (bounded <3)
    transient_retry_count: int = 0   # "artifact removed" geçici hata sonrası taze-retry sayacı
    next_dispatch_at: float = 0.0    # queued iş için "bu ana kadar dispatch etme" (transient backoff)
    tried_profiles: list = field(default_factory=list)  # bu iş için denenmiş profil id'leri (farklı-hesap rotasyonu)
    # Audit trail — script iteration geçmişi (Phase A)
    original_script: str = ""        # Kullanıcının ilk yapıştırdığı versiyon (AI iterasyonundan önce)
    iterations: list = field(default_factory=list)  # [{script, feedback, model, ts}, ...]
    # Phase B: extracted assets — her görsel için {id, position, description, query, style}
    assets: list = field(default_factory=list)
    # Phase E: Custom Prompt + style guide source listesi (audit ve admin display için)
    custom_prompt: str = ""          # NotebookLM Cinematic Customize → Custom Prompt'a yapışan metin
    style_guides_used: list = field(default_factory=list)  # [filename, ...] — submit anında attach edilenler
    learning_objectives: str = ""    # _lo.docx companion content (bulk Drive import'tan), opsiyonel
    # "dev" | "prod" — submit anında URL ?env=... parametresinden gelir.
    # Dispatcher sadece aynı environment'taki profillere atar.
    # Backward compat: eski job'larda alan yok → "prod" varsayılır.
    environment: str = "prod"
    # Phase 4: Video Edit / Revize — bu job başka bir job'ı revize ediyorsa
    parent_job_id: str = ""              # Hangi job'ı revize ediyor (boş = orijinal)
    revision_instructions: str = ""      # "Ne değişsin?" (user'ın yazdığı revize prompt'u)
    revision_video_url: str = ""         # Parent'in Azure URL'si (download için referans)
    revision_video_local: str = ""       # İndirilen MP4'ün lokal path'i (job_assets/<id>/_parent.mp4)
    # Harvest (video collection) fields
    video_url: str = ""              # NotebookLM tarafındaki direkt <video src> URL
    video_local_path: str = ""       # data/downloads/<job_id>.mp4 (relative)
    video_remote_url: str = ""       # Azure Blob URL (uploaded sonrası)
    harvest_status: str = "pending"  # pending | checking | ready | downloaded | uploaded | expired | skip
    harvest_attempts: int = 0
    next_harvest_at: float = 0.0
    harvest_error: str = ""


@dataclass
class Batch:
    """Aynı anda kuyruğa eklenen job grubunu temsil eder (Drive toplu import vb.).
    Slack oturum bildirimleri bu veri üzerinden üretilir."""
    id: str
    name: str                           # "Drive · 22.05 14:37"
    source: str                         # Drive URL veya "manuel"
    total: int                          # kuyruğa eklenen toplam job
    submitted_by: str
    created_at: float = field(default_factory=time.time)
    # Bildirim durum takibi
    last_notified_terminal: int = 0    # son bildirim anındaki terminal (done+failed) sayısı
    queued_empty_notified: bool = False # "sırada bekleyen kalmadı" bildirimi gönderildi mi
    notified_complete: bool = False     # oturum özeti gönderildi mi
    quota_wall_notified: bool = False   # batch'in tüm profilleri quota-blocked olunca 1× duvar mesajı
    completed_at: float = 0.0


@dataclass
class User:
    """İç kullanıcı kaydı. Şifreler PBKDF2-HMAC-SHA256 ile hashlenir."""
    username: str
    password_hash: str               # "pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>"
    role: str = "user"               # "admin" | "user"
    display_name: str = ""
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Şifre hash + verify (stdlib pbkdf2_hmac, ek dep gerek yok)
# ---------------------------------------------------------------------------
import hashlib  # noqa: E402
import hmac     # noqa: E402
import base64   # noqa: E402

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256, 16 byte random salt, 32 byte derived key."""
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iters_str, salt_b64, hash_b64 = password_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_str)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
        return hmac.compare_digest(expected, actual)
    except (ValueError, KeyError):
        return False


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------
def load_users() -> list[User]:
    raw = _read_json(USERS_FILE, [])
    out: list[User] = []
    for u in raw:
        try:
            out.append(User(**u))
        except TypeError:
            continue
    return out


def save_users(users: list[User]) -> None:
    _atomic_write_json(USERS_FILE, [asdict(u) for u in users])


def find_user(username: str) -> Optional[User]:
    username = username.strip().lower()
    for u in load_users():
        if u.username.lower() == username:
            return u
    return None


def authenticate(username: str, password: str) -> Optional[User]:
    user = find_user(username)
    if user and verify_password(password, user.password_hash):
        return user
    return None


def ensure_seed_admin() -> None:
    """İlk açılışta users.json yoksa default admin'i oluştur.
    Şifre: ADMIN_PASSWORD env var → yoksa 'changeme' (uyarı yazılır).
    Username: 'admin'."""
    if USERS_FILE.exists():
        return
    seed_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    auto_generated = False
    if not seed_pw:
        # GÜVENLİK: 'changeme' gibi bilinen bir default ASLA kullanma (herkesçe
        # tahmin edilebilir admin = anında devralma). ADMIN_PASSWORD env yoksa
        # rastgele güçlü şifre üret + log/Slack'e BİR kez yaz (admin alsın diye).
        seed_pw = secrets.token_urlsafe(18)
        auto_generated = True
    seed = User(
        username="admin",
        password_hash=hash_password(seed_pw),
        role="admin",
        display_name="Admin",
    )
    save_users([seed])
    if auto_generated:
        launcher_log(
            f"⚠ ADMIN_PASSWORD env YOK — rastgele admin şifresi üretildi: "
            f"{seed_pw}  (username=admin). .env'ye ADMIN_PASSWORD koyup yeniden "
            f"başlat → kalıcı şifre."
        )
        try:
            send_slack_message(
                f"🔐 *Default admin oluşturuldu* (ADMIN_PASSWORD env yoktu).\n"
                f"username: `admin`\nşifre: `{seed_pw}`\n"
                f"_.env'ye ADMIN_PASSWORD ekleyip yeniden başlat._"
            )
        except Exception:
            pass
    else:
        launcher_log("Default admin oluşturuldu (username=admin, ADMIN_PASSWORD env'den).")


# ---------------------------------------------------------------------------
# Profile / Draft / Job repository (JSON file-backed)
# ---------------------------------------------------------------------------
def load_profiles() -> list[Profile]:
    raw = _read_json(PROFILES_FILE, [])
    out: list[Profile] = []
    for p in raw:
        try:
            out.append(Profile(**p))
        except TypeError:
            # Eski formatları skip et
            continue
    return out


def save_profiles(profiles: list[Profile]) -> None:
    _atomic_write_json(PROFILES_FILE, [asdict(p) for p in profiles])


def load_drafts() -> list[Draft]:
    raw = _read_json(DRAFTS_FILE, [])
    out: list[Draft] = []
    for d in raw:
        try:
            out.append(Draft(**d))
        except TypeError:
            continue
    return out


def save_drafts(drafts: list[Draft]) -> None:
    _atomic_write_json(DRAFTS_FILE, [asdict(d) for d in drafts])


def _jobs_backup_path() -> Path:
    return JOBS_FILE.with_name(JOBS_FILE.name + ".bak")


def _load_jobs_raw() -> list:
    """jobs.json'u oku. BOZULMA (parse hatası) → jobs.json.bak'tan kurtar + alarm.
    Atomik yazım partial-write'ı zaten önler; bu disk-bozulması / manuel-edit gibi
    nadir durumlar için son kalkan. Kurtarma olmadan bozuk dosya sessizce [] döner,
    sonraki save onu kalıcılaştırır (veri kaybı) — bunu engeller."""
    with _FILE_LOCK:
        if not JOBS_FILE.exists():
            return []
        # Parse + tip kontrolü. İKİSİ de fail-mode: (a) JSON parse hatası,
        # (b) geçerli JSON ama liste değil (manuel-edit bir dict bırakmış olabilir).
        # Eskiden (b) durumu except'e düşmediği için .bak'a bakmadan [] dönüyordu
        # (sessiz veri kaybı). Şimdi ikisi de kurtarma path'ine gider.
        why = ""
        try:
            data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            why = f"liste değil ({type(data).__name__})"
        except (json.JSONDecodeError, OSError) as e:
            why = f"parse hatası: {e}"

        # --- Kurtarma: bozuk dosyayı her durumda .corrupt'a sakla (forensic),
        # sonra .bak'tan geri yükle. ---
        corrupt_dst = JOBS_FILE.with_name(f"{JOBS_FILE.name}.corrupt.{int(time.time())}")
        try:
            JOBS_FILE.replace(corrupt_dst)
        except OSError:
            corrupt_dst = None
        bak = _jobs_backup_path()
        try:
            if bak.exists():
                data = json.loads(bak.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    _atomic_write_json(JOBS_FILE, data)
                    try:
                        launcher_log(
                            f"⚠ jobs.json BOZUK ({why}) → .bak'tan kurtarıldı "
                            f"({len(data)} iş)")
                        send_slack_message(
                            f"🚨 *jobs.json bozulmuştu* — backup'tan kurtarıldı "
                            f"({len(data)} iş). Bozuk dosya .corrupt olarak saklandı.")
                    except Exception:
                        pass
                    return data
        except (json.JSONDecodeError, OSError):
            pass
        try:
            launcher_log(
                f"⚠ jobs.json BOZUK ve .bak kurtarılamadı ({why}). "
                f"Bozuk kopya: {corrupt_dst.name if corrupt_dst else 'saklanamadı'}")
            send_slack_message(
                f"🚨 *jobs.json bozuk ve backup yok* ({why}) — boş liste ile "
                f"devam ediliyor. Bozuk dosya .corrupt olarak saklandı, elle "
                f"incele.")
        except Exception:
            pass
        return []


def load_jobs() -> list[Job]:
    raw = _load_jobs_raw()
    out: list[Job] = []
    for j in raw:
        try:
            out.append(Job(**j))
        except TypeError:
            continue
    return out


def save_jobs(jobs: list[Job]) -> None:
    payload = [asdict(j) for j in jobs]
    _atomic_write_json(JOBS_FILE, payload)
    # Son-iyi backup — BOŞ listeyle iyi backup'ı ezme (bozuk-load→[]→save zincirinde
    # .bak korunsun ki kurtarma çalışsın).
    if payload:
        try:
            _atomic_write_json(_jobs_backup_path(), payload)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Atomic job mutation — 10+ eşzamanlı notebooklm-py thread'i jobs.json'a
# kilitsiz read-modify-write yapıyordu → lost update (notebook_url kayboluyor,
# "submitted" stuck job'lar). Process-wide threading.Lock ile RMW serialize.
# Lock @st.cache_resource ile process boyunca tek instance (Streamlit modülü
# her run'da re-exec ediyor → module-global lock paylaşılmaz).
# ---------------------------------------------------------------------------
import threading as _threading_for_lock  # noqa: E402


@st.cache_resource
def _jobs_lock() -> "_threading_for_lock.Lock":
    return _threading_for_lock.Lock()


def mutate_jobs(mutator) -> object:
    """Atomic read-modify-write on jobs.json under process-wide lock.

    mutator(jobs_list) — listeyi yerinde değiştirir; opsiyonel değer döndürür.
    Tüm eşzamanlı job state güncellemeleri bunu kullanmalı ki lost update olmasın.

    Örnek:
        def _m(jobs):
            for j in jobs:
                if j.id == jid:
                    j.status = "done"
        mutate_jobs(_m)
    """
    with _jobs_lock():
        jobs = load_jobs()
        result = mutator(jobs)
        save_jobs(jobs)
        return result


def _requeue_job(job_id: str) -> None:
    """Failed bir işi kuyruğa geri al (admin '🔄 Tekrar dene' butonu — SSH yerine).
    profile/error/notebook/timestamp + retry sayaçları temizlenir → dispatcher
    sağlıklı bir hesaba yeniden gönderir. Atomik (mutate_jobs) → race yok."""
    def _m(jobs):
        for j in jobs:
            if j.id == job_id:
                j.status = "queued"
                j.profile_id = ""
                j.profile_name = ""
                j.notebook_url = ""
                j.error = ""
                j.started_at = 0.0
                j.finished_at = 0.0
                j.pid = 0
                j.harvest_status = "pending"
                j.harvest_attempts = 0
                j.next_harvest_at = 0.0
                j.auth_retry_count = 0
                # Video alanlarını da temizle. Aksi halde upload sonrası
                # 'failed' olmuş bir işi requeue edince Step-B dedup işin
                # kendi video_remote_url'ünü görüp onu 'stopped' yapıyor
                # (buton sessizce "tekrar dene" yerine "durdur" oluyordu).
                # + eski lokal MP4 yanlış gösterilmesin.
                j.video_remote_url = ""
                j.video_local_path = ""
                j.video_url = ""
                j.harvest_error = ""
                j.transient_retry_count = 0
                j.next_dispatch_at = 0.0
                j.tried_profiles = []
                break
        return jobs

    mutate_jobs(_m)


def load_batches() -> list[Batch]:
    raw = _read_json(BATCHES_FILE, [])
    out: list[Batch] = []
    for b in raw:
        try:
            out.append(Batch(**{k: v for k, v in b.items() if k in Batch.__dataclass_fields__}))
        except TypeError:
            continue
    return out


def save_batches(batches: list[Batch]) -> None:
    _atomic_write_json(BATCHES_FILE, [asdict(b) for b in batches])


def _fmt_duration_batch(sec: float) -> str:
    """Batch süresi için okunabilir format: '2 gün 14 saat' vb."""
    sec = max(0, int(sec))
    if sec < 3600:
        m = sec // 60
        return f"{m} dakika"
    elif sec < 86400:
        h, m = divmod(sec, 3600)
        return f"{h} saat {m // 60} dakika"
    else:
        d, r = divmod(sec, 86400)
        h = r // 3600
        return f"{d} gün {h} saat"


# ---------------------------------------------------------------------------
# Script drafts (Phase A) — kullanıcının yazmakta olduğu senaryo + iterasyonlar
# Dosya formatı: {username: {script: str, iterations: list, updated_at: float}}
# Refresh kaybetmesin diye disk'e persist edilir, submit'te temizlenir.
# ---------------------------------------------------------------------------
def _load_all_script_drafts() -> dict:
    raw = _read_json(SCRIPT_DRAFTS_FILE, {})
    return raw if isinstance(raw, dict) else {}


def load_script_draft(username: str) -> Optional[dict]:
    """Bu kullanıcının kayıtlı draft'ını döndür (None = yok)."""
    if not username:
        return None
    all_drafts = _load_all_script_drafts()
    d = all_drafts.get(username)
    if not isinstance(d, dict):
        return None
    return d


def save_script_draft(username: str, script: str, iterations: list,
                      assets: Optional[list] = None,
                      custom_prompt: Optional[str] = None,
                      custom_prompt_edited: bool = False,
                      learning_objectives: Optional[str] = None) -> None:
    """Kullanıcının draft'ını disk'e yaz."""
    if not username:
        return
    all_drafts = _load_all_script_drafts()
    all_drafts[username] = {
        "script": script or "",
        "iterations": iterations or [],
        "assets": assets or [],
        "custom_prompt": custom_prompt or "",
        "custom_prompt_edited": bool(custom_prompt_edited),
        "learning_objectives": learning_objectives or "",
        "updated_at": time.time(),
    }
    _atomic_write_json(SCRIPT_DRAFTS_FILE, all_drafts)


def clear_script_draft(username: str) -> None:
    """Kullanıcının draft'ını sil (submit veya manuel sıfırla sonrası)."""
    if not username:
        return
    all_drafts = _load_all_script_drafts()
    if username in all_drafts:
        del all_drafts[username]
        _atomic_write_json(SCRIPT_DRAFTS_FILE, all_drafts)


# ---------------------------------------------------------------------------
# Style Guides (Phase E) — admin'in upload ettiği reusable source dosyaları.
# Her job submit'inde otomatik notebook'a "Add sources" ile attach edilir.
# Dosya isimleri = NotebookLM'deki source ismi (Custom Prompt'tan ref edilir).
# ---------------------------------------------------------------------------
# NotebookLM kabul ettiği tipler: pdf, txt, md, docx, image (jpg/png), audio (mp3/m4a)
STYLE_GUIDE_ALLOWED_EXTS = {
    ".pdf", ".txt", ".md", ".docx", ".doc",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".mp3", ".m4a", ".wav",
}


def list_style_guides() -> list[dict]:
    """Disk'teki style guide dosyalarını listele (sıralı)."""
    out = []
    for p in sorted(STYLE_GUIDES_DIR.glob("*")):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
            out.append({
                "name": p.name,
                "path": str(p),
                "size": stat.st_size,
                "uploaded_at": stat.st_mtime,
            })
        except OSError:
            continue
    return out


def save_style_guide(filename: str, data: bytes) -> tuple[bool, str]:
    """Bytes'i style_guides/ altına yaz. (ok, error_or_path) döner."""
    if not filename:
        return False, "Dosya adı boş."
    # Path traversal koruma
    safe_name = Path(filename).name  # parent path'leri at
    ext = Path(safe_name).suffix.lower()
    if ext not in STYLE_GUIDE_ALLOWED_EXTS:
        return False, f"Tip desteklenmiyor: {ext}. Kabul: {', '.join(sorted(STYLE_GUIDE_ALLOWED_EXTS))}"
    if len(data) > 30 * 1024 * 1024:  # 30MB üst sınır
        return False, "Dosya 30MB'den büyük."
    dest = STYLE_GUIDES_DIR / safe_name
    try:
        dest.write_bytes(data)
        return True, str(dest)
    except OSError as e:
        return False, f"Yazma hatası: {e}"


def delete_style_guide(filename: str) -> bool:
    """Dosyayı sil. Path traversal'a dirençli."""
    safe_name = Path(filename).name
    p = STYLE_GUIDES_DIR / safe_name
    if not p.exists() or not p.is_file():
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Custom Prompt template — NotebookLM Cinematic Customize → Custom Prompt
# alanına yapışan metin. {{...}} placeholder'ları submit anında doldurulur.
# ---------------------------------------------------------------------------
DEFAULT_CUSTOM_PROMPT_TEMPLATE = """Role: You are a specialized Educational Video Producer.

Task: Generate a cinematic video overview based on the provided Script and Learning Objectives.

Core Constraints (STRICT ADHERENCE REQUIRED):
1. Zero-Text Visuals: Absolutely no text, labels, or logos in the frame. Use visual metaphors or color-coding only.
2. Verbatim Narration: The audio must follow the provided script exactly; do not summarize, add intros, or include concluding remarks.
3. Historical & Identity Fidelity: Use the Historical Accuracy & Identity Protocol to ensure correct ethnicity, age, and real-world image integration.
4. Compositional Logic: Follow the Spatial Simplicity Rule. Isolate one central subject per scene with significant white space to maintain clarity.
5. Style Ratio: [Fully Realistic Style] — Ensure no hybrid clutter; do not mix photos and illustrations in a single frame.
6. Video Length: Video must be under 3 minutes long UNDER ALL CIRCUMSTANCES!

Required Sources for this Task:
{{SOURCES_LIST}}"""


def _format_source_image_line(idx: int, asset: dict, base_name: str) -> str:
    """Bir image source için tek satır listing inşa et: filename + description + position."""
    desc = (asset.get("description") or "").strip()
    pos = (asset.get("position") or "").strip()
    line = f"Source {idx}: {base_name}"
    if desc and pos:
        line += f" — visual: {desc}; show when narration says: \"{pos}\""
    elif desc:
        line += f" — visual: {desc}"
    elif pos:
        line += f" — show when narration says: \"{pos}\""
    return line


def _safe_filename_from_query(query: str, fallback: str = "image") -> str:
    """Asset query'sinden filesystem-safe bir base name türet (uzantı yok)."""
    import re as _re
    q = (query or "").strip().lower()
    if not q:
        q = fallback
    # Sadece alfanümerik + boşluk + tire, sonra alt-tire
    cleaned = _re.sub(r"[^a-z0-9\s-]", "", q)
    cleaned = _re.sub(r"\s+", "_", cleaned).strip("_-")
    return (cleaned[:48] or fallback)


def build_source_listing(script_title: str, assets: list,
                          has_learning_objectives: bool = False) -> tuple[str, list[str]]:
    """Custom prompt'ta listelenecek source isimleri + numaralandırma.

    Sıra NotebookLM upload sırasıyla bire bir uyumlu:
      [1] <Title>_Script.txt
      [2] <Title>_LearningObjectives.txt (opsiyonel — _lo.docx companion)
      [3] Narrative & Text-Free Execution Guide
      [4] Historical Accuracy & Identity Protocol
      [5] Fully Realistic Style
      [6] _custom_prompt.txt (this Task Brief)
      [7..N] Selected images

    Image'lar için description + position eklenir → NotebookLM her görseli
    script'in hangi anında göstereceğini bilir.

    Returns (formatted_listing_text, ordered_source_names).
    """
    lines: list[str] = []
    names: list[str] = []

    # Source 1: Script
    script_name = f"{script_title}_Script" if script_title else "Script"
    lines.append(f"Source 1: {script_name} — verbatim narration content")
    names.append(script_name)

    # Source 2: Learning Objectives (opsiyonel companion)
    if has_learning_objectives:
        lo_name = f"{script_title}_LearningObjectives" if script_title else "LearningObjectives"
        lines.append(
            f"Source 2: {lo_name} — pedagogical aims and learning outcomes "
            "for this video."
        )
        names.append(lo_name)

    # Sources 3-5: 3 ayrı execution guide protokolü (sabit, her job'a)
    lines.append("Source 3: Narrative & Text-Free Execution Guide")
    names.append("_Narrative_TextFree_Guide")
    lines.append("Source 4: Historical Accuracy & Identity Protocol")
    names.append("_HistoricalAccuracy_Identity")
    lines.append("Source 5: Fully Realistic Style")
    names.append("_FullyRealistic_Style")

    # Source 6: Task Brief (this prompt — Role/Task/Constraints; uploaded as source
    # so NotebookLM can reference it directly, redundancy with Customize field)
    lines.append(
        "Source 6: Task Brief (this document) — "
        "Role/Task/Constraints/Required Sources."
    )
    names.append("_custom_prompt")

    # Source 8+: Selected images with description + position mapping
    for i, a in enumerate(assets):
        sel = a.get("selected_image") or {}
        if not sel.get("full_url") and not sel.get("thumb_url"):
            continue
        base = _safe_filename_from_query(a.get("query", ""), fallback=f"image_{i+1}")
        full_name = f"{base}_{i+1:02d}"
        idx = len(names) + 1
        lines.append(_format_source_image_line(idx, a, full_name))
        names.append(full_name)

    return ("\n".join(lines), names)


def render_custom_prompt(template: str, script_title: str,
                          assets: list,
                          has_learning_objectives: bool = False) -> str:
    """Template'teki {{SOURCES_LIST}} placeholder'ını doldur."""
    listing, _ = build_source_listing(
        script_title, assets,
        has_learning_objectives=has_learning_objectives,
    )
    return template.replace("{{SOURCES_LIST}}", listing)


# ---------------------------------------------------------------------------
# Sabit execution guide — 4 ayrı protokol olarak NotebookLM'e source upload
# edilir. Her job'a otomatik. Tek dosya yerine 4 ayrı dosya: NotebookLM source
# panelinde her protokol ayrı görünür, Cinematic gen her sahnede 4 ayrı kuralı
# referans alır (daha güçlü prime).
# ---------------------------------------------------------------------------
EXEC_GUIDE_NARRATIVE_TEXT_FREE = """Narrative & Text-Free Execution Guide

Verbatim Narration: The audio must follow the provided script exactly; do not summarize, add intros, or include concluding remarks.
Zero-Text Policy: Absolutely no letters, numbers, labels, or titles are permitted in the frame.
Symbolic Replacement: Use color-coded icons, focal shifts, or zooms to highlight specific parts of a subject instead of using text labels.
Language Barrier: Do not generate any text overlays to ensure the video is ready for immediate localization."""

EXEC_GUIDE_HISTORICAL_ACCURACY = """Historical Accuracy & Identity Protocol

Authentic Representation Visual Fidelity: When the script identifies a specific historical or real-world figure, all visual depictions—regardless of artistic style—must accurately reflect that individual's documented ethnicity, age, and identity.
Contextual Accuracy: Any specific locations, tools, or environments mentioned must be represented in a way that respects the historical or geographical reality of the narrative.
Primary Source Integration Real-Image Mandate: For any specific real-world subject (person or place) featured in the script, the video must include at least 1-2 appearances of an actual primary source image (e.g., an authentic photograph, a verified portrait, or a contemporary document).
Strategic Placement: These authentic images should be timed to coincide with the introduction or a significant point of the subject within the narration.
Stylistic Continuity Visual Bridge: The "Illustrated/Animated" versions of a subject must maintain recognizable visual features consistent with real-life person.
Safety-Adjusted History Visual Correction: Primary source images that are naturally dark, high-contrast, or grainy must be adjusted to align with High-Key lighting standards. Lift shadows to ensure the image is clear and non-threatening for a student audience."""

EXEC_GUIDE_REALISTIC_STYLE = """Fully Realistic Style Guide

Photorealistic Style: Apply a Photorealistic, Cinematic style as the absolute baseline for all standard scenes across any topic. Visuals must look like high-definition, documentary photography or professional, real-world documentary footage. Ensure a welcoming atmosphere using continuous camera motion, bright high-key lighting with lifted shadows, and a clear background exit point.
Real-World Texture Allowance: To maintain true documentary realism, allow for the natural rendering of real-world textures, complex environments, and authentic details.
Visual Safety Baseline: Avoid frightening, horror-themed, or overtly scary imagery. Do not use visual styles reminiscent of thrillers, horror movies, or dark fantasy. Stick to standard, neutral documentary framing and avoid creepy, highly distorted, or intimidating camera angles.
Realistic Environments: Ground all scenes in normal, recognizable, everyday real-world settings. Do not use stylized, giant, cavernous, or empty futuristic architecture that looks like a sci-fi prison, abstract maze, or dystopian bunker. Keep indoor spaces naturally proportioned and realistically lit."""

# Source numaralandırması content ekibinin güncel template'ine uygun (3 sabit guide):
# Source 3: Narrative & Text-Free Execution Guide
# Source 4: Historical Accuracy & Identity Protocol
# Source 5: Fully Realistic Style
# (NOT: Student Safety & Visual Harmony + 80/20 Animation-Heavy kaldırıldı —
#  content ekibi "fully realistic" yönüne geçti; safety guide realistic visuals'ı
#  engelliyordu.)
EXECUTION_GUIDE_FILES: list[tuple[str, str]] = [
    ("_03_Narrative_TextFree_Guide.txt", EXEC_GUIDE_NARRATIVE_TEXT_FREE),
    ("_04_HistoricalAccuracy_Identity.txt", EXEC_GUIDE_HISTORICAL_ACCURACY),
    ("_05_FullyRealistic_Style.txt", EXEC_GUIDE_REALISTIC_STYLE),
]

# Backward-compat: bazı yerler hâlâ EXECUTION_GUIDE_PROMPT'a refer ediyor olabilir
EXECUTION_GUIDE_PROMPT = "\n\n".join(text for _, text in EXECUTION_GUIDE_FILES)


def write_execution_guide_sources(out_dir: Path) -> list[Path]:
    """4 ayrı protokol dosyasını yaz, path listesi dön.

    Her dosya NotebookLM'e ayrı source olarak yüklenir. Sıra user'ın
    template'iyle uyumlu (Narrative → Historical → Safety → Animation).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, text in EXECUTION_GUIDE_FILES:
        p = out_dir / filename
        try:
            p.write_text(text, encoding="utf-8")
            written.append(p)
        except OSError:
            pass
    return written


# ---------------------------------------------------------------------------
# Stale-state cleanup: streamlit crash sonrası "running" kalan job'lar
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cleanup_stale_jobs() -> int:
    """Streamlit/servis restart sonrası "running" kalan job'ları temizle.

    İKİ farklı job tipi var:
    - Subprocess (legacy tmc/nlm + Playwright): j.pid > 0. PID ölüyse stale.
    - Thread-based (notebooklm-py): pid=0 (subprocess yok, thread'de koşar).
      running→generating geçişi saniyeler sürdüğü için pid=0'ı ANINDA ölü
      saymak FALSE POSITIVE yaratıyordu ("Process kayboldu" — notebook bile
      yaratılmış job'lar failed işaretleniyordu). pid=0 job sadece çok uzun
      (15dk+) "running" kaldıysa stale say.
    """
    jobs = load_jobs()
    changed = 0
    now = time.time()
    STALE_THREAD_SEC = 15 * 60
    for j in jobs:
        if j.status != "running":
            continue
        stale = False
        if j.pid and j.pid > 0:
            if not _pid_alive(j.pid):  # subprocess öldü
                stale = True
        else:
            # Thread-based (pid=0): running→generating saniyeler sürer. 15dk+
            # running kaldıysa thread gerçekten öldü (restart vb.) → stale.
            if (now - (j.started_at or j.created_at)) > STALE_THREAD_SEC:
                stale = True
        if not stale:
            continue
        # Thread/process kayboldu = ALTYAPI hatası (restart/çökme), içerik/hesap
        # hatası DEĞİL → hard-fail YERİNE taze retry farklı hesapta (kullanıcının
        # "her hata → başka hesapla dene" mantığı). tried_profiles korunur.
        n = getattr(j, "transient_retry_count", 0)
        if n < TRANSIENT_RETRY_MAX:
            j.status = "queued"
            j.profile_id = ""
            j.profile_name = ""
            j.notebook_url = ""
            j.error = ""
            j.started_at = 0.0
            j.finished_at = 0.0
            j.pid = 0
            j.harvest_status = "pending"
            j.harvest_attempts = 0
            j.next_harvest_at = 0.0
            j.transient_retry_count = n + 1
            j.next_dispatch_at = 0.0
        else:
            j.status = "failed"
            j.error = (j.error or
                       f"thread/process kayboldu ({TRANSIENT_RETRY_MAX} deneme tükendi)")
            j.finished_at = time.time()
        changed += 1
    if changed:
        save_jobs(jobs)
    return changed


# ---------------------------------------------------------------------------
# Logging — launcher.log + her job için ayrı log
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()


def launcher_log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    with _log_lock:
        try:
            # Rotasyon: >5MB → launcher.log.1'e taşı (tek yedek), taze dosya başlat.
            # Sınırsız büyümeyi önler (eskiden append-only → GB'lara çıkabiliyordu).
            if (LAUNCHER_LOG.exists()
                    and LAUNCHER_LOG.stat().st_size > 5 * 1024 * 1024):
                LAUNCHER_LOG.replace(LAUNCHER_LOG.with_name(LAUNCHER_LOG.name + ".1"))
            with LAUNCHER_LOG.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def job_log_path(job_id: str) -> Path:
    return LOGS_DIR / f"{job_id}.log"


def init_log_path(profile_id: str) -> Path:
    return LOGS_DIR / f"init_{profile_id}.log"


def harvest_log_path(job_id: str) -> Path:
    return LOGS_DIR / f"harvest_{job_id}.log"


# ---------------------------------------------------------------------------
# Azure Blob upload (Phase 3) — env var ile gated, opsiyonel.
# AZURE_STORAGE_CONNECTION_STRING ve AZURE_CONTAINER set edilmediyse skip.
# ---------------------------------------------------------------------------
def _extract_sas_from_conn(conn: str) -> str:
    """SAS-based connection string'lerden SharedAccessSignature param'ını çıkarır.
    Account-key'li connection string'de SAS olmaz, boş döner."""
    m = re.search(r"SharedAccessSignature=([^;]+)", conn)
    return m.group(1) if m else ""


def _azure_prefix_for_env(env: str) -> str:
    """Env'e göre Azure blob prefix. Default 'videos/' (prod) — env subfolder
    eklenir: dev → 'videos/dev/', prod → 'videos/prod/'. Backward compat:
    eski 'videos/' prefix'i prod sayılır (manuel migration gerekirse yapılır).
    """
    base = AZURE_BLOB_PREFIX.rstrip("/")
    e = (env or "prod").strip().lower()
    if e not in ALLOWED_ENVS:
        e = "prod"
    return f"{base}/{e}"


def _esc(s) -> str:
    """HTML escape — kullanıcı içeriğini (job başlığı/Drive dosya adı, gönderen,
    display_name) unsafe_allow_html render'larında XSS'e karşı korur. quote=True
    olduğu için title="..." gibi attribute context'lerinde de güvenli."""
    return html.escape(str(s) if s is not None else "")


def _sanitize_blob_stem(title: str) -> str:
    """Title → blob/dosya-güvenli stem (uzantısız). Boşluk → _, sadece
    alfanumerik + . _ - kalır (Türkçe karakterler ASCII'ye çevrilir)."""
    s = (title or "").strip()
    # Türkçe karakterleri ASCII'ye indir (dosya adı portability)
    _tr = {"ç": "c", "Ç": "C", "ğ": "g", "Ğ": "G", "ı": "i", "İ": "I",
           "ö": "o", "Ö": "O", "ş": "s", "Ş": "S", "ü": "u", "Ü": "U"}
    for k, v in _tr.items():
        s = s.replace(k, v)
    s = s.replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9._-]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:120] or "untitled"


def azure_blob_basename_for_job(job_id: str,
                                jobs: Optional[list] = None) -> str:
    """Job için Azure blob stem'i (uzantısız) hesapla.

    Title bazlı: 'Mebbty 6 Bty 6 1.6.6.2.0.2 En Discoverybit' →
    'Mebbty_6_Bty_6_1.6.6.2.0.2_En_Discoverybit'.

    Aynı title'da birden fazla gerçek çıktı (done veya video'su olan) varsa
    her birine created_at sırasına göre _v1/_v2/_v3 eklenir; tek ise suffix yok.
    Job bulunamazsa fallback olarak job_id döner.
    """
    if jobs is None:
        jobs = load_jobs()
    target = next((j for j in jobs if j.id == job_id), None)
    if target is None:
        return job_id
    title = (target.title or "").strip()
    stem = _sanitize_blob_stem(title)
    # Aynı title'ın gerçek çıktıları (done ya da video'su olan).
    # ENV filtresi: blob path zaten 'videos/<env>/' ile ayrık, _closed_job_url
    # da env-filtreli rank hesaplıyor. Burada env filtresi YOKKEN bir dev-test
    # job'u aynı title'da prod job'un rank'ını kaydırıp 'X.mp4' yerine 'X_v2.mp4'
    # yazdırıyordu (link uyumsuzluğu). Aynı env içinde rank → tutarlı.
    _tenv = (target.environment or "prod").strip().lower()
    siblings = [
        j for j in jobs
        if (j.title or "").strip() == title
        and (j.environment or "prod").strip().lower() == _tenv
        and (j.status == "done" or j.video_remote_url or j.video_local_path)
    ]
    if len(siblings) <= 1:
        return stem
    siblings.sort(key=lambda j: j.created_at)
    rank = next((i for i, j in enumerate(siblings, 1) if j.id == job_id),
                len(siblings))
    return f"{stem}_v{rank}"


def upload_to_azure(local_path: Path, job_id: str,
                    environment: str = "prod",
                    blob_basename: Optional[str] = None) -> tuple[bool, str, str]:
    """Returns (success, remote_url_or_empty, error_or_empty).
    SAS-based connection ise döndürdüğü URL'e SAS append'lenir →
    private container'da bile direkt browser'da oynatılabilir.

    environment='dev'|'prod' → blob path 'videos/<env>/<name>.mp4'.
    blob_basename verilirse dosya adı o olur (title-bazlı, okunabilir);
    None ise azure_blob_basename_for_job(job_id) ile title'dan türetilir.
    """
    if not AZURE_ENABLED:
        return False, "", "azure_disabled"
    try:
        # azure-storage-blob opsiyonel — eksikse graceful fail
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError:
        return False, "", "azure-storage-blob package not installed"

    try:
        env_prefix = _azure_prefix_for_env(environment)
        stem = blob_basename or azure_blob_basename_for_job(job_id)
        if not stem:
            stem = job_id
        blob_name = f"{env_prefix}/{stem}.mp4"
        svc = BlobServiceClient.from_connection_string(AZURE_CONN)
        container = svc.get_container_client(AZURE_CONTAINER)
        # Container yoksa oluşturmaya çalış (yetkisi varsa). Yoksa next call fail eder.
        try:
            container.create_container()
        except Exception:
            pass
        blob = container.get_blob_client(blob_name)
        with local_path.open("rb") as f:
            blob.upload_blob(
                f,
                overwrite=True,
                content_settings=ContentSettings(content_type="video/mp4"),
            )

        base_url = blob.url
        # GÜVENLİK: Paylaşılan video linki READ-ONLY SAS taşısın. Upload AZURE_CONN
        # (rwdla, 2-yıl) ile yapılır AMA döndürülen URL'e — varsa — AZURE_READONLY_SAS
        # (yalnız Read+List) eklenir. Böylece Slack'te/link'te paylaşılan URL ile
        # kimse container'a yazamaz/silemez. .env'e AZURE_READONLY_SAS koyulunca
        # otomatik devreye girer; yoksa eski (rwdla) davranışa düşer.
        ro_sas = os.environ.get("AZURE_READONLY_SAS", "").strip().lstrip("?")
        if ro_sas:
            clean = base_url.split("?", 1)[0]  # blob.url'deki mevcut (rwdla) SAS'i at
            return True, f"{clean}?{ro_sas}", ""
        # azure-storage-blob SAS-based credential ile init edildiğinde blob.url
        # zaten SAS içerebilir. Duplicate eklemekten kaçın: ?sv= veya &sig=
        # zaten varsa olduğu gibi döndür.
        if "sig=" in base_url:
            return True, base_url, ""
        sas = _extract_sas_from_conn(AZURE_CONN)
        if sas:
            # SAS-based connection: URL'e ekle → tarayıcıda direkt oynanabilir.
            # Note: AZURE_READONLY_SAS koyulmadıysa bu URL SAS'in tüm scope'u
            # (rwdla) ile erişir → read-only SAS ekle (yukarıda) önerilir.
            sep = "&" if "?" in base_url else "?"
            return True, f"{base_url}{sep}{sas}", ""
        # Account-key veya public container: base URL yeterli
        return True, base_url, ""
    except Exception as e:
        return False, "", str(e)[:300]


# ---------------------------------------------------------------------------
# OpenRouter LLM client — script iteration, asset extraction
# ---------------------------------------------------------------------------
# NOTE: _openrouter_chat() kaldırıldı. Text gen artık gemini_client.gemini_chat()
# üzerinden, OAuth (Sign-in with Google) ile yapılıyor. 3 caller (regenerate_script,
# generate_script_from_prompt, extract_assets) direkt gemini_chat() çağırıyor.


# Sistem prompt'u (her script regenerate'de prepend edilir).
# İleride config dosyasından okunabilir.
SCRIPT_EDITOR_SYSTEM = """You are an expert video script editor for the "Weird Fact" short-form format on YouTube/social media. Your job is to revise scripts based on user feedback while keeping these principles:

- Hook in the first 3-5 seconds
- Conversational, energetic narration
- Concrete facts and numbers, no fluff
- Clear visual cues (so animation/imagery is obvious)
- Tight pacing — every sentence earns its place
- End with a memorable kicker, not a generic CTA

Output ONLY the revised script text. No preamble, no explanation, no markdown headers — just the script ready to be sent to NotebookLM. Match the language of the input script (Turkish input → Turkish output)."""


def regenerate_script(current_script: str, feedback: str,
                      model: Optional[str] = None) -> tuple[bool, str]:
    """Mevcut scripti feedback'e göre yeniden üretir. (success, new_script_or_error) döner.

    Gemini CLI üzerinden çalışır (oauth). Eski OpenRouter yolu kaldırıldı.
    """
    if not feedback.strip():
        return False, "Feedback boş olamaz."
    user_prompt = f"""CURRENT SCRIPT:
{current_script.strip()}

USER FEEDBACK (apply these changes):
{feedback.strip()}

Generate the revised script."""
    return gemini_chat(
        user_prompt,
        model=model,
        system_prompt=SCRIPT_EDITOR_SYSTEM,
        temperature=0.8,
        max_tokens=3000,
        timeout=300,  # pro + uzun script 2-3dk olabilir
    )


# ---------------------------------------------------------------------------
# Phase 1 (Step 1): Script generation from prompt
# Kullanıcı text area'ya prompt yapıştırır → "Çıktı oluştur" → LLM script verir.
# Ya da Weird Facts template'ini form'la doldurup oradan üretir.
# ---------------------------------------------------------------------------

# Weird Facts system prompt — Twin Learning Vision'ın script writer'ı.
# {TOPIC}/{GRADE_LEVEL}/{LANGUAGE}/{LEARNING_OBJECTIVE} placeholder'ları
# render_weird_facts_prompt() tarafından doldurulur.
WEIRD_FACTS_TEMPLATE = """You are the Twin Learning Vision Weird Facts Script Writer.

Your job is to produce a single narration script in the Weird Facts format. The script will be sent to NotebookLM, which will generate the final video. Write only what should be spoken aloud — no scene directions, no shot lists, no headers, no markdown, no on-screen text cues.

WHAT "WEIRD FACTS" IS
A short, single-narrator video script that lands ONE surprising, counter-intuitive idea tied to a curriculum learning objective. It is written for a curious student, not a textbook. The script is a short narrative — not a topic summary, not a definition list. ONE strong "weird fact" is the spine of the script from the opening question all the way to the closing line. Every paragraph should still be feeding that one fact.

If your draft starts to feel like "Topic: X. Definition: Y. Examples: Z." — stop and rewrite. The voice should feel like a friend who just learned something cool and is telling you about it, not like a teacher delivering a lesson plan.

THE ARC

1) HOOK QUESTION — a real mystery, not a yes/no opener.
Open with a question that creates genuine intrigue: a concrete puzzle, an absurd-sounding-but-true situation, a counter-intuitive image, or a tiny scenario the viewer can picture. The viewer should think "wait, really?" — not "obviously yes" or "obviously no". Avoid generic conversational openers. The hook should already point at the weird fact.

2) THE WEIRD FACT ITSELF — within the first 2–3 sentences.
Deliver the surprising claim quickly. Do not bury it. Do NOT use the literal words "weird fact" or "tuhaf gerçek" in the script — the surprise should land through the content, not through a label.

3) WHY IT'S TRUE — woven, not listed.
Explain the science or reasoning in plain language a student at the target grade can follow. This is where the learning objective gets covered — but weave it into the story of the weird fact. Do NOT march through the LO's sub-items one by one. Choose ONE concrete situation and let the sub-items surface naturally.

4) REAL-WORLD TIE-IN — concrete, age-appropriate.
Connect the fact to something tangible from the student's real life at that grade level.

5) ENGINEERING / SCIENTIFIC FRAMING + CALLBACK TO THE HOOK.
End with the bigger picture: what humans do with this knowledge. The closing must call back to the puzzle raised in the hook.

6) SIGN-OFF — warm, topic-specific.
Address the viewer directly. Brand convention: "Twinner" in English or "Tivinır" in Turkish. The sign-off line MUST be specific to this script's topic.

HARD RULES (non-negotiable)
• Output one language only, matching the requested LANGUAGE.
• No on-screen text. Convey everything through spoken narration.
• No bullet lists, no headers, no markdown, no stage directions. Continuous prose only.
• First sentence is always a question — and a question that creates real intrigue.
• Target length: 220–300 words. Do not exceed 320 words.
• Cover the learning objective directly. Use the EXACT scientific terms from the LEARNING OBJECTIVE — no synonyms.
• Match the grade level. Define any technical term a student that age wouldn't know.
• One narrator, one voice. No dialogue.
• Scientific accuracy is non-negotiable. Simplification must not introduce misconceptions.
• Do not anthropomorphize the body or natural systems for grades 5+.
• For LOs with process-verbs (problem-solving, hypothesizing, observing, classifying), structure the script around that process.
• ONE weird fact per script.

STYLE
• Warm, curious, slightly playful. A friend sharing something cool, not a teacher.
• Avoid didactic opening sentences in body paragraphs. Embed definitions inside scenes.
• Short sentences. Vary rhythm. Concrete imagery.
• End on agency tied to THIS topic.

ANTI-PATTERNS — REWRITE IF YOUR DRAFT DOES ANY OF THESE
• Hook question whose answer is obvious yes or obvious no.
• Listing every sub-clause of the LO in order.
• Substituting curriculum vocabulary with "simpler" synonyms.
• Cartoonish anthropomorphism for grades 5+.
• Generic closing line.
• The literal words "weird fact" or "tuhaf gerçek" in the narration.

OUTPUT FORMAT
Output only the script text. No preamble, no explanation, no metadata, no title. Just the narration paragraphs.

INPUT
TOPIC: {TOPIC}
GRADE LEVEL: {GRADE_LEVEL}
LANGUAGE: {LANGUAGE}
LEARNING OBJECTIVE:
{LEARNING_OBJECTIVE}

Now write the script. Output only the narration."""


def render_weird_facts_prompt(topic: str, grade_level: str,
                              language: str, learning_objective: str) -> str:
    """Weird Facts template'inin placeholder'larını doldurur."""
    return (
        WEIRD_FACTS_TEMPLATE
        .replace("{TOPIC}", (topic or "").strip())
        .replace("{GRADE_LEVEL}", (grade_level or "").strip())
        .replace("{LANGUAGE}", (language or "TR").strip())
        .replace("{LEARNING_OBJECTIVE}", (learning_objective or "").strip())
    )


def generate_script_from_prompt(prompt: str,
                                 model: Optional[str] = None) -> tuple[bool, str]:
    """Verilen prompt'u Gemini'ye gönder, script çıktısı al. (success, script_or_error)."""
    if not prompt.strip():
        return False, "Prompt boş olamaz."
    return gemini_chat(
        prompt.strip(),
        model=model,
        temperature=0.8,
        max_tokens=2000,
        timeout=180,  # uzun bir Weird Facts prompt'tan tam script üretmek 30-60s
    )


# ---------------------------------------------------------------------------
# Phase B: Asset extraction — script'ten görsel listesi çıkar
# ---------------------------------------------------------------------------
ASSET_EXTRACTOR_SYSTEM = """You analyze short educational video scripts for grade-school students. The script will be visualized/illustrated, and we need to show some items in their REAL STATE for educational purposes (e.g. solar panels, microscopes, animals, historical artifacts, real people).

Given a video narration script, extract a list of the important OBJECTS and PERSONS mentioned (or strongly implied) in the text that should be shown in their true real-world form. Each asset must:
- Be a CONCRETE, real-world subject (a thing, a person, a place, a specific phenomenon) — not an abstract concept, metaphor, or feeling
- Match a specific narration moment (a phrase or sentence) where that object/person comes up
- Be the kind of thing a stock-image site (Wikimedia Commons, Openverse, Pexels) could provide, or an AI image model could render realistically

Output ONLY a JSON array. No preamble, no markdown fences, no explanation. Strictly this schema:

[
  {
    "position": "<3-7 word excerpt of the narration line this image accompanies, in the script's original language>",
    "description": "<one-sentence Turkish description of what the image should show, for the user to review>",
    "query": "<3-6 English keywords for stock image search — concrete nouns, no articles>",
    "style": "photo" | "illustration" | "diagram" | "archive"
  },
  ...
]

Rules:
- Aim for one key visual per main idea. Don't pad — quality over quantity.
- Adjust count to script length: ~5 for short scripts (under 200 words), ~10-15 for longer.
- "query" MUST be English even if the script is Turkish.
- "description" MUST be Turkish if the script is Turkish, else English.
- "style" guidance: "photo" for real-world subjects, "illustration" for stylized concepts, "diagram" for charts/processes, "archive" for historical/old footage.

Return ONLY the JSON array. Anything else breaks the parser."""


def extract_assets(script: str, model: Optional[str] = None,
                   system_prompt_override: Optional[str] = None) -> tuple[bool, list, str]:
    """Script'ten asset listesi çıkar. (success, assets_list, error_msg) döner.

    assets_list elemanları: {position, description, query, style}.
    system_prompt_override boş değilse default ASSET_EXTRACTOR_SYSTEM yerine kullanılır.
    Hata durumunda assets_list = [].
    """
    if not script.strip():
        return False, [], "Senaryo boş olamaz."

    sys_prompt = (system_prompt_override or "").strip() or ASSET_EXTRACTOR_SYSTEM

    ok, raw = gemini_chat(
        f"SCRIPT:\n{script.strip()}\n\nExtract assets as JSON array.",
        model=model,
        system_prompt=sys_prompt,
        temperature=0.4,
        max_tokens=4000,
        json_mode=True,  # downstream parser markdown fence + array slicing yapıyor zaten
        timeout=180,
    )
    if not ok:
        return False, [], raw  # raw = error mesajı

    # JSON parse — model bazen markdown fence (```json ... ```) ekliyor, temizle
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # ```json\n...\n``` veya ```\n...\n``` formatını sıyır
        lines = cleaned.split("\n")
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]) if lines[-1].strip().startswith("```") else "\n".join(lines[1:])
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    # Bazen başında "Here is the JSON:" gibi yazılar — ilk [ karakterinden başlat
    if "[" in cleaned:
        cleaned = cleaned[cleaned.index("["):]
        # Son ] karakterinde kes
        if "]" in cleaned:
            cleaned = cleaned[: cleaned.rindex("]") + 1]

    try:
        assets = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return False, [], (
            f"Model JSON yerine başka bir şey döndürdü ({e.msg}). "
            f"Başka model dene. İlk 200 karakter: {raw[:200]!r}"
        )

    if not isinstance(assets, list):
        return False, [], "Model array değil, başka bir şey döndürdü."

    # Validate + normalize
    valid = []
    for i, item in enumerate(assets):
        if not isinstance(item, dict):
            continue
        valid.append({
            "id": uuid.uuid4().hex[:8],  # stable id for editing/regenerate
            "position": str(item.get("position", ""))[:200],
            "description": str(item.get("description", ""))[:500],
            "query": str(item.get("query", ""))[:200],
            "style": str(item.get("style", "photo")).lower() or "photo",
        })

    if not valid:
        return False, [], "Model boş veya geçersiz liste döndürdü."

    return True, valid, ""


# ---------------------------------------------------------------------------
# Phase C: Image search — Wikimedia Commons + Openverse (free, no API key)
# ---------------------------------------------------------------------------
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

_HTTP_TIMEOUT = 12  # seconds — image search'te beklemek istemiyoruz


def _http_get_json(url: str, headers: Optional[dict] = None) -> Optional[dict]:
    """Tek seferlik GET → JSON, hata varsa None.

    UA: Cloudflare bot-detection (error 1010) Python urllib UA'sını blokluyor
    (Pexels gibi). Mozilla-tarzı UA gönderiyoruz.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, */*;q=0.5",
                **(headers or {}),
            },
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            data = r.read()
            return json.loads(data) if data else None
    except Exception:
        return None


# Style keyword boosts — Wikimedia/Openverse/Pexels text query'sine eklenir
# (bu API'lerin native image_type filter'ı yok). Tek kelime: search engine'ler
# multi-keyword'ü genelde AND ile match'liyor, fazla keyword sonuçları 0'a indiriyor.
_STYLE_QUERY_BOOST = {
    "photo": "",                # default zaten photo ağırlıklı, boost gereksiz
    "illustration": " illustration",
    "diagram": " diagram",
    "archive": " historical",
}


def _style_boost_query(query: str, style: str) -> str:
    """Query'yi style keyword'leriyle genişlet (search relevance için)."""
    boost = _STYLE_QUERY_BOOST.get(style, "")
    if not boost:
        return query
    return f"{query}{boost}"


def _search_wikimedia(query: str, limit: int = 4,
                      style: str = "photo") -> list[dict]:
    """Wikimedia Commons'ta görsel ara. CC-BY-SA / public domain genelde."""
    if not query.strip():
        return []
    query = _style_boost_query(query, style)
    # generator=search → search results
    # prop=imageinfo + iiurlwidth=400 → 400px thumbnail URL döner
    # iiprop: url (full), size, extmetadata (license info için)
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",  # File: namespace only
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": "400",
    }
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    data = _http_get_json(url)
    if not data:
        return []
    pages = (data.get("query") or {}).get("pages") or {}
    results = []
    for _pid, page in pages.items():
        info_list = page.get("imageinfo") or []
        if not info_list:
            continue
        info = info_list[0]
        # Sadece görüntü mime'ları (svg, png, jpg, gif, webp). Video/PDF skip.
        mime = (info.get("mime") or "").lower()
        if not mime.startswith("image/"):
            continue
        # extmetadata'dan lisans + attribution
        meta = info.get("extmetadata") or {}
        license_short = (meta.get("LicenseShortName") or {}).get("value", "")
        artist = (meta.get("Artist") or {}).get("value", "") or ""
        # Artist HTML olabilir, basit strip
        if "<" in artist:
            import re as _re
            artist = _re.sub(r"<[^>]+>", "", artist).strip()
        title = page.get("title", "").replace("File:", "")
        results.append({
            "source": "wikimedia",
            "thumb_url": info.get("thumburl") or info.get("url"),
            "full_url": info.get("url"),
            "title": title[:120],
            "license": license_short[:40] or "—",
            "attribution": artist[:120] or "Wikimedia Commons",
            "page_url": f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(page.get('title',''))}",
            "width": info.get("width", 0),
            "height": info.get("height", 0),
        })
    return results


def _search_openverse(query: str, limit: int = 4,
                      style: str = "photo") -> list[dict]:
    """Openverse aggregator (Flickr/CC kaynakları). API key gerekmez.

    Openverse 'category' parametresi destekler: photograph | illustration |
    digitized_artwork. Style buna map'lenir; ek olarak query keyword boost.

    Not: license_type filtresi koymuyoruz — Openverse zaten sadece CC içerik
    indeksliyor, ama strict "commercial" filtresi sonuçları çok daraltıyor.
    BY-NC, BY-ND da kabul (kullanım amacımıza uygun, atıf veriyoruz).
    """
    if not query.strip():
        return []
    # Openverse native category filter
    _OV_CATEGORY = {
        "photo": "photograph",
        "illustration": "illustration",
        "diagram": "illustration",       # diagram için yakın eşleşme
        "archive": "digitized_artwork",  # archival material
    }
    params = {
        "q": _style_boost_query(query, style),
        "page_size": str(limit),
        "category": _OV_CATEGORY.get(style, "photograph"),
    }
    # Yeni domain api.openverse.org — eski .engineering hala çalışıyor ama yeni daha stabil
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
    data = _http_get_json(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        results.append({
            "source": "openverse",
            "thumb_url": item.get("thumbnail") or item.get("url"),
            "full_url": item.get("url"),
            "title": (item.get("title") or "")[:120],
            "license": (item.get("license") or "").upper()[:40] or "—",
            "attribution": (item.get("creator") or "")[:120] or item.get("source", ""),
            "page_url": item.get("foreign_landing_url") or item.get("url"),
            "width": item.get("width", 0),
            "height": item.get("height", 0),
        })
    return results


def _search_pixabay(query: str, limit: int = 4,
                    style: str = "photo") -> list[dict]:
    """Pixabay — free tier (key gerek). https://pixabay.com/api/docs/

    image_type style'a map'lenir: photo|illustration|vector|all.
    Lisans: Pixabay License (commercial-friendly, atıf opsiyonel).
    """
    if not PIXABAY_API_KEY or not query.strip():
        return []
    # Style → Pixabay image_type
    _PIXABAY_TYPE = {
        "photo": "photo",
        "illustration": "illustration",
        "diagram": "vector",   # vector graphics ≈ diagram/infographic
        "archive": "photo",    # archive = old photo
    }
    params = {
        "key": PIXABAY_API_KEY,
        "q": _style_boost_query(query, style),
        "per_page": str(max(3, limit)),  # min 3
        "image_type": _PIXABAY_TYPE.get(style, "photo"),
        "safesearch": "true",
    }
    url = "https://pixabay.com/api/?" + urllib.parse.urlencode(params)
    data = _http_get_json(url)
    if not data:
        return []
    results = []
    for item in (data.get("hits") or [])[:limit]:
        results.append({
            "source": "pixabay",
            "thumb_url": item.get("previewURL") or item.get("webformatURL"),
            "full_url": item.get("largeImageURL") or item.get("webformatURL"),
            "title": (item.get("tags") or "")[:120],
            "license": "Pixabay",
            "attribution": (item.get("user") or "")[:80] or "Pixabay",
            "page_url": item.get("pageURL"),
            "width": item.get("imageWidth", 0),
            "height": item.get("imageHeight", 0),
        })
    return results


def _search_pexels(query: str, limit: int = 4,
                   style: str = "photo") -> list[dict]:
    """Pexels — free tier (key gerek). https://www.pexels.com/api/documentation/

    Pexels'te native style filter yok (sadece fotoğraf), style sadece
    query'ye keyword olarak eklenir. illustration/diagram için sonuçlar
    zayıf çıkar — Pixabay ve Wikimedia daha iyi alternatif.
    Lisans: Pexels License (commercial-friendly, atıf opsiyonel).
    """
    if not PEXELS_API_KEY or not query.strip():
        return []
    # Pexels sadece foto, style query-boost
    if style != "photo":
        # illustration / diagram için Pexels skip (kalitesiz match)
        return []
    params = {"query": _style_boost_query(query, style), "per_page": str(limit)}
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(params)
    data = _http_get_json(url, headers={"Authorization": PEXELS_API_KEY})
    if not data:
        return []
    results = []
    for item in (data.get("photos") or [])[:limit]:
        src = item.get("src") or {}
        results.append({
            "source": "pexels",
            "thumb_url": src.get("medium") or src.get("small"),
            "full_url": src.get("large2x") or src.get("large") or src.get("original"),
            "title": (item.get("alt") or "")[:120],
            "license": "Pexels",
            "attribution": (item.get("photographer") or "")[:80] or "Pexels",
            "page_url": item.get("url"),
            "width": item.get("width", 0),
            "height": item.get("height", 0),
        })
    return results


# ---------------------------------------------------------------------------
# Phase E: Asset image download — submit anında selected_image URL'lerini
# disk'e indir, automator'a path olarak ver. Pollinations URL'leri ilk fetch'te
# generation tetikliyor, 5-15s sürüyor; paralel indir.
# ---------------------------------------------------------------------------
_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "image/*,*/*;q=0.8",
}


def _ext_from_content_type(ctype: str, fallback_url: str) -> str:
    """Content-Type → uzantı. NotebookLM image upload'ı dosya tipini denetliyor."""
    ctype = (ctype or "").lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/bmp": ".bmp",
        "image/tiff": ".tif",
    }
    if ctype in mapping:
        return mapping[ctype]
    # URL'den çıkar fallback
    lower = fallback_url.lower().split("?")[0]
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"):
        if lower.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"  # safe default — NotebookLM jpg kabul ediyor


def download_image(url: str, dest_dir: Path, name_prefix: str,
                   timeout: int = 60) -> Optional[Path]:
    """Tek görseli indir, dosya path'i döndür. Hata varsa None.

    Pollinations URL'leri server-side gen yapıyor — timeout uzun tutmak gerek.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers=_DOWNLOAD_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if not data or len(data) < 100:  # boş ya da bozuk
                return None
            ctype = r.headers.get("Content-Type", "")
            ext = _ext_from_content_type(ctype, url)
        out_path = dest_dir / f"{name_prefix}{ext}"
        out_path.write_bytes(data)
        return out_path
    except Exception:
        return None


def download_video_for_revision(url: str, dest_path: Path,
                                 timeout: int = 300) -> Optional[Path]:
    """Revize için parent video MP4'ünü Azure URL'sinden indir.

    Azure SAS URL'leri public — auth gerekmez (SAS token URL'de). Büyük
    dosyalar (40-50MB) için stream-based write.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers=_DOWNLOAD_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            with dest_path.open("wb") as out:
                shutil.copyfileobj(r, out, length=1024 * 64)
        if dest_path.exists() and dest_path.stat().st_size > 10_000:
            return dest_path
        return None
    except Exception:
        return None


def download_job_images(job_id: str, assets: list) -> list[Path]:
    """Job'ın selected_image'lerini paralel indir, local path listesi döndür.

    Sadece selected_image olan asset'ler indirilir (kullanıcı seçmediyse skip).
    Hata olan görsel atılır, kalanlar yine de upload edilir.
    """
    if not assets:
        return []
    selected = []
    for i, a in enumerate(assets):
        sel = a.get("selected_image") or {}
        # Shutterstock → sstk_id (pipeline'da lisanslanır; filigranlı thumb İNDİRİLMEZ).
        sstk_id = sel.get("sstk_id") or ""
        # Gemini üretimi → local_path (diskte). HTTP url yerine kopyalanır.
        local_path = sel.get("local_path") or ""
        url = ""
        if not sstk_id:
            url = sel.get("full_url") or ""
            # thumb_url sadece http ise indirme fallback'i olur (data: URI atlanır)
            _t = sel.get("thumb_url") or ""
            if not url and _t.startswith(("http://", "https://")):
                url = _t
        if not sstk_id and not local_path and not url:
            continue
        # Anlamlı dosya adı: indeks + asset id (sıralı upload için)
        name = f"{i+1:02d}_{a.get('id','asset')[:8]}"
        selected.append((sstk_id, local_path, url, name))
    if not selected:
        return []

    job_dir = JOB_ASSETS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Optional[Path]] = [None] * len(selected)
    from concurrent.futures import ThreadPoolExecutor

    def _dl(idx: int) -> None:
        sstk_id, local_path, url, name = selected[idx]
        # Shutterstock → şimdi lisansla (1 indirme harcar) + temiz indir
        if sstk_id:
            paths[idx] = shutterstock_license_download(sstk_id, job_dir, name)
            return
        # Lokal (Gemini-üretimi) → kopyala, HTTP'ye gitme
        if local_path:
            src = Path(local_path)
            if src.exists():
                try:
                    dst = job_dir / f"{name}{src.suffix or '.png'}"
                    shutil.copyfile(src, dst)
                    paths[idx] = dst
                    return
                except OSError:
                    pass
        if url:
            paths[idx] = download_image(url, job_dir, name)

    # Pollinations gen 5-15s × N parallel; max 4 thread iyi denge
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_dl, range(len(selected))))

    return [p for p in paths if p is not None]


# ---------------------------------------------------------------------------
# Phase D: Image generation fallback (Pollinations.ai — free, key gerek yok)
# ---------------------------------------------------------------------------
# Pollinations URL-based: GET request hem trigger hem result. CDN cache'liyor,
# aynı seed+prompt+model = aynı görsel.
# Probe ile çalıştığını teyit ettiklerimiz:
POLLINATIONS_MODELS: list[tuple[str, str]] = [
    ("flux", "Flux — kaliteli, dengeli (varsayılan)"),
    ("turbo", "Turbo — daha hızlı (SDXL Turbo)"),
    ("flux-pro", "Flux Pro — yüksek kalite"),
]

# Style → prompt suffix (Flux daha "photorealistic" gibi keyword'lere iyi tepki veriyor)
_STYLE_SUFFIX = {
    "photo": "photorealistic, high detail, sharp focus, professional photography",
    "illustration": "digital illustration, vibrant colors, detailed, concept art",
    "diagram": "clean infographic, vector style, flat design, white background",
    "archive": "vintage archival photograph, sepia tone, film grain, historical",
}


def generate_images(prompt: str, count: int = 4, model: str = "flux",
                    style: str = "photo") -> list[dict]:
    """Pollinations.ai ile farklı seed'lerde N varyant üret.

    Aslında üretim yapmıyor — sadece doğru URL'leri inşa ediyor. Browser'in
    img tag'i URL'i fetch ettiğinde Pollinations server-side üretiyor (5-15s).
    Cache'lendikten sonra anlık.
    """
    if not prompt.strip():
        return []
    suffix = _STYLE_SUFFIX.get(style, _STYLE_SUFFIX["photo"])
    full_prompt = f"{prompt.strip()}, {suffix}"

    out = []
    base_seed = int(time.time() * 1000) % 1_000_000
    for i in range(count):
        seed = base_seed + i * 7919  # asal ile çoğalt → daha çeşitli seedler
        params = {
            "width": "1024",
            "height": "1024",
            "model": model,
            "seed": str(seed),
            "nologo": "true",
        }
        url = (
            "https://image.pollinations.ai/prompt/"
            + urllib.parse.quote(full_prompt)
            + "?" + urllib.parse.urlencode(params)
        )
        out.append({
            "source": "pollinations",
            "thumb_url": url,
            "full_url": url,
            "title": full_prompt[:120],
            "license": "Pollinations (free)",
            "attribution": f"AI · {model} · seed {seed}",
            "page_url": url,
            "width": 1024,
            "height": 1024,
            # Debug/regen için ek alanlar
            "_seed": seed,
            "_model": model,
            "_prompt": full_prompt,
        })
    return out


def _make_thumb_datauri(data: bytes, size: int = 240) -> str:
    """Görsel byte'larından küçük JPEG thumbnail data-URI üret (HTML <img> için).
    Tam çözünürlük diskte; bu sadece UI önizleme (jobs.json'u şişirmesin diye küçük)."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.thumbnail((size, size))
        if im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def generate_images_gemini(prompt: str, count: int = 4, model: str = "img-flash-2.5",
                           style: str = "photo") -> list[dict]:
    """Gemini ile N görsel varyantı üret. Byte'ları GEN_IMAGES_DIR'e kaydeder,
    candidate dict listesi döndürür (local_path = tam çözünürlük disk, thumb_url =
    küçük data-URI önizleme). Pollinations'tan farkı: gerçek üretim (key/maliyet).

    download_job_images local_path'i görünce HTTP indirme yerine kopyalar.
    """
    if not prompt.strip() or not _GEMINI_AVAILABLE:
        return []
    suffix = _STYLE_SUFFIX.get(style, _STYLE_SUFFIX["photo"])
    full_prompt = f"{prompt.strip()}, {suffix}"
    GEN_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    def _one(i: int) -> Optional[dict]:
        try:
            # variation hint → her çağrı biraz farklı sonuç
            data = gemini_generate_image(
                f"{full_prompt}. Variation {i + 1}: distinct composition.",
                model=model,
            )
        except Exception as e:
            launcher_log(f"Gemini image gen fail (variation {i+1}): {e}")
            return None
        if not data or len(data) < 100:
            return None
        ext = ".png" if data[:4] == b"\x89PNG" else (".jpg" if data[:2] == b"\xff\xd8" else ".png")
        fpath = GEN_IMAGES_DIR / f"{uuid.uuid4().hex[:12]}{ext}"
        try:
            fpath.write_bytes(data)
        except OSError as e:
            launcher_log(f"Gemini image save fail: {e}")
            return None
        return {
            "source": "gemini",
            "thumb_url": _make_thumb_datauri(data),  # küçük data-URI (HTML preview)
            "full_url": "",                          # HTTP yok — local_path kullanılır
            "local_path": str(fpath),                # tam çözünürlük (grid + pipeline)
            "title": full_prompt[:120],
            "license": f"AI · {model}",
            "attribution": f"Gemini · {model}",
            "page_url": "",
            "_model": model,
            "_prompt": full_prompt,
        }

    from concurrent.futures import ThreadPoolExecutor
    out: list = [None] * count
    with ThreadPoolExecutor(max_workers=min(4, count)) as ex:
        for idx, res in enumerate(ex.map(_one, range(count))):
            out[idx] = res
    return [r for r in out if r]


# ---------------------------------------------------------------------------
# Shutterstock — lisanslı stok arama + lisansla/indir (paralı abonelik)
# ---------------------------------------------------------------------------
_SSTK_BASE = "https://api.shutterstock.com"
_sstk_sub_cache: dict = {}


def _shutterstock_headers() -> dict:
    return {"Authorization": "Bearer " + SHUTTERSTOCK_TOKEN}


def shutterstock_subscription() -> Optional[dict]:
    """Lisanslamaya uygun ilk image aboneliği (5dk cache). {id,downloads_left,...} ya da None."""
    if not SHUTTERSTOCK_ENABLED:
        return None
    if _sstk_sub_cache.get("sub") and (time.time() - _sstk_sub_cache.get("ts", 0) < 300):
        return _sstk_sub_cache["sub"]
    try:
        req = urllib.request.Request(_SSTK_BASE + "/v2/user/subscriptions",
                                     headers=_shutterstock_headers())
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        for s in data.get("data", []):
            if s.get("asset_type") == "images":
                al = s.get("allotment") or {}
                sub = {"id": s.get("id"),
                       "downloads_left": al.get("downloads_left", 0),
                       "downloads_limit": al.get("downloads_limit", 0)}
                _sstk_sub_cache.update(sub=sub, ts=time.time())
                return sub
    except Exception as e:
        launcher_log(f"shutterstock subscription fetch fail: {e}")
    return None


def shutterstock_search(query: str, limit: int = 8, style: str = "photo") -> list[dict]:
    """Shutterstock'ta ara → candidate listesi (FİLİGRANLI önizleme + sstk_id).
    Arama ücretsiz; lisanslama pipeline'da (seçilen + job çalışınca) yapılır."""
    if not SHUTTERSTOCK_ENABLED or not query.strip():
        return []
    img_type = "illustration" if style in ("illustration", "diagram") else "photo"
    params = {"query": query.strip(), "per_page": min(max(limit, 1), 20),
              "image_type": img_type, "sort": "relevance", "safe": "true"}
    url = _SSTK_BASE + "/v2/images/search?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=_shutterstock_headers())
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        launcher_log(f"shutterstock search fail: {e}")
        return []
    out = []
    for a in data.get("data", []):
        assets = a.get("assets") or {}
        prev = ((assets.get("preview") or {}).get("url")
                or (assets.get("preview_1000") or {}).get("url")
                or (assets.get("preview_1500") or {}).get("url") or "")
        if not prev:
            continue
        out.append({
            "source": "shutterstock",
            "thumb_url": prev,        # filigranlı önizleme (HTML <img>)
            "full_url": "",           # pipeline'da lisanslanıp indirilecek
            "sstk_id": str(a.get("id")),
            "needs_license": True,
            "title": (a.get("description") or "")[:120],
            "license": "Shutterstock · seçim = 1 indirme",
            "attribution": "Shutterstock",
            "page_url": "",
        })
    return out


def shutterstock_license_download(sstk_id: str, dest_dir: Path,
                                  name: str) -> Optional[Path]:
    """Shutterstock görselini LİSANSLA (1 indirme harcar) + temiz (filigransız) indir.
    Pipeline'da (job çalışırken) çağrılır → seçilip iptal edilen draft'lar kredi harcamaz.
    Doğru format (doğrulandı): subscription_id query'de, body sadece image_id."""
    if not SHUTTERSTOCK_ENABLED or not sstk_id:
        return None
    sub = shutterstock_subscription()
    if not sub or not sub.get("id"):
        launcher_log(f"shutterstock: kullanılabilir abonelik yok (id={sstk_id})")
        return None
    body = json.dumps({"images": [{"image_id": str(sstk_id)}]}).encode()
    url = _SSTK_BASE + "/v2/images/licenses?subscription_id=" + urllib.parse.quote(sub["id"])
    try:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={**_shutterstock_headers(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            lic = json.loads(r.read().decode())
    except Exception as e:
        launcher_log(f"shutterstock license fail (id={sstk_id}): {e}")
        return None
    d0 = (lic.get("data") or [{}])[0]
    if d0.get("error"):
        launcher_log(f"shutterstock license error (id={sstk_id}): {d0.get('error')}")
        return None
    dl_url = (d0.get("download") or {}).get("url", "")
    if not dl_url:
        launcher_log(f"shutterstock no download url (id={sstk_id}): {str(lic)[:160]}")
        return None
    _sstk_sub_cache.clear()  # kota değişti → cache invalidate
    p = download_image(dl_url, dest_dir, name, timeout=90)
    launcher_log(f"shutterstock lisanslandı + indirildi (id={sstk_id}) → "
                 f"{'OK' if p else 'indirme hatası'}")
    return p


def search_images(query: str, limit: int = 12,
                  style: str = "photo") -> list[dict]:
    """Aynı query'yi tüm aktif kaynaklarda ara, sonuçları interleave et.

    style: 'photo' | 'illustration' | 'diagram' | 'archive'
      - Pixabay: native image_type filter (photo/illustration/vector)
      - Openverse: native category filter (photograph/illustration/digitized_artwork)
      - Wikimedia: query keyword boost
      - Pexels: sadece photo (illustration/diagram için skip — kalitesiz match)

    Aktif kaynaklar:
    - Wikimedia (her zaman, key gerekmez)
    - Openverse (her zaman, key gerekmez)
    - Pixabay (PIXABAY_API_KEY env set ise)
    - Pexels (PEXELS_API_KEY env set ise — sadece photo style)

    Interleave: kaynak çeşitliliği için round-robin.
    """
    if not query.strip():
        return []
    per_source = max(2, limit // 4)
    sources: list[list[dict]] = []
    sources.append(_search_wikimedia(query, limit=per_source + 2, style=style))
    sources.append(_search_openverse(query, limit=per_source + 2, style=style))
    if PIXABAY_API_KEY:
        sources.append(_search_pixabay(query, limit=per_source + 2, style=style))
    if PEXELS_API_KEY:
        sources.append(_search_pexels(query, limit=per_source + 2, style=style))

    # Round-robin interleave
    out = []
    i = 0
    while len(out) < limit:
        added_this_round = False
        for src_list in sources:
            if i < len(src_list) and len(out) < limit:
                out.append(src_list[i])
                added_this_round = True
        if not added_this_round:
            break
        i += 1
    return out[:limit]


# ---------------------------------------------------------------------------
# Auth failure detection
# ---------------------------------------------------------------------------
# NotebookLM cookies expire, signin redirect, vs. yedi mi? Bu fonksiyon hem
# pipeline stage'ine hem error mesajına bakarak geniş yakalama yapar. Eskiden
# sadece `e.stage == "auth"` kontrolü vardı — ama cookies pipeline ortasında
# (örn. video_gen) expire olursa stage başka oluyor, auth handler tetiklenmiyor
# ve aynı profile'a 20 job daha gönderiliyordu.
_AUTH_FAIL_PATTERNS = (
    "redirected to login",
    "accounts.google.com",
    "/signin",
    "auth token",
    "auth.json",
    "nlm auth",
    "login_required",
    "login required",
    "session expired",
    "unauthorized",
    # 401/403: bare substring "403"/"401" notebook/task ID'lerinin içinde
    # tesadüfen geçip FALSE auth-fail (auth.json silme + re-login) tetikliyordu.
    # HTTP-bağlamı zorunlu kıl → sadece gerçek HTTP hataları yakalanır.
    "http 401", "http 403", "status 401", "status 403",
    "401 unauthorized", "403 forbidden", "error 401", "error 403",
    " 401:", " 403:", "(401)", "(403)",
)


def is_auth_failure(stage: str, error_msg: str) -> bool:
    """Pipeline stage veya error mesajından auth fail çıkarımı."""
    if (stage or "").lower() == "auth":
        return True
    msg = (error_msg or "").lower()
    return any(pat in msg for pat in _AUTH_FAIL_PATTERNS)


# Kota/rate-limit token'ları — bare "limit" YERİNE spesifik. Geniş "limit"
# ("character limit", alakasız bir API'nin "rate limit"i vb.) bir hesabı 8 saat
# bloklayabiliyordu. Pipeline'daki is_quota (run_full_pipeline) ile aynı küme +
# "kota" (quota marker'ının error metni Türkçe "kotası" içerir).
_QUOTA_PATTERNS = (
    "kota", "quota", "429", "resource_exhausted", "too many requests",
    "rate limit", "rate-limit", "rate_limit",
    "daily limit", "limit reached", "limit exceeded",
    "featureunavailable", "feature unavailable", "generation is unavailable",
)


def _is_quota_error(error_msg: str) -> bool:
    """Kota/rate-limit hatası mı? (spesifik token — bare 'limit' değil)."""
    msg = (error_msg or "").lower()
    return any(pat in msg for pat in _QUOTA_PATTERNS)


# ---------------------------------------------------------------------------
# Worker — background dispatcher thread
# ---------------------------------------------------------------------------
class Worker:
    def __init__(self) -> None:
        self._stop_evt = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="nbworker", daemon=True)
        self._procs: dict[str, subprocess.Popen] = {}  # job_id -> Popen
        self._proc_lock = threading.Lock()
        # ensure_alive() her render'da (çok session paralel) çağrılır; check+restart
        # lock'suz olursa iki session aynı anda "ölü" görüp İKİ thread başlatır →
        # iki dispatch loop → aynı iş 2× submit. Lock bunu serileştirir.
        self._ensure_lock = threading.Lock()
        # Startup reconciliation 1× / PROCESS çalışsın. ensure_alive sadece
        # thread'i yeniden yaratır (aynı Worker instance, eski pipeline
        # thread'leri yaşıyor olabilir) → o yolda reconciliation YAPILMAMALI,
        # yoksa canlı gen'le paralel resume (aynı MP4'e çift yazma) riski doğar.
        self._startup_reconciled = False
        # Stale-resume sweeper: aktif resume thread'lerini takip et ki aynı
        # job için birden fazla resume thread spawn etmeyelim.
        self._resume_threads: dict[str, threading.Thread] = {}
        # _auto_init_check'in smoke_test FAIL log spam'ini bastırmak için
        # profile_id → son fail timestamp.
        self._smoke_fail_ts: dict[str, float] = {}
        # Env-eşleşmesiz queued job uyarısını throttle etmek için
        # env → son uyarı timestamp (saatte 1×).
        self._env_warn_ts: dict[str, float] = {}

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()
            launcher_log("Worker thread başladı.")

    def ensure_alive(self) -> None:
        """Worker thread sessizce ölmüşse (cache_resource singleton ölü thread'i
        tutar) yeniden başlat + Slack alarm. Her render'da çağrılır → kuyruğun
        sessizce donmasını (worker öldü ama UI 'queued' gösteriyor) önler.
        60sn throttle: anında tekrar ölürse spam-restart yapmaz."""
        try:
            if self._thread.is_alive():
                return
            # Stop() çağrılmışsa (kasıtlı kapatma) diriltme — yoksa sonsuz
            # restart+alarm döngüsü olur.
            if self._stop_evt.is_set():
                return
            # check-and-restart'ı serileştir: iki session aynı anda girip
            # iki thread başlatmasın (çift dispatch loop → çift submit).
            with self._ensure_lock:
                if self._thread.is_alive():
                    return
                now = time.time()
                if now - getattr(self, "_last_restart", 0.0) < 60:
                    return
                self._last_restart = now
                launcher_log("⚠ Worker thread ÖLÜ bulundu — yeniden başlatılıyor.")
                try:
                    send_slack_message(
                        "🚨 *Worker thread ölmüştü* — otomatik yeniden başlatıldı. "
                        "Kuyruk bir süre donmuş olabilir; durumu kontrol et.")
                except Exception:
                    pass
                self._thread = threading.Thread(
                    target=self._loop, name="nbworker", daemon=True)
                self._thread.start()
        except Exception as e:
            launcher_log(f"ensure_alive error: {e!r}")

    def stop(self) -> None:
        self._stop_evt.set()

    def stop_all_jobs(self) -> int:
        """Çalışan tüm subprocess'leri öldür. Yeni job dispatch edilmeyecek
        çünkü status=queued olanlar el ile temizlenecek."""
        n = 0
        with self._proc_lock:
            for jid, proc in list(self._procs.items()):
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        n += 1
                except Exception:
                    pass
        # Kuyruktakileri "stopped" işaretle
        jobs = load_jobs()
        for j in jobs:
            if j.status == "queued":
                j.status = "stopped"
                j.finished_at = time.time()
                n += 1
        save_jobs(jobs)
        return n

    def stop_job(self, job_id: str) -> bool:
        with self._proc_lock:
            proc = self._procs.get(job_id)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    return True
                except Exception:
                    return False
        return False

    def _loop(self) -> None:
        last_harvest_check = 0.0
        last_stale_check = 0.0
        last_batch_check = 0.0
        # Auth health check ilk round'da çalışmasın — server boot anında
        # initialize henüz tamamlanmamış olabilir. İlk full interval'dan sonra ilk.
        last_auth_health_check = time.time()
        # Boot'ta bir kez: yüklenmiş MP4 backlog'unu temizle (disk sızıntısı).
        try:
            self._cleanup_uploaded_mp4s()
        except Exception as e:
            launcher_log(f"startup mp4 cleanup error: {e!r}")
        # Boot'ta bir kez (PROCESS başına): restart'ın öksüz bıraktığı
        # 'generating' işleri sweeper'a arm et (self-healing, elle müdahale yok).
        if not self._startup_reconciled:
            self._startup_reconciled = True
            try:
                self._reconcile_orphans_on_start()
            except Exception as e:
                launcher_log(f"startup reconciliation error: {e!r}")
        while not self._stop_evt.is_set():
            try:
                self._auto_init_check()
                self._dispatch_round()
                self._reap_finished()
                # Harvest round'u her HARVEST_CHECK_INTERVAL_SEC'de bir çağır
                if time.time() - last_harvest_check >= HARVEST_CHECK_INTERVAL_SEC:
                    self._harvest_round()
                    last_harvest_check = time.time()
                # Stale-resume sweeper: notebooklm-py path'inde stuck olan
                # 'generating' job'ları otomatik resume et (5dk'da bir tara)
                if time.time() - last_stale_check >= STALE_RESUME_CHECK_INTERVAL_SEC:
                    self._stale_resume_round()
                    last_stale_check = time.time()
                # Batch / oturum bildirim monitörü — 30 sn'de bir
                if time.time() - last_batch_check >= 30:
                    self._batch_monitor_round()
                    last_batch_check = time.time()
                # Auth health check: periyodik smoke test → expired profilleri
                # SUBMIT'TEN ÖNCE keşfet + Slack alert (1. job ziyanını önler)
                if time.time() - last_auth_health_check >= AUTH_HEALTH_CHECK_INTERVAL_SEC:
                    self._auth_health_check_round()
                    self._cleanup_uploaded_mp4s()
                    last_auth_health_check = time.time()
                # Günsonu özet — günde 1×, DAILY_SUMMARY_HOUR_UTC saatinde
                self._daily_summary_round()
            except Exception as e:
                launcher_log(f"dispatch error: {e!r}")
            self._stop_evt.wait(DISPATCH_INTERVAL_SEC)

    def _daily_summary_round(self) -> None:
        """Günde 1× Slack özet raporu (DAILY_SUMMARY_HOUR_UTC saatinde).

        İçerik: bugün üretilen video, fail, kota-block profil, env dağılımı.
        Son gönderim tarihi data/.last_daily_summary'de tutulur → restart-safe,
        aynı gün iki kez göndermez.
        """
        if not SLACK_ENABLED:
            return
        now = datetime.now()  # server UTC
        if now.hour < DAILY_SUMMARY_HOUR_UTC:
            return  # henüz vakti gelmedi
        today_str = now.strftime("%Y-%m-%d")
        # Son gönderim tarihini oku
        try:
            last = DAILY_SUMMARY_STATE_FILE.read_text(encoding="utf-8").strip()
        except (OSError, FileNotFoundError):
            last = ""
        if last == today_str:
            return  # bugün zaten gönderildi

        # Bugünün istatistiği
        jobs = load_jobs()
        today = now.date()

        def _is_today(j: Job) -> bool:
            ts = j.started_at or j.created_at
            if not ts:
                return False
            try:
                return datetime.fromtimestamp(ts).date() == today
            except (OSError, OverflowError, ValueError):
                return False

        today_jobs = [j for j in jobs if _is_today(j)]
        done = [j for j in today_jobs if j.status == "done"]
        failed = [j for j in today_jobs
                  if j.status == "failed"
                  and not (j.title or "").startswith("[KOTA]")]
        # Env dağılımı (done)
        env_done = {}
        for j in done:
            e = (j.environment or "prod")
            env_done[e] = env_done.get(e, 0) + 1
        env_line = " · ".join(f"{e}: {n}" for e, n in sorted(env_done.items())) or "—"

        # Expired (initialized=False) profiller
        try:
            expired = [p.name for p in load_profiles() if not p.initialized]
        except Exception:
            expired = []

        # Kullanıcı tercihi (2026-06-10): Slack'e HATA DETAYI gitmesin —
        # sadece üretim sayısı + login gereken hesaplar (logout = aksiyon).
        # Hata/kota detayları admin panelde (Durum + Üretim sekmeleri).
        lines = [
            f"📅 *Günsonu Özet — {today_str}*",
            f"✅ Üretilen video: *{len(done)}*",
            f"🌍 Env: {env_line}",
        ]
        if expired:
            lines.append(
                f"🔑 Login gereken profiller: {', '.join(expired)} "
                f"→ `./deploy/login.sh`"
            )

        send_slack_message("\n".join(lines))
        try:
            DAILY_SUMMARY_STATE_FILE.write_text(today_str, encoding="utf-8")
        except OSError:
            pass
        launcher_log(f"Daily summary gönderildi: {today_str} "
                     f"(done={len(done)}, failed={len(failed)})")

    def _batch_monitor_round(self) -> None:
        """Batch (toplu import oturumu) yaşam döngüsünü izler, Slack bildirimleri gönderir.

        Bildirim tetikleyicileri:
          1. Her N tamamlamada ilerleme güncellemesi (threshold: total // 3, min 5)
          2. Tüm kuyruk boşaldığında: "Son iş işleniyor"
          3. Tüm işler bittiğinde: Oturum özeti
        """
        batches = load_batches()
        if not batches:
            return
        jobs = load_jobs()
        changed = False

        for batch in batches:
            if batch.notified_complete:
                continue

            batch_jobs = [j for j in jobs if j.batch_id == batch.id]
            if not batch_jobs:
                continue

            in_progress = [j for j in batch_jobs
                           if j.status in ("running", "generating", "submitted")]
            queued_jobs  = [j for j in batch_jobs if j.status == "queued"]
            done_jobs    = [j for j in batch_jobs if j.status == "done"]
            # [KOTA] marker'ları hata sayısına katma
            failed_jobs  = [j for j in batch_jobs
                            if j.status == "failed" and not j.title.startswith("[KOTA]")]
            terminal_jobs = done_jobs + failed_jobs

            n_ip      = len(in_progress)
            n_q       = len(queued_jobs)
            n_done    = len(done_jobs)
            n_fail    = len(failed_jobs)
            n_term    = len(terminal_jobs)

            # -- Tüm kuyruk boşaldı bildirimi (henüz gönderilmemişse) --
            if n_q == 0 and n_ip > 0 and not batch.queued_empty_notified:
                send_slack_message(
                    f"🔄 *Tüm işler başladı — sırada bekleyen kalmadı*\n"
                    f"📁 {batch.name}\n"
                    f"⚙️ {n_ip} iş işleniyor · "
                    f"✅ {n_done} tamamlandı · ❌ {n_fail} hatalı"
                )
                batch.queued_empty_notified = True
                changed = True

            # -- Kota duvarı bildirimi: tüm queued env'lerinde profil kalmadıysa --
            # Per-job quota Slack'i kaldırıldı (spam'di). Yerine batch-level
            # 1× toplu mesaj: tüm profiller bugün için bloke olunca fire eder.
            if n_q > 0 and not batch.quota_wall_notified:
                initialized_profiles = [p for p in load_profiles() if p.initialized]
                queued_envs = set(
                    (j.environment or "prod").strip().lower()
                    for j in queued_jobs
                )
                blocked_names: set[str] = set()
                all_walled = True
                for env in queued_envs:
                    env_profiles = [p for p in initialized_profiles
                                    if (p.environment or "prod").strip().lower() == env]
                    if not env_profiles:
                        # env-eşleşmesiz queued — env validation flow'unun işi,
                        # quota duvarı değil. Bu batch için duvar tetikleme.
                        all_walled = False
                        break
                    blocked = [p for p in env_profiles
                               if self._quota_blocked_today(jobs, p.id)]
                    if len(blocked) < len(env_profiles):
                        all_walled = False
                        break
                    blocked_names.update(p.name for p in env_profiles)
                if all_walled and blocked_names:
                    names_str = ", ".join(sorted(blocked_names))
                    send_slack_message(
                        f"🛑 *Kota duvarı*\n"
                        f"📁 {batch.name}\n"
                        f"⏳ {n_q} iş yarın quota reset olunca otomatik devam edecek\n"
                        f"✅ {n_done} tamamlandı · ❌ {n_fail} hatalı\n"
                        f"👥 Kapalı profiller: {names_str}"
                    )
                    batch.quota_wall_notified = True
                    changed = True

            # -- İlerleme bildirimi (her threshold tamamlamada) --
            threshold = max(5, batch.total // 3)
            if (n_term >= batch.last_notified_terminal + threshold
                    and n_q > 0):   # hâlâ bekleyen var → dalga arası güncelleme
                # Kullanıcı tercihi (2026-06-10): hata DETAYI Slack'e gitmez
                # (sadece sayı); detay admin panelde.
                send_slack_message(
                    f"📊 *İlerleme: {n_term}/{batch.total}*\n"
                    f"📁 {batch.name}\n"
                    f"✅ {n_done} başarılı · ❌ {n_fail} hatalı · "
                    f"⏳ {n_q} sırada · ⚙️ {n_ip} işleniyor"
                )
                batch.last_notified_terminal = n_term
                changed = True

            # -- Oturum tamamlandı bildirimi --
            if n_ip == 0 and n_q == 0 and n_term >= batch.total:
                batch.completed_at = time.time()
                batch.notified_complete = True
                dur_str = _fmt_duration_batch(batch.completed_at - batch.created_at)

                source_line = (
                    f"\n📎 Drive: {batch.source}"
                    if batch.source.startswith("http") else ""
                )

                # Kullanıcı tercihi (2026-06-10): hata DETAYI Slack'e gitmez
                # (sadece sayı); detay admin panelde.
                send_slack_message(
                    f"🏁 *Oturum Tamamlandı*\n"
                    f"📁 {batch.name} · {batch.total} proje\n"
                    f"✅ Başarılı: {n_done}   ❌ Hatalı: {n_fail}\n"
                    f"⏱ Süre: {dur_str}{source_line}"
                )
                changed = True

        if changed:
            save_batches(batches)

    def _stale_resume_round(self) -> None:
        """notebooklm-py path'inde stuck kalan 'generating' job'ları tarar
        ve resume_via_notebooklm'i ayrı thread'de tetikler.

        Filtreler:
          - status='generating' (henüz fail/done değil)
          - harvest_status='skip' (notebooklm-py işareti)
          - notebook_url var (gen başlamış)
          - video_local_path yok (henüz indirilmemiş)
          - age >= STALE_RESUME_MIN_AGE_SEC (Cinematic gen normalde 30-40dk
            sürer, 90dk geçtiyse büyük ihtimalle thread öldü)
          - harvest_attempts < MAX (sonsuz retry'ı önle)
          - next_harvest_at < now (backoff'a saygı)

        Round başına max 1 job spawn — paralel kurtarma yapmıyoruz.
        """
        # Ölü resume thread'lerini sözlükten temizle (bilgi amaçlı)
        for jid in list(self._resume_threads.keys()):
            t = self._resume_threads.get(jid)
            if t and not t.is_alive():
                self._resume_threads.pop(jid, None)

        jobs = load_jobs()
        now = time.time()
        candidates: list[Job] = []
        exhausted: list = []  # attempts>=MAX, hâlâ 'generating'de stuck → terminal (2A)
        for j in jobs:
            if j.status != "generating":
                continue
            if j.harvest_status != "skip":
                continue
            if not j.notebook_url:
                continue
            if j.video_local_path:
                continue
            # Aynı job için zaten resume thread çalışıyorsa pas geç
            existing = self._resume_threads.get(j.id)
            if existing and existing.is_alive():
                continue
            age = now - (j.started_at or j.created_at)
            # 90dk yaş eşiği canlı pipeline thread'iyle çakışmayı önler.
            # ARM edilmiş işler (next_harvest_at>0 — artifact-removed handler /
            # startup reconciliation; ikisinde de orijinal thread ölü) bu
            # eşiği BEKLEMEZ: zamanlamayı aşağıdaki next_harvest_at kontrolü yönetir.
            if age < STALE_RESUME_MIN_AGE_SEC and not (j.next_harvest_at or 0):
                continue
            if j.harvest_attempts >= STALE_RESUME_MAX_ATTEMPTS:
                exhausted.append(j.id)
                continue
            if j.next_harvest_at and j.next_harvest_at > now:
                continue
            candidates.append(j)

        # 2A: MAX deneme tükenmiş + hâlâ "generating"de takılı işler:
        # notebook'ta GERÇEKTEN video yok (3 kontrol boş döndü — gen hiç
        # kalıcılaşmamış, tipik burst kurbanı). Eskiden direkt failed olup elle
        # '🔄 Tekrar dene' bekliyordu. Şimdi: transient_retry_count bütçesi
        # varsa OTOMATİK taze deneme (notebook temiz, 15dk dispatch backoff →
        # round-robin farklı hesaba düşer); bütçe bittiyse failed (elle buton).
        if exhausted:
            _ex = set(exhausted)

            def _term(js):
                rq, fl = 0, 0
                now3 = time.time()
                for j in js:
                    if j.id not in _ex or j.status != "generating":
                        continue
                    n = getattr(j, "transient_retry_count", 0)
                    if n < TRANSIENT_RETRY_MAX:
                        j.status = "queued"
                        j.profile_id = ""
                        j.profile_name = ""
                        j.notebook_url = ""
                        j.error = ""
                        j.started_at = 0.0
                        j.finished_at = 0.0
                        j.pid = 0
                        j.harvest_status = "pending"
                        j.harvest_attempts = 0
                        j.next_harvest_at = 0.0
                        j.transient_retry_count = n + 1
                        j.next_dispatch_at = now3 + 900  # 15dk sonra, sakin başla
                        rq += 1
                    else:
                        j.status = "failed"
                        j.harvest_status = "expired"
                        if not (j.error or "").strip():
                            j.error = ("video kalıcılaşmadı: 3 notebook kontrolü "
                                       "boş + 2 taze deneme tükendi")
                        if not j.finished_at:
                            j.finished_at = now3
                        fl += 1
                return (rq, fl)

            _rq, _fl = mutate_jobs(_term) or (0, 0)
            if _rq or _fl:
                launcher_log(
                    f"Stale-resume tükenen: {_rq} iş otomatik taze deneme "
                    f"(15dk backoff, farklı hesap), {_fl} iş failed "
                    f"(retry bütçesi bitti)")

        # B4: 'submitted' kara-deliği. Automation exit_code=0 ama notebook_url
        # döndürmediğinde iş 'submitted' kalır: harvest'lenmez (URL yok),
        # stale-resume edilmez (generating değil), requeue edilmez (failed değil)
        # → _busy_count_for onu sonsuza dek 'busy' sayar, max_concurrent=1 hesap
        # bir daha dispatch ETMEZ (sessiz slot sızıntısı). Yaşı eşiği geçmiş +
        # URL'süz submitted işleri 'failed' yap → slot serbest + 'Tekrar dene' çıkar.
        stuck_submitted = [
            j.id for j in jobs
            if j.status == "submitted"
            and not (j.notebook_url or "").strip()
            and (now - (j.started_at or j.created_at)) >= STALE_RESUME_MIN_AGE_SEC
        ]
        if stuck_submitted:
            _ss = set(stuck_submitted)

            def _term_sub(js):
                n = 0
                for j in js:
                    if j.id in _ss and j.status == "submitted":
                        j.status = "failed"
                        if not (j.error or "").strip():
                            j.error = ("submitted ama notebook_url yok "
                                       "(automation URL döndürmedi, stuck)")
                        if not j.finished_at:
                            j.finished_at = time.time()
                        n += 1
                return n

            _ns = mutate_jobs(_term_sub)
            if _ns:
                launcher_log(
                    f"Stuck-submitted: {_ns} iş failed yapıldı "
                    f"(slot serbest bırakıldı, requeue edilebilir)")

        if not candidates:
            return

        target = candidates[0]
        # Attempt sayacını + backoff'u şimdi güncelle (retry hammer'ı önle)
        attempt_idx = target.harvest_attempts
        backoff_idx = min(attempt_idx, len(STALE_RESUME_BACKOFF_SEC) - 1)
        backoff = STALE_RESUME_BACKOFF_SEC[backoff_idx]
        jobs = load_jobs()
        for j in jobs:
            if j.id == target.id:
                j.harvest_attempts += 1
                j.next_harvest_at = now + backoff
                break
        save_jobs(jobs)

        launcher_log(
            f"Stale-resume sweeper: spawning thread for job {target.id} "
            f"(attempt {attempt_idx + 1}/{STALE_RESUME_MAX_ATTEMPTS}, "
            f"age={int((now - target.started_at) / 60)}min, "
            f"next backoff after fail: {backoff // 60}min)"
        )
        t = threading.Thread(
            target=self._run_stale_resume,
            args=(target.id, attempt_idx + 1),
            name=f"stale-{target.id}",
            daemon=True,
        )
        self._resume_threads[target.id] = t
        t.start()

    def _reconcile_orphans_on_start(self) -> None:
        """Restart sonrası öksüz kalan işleri otomatik kurtar.

        systemctl restart anında pipeline thread'leri ölür ama job'lar
        'generating' kalır (öksüz). Google tarafında gen sürüyor/bitti olabilir.
        Eskiden bunlar 90dk yaş eşiğini bekliyordu (veya elle started_at geri
        alınıyordu). Şimdi: process başlangıcında (hiçbir pipeline thread'i
        yaşamıyor — güvenli) hepsini sweeper'a arm et → 2dk içinde notebook
        kontrol edilip video indirilmeye başlanır."""
        def _m(jobs_all):
            n = 0
            now2 = time.time()
            for j in jobs_all:
                if (j.status == "generating" and j.harvest_status == "skip"
                        and (j.notebook_url or "").strip()
                        and not (j.video_local_path or "").strip()):
                    j.harvest_attempts = 0
                    j.next_harvest_at = now2 + 120  # 2dk sonra ilk kontrol
                    n += 1
                elif j.status in ("running", "submitted"):
                    # Pipeline ortasında restart'la ölmüş (thread yok) → 15dk reaper'ı
                    # BEKLEME, HEMEN taze retry farklı hesapta. tried_profiles korunur.
                    j.status = "queued"
                    j.profile_id = ""
                    j.profile_name = ""
                    j.notebook_url = ""
                    j.error = ""
                    j.started_at = 0.0
                    j.finished_at = 0.0
                    j.pid = 0
                    j.harvest_status = "pending"
                    j.harvest_attempts = 0
                    j.next_harvest_at = 0.0
                    j.next_dispatch_at = 0.0
                    n += 1
            return n

        n = mutate_jobs(_m)
        if n:
            launcher_log(
                f"Startup reconciliation: {n} öksüz iş kurtarıldı "
                f"(generating→sweeper, running/submitted→taze retry farklı hesapta)")

    def _cleanup_uploaded_mp4s(self) -> None:
        """Azure'a yüklenmiş job'ların lokal MP4'lerini sil — disk sızıntısı önler.
        done + video_remote_url + video_local_path var → dosyayı sil + alanı temizle.
        Saatlik (auth-health kadansı) + boot'ta çalışır; mevcut backlog'u da temizler."""
        jobs = load_jobs()
        targets: list = []
        for j in jobs:
            if (j.status == "done" and (j.video_remote_url or "").strip()
                    and (j.video_local_path or "").strip()):
                p = APP_DIR / j.video_local_path
                if p.exists() and p.suffix == ".mp4":
                    targets.append((j.id, p))
        if not targets:
            return
        freed = 0
        cleaned: set = set()
        for jid, p in targets:
            try:
                sz = p.stat().st_size
                p.unlink()
                freed += sz
                cleaned.add(jid)
            except OSError:
                pass
        if cleaned:
            def _m(js):
                for j in js:
                    if j.id in cleaned:
                        j.video_local_path = ""
                return js

            mutate_jobs(_m)
            launcher_log(
                f"MP4 cleanup: {len(cleaned)} lokal dosya silindi "
                f"({freed // 1024 // 1024}MB boşaltıldı)")

        # B6: Revize parent video sızıntısı. Revize işleri parent'ın tam MP4'ünü
        # job_assets/<id>/_parent_video.mp4'e indiriyor (revize için source).
        # done+uploaded olunca artık gereksiz ama hiç silinmiyordu (her revize
        # tam-boy video sızdırır). done revize işlerinin parent videosunu sil.
        pfreed = 0
        pcleaned: set = set()
        for j in jobs:
            if not (j.parent_job_id and j.status == "done"
                    and (j.video_remote_url or "").strip()):
                continue
            pv = JOB_ASSETS_DIR / j.id / "_parent_video.mp4"
            if pv.exists():
                try:
                    pfreed += pv.stat().st_size
                    pv.unlink()
                    pcleaned.add(j.id)
                except OSError:
                    pass
        if pcleaned:
            def _mp(js):
                for j in js:
                    if j.id in pcleaned:
                        j.revision_video_local = ""
                return js

            mutate_jobs(_mp)
            launcher_log(
                f"Parent-video cleanup: {len(pcleaned)} revize parent MP4 silindi "
                f"({pfreed // 1024 // 1024}MB boşaltıldı)")

        # Gemini ile üretilen ama seçilmeyen görseller (gen_images/) 2 günden
        # eskiyse sil — seçilenler zaten job_assets'e kopyalanıyor, orijinal
        # disposable. Aksi halde disk sızıntısı (her üretim 4×~400KB).
        try:
            _cut = time.time() - 2 * 24 * 3600
            _gfreed = 0
            _gn = 0
            for f in GEN_IMAGES_DIR.glob("*"):
                try:
                    if f.is_file() and f.stat().st_mtime < _cut:
                        _gfreed += f.stat().st_size
                        f.unlink()
                        _gn += 1
                except OSError:
                    pass
            if _gn:
                launcher_log(
                    f"gen_images cleanup: {_gn} eski üretilen görsel silindi "
                    f"({_gfreed // 1024 // 1024}MB)")
        except Exception:
            pass

    def _run_stale_resume(self, job_id: str, attempt: int) -> None:
        """resume_via_notebooklm thread wrapper. Log + state cleanup."""
        try:
            ok, msg = self.resume_via_notebooklm(job_id)
            if ok:
                launcher_log(
                    f"Stale-resume OK (attempt {attempt}) {job_id}: {msg[:120]}"
                )
            else:
                launcher_log(
                    f"Stale-resume FAIL (attempt {attempt}/{STALE_RESUME_MAX_ATTEMPTS}) "
                    f"{job_id}: {msg[:160]}"
                )
        except Exception as e:
            launcher_log(
                f"Stale-resume CRASH (attempt {attempt}) {job_id}: "
                f"{type(e).__name__}: {e}"
            )

    def _auto_init_check(self) -> None:
        """auth.json yazılmış profilleri otomatik 'initialized=True' yap.
        Kullanıcının elle 'Login tamamlandı' butonuna basmasına gerek kalmaz.

        ÖNEMLİ: Sadece auth.json varlığı + size kontrolü YETERLİ DEĞİL.
        Chromium signin sayfasında bile Google'ın anonim cookies'i (NID,
        CONSENT) var ve >100 byte JSON üretiyor. Asıl auth doğrulaması için
        notebooklm-py smoke_test ile gerçek NotebookLM endpoint'ine bir
        notebook list çağrısı yapılır — sadece bu geçerse initialized=True.

        Smoke test ~1-2 sn sürer. Polling thread'inde çalıştığı için ana
        UI'yi etkilemez. initialized=False olanlarda 1 kez koşar, sonuca
        göre kararı verir; True olursa bir daha çalışmaz."""
        from notebooklm_client import smoke_test as _nlm_smoke

        profiles = load_profiles()
        changed = False
        for p in profiles:
            if p.initialized:
                continue
            auth_path = PROFILES_DIR / p.id / "auth.json"
            if not (auth_path.exists() and auth_path.stat().st_size > 100):
                continue
            # auth.json son fail'den daha yeni mi? Yoksa smoke_test'i her
            # polling'de tekrarlamak boşa yük (1-2 sn HTTP request). Sadece
            # auth.json yeniden yazılmışsa (kullanıcı yeniden login etmişse)
            # tekrar dene.
            auth_mtime = auth_path.stat().st_mtime
            last_fail = self._smoke_fail_ts.get(p.id, 0.0)
            if last_fail and auth_mtime <= last_fail:
                continue  # auth.json hâlâ aynı dosya — tekrar smoke etme
            try:
                ok, msg = _nlm_smoke(p.id)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            if ok:
                p.initialized = True
                changed = True
                # Başarı: fail cache'i temizle (sonradan deinit olursa
                # smoke yeniden çalışabilsin)
                self._smoke_fail_ts.pop(p.id, None)
                launcher_log(
                    f"Auto-init: profile {p.name} ({p.id}) smoke_test PASSED — initialized=True. {msg}"
                )
            else:
                # auth.json var ama yetki yok — login tamamlanmadı.
                # initialized=False bırak, UI ⚪ ve 'Hesabı aktive et' göstersin.
                self._smoke_fail_ts[p.id] = time.time()
                launcher_log(
                    f"Auto-init: profile {p.name} ({p.id}) auth.json var "
                    f"ama smoke_test FAIL — initialized=False bırakıldı. {msg}"
                )
        if changed:
            save_profiles(profiles)

    def _handle_auth_failure(self, profile_id: str, profile_name: str,
                             job_id: str, err_msg: str) -> None:
        """Auth fail merkezi handler — 3 dağınık akış yerine tek giriş noktası.

        Yapılan iş:
        1. Job'u "queued"a geri al (status reset, profile_id temizle) — quota
           pattern'iyle aynı. Dispatcher başka müsait profile'a gönderir.
           Job'un error alanına AUTH_FAIL: prefix'i bırak (debugging için).
        2. Profile init=False yap (idempotent — sadece ilk transition aktif iş yapar):
           - auth.json sil ki sahte initialized=True tekrar tetiklenmesin
           - Slack notification gönder (admin re-login yapması için)
        3. İkinci ve sonraki çağrılar (zaten init=False ise) — sadece job
           requeue, Slack/file delete tekrar yapılmaz (spam önlenir).
        """
        # 1) Job'u queued'a geri al
        jobs_all = load_jobs()
        target_title = ""
        for j in jobs_all:
            if j.id == job_id:
                target_title = j.title
                j.status = "queued"
                # AUTH_FAIL marker — log/debug için, dispatch'te clear olur
                j.error = f"AUTH_FAIL ({profile_name}): {str(err_msg)[:200]}"
                j.profile_id = ""
                j.profile_name = ""
                j.started_at = 0.0
                j.finished_at = 0.0
                j.notebook_url = ""
                j.pid = 0
                break
        save_jobs(jobs_all)
        launcher_log(
            f"Job {job_id} AUTH_FAIL on {profile_name} → requeued: {err_msg[:120]}"
        )

        # 2) Profile state transition (idempotent)
        ps = load_profiles()
        was_initialized = False
        for p in ps:
            if p.id == profile_id:
                was_initialized = p.initialized
                p.initialized = False
                break
        if was_initialized:
            save_profiles(ps)
            # auth.json sil → sahte initialized tekrar tetiklenmesin
            try:
                auth_p = PROFILES_DIR / profile_id / "auth.json"
                if auth_p.exists():
                    auth_p.unlink()
            except OSError:
                pass
            # Slack: ilk transition'da TEK kez bildir
            send_slack_message(
                f"🚨 *Hesap yetkisiz:* `{profile_name}`\n"
                f"NotebookLM session expired. Bu profile'a artık iş gönderilmiyor.\n"
                f"Admin panelden 'Yeniden giriş' yapılması gerek.\n"
                f"Job: _{target_title[:120]}_ (kuyruğa geri alındı, başka profile denenecek)"
            )
            launcher_log(
                f"Profile {profile_name} ({profile_id}) AUTH_FAIL transition: "
                f"init=True → init=False, auth.json silindi, Slack gönderildi"
            )
        # else: zaten init=False — başka bir job aynı anda failed olmuş, no-op

    def _mark_profile_expired(self, profile_id: str, profile_name: str,
                              reason: str) -> bool:
        """Job context'i olmadan profil expire mark — proaktif health check için.

        _handle_auth_failure'ın job-agnostic kardeşi. Idempotent: zaten
        initialized=False ise no-op (Slack spam önlenir).

        Returns: True if transition happened (init=True → False), False if no-op.
        """
        ps = load_profiles()
        was_initialized = False
        for p in ps:
            if p.id == profile_id:
                was_initialized = p.initialized
                p.initialized = False
                break
        if not was_initialized:
            return False
        save_profiles(ps)
        # auth.json sil → sahte initialized tekrar tetiklenmesin
        try:
            auth_p = PROFILES_DIR / profile_id / "auth.json"
            if auth_p.exists():
                auth_p.unlink()
        except OSError:
            pass
        # Slack alert — actionable hint ile (login.sh komutu)
        send_slack_message(
            f"🚨 *Hesap session expired (proaktif tespit):* `{profile_name}`\n"
            f"Sebep: {reason[:200]}\n"
            f"Profile init=False yapıldı, dispatcher yeni job göndermiyor.\n\n"
            f"*Düzeltmek için Mac'te çalıştır:*\n"
            f"```\n./deploy/login.sh\n```\n"
            f"Listeden `{profile_name}` seç → Chrome açılır → login → "
            f"otomatik sunucuya rsync."
        )
        launcher_log(
            f"Profile {profile_name} ({profile_id}) PROACTIVE_AUTH_FAIL: "
            f"init=True → init=False, auth.json silindi, Slack gönderildi. "
            f"Reason: {reason[:120]}"
        )
        return True

    def _auth_health_check_round(self) -> None:
        """Proaktif auth check: initialized profillere smoke_test çağırıp
        expired olanları mark et. Worker._loop her N saatte bir tetikler.

        Plan A reactive flow'unda 1. job kaybını önler — submit zamanı yerine
        periyodik kontrolde expire keşfedilir, Slack alert ile admin uyarılır.
        """
        try:
            from notebooklm_client import smoke_test as _nlm_smoke
        except ImportError:
            return  # library yok → check anlamsız
        ps = load_profiles()
        initialized = [p for p in ps if p.initialized]
        if not initialized:
            return
        # B8: Aktif gen'i olan profili smoke-test ETME. smoke_test ayrı bir
        # client açar; library cookie rotasyonu auth.json'a geri yazıyor
        # (per-client lock, dosyada yarış) → in-flight pipeline'ın cookie zincirini
        # clobber edip mid-gen 401'e yol açabilir. İş bitince sonraki round test eder.
        try:
            in_flight_pids = {
                j.profile_id for j in load_jobs()
                if j.status in ("running", "generating", "submitted")
                and (j.profile_id or "")
            }
        except Exception:
            in_flight_pids = set()
        launcher_log(
            f"Auth health check başladı — {len(initialized)} initialized profile"
        )
        flagged = 0
        for p in initialized:
            if p.id in in_flight_pids:
                launcher_log(
                    f"Auth health: {p.name} aktif gen var → smoke atlandı "
                    f"(cookie clobber / mid-gen 401 önleme)")
                continue
            try:
                ok, msg = _nlm_smoke(p.id)
            except Exception as e:
                # Network/timeout vs. — geçici hata sayıp atla, sonraki round'a bırak
                launcher_log(
                    f"Auth health: {p.name} smoke exception (skip this round): "
                    f"{type(e).__name__}: {str(e)[:120]}"
                )
                continue
            if ok:
                continue
            # Fail edebilir 2 sebepten: auth expired (önemli) veya transient
            # (geçici). "Authentication expired", "Missing required cookies",
            # "Redirected to: accounts.google.com" gibi keyword'ler kalıcı.
            m_low = (msg or "").lower()
            is_real_auth_fail = (
                "auth" in m_low
                or "expired" in m_low
                or "missing required cookies" in m_low
                or "accounts.google.com" in m_low
                or "not logged in" in m_low
                or "redirect" in m_low
            )
            if not is_real_auth_fail:
                launcher_log(
                    f"Auth health: {p.name} smoke FAIL ama auth-fail değil "
                    f"(transient varsayıldı): {msg[:140]}"
                )
                continue
            # TOLERANT: tek fail, residential proxy IP rotasyonu blip'i olabilir
            # (IP döner → o an refresh "yeni IP" diye reddedilir, sonraki stabil
            # IP'de düzelir). Hemen ölü işaretleme — kısa bekleyip TEKRAR smoke et;
            # ikisi de fail ederse gerçekten ölü. İlk fail transient ise re-smoke
            # OK gelir → gereksiz init=False + re-login uyarısı engellenir.
            time.sleep(5)
            try:
                ok2, _msg2 = _nlm_smoke(p.id)
            except Exception:
                ok2 = True  # exception → transient varsay, ölü işaretleme
            if ok2:
                launcher_log(
                    f"Auth health: {p.name} ilk smoke FAIL ama re-smoke OK "
                    f"→ transient blip (IP rotasyonu); init=True korundu."
                )
                continue
            if self._mark_profile_expired(p.id, p.name, reason=msg[:240]):
                flagged += 1
        if flagged:
            launcher_log(
                f"Auth health check bitti: {flagged}/{len(initialized)} profile "
                f"expired olarak mark edildi"
            )

        # Auth-fail blip'inde HARD-fail olmuş (re-queue edilmeyip "failed" kalmış)
        # işleri otomatik kurtar: auth-pattern hatalı, SON 24 saatte düşmüş,
        # auth_retry_count<3 olan failed işleri queue'ya geri al → dispatcher
        # sağlıklı bir hesaba yeniden gönderir. Bounded sayaç sonsuz loop önler.
        _cut = time.time() - 24 * 3600
        _rq = {"n": 0}

        def _auth_requeue(jobs_all):
            for j in jobs_all:
                if j.status != "failed":
                    continue
                if getattr(j, "auth_retry_count", 0) >= 3:
                    continue
                if (j.title or "").startswith("[KOTA]"):
                    continue
                if (j.video_remote_url or "").strip():
                    continue
                ts = j.finished_at or j.started_at or j.created_at
                if not ts or ts < _cut:
                    continue
                if not is_auth_failure("", j.error or ""):
                    continue
                j.status = "queued"
                j.profile_id = ""
                j.profile_name = ""
                j.notebook_url = ""
                j.error = ""
                j.started_at = 0.0
                j.finished_at = 0.0
                j.pid = 0
                j.harvest_status = "pending"
                j.harvest_attempts = 0
                j.next_harvest_at = 0.0
                j.auth_retry_count = getattr(j, "auth_retry_count", 0) + 1
                _rq["n"] += 1
            return jobs_all

        mutate_jobs(_auth_requeue)
        if _rq["n"]:
            launcher_log(
                f"Auth-fail auto-requeue: {_rq['n']} iş queue'ya geri alındı "
                f"(transient blip kurtarma, bounded auth_retry_count<3)"
            )

    def _today_count_for(self, jobs: list[Job], profile_id: str) -> int:
        today = date.today()
        n = 0
        for j in jobs:
            if j.profile_id != profile_id:
                continue
            if j.status not in COUNTED_STATUSES:
                continue
            # [KOTA] markerları (failed statü) üretilmiş video değil, yalnız
            # kota-takip kaydı — daily_limit'i TÜKETMEMELİ. Aksi halde gece
            # kota-fail'leri günlük limiti doldurup 8h-blok açıldıktan sonra
            # bile dispatch'i bloklar (panel ile çelişir: panel [KOTA] dışlar).
            if (j.title or "").startswith("[KOTA]"):
                continue
            ts = j.started_at or j.created_at
            try:
                d = datetime.fromtimestamp(ts).date()
            except (OSError, OverflowError, ValueError):
                continue
            if d == today:
                n += 1
        return n

    def _busy_count_for(self, jobs: list[Job], profile_id: str) -> int:
        # In-flight = running + generating + submitted (HEPSİ slot işgal eder).
        # ÖNCEDEN sadece "running" sayılıyordu → iş "running"dan "generating"e
        # geçince busy=0 görünüyor, dispatcher aynı hesaba bir tane daha
        # gönderiyordu → max_concurrent=1 olsa bile BURST (8 iş aynı anda
        # submit → 2-core sunucu load 38). Artık generating de sayılır →
        # max_concurrent gerçekten uygulanır, eşzamanlı submit sınırlanır.
        return sum(
            1 for j in jobs
            if j.profile_id == profile_id
            and j.status in ("running", "generating", "submitted")
        )

    def _quota_blocked_today(self, jobs: list[Job], profile_id: str) -> bool:
        """NotebookLM kota dolu mesajı son N saat içinde yiyen profil — pas geç.

        Önceden 'bugün UTC date' check'i vardı. Sorun: NotebookLM kota reset'i
        Pacific time (~07-08:00 UTC) iken bizim UTC date 00:00'da rollover
        oluyordu → 00:00 UTC'de retry, Google hâlâ kotalı, empirical fail
        → 1 gün kayıp. Şimdi son 8 saat içinde kota hatası yedikse blokla;
        8 saatte bir self-correctively retry → Google reset'e otomatik denk
        gelir, max 8 saat overshoot.
        """
        cutoff = time.time() - QUOTA_BLOCK_HOURS * 3600
        for j in jobs:
            if j.profile_id != profile_id:
                continue
            if not j.error:
                continue
            if not _is_quota_error(j.error):
                continue
            ts = j.finished_at or j.started_at or j.created_at
            if ts > cutoff:
                return True
        return False

    def _dispatch_round(self) -> None:
        profiles = [p for p in load_profiles() if p.initialized]
        if not profiles:
            return
        jobs = load_jobs()
        _now = time.time()
        # Backoff'ta olan (transient retry beklemede) queued işleri dispatch ETME.
        queued = [j for j in jobs if j.status == "queued"
                  and (getattr(j, "next_dispatch_at", 0) or 0) <= _now]
        # ÖNCELİK sırası: yüksek priority önce (öncelikli batch sıranın başına),
        # eşitlikte FIFO (created_at). priority=0 normal işler en sonda.
        queued.sort(key=lambda j: (-(getattr(j, "priority", 0) or 0), j.created_at))
        if not queued:
            return

        # === ADIM B: DEDUP / RESUME GUARD (duplicate notebook önle) ===
        # Kök sebep: notebook+video yapmış bir iş (false-[KOTA] / restart'ta ölen
        # thread) re-queue olup TEKRAR dispatch edilince 2. notebook yaratılıyor
        # (aynı başlık birden çok hesapta üretiliyor). Fix:
        #  (1) İş zaten notebook_url'e sahipse → fresh submit DEĞİL, resume'a
        #      yönlendir (generating+skip → stale-resume sweeper indirir).
        #  (2) Aynı (title, batch) başka iş zaten üretiyor/ürettiyse → skip
        #      (batch-aware: farklı batch = farklı versiyon, dokunma).
        _producing = set()
        for j in jobs:
            if (j.video_remote_url or "").strip() or j.status in (
                "done", "generating", "running", "submitted"
            ):
                _producing.add(((j.title or "").strip(), (j.batch_id or ""),
                                getattr(j, "version", 0)))
        _resume_ids, _skip_ids = [], []
        for j in queued:
            if (j.notebook_url or "").strip():
                _resume_ids.append(j.id)
            elif ((j.title or "").strip(), (j.batch_id or ""),
                  getattr(j, "version", 0)) in _producing:
                _skip_ids.append(j.id)
        if _resume_ids or _skip_ids:
            _ri, _si = set(_resume_ids), set(_skip_ids)

            def _dedup(js):
                for x in js:
                    if x.status != "queued":
                        continue
                    if x.id in _ri:
                        x.status = "generating"
                        x.harvest_status = "skip"
                        x.harvest_attempts = 0
                        x.next_harvest_at = 0
                    elif x.id in _si:
                        x.status = "stopped"
                        x.error = ("dedup: aynı başlık bu batch'te zaten "
                                   "üretildi/üretiliyor")
                return js

            mutate_jobs(_dedup)
            if _resume_ids:
                launcher_log(
                    f"Step B dedup: {len(_resume_ids)} iş notebook_url'lü → "
                    f"resume'a yönlendirildi (fresh dispatch yok)"
                )
            if _skip_ids:
                launcher_log(
                    f"Step B dedup: {len(_skip_ids)} iş skip (aynı başlık "
                    f"zaten üretiliyor)"
                )
            jobs = load_jobs()
            queued = [j for j in jobs if j.status == "queued"
                      and (getattr(j, "next_dispatch_at", 0) or 0) <= time.time()]
            queued.sort(key=lambda j: (-(getattr(j, "priority", 0) or 0), j.created_at))
            if not queued:
                return

        # Profil başına slot hesapla
        slot_map: dict[str, int] = {}
        for p in profiles:
            # NotebookLM tarafı kota mesajı yedi mi? Yediyse bugün pas geç.
            if self._quota_blocked_today(jobs, p.id):
                slot_map[p.id] = 0
                continue
            busy = self._busy_count_for(jobs, p.id)
            today = self._today_count_for(jobs, p.id)
            slots = max(0, p.max_concurrent - busy)
            if p.daily_limit > 0:
                slots = min(slots, max(0, p.daily_limit - today))
            slot_map[p.id] = slots

        # Env-aware dispatch:
        # Her queued job için, o job'un env'ine uygun (initialized + slot var)
        # profilleri filtrele, en az kullanılan profile assign et.
        # Dev job dev profile'a, prod job prod profile'a gider — kotalar ayrı.
        # Env-mismatch detection: hangi env'lerde initialized profile var,
        # uyarı throttling için tut.
        envs_with_profile = set(
            (p.environment or "prod").strip().lower() for p in profiles
        )
        # GLOBAL submission throttle: şu an kaç iş aktif submit ediliyor?
        # ("running"/"submitted" = heavy Chromium fazı; generating sayılmaz.)
        # Bu round'da yeni launch ettikçe sayacı artır; limit dolunca dur →
        # kalan kuyruk sonraki round'larda (her 2sn) kademeli akar.
        submitting_now = sum(
            1 for j in jobs if j.status in ("running", "submitted")
        )
        # TOPLAM in-flight (running+generating+submitted) = Google'da eşzamanlı
        # üretilen video sayısı. Bu sınır dolu ise yeni iş başlatma → burst yok.
        inflight_now = sum(
            1 for j in jobs if j.status in ("running", "generating", "submitted")
        )
        any_dispatched = False
        for job in list(queued):
            if submitting_now >= GLOBAL_MAX_SUBMITTING:
                break  # global submission limiti — gerisi sonraki round'da
            if inflight_now >= GLOBAL_MAX_INFLIGHT:
                break  # eşzamanlı üretim limiti — gerisi sonraki round'da
            job_env = (job.environment or "prod").strip().lower()
            if job_env not in ALLOWED_ENVS:
                job_env = "prod"
            matching = [p for p in profiles
                        if (p.environment or "prod").strip().lower() == job_env
                        and slot_map.get(p.id, 0) > 0]
            if not matching:
                # Env'de hiç initialized profile yoksa bu sessiz takılma'dır
                # (slot dolu olmasıyla aynı şey değil). Saatte 1× uyarı bas.
                if job_env not in envs_with_profile:
                    now = time.time()
                    if now - self._env_warn_ts.get(job_env, 0.0) > 3600:
                        env_queued_count = sum(
                            1 for j in queued
                            if (j.environment or "prod").strip().lower() == job_env
                        )
                        launcher_log(
                            f"⚠ Dispatcher silent-skip: env={job_env!r} için "
                            f"initialized profile yok ({env_queued_count} queued job "
                            f"sonsuza kadar bekleyebilir). Submit env'i doğru mu?"
                        )
                        self._env_warn_ts[job_env] = now
                # Bu env için profil yok veya tüm slot'lar dolu —
                # job kuyrukta bekler, sonraki round'da tekrar denenir
                continue
            # Bu iş için DENENMEMİŞ profilleri tercih et → transient retry farklı
            # hesaba düşer (kullanıcının "hata aldıysan başka hesapla dene" akışı).
            # Hepsi denendiyse round-robin'e düş (tekrar denenebilir). En eski
            # kullanılan önce (round-robin).
            _tried = set(getattr(job, "tried_profiles", None) or [])
            _untried = [p for p in matching if p.id not in _tried]
            _pool = _untried if _untried else matching
            _pool.sort(key=lambda p: p.last_used)
            target_profile = _pool[0]
            self._launch_job(job, target_profile)
            slot_map[target_profile.id] -= 1
            submitting_now += 1  # global submission sayacı
            inflight_now += 1    # global eşzamanlı üretim sayacı
            target_profile.last_used = time.time()
            any_dispatched = True

        if any_dispatched:
            save_profiles(load_profiles_with_updates(profiles))
            # job state launch sırasında güncellendi, ama save_jobs Worker
            # tarafından _launch_job içinde yapılıyor

    def _launch_job(self, job: Job, profile: Profile) -> None:
        job.profile_id = profile.id
        job.profile_name = profile.name
        job.status = "running"
        job.started_at = time.time()
        job.error = ""
        # Bu profil bu iş için denendi → transient retry'da farklı hesaba gidilsin.
        _tp = list(job.tried_profiles or [])
        if profile.id not in _tp:
            _tp.append(profile.id)
        job.tried_profiles = _tp

        # Phase E: Job paketini hazırla (script.txt + style guides + images)
        # Tek temp klasörde topla, automator'a path listeleri olarak ver.
        # Source isimleri = filename (NotebookLM'de görünür).
        job_pack_dir = JOB_ASSETS_DIR / job.id
        job_pack_dir.mkdir(parents=True, exist_ok=True)

        # ===== Phase 4: Revision job — parent video MP4'ünü indir =====
        # Bu, image_paths'in BAŞINA eklenecek (ilk source önceki video olsun)
        revision_video_path: Optional[Path] = None
        if job.parent_job_id and job.revision_video_url:
            launcher_log(
                f"Job {job.id}: revision → parent video indiriliyor "
                f"({job.revision_video_url[:60]}...)"
            )
            target = job_pack_dir / "_parent_video.mp4"
            got = download_video_for_revision(job.revision_video_url, target)
            if got and got.exists():
                revision_video_path = got
                try:
                    job.revision_video_local = str(got.resolve().relative_to(APP_DIR))
                except (ValueError, OSError):
                    job.revision_video_local = str(got)
                launcher_log(
                    f"Job {job.id}: parent video indirildi "
                    f"({got.stat().st_size // (1024 * 1024)} MB)"
                )
            else:
                launcher_log(
                    f"Job {job.id}: parent video indirilemedi — devam, ama "
                    f"revize source eksik kalacak"
                )

        # 1) Script'i (revize için: revision_instructions) .txt olarak yaz
        safe_title = re.sub(r"[^A-Za-z0-9_\-]+", "_", job.title or "Script")[:60].strip("_") or "Script"
        # Revize ise script alanı revize talimatlarını içerir → adı RevisionInstructions
        if job.parent_job_id:
            script_filename = f"{safe_title}_RevisionInstructions.txt"
        else:
            script_filename = f"{safe_title}_Script.txt"
        script_path = job_pack_dir / script_filename
        try:
            script_path.write_text(job.text or "", encoding="utf-8")
        except OSError as e:
            launcher_log(f"Job {job.id}: script.txt yazma hatası: {e}")
            script_path = None

        # 1.b) Learning Objectives companion (bulk Drive _lo.docx)
        lo_path: Optional[Path] = None
        if (job.learning_objectives or "").strip():
            try:
                lo_path = job_pack_dir / f"{safe_title}_LearningObjectives.txt"
                lo_path.write_text(job.learning_objectives, encoding="utf-8")
                launcher_log(f"Job {job.id}: learning objectives source eklendi ({lo_path.name})")
            except OSError as e:
                launcher_log(f"Job {job.id}: LO write hatası: {e}")
                lo_path = None

        # 1.c) Sabit execution guide — 4 ayrı protokol dosyası (her job'a otomatik)
        # NotebookLM source panelinde her protokol ayrı görünür, Cinematic gen
        # her sahnede 4 ayrı kuralı referans alır.
        guide_paths = write_execution_guide_sources(job_pack_dir)
        if guide_paths:
            launcher_log(
                f"Job {job.id}: {len(guide_paths)} execution guide source eklendi "
                f"({', '.join(p.name for p in guide_paths)})"
            )

        # 2) Image'leri indir
        image_paths: list[Path] = []
        if job.assets:
            n_sel = sum(1 for a in job.assets if a.get("selected_image"))
            launcher_log(f"Job {job.id}: indiriliyor → {n_sel} selected image")
            try:
                image_paths = download_job_images(job.id, job.assets)
                launcher_log(f"Job {job.id}: {len(image_paths)} image indirildi")
            except Exception as e:
                launcher_log(f"Job {job.id}: image download hata, devam: {e}")

        # 3) Custom prompt'u disk'e yaz.
        # - Kullanıcı edit ettiyse onu kullan
        # - Boş bıraktıysa default template + dinamik source listesi
        # Bu dosya hem NotebookLM-py path'inde source olarak yüklenir, hem
        # legacy nlm path'inde CLI escape sorunu yaşamamak için kullanılır.
        prompt_path: Optional[Path] = None
        _prompt_text = (job.custom_prompt or "").strip()
        if not _prompt_text:
            # User edit etmedi → default template'i şimdi render et
            try:
                _prompt_text = render_custom_prompt(
                    DEFAULT_CUSTOM_PROMPT_TEMPLATE,
                    job.title or "",
                    job.assets or [],
                    has_learning_objectives=bool((job.learning_objectives or "").strip()),
                )
            except Exception as _e:
                launcher_log(f"Job {job.id}: default prompt render hatası: {_e}")
                _prompt_text = ""
        if _prompt_text:
            prompt_path = job_pack_dir / "_custom_prompt.txt"
            try:
                prompt_path.write_text(_prompt_text, encoding="utf-8")
            except OSError as e:
                launcher_log(f"Job {job.id}: custom prompt yazma hatası: {e}")
                prompt_path = None

        # ---- Automator komutu ----
        # text args'ı boş geçiyoruz: script artık .txt dosyası olarak upload
        # ediliyor; automator "Copied text" akışı yerine "Add sources → Upload"
        # akışını kullanacak. Backward-compat için text yine pozisyonel arg.
        cmd = [
            PYTHON_BIN,
            str(APP_DIR / "notebooklm_automator.py"),
            job.text,  # legacy fallback — automator script_path varsa onu kullanır
            "--profile-dir", str(PROFILES_DIR / profile.id),
            "--authuser", str(profile.authuser),
            "--job-id", job.id,
            "--json-events",
            "--no-wait-input",
            "--download-dir", str(DOWNLOADS_DIR),
            "--screenshots-dir", str(SCREENSHOTS_DIR),
        ]
        cmd.append("--headless" if profile.headless else "--no-headless")

        # Revize MP4'ü image_paths'in BAŞINA ekle → ilk upload edilen source bu olur
        # (Custom prompt'ta "Source 1 is the PREVIOUS VIDEO" diyorsa eşleşir)
        if revision_video_path and revision_video_path.exists():
            image_paths = [revision_video_path] + image_paths

        if script_path is not None:
            cmd += ["--script-file", str(script_path)]
        if image_paths:
            cmd.append("--images")
            cmd.extend(str(p) for p in image_paths)
        if prompt_path is not None:
            cmd += ["--custom-prompt-file", str(prompt_path)]

        launcher_log(
            f"Job {job.id} packet: 1 script + {len(image_paths)} attachments"
            f"{' (incl parent video for revision)' if revision_video_path else ''} + "
            f"custom_prompt={'yes' if prompt_path else 'no'}"
        )

        log_path = job_log_path(job.id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Submit path öncelik sırası:
        #   1) notebooklm-py (default, en yeni) — Python native, MP4 download
        #   2) tmc/nlm Go CLI (USE_LEGACY_SUBMIT=1 ile aktif) — fallback
        #   3) Playwright automator (USE_PLAYWRIGHT_SUBMIT=1 ile aktif) — son çare
        use_playwright = os.environ.get("USE_PLAYWRIGHT_SUBMIT", "").strip() == "1"
        use_legacy_nlm = os.environ.get("USE_LEGACY_SUBMIT", "").strip() == "1"
        # Availability fallback chain
        if not _NOTEBOOKLM_AVAILABLE and not use_legacy_nlm and not use_playwright:
            if _NLM_AVAILABLE:
                use_legacy_nlm = True
                launcher_log(
                    f"Job {job.id}: notebooklm-py yok, tmc/nlm fallback'a düşülüyor"
                )
            else:
                use_playwright = True
                launcher_log(
                    f"Job {job.id}: notebooklm-py + tmc/nlm yok, Playwright fallback"
                )

        try:
            log_fp = log_path.open("w", encoding="utf-8", buffering=1)
            log_fp.write(f"# Job {job.id} — Profile {profile.name} ({profile.id})\n")

            if use_playwright:
                # ===== LEGACY Playwright submit path =====
                log_fp.write(f"# Submit path: Playwright (subprocess automator)\n")
                log_fp.write(f"# Cmd: {' '.join(cmd)}\n\n")
                log_fp.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(APP_DIR),
                    text=True,
                    bufsize=1,
                )
                job.pid = proc.pid
                with self._proc_lock:
                    self._procs[job.id] = proc
                t = threading.Thread(
                    target=self._stdout_reader,
                    args=(job.id, proc, log_fp),
                    name=f"stdout-{job.id}",
                    daemon=True,
                )
                t.start()
                launcher_log(
                    f"Job {job.id} launched via Playwright on profile "
                    f"{profile.name} (pid={proc.pid})"
                )
            elif use_legacy_nlm:
                # ===== LEGACY: tmc/nlm Go CLI =====
                log_fp.write(f"# Submit path: nlm CLI (tmc/nlm, legacy)\n")
                log_fp.write(
                    f"# Files: script={script_path}, "
                    f"images={len(image_paths)}, "
                    f"prompt_chars={len(job.custom_prompt or '')}\n\n"
                )
                log_fp.flush()
                t = threading.Thread(
                    target=self._run_job_via_nlm,
                    args=(job.id, profile, script_path, image_paths,
                          job.custom_prompt or "", log_fp),
                    name=f"nlm-{job.id}",
                    daemon=True,
                )
                t.start()
                launcher_log(
                    f"Job {job.id} launched via tmc/nlm on profile "
                    f"{profile.name} (legacy)"
                )
            else:
                # ===== NEW: notebooklm-py (Python native) =====
                log_fp.write(f"# Submit path: notebooklm-py (Python native)\n")
                log_fp.write(
                    f"# Files: script={script_path}, "
                    f"images={len(image_paths)}, "
                    f"prompt_chars={len(job.custom_prompt or '')}\n\n"
                )
                log_fp.flush()
                t = threading.Thread(
                    target=self._run_job_via_notebooklm,
                    args=(job.id, profile, script_path, image_paths,
                          job.custom_prompt or "", log_fp, guide_paths,
                          prompt_path, lo_path),
                    name=f"nbpy-{job.id}",
                    daemon=True,
                )
                t.start()
                launcher_log(
                    f"Job {job.id} launched via notebooklm-py on profile "
                    f"{profile.name} (thread)"
                )
        except Exception as e:
            job.status = "failed"
            job.error = f"Launch failed: {e!r}"
            job.finished_at = time.time()
            launcher_log(f"Job {job.id} launch failed: {e!r}")

        # Job state'i ve profile last_used'ı kaydet
        jobs_all = load_jobs()
        for i, j in enumerate(jobs_all):
            if j.id == job.id:
                jobs_all[i] = job
                break
        save_jobs(jobs_all)

        profiles_all = load_profiles()
        for i, p in enumerate(profiles_all):
            if p.id == profile.id:
                p.last_used = profile.last_used
                profiles_all[i] = p
                break
        save_profiles(profiles_all)

    def _run_job_via_notebooklm(self, job_id: str, profile: "Profile",
                                 script_path: Optional[Path],
                                 image_paths: list[Path],
                                 custom_prompt: str,
                                 log_fp,
                                 guide_paths: Optional[list[Path]] = None,
                                 prompt_path: Optional[Path] = None,
                                 lo_path: Optional[Path] = None) -> None:
        """teng-lin/notebooklm-py ile end-to-end pipeline. Worker thread'inde.

        Tek async chain: notebook create → sources add → cinematic generate →
        wait_for_completion (30-40dk) → download MP4. Playwright harvest cycle
        gerekmez — MP4 native indirilir.

        Job state geçişleri:
          running → generating (notebook + sources upload OK, gen başladı)
          generating → done (MP4 indirildi + Azure'a upload sonrası)
        """
        import traceback as _tb

        def on_event(event: str, **payload) -> None:
            """notebooklm_client callback → log + state update.

            Not: parametre adı `name` değil `event` — çünkü payload'da source
            file için `name=...` kwarg'ı geliyor, çakışırsa TypeError verir.
            """
            try:
                items = " ".join(f"{k}={v!r}" for k, v in payload.items() if k != "auth")
                log_fp.write(f"## [{event}] {items[:300]}\n")
                log_fp.flush()
            except Exception:
                pass
            # Önemli event'lerde job state'i güncelle (admin UI ve harvest skip için)
            if event == "notebook_created":
                self._apply_event(job_id, {
                    "type": "notebook_created",
                    "notebook_url": payload.get("url", ""),
                })
            elif event == "video_gen_started":
                self._apply_event(job_id, {
                    "type": "automation_complete",
                    "exit_code": 0,
                    "notebook_url": "",  # zaten set edildi yukarıda
                })
                # notebooklm-py native MP4 download yapacak — harvest dispatcher
                # bu job'a dokunmasın (paralel cookie-fetch'i önle). Atomik.
                try:
                    def _m_skip(jobs_all):
                        for j in jobs_all:
                            if j.id == job_id:
                                j.harvest_status = "skip"
                                break
                    mutate_jobs(_m_skip)
                except Exception:
                    pass

        try:
            # 1. Source paket sırası (user'ın template'iyle bire bir uyumlu):
            #    [1] <Title>_Script.txt
            #    [2] <Title>_LearningObjectives.txt (varsa, _lo.docx companion)
            #    [3] Narrative & Text-Free Execution Guide
            #    [4] Historical Accuracy & Identity Protocol
            #    [5] Fully Realistic Style
            #    [6] _custom_prompt.txt (Role/Task/Constraints)
            #    [7..N] Image'ler
            source_paths: list[Path] = []
            if script_path and script_path.exists():
                source_paths.append(script_path)
            if lo_path and lo_path.exists():
                source_paths.append(lo_path)
            for gp in (guide_paths or []):
                if isinstance(gp, Path) and gp.exists():
                    source_paths.append(gp)
            if prompt_path and prompt_path.exists():
                source_paths.append(prompt_path)
            for p in (image_paths or []):
                if isinstance(p, Path) and p.exists():
                    source_paths.append(p)
            if not source_paths:
                raise NotebookLMClientError(
                    "Hiç source dosyası yok",
                    stage="prep",
                )

            log_fp.write(
                f"## starting notebooklm-py pipeline: {len(source_paths)} sources "
                f"(script={'yes' if script_path else 'no'}, "
                f"lo={'yes' if lo_path else 'no'}, "
                f"guides={len(guide_paths or [])}, "
                f"prompt_brief={'yes' if prompt_path else 'no'}, "
                f"images={len(image_paths or [])}), "
                f"prompt {len(custom_prompt)} chars\n"
            )
            log_fp.flush()

            # 2. Tek senkron çağrı (içeride asyncio.run → tüm pipeline)
            jobs_all = load_jobs()
            target = next((j for j in jobs_all if j.id == job_id), None)
            title = (target.title if target else "Untitled")[:80]
            try:
                result = notebooklm_submit_job(
                    profile_id=profile.id,
                    title=title,
                    source_paths=source_paths,
                    custom_prompt=custom_prompt,  # User'ın yazdığı, guide yok
                    on_event=on_event,
                    language="tr",  # script Türkçe ağırlıklı
                    video_timeout_sec=7200.0,  # 2h — Cinematic gerçekte 60-90dk sürebiliyor; 1h timeout false-failed üretiyordu
                )
            except NotebookLMClientError as e:
                log_fp.write(f"## NotebookLMClientError [{e.stage}]: {e}\n")
                log_fp.flush()
                # --- Quota detection: NotebookLM "daily Cinematic limit"
                # mesajı varsa job'u failed yapma → quota_exceeded event tetikle
                # (mevcut _apply_event flow: failed marker oluşturur ki dispatcher
                # bu profili pas geçsin, ASIL job'u queued'a geri alır → yarın
                # otomatik retry edilir veya başka profile dispatch olur).
                err_msg = str(e).lower()
                # MUĞLAK "artifact was removed from the list" / "disappeared" hatası:
                # library bunu "quota/rate limit VEYA invalid notebook VEYA GEÇİCİ
                # API sorunu olabilir" diye açıklıyor. Bu disclaimer metnindeki
                # 'quota/rate limit' kelimeleri yüzünden EskidEN günlük-kota sanılıp
                # hesap 8h bloklanıyordu → FALSE-POSITIVE (hesap dakikalar sonra
                # üretebiliyor; tech'te bizzat görüldü). Artık kota SAYMA: geçici
                # hata kabul et → bounded auto-requeue (max 2), hesabı BLOKLAMA,
                # [KOTA] marker YARATMA. Gerçek günlük kota zaten "quota"/"limit
                # reached"/"featureunavailable" gibi NET token'larla geliyor.
                ambiguous_transient = (
                    e.stage in ("video_wait", "video_download")
                    and ("artifact was removed" in err_msg
                         or "disappeared from list" in err_msg)
                )
                if ambiguous_transient:
                    # KÖKLÜ ÇÖZÜM (2026-06-12 veri analizi). "artifact was removed
                    # from the list" = Google Cinematic'i BAŞLATIP artifact'ı
                    # tamamlamadan SİLDİ → o notebook'ta video ASLA gelmez
                    # (resume futile: 17/17 list-verify boş döndü). Hata OLASILIKSAL
                    # (~%24), İÇERİĞE/HESABA BAĞLI DEĞİL (aynı script bir hesapta
                    # oluyor, başka denemede olmuyor; aynı hesap hem ✅ hem ❌).
                    #
                    # Eski davranış (aynı notebook'u 10/30/60dk resume + sonra 2 taze)
                    # ~80dk boşa harcayıp 0.25^2≈%6 kalıcı-fail bırakıyordu (5/80).
                    #
                    # Yeni: aynı notebook'u resume ETME → HEMEN farklı hesapta TAZE
                    # dene (tried_profiles rotasyonu + cap=4 throttle), cömert bütçe.
                    # 0.25^8 ≈ %0.001 → pratikte her iş eninde sonunda üretilir.
                    def _m_transient(jobs_all, _e=e):
                        for j in jobs_all:
                            if j.id != job_id:
                                continue
                            n = getattr(j, "transient_retry_count", 0)
                            if n < TRANSIENT_RETRY_MAX:
                                _wait = TRANSIENT_BACKOFF_SEC[
                                    min(n, len(TRANSIENT_BACKOFF_SEC) - 1)]
                                j.status = "queued"
                                j.profile_id = ""
                                j.profile_name = ""
                                j.notebook_url = ""   # artifact silinmiş notebook'u bırak
                                j.error = ""
                                j.started_at = 0.0
                                j.finished_at = 0.0
                                j.pid = 0
                                j.harvest_status = "pending"
                                j.harvest_attempts = 0
                                j.next_harvest_at = 0.0
                                j.transient_retry_count = n + 1
                                # Artan backoff → kötü pencereyi atlat (ilk=hemen)
                                j.next_dispatch_at = time.time() + _wait
                                _wlbl = "hemen" if _wait == 0 else f"{_wait // 60}dk sonra"
                                return f"taze deneme {n + 1}/{TRANSIENT_RETRY_MAX} (farklı hesap, {_wlbl})"
                            j.status = "failed"
                            j.error = (f"{_e.stage}: Google artifact removed — "
                                       f"{TRANSIENT_RETRY_MAX} ayrı denemede de üretmedi")
                            j.finished_at = time.time()
                            return f"failed ({TRANSIENT_RETRY_MAX} deneme tükendi)"
                        return None
                    _r = mutate_jobs(_m_transient)
                    log_fp.write(
                        f"## 'artifact removed' GEÇİCİ (kota DEĞİL) → {_r} — "
                        f"resume YOK, hemen farklı hesapta taze gen\n")
                    log_fp.flush()
                    return
                is_quota = (
                    e.stage in ("video_gen", "video_wait", "video_download")
                    and any(k in err_msg for k in (
                        "quota", "rate limit", "rate-limit", "rate_limit",
                        "daily limit", "limit reached", "exceeded",
                        "too many requests", "429",
                        # Cinematic günlük kota tükenince Google bazen "kota dolu"
                        # yerine ArtifactFeatureUnavailableError: "Cinematic video
                        # generation is unavailable" döner → bunu da kota say (yoksa
                        # hard-fail + re-dispatch loop). Hedefli: generic 503
                        # "service unavailable" over-match etmesin diye spesifik.
                        "featureunavailable", "generation is unavailable",
                    ))
                )
                if is_quota:
                    # Mid-flight (video_wait/video_download): gen başlamış, Google
                    # tarafında video üretiliyor. Job'u demote ETME — notebook_url'i
                    # koru ki stale-resume sweeper kurtarabilsin. Sadece profile'ı
                    # blokla (yeni iş düşmesin).
                    mid_flight = e.stage in ("video_wait", "video_download")
                    log_fp.write(
                        f"## quota_exceeded detected (stage={e.stage}, "
                        f"mid_flight={mid_flight}) → "
                        f"{'preserve generating' if mid_flight else 'requeue'} "
                        f"+ block profile today\n"
                    )
                    log_fp.flush()
                    self._apply_event(job_id, {
                        "type": "quota_exceeded",
                        "raw": str(e)[:500],
                        "mid_flight": mid_flight,
                    })
                    return
                # Auth detection — sadece e.stage="auth" değil, ortada
                # expire olan cookies de pipeline'ın herhangi bir aşamasında
                # signin redirect'ine düşebilir.
                if is_auth_failure(e.stage, str(e)):
                    log_fp.write(
                        f"## AUTH_FAIL detected (stage={e.stage}) → "
                        f"requeue job + mark profile init=False + Slack\n"
                    )
                    log_fp.flush()
                    self._handle_auth_failure(
                        profile.id, profile.name, job_id,
                        f"{e.stage}: {str(e)[:200]}"
                    )
                    return
                # Diğer hatalar — sadece job failed (atomik)
                def _m_fail(jobs_all, _e=e):
                    for j in jobs_all:
                        if j.id == job_id:
                            j.status = "failed"
                            j.error = f"{_e.stage}: {str(_e)[:280]}"
                            j.finished_at = time.time()
                            break
                mutate_jobs(_m_fail)
                return

            # 3. Başarı — Job güncelle
            log_fp.write(
                f"## PIPELINE DONE: notebook={result['notebook_id']} "
                f"task={result['task_id']} mp4={result['local_mp4']} "
                f"duration={result['duration_sec']}s\n"
            )
            log_fp.flush()

            # MP4 lokalde, video_local_path doldur + Azure upload (eğer enabled)
            local_mp4 = Path(result["local_mp4"])
            try:
                _vlp = str(local_mp4.resolve().relative_to(APP_DIR))
            except (ValueError, OSError):
                _vlp = str(local_mp4)

            def _m_done(jobs_all):
                for j in jobs_all:
                    if j.id == job_id:
                        j.notebook_url = result["notebook_url"]
                        j.video_url = ""  # notebooklm-py local download, CDN URL yok
                        j.video_local_path = _vlp
                        j.harvest_status = "downloaded"
                        j.status = "done"  # video hazır, harvest gerek yok
                        j.finished_at = time.time()
                        break
            mutate_jobs(_m_done)

            # 4. Azure upload (best-effort, fail olursa job done kalır)
            if AZURE_ENABLED and local_mp4.exists():
                # Job env'ini al — Azure prefix dev/prod ayırımı için
                _env_for_azure = "prod"
                try:
                    _job_now = next(
                        (j for j in load_jobs() if j.id == job_id), None
                    )
                    if _job_now:
                        _env_for_azure = (_job_now.environment or "prod")
                except Exception:
                    pass
                log_fp.write(
                    f"## Azure upload: {local_mp4.name} (env={_env_for_azure})\n"
                )
                log_fp.flush()
                ok, remote_url, err = upload_to_azure(
                    local_mp4, job_id, environment=_env_for_azure,
                )
                def _m_azure(jobs2):
                    for j in jobs2:
                        if j.id == job_id:
                            if ok:
                                j.video_remote_url = remote_url
                                j.harvest_status = "uploaded"
                                return {"title": j.title, "submitted_by": j.submitted_by,
                                        "batch_id": j.batch_id}
                            else:
                                j.harvest_error = f"Azure upload failed: {err}"
                            break
                    return None
                _slack_job = mutate_jobs(_m_azure)
                # Per-video "Video hazır" sadece batch'siz (tek-script) job'larda.
                # Batch job'lar için batch özeti (progress + oturum tamamlandı)
                # bildirimleri var — 40 videoluk batch'te 40 ayrı mesaj olmasın.
                if _slack_job and ok and not (_slack_job.get("batch_id") or "").strip():
                    _sb = _slack_job.get("submitted_by") or ""
                    _submitter = f" · gönderen: {_sb}" if _sb else ""
                    send_slack_message(
                        f"✅ *Video hazır:* <{remote_url}|{(_slack_job.get('title') or '')[:120]}>"
                        f"{_submitter}"
                    )
                launcher_log(
                    f"Azure upload for {job_id}: "
                    f"{'ok' if ok else 'failed'} {err}"
                )
                log_fp.write(f"## Azure done: ok={ok}\n")
            log_fp.flush()

        except Exception as e:
            log_fp.write(f"## Unexpected error: {type(e).__name__}: {e}\n")
            log_fp.write(_tb.format_exc() + "\n")
            log_fp.flush()
            jobs_all = load_jobs()
            for j in jobs_all:
                if j.id == job_id and j.status not in ("queued",):
                    j.status = "failed"
                    if not j.error:
                        j.error = f"{type(e).__name__}: {str(e)[:240]}"
                    j.finished_at = time.time()
                    # Kullanıcı tercihi (2026-06-10): per-job hata Slack'i YOK —
                    # hata detayı panelde; Slack sadece login/logout uyarısı.
                    break
            save_jobs(jobs_all)
        finally:
            try:
                log_fp.close()
            except Exception:
                pass

    def _run_job_via_nlm(self, job_id: str, profile: "Profile",
                          script_path: Optional[Path],
                          image_paths: list[Path],
                          custom_prompt: str,
                          log_fp) -> None:
        """tmc/nlm CLI üzerinden notebook create + sources upload + create-video.

        Bu fonksiyon Worker thread'inde çalışır. nlm subprocess çağrıları senkron
        (her biri saniyeler). Job state'i nlm event'leri yerine direkt
        _apply_event() ile güncellenir (compat path).
        """
        import traceback as _tb
        try:
            # Auth setup
            auth_json = PROFILES_DIR / profile.id / "auth.json"
            if not auth_json.exists():
                raise NlmError(
                    f"auth.json yok: {auth_json}. Profile init et."
                )
            cookies = extract_nlm_cookies(auth_json)
            log_fp.write(f"## auth: cookies extracted ({len(cookies)} chars)\n")
            log_fp.flush()
            try:
                auth_token = fetch_nlm_auth_token(cookies, authuser=profile.authuser)
                log_fp.write(f"## auth: token fetched ({auth_token[:6]}…{auth_token[-4:]})\n")
            except NlmError as e:
                # Token expired ya da cookie geçersiz — auth fail.
                # Tek bir handler: job requeue + profile deinit + Slack.
                log_fp.write(f"## auth FAIL: {e}\n")
                log_fp.flush()
                self._handle_auth_failure(
                    profile.id, profile.name, job_id,
                    f"NLM auth: {str(e)[:200]}"
                )
                return
            log_fp.flush()

            # 1) Notebook create
            title = (load_jobs() and next(
                (j.title for j in load_jobs() if j.id == job_id), "Untitled"
            )) or "Untitled"
            try:
                nb_id = nlm_create_notebook(
                    title, cookies, auth_token=auth_token,
                    authuser=profile.authuser,
                )
            except NlmError as e:
                # Kota dolması bu yolla gelebilir
                msg = str(e).lower()
                if "kota" in msg or "limit" in msg or "quota" in msg:
                    log_fp.write(f"## NLM quota: {e}\n")
                    self._apply_event(job_id, {"type": "quota_exceeded", "raw": str(e)})
                    log_fp.flush()
                    return
                raise
            notebook_url = notebook_web_url(nb_id, authuser=profile.authuser)
            log_fp.write(f"## NLM notebook created: {nb_id}\n")
            log_fp.write(f"## URL: {notebook_url}\n")
            log_fp.flush()
            self._apply_event(job_id, {
                "type": "notebook_created", "notebook_url": notebook_url,
            })

            # 2) Sources upload (script + images, sırasıyla)
            sources_to_upload: list[Path] = []
            if script_path and script_path.exists():
                sources_to_upload.append(script_path)
            for p in (image_paths or []):
                if isinstance(p, Path) and p.exists():
                    sources_to_upload.append(p)

            n_ok, n_fail = 0, 0
            for i, src in enumerate(sources_to_upload):
                log_fp.write(
                    f"## Uploading source [{i+1}/{len(sources_to_upload)}]: "
                    f"{src.name} ({src.stat().st_size // 1024} KB)\n"
                )
                log_fp.flush()
                try:
                    src_id = nlm_source_add(
                        nb_id, src, cookies, auth_token=auth_token,
                        authuser=profile.authuser, timeout=300,
                    )
                    log_fp.write(f"##   → source_id={src_id}\n")
                    n_ok += 1
                except NlmError as e:
                    log_fp.write(f"##   → FAILED: {str(e)[:200]}\n")
                    n_fail += 1
                    # Devam et — kısmi sources daha iyi sonuçtan vazgeçmek
                log_fp.flush()
            log_fp.write(f"## Sources summary: {n_ok} OK, {n_fail} failed\n")

            if n_ok == 0:
                # Hiç source yüklenememişse — fail
                raise NlmError(
                    f"Tüm source upload'ları başarısız ({len(sources_to_upload)} dosya). "
                    f"Bu notebook boş kalır, generation anlamsız."
                )

            # 3) create-video
            log_fp.write(f"## Creating Cinematic Video Overview...\n")
            log_fp.flush()
            try:
                out = nlm_create_video(
                    nb_id, custom_prompt, cookies, auth_token=auth_token,
                    authuser=profile.authuser, timeout=60,
                )
                log_fp.write(f"## create-video output: {out[:400]}\n")
            except NlmError as e:
                log_fp.write(f"## create-video FAILED: {e}\n")
                # Notebook oluşturuldu, sources yüklendi, ama generate fail —
                # kullanıcı manuel olarak NotebookLM'den Cinematic'i tetikleyebilir
                raise
            log_fp.flush()

            # 4) Başarı — automation_complete event'i (status=generating)
            self._apply_event(job_id, {
                "type": "automation_complete",
                "exit_code": 0,
                "notebook_url": notebook_url,
            })
            log_fp.write(f"## ALL DONE — job status=generating, harvest will pick up.\n")
            log_fp.flush()

        except NlmError as e:
            log_fp.write(f"## NLM ERROR: {e}\n")
            if e.stderr:
                log_fp.write(f"##   stderr: {e.stderr[:400]}\n")
            log_fp.flush()
            self._apply_event(job_id, {
                "type": "automation_error", "error": str(e)[:300],
            })
            jobs_all = load_jobs()
            for j in jobs_all:
                if j.id == job_id and j.status not in ("queued",):
                    j.status = "failed"
                    if not j.error:
                        j.error = f"NLM: {str(e)[:200]}"
                    j.finished_at = time.time()
                    break
            save_jobs(jobs_all)
        except Exception as e:
            log_fp.write(
                f"## Unexpected error: {type(e).__name__}: {e}\n"
            )
            log_fp.write(_tb.format_exc() + "\n")
            log_fp.flush()
            jobs_all = load_jobs()
            for j in jobs_all:
                if j.id == job_id and j.status not in ("queued",):
                    j.status = "failed"
                    if not j.error:
                        j.error = f"{type(e).__name__}: {str(e)[:200]}"
                    j.finished_at = time.time()
                    break
            save_jobs(jobs_all)
        finally:
            try:
                log_fp.close()
            except Exception:
                pass

    def _stdout_reader(self, job_id: str, proc: subprocess.Popen, log_fp) -> None:
        """Subprocess stdout'unu satır satır oku, ##JSON## event'lerini parse et."""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    log_fp.write(line)
                    log_fp.flush()
                except Exception:
                    pass
                line = line.rstrip()
                if line.startswith("##JSON## "):
                    try:
                        evt = json.loads(line[len("##JSON## "):])
                    except json.JSONDecodeError:
                        continue
                    self._apply_event(job_id, evt)
        except Exception as e:
            launcher_log(f"stdout reader for {job_id} failed: {e!r}")
        finally:
            try:
                log_fp.close()
            except Exception:
                pass

    def _apply_event(self, job_id: str, evt: dict) -> None:
        with _jobs_lock():
            etype = evt.get("type", "")
            jobs = load_jobs()
            target: Optional[Job] = None
            for j in jobs:
                if j.id == job_id:
                    target = j
                    break
            if target is None:
                return
            if etype == "notebook_created":
                url = evt.get("notebook_url", "")
                if url:
                    target.notebook_url = url
            elif etype == "quota_exceeded":
                # Bu profilin kotası dolmuş — bugün için bir failed kayıt bırak ki
                # _quota_blocked_today() bu profili pas geçsin.
                #
                # mid_flight=True (video_wait/video_download stage'i): gen başlamış,
                # Google'da üretim devam ediyor. notebook_url'i koru, status="generating"
                # bırak → stale-resume sweeper kurtarır.
                # mid_flight=False (video_gen stage'i): gen başlamadı → job'u queued'a
                # döndür, başka profile dispatch edilsin.
                mid_flight = bool(evt.get("mid_flight", False))
                quota_marker = Job(
                    id=uuid.uuid4().hex[:12],
                    title=f"[KOTA] {target.title[:50]}",
                    text="(quota detection marker)",
                    profile_id=target.profile_id,
                    profile_name=target.profile_name,
                    status="failed",
                    error="NotebookLM günlük Cinematic kotası dolmuş — yarın resetlenir.",
                    started_at=time.time(),
                    finished_at=time.time(),
                    submitted_by="system",
                )
                jobs.append(quota_marker)
                # Slack: per-event spam yerine batch-level "kota duvarı" mesajı
                # (_batch_monitor_round → _check_quota_wall) — tüm profiller
                # bugün için bloklanınca 1× toplu bildirim. Burada sadece launcher.log.
                if mid_flight:
                    # Gen başlamış, Google üretiyor — job'a dokunma, sadece profile blok.
                    launcher_log(
                        f"Job {target.id} mid-flight quota ({target.profile_name}): "
                        f"notebook_url korundu, stale-resume sweeper indirecek."
                    )
                else:
                    launcher_log(
                        f"Job {target.id} quota ({target.profile_name}): "
                        f"queued'a alındı, başka profile dispatch edilecek."
                    )
                    # Asıl job'u queued'a geri al — Worker başka profile dene
                    target.status = "queued"
                    target.profile_id = ""
                    target.profile_name = ""
                    target.started_at = 0.0
                    target.finished_at = 0.0
                    target.notebook_url = ""  # eski profilin oluşturduğu notebook'u unut
                    target.pid = 0
            elif etype in ("login_required_headless", "login_timeout"):
                # Auth fail: merkez handler — requeue + init=False + Slack
                # (idempotent transition: ilk düşüşte Slack, sonraki çağrılarda no-op).
                # Handler kendi load_jobs/save_jobs yapar — burada erken return ile
                # alttaki save_jobs(jobs)'un handler'ın yazdığını üzerine yazmasını
                # engelliyoruz.
                self._handle_auth_failure(
                    target.profile_id, target.profile_name, target.id,
                    f"login event: {etype}",
                )
                return
            elif etype == "automation_complete":
                # Eğer quota_exceeded başka event tarafından zaten queued'a alındıysa
                # (kota auto-retry), bu event'i skip et — yoksa failed'e override eder.
                if target.status == "queued":
                    pass  # already requeued, no-op
                else:
                    url = evt.get("notebook_url", "") or target.notebook_url
                    target.notebook_url = url
                    if int(evt.get("exit_code", 1)) == 0:
                        # Automator işini bitirdi (Generate tıklandı), ama NotebookLM
                        # Cinematic videoyu 30-60dk'da üretiyor. status="generating"
                        # olarak işaretle — harvest cycle bunu pick'leyip video URL'i
                        # geldiğinde "done"a çevirecek.
                        if url:
                            target.status = "generating"
                        else:
                            target.status = "submitted"
                    else:
                        target.status = "failed"
                        if not target.error:
                            target.error = "automator exit_code != 0"
                    target.finished_at = time.time()
            elif etype == "automation_error":
                target.error = str(evt.get("error", ""))[:500]
            save_jobs(jobs)

    def _reap_finished(self) -> None:
        """Subprocess'leri reap, kayıt güncellemesi gerek yoksa skip."""
        with self._proc_lock:
            done_ids = []
            for jid, proc in self._procs.items():
                if proc.poll() is not None:
                    done_ids.append((jid, proc.returncode))
            for jid, rc in done_ids:
                self._procs.pop(jid, None)
                # Eğer event ile status güncellenmediyse şimdi doldur
                jobs = load_jobs()
                changed = False
                for j in jobs:
                    if j.id == jid and j.status == "running":
                        j.finished_at = time.time()
                        if rc == 0:
                            j.status = "submitted" if not j.notebook_url else "done"
                        else:
                            j.status = "failed"
                            if not j.error:
                                j.error = f"process exited rc={rc}"
                        changed = True
                        break
                if changed:
                    save_jobs(jobs)

    # ----- HARVEST -----

    def _harvest_round(self) -> None:
        """Done job'lar için video harvest cycle. Worker thread'ten dakikada bir çağrılır."""
        jobs = load_jobs()
        now = time.time()
        candidates = []
        for j in jobs:
            # Harvest pickup: 'generating' (yeni — automator success) veya
            # 'done'/'submitted' (geriye dönük uyum).
            if j.status not in ("generating", "done", "submitted"):
                continue
            if not j.notebook_url:
                continue
            # "checking" da skip — şu an harvest çalışıyor, paralel tetikleme.
            if j.harvest_status in ("ready", "downloaded", "uploaded", "expired", "skip", "checking"):
                continue
            # İlk deneme için: finished_at + HARVEST_FIRST_DELAY_SEC bekleyelim
            if j.harvest_attempts == 0:
                if (j.finished_at or j.created_at) + HARVEST_FIRST_DELAY_SEC > now:
                    continue
            else:
                if j.next_harvest_at > now:
                    continue
            candidates.append(j)

        if not candidates:
            return

        # Aynı anda çok harvest açma — round başına 1 tane (yavaş ama güvenli)
        target = candidates[0]
        self._launch_harvest(target)

    def _launch_harvest(self, job: Job) -> None:
        """Harvest subprocess'ini ayağa kaldır, blocking değil."""
        # Profile bul (auth.json kullanmak için)
        profiles = load_profiles()
        profile = next((p for p in profiles if p.id == job.profile_id), None)
        if profile is None:
            self._mark_harvest_expired(job.id, "profile not found")
            return

        # Status güncelle: checking, attempt+1
        jobs = load_jobs()
        for j in jobs:
            if j.id == job.id:
                j.harvest_status = "checking"
                j.harvest_attempts += 1
                break
        save_jobs(jobs)

        cmd = [
            PYTHON_BIN,
            str(APP_DIR / "notebooklm_automator.py"),
            "--profile-dir", str(PROFILES_DIR / profile.id),
            "--authuser", str(profile.authuser),
            "--harvest", job.notebook_url,
            "--job-id", job.id,
            "--json-events",
            "--headless",
            "--download-dir", str(DOWNLOADS_DIR),
            "--screenshots-dir", str(SCREENSHOTS_DIR),
        ]
        log_path = harvest_log_path(job.id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_fp = log_path.open("a", encoding="utf-8", buffering=1)
            log_fp.write(f"\n# Harvest attempt #{job.harvest_attempts} at {datetime.now().isoformat(timespec='seconds')}\n")
            log_fp.write(f"# Cmd: {' '.join(cmd)}\n")
            log_fp.flush()
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(APP_DIR),
                text=True,
                bufsize=1,
            )
            t = threading.Thread(
                target=self._harvest_stdout_reader,
                args=(job.id, proc, log_fp),
                name=f"harvest-{job.id}",
                daemon=True,
            )
            t.start()
            launcher_log(f"Harvest #{job.harvest_attempts} launched for job {job.id} (pid={proc.pid})")
        except Exception as e:
            self._mark_harvest_expired(job.id, f"launch failed: {e!r}")

    def _harvest_stdout_reader(self, job_id: str, proc: subprocess.Popen, log_fp) -> None:
        video_url = ""
        local_path = ""
        not_ready = False
        login_required = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    log_fp.write(line); log_fp.flush()
                except Exception:
                    pass
                line = line.rstrip()
                if line.startswith("##JSON## "):
                    try:
                        evt = json.loads(line[len("##JSON## "):])
                    except json.JSONDecodeError:
                        continue
                    et = evt.get("type", "")
                    if et == "harvest_video_url_found":
                        video_url = evt.get("video_url", "")
                    elif et == "harvest_downloaded":
                        local_path = evt.get("path", "")
                    elif et == "harvest_not_ready":
                        not_ready = True
                    elif et == "harvest_login_required":
                        login_required = True
        except Exception as e:
            launcher_log(f"harvest stdout reader for {job_id} failed: {e!r}")
        finally:
            try:
                log_fp.close()
            except Exception:
                pass

        rc = proc.wait()
        self._apply_harvest_result(job_id, rc, video_url, local_path, not_ready, login_required)

    def _apply_harvest_result(
        self,
        job_id: str,
        rc: int,
        video_url: str,
        local_path: str,
        not_ready: bool,
        login_required: bool,
    ) -> None:
        jobs = load_jobs()
        target: Optional[Job] = next((j for j in jobs if j.id == job_id), None)
        if target is None:
            return

        if login_required:
            target.harvest_status = "expired"
            target.harvest_error = "Hesap login süresi geçmiş — admin re-login yapsın"
            save_jobs(jobs)
            return

        if video_url:
            target.video_url = video_url
            target.harvest_status = "ready"
            if local_path:
                # Relative path olarak sakla (data/downloads altında)
                try:
                    rel = Path(local_path).resolve().relative_to(APP_DIR)
                    target.video_local_path = str(rel)
                except (ValueError, OSError):
                    target.video_local_path = local_path
                target.harvest_status = "downloaded"
                # Phase E.4: video harvest edildi → job status'u 'generating'den
                # 'done'a yükselt (gerçekten tamamlandı).
                if target.status in ("generating", "submitted"):
                    target.status = "done"
            save_jobs(jobs)

            # Phase 3: Azure upload (eğer enabled ise ve dosya varsa)
            if AZURE_ENABLED and target.video_local_path:
                full_path = APP_DIR / target.video_local_path
                if full_path.exists():
                    ok, remote_url, err = upload_to_azure(
                        full_path, job_id,
                        environment=(target.environment or "prod"),
                    )
                    jobs2 = load_jobs()
                    for j in jobs2:
                        if j.id == job_id:
                            if ok:
                                j.video_remote_url = remote_url
                                j.harvest_status = "uploaded"
                                # Eğer hâlâ generating ise (downloaded path'inde
                                # set edilmemişse), şimdi done yap.
                                if j.status in ("generating", "submitted"):
                                    j.status = "done"
                            else:
                                j.harvest_error = f"Azure upload failed: {err}"
                            break
                    save_jobs(jobs2)
                    launcher_log(
                        f"Azure upload for {job_id}: {'ok' if ok else 'failed'} {err}"
                    )
            return

        # Video yok henüz — retry veya expire
        if not_ready or rc == 2:
            if target.harvest_attempts >= HARVEST_MAX_ATTEMPTS:
                target.harvest_status = "expired"
                target.harvest_error = (
                    f"{HARVEST_MAX_ATTEMPTS} denemeden sonra video hazır değildi"
                )
            else:
                target.harvest_status = "pending"
                target.next_harvest_at = time.time() + HARVEST_RETRY_INTERVAL_SEC
            save_jobs(jobs)
            return

        # Beklenmedik failure
        if target.harvest_attempts >= HARVEST_MAX_ATTEMPTS:
            target.harvest_status = "expired"
            target.harvest_error = f"Harvest hatası (rc={rc})"
        else:
            target.harvest_status = "pending"
            target.next_harvest_at = time.time() + HARVEST_RETRY_INTERVAL_SEC
            target.harvest_error = f"rc={rc}, retry"
        save_jobs(jobs)

    def _mark_harvest_expired(self, job_id: str, reason: str) -> None:
        jobs = load_jobs()
        for j in jobs:
            if j.id == job_id:
                j.harvest_status = "expired"
                j.harvest_error = reason
                break
        save_jobs(jobs)

    def trigger_harvest_now(self, job_id: str) -> bool:
        """Admin'in 'şimdi harvest et' butonu için. Job'u next_harvest_at=now yapar."""
        jobs = load_jobs()
        for j in jobs:
            if j.id == job_id:
                if j.harvest_status not in ("ready", "downloaded", "uploaded"):
                    j.harvest_status = "pending"
                    j.next_harvest_at = 0
                    j.harvest_attempts = 0
                    save_jobs(jobs)
                    return True
        return False

    def resume_via_notebooklm(self, job_id: str) -> tuple[bool, str]:
        """notebooklm-py path'inde stuck kalan 'generating' job için manuel kurtarma.

        Senaryo: server restart sırasında pipeline thread öldü, video gen
        NotebookLM tarafında devam etti veya bitti, ama MP4 indirilemedi.
        Bu method var olan notebook'a yeniden bağlanır, video artifact'ı bulur,
        indirir + Azure'a yükler + job state günceller.

        Returns (ok, message). Streamlit thread'inde blocking — bekleme süresi
        Cinematic gen tamamlanmadıysa 30+ dakika olabilir.
        """
        try:
            from notebooklm_client import (
                resume_download, notebook_id_from_url, NotebookLMClientError,
            )
        except ImportError as e:
            return False, f"notebooklm-py import: {e}"

        jobs = load_jobs()
        target = next((j for j in jobs if j.id == job_id), None)
        if target is None:
            return False, "Job bulunamadı."
        if not target.notebook_url:
            return False, "notebook_url yok — gen henüz başlamamış. Önce dispatcher'ı bekle."
        if not target.profile_id:
            return False, "profile_id yok."
        nb_id = notebook_id_from_url(target.notebook_url)
        if not nb_id:
            return False, f"notebook_url parse edilemedi: {target.notebook_url[:60]}"

        out_path = Path(DOWNLOADS_DIR) / f"{nb_id}.mp4"
        try:
            result = resume_download(
                profile_id=target.profile_id,
                notebook_id=nb_id,
                out_path=out_path,
                wait_if_processing=True,
                wait_timeout_sec=1800.0,  # 30dk — gen devam ediyor olabilir
            )
        except NotebookLMClientError as e:
            return False, f"resume FAIL: {e}"
        except Exception as e:
            return False, f"unexpected: {type(e).__name__}: {e}"

        # Job state güncelle
        jobs = load_jobs()
        for j in jobs:
            if j.id == job_id:
                j.status = "done"
                j.harvest_status = "downloaded"
                try:
                    j.video_local_path = str(out_path.resolve().relative_to(APP_DIR))
                except (ValueError, OSError):
                    j.video_local_path = str(out_path)
                j.finished_at = time.time()
                break
        save_jobs(jobs)

        # Azure upload (best-effort) — job env'iyle prefix doğru gelir
        if AZURE_ENABLED and out_path.exists():
            try:
                _resume_env = "prod"
                _job_now = next((j for j in jobs if j.id == job_id), None)
                if _job_now:
                    _resume_env = (_job_now.environment or "prod")
                ok, url, err = upload_to_azure(
                    out_path, job_id, environment=_resume_env,
                )
                if ok:
                    jobs = load_jobs()
                    _slack_job = None
                    for j in jobs:
                        if j.id == job_id:
                            j.video_remote_url = url
                            j.harvest_status = "uploaded"
                            _slack_job = j
                            break
                    save_jobs(jobs)
                    # Per-video bildirim sadece batch'siz job'larda (bkz.
                    # _run_job_via_notebooklm'deki aynı kural).
                    if _slack_job and not (_slack_job.batch_id or "").strip():
                        _submitter = (
                            f" · gönderen: {_slack_job.submitted_by}"
                            if _slack_job.submitted_by else ""
                        )
                        send_slack_message(
                            f"✅ *Video hazır:* <{url}|{_slack_job.title[:120]}>"
                            f"{_submitter}"
                        )
            except Exception:
                pass

        return True, f"MP4 ({result.get('size_mb', '?')}MB) indi + state=done."


def load_profiles_with_updates(updated: list[Profile]) -> list[Profile]:
    """Diskteki profil listesini güncellenmiş profilelarla merge et — last_used."""
    on_disk = load_profiles()
    by_id = {p.id: p for p in on_disk}
    for u in updated:
        if u.id in by_id:
            by_id[u.id].last_used = u.last_used
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Worker singleton
# ---------------------------------------------------------------------------
@st.cache_resource
def get_worker() -> Worker:
    cleanup_stale_jobs()
    w = Worker()
    w.start()
    return w


# ---------------------------------------------------------------------------
# Init script: profil için Chromium aç (login için), kullanıcı kapatınca çık
# ---------------------------------------------------------------------------
def launch_profile_init(profile: Profile) -> int:
    profile_dir = PROFILES_DIR / profile.id
    profile_dir.mkdir(parents=True, exist_ok=True)
    log_path = init_log_path(profile.id)
    cmd = [
        PYTHON_BIN,
        str(APP_DIR / "notebooklm_automator.py"),
        "--init",
        "--profile-dir", str(profile_dir),
        "--authuser", str(profile.authuser),
        "--no-headless",
        "--json-events",
    ]
    log_fp = log_path.open("w", encoding="utf-8", buffering=1)
    log_fp.write(f"# Init for profile {profile.name} ({profile.id})\n")
    log_fp.write(f"# Cmd: {' '.join(cmd)}\n\n")
    log_fp.flush()

    # Sunucuda xvfb + noVNC kuruluysa Chromium virtual display'de açılır.
    # Lokal'de native pencerede açılır.
    env = os.environ.copy()
    if HEADLESS_INIT_DISPLAY:
        env["DISPLAY"] = HEADLESS_INIT_DISPLAY
        log_fp.write(f"# DISPLAY={HEADLESS_INIT_DISPLAY} (xvfb)\n\n")
        log_fp.flush()

    proc = subprocess.Popen(
        cmd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        cwd=str(APP_DIR),
        env=env,
    )
    launcher_log(
        f"Init launched for profile {profile.name} (pid={proc.pid}) "
        f"display={HEADLESS_INIT_DISPLAY or 'native'}"
    )
    return proc.pid


# ---------------------------------------------------------------------------
# Util: format human-readable duration
# ---------------------------------------------------------------------------
def fmt_duration(start: float, end: float) -> str:
    if not start:
        return "—"
    end = end or time.time()
    sec = max(0, int(end - start))
    if sec < 60:
        return f"{sec}sn"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}dk {s}sn"
    h, m = divmod(m, 60)
    return f"{h}sa {m}dk"


def fmt_time(ts: float) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


def fmt_datetime(ts: float) -> str:
    """Tarih + saat: '22.05 14:37' — job tablosu için."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


def derive_title(text: str, limit: int = 64) -> str:
    text = (text or "").strip()
    if not text:
        return "(boş)"
    first_line = text.splitlines()[0].strip()
    if len(first_line) > limit:
        return first_line[: limit - 1] + "…"
    return first_line


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="NotebookLM Cinematic Studio",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Custom CSS: pill, kartlar, hero, mobil media query, daha temiz tipografi
# ---------------------------------------------------------------------------
_CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');

:root {
  /* ===== Twin Design System tokens (app.twinscience.com) ===== */
  --twin-primary-50:#F5EEFE; --twin-primary-100:#EADCFD; --twin-primary-200:#D4B8FB;
  --twin-primary-400:#A463F0; --twin-primary-500:#8B30E8; --twin-primary-600:#7A1FD4;
  --twin-primary-700:#6418B0; --twin-primary-900:#3A1366;
  --twin-grape-500:#7A2B96; --twin-grape-tint:#F3E4F8;
  --twin-blue-500:#2563EB; --twin-blue-600:#1D4FD0; --twin-blue-tint:#E0EAFE;
  --twin-green-500:#16893E; --twin-green-600:#11722F; --twin-green-tint:#DCFAE6;
  --twin-red-500:#EF4444; --twin-red-600:#DC2626; --twin-red-tint:#FEE4E2;
  --twin-orange-500:#F97316; --twin-orange-tint:#FFEAD5;
  --twin-amber-400:#FBBF24; --twin-amber-tint:#FEF3C7;
  --twin-pink-500:#EC4899; --twin-pink-tint:#FCE7F3;
  --twin-ink:#1E1B2E; --twin-text:#423E54; --twin-text-muted:#6B7280; --twin-text-faint:#9CA3AF;
  --twin-border:#ECEBF0; --twin-surface:#FFFFFF; --twin-bg:#FAF9FE; --twin-bg-alt:#F4F2FB;
  --twin-gradient-hero:linear-gradient(120deg,#EDE4FB 0%,#F6E9F9 60%,#F3E8FB 100%);
  --twin-gradient-brand:linear-gradient(135deg,#8B30E8 0%,#A463F0 100%);
  --twin-shadow-xs:0 1px 2px rgba(30,27,46,.06); --twin-shadow-sm:0 2px 6px rgba(30,27,46,.06);
  --twin-shadow-md:0 4px 14px rgba(30,27,46,.08); --twin-shadow-focus:0 0 0 3px rgba(139,48,232,.30);
  --twin-ease:cubic-bezier(.4,0,.2,1);
  /* legacy alias'lar → Twin (eski var(--nlm-*) referansları otomatik Twin olsun) */
  --nlm-primary:#8B30E8; --nlm-primary-dark:#7A1FD4;
  --nlm-bg-elev:#F4F2FB; --nlm-border:#ECEBF0; --nlm-radius:12px;
}

/* Poppins — geometrik yuvarlak, Twin'in dili */
html, body, .stApp, [class*="css"], button, input, textarea, select,
[data-testid="stMarkdownContainer"], [data-baseweb] {
  font-family:'Poppins','Inter','Segoe UI',system-ui,-apple-system,sans-serif !important;
}
.stApp { background: var(--twin-bg); }
h1,h2,h3,h4 { color: var(--twin-ink) !important; letter-spacing:-.01em; }

[data-testid="stHeader"] { background: transparent !important; height: 2.2rem !important; }
[data-testid="stToolbar"] { right: 0.5rem; }
.block-container {
  padding-top: 0.6rem !important; padding-bottom: 4rem !important; max-width: 1400px !important;
}

/* Hero — Twin soft lavanta banner */
.app-hero {
  padding: 1.3rem 1.5rem; border-radius: 16px;
  background: var(--twin-gradient-hero);
  border: 1px solid var(--twin-border);
  margin: 0 0 1.2rem 0; box-shadow: var(--twin-shadow-sm);
}
.app-hero h1 {
  margin: 0; line-height: 1.2; font-size: 1.6rem; font-weight: 700;
  color: var(--twin-primary-700) !important; letter-spacing: -0.01em;
}
.app-hero p { margin: 0.4rem 0 0 0; color: var(--twin-text-muted); font-size: 0.92rem; font-weight: 400; }

/* Section header */
.section-h {
  display: flex; align-items: center; gap: 0.55rem;
  font-size: 1.12rem; font-weight: 700; color: var(--twin-ink); margin: 0.4rem 0 0.6rem 0;
}
.section-h .section-sub { font-size: 0.82rem; font-weight: 500; color: var(--twin-text-muted); margin-left: auto; }

/* Status pill — Twin tint'leri */
.pill {
  display: inline-block; padding: 3px 11px; border-radius: 9999px;
  font-size: 0.74rem; font-weight: 600; border: 1px solid transparent;
  white-space: nowrap; vertical-align: middle;
}
.pill-queued     { background: var(--twin-amber-tint);   color: #9A6B00; }
.pill-running    { background: var(--twin-blue-tint);    color: var(--twin-blue-600); }
.pill-generating { background: var(--twin-primary-50);   color: var(--twin-primary-700); }
.pill-done       { background: var(--twin-green-tint);   color: var(--twin-green-600); }
.pill-submitted  { background: var(--twin-primary-100);  color: var(--twin-primary-700); }
.pill-failed     { background: var(--twin-red-tint);     color: var(--twin-red-600); }
.pill-stopped    { background: var(--twin-bg-alt);       color: var(--twin-text-muted); }

/* Buttons — Twin (primary mor, secondary beyaz, radius 12) */
.stButton button, .stDownloadButton button, .stFormSubmitButton button {
  border-radius: 12px !important; font-weight: 600 !important;
  font-family:'Poppins',sans-serif !important; transition: all 0.14s var(--twin-ease) !important;
}
.stButton button:active { transform: translateY(1px); }
.stButton button[kind="primary"], button[data-testid="baseButton-primary"],
.stFormSubmitButton button[kind="primary"] {
  background: var(--twin-primary-500) !important; border: 0 !important; color: #fff !important;
}
.stButton button[kind="primary"]:hover, button[data-testid="baseButton-primary"]:hover {
  background: var(--twin-primary-600) !important; box-shadow: var(--twin-shadow-md) !important;
}
.stButton button[kind="secondary"], button[data-testid="baseButton-secondary"] {
  background: #fff !important; border: 1px solid var(--twin-border) !important; color: var(--twin-ink) !important;
}
.stButton button[kind="secondary"]:hover { background: var(--twin-bg-alt) !important; border-color: var(--twin-primary-200) !important; }
.stButton button:focus-visible { box-shadow: var(--twin-shadow-focus) !important; }

/* Inputs — Twin */
.stTextInput input, .stTextArea textarea, .stNumberInput input,
[data-baseweb="input"] input, [data-baseweb="textarea"] textarea, [data-baseweb="select"] > div {
  border-radius: 8px !important; border: 1px solid var(--twin-border) !important;
}
.stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus {
  border-color: var(--twin-primary-500) !important; box-shadow: var(--twin-shadow-focus) !important;
}

/* Sidebar — beyaz panel */
section[data-testid="stSidebar"] { background: var(--twin-surface) !important; border-right: 1px solid var(--twin-border); }
section[data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 12px !important; }

/* Expander — Twin kart */
[data-testid="stExpander"] {
  border: 1px solid var(--twin-border) !important; border-radius: 12px !important;
  background: var(--twin-surface) !important; box-shadow: var(--twin-shadow-xs);
}
[data-testid="stExpander"] summary { font-weight: 600; color: var(--twin-ink); }
[data-testid="stExpander"] summary:hover { color: var(--twin-primary-600); }

/* Progress bar → mor */
.stProgress > div > div > div > div { background: var(--twin-primary-500) !important; }
[data-testid="stProgress"] [role="progressbar"] > div { background: var(--twin-primary-500) !important; }

/* Metric'ler */
[data-testid="stMetricValue"] { font-size: 1.7rem !important; font-weight: 700 !important; color: var(--twin-ink) !important; }
[data-testid="stMetricLabel"] { font-weight: 500 !important; color: var(--twin-text-muted) !important; }

/* Tab başlıkları */
button[data-baseweb="tab"] { font-weight: 600 !important; padding: 0.6rem 1.1rem !important; }
button[data-baseweb="tab"][aria-selected="true"] { color: var(--twin-primary-600) !important; }
[data-baseweb="tab-highlight"] { background: var(--twin-primary-500) !important; }

/* Linkler → mor */
a, a:visited { color: var(--twin-primary-600); }
a:hover { color: var(--twin-primary-700); }

/* Job satırı */
.job-row-wrap [data-testid="stHorizontalBlock"] {
  padding: 0.55rem 0.4rem; border-bottom: 1px solid var(--twin-border);
  border-radius: 8px; transition: background 0.12s ease;
}
.job-row-wrap [data-testid="stHorizontalBlock"]:hover { background: var(--twin-bg-alt); }

/* Empty state */
.empty-state {
  text-align: center; padding: 2.5rem 1rem; border: 2px dashed var(--twin-border);
  border-radius: 16px; color: var(--twin-text-muted);
}
.empty-state .es-icon { font-size: 2.2rem; margin-bottom: 0.4rem; }
.empty-state .es-title { font-weight: 600; color: var(--twin-ink); margin-bottom: 0.25rem; }
.empty-state .es-sub { font-size: 0.85rem; color: var(--twin-text-muted); }

.url-truncate {
  display: inline-block; max-width: 100%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
}
.sidebar-section {
  font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em;
  font-weight: 600; color: var(--twin-text-faint); margin: 0.7rem 0 0.4rem 0;
}

/* RESPONSIVE */
@media (max-width: 900px) {
  .app-hero h1 { font-size: 1.3rem; }
  .app-hero p  { font-size: 0.82rem; }
  .job-header { display: none !important; }
  .block-container { padding-left: 0.8rem !important; padding-right: 0.8rem !important; }
}
@media (max-width: 720px) {
  div[data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 0.4rem !important; }
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
    flex: 1 1 100% !important; width: 100% !important; min-width: unset !important;
  }
  [data-testid="stMetric"] { padding: 0.4rem 0.6rem; }
}
</style>
"""
st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)

# Worker'ı modül yüklemesinde başlat
worker = get_worker()
worker.ensure_alive()  # 2D: her render'da thread canlı mı kontrol et, ölmüşse restart+alarm


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
_STATUS_LABELS = {
    "queued": "KUYRUKTA",
    "running": "ÇALIŞIYOR",
    "generating": "VİDEO ÜRETİLİYOR",  # NotebookLM Cinematic'te 30-60dk
    "done": "TAMAMLANDI",
    "submitted": "GÖNDERİLDİ",
    "failed": "HATA",
    "stopped": "DURDURULDU",
}


def status_pill(status: str) -> str:
    label = _STATUS_LABELS.get(status, status.upper())
    return f'<span class="pill pill-{status}">{label}</span>'


def hero(title: str, subtitle: str = "") -> None:
    sub_html = f"<p>{subtitle}</p>" if subtitle else ""
    st.markdown(
        f'<div class="app-hero"><h1>🎬 {title}</h1>{sub_html}</div>',
        unsafe_allow_html=True,
    )


def section_header(title: str, sub: str = "") -> None:
    sub_html = f'<span class="section-sub">{sub}</span>' if sub else ""
    st.markdown(
        f'<div class="section-h">{title}{sub_html}</div>',
        unsafe_allow_html=True,
    )


def empty_state(icon: str, title: str, sub: str = "") -> None:
    sub_html = f'<div class="es-sub">{sub}</div>' if sub else ""
    st.markdown(
        f'<div class="empty-state"><div class="es-icon">{icon}</div>'
        f'<div class="es-title">{title}</div>{sub_html}</div>',
        unsafe_allow_html=True,
    )


def open_in_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as e:
        st.toast(f"Tarayıcı açılamadı: {e}", icon="⚠️")


def send_slack_message(text: str, blocks: list | None = None) -> bool:
    """Slack incoming webhook ile mesaj gönder. Başarılıysa True döner.

    Bağımlılık gerektirmez — stdlib urllib.request kullanır.
    SLACK_ENABLED False ise sessizce atlar.
    """
    if not SLACK_ENABLED:
        return False
    import json as _json
    import urllib.request as _req
    import urllib.error as _uerr
    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        data = _json.dumps(payload).encode("utf-8")
        req = _req.Request(
            SLACK_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _req.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except (_uerr.URLError, OSError, Exception) as exc:
        launcher_log(f"Slack webhook hatası: {exc}")
        return False


# ---------------------------------------------------------------------------
# Auth: session_state.auth ile login/logout, rol-tabanlı routing.
# Refresh sonrası session korunsun diye URL'de short-lived token tutuyoruz;
# token → auth mapping'i process memory'sinde (servis restart'ta sıfırlanır).
# ---------------------------------------------------------------------------
import secrets  # noqa: E402


# Token store — DOSYA-BACKED (restart'a dayanıklı). Eskiden @st.cache_resource
# process memory'deydi → her servis restart'ında (deploy) tüm session'lar
# düşüyordu = "refresh'te logout". Şimdi data/.session_tokens.json'da tutulur.
# Format: {token: {"auth": {...}, "expires": epoch_ts}}. 30 gün TTL.
SESSION_TOKEN_FILE = DATA_DIR / ".session_tokens.json"
SESSION_TOKEN_TTL_SEC = 7 * 24 * 3600  # 7 gün (token URL'de ?t= ile taşınıyor →
# tarayıcı history + reverse-proxy loglarına düşer; URL paylaşmak = hesap devri.
# TTL kısaltıldı. KALICI çözüm: token'ı cookie'ye taşı (Streamlit cookie component)
# — ayrı iş. O zamana kadar TTL düşük tutulur.


@st.cache_resource
def _token_store_mem() -> dict[str, dict]:
    """Process-içi hızlı cache. Dosya ile senkron tutulur."""
    return {}


def _load_token_file() -> dict[str, dict]:
    try:
        import json as _json
        with SESSION_TOKEN_FILE.open(encoding="utf-8") as f:
            return _json.load(f)
    except (OSError, ValueError):
        return {}


def _save_token_file(store: dict[str, dict]) -> None:
    try:
        import json as _json
        tmp = SESSION_TOKEN_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            _json.dump(store, f)
        tmp.replace(SESSION_TOKEN_FILE)
    except OSError:
        pass


def _prune_expired_tokens(store: dict[str, dict]) -> dict[str, dict]:
    now = time.time()
    return {t: v for t, v in store.items()
            if isinstance(v, dict) and v.get("expires", 0) > now}


def _issue_session_token(auth: dict) -> str:
    token = secrets.token_urlsafe(24)
    entry = {"auth": auth, "expires": time.time() + SESSION_TOKEN_TTL_SEC}
    # Memory + dosya ikisine de yaz
    _token_store_mem()[token] = entry
    store = _prune_expired_tokens(_load_token_file())
    store[token] = entry
    _save_token_file(store)
    return token


def _lookup_session_token(token: str) -> Optional[dict]:
    if not token:
        return None
    now = time.time()
    # Önce memory
    entry = _token_store_mem().get(token)
    if entry is None:
        # Memory'de yok (restart sonrası) → dosyadan oku
        store = _load_token_file()
        entry = store.get(token)
        if entry is not None:
            _token_store_mem()[token] = entry  # memory'ye geri yükle
    if not isinstance(entry, dict):
        return None
    if entry.get("expires", 0) <= now:
        return None  # expired
    return entry.get("auth")


def _revoke_session_token(token: str) -> None:
    _token_store_mem().pop(token, None)
    store = _load_token_file()
    if token in store:
        store.pop(token, None)
        _save_token_file(store)


def _restore_session_from_url() -> None:
    """Page load: URL'de ?t=TOKEN varsa session_state'e auth restore et.
    session_state.auth yoksa ama token geçerliyse re-hydrate."""
    if st.session_state.get("auth"):
        return
    token = st.query_params.get("t", "")
    if not token:
        return
    auth = _lookup_session_token(token)
    if auth:
        st.session_state["auth"] = auth
        st.session_state["session_token"] = token


def is_logged_in() -> bool:
    _restore_session_from_url()
    auth = st.session_state.get("auth")
    return bool(auth and auth.get("username"))


def current_user() -> Optional[dict]:
    return st.session_state.get("auth") if is_logged_in() else None


def _is_admin() -> bool:
    auth = current_user()
    return bool(auth and auth.get("role") == "admin")


# ALLOWED_ENVS / DEFAULT_ENV: yukarıda ~line 140 civarında tanımlı (worker
# thread race condition fix). current_env() onları kullanır.
def current_env() -> str:
    """URL ?env=... param'ı oku. Sonra session_state'e cache (refresh sonrası
    da kalsın). Default DEFAULT_ENV (env-config'den).

    Prod canlıya geçince ya .env'e `DEFAULT_ENV=prod` ekle, ya da kodda
    `DEFAULT_ENV = "dev"` satırını `"prod"` yap → default URL prod'a düşer.
    """
    try:
        q = st.query_params.get("env", "")
    except Exception:
        q = ""
    if q in ALLOWED_ENVS:
        st.session_state["_env"] = q
        return q
    # Param yok → session_state'den oku, o da yoksa DEFAULT_ENV
    cached = st.session_state.get("_env", "")
    if cached in ALLOWED_ENVS:
        return cached
    return DEFAULT_ENV


def _user_name() -> str:
    """User view'ın "kim gönderdi" alanı için — display_name'den alır."""
    auth = current_user()
    return auth.get("display_name", "") if auth else ""


def do_logout() -> None:
    # Token'ı sunucu memory'sinden de iptal et
    token = st.session_state.get("session_token", "")
    if token:
        _revoke_session_token(token)
    st.session_state.pop("auth", None)
    st.session_state.pop("session_token", None)
    # URL query param'ları da temizle (eski legacy + yeni session token)
    for k in ("u", "admin", "reset_name", "t"):
        try:
            del st.query_params[k]
        except KeyError:
            pass


def render_login_view() -> None:
    """Sayfa açıldığında giriş ekranı. Login başarılı olursa rol'e göre
    admin veya user view'a yönlendirilir (rerun)."""
    hero("Cinematic Studio", "Giriş yap")

    st.markdown("&nbsp;", unsafe_allow_html=True)
    cs = st.columns([1, 2, 1])
    with cs[1]:
        with st.container(border=True):
            st.markdown(
                '<div style="font-weight:600; font-size:1.05rem; margin-bottom:0.6rem;">'
                '🔐 Hoş geldin</div>',
                unsafe_allow_html=True,
            )
            with st.form("login_form", clear_on_submit=False):
                username = st.text_input("Kullanıcı adı", placeholder="örn. mustafa")
                password = st.text_input("Şifre", type="password")
                submitted = st.form_submit_button("Giriş yap →", type="primary", use_container_width=True)
                if submitted:
                    # Login rate-limit (per-session): brute-force'u yavaşlat.
                    # 5 hatalı denemeden sonra 5 dk kilit.
                    _lock_until = st.session_state.get("_login_lock_until", 0.0)
                    _now = time.time()
                    if _lock_until > _now:
                        st.error(
                            f"Çok fazla hatalı deneme. "
                            f"{int(_lock_until - _now)} sn sonra tekrar dene.")
                        user = None
                        submitted = False
                    else:
                        user = authenticate(username, password)
                    if submitted and user is None:
                        _fails = st.session_state.get("_login_fails", 0) + 1
                        st.session_state["_login_fails"] = _fails
                        if _fails >= 5:
                            st.session_state["_login_lock_until"] = time.time() + 300
                            st.session_state["_login_fails"] = 0
                            st.error("Çok fazla hatalı deneme — 5 dk kilitlendi.")
                        else:
                            st.error(
                                f"Kullanıcı adı veya şifre hatalı. "
                                f"({_fails}/5 deneme)")
                    elif submitted and user is not None:
                        st.session_state["_login_fails"] = 0
                        st.session_state.pop("_login_lock_until", None)
                        auth = {
                            "username": user.username,
                            "role": user.role,
                            "display_name": user.display_name or user.username,
                        }
                        st.session_state["auth"] = auth
                        # Session token oluştur, URL'e ?t=... ekle ki refresh'te korunsun
                        token = _issue_session_token(auth)
                        st.session_state["session_token"] = token
                        # Eski legacy query params'ı temizle
                        for k in ("u", "admin", "reset_name"):
                            try:
                                del st.query_params[k]
                            except KeyError:
                                pass
                        st.query_params["t"] = token
                        st.rerun()

            st.caption("Hesabın yoksa yöneticiden iste.")


# ---------------------------------------------------------------------------
# Style Guides UI — admin tab + user view'da paylaşılan render fonksiyonu.
# key_prefix farklı çağrı yerleri için widget key conflict'ini önler.
# ---------------------------------------------------------------------------
def render_style_guides_ui(key_prefix: str = "sg",
                           heading: bool = True) -> None:
    if heading:
        section_header(
            "📚 Style Guides",
            "Reusable kaynaklar — her video job'unda otomatik notebook'a attach edilir"
        )
    st.markdown(
        '<div style="font-size:0.85rem; opacity:0.78; margin-bottom:0.6rem;">'
        '⚙ Buraya yüklediğin dosyalar (Identity Protocol, Fully Realistic Style, '
        'Narrative Execution Guide gibi) <b>her job\'da</b> '
        'NotebookLM\'e Add sources akışıyla yüklenir ve Custom Prompt\'tan '
        'isimleriyle referanslanır.<br>'
        'Kabul edilen tipler: PDF, TXT, MD, DOCX, image (JPG/PNG/...), audio (MP3/M4A). '
        'Maksimum dosya: 30 MB.</div>',
        unsafe_allow_html=True,
    )

    # Upload form
    with st.expander("➕ Yeni dosya yükle", expanded=False):
        up_files = st.file_uploader(
            "Dosya seç (birden fazla seçebilirsin)",
            accept_multiple_files=True,
            key=f"{key_prefix}_uploader",
            label_visibility="collapsed",
            help="Sürükle-bırak veya tıklayıp seç. Aynı isimde dosya varsa üzerine yazar.",
        )
        if up_files:
            cs_up = st.columns([1, 4])
            with cs_up[0]:
                if st.button("⬆ Yükle", type="primary",
                              key=f"{key_prefix}_save_btn",
                              use_container_width=True):
                    ok_count, err_count = 0, 0
                    errs = []
                    for uf in up_files:
                        ok, msg = save_style_guide(uf.name, uf.read())
                        if ok:
                            ok_count += 1
                        else:
                            err_count += 1
                            errs.append(f"{uf.name}: {msg}")
                    if ok_count:
                        st.toast(f"{ok_count} dosya kaydedildi.", icon="✅")
                    if err_count:
                        st.error("Hatalar:\n" + "\n".join(errs))
                    st.rerun()
            with cs_up[1]:
                st.caption(f"Seçilen: {len(up_files)} dosya — toplam "
                           f"{sum(uf.size for uf in up_files) // 1024} KB")

    # Mevcut dosyalar
    guides = list_style_guides()
    if not guides:
        empty_state(
            "📚",
            "Henüz style guide yüklenmemiş",
            "Üstteki yükle alanından dosyalarını ekle. Bunlar her job'da "
            "NotebookLM kaynak listesine otomatik girer.",
        )
    else:
        st.markdown(
            f'<div style="font-size:0.82rem; opacity:0.7; margin:6px 0;">'
            f'<b>{len(guides)} dosya</b> · her submit\'te tüm dosyalar '
            f'NotebookLM\'e gider</div>',
            unsafe_allow_html=True,
        )
        for g in guides:
            with st.container(border=True):
                cs_g = st.columns([4, 1.5, 1])
                with cs_g[0]:
                    icon = "📄"
                    ext = Path(g["name"]).suffix.lower()
                    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        icon = "🖼"
                    elif ext in (".mp3", ".m4a", ".wav"):
                        icon = "🎵"
                    elif ext == ".pdf":
                        icon = "📕"
                    st.markdown(
                        f'<div style="font-size:0.92rem; font-weight:600;">'
                        f'{icon} {g["name"]}</div>'
                        f'<div style="font-size:0.74rem; opacity:0.65; margin-top:2px;">'
                        f'{g["size"] // 1024} KB · '
                        f'<i>NotebookLM\'de source adı: <code>{Path(g["name"]).stem}</code></i>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                with cs_g[1]:
                    st.caption(fmt_time(g["uploaded_at"]))
                with cs_g[2]:
                    if st.button("🗑 Sil",
                                  key=f"{key_prefix}_del_{g['name']}",
                                  use_container_width=True):
                        if delete_style_guide(g["name"]):
                            st.toast(f"{g['name']} silindi.", icon="🗑")
                            st.rerun()
                        else:
                            st.error("Silinemedi.")


# ---------------------------------------------------------------------------
# BULK DRIVE IMPORT widget — hem admin tab'ında hem user view'da kullanılır.
# Drive klasöründen tüm .docx → Job (queued). Image yok, custom prompt tek.
# ---------------------------------------------------------------------------
def render_bulk_drive_section(*, key_prefix: str = "blk") -> None:
    """Drive Toplu UI bloğu. key_prefix farklı bağlamlarda session_state
    çakışmasın diye verilir ('adm' admin tab, 'usr' user view)."""
    section_header(
        "🗂️ Drive'dan toplu docx import",
        "Bir Drive klasöründeki tüm .docx/.txt/.md dosyalarını otomatik script yap, queue'ya at",
    )
    if not _BULK_AVAILABLE:
        st.error(
            f"bulk_import modülü yüklü değil: {_bulk_imp_err_msg}. "
            "Server'da `pip install gdown python-docx` çalıştır."
        )
        return
    _bulk_ok, _bulk_msg = bulk_is_available()
    if not _bulk_ok:
        st.error(f"Bulk import bağımlılıkları eksik: {_bulk_msg}")
        return
    st.caption(f"✓ {_bulk_msg}")

    # ---- Input ----
    st.markdown(
        '**Drive klasörü URL veya ID** — klasör <b>"Anyone with the link"</b> '
        'olmalı (sağ tık → Share → Genel link). Aksi halde erişim hatası alır.',
        unsafe_allow_html=True,
    )
    url_key = f"{key_prefix}_bulk_drive_url"
    tpl_key = f"{key_prefix}_bulk_prompt_template"
    preview_key = f"{key_prefix}_bulk_preview"

    drive_url = st.text_input(
        "Drive klasör URL/ID",
        key=url_key,
        placeholder="https://drive.google.com/drive/folders/1AbCdEf...",
    )

    # Kaç versiyon: her dosya bu kadar kez üretilir (v1..vN). Aynı script +
    # guide ile N ayrı Cinematic çıktı → farklı varyasyonlar. Hepsi TEK batch'te,
    # job.version=1..N (Step B versiyon-farkındalıklı dedup ile durdurulmaz).
    versions_key = f"{key_prefix}_bulk_versions"
    n_versions = int(st.number_input(
        "Kaç versiyon üretilsin? (her dosya bu kadar kez → v1..vN)",
        min_value=1, max_value=8, value=1, step=1, key=versions_key,
    ))
    # Öncelik: işaretliyse bu batch sıranın BAŞINA geçer (bekleyen normal işler
    # durur gibi bekler), bitince eski işler kaldığı yerden devam eder. Çalışan
    # işler öldürülmez (boşa gitmez) — yeni dispatch'ler bu batch'e gider.
    _priority_key = f"{key_prefix}_bulk_priority"
    _is_priority = st.checkbox(
        "⚡ Öncelikli — bu linki sıranın başına al",
        key=_priority_key,
        help="Bekleyen diğer toplu işler beklemeye geçer, önce bu link üretilir. "
             "Bitince diğerleri kaldığı yerden devam eder. Çalışan videolar kesilmez.",
    )

    # Drive Toplu default: tek bir script + guide kullanıldığında {{SOURCES_LIST}}
    # bulk submit anında render edilemiyor (asset listesi yok). Statik versiyon —
    # NotebookLM template'i yine kuralları source'tan okur, sadece prompt'ta
    # source numaralandırması generic kalır.
    _default_template = (
        DEFAULT_CUSTOM_PROMPT_TEMPLATE
        .replace(
            "{{SOURCES_LIST}}",
            "Source 1: Execution Guide — STRICT visual rules "
            "(Text-Free / Fully Realistic Style / Historical Accuracy). "
            "Apply these rules to EVERY scene.\n"
            "Source 2: <Script filename> — verbatim narration content.",
        )
    )
    st.markdown("**Custom prompt template** (tüm jobs'lar bunu kullanır):")
    prompt_template = st.text_area(
        "Template",
        value=st.session_state.get(tpl_key, _default_template),
        key=tpl_key,
        height=140,
        label_visibility="collapsed",
    )

    st.info(
        "🔒 Her job için NotebookLM'e otomatik yüklenen source'lar:\n"
        "  • **Script** + **Learning Objectives** (`<name>_lo.<ext>` companion varsa)\n"
        "  • **3 ayrı sabit guide** (Narrative & Text-Free / "
        "Historical Accuracy / Fully Realistic Style)\n"
        "  • **Custom Prompt** (Role/Task/Constraints)\n"
        "  • Görseller\n\n"
        "Drive klasöründe `senaryo1.docx` + `senaryo1_lo.docx` (veya .txt/.md) "
        "çiftleri otomatik eşleştirilir — LO ayrı bir source olarak yüklenir. "
        "Mix de OK: `senaryo1.docx` + `senaryo1_lo.txt` çalışır.",
        icon="ℹ️",
    )
    with st.expander("👁 4 sabit guide'ı gör (read-only)"):
        for filename, text in EXECUTION_GUIDE_FILES:
            st.markdown(f"**📄 {filename}**")
            st.code(text, language=None)

    cols = st.columns([1, 1, 2])
    with cols[0]:
        preview_btn = st.button("👁 Önizle", use_container_width=True,
                                key=f"{key_prefix}_bulk_preview_btn")
    with cols[1]:
        submit_btn = st.button(
            "🚀 Hepsini queue'ya at",
            use_container_width=True, type="primary",
            key=f"{key_prefix}_bulk_submit_btn",
        )

    if preview_key not in st.session_state:
        st.session_state[preview_key] = None

    # ---- Önizleme handler ----
    if preview_btn:
        folder_id = bulk_extract_folder_id(drive_url) if drive_url else None
        if not folder_id:
            st.error("Geçerli Drive klasör URL/ID değil.")
        else:
            with st.spinner(f"Drive klasörü taranıyor (folder_id={folder_id[:12]}…)…"):
                try:
                    from bulk_import import (
                        list_drive_folder_docx, get_docx_metadata, pair_docx_with_lo,
                    )
                    import tempfile
                    _tmp = Path(tempfile.mkdtemp(prefix="bulk_preview_"))
                    docx_paths = list_drive_folder_docx(drive_url, _tmp)
                    pairs = pair_docx_with_lo(docx_paths)
                    # items: sadece ana (main) docx'ler. _lo companion'lar
                    # eşleştirilenler item gibi tekrar listelenmez.
                    items = []
                    for main_p, lo_p in pairs:
                        md = get_docx_metadata(main_p)
                        items.append({
                            "path": str(main_p),
                            "name": main_p.name,
                            "size": main_p.stat().st_size,
                            "modified": md.get("modified"),
                            "created": md.get("created"),
                            "author": md.get("author", ""),
                            "n_paragraphs": md.get("n_paragraphs", 0),
                            "lo_path": str(lo_p) if lo_p else "",
                            "lo_name": lo_p.name if lo_p else "",
                        })
                    # Son değiştirilme tarihine göre sırala (yeni en üstte)
                    items.sort(
                        key=lambda x: x.get("modified") or x.get("created") or "",
                        reverse=True,
                    )
                    st.session_state[preview_key] = {
                        "folder_id": folder_id,
                        "count": len(items),
                        "items": items,
                        "tmp_dir": str(_tmp),
                    }
                    # Tüm dosyaları default olarak seçili tut (checkbox state'ler)
                    for it in items:
                        sel_key = f"{key_prefix}_bulk_sel_{it['name']}"
                        if sel_key not in st.session_state:
                            st.session_state[sel_key] = True
                except Exception as e:
                    st.error(f"Drive okuma hatası: {type(e).__name__}: {e}")
                    st.session_state[preview_key] = None

    preview = st.session_state.get(preview_key)
    if preview:
        # Modified time formatter
        def _fmt_dt(iso_str: Optional[str]) -> str:
            if not iso_str:
                return "—"
            try:
                dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                # UTC ise yerel saate çevirme yapmadan göster (kullanıcı +03 farkı görüyor)
                return dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                return iso_str[:16]

        # Şu an seçili olanları say
        n_selected = sum(
            1 for it in preview["items"]
            if st.session_state.get(f"{key_prefix}_bulk_sel_{it['name']}", True)
        )

        # Header + toggle-all
        hd = st.columns([3, 1, 1])
        with hd[0]:
            st.success(
                f"✓ **{preview['count']}** doküman · "
                f"**{n_selected}** seçili"
            )
        with hd[1]:
            if st.button("☑ Hepsi", key=f"{key_prefix}_bulk_sel_all",
                         use_container_width=True):
                for it in preview["items"]:
                    st.session_state[f"{key_prefix}_bulk_sel_{it['name']}"] = True
                st.rerun()
        with hd[2]:
            if st.button("☐ Hiçbiri", key=f"{key_prefix}_bulk_sel_none",
                         use_container_width=True):
                for it in preview["items"]:
                    st.session_state[f"{key_prefix}_bulk_sel_{it['name']}"] = False
                st.rerun()

        # Per-file checkbox + metadata table
        with st.expander(f"Dosya listesi ({preview['count']})", expanded=True):
            for it in preview["items"]:
                sel_key = f"{key_prefix}_bulk_sel_{it['name']}"
                cs = st.columns([0.5, 4, 3, 1.5])
                with cs[0]:
                    st.checkbox(
                        "",
                        key=sel_key,
                        label_visibility="collapsed",
                    )
                with cs[1]:
                    lo_badge = ""
                    if it.get("lo_name"):
                        lo_badge = (
                            f" <span style='font-size:0.72rem; "
                            f"padding:1px 6px; background:rgba(99,102,241,0.15); "
                            f"border-radius:8px; color:#6366f1;'>+ LO</span>"
                        )
                    st.markdown(
                        f"<div style='font-size:0.88rem; font-weight:500;'>"
                        f"📄 {_esc(it['name'])}{lo_badge}</div>"
                        + (f"<div style='font-size:0.72rem; opacity:0.55;'>"
                           f"↳ {_esc(it['lo_name'])}</div>" if it.get("lo_name") else ""),
                        unsafe_allow_html=True,
                    )
                with cs[2]:
                    mod = _fmt_dt(it.get("modified"))
                    author = it.get("author") or "—"
                    st.markdown(
                        f"<div style='font-size:0.78rem; opacity:0.75;'>"
                        f"📅 {mod} &nbsp;·&nbsp; ✍️ {_esc(author[:30])}</div>",
                        unsafe_allow_html=True,
                    )
                with cs[3]:
                    sz_kb = it["size"] // 1024
                    n_p = it.get("n_paragraphs", 0)
                    st.markdown(
                        f"<div style='font-size:0.74rem; opacity:0.65; "
                        f"text-align:right;'>{sz_kb}KB · {n_p} para</div>",
                        unsafe_allow_html=True,
                    )

        _profs_init = [p for p in load_profiles() if p.initialized]
        _daily_cap = sum(max(p.daily_limit or 0, 0) for p in _profs_init) or 9
        _total_jobs = n_selected * n_versions
        _days = max(1, -(-_total_jobs // _daily_cap))
        _ver_note = (
            f" × **{n_versions} versiyon** = **{_total_jobs} job**"
            if n_versions > 1 else ""
        )
        st.caption(
            f"📅 Kapasite: {len(_profs_init)} profil × "
            f"{(_daily_cap // len(_profs_init)) if _profs_init else 0}/gün "
            f"= **{_daily_cap} job/gün** → **~{_days} gün**'de biter "
            f"({n_selected} seçili dosya{_ver_note})"
        )

    # ---- Submit handler ----
    if submit_btn:
        folder_id = bulk_extract_folder_id(drive_url) if drive_url else None
        # Env-eşleşmesiz submit'i bloklama: silent-skip dispatcher'ı sonsuz
        # döngüye sokuyordu (env=dev jobs, env=prod profiles → 34h sessizlik).
        _submit_env = current_env()
        _env_profiles = [
            p for p in load_profiles()
            if p.initialized and (p.environment or "prod").strip().lower() == _submit_env
        ]
        if not folder_id:
            st.error("Geçerli Drive klasör URL/ID değil.")
        elif not prompt_template.strip():
            st.error("Custom prompt boş olamaz.")
        elif not _env_profiles:
            st.error(
                f"`{_submit_env}` ortamında initialized profil yok — "
                f"submit blokludur (jobs dispatch edilmezdi). "
                f"Önce bir profil ekle ve login et veya farklı env'e geç."
            )
        else:
            submitter = _user_name() or "bulk"

            # current_env'i closure'a yakala (background içinde kullanılır)
            _bulk_env = _submit_env

            def _job_factory(title, text, custom_prompt, submitted_by,
                             learning_objectives: str = ""):
                return {
                    "id": uuid.uuid4().hex[:12],
                    "title": title,
                    "text": text,
                    "custom_prompt": custom_prompt,
                    "submitted_by": submitted_by,
                    "status": "queued",
                    "assets": [],
                    "created_at": time.time(),
                    "learning_objectives": learning_objectives,
                    "environment": _bulk_env,
                }

            progress_box = st.empty()

            def _progress(msg: str) -> None:
                try:
                    progress_box.info(msg)
                except Exception:
                    pass

            # Cached preview varsa onu kullan (re-download'a gerek yok) +
            # checkbox filter uygula. Preview yoksa direkt download.
            cached = st.session_state.get(preview_key)
            try:
                if cached and cached.get("items"):
                    # Checkbox'tan seçili olanları al — her main için _lo
                    # companion path'i de listeye ekle ki bulk_create_jobs
                    # içindeki pair_docx_with_lo onu bulup eşleştirsin.
                    sel_paths = []
                    for it in cached["items"]:
                        sel_key = f"{key_prefix}_bulk_sel_{it['name']}"
                        if st.session_state.get(sel_key, True):
                            p = Path(it["path"])
                            if p.exists():
                                sel_paths.append(p)
                            lo_str = it.get("lo_path", "")
                            if lo_str:
                                lo_p = Path(lo_str)
                                if lo_p.exists():
                                    sel_paths.append(lo_p)
                    if not sel_paths:
                        st.error("Hiçbir dosya seçili değil.")
                        result = None
                    else:
                        _progress(
                            f"{len(sel_paths)} seçili dosya işleniyor "
                            f"(cached, yeniden indirilmiyor)…"
                        )
                        from bulk_import import bulk_create_jobs_from_docx_paths
                        result = bulk_create_jobs_from_docx_paths(
                            sel_paths,
                            custom_prompt_template=prompt_template,
                            submitted_by=submitter,
                            job_factory=_job_factory,
                            on_progress=_progress,
                        )
                        result["total_files"] = len(sel_paths)
                else:
                    # Preview yapılmadıysa direkt download + tümünü işle
                    result = bulk_import_from_drive(
                        drive_url_or_id=drive_url,
                        custom_prompt_template=prompt_template,
                        submitted_by=submitter,
                        job_factory=_job_factory,
                        on_progress=_progress,
                    )
            except Exception as e:
                st.error(f"Bulk import hatası: {type(e).__name__}: {e}")
                result = None

            if result:
                existing = load_jobs()
                created_jobs: list = []
                # Öncelikli batch → priority=now (yeni öncelikli batch'ler eskinin
                # de üstüne çıkar). Normal=0 (FIFO en sonda).
                _batch_priority = time.time() if st.session_state.get(_priority_key) else 0.0
                # Batch oluştur — tüm job'lar (tüm versiyonlar) aynı oturuma bağlı
                _batch_id = uuid.uuid4().hex[:12]
                _n_files = len(result["created"])
                _ver_suffix = f" ({n_versions} versiyon)" if n_versions > 1 else ""
                _batch_name = (
                    f"Drive · {datetime.now().strftime('%d.%m %H:%M')}{_ver_suffix}"
                )
                # Her dosya için N versiyon: version=1..N, hepsi TEK batch'te.
                # v1 orijinal id'yi kullanır; v2..vN yeni uuid + hafif artan
                # created_at (created_at-rank = version sırası garanti → azure
                # _v1.._vN isimlendirmesi ve _closed_job_url ile tutarlı).
                for jd in result["created"]:
                    for _v in range(1, n_versions + 1):
                        try:
                            j = Job(
                                id=(jd["id"] if _v == 1 else uuid.uuid4().hex[:12]),
                                title=jd["title"],
                                text=jd["text"],
                                profile_id="",
                                profile_name="",
                                status=jd["status"],
                                submitted_by=jd["submitted_by"],
                                created_at=jd["created_at"] + (_v - 1) * 0.001,
                                custom_prompt=jd["custom_prompt"],
                                learning_objectives=jd.get("learning_objectives", ""),
                                environment=jd.get("environment", "prod"),
                                batch_id=_batch_id,
                                version=_v,
                                priority=_batch_priority,
                            )
                            j.assets = []
                            created_jobs.append(j)
                        except Exception as e:
                            result["errors"].append(
                                (jd.get("title", "?"), f"Job dataclass error: {e}")
                            )
                existing.extend(created_jobs)
                save_jobs(existing)
                # Batch kaydet
                _batch = Batch(
                    id=_batch_id,
                    name=_batch_name,
                    source=drive_url,
                    total=len(created_jobs),
                    submitted_by=submitter or "admin",
                )
                _existing_batches = load_batches()
                _existing_batches.append(_batch)
                save_batches(_existing_batches)
                # Slack: toplu ekleme bildirimi (TEK mesaj — N versiyon dahil)
                _ver_line = (
                    f"🎞 {_n_files} dosya × {n_versions} versiyon\n"
                    if n_versions > 1 else ""
                )
                send_slack_message(
                    f"📥 *{len(created_jobs)} proje kuyruğa eklendi*\n"
                    f"📁 {_batch_name}\n"
                    f"{_ver_line}"
                    f"📎 {drive_url}\n"
                    f"⏳ Tümü sırada — işleme alınmayı bekliyor"
                )
                progress_box.empty()
                _ver_ok = (
                    f" ({_n_files} dosya × {n_versions} versiyon)"
                    if n_versions > 1 else ""
                )
                st.success(
                    f"✅ {len(created_jobs)} job kuyruğa eklendi{_ver_ok} "
                    f"({len(result['errors'])} hatalı)."
                )
                if result["errors"]:
                    with st.expander(f"⚠ Hatalı dosyalar ({len(result['errors'])})"):
                        for fname, err in result["errors"]:
                            st.markdown(f"• **{fname}** — {err}")
                st.caption(
                    "📊 Aşağıdaki 'son istekler' listesinde queued jobs'ları "
                    "takip edebilirsin (admin: 'Durum' sekmesinde de görünür)."
                )
                st.session_state[preview_key] = None


# ---------------------------------------------------------------------------
# USER VIEW — Mustafa-tier sadeliği. Tek sayfa, tek textarea, tek button.
# ---------------------------------------------------------------------------
def render_user_view() -> None:
    profiles_all = load_profiles()
    jobs_all = load_jobs()
    today = date.today()

    # --- Environment routing: URL ?env=dev|prod ---
    env = current_env()

    # Auto-fallback: seçili env'de initialized profil yoksa AMA diğer env'de
    # varsa, otomatik dolu env'e geç. Sticky "dev" session cache'i + boş env
    # "hesap yok" confusion'ını çözer (kullanıcı manuel link tıklamak zorunda
    # kalmaz). URL'de açık ?env= varsa ona saygı: sadece session cache fallback.
    def _env_has_init(e: str) -> bool:
        return any(
            (p.environment or "prod").strip().lower() == e and p.initialized
            for p in profiles_all
        )
    if not _env_has_init(env):
        _other = "prod" if env == "dev" else "dev"
        if _env_has_init(_other):
            env = _other
            st.session_state["_env"] = env

    # Env badge + dev/prod switch link (TEK SATIR HTML — çok satırlı + girintili
    # HTML'i Streamlit markdown kod-bloğu sanıp bozuyordu, "kesik" görünüm).
    _env_color = "#f59e0b" if env == "dev" else "#10b981"  # dev=turuncu, prod=yeşil
    _env_label = "🧪 DEV" if env == "dev" else "🚀 PROD"
    _other_env = "prod" if env == "dev" else "dev"
    _other_label = "🚀 PROD" if env == "dev" else "🧪 DEV"
    # Env link'i session token'ı (?t=) korumalı — yoksa env değişince logout.
    _tok = st.session_state.get("session_token", "")
    _env_href = f"?env={_other_env}" + (f"&t={_tok}" if _tok else "")
    st.markdown(
        f'<div style="display:flex; align-items:center; gap:12px; padding:10px 16px; '
        f'background:{_env_color}15; border-left:4px solid {_env_color}; '
        f'border-radius:8px; margin:4px 0 16px 0;">'
        f'<span style="font-size:1.05rem; font-weight:700; color:{_env_color};">{_env_label}</span>'
        f'<span style="opacity:0.6; font-size:0.85rem;">ortamı aktif</span>'
        f'<a href="{_env_href}" target="_self" style="margin-left:auto; '
        f'font-size:0.85rem; text-decoration:none; padding:4px 10px; '
        f'background:#ffffff20; border-radius:6px; color:inherit;">'
        f'{_other_label} ortamına geç →</a>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Bu env'in profilleri + job'ları (UI'da sadece o env görünür)
    profiles = [p for p in profiles_all
                if (p.environment or "prod").strip().lower() == env]
    jobs = [j for j in jobs_all
            if (j.environment or "prod").strip().lower() == env]

    # Aktif hesap sayısı + bugün üretilen toplam
    initialized_profiles = [p for p in profiles if p.initialized]
    today_total = sum(
        1 for j in jobs
        if j.status in COUNTED_STATUSES and j.started_at
        and datetime.fromtimestamp(j.started_at).date() == today
    )

    # Kullanılabilir hesap var mı? (Kota dolu olanlar hariç)
    # Time-based block: son QUOTA_BLOCK_HOURS saat içinde kota hatası yediyse pas
    # geç (UTC date rollover yerine; Google'ın Pacific reset'iyle uyumlu).
    def _profile_blocked(pid: str) -> bool:
        cutoff = time.time() - QUOTA_BLOCK_HOURS * 3600
        for j in jobs:
            if j.profile_id != pid or not j.error:
                continue
            err = j.error.lower()
            if "kota" not in err and "limit" not in err:
                continue
            ts = j.finished_at or j.started_at or j.created_at
            if ts > cutoff:
                return True
        return False

    available_profiles = [p for p in initialized_profiles if not _profile_blocked(p.id)]
    no_profile = len(initialized_profiles) == 0
    all_blocked = len(initialized_profiles) > 0 and len(available_profiles) == 0

    # Hero
    if no_profile:
        status_line = "Yönetici hesap eklemeli"
    elif all_blocked:
        status_line = "Tüm hesaplar kotaya doldu — ~8 saatte bir otomatik yeniden denenir"
    else:
        status_line = f"{len(available_profiles)} hesap hazır · bugün {today_total} video tetiklendi"

    hero("Senaryonu Gönder, Video Üretelim", status_line)

    # ===== 📊 Hesap kotaları (bugün) — TÜM kullanıcılar görür =====
    # Her hesabın günlük kota kullanımı. Kapasite planlaması için: kaç slot
    # boş, hangi hesap dolu, hangi hesap kota-blocked / login gerekli.
    _today_used = {}
    for _j in jobs:
        if _j.status not in ("done", "running", "generating", "submitted"):
            continue
        if (_j.title or "").startswith("[KOTA]"):
            continue
        _ts = _j.started_at or _j.created_at
        if not _ts:
            continue
        try:
            if datetime.fromtimestamp(_ts).date() != today:
                continue
        except (OSError, OverflowError, ValueError):
            continue
        _today_used[_j.profile_id] = _today_used.get(_j.profile_id, 0) + 1

    _total_used = sum(
        min(_today_used.get(p.id, 0), p.daily_limit or 99)
        for p in initialized_profiles
    )
    _total_cap = sum((p.daily_limit or 0) for p in initialized_profiles)
    # Boş slot = SADECE kota-bloklanmamış hesapların kalan limiti. Bloklu hesabın
    # kalan limiti bugün KULLANILAMAZ (8h-blok) → "boş" sayma. Önceden _total_cap
    # - _total_used idi → bloklu hesapların kalanını da boş sayıyordu (ör. Ultra
    # 20-6=14 hayalet slot → yanıltıcı "27 slot boş"; gerçekte sadece ~3 müsait).
    _blocked_ids = {p.id for p in initialized_profiles if _profile_blocked(p.id)}
    _free = sum(
        max(0, (p.daily_limit or 0)
            - min(_today_used.get(p.id, 0), p.daily_limit or 99))
        for p in initialized_profiles
        if p.id not in _blocked_ids
    )
    _n_blocked = len(_blocked_ids)
    _qlabel = (
        f"📊 Hesap kotaları · bugün {_total_used}/{_total_cap} kullanıldı"
        f" · {_free} slot müsait"
        + (f" · 🛑 {_n_blocked} bloklu" if _n_blocked else "")
    )
    with st.expander(_qlabel, expanded=False):
        if _total_cap:
            st.progress(min(1.0, _total_used / _total_cap),
                        text=f"{_total_used}/{_total_cap} slot kullanıldı (bugün)")
        # env'deki TÜM profiller (initialized + login-gerekli) — isim sıralı
        _qcols = st.columns(2)
        _all_env_profiles = sorted(profiles, key=lambda x: x.name.lower())
        for _i, _p in enumerate(_all_env_profiles):
            _used = _today_used.get(_p.id, 0)
            _lim = _p.daily_limit or 0
            if not _p.initialized:
                _icon, _txt = "🔑", "giriş gerekli"
            elif _profile_blocked(_p.id):
                _icon, _txt = "🛑", "kota dolu (bugün)"
            elif _lim and _used >= _lim:
                _icon, _txt = "🔴", f"{_used}/{_lim} dolu"
            elif _used == 0:
                _icon, _txt = "🟢", f"0/{_lim} boş"
            else:
                _icon, _txt = "🟡", f"{_used}/{_lim}"
            _nm = _p.name if len(_p.name) <= 26 else _p.name[:25] + "…"
            with _qcols[_i % 2]:
                st.markdown(
                    f"<div style='font-size:0.85rem; padding:2px 0;'>"
                    f"{_icon} <b>{_nm}</b> — <span style='opacity:0.75;'>{_txt}</span></div>",
                    unsafe_allow_html=True,
                )
        # Reset bilgisi — NotebookLM Cinematic kotasının sabit reset saati YOK
        # (ampirik: Pasifik gece yarısında bile dolu çıkabiliyor, rolling gibi).
        # Sistem dolu hesabı ~8 saatte bir otomatik tekrar dener; kota açılınca
        # kendiliğinden üretime devam eder.
        st.caption(
            "ℹ️ NotebookLM kotasının sabit reset saati yok (düzensiz/rolling). "
            "Dolu hesaplar ~8 saatte bir otomatik yeniden denenir; kota "
            "açıldığında üretim kendiliğinden devam eder. 🔑 = admin giriş yapmalı."
        )

    # ===== Phase 4: Revize Modal =====
    # Job history'deki '✏ Revize et' butonuna basılınca revize_target_id set
    # edilir; aşağıda render edilir.
    _revize_target_id = st.session_state.get("revize_target_id", "")
    if _revize_target_id:
        _target_job = next((x for x in jobs if x.id == _revize_target_id), None)
        if _target_job and _target_job.video_remote_url:
            with st.container(border=True):
                st.markdown(
                    f'<div style="font-size:1.05rem; font-weight:700; margin-bottom:0.2rem;">'
                    f'✏ Videoyu Revize Et</div>'
                    f'<div style="font-size:0.85rem; opacity:0.75; margin-bottom:0.5rem;">'
                    f'Önceki video yeni notebook\'a source olarak eklenir. '
                    f'Yazdığın talimat + opsiyonel yeni görseller ile NotebookLM '
                    f'yeni bir Cinematic versiyon üretir.</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div style="font-size:0.85rem; padding:8px 12px; '
                    f'background:rgba(99,102,241,0.06); border-radius:6px; margin-bottom:8px;">'
                    f'<b>📹 Revize edilecek video:</b><br>'
                    f'<span style="font-size:0.78rem; opacity:0.85; font-style:italic;">'
                    f'{_esc((_target_job.title or "(başlıksız)")[:80])}</span><br>'
                    f'<a href="{_target_job.video_remote_url}" target="_blank" '
                    f'style="font-size:0.8rem;">☁️ Mevcut videoyu oynat</a>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # Session_state key — modal kapatınca temizle
                if "revize_instructions" not in st.session_state:
                    st.session_state["revize_instructions"] = ""
                if "revize_image_urls" not in st.session_state:
                    st.session_state["revize_image_urls"] = ""

                def _cb_revize_cancel() -> None:
                    st.session_state["revize_target_id"] = ""
                    st.session_state["revize_instructions"] = ""
                    st.session_state["revize_image_urls"] = ""

                def _cb_revize_submit() -> None:
                    instr = (st.session_state.get("revize_instructions", "") or "").strip()
                    if not instr:
                        st.session_state["_script_msg"] = (
                            "err", "Revize talimatı boş olamaz."
                        )
                        return
                    parent = next((x for x in load_jobs()
                                   if x.id == st.session_state.get("revize_target_id")), None)
                    if not parent or not parent.video_remote_url:
                        st.session_state["_script_msg"] = (
                            "err", "Parent video bulunamadı veya Azure URL eksik."
                        )
                        return
                    # Opsiyonel yeni görseller: her satıra bir URL. Asset
                    # formatına çevrilir → pipeline download_job_images ile
                    # indirip notebook'a source olarak ekler (bozuk URL atlanır).
                    _img_urls = [
                        u.strip() for u in
                        (st.session_state.get("revize_image_urls", "") or "").splitlines()
                        if u.strip()
                    ]
                    rev_assets: list = []
                    for _i, _u in enumerate(_img_urls[:8], 1):
                        if not _u.lower().startswith(("http://", "https://")):
                            _u = "https://" + _u
                        rev_assets.append({
                            "id": uuid.uuid4().hex[:8],
                            "position": "",
                            "description": f"Revize görseli {_i}",
                            "query": "",
                            "style": "photo",
                            "selected_image": {
                                "full_url": _u,
                                "thumb_url": _u,
                                "source": "manual",
                                "license": "?",
                            },
                        })
                    # Yeni revize job'u oluştur — text alanı revize talimatı
                    # (script yerine geçer; create-video custom prompt'u
                    # gerçek revize bilgisini içerir)
                    new_id = uuid.uuid4().hex[:12]
                    rev_title = f"[Revize] {(parent.title or 'Untitled')[:60]}"
                    # Custom prompt: revize bağlamını ekle, kalan template'i kullan
                    rev_custom_prompt = (
                        f"Role: You are a video revision editor. The user submitted a "
                        f"previous Cinematic video (Source 1, MP4) and wants a revised "
                        f"version following these instructions:\n\n"
                        f"{instr}\n\n"
                        f"Constraints:\n"
                        f"1. Use the previous video (Source 1) as the visual and narrative baseline.\n"
                        f"2. Apply the user's revision instructions above.\n"
                        f"3. Keep the same overall topic/learning objective.\n"
                        f"4. Maintain zero-text visuals, high-key lighting, soft geometry.\n"
                        f"5. 80% photorealistic, 20% illustration; no mixed frames.\n"
                        f"6. Cinematic style throughout.\n"
                    )
                    new_job = Job(
                        id=new_id,
                        title=rev_title,
                        text=instr,  # script alanı revize talimatı
                        submitted_by=_user_name(),
                        custom_prompt=rev_custom_prompt,
                        assets=rev_assets,  # opsiyonel yeni görseller
                        parent_job_id=parent.id,
                        revision_instructions=instr,
                        revision_video_url=parent.video_remote_url,
                        # Revize parent ile aynı env'de kalır (dev'in revisi
                        # dev'de, prod revize prod'da)
                        environment=(parent.environment or "prod"),
                    )
                    jobs_all = load_jobs()
                    jobs_all.append(new_job)
                    save_jobs(jobs_all)
                    st.session_state["revize_target_id"] = ""
                    st.session_state["revize_instructions"] = ""
                    st.session_state["revize_image_urls"] = ""
                    _img_note = f" (+{len(rev_assets)} görsel)" if rev_assets else ""
                    st.session_state["_script_msg"] = (
                        "ok", f"Revize kuyruğa eklendi: {rev_title[:50]}{_img_note}"
                    )

                st.text_area(
                    "Ne değişsin? (revize talimatı)",
                    key="revize_instructions",
                    height=160,
                    placeholder=(
                        "Örn. 'Narration daha yavaş olsun, hook'a 1 cümle ekle, "
                        "kapanış kısmını değiştir.' "
                        "Bu metin NotebookLM'e source olarak ek olarak custom "
                        "prompt'tan referans edilir."
                    ),
                )

                st.text_area(
                    "🖼 Yeni görsel URL'leri (opsiyonel — her satıra bir, max 8)",
                    key="revize_image_urls",
                    height=80,
                    placeholder=(
                        "https://upload.wikimedia.org/...jpg\n"
                        "https://...png\n"
                        "Boş bırakılabilir. URL'ler indirilip yeni notebook'a "
                        "görsel source olarak eklenir (bozuk URL atlanır)."
                    ),
                )

                cs_r = st.columns([2, 1, 1.4])
                with cs_r[1]:
                    st.button("İptal", on_click=_cb_revize_cancel,
                              use_container_width=True, key="btn_revize_cancel")
                with cs_r[2]:
                    st.button("🚀 Revize gönder",
                              type="primary",
                              on_click=_cb_revize_submit,
                              use_container_width=True, key="btn_revize_submit")

            st.markdown("&nbsp;", unsafe_allow_html=True)
        else:
            # Target bulunamadı, modal'ı kapat
            st.session_state["revize_target_id"] = ""

    # Eğer hiç hesap yoksa erken çık
    if no_profile:
        st.warning(
            "🛠 Henüz aktif Google hesabı yok. Yöneticiye haber ver — kurulum yapması gerek.",
            icon="🛠️",
        )
        return

    # Tüm hesaplar bloke ise uyarı (ama yine de submit göster, kuyruğa atılabilir)
    if all_blocked:
        st.warning(
            "🚫 Tüm hesapların bugünkü Cinematic kotası dolmuş. "
            "Yine de gönder — kotalar resetlenince (yarın TR ~10:00) otomatik üretilir.",
            icon="🚫",
        )

    # ===== Script editor (form değil, session_state-driven) =====
    # Streamlit kuralı: widget key'ine bağlı session_state, widget render
    # edildikten SONRA aynı run içinde mutate edilemez (StreamlitAPIException
    # fırlar). Çözüm: callback'leri (on_click) kullan — callback'ler bir sonraki
    # run'da widget render etmeden ÖNCE çalışır.

    # Submit sonrası ilk run'da widget'lardan ÖNCE alanları temizle.
    if st.session_state.pop("_clear_after_submit", False):
        st.session_state["script_draft"] = ""
        st.session_state["script_feedback"] = ""
        st.session_state["script_iterations"] = []
        st.session_state["script_assets"] = []
        st.session_state["script_custom_prompt"] = ""
        st.session_state["script_custom_prompt_user_edited"] = False
        st.session_state["script_title_override"] = ""
        st.session_state["script_n_versions"] = 1
        st.session_state["script_lo"] = ""
        st.session_state["ui_step"] = 1  # baştan başla

    # İlk yüklemede disk'ten restore (refresh / yeni sekme / başka cihazdan
    # gelirken yarım kalan draft'ı geri getir). Sadece ilk run'da çalışır.
    if "_script_draft_initialized" not in st.session_state:
        _user = _user_name()
        _saved = load_script_draft(_user) if _user else None
        if _saved:
            st.session_state["script_draft"] = _saved.get("script", "")
            st.session_state["script_iterations"] = _saved.get("iterations", []) or []
            st.session_state["script_assets"] = _saved.get("assets", []) or []
            st.session_state["script_custom_prompt"] = _saved.get("custom_prompt", "")
            st.session_state["script_custom_prompt_user_edited"] = bool(
                _saved.get("custom_prompt_edited", False)
            )
            st.session_state["script_lo"] = _saved.get("learning_objectives", "")
            if _saved.get("script"):
                # Kullanıcıya bildir — bilinmeyen yerden draft gelmesin
                st.session_state["_script_msg"] = (
                    "ok", "Yarım kalan draft'ın geri yüklendi."
                )
        else:
            st.session_state["script_draft"] = ""
            st.session_state["script_iterations"] = []
            st.session_state["script_assets"] = []
            st.session_state["script_custom_prompt"] = ""
            st.session_state["script_custom_prompt_user_edited"] = False
            st.session_state["script_lo"] = ""
        st.session_state["script_feedback"] = ""
        st.session_state["_script_draft_initialized"] = True

    if "script_draft" not in st.session_state:
        st.session_state["script_draft"] = ""
    if "script_iterations" not in st.session_state:
        st.session_state["script_iterations"] = []
    if "script_feedback" not in st.session_state:
        st.session_state["script_feedback"] = ""
    if "script_assets" not in st.session_state:
        st.session_state["script_assets"] = []
    if "script_custom_prompt" not in st.session_state:
        st.session_state["script_custom_prompt"] = ""
    # User'ın "auto-doldur düğmesine bastı mı" track'i — ilk asset'lerde otomatik
    # render edilince user override etmiş olabilir, oturumda flag ile koru.
    if "script_custom_prompt_user_edited" not in st.session_state:
        st.session_state["script_custom_prompt_user_edited"] = False
    # Callback'ler arası mesaj geçişi (toast/error). Render'da tüketilir.
    if "_script_msg" not in st.session_state:
        st.session_state["_script_msg"] = None  # ("ok"|"err", text)
    # Submit niyeti — submit callback'i set eder, ana akış kuyruğa ekler.
    if "_pending_submit" not in st.session_state:
        st.session_state["_pending_submit"] = False

    # ===== 3-Step UI state machine =====
    # ui_step = 1 → Step 1 (Senaryo)
    # ui_step = 2 → Step 1 tamam + Step 2 (Görseller) açık
    # ui_step = 3 → Step 1+2 tamam + Step 3 (Cinematic) açık
    if "ui_step" not in st.session_state:
        # Disk'ten restore edilen draft varsa ileri adımdan başlat
        if st.session_state.get("script_assets"):
            st.session_state["ui_step"] = 3
        elif (st.session_state.get("script_draft") or "").strip():
            st.session_state["ui_step"] = 2
        else:
            st.session_state["ui_step"] = 1
    # Step 1 — Weird Facts template form alanları
    for _k, _default in (
        ("wf_topic", ""),
        ("wf_grade", "7"),
        ("wf_language", "TR"),
        ("wf_lo", ""),
        ("wf_template_open", False),  # gizli expander state
    ):
        if _k not in st.session_state:
            st.session_state[_k] = _default

    # --- Persistence helper (disk autosave) ---
    def _persist_draft() -> None:
        u = _user_name()
        if u:
            save_script_draft(
                u,
                st.session_state.get("script_draft", ""),
                st.session_state.get("script_iterations", []),
                st.session_state.get("script_assets", []),
                custom_prompt=st.session_state.get("script_custom_prompt", ""),
                custom_prompt_edited=bool(st.session_state.get(
                    "script_custom_prompt_user_edited", False)),
                learning_objectives=st.session_state.get("script_lo", ""),
            )

    # --- Callbacks ---
    def _cb_regenerate() -> None:
        current = st.session_state.get("script_draft", "").strip()
        feedback = st.session_state.get("script_feedback", "").strip()
        model = st.session_state.get("script_model") or GEMINI_DEFAULT_MODEL
        if not current:
            st.session_state["_script_msg"] = ("err", "Önce bir senaryo yapıştır.")
            return
        if not feedback:
            st.session_state["_script_msg"] = ("err", "Feedback boş.")
            return
        ok, result = regenerate_script(current, feedback, model=model)
        if ok:
            st.session_state["script_iterations"].append({
                "script": current,
                "feedback": feedback,
                "model": model,
                "ts": time.time(),
            })
            st.session_state["script_draft"] = result.strip()
            st.session_state["script_feedback"] = ""
            st.session_state["_script_msg"] = ("ok", "Yeni versiyon hazır.")
            _persist_draft()
        else:
            st.session_state["_script_msg"] = ("err", result)

    def _cb_reset_history() -> None:
        st.session_state["script_iterations"] = []
        _persist_draft()

    def _cb_revert(actual_i: int) -> None:
        iters = st.session_state["script_iterations"]
        if 0 <= actual_i < len(iters):
            st.session_state["script_draft"] = iters[actual_i]["script"]
            # Bu noktadan sonraki versiyonları sil
            st.session_state["script_iterations"] = iters[:actual_i]
            st.session_state["_script_msg"] = ("ok", f"v{actual_i + 1} geri yüklendi.")
            _persist_draft()

    def _cb_text_changed() -> None:
        """text_area on_change: kullanıcı yazıp blur ettiğinde disk'e kaydet."""
        _persist_draft()

    # --- Step 1 callbacks ---
    def _cb_apply_wf_template() -> None:
        """Weird Facts form alanlarını template'e doldur, text area'ya yapıştır."""
        rendered = render_weird_facts_prompt(
            st.session_state.get("wf_topic", ""),
            st.session_state.get("wf_grade", ""),
            st.session_state.get("wf_language", ""),
            st.session_state.get("wf_lo", ""),
        )
        st.session_state["script_draft"] = rendered
        st.session_state["_script_msg"] = (
            "ok", "Weird Facts template'i dolduruldu. Şimdi 'Çıktı oluştur' butonuna bas."
        )
        _persist_draft()

    def _cb_generate_output() -> None:
        """'Çıktı oluştur': text area'daki prompt'u LLM'e gönder, script al.

        Üretilen script text alana yazılır, ui_step DEĞİŞMEZ (kullanıcı Step 1'de
        kalır, üretileni görüp düzenleyebilir). 'Çıktıyı kullan' → Step 2.
        """
        prompt = st.session_state.get("script_draft", "").strip()
        model = st.session_state.get("script_model") or GEMINI_DEFAULT_MODEL
        if not prompt:
            st.session_state["_script_msg"] = (
                "err", "Text area boş — önce prompt yapıştır ya da template doldur."
            )
            return
        ok, result = generate_script_from_prompt(prompt, model=model)
        if ok:
            # Orijinal prompt'u history'ye kaydet (input olarak)
            st.session_state["script_iterations"].append({
                "script": prompt,            # input prompt
                "feedback": "(initial output generation)",
                "model": model,
                "ts": time.time(),
            })
            st.session_state["script_draft"] = result.strip()
            # ui_step DEĞİŞMEZ — user üretileni görsün, düzenlerse düzenlesin,
            # hazır olunca 'Çıktıyı kullan' ile Step 2'ye geçsin.
            st.session_state["_script_msg"] = (
                "ok",
                f"Script üretildi ({len(result)} karakter). "
                "Aşağıda görebilirsin — düzenleyebilir veya direkt "
                "'Çıktıyı kullan' ile Step 2'ye geçebilirsin."
            )
            _persist_draft()
        else:
            st.session_state["_script_msg"] = ("err", result)

    def _cb_use_output() -> None:
        """'Çıktıyı kullan': text area'daki içeriği script olarak kabul et, Step 2'ye geç."""
        text = st.session_state.get("script_draft", "").strip()
        if not text:
            st.session_state["_script_msg"] = (
                "err", "Text area boş — script yapıştır."
            )
            return
        st.session_state["ui_step"] = max(st.session_state.get("ui_step", 1), 2)
        st.session_state["_script_msg"] = (
            "ok", "Script onaylandı. Sonraki adım: görseller."
        )
        _persist_draft()

    def _cb_back_to_step(step: int) -> None:
        """Önceki adıma dön — sadece ui_step değişir, veri kaybolmaz."""
        st.session_state["ui_step"] = max(1, step)
        st.session_state["_script_msg"] = ("ok", f"Step {step}'e geri döndün.")

    def _cb_skip_step2() -> None:
        """Step 2'yi atla — assets'i temizle, ui_step=3."""
        st.session_state["script_assets"] = []
        st.session_state["ui_step"] = 3
        st.session_state["_script_msg"] = (
            "ok", "Görseller atlandı. Custom Prompt ile devam edebilirsin."
        )
        _persist_draft()

    def _cb_submit() -> None:
        text = st.session_state.get("script_draft", "").strip()
        if not text:
            st.session_state["_script_msg"] = ("err", "Senaryo boş olamaz.")
            return
        st.session_state["_pending_submit"] = True

    # --- Phase B (asset extraction) callbacks ---
    def _cb_extract_assets() -> None:
        script = st.session_state.get("script_draft", "").strip()
        model = st.session_state.get("script_model") or GEMINI_DEFAULT_MODEL
        override = (
            st.session_state.get("asset_extractor_prompt_override", "") or ""
        ).strip()
        if not script:
            st.session_state["_script_msg"] = ("err", "Önce senaryo yapıştır.")
            return
        ok, assets, err = extract_assets(
            script, model=model,
            system_prompt_override=override if override else None,
        )
        if ok:
            st.session_state["script_assets"] = assets
            st.session_state["_script_msg"] = (
                "ok", f"{len(assets)} görsel önerildi. Listeyi düzenleyebilirsin."
            )
            _persist_draft()
        else:
            st.session_state["_script_msg"] = ("err", err)

    def _cb_clear_assets() -> None:
        st.session_state["script_assets"] = []
        _persist_draft()

    def _cb_delete_asset(asset_id: str) -> None:
        st.session_state["script_assets"] = [
            a for a in st.session_state.get("script_assets", [])
            if a.get("id") != asset_id
        ]
        _persist_draft()

    def _cb_add_asset() -> None:
        st.session_state["script_assets"].append({
            "id": uuid.uuid4().hex[:8],
            "position": "",
            "description": "",
            "query": "",
            "style": "photo",
        })
        _persist_draft()

    def _cb_asset_edit(asset_id: str, field_name: str, widget_key: str) -> None:
        """Inline edit: text_input/selectbox değiştiğinde asset dict'i güncelle."""
        new_val = st.session_state.get(widget_key, "")
        for a in st.session_state.get("script_assets", []):
            if a.get("id") == asset_id:
                a[field_name] = new_val
                break
        _persist_draft()

    # --- Phase C (image search) callbacks ---
    def _cb_search_images(asset_id: str) -> None:
        for a in st.session_state.get("script_assets", []):
            if a.get("id") != asset_id:
                continue
            q = (a.get("query") or "").strip()
            if not q:
                st.session_state["_script_msg"] = (
                    "err", "Search query boş — önce query alanını doldur."
                )
                return
            style = (a.get("style") or "photo").lower()
            results = search_images(q, limit=8, style=style)
            a["candidates"] = results
            a["search_done_at"] = time.time()
            if not results:
                st.session_state["_script_msg"] = (
                    "err", f"'{q}' için görsel bulunamadı. Query'yi değiştir veya Phase D (üret) sırasında kullanılacak."
                )
            else:
                st.session_state["_script_msg"] = (
                    "ok", f"{len(results)} aday görsel geldi. Birini seç."
                )
            _persist_draft()
            break

    def _cb_search_shutterstock(asset_id: str) -> None:
        """Shutterstock'ta ara (lisanslı stok). Sonuçlar filigranlı önizleme;
        seçilen görsel video üretilirken lisanslanır (1 indirme)."""
        for a in st.session_state.get("script_assets", []):
            if a.get("id") != asset_id:
                continue
            q = (a.get("query") or "").strip() or (a.get("description") or "").strip()
            if not q:
                st.session_state["_script_msg"] = ("err", "Arama için query/description gerek.")
                return
            results = shutterstock_search(q, limit=8, style=(a.get("style") or "photo").lower())
            a["candidates"] = results
            a["search_done_at"] = time.time()
            if not results:
                st.session_state["_script_msg"] = (
                    "err", "Shutterstock sonuç yok / arama hatası (loga bak).")
            else:
                st.session_state["_script_msg"] = (
                    "ok", f"{len(results)} Shutterstock sonucu — filigranlı önizleme; "
                          f"seçince video üretilirken lisanslanır (1 indirme).")
            _persist_draft()
            break

    def _cb_select_image(asset_id: str, cand_index: int) -> None:
        for a in st.session_state.get("script_assets", []):
            if a.get("id") != asset_id:
                continue
            cands = a.get("candidates") or []
            if 0 <= cand_index < len(cands):
                a["selected_image"] = cands[cand_index]
                _persist_draft()
            break

    def _cb_clear_selection(asset_id: str) -> None:
        for a in st.session_state.get("script_assets", []):
            if a.get("id") == asset_id:
                a.pop("selected_image", None)
                _persist_draft()
                break

    def _cb_generate_images(asset_id: str) -> None:
        """Gemini ile 4 görsel varyantı üret, candidates'e koy."""
        for a in st.session_state.get("script_assets", []):
            if a.get("id") != asset_id:
                continue
            # Prompt önceliği: query (EN) > description (TR fallback)
            q = (a.get("query") or "").strip()
            desc = (a.get("description") or "").strip()
            prompt = q or desc
            if not prompt:
                st.session_state["_script_msg"] = (
                    "err", "Üretim için query ya da description gerek."
                )
                return
            model = st.session_state.get(f"asset_genmodel_{asset_id}") or GEMINI_IMAGE_DEFAULT
            style = a.get("style", "photo")
            results = generate_images_gemini(prompt, count=4, model=model, style=style)
            if not results:
                st.session_state["_script_msg"] = (
                    "err", "Gemini görsel üretemedi (key/model/limit?). Loglara bak.")
                return
            a["candidates"] = results
            a["search_done_at"] = time.time()
            st.session_state["_script_msg"] = (
                "ok", f"{len(results)} Gemini varyant hazır."
            )
            _persist_draft()
            break

    # --- Phase E (custom prompt) callbacks ---
    def _cb_autofill_prompt() -> None:
        """Mevcut script title + selected assets'ten template doldur (image-script mapping)."""
        title = (
            (st.session_state.get("script_title_override", "") or "").strip()
            or derive_title(st.session_state.get("script_draft", ""))
            or "Untitled"
        )
        assets_full = st.session_state.get("script_assets", []) or []
        # Sadece selected_image olan asset'leri prompt'a koy
        sel_assets = [a for a in assets_full if a.get("selected_image")]
        rendered = render_custom_prompt(
            DEFAULT_CUSTOM_PROMPT_TEMPLATE, title, sel_assets,
            has_learning_objectives=bool(
                (st.session_state.get("script_lo", "") or "").strip())
        )
        st.session_state["script_custom_prompt"] = rendered
        st.session_state["script_custom_prompt_user_edited"] = False
        st.session_state["_script_msg"] = ("ok", "Custom prompt template'den dolduruldu.")
        _persist_draft()

    def _cb_prompt_edited() -> None:
        """text_area on_change: kullanıcı düzenlediyse 'edited' işaretle + persist."""
        st.session_state["script_custom_prompt_user_edited"] = True
        _persist_draft()

    def _cb_use_manual_url(asset_id: str, widget_key: str) -> None:
        """Kullanıcının yapıştırdığı URL'i selected_image olarak set et.

        Tolerant: protocol eksikse https:// prepend, tüm whitespace temizlenir.
        Wikipedia article URL'leri (#/media/File:Foo.jpg) otomatik Commons
        FilePath endpoint'ine yönlendirilir → direct image. HEAD request ile
        content-type doğrulanır — image olmadığı kesinse reddedilir.
        Hata mesajında ne paste edildiği görünür.
        """
        raw = st.session_state.get(widget_key) or ""
        url = "".join(raw.split()).strip()
        if not url:
            st.session_state["_script_msg"] = ("err", "URL kutusu boş.")
            return
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")
                or low.startswith("data:image/")):
            if "://" in url:
                st.session_state["_script_msg"] = (
                    "err",
                    f"Sadece http://, https:// veya data:image/ desteklenir "
                    f"(paste: {url[:60]}…)",
                )
                return
            url = "https://" + url
            low = url.lower()

        # --- Wikipedia article URL → direct image URL ---
        # 'https://en.wikipedia.org/wiki/Foo#/media/File:Bar.jpg' kalıbı
        # Commons Special:FilePath ile resolve edilir (302 → actual upload URL).
        wiki_marker = "/wiki/"
        media_marker = "#/media/file:"
        if wiki_marker in low and media_marker in low:
            try:
                # File:... kısmını ayır
                idx = low.index(media_marker)
                filename = url[idx + len(media_marker):]
                # URL decode ki space/Türkçe karakter düzgün geçsin
                filename = urllib.parse.unquote(filename)
                # Special:FilePath direct image redirect endpoint
                resolved = (
                    "https://commons.wikimedia.org/wiki/Special:FilePath/"
                    + urllib.parse.quote(filename)
                )
                st.session_state["_script_msg"] = (
                    "ok",
                    f"Wikipedia URL'i → Commons FilePath'e çevrildi: {filename[:60]}",
                )
                url = resolved
                low = url.lower()
            except Exception:
                pass  # heuristic fail → orijinali kullan, HEAD validate yapsın

        # --- HEAD request ile content-type doğrulama (data: hariç) ---
        # Hızlı sanity check — image değilse paste anında uyar (submit'e bırakma).
        if not low.startswith("data:"):
            try:
                req = urllib.request.Request(
                    url, method="HEAD", headers=_DOWNLOAD_HEADERS,
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    ctype = r.headers.get("Content-Type", "").lower()
                if ctype and not ctype.startswith("image/"):
                    # Yine de izin ver ama uyar — bazı CDN'ler HEAD'i yanlış cevaplar
                    st.session_state["_script_msg"] = (
                        "warn",
                        f"⚠ URL bir görsel değil gibi (Content-Type: {ctype.split(';')[0]}). "
                        f"Eğer doğru URL ise sağ tıkla → 'Resim adresini kopyala' deneyebilirsin.",
                    )
            except urllib.error.HTTPError as he:
                # 404, 403, 405 (HEAD not allowed) — soft warn, izin ver
                if he.code in (404, 410):
                    st.session_state["_script_msg"] = (
                        "err",
                        f"URL erişilemez ({he.code} {he.reason}). "
                        f"URL: {url[:80]}",
                    )
                    return
            except Exception:
                # Timeout, DNS, vs — paste'e izin ver, submit'te yine de denenir
                pass

        for a in st.session_state.get("script_assets", []):
            if a.get("id") != asset_id:
                continue
            a["selected_image"] = {
                "source": "manual",
                "thumb_url": url,
                "full_url": url,
                "title": "(manuel URL)",
                "license": "kullanıcı belirtmedi",
                "attribution": "manuel",
                "page_url": url,
                "width": 0,
                "height": 0,
            }
            st.session_state[widget_key] = ""
            # Eğer önceden warn/err mesajı set edilmediyse OK mesajı koy
            if "_script_msg" not in st.session_state or st.session_state.get("_script_msg") is None:
                st.session_state["_script_msg"] = ("ok", f"URL seçildi: {url[:80]}")
            _persist_draft()
            break

    # Önceki run'da bir mesaj set edildiyse göster (callback render'dan önce çalışır)
    _msg = st.session_state.pop("_script_msg", None)

    # --- Tekli senaryo akışı: aç/kapa ---
    _show_single = st.toggle(
        "📝 Tekli senaryo oluştur",
        value=st.session_state.get("show_single_flow", False),
        key="show_single_flow",
        help="Tek script yapıştırıp video üret. Genelde toplu (Drive) "
             "kullanılıyor — varsayılan kapalı. Aç → 3-step akış görünür.",
    )
    if _show_single:
        st.caption(
            "⏸ Tekli mod açık — sayfa otomatik yenilenmiyor, rahatça yaz. "
            "Videolarını canlı takip etmek istersen bu anahtarı kapat."
        )
        # ===== 3-Step pipeline state =====
        ui_step = int(st.session_state.get("ui_step", 1))

        # Step indicator stripe (her zaman görünür)
        def _step_pill(num: int, label: str, active: bool, done: bool) -> str:
            if done:
                bg, fg, icon = "#10B981", "#FFFFFF", "✓"
            elif active:
                bg, fg, icon = "#6366F1", "#FFFFFF", str(num)
            else:
                bg, fg, icon = "rgba(156,163,175,0.25)", "#9CA3AF", str(num)
            return (
                f'<span style="display:inline-flex; align-items:center; gap:6px; '
                f'padding:4px 10px; background:{bg}; color:{fg}; border-radius:14px; '
                f'font-size:0.8rem; font-weight:600;">'
                f'<span style="background:rgba(255,255,255,0.2); width:18px; height:18px; '
                f'border-radius:9px; display:inline-flex; align-items:center; justify-content:center; '
                f'font-size:0.72rem;">{icon}</span>{label}</span>'
            )

        st.markdown("&nbsp;", unsafe_allow_html=True)
        st.markdown(
            '<div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">'
            + _step_pill(1, "Senaryo", ui_step == 1, ui_step > 1)
            + _step_pill(2, "Görseller", ui_step == 2, ui_step > 2)
            + _step_pill(3, "Cinematic", ui_step == 3, False)
            + '</div>',
            unsafe_allow_html=True,
        )

        # ===== STEP 1: Senaryonu Hazırla =====
        if ui_step == 1:
            st.markdown(
                '<div style="font-size:1.05rem; font-weight:700; margin-bottom:0.2rem;">'
                '1️⃣ Senaryo + Learning Objectives</div>'
                '<div style="font-size:0.8rem; opacity:0.7; margin-bottom:0.5rem;">'
                'Hazır <b>senaryonu</b> ve (varsa) <b>Learning Objectives</b>\'ini yapıştır — '
                'tıpkı Drive toplu akışındaki <code>senaryo.docx</code> + '
                '<code>senaryo_lo.docx</code> gibi. Custom prompt ve 4 sabit guide '
                'otomatik eklenir.</div>',
                unsafe_allow_html=True,
            )

            st.text_area(
                "Senaryo (hazır script)",
                height=300,
                placeholder="Bitmiş senaryonu buraya yapıştır...",
                key="script_draft",
                on_change=_cb_text_changed,
            )
            st.text_area(
                "🎯 Learning Objectives (opsiyonel)",
                height=110,
                placeholder=(
                    "a) ...\nb) ...\nc) ...\n\n"
                    "Boş bırakılabilir. Doluysa <Title>_LearningObjectives.txt "
                    "olarak notebook'a source eklenir (toplu akıştaki _lo gibi)."
                ),
                key="script_lo",
                on_change=_persist_draft,
            )

            # AI script üretimi/refine kaldırıldı → tek "devam" butonu.
            # Senaryo (+ opsiyonel LO) hazır, sonraki adım görseller.
            st.button(
                "Devam → Görseller",
                type="primary",
                use_container_width=True,
                on_click=_cb_use_output,
                key="btn_use_output",
            )

        else:
            # ui_step > 1 → Step 1 collapsed summary
            _draft_preview = (st.session_state.get("script_draft") or "").strip()
            _preview = (_draft_preview[:120] + "…") if len(_draft_preview) > 120 else _draft_preview
            cs_sum = st.columns([5, 1])
            with cs_sum[0]:
                st.markdown(
                    f'<div style="padding:10px 14px; background:rgba(16,185,129,0.08); '
                    f'border-left:3px solid #10B981; border-radius:6px; margin-bottom:8px;">'
                    f'<div style="font-size:0.85rem; font-weight:600;">✓ 1. Senaryo hazır</div>'
                    f'<div style="font-size:0.78rem; opacity:0.75; margin-top:4px; font-style:italic;">'
                    f'"{_preview}"</div></div>',
                    unsafe_allow_html=True,
                )
            with cs_sum[1]:
                st.button(
                    "↩ Düzenle",
                    on_click=_cb_back_to_step,
                    args=(1,),
                    use_container_width=True,
                    key="btn_back_step1",
                )

        # ===== AI Editor (KALDIRILDI) =====
        # Kullanıcı tercihi (2026-06-10): hazır senaryo + LO modeli — AI feedback/
        # refine döngüsü kullanılmıyor. Blok `if False` ile devre dışı (ölü kod;
        # ileride tamamen silinebilir). _cb_regenerate/_cb_revert/_cb_reset_history
        # ve regenerate_script de artık çağrılmıyor.
        if False and ui_step == 1:
            iter_count = len(st.session_state["script_iterations"])
            # Sadece initial-generation dışı gerçek iterasyon varsa label'da göster
            real_iters = sum(
                1 for it in st.session_state["script_iterations"]
                if it.get("feedback", "") and not it["feedback"].startswith("(initial")
            )
            expander_label = "✨ AI ile rafine et (feedback ver)"
            if real_iters:
                expander_label += f" — {real_iters} iterasyon"
            # Keşfedilebilirlik: script varsa ve henüz hiç rafine edilmediyse
            # AÇIK başlat (kullanıcılar gömülü expander'ı fark etmiyordu);
            # kullanmaya başlayınca (real_iters>0) kapalı başlar, yer kaplamaz.
            _refine_open = bool(
                (st.session_state.get("script_draft", "") or "").strip()
                and not real_iters
            )
            with st.expander(expander_label, expanded=_refine_open):
                st.caption(
                    "Script'i beğendinse atla. Beğenmediysen aşağıya 'ne değişsin' "
                    "yaz, üstteki model'le AI yeniden üretsin."
                )
                st.text_area(
                    "Feedback / değişiklik notları",
                    key="script_feedback",
                    height=80,
                    placeholder=(
                        "örn. 'biraz daha kısa tut, hook'u güçlendir, sonunda kicker ekle'"
                    ),
                )
                cs = st.columns([2, 1])
                with cs[0]:
                    st.button(
                        "🔄 AI ile yeniden üret",
                        type="primary",
                        use_container_width=True,
                        on_click=_cb_regenerate,
                        key="btn_regenerate",
                    )
                with cs[1]:
                    if iter_count:
                        st.button(
                            "🔁 Sıfırla",
                            use_container_width=True,
                            help="İterasyon geçmişini temizle",
                            on_click=_cb_reset_history,
                            key="btn_reset_history",
                        )

                # İterasyon geçmişi — geri dönme imkanı
                if iter_count:
                    st.markdown("**Geçmiş versiyonlar**")
                    for i, it in enumerate(reversed(st.session_state["script_iterations"])):
                        actual_i = iter_count - 1 - i
                        short_fb = (it["feedback"][:60] + "…") if len(it["feedback"]) > 60 else it["feedback"]
                        used_model = it.get("model", "?")
                        # Model id'sinden kısa label türet (örn. qwen3-next-80b)
                        model_short = used_model.split("/")[-1].replace(":free", "")
                        with st.container(border=True):
                            cs2 = st.columns([5, 1])
                            with cs2[0]:
                                st.caption(f"v{actual_i + 1} · `{model_short}` — {short_fb}")
                                with st.expander("Görüntüle", expanded=False):
                                    st.text(it["script"])
                            with cs2[1]:
                                st.button(
                                    "↶ Geri dön",
                                    key=f"revert_{actual_i}",
                                    use_container_width=True,
                                    on_click=_cb_revert,
                                    args=(actual_i,),
                                )

        # ===== STEP 2: Görseller (Phase B/C/D) =====
        # ui_step >= 2 olunca açılır. ui_step > 2 ise collapsed summary.
        if ui_step >= 2 and LLM_ENABLED:
            assets = st.session_state.get("script_assets", []) or []
            n_assets = len(assets)
            n_selected = sum(1 for a in assets if a.get("selected_image"))

            if ui_step == 2:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                st.markdown(
                    '<div style="font-size:1.05rem; font-weight:700; margin-bottom:0.2rem;">'
                    '2️⃣ Görseller</div>'
                    '<div style="font-size:0.8rem; opacity:0.7; margin-bottom:0.5rem;">'
                    'LLM script\'ten gerçek-durum nesneleri çıkarır → her biri için '
                    'görsel ara / AI ile üret / manuel URL. Beğendiklerinden seç. '
                    'İstemiyorsan <b>Bu adımı atla</b> butonuyla geç.</div>',
                    unsafe_allow_html=True,
                )

                # Gemini model selector — asset extraction için (default: 3.5 Flash)
                _s2_model_ids = [m[0] for m in GEMINI_MODELS]
                _s2_model_labels = {m[0]: m[1] for m in GEMINI_MODELS}
                if "script_model" not in st.session_state or st.session_state["script_model"] not in _s2_model_ids:
                    st.session_state["script_model"] = "flash-3.5"
                st.selectbox(
                    "AI Model (asset extraction için)",
                    options=_s2_model_ids,
                    format_func=lambda mid: _s2_model_labels.get(mid, mid).split(" — ")[0],
                    key="script_model",
                    help="Gemini 3.5 Flash (yeni) önerilir. Pro daha kaliteli ama "
                         "uzun script'lerde yavaş olabilir.",
                )
                # Pro seçildiyse uyar
                if st.session_state.get("script_model") == "pro":
                    st.caption(
                        "⏳ **Pro model uzun input'larda yavaştır** — bu Step için "
                        "Flash genelde yeterli, sadece çok kaliteli çıktı gerekiyorsa Pro tut."
                    )

                # Step 2 action row: extract + skip + next
                top_cs = st.columns([1.6, 1, 1, 1])
                with top_cs[0]:
                    extract_label = ("🔄 Yeniden çıkar (listeyi sıfırlar)"
                                     if n_assets else "🖼 Görselleri çıkar")
                    st.button(
                        extract_label,
                        type="primary",
                        use_container_width=True,
                        on_click=_cb_extract_assets,
                        key="btn_extract_assets",
                        help="Aktif senaryoyu LLM'e gönderir, görsel listesi çıkarır."
                    )
                with top_cs[1]:
                    if n_assets:
                        st.button(
                            "➕ Manuel ekle",
                            use_container_width=True,
                            on_click=_cb_add_asset,
                            key="btn_add_asset_top",
                        )
                with top_cs[2]:
                    st.button(
                        "⏭ Bu adımı atla",
                        use_container_width=True,
                        on_click=_cb_skip_step2,
                        key="btn_skip_step2",
                        help="Görselsiz devam et — Cinematic sadece script'i kullanır."
                    )
                with top_cs[3]:
                    _can_next = (n_selected > 0) or (n_assets == 0)  # ya seçim ya hiç asset yok
                    if st.button(
                        "Step 3 ▶",
                        type="primary" if n_selected > 0 else "secondary",
                        use_container_width=True,
                        key="btn_next_step3",
                        disabled=not n_selected and bool(n_assets),
                        help=("En az 1 görsel seç ya da atla." if n_assets and not n_selected
                              else "Cinematic adımına geç."),
                    ):
                        st.session_state["ui_step"] = 3
                        st.rerun()

                # Asset extractor prompt override (gizli expander)
                with st.expander("⚙ Gelişmiş: Asset extractor prompt'unu düzenle", expanded=False):
                    st.caption(
                        "Default: kodda sabit prompt (gerçek-durum nesneler için). "
                        "Buradan değiştirirsen, sadece bu oturumda 'Görselleri çıkar' "
                        "çağrılarında kullanılır."
                    )
                    st.text_area(
                        "Asset extractor system prompt",
                        key="asset_extractor_prompt_override",
                        value=st.session_state.get(
                            "asset_extractor_prompt_override", ""
                        ),
                        height=180,
                        placeholder="Boş bırak = default prompt kullanılır",
                        help="Boş bırakırsan kodda tanımlı ASSET_EXTRACTOR_SYSTEM kullanılır.",
                    )

                st.markdown("&nbsp;", unsafe_allow_html=True)
            else:
                # ui_step == 3 → Step 2 collapsed summary
                _summary = f"{n_assets} öneri · {n_selected} seçili" if n_assets else "atlandı"
                cs_sum2 = st.columns([5, 1])
                with cs_sum2[0]:
                    st.markdown(
                        f'<div style="padding:10px 14px; background:rgba(16,185,129,0.08); '
                        f'border-left:3px solid #10B981; border-radius:6px; margin-bottom:8px;">'
                        f'<div style="font-size:0.85rem; font-weight:600;">✓ 2. Görseller — {_summary}</div></div>',
                        unsafe_allow_html=True,
                    )
                with cs_sum2[1]:
                    st.button(
                        "↩ Düzenle",
                        on_click=_cb_back_to_step,
                        args=(2,),
                        use_container_width=True,
                        key="btn_back_step2",
                    )

        # ===== Phase B detail UI: sadece ui_step == 2 iken render =====
        # Asset listesinin ayrıntılı per-item UI'sı (eski expander içeriği,
        # şimdi açık layout). Üst butonlar (extract/skip/next) yukarıdaki
        # wrapper'da Step 2 header'ında yer alıyor.
        if ui_step == 2 and LLM_ENABLED:
            assets = st.session_state.get("script_assets", []) or []
            n_assets = len(assets)
            if n_assets:
                # "Tümünü sil" küçük buton (top row'dan ayrı)
                del_cs = st.columns([5, 1])
                with del_cs[1]:
                    st.button(
                        "🗑 Tümünü sil",
                        use_container_width=True,
                        on_click=_cb_clear_assets,
                        key="btn_clear_assets",
                    )

                if True:
                    st.markdown("&nbsp;", unsafe_allow_html=True)
                    STYLE_OPTS = ["photo", "illustration", "diagram", "archive"]
                    STYLE_ICON = {
                        "photo": "📷", "illustration": "🎨",
                        "diagram": "📊", "archive": "🗄"
                    }
                    for idx, asset in enumerate(assets):
                        aid = asset.get("id") or uuid.uuid4().hex[:8]
                        asset["id"] = aid  # ensure id always present
                        style_now = asset.get("style", "photo")
                        if style_now not in STYLE_OPTS:
                            style_now = "photo"
                        icon = STYLE_ICON.get(style_now, "📷")

                        with st.container(border=True):
                            head = st.columns([5, 1.2, 0.6])
                            with head[0]:
                                pos = asset.get("position", "")
                                st.markdown(
                                    f'<div style="font-size:0.85rem; font-weight:600; '
                                    f'margin-bottom:2px;">'
                                    f'{icon} <span style="opacity:0.55;">#{idx+1}</span> · '
                                    f'<span style="opacity:0.7; font-weight:500; '
                                    f'font-style:italic;">{pos[:90] if pos else "(konum belirtilmedi)"}</span>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )
                            with head[1]:
                                st.selectbox(
                                    "Stil",
                                    options=STYLE_OPTS,
                                    index=STYLE_OPTS.index(style_now),
                                    label_visibility="collapsed",
                                    key=f"asset_style_{aid}",
                                    on_change=_cb_asset_edit,
                                    args=(aid, "style", f"asset_style_{aid}"),
                                )
                            with head[2]:
                                st.button(
                                    "🗑",
                                    key=f"asset_del_{aid}",
                                    use_container_width=True,
                                    on_click=_cb_delete_asset,
                                    args=(aid,),
                                    help="Bu öneriyi sil",
                                )
                            st.text_input(
                                "Açıklama (TR)",
                                value=asset.get("description", ""),
                                key=f"asset_desc_{aid}",
                                on_change=_cb_asset_edit,
                                args=(aid, "description", f"asset_desc_{aid}"),
                            )
                            st.text_input(
                                "Search query (EN, image API'leri için)",
                                value=asset.get("query", ""),
                                key=f"asset_query_{aid}",
                                on_change=_cb_asset_edit,
                                args=(aid, "query", f"asset_query_{aid}"),
                                help="3-6 İngilizce keyword. Phase C'de Wikimedia/Openverse search'e gönderilecek.",
                            )

                            # ===== Phase C: Image search per asset =====
                            selected = asset.get("selected_image")
                            candidates = asset.get("candidates") or []

                            if selected:
                                # Seçili görsel — sade görünüm + değiştirme imkanı
                                st.markdown("---")
                                sc = st.columns([1, 2.5, 1])
                                with sc[0]:
                                    try:
                                        st.image(selected.get("thumb_url"),
                                                 use_container_width=True)
                                    except Exception:
                                        st.caption("(thumbnail yüklenemedi)")
                                with sc[1]:
                                    src_emoji = {
                                        "wikimedia": "🌐", "openverse": "🌍",
                                        "pixabay": "🎯", "pexels": "📸",
                                        "pollinations": "🤖", "manual": "✏",
                                    }.get(selected.get("source", ""), "🖼")
                                    st.markdown(
                                        f'<div style="font-size:0.85rem;">'
                                        f'<b>✅ Seçili</b> · {src_emoji} {selected.get("source","?")}<br>'
                                        f'<span style="opacity:0.7; font-size:0.78rem;">'
                                        f'📜 Lisans: <b>{selected.get("license","?")}</b><br>'
                                        f'👤 {selected.get("attribution","")[:60]}</span>'
                                        f'</div>',
                                        unsafe_allow_html=True,
                                    )
                                    if selected.get("page_url"):
                                        st.markdown(
                                            f'<a href="{selected["page_url"]}" target="_blank" '
                                            f'style="font-size:0.75rem; opacity:0.7;">↗ kaynak sayfa</a>',
                                            unsafe_allow_html=True,
                                        )
                                with sc[2]:
                                    st.button(
                                        "✕ Kaldır",
                                        key=f"asset_unsel_{aid}",
                                        use_container_width=True,
                                        on_click=_cb_clear_selection,
                                        args=(aid,),
                                        help="Seçimi kaldır, başka görsel seç",
                                    )
                            elif not candidates:
                                # Henüz arama yapılmamış — search + generate butonları
                                st.markdown("&nbsp;", unsafe_allow_html=True)
                                search_cs = st.columns([2, 2, 1.5])
                                with search_cs[0]:
                                    st.button(
                                        "🔍 Görsel ara",
                                        key=f"asset_search_{aid}",
                                        on_click=_cb_search_images,
                                        args=(aid,),
                                        use_container_width=True,
                                        help="Aktif kaynaklarda bu query ile ara",
                                    )
                                with search_cs[1]:
                                    st.button(
                                        "🎨 AI ile üret",
                                        key=f"asset_gen_{aid}",
                                        on_click=_cb_generate_images,
                                        args=(aid,),
                                        use_container_width=True,
                                        help="Gemini ile 4 varyant üret (API key kullanır)",
                                    )
                                with search_cs[2]:
                                    # Gemini görsel model selector
                                    _pmodel_ids = [m[0] for m in GEMINI_IMAGE_MODELS]
                                    _pmodel_lbls = {m[0]: m[1] for m in GEMINI_IMAGE_MODELS}
                                    _pmkey = f"asset_genmodel_{aid}"
                                    if _pmkey not in st.session_state or st.session_state[_pmkey] not in _pmodel_ids:
                                        st.session_state[_pmkey] = GEMINI_IMAGE_DEFAULT
                                    st.selectbox(
                                        "Görsel modeli",
                                        options=_pmodel_ids,
                                        format_func=lambda m: _pmodel_lbls.get(m, m).split(" — ")[0],
                                        key=_pmkey,
                                        label_visibility="collapsed",
                                        help="Gemini görsel üretim modeli (üret butonunda kullanılır)",
                                    )

                                # Shutterstock (lisanslı stok) — ayrı buton (paralı)
                                if SHUTTERSTOCK_ENABLED:
                                    _ssub = shutterstock_subscription()
                                    _sleft = _ssub.get("downloads_left", "?") if _ssub else "?"
                                    st.button(
                                        f"🛒 Shutterstock'ta ara · {_sleft} indirme kaldı",
                                        key=f"asset_sstk_{aid}",
                                        on_click=_cb_search_shutterstock,
                                        args=(aid,),
                                        use_container_width=True,
                                        help="Lisanslı stok. Sonuçlar filigranlı önizleme; "
                                             "seçtiğin görsel video üretilirken lisanslanır (1 indirme).",
                                    )

                                # Aktif kaynak listesi
                                _active = ["Wikimedia", "Openverse"]
                                if PIXABAY_API_KEY:
                                    _active.append("Pixabay")
                                if PEXELS_API_KEY:
                                    _active.append("Pexels")
                                if SHUTTERSTOCK_ENABLED:
                                    _active.append("Shutterstock(lisanslı)")
                                st.caption(
                                    f"Arama kaynakları: **{' · '.join(_active)}** "
                                    f"&nbsp;·&nbsp; AI üretim: **Gemini**"
                                )

                                # Manuel URL paste — kullanıcı kendisi URL bulup yapıştırabilir
                                with st.expander("✏ Manuel URL yapıştır", expanded=False):
                                    _q_for_external = (asset.get("query") or "").strip()
                                    if _q_for_external:
                                        _g_url = "https://www.google.com/search?tbm=isch&q=" + urllib.parse.quote(_q_for_external)
                                        _ddg_url = "https://duckduckgo.com/?iax=images&ia=images&q=" + urllib.parse.quote(_q_for_external)
                                        st.markdown(
                                            f'<div style="font-size:0.78rem; opacity:0.8; margin-bottom:6px;">'
                                            f'Bulamadıysan dış arama: '
                                            f'<a href="{_g_url}" target="_blank">🔗 Google Images</a> · '
                                            f'<a href="{_ddg_url}" target="_blank">🔗 DuckDuckGo</a> '
                                            f'→ uygun görseli bul → <b>resme sağ tıkla → "Resim adresini kopyala"</b>'
                                            f'</div>',
                                            unsafe_allow_html=True,
                                        )
                                    _manual_key = f"asset_manual_url_{aid}"
                                    st.text_input(
                                        "Görsel URL'i (https://... ile başlamalı)",
                                        key=_manual_key,
                                        placeholder="https://example.com/image.jpg",
                                    )
                                    st.button(
                                        "Bu URL'i kullan",
                                        key=f"asset_manual_use_{aid}",
                                        on_click=_cb_use_manual_url,
                                        args=(aid, _manual_key),
                                    )
                            else:
                                # Aday görseller var, henüz seçim yok
                                st.markdown("---")
                                n_cands = len(candidates)
                                # Tipini belirt (gerçek arama sonuçları mı, AI üretim mi)
                                sources_in_cands = {c.get("source", "?") for c in candidates}
                                is_all_ai = sources_in_cands <= {"pollinations", "gemini"}
                                label_kind = "AI varyant" if is_all_ai else "aday görsel"
                                head_cs = st.columns([3, 1.3, 1.3])
                                with head_cs[0]:
                                    st.markdown(
                                        f'<small><b>{n_cands} {label_kind}</b> · '
                                        f'birini seç ↓</small>',
                                        unsafe_allow_html=True,
                                    )
                                with head_cs[1]:
                                    st.button(
                                        "🔄 Yeniden ara",
                                        key=f"asset_research_{aid}",
                                        on_click=_cb_search_images,
                                        args=(aid,),
                                        use_container_width=True,
                                        help="Aramayı yenile",
                                    )
                                with head_cs[2]:
                                    st.button(
                                        "🎨 AI üret",
                                        key=f"asset_gen2_{aid}",
                                        on_click=_cb_generate_images,
                                        args=(aid,),
                                        use_container_width=True,
                                        help="Beğenmediysen Gemini ile 4 varyant üret",
                                    )

                                # Thumbnail grid — 4 sütun
                                cands_to_show = candidates[:8]
                                n_cols = 4
                                for row_start in range(0, len(cands_to_show), n_cols):
                                    row = cands_to_show[row_start:row_start + n_cols]
                                    grid_cs = st.columns(n_cols)
                                    for j, cand in enumerate(row):
                                        cand_idx = row_start + j
                                        with grid_cs[j]:
                                            try:
                                                # Gemini = local_path (disk), arama = thumb_url (http)
                                                st.image(
                                                    cand.get("local_path") or cand.get("thumb_url"),
                                                    use_container_width=True,
                                                )
                                            except Exception:
                                                st.caption("⚠ yüklenemedi")
                                            src_short = {
                                                "wikimedia": "🌐 wm",
                                                "openverse": "🌍 ov",
                                                "pixabay": "🎯 pix",
                                                "pexels": "📸 pex",
                                                "pollinations": "🤖 AI",
                                                "gemini": "✨ Gemini",
                                                "shutterstock": "🛒 SS",
                                                "manual": "✏ man",
                                            }.get(cand.get("source", ""), "?")
                                            license_short = (cand.get("license") or "")[:14]
                                            st.markdown(
                                                f'<div style="font-size:0.7rem; opacity:0.7; '
                                                f'text-align:center; line-height:1.2; margin-top:-4px;">'
                                                f'{src_short} · {license_short}</div>',
                                                unsafe_allow_html=True,
                                            )
                                            st.button(
                                                "Seç",
                                                key=f"asset_pick_{aid}_{cand_idx}",
                                                on_click=_cb_select_image,
                                                args=(aid, cand_idx),
                                                use_container_width=True,
                                            )

                                # Manuel URL paste — adaylar varken de erişilebilir
                                with st.expander("✏ Beğenmediysen: manuel URL yapıştır", expanded=False):
                                    _q_for_external2 = (asset.get("query") or "").strip()
                                    if _q_for_external2:
                                        _g_url2 = "https://www.google.com/search?tbm=isch&q=" + urllib.parse.quote(_q_for_external2)
                                        st.markdown(
                                            f'<a href="{_g_url2}" target="_blank" '
                                            f'style="font-size:0.78rem;">🔗 Google Images\'te aç</a>',
                                            unsafe_allow_html=True,
                                        )
                                    _manual_key2 = f"asset_manual_url2_{aid}"
                                    st.text_input(
                                        "Görsel URL'i",
                                        key=_manual_key2,
                                        placeholder="https://example.com/image.jpg",
                                    )
                                    st.button(
                                        "Bu URL'i kullan",
                                        key=f"asset_manual_use2_{aid}",
                                        on_click=_cb_use_manual_url,
                                        args=(aid, _manual_key2),
                                    )

        # ===== STEP 3: Cinematic Video (Custom Prompt + Submit) =====
        # Source listesi her seçili görselin description+position bilgisiyle birlikte
        # enumere edilir → NotebookLM hangi görseli script'in hangi anında göstereceğini bilir.
        _assets_now = st.session_state.get("script_assets", []) or []
        _selected_assets = [a for a in _assets_now if a.get("selected_image")]
        # Başlık: kullanıcı elle girdiyse onu kullan; boşsa ilk satırdan türet.
        # (İlk satır markdown/giriş cümlesi olunca bozuk başlık Azure blob adına
        # kadar gidiyordu — elle alan bunu çözer.)
        _title_now = (
            (st.session_state.get("script_title_override", "") or "").strip()
            or derive_title(st.session_state.get("script_draft", ""))
            or "Untitled"
        )
        _has_lo = bool((st.session_state.get("script_lo", "") or "").strip())
        _src_listing, _src_names = build_source_listing(
            _title_now, _selected_assets, has_learning_objectives=_has_lo)
        _total_sources = len(_src_names)

        # Eğer custom prompt boşsa ve script var ve "user edited" değilse, otomatik doldur
        if (
            st.session_state.get("script_draft", "").strip()
            and not st.session_state.get("script_custom_prompt", "").strip()
            and not st.session_state.get("script_custom_prompt_user_edited", False)
        ):
            st.session_state["script_custom_prompt"] = render_custom_prompt(
                DEFAULT_CUSTOM_PROMPT_TEMPLATE, _title_now, _selected_assets,
                has_learning_objectives=_has_lo
            )

        if ui_step >= 3:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            st.markdown(
                '<div style="font-size:1.05rem; font-weight:700; margin-bottom:0.2rem;">'
                '3️⃣ Cinematic Video</div>'
                '<div style="font-size:0.8rem; opacity:0.7; margin-bottom:0.5rem;">'
                'Custom Prompt NotebookLM\'in Customize → Custom prompt alanına '
                'gidecek. Source listesi otomatik enumere edildi. Beğenmediysen '
                'aşağıdaki text alanı edit edilebilir. Hazır olunca '
                '<b>🚀 Video üret</b>.</div>',
                unsafe_allow_html=True,
            )

            n_images = max(0, _total_sources - 1)
            # Paket önizleme — neler upload edilecek
            st.markdown(
                f'<div style="font-size:0.85rem; padding:8px 12px; '
                f'background:rgba(99,102,241,0.06); border-radius:6px; margin-bottom:8px;">'
                f'<b>📦 NotebookLM\'e gidecek source\'lar ({_total_sources}):</b><br>'
                f'<span style="font-size:0.78rem;">'
                + "<br>".join(f"• <code>{n}</code>" for n in _src_names) +
                f'</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            with st.expander("📋 Custom Prompt (edit edilebilir)", expanded=False):
                st.caption(
                    "Bu metin NotebookLM'de Cinematic Customize → Custom prompt "
                    "alanına yapıştırılır. Source listesinin satırlarındaki 'show "
                    "when narration says: \"...\"' kısımları görsel-anlatım eşlemesini "
                    "yönlendirir."
                )
                st.info(
                    "🔒 NotebookLM source listesi (her job otomatik):\n"
                    "1. `<Title>_Script.txt` — Senaryon\n"
                    "2. `<Title>_LearningObjectives.txt` — _lo.docx/.txt/.md companion (varsa)\n"
                    "3. Narrative & Text-Free Execution Guide\n"
                    "4. Historical Accuracy & Identity Protocol\n"
                    "5. Fully Realistic Style\n"
                    "6. `_custom_prompt.txt` — Bu doküman (Role/Task/Constraints)\n"
                    "7..N. Görseller\n\n"
                    "Custom prompt **hem source olarak hem Cinematic Customize alanına** "
                    "gider — daha güçlü prime. Drive'da `senaryo1.docx` + `senaryo1_lo.docx` "
                    "var ise ikisi de yüklenir.",
                    icon="ℹ️",
                )
                with st.expander("👁 4 sabit guide'ı gör (read-only)"):
                    for filename, text in EXECUTION_GUIDE_FILES:
                        st.markdown(f"**📄 {filename}**")
                        st.code(text, language=None)

                cs_p = st.columns([1.2, 4])
                with cs_p[0]:
                    st.button(
                        "🔄 Template'den doldur",
                        key="btn_autofill_prompt",
                        on_click=_cb_autofill_prompt,
                        use_container_width=True,
                        help="Default template + güncel source listesiyle yeniden doldur (manuel düzenlemen silinir)",
                    )
                with cs_p[1]:
                    edited_flag = st.session_state.get("script_custom_prompt_user_edited", False)
                    st.caption(
                        ("✏ Manuel düzenledin — auto-refresh kapalı." if edited_flag
                         else "📝 Auto-render: source eklediğinde/sildiğinde template güncellenir.")
                    )

                st.text_area(
                    "Custom Prompt (NotebookLM'e yapışacak)",
                    key="script_custom_prompt",
                    height=300,
                    on_change=_cb_prompt_edited,
                    help="Kendi role/constraint metnini yazabilirsin. "
                         "{{SOURCES_LIST}} placeholder'ı template'de otomatik doldurulur.",
                )

        # ===== Submit button (ui_step >= 3) =====
        if ui_step >= 3:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            # Başlık + versiyon sayısı (toplu akışla eşitlik)
            cs_t = st.columns([3, 1.2])
            with cs_t[0]:
                st.text_input(
                    "🏷 Video başlığı",
                    key="script_title_override",
                    placeholder=f"Boş bırakılırsa ilk satırdan: {_title_now[:60]}",
                    help="Azure dosya adı ve NotebookLM source isimleri bu başlıktan türetilir.",
                )
            with cs_t[1]:
                st.number_input(
                    "🎞 Kaç versiyon?",
                    min_value=1, max_value=4, value=1, step=1,
                    key="script_n_versions",
                    help="Aynı senaryodan kaç Cinematic versiyon üretilsin (v1..vN).",
                )
            cs = st.columns([2, 1.4, 1.4])
            with cs[0]:
                st.markdown(
                    f'<div style="font-size:0.78rem; opacity:0.65; margin-top:8px;">'
                    f'Gönderen: <b>{_user_name()}</b> · Paket: '
                    f'<b>{_total_sources}</b> source</div>',
                    unsafe_allow_html=True,
                )
            with cs[1]:
                st.button(
                    "↩ Step 2'ye dön",
                    on_click=_cb_back_to_step,
                    args=(2,),
                    use_container_width=True,
                    key="btn_back_step2_from3",
                )
            with cs[2]:
                st.button(
                    "🚀 Video üret",
                    type="primary",
                    use_container_width=True,
                    key="submit_video",
                    on_click=_cb_submit,
                )

    # Mesajları göster (en son)
    if _msg:
        kind, text_msg = _msg
        if kind == "ok":
            st.toast(text_msg, icon="✨")
        elif kind == "warn":
            st.warning(text_msg)
        else:
            st.error(text_msg)

    # Submit niyeti varsa şimdi işle (widget'lardan sonra, callback dışında)
    if st.session_state.get("_pending_submit"):
        st.session_state["_pending_submit"] = False
        text_submit = st.session_state.get("script_draft", "").strip()
        lo_at_submit = (st.session_state.get("script_lo", "") or "").strip()
        iterations_at_submit = list(st.session_state.get("script_iterations", []))
        assets_at_submit = list(st.session_state.get("script_assets", []))
        custom_prompt_submit = (
            st.session_state.get("script_custom_prompt", "") or ""
        ).strip()
        # Audit: original_script = ilk iterasyondaki "script" (ilk yapıştırılan
        # versiyon). Hiç iterasyon yoksa = final script.
        if iterations_at_submit:
            original_at_submit = iterations_at_submit[0].get("script", text_submit)
        else:
            original_at_submit = text_submit
        if text_submit:
            # Başlık: elle girilen öncelikli; boşsa ilk satırdan türet
            title = (
                (st.session_state.get("script_title_override", "") or "").strip()
                or derive_title(text_submit)
            )
            # Versiyon sayısı (1..4) — toplu akışla aynı mantık: version=1..N,
            # hafif artan created_at → azure _v1.._vN sıralaması tutarlı.
            # N=1 ise version=0 (eski davranış, suffix'siz).
            try:
                n_ver = int(st.session_state.get("script_n_versions", 1) or 1)
            except (TypeError, ValueError):
                n_ver = 1
            n_ver = max(1, min(4, n_ver))
            _base_ts = time.time()
            jobs_all = load_jobs()
            for _v in range(1, n_ver + 1):
                jobs_all.append(Job(
                    id=uuid.uuid4().hex[:12],
                    title=title,
                    text=text_submit,
                    submitted_by=_user_name(),
                    original_script=original_at_submit,
                    iterations=iterations_at_submit,
                    assets=assets_at_submit,
                    custom_prompt=custom_prompt_submit,
                    learning_objectives=lo_at_submit,
                    version=(_v if n_ver > 1 else 0),
                    created_at=_base_ts + (_v - 1) * 0.001,
                    # Env URL param'dan (default prod). Dispatcher buna uygun
                    # profili seçer.
                    environment=current_env(),
                ))
            save_jobs(jobs_all)
            # Disk'teki yarım draft'ı temizle (artık jobs.json'da audit'le birlikte var)
            clear_script_draft(_user_name())
            # Submit sonrası alanları temizle — widget değerleri callback dışında
            # ama yeni run başlamadan önce session_state'i temizleyemeyiz çünkü
            # widget'lar zaten render edildi. Bu yüzden bayrak kullan: bir
            # sonraki run'ın başında temizle.
            st.session_state["_clear_after_submit"] = True
            _ver_note = f" ({n_ver} versiyon)" if n_ver > 1 else ""
            st.toast(f"Kuyruğa eklendi{_ver_note}! Birkaç dakika içinde tetiklenecek.", icon="🚀")
            time.sleep(0.5)
            st.rerun()


    # Drive Toplu (expander içinde, normal akışı engellemesin)
    st.markdown("&nbsp;", unsafe_allow_html=True)
    with st.expander("🗂️  Drive klasöründen toplu video üret (40+ docx'i otomatik işle)"):
        render_bulk_drive_section(key_prefix="usr")

    # Tüm videolar — herkesin job'ları tek listede (paylaşımlı görünüm).
    st.markdown("&nbsp;", unsafe_allow_html=True)
    user = _user_name()
    # Hem display_name hem username ile case-insensitive eşleşme — "sen" marker'ı için.
    auth = current_user() or {}
    user_lower = user.lower()
    username_lower = auth.get("username", "").lower()

    def _belongs_to_me(j: Job) -> bool:
        sb = (j.submitted_by or "").strip().lower()
        return sb == user_lower or sb == username_lower

    # Bu env'in TÜM job'ları (jobs zaten env'e göre filtrelenmiş — render_user_view
    # başında). Herkes hepsini görür; submit eden etiketle gösterilir.
    #
    # Dedup: Aynı title için "aktif/başarılı" kayıt (done/generating/running/
    # queued/submitted) varsa, o title'ın stopped/failed kayıtlarını GİZLE.
    # Sebep: retry/re-import yeni Job kaydı açıyor → bir script hem stopped hem
    # done görünüyordu (2 ayrı kart). Done'lar gerçek video → hepsi kalır;
    # ölü stopped/failed çiftler gizlenir. Gizliyse görünmezler ama veride durur.
    _SUPERSEDING = {"done", "generating", "running", "queued", "submitted"}
    _titles_with_active = {
        (j.title or "").strip()
        for j in jobs
        if j.status in _SUPERSEDING
    }

    def _is_superseded(j: Job) -> bool:
        return (
            j.status in ("stopped", "failed")
            and (j.title or "").strip() in _titles_with_active
        )

    # [KOTA] marker'ları iç kota-takip kayıtları (quota_exceeded'da yaratılır,
    # _quota_blocked_today kullanır) — kullanıcıya "video" gibi gösterilmez.
    _visible_jobs = [
        j for j in jobs
        if not _is_superseded(j)
        and not (j.title or "").startswith("[KOTA]")
    ]

    # batch_id → Batch objesi + Drive source URL haritası
    _batch_source = {}
    _batch_by_id = {}
    try:
        for _b in load_batches():
            _batch_by_id[_b.id] = _b
            src = (_b.source or "").strip()
            if src and src.lower() not in ("manuel", "manual"):
                _batch_source[_b.id] = src
    except Exception:
        _batch_source, _batch_by_id = {}, {}

    # ===== 📁 TOPLU İŞLER (Batch) — Drive bulk takibi =====
    # Her batch bir expander: başlıkta progress özeti, içinde her job'ın durumu.
    # _env'e göre filtreli _visible_jobs'ı batch_id'ye göre grupla.
    _STATUS_ICON = {
        "done": "✅", "generating": "🎬", "running": "▶", "queued": "⏳",
        "failed": "❌", "stopped": "⏹", "submitted": "📤",
    }
    # Batch gruplaması: TÜM batch job'ları ([KOTA] hariç) — dedup UYGULAMA.
    # Kullanıcı batch içinde her işin akıbetini görsün (done + stopped/closed +
    # failed dahil). Aksi halde kapatılan/superseded işler gizlenip "9/13 ama
    # 9 satır" gibi kafa karıştıran görünüm oluyordu.
    _env_jobs_no_kota = [
        j for j in jobs if not (j.title or "").startswith("[KOTA]")
    ]
    _batch_groups = {}
    for j in _env_jobs_no_kota:
        bid = (j.batch_id or "").strip()
        if bid:
            _batch_groups.setdefault(bid, []).append(j)
    # Tekil (batch'siz) liste: dedup uygula (flat görünüm temiz kalsın)
    _single_jobs = [
        j for j in _visible_jobs if not (j.batch_id or "").strip()
    ]
    # Kapatılan/stopped işlerin "Aç" linki için: kendi video_remote_url'i yok,
    # ama videosu başka bir kayıtta (recovery / başka versiyon) hazır.
    # Hangi versiyona linklenmeli? Kapatılan işin created_at'i, aynı başlığın
    # gerçek çıktıları arasında KAÇINCI jenerasyona denk geliyorsa o versiyona.
    # Bu kural batch sırasına göre STABİL: 16:44 işi (16:44'te yaratıldı) hep
    # 2., 17:06 işi (17:06) hep 3. sıraya düşer — daha sonra eklenen 23:02 (v4)
    # batch'i created_at'i sonra olduğu için bu sırayı kaydırmaz.
    # (NOT: kapatılan işe video_remote_url PINLEMEK yanlış olur — azure
    #  versiyon rank'ını [bkz. azure_blob_basename_for_job sibling sayımı]
    #  bozar. Bu yüzden link display anında çözülür.)
    _title_outputs = {}  # title → [(created_at, url)] gerçek çıktılar, sıralı
    for j in jobs:
        if not j.video_remote_url:
            continue
        t = (j.title or "").strip()
        _title_outputs.setdefault(t, []).append((j.created_at, j.video_remote_url))
    for _t in _title_outputs:
        _title_outputs[_t].sort(key=lambda x: x[0])

    def _closed_job_url(j) -> str:
        """Kapatılan iş için created_at-sıralı versiyon linkini döndür."""
        outs = _title_outputs.get((j.title or "").strip(), [])
        if not outs:
            return ""
        # created_at'i kendinden ÖNCE olan çıktı sayısı → jenerasyon sırası
        rank = sum(1 for ca, _ in outs if ca < j.created_at) + 1
        idx = min(rank, len(outs)) - 1  # taşarsa en sonuncuya (en yüksek) düş
        return outs[idx][1]

    if _batch_groups:
        # === 2 SEVİYE GRUPLAMA: DRIVE KLASÖRÜ → KAYNAK VİDEO → VERSİYON ÇİPLERİ ===
        # Üst seviye: hangi Drive klasöründen geldiği (batch source URL) — kullanıcı
        # hangi linke bağlı olduğunu görsün. Alt seviye: o klasördeki her kaynak
        # video (=title) + TÜM versiyonları (v1..vN, created_at sırası).
        # Üretilirken canlı: done=▶ tıkla-aç / generating=🎬 / queued=⏳ / failed=❌.
        # Drive folder ID'ye göre grupla — aynı klasör farklı URL string'iyle
        # (trailing slash / ?usp=sharing / bare-id) girilmiş olsa bile TEK grupta
        # birleşsin. fkey = folder ID; her grup temsili tam URL'i de tutar (link).
        def _folder_key(src):
            _mm = re.search(r"/folders/([A-Za-z0-9_-]+)", src or "")
            return _mm.group(1) if _mm else (src or "").strip()

        _by_folder: dict[str, dict] = {}  # fkey -> {"url": str, "titles": {t: [jobs]}}
        for _bjobs in _batch_groups.values():
            for _j in _bjobs:
                _src = (_batch_source.get((_j.batch_id or "").strip(), "") or "").strip()
                _fk = _folder_key(_src)
                _ent = _by_folder.setdefault(_fk, {"url": _src, "titles": {}})
                if _src and not _ent["url"]:
                    _ent["url"] = _src
                _ent["titles"].setdefault((_j.title or "").strip(), []).append(_j)

        def _fold_recency(fk):
            return max((j.created_at for tj in _by_folder[fk]["titles"].values()
                        for j in tj), default=0)
        _fold_order = sorted(_by_folder, key=_fold_recency, reverse=True)
        _total_videos = sum(len(e["titles"]) for e in _by_folder.values())
        section_header("🎬 Toplu videolar (Drive klasörüne göre)",
                       f"{len(_fold_order)} klasör · {_total_videos} video")

        # 3B: Başlık/klasör arama — 100+ videoda gerekli. Eşleşen klasör+başlık
        # filtrelenir, eşleşen klasörler açık gelir.
        _q = st.text_input(
            "Ara", value="", placeholder="🔍 Başlık veya klasör ara…",
            label_visibility="collapsed", key="_folder_search").strip().lower()

        _CHIP_BASE = ("display:inline-block; margin:2px 5px 2px 0; padding:3px 9px;"
                      "border-radius:10px; font-size:0.78rem; font-weight:600;")

        for _fk in _fold_order:
            _ent = _by_folder[_fk]
            _src = _ent["url"]
            _titles = _ent["titles"]
            if _q:
                # Klasör adı/URL eşleşiyorsa tüm başlıkları göster; yoksa sadece
                # başlığı eşleşenleri. Hiç eşleşme yoksa klasörü atla.
                _lbl_match = _q in (_fk or "").lower() or _q in (_src or "").lower()
                if not _lbl_match:
                    _titles = {t: jl for t, jl in _titles.items()
                               if _q in (t or "").lower()}
                    if not _titles:
                        continue
            _all_jobs = [j for tj in _titles.values() for j in tj]
            _f_tot = len(_all_jobs)
            _f_done = sum(1 for j in _all_jobs
                          if j.status == "done" or (j.video_remote_url or "").strip())
            _f_gen = sum(1 for j in _all_jobs if j.status in ("generating", "running"))
            _f_q = sum(1 for j in _all_jobs if j.status in ("queued", "submitted"))
            # Klasör etiketi: Drive folder ID — kullanıcı tanısın
            if not _fk:
                _flabel = "kaynak belirtilmemiş"
            elif "/" in _fk or _fk.startswith("http"):
                _flabel = _fk[:30] + ("…" if len(_fk) > 30 else "")
            else:
                _flabel = _fk[:16] + ("…" if len(_fk) > 16 else "")
            # Öncelikli batch (bekleyen işi priority>0) → ⚡ rozeti
            _f_prio = any((getattr(j, "priority", 0) or 0) > 0 and j.status == "queued"
                          for j in _all_jobs)
            _fhead = ((f"⚡ " if _f_prio else "")
                      + f"📁 {_flabel}  ·  {len(_titles)} video  ·  ✅ {_f_done}/{_f_tot}"
                      + (f"  ·  🎬 {_f_gen}" if _f_gen else "")
                      + (f"  ·  ⏳ {_f_q}" if _f_q else ""))
            # Aktif iş olan klasör açık başlar; tamamen bitmiş klasör kapalı.
            # Arama varsa eşleşen klasör açık gelsin.
            with st.expander(_fhead, expanded=(bool(_q) or _f_gen + _f_q > 0)):
                if _src:
                    st.markdown(
                        f"📎 <a href='{_src}' target='_blank' "
                        f"style='font-size:0.8rem; word-break:break-all;'>"
                        f"{_esc(_src)}</a>", unsafe_allow_html=True)
                _title_order = sorted(
                    _titles, key=lambda t: max((j.created_at for j in _titles[t]),
                                               default=0), reverse=True)
                for _t in _title_order:
                    _vjobs = sorted(_titles[_t], key=lambda x: x.created_at)
                    _n_tot = len(_vjobs)
                    _n_ok = sum(1 for j in _vjobs
                                if j.status == "done" or (j.video_remote_url or "").strip())
                    def _short_acct(nm):
                        nm = (nm or "").split("@")[0]
                        for s in (".ho", ".twin.ai"):
                            nm = nm.replace(s, "")
                        return nm

                    _chips = []
                    for _i, j in enumerate(_vjobs, 1):
                        # Üreten hesabı çipe yaz (atanmışsa) → hangi NotebookLM'de
                        # bakacağını görsün. Hover'da tam hesap adı.
                        _acct = _short_acct(j.profile_name)
                        _albl = f" {_esc(_acct)}" if _acct else ""
                        _tip = (f"hesap: {_esc(j.profile_name)}" if j.profile_name
                                else "hesap atanmadı")
                        _url = j.video_remote_url
                        if not _url and j.status == "stopped":
                            _url = _closed_job_url(j)
                        if _url:
                            _chips.append(
                                f"<a href='{_url}' target='_blank' title='{_tip}' style='{_CHIP_BASE}"
                                f"background:rgba(22,137,62,0.14); color:#16893E; "
                                f"text-decoration:none;'>v{_i}{_albl} ▶</a>")
                        elif j.status in ("generating", "running"):
                            _chips.append(
                                f"<span title='{_tip}' style='{_CHIP_BASE}background:rgba(37,99,235,0.14); "
                                f"color:#2563EB;'>v{_i}{_albl} 🎬</span>")
                        elif j.status in ("queued", "submitted"):
                            _chips.append(
                                f"<span title='{_tip}' style='{_CHIP_BASE}background:rgba(251,191,36,0.18); "
                                f"color:#9A6B00;'>v{_i} ⏳</span>")
                        elif j.status == "failed":
                            _chips.append(
                                f"<span title='{_tip}' style='{_CHIP_BASE}background:rgba(239,68,68,0.14); "
                                f"color:#DC2626;'>v{_i}{_albl} ❌</span>")
                        else:
                            _chips.append(
                                f"<span style='{_CHIP_BASE}background:rgba(148,163,184,0.16); "
                                f"color:#6B7280;'>v{_i} •</span>")
                    _ttl = _t if len(_t) <= 60 else _t[:59] + "…"
                    _badge = "✅" if (_n_ok == _n_tot and _n_tot > 0) else ""
                    st.markdown(
                        f"<div style='display:flex; justify-content:space-between; "
                        f"align-items:baseline; margin:9px 0 1px 0;'>"
                        f"<span style='font-size:0.88rem; font-weight:600;'>📹 {_esc(_ttl)}</span>"
                        f"<span style='font-size:0.78rem; opacity:0.7; white-space:nowrap;'>"
                        f"{_n_ok}/{_n_tot} {_badge}</span></div>"
                        f"<div style='line-height:1.9;'>{''.join(_chips)}</div>",
                        unsafe_allow_html=True)

    # ===== 🎬 TEKİL VİDEOLAR (batch'siz gönderiler) =====
    _singles_sorted = sorted(_single_jobs, key=lambda j: j.created_at,
                             reverse=True)[:80]
    section_header(f"🎬 Tekil videolar", f"{len(_single_jobs)} kayıt")

    if not _singles_sorted:
        if not _batch_groups:
            empty_state(
                "📭",
                "Henüz video yok",
                "Yukarıdan senaryo gönder veya Drive klasörü ekle.",
            )
        else:
            st.caption("Tekil (batch'siz) gönderi yok — hepsi Drive toplu.")
    else:
        st.markdown('<div class="job-row-wrap">', unsafe_allow_html=True)
        for j in _singles_sorted:
            mine = _belongs_to_me(j)
            with st.container(border=True):
                cs = st.columns([1.3, 5, 1.5])
                with cs[0]:
                    st.markdown(status_pill(j.status), unsafe_allow_html=True)
                with cs[1]:
                    title_short = j.title if len(j.title) <= 80 else j.title[:79] + "…"
                    st.markdown(
                        f'<div style="font-size:0.95rem; font-weight:600; line-height:1.3;" '
                        f'title="{_esc(j.title)}">{_esc(title_short)}</div>',
                        unsafe_allow_html=True,
                    )
                    # Gönderen + ÜRETEN HESAP (hangi NotebookLM'de bakacağını görsün)
                    _sb = (j.submitted_by or "").strip() or "?"
                    _who = "👤 sen" if mine else f"👤 {_esc(_sb)}"
                    _pacct = (j.profile_name or "").split("@")[0].replace(".ho", "").replace(".twin.ai", "")
                    _acct_part = (f" &nbsp;·&nbsp; 🤖 <b>{_esc(_pacct)}</b>"
                                  if _pacct else "")
                    st.markdown(
                        f'<div style="font-size:0.72rem; opacity:0.6; margin-top:1px;">'
                        f'{_who}{_acct_part}</div>',
                        unsafe_allow_html=True,
                    )
                    # Status'a göre alt-açıklama
                    if j.status == "queued":
                        sub = f"⏳ Kuyrukta · {fmt_time(j.created_at)}"
                    elif j.status == "running":
                        elapsed = fmt_duration(j.started_at, 0)
                        sub = f"▶ NotebookLM tetikleniyor · {elapsed} sürdü"
                    elif j.status == "generating":
                        # Automator bitti, NotebookLM Cinematic videoyu üretiyor.
                        # Harvest cycle'ında ne aşamada olduğumuza göre alt-başlık.
                        hs = j.harvest_status
                        elapsed_min = max(0, int((time.time() - (j.finished_at or time.time())) / 60))
                        if hs == "checking":
                            sub = f"🔍 Video kontrol ediliyor... (deneme {j.harvest_attempts}/{HARVEST_MAX_ATTEMPTS})"
                        elif hs == "expired":
                            sub = "⌛ Video kontrolü zaman aşımı — Notebook'u açıp manuel bak"
                        elif j.harvest_attempts == 0:
                            first_at = (j.finished_at or j.created_at) + HARVEST_FIRST_DELAY_SEC
                            wait_min = max(0, int((first_at - time.time()) / 60))
                            if wait_min > 0:
                                sub = f"🎬 Video üretiliyor · {elapsed_min} dk geçti · ilk kontrol {wait_min} dk sonra"
                            else:
                                sub = f"🎬 Video üretiliyor · {elapsed_min} dk · kontrol çok yakında"
                        else:
                            next_min = max(0, int((j.next_harvest_at - time.time()) / 60))
                            sub = f"🎬 Video üretiliyor · {elapsed_min} dk · sonraki kontrol {next_min} dk (deneme {j.harvest_attempts}/{HARVEST_MAX_ATTEMPTS})"
                    elif j.status == "done":
                        hs = j.harvest_status
                        if hs == "uploaded":
                            sub = "🎬 Video hazır + bulutta paylaşıma açık!"
                        elif hs == "downloaded":
                            sub = "🎬 Video hazır ve indirilmiş!"
                        elif hs == "ready":
                            sub = "🎬 Video hazır — oynatabilirsin"
                        else:
                            sub = "✓ Tamamlandı"
                    elif j.status == "submitted":
                        sub = "📤 Tetiklendi (notebook URL'i yok). Admin loga baksın."
                    elif j.status == "failed":
                        err = j.error.lower() if j.error else ""
                        if _is_quota_error(j.error or ""):
                            sub = "🚫 Kota dolu — yarın otomatik denenir"
                        elif "login" in err or "giriş" in err:
                            sub = "🔓 Hesap login süresi geçmiş — yöneticiye haber ver"
                        else:
                            sub = f"⚠ Hata: {_esc((j.error or 'bilinmiyor')[:120])}"
                    elif j.status == "stopped":
                        sub = "⏹ Durduruldu"
                    else:
                        sub = j.status
                    st.markdown(
                        f'<div style="font-size:0.78rem; opacity:0.7; margin-top:3px;">{sub}</div>',
                        unsafe_allow_html=True,
                    )
                with cs[2]:
                    # Öncelik: Azure remote URL > local download > video URL > notebook URL
                    if j.video_remote_url:
                        st.markdown(
                            f'<a href="{j.video_remote_url}" target="_blank" '
                            f'style="display:block; text-align:center; padding:6px 10px; '
                            f'background:#10B981; color:white; border-radius:6px; '
                            f'text-decoration:none; font-size:0.82rem; font-weight:600; '
                            f'margin-bottom:4px;">☁️ Video aç</a>',
                            unsafe_allow_html=True,
                        )
                    elif j.video_local_path and (APP_DIR / j.video_local_path).exists():
                        full_path = APP_DIR / j.video_local_path
                        try:
                            with full_path.open("rb") as fh:
                                st.download_button(
                                    "⬇️ Videoyu indir",
                                    data=fh.read(),
                                    file_name=full_path.name,
                                    mime="video/mp4",
                                    key=f"u_dl_{j.id}",
                                    use_container_width=True,
                                )
                        except OSError:
                            pass
                    elif j.video_url:
                        st.markdown(
                            f'<a href="{j.video_url}" target="_blank" '
                            f'style="display:block; text-align:center; padding:6px 10px; '
                            f'background:#6366F1; color:white; border-radius:6px; '
                            f'text-decoration:none; font-size:0.82rem; font-weight:600; '
                            f'margin-bottom:4px;">▶ Video oynat</a>',
                            unsafe_allow_html=True,
                        )
                    elif j.notebook_url and j.status in TERMINAL_STATUSES:
                        st.link_button("🌐 Notebook'u aç", j.notebook_url, use_container_width=True)

                    # Phase 4: Revize butonu — sadece video Azure'da hazırsa
                    if j.video_remote_url and j.status == "done":
                        if st.button("✏ Revize et",
                                      key=f"u_revize_{j.id}",
                                      use_container_width=True,
                                      help="Bu videoyu source yapıp yeni bir Cinematic üret"):
                            st.session_state["revize_target_id"] = j.id
                            st.rerun()

                # --- Kaynak metni + Drive linki (kart altı, full-width) ---
                _drive_src = _batch_source.get(j.batch_id, "") if j.batch_id else ""
                _txt = (j.text or "").strip()
                if _drive_src or _txt:
                    with st.expander("📄 Kaynak metni gör"):
                        if _drive_src:
                            st.markdown(
                                f'📎 **Drive klasörü:** [{_drive_src[:70]}…]({_drive_src})'
                                if len(_drive_src) > 70 else
                                f'📎 **Drive klasörü:** [{_drive_src}]({_drive_src})'
                            )
                        if j.learning_objectives and j.learning_objectives.strip():
                            st.markdown("**🎯 Learning Objectives:**")
                            st.text(j.learning_objectives.strip()[:3000])
                            st.markdown("**📝 Script:**")
                        if _txt:
                            st.text(_txt[:8000])
                            if len(_txt) > 8000:
                                st.caption(f"… (+{len(_txt) - 8000} karakter daha)")
        st.markdown('</div>', unsafe_allow_html=True)

    # Footer: logout
    st.markdown("&nbsp;", unsafe_allow_html=True)
    cs = st.columns([5, 1])
    with cs[1]:
        if st.button("🚪 Çıkış", key="user_logout", use_container_width=True):
            do_logout()
            st.rerun()
    st.markdown(
        '<div style="margin-top:1rem; text-align:center; font-size:0.78rem; opacity:0.5;">'
        'Sorun var mı? Yöneticiye haber ver.'
        '</div>',
        unsafe_allow_html=True,
    )


# ===== AUTH GATE + MODE DISPATCH =====
# users.json yoksa default admin oluştur (ADMIN_PASSWORD env var'dan).
ensure_seed_admin()

# Login değilse → login view göster, çık.
if not is_logged_in():
    render_login_view()
    st.stop()

# Login + role=user → user view, çık.
if not _is_admin():
    render_user_view()
    # Auto-refresh (aktif iş varken canlı takip için ~4sn'de bir tüm sayfayı
    # yeniler). ANCAK kullanıcı tekli senaryo yazarken / revize ederken bu rerun
    # odağı kaçırıp yarım metni bozuyordu (şikayet). Compose modunda DURDUR →
    # sayfa stabil; toggle/modal kapanınca canlı takip geri döner. (Streamlit
    # tab'leri tüm sayfayı render ettiği için ayrı tab bunu çözmezdi; refresh'i
    # mod-bazlı durdurmak gerçek izolasyon.)
    _composing = (
        st.session_state.get("show_single_flow", False)
        or bool(st.session_state.get("revize_target_id", ""))
    )
    _jobs_now = load_jobs()
    if (not _composing
            and any(j.status in ("running", "queued", "generating") for j in _jobs_now)):
        time.sleep(4)
        st.rerun()
    st.stop()

# Login + role=admin → aşağıdaki admin UI render edilir (sidebar + tab'lar).


# ===== SIDEBAR =====
with st.sidebar:
    _au = current_user() or {}
    st.markdown(
        f'<div style="padding: 0.4rem 0 0.6rem 0;">'
        f'<div style="font-size: 1.3rem; font-weight: 700; letter-spacing: -0.02em;">🎬 Cinematic Studio</div>'
        f'<div style="font-size: 0.78rem; opacity: 0.7; margin-top: 2px;">'
        f'Giriş: <b>{_au.get("display_name", "")}</b> · '
        f'<span style="color:#6366F1;">{_au.get("role", "").upper()}</span></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    cs_top = st.columns([3, 1])
    with cs_top[0]:
        st.link_button("🌐 NotebookLM açar", "https://notebooklm.google.com/", use_container_width=True)
    with cs_top[1]:
        if st.button("🚪", key="admin_logout", use_container_width=True, help="Çıkış yap"):
            do_logout()
            st.rerun()

    st.markdown('<div class="sidebar-section">Hesap profilleri</div>', unsafe_allow_html=True)

    profiles = load_profiles()
    jobs_for_count = load_jobs()

    if not profiles:
        st.info("Henüz profil yok. Aşağıdan ekle.", icon="ℹ️")

    for p in profiles:
        today_count = sum(
            1
            for j in jobs_for_count
            if j.profile_id == p.id
            and j.status in COUNTED_STATUSES
            and j.started_at
            and datetime.fromtimestamp(j.started_at).date() == date.today()
        )
        dot = "🟢" if p.initialized else "⚪"
        limit_str = f"{today_count}/{p.daily_limit}" if p.daily_limit > 0 else f"{today_count}/∞"
        # İsim uzunsa kısalt
        name_short = p.name if len(p.name) <= 28 else p.name[:27] + "…"
        # Env badge: dev=turuncu, prod=yeşil
        _p_env = (p.environment or "prod").lower()
        _env_clr = "#f59e0b" if _p_env == "dev" else "#10b981"
        _env_txt = "🧪 DEV" if _p_env == "dev" else "🚀 PROD"

        with st.container(border=True):
            st.markdown(
                f'<div style="display:flex; align-items:center; gap:6px; margin-bottom:4px;">'
                f'<span style="font-size:0.85rem;">{dot}</span>'
                f'<span style="font-weight:600; font-size:0.88rem; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="{p.name}">{name_short}</span>'
                f'<span style="font-size:0.68rem; padding:1px 6px; background:{_env_clr}20; '
                f'color:{_env_clr}; border-radius:8px; font-weight:600;">{_env_txt}</span>'
                f'</div>'
                f'<div style="font-size:0.74rem; opacity:0.75; margin-bottom:6px;">'
                f'bugün <b>{limit_str}</b> &nbsp;·&nbsp; paralel <b>×{p.max_concurrent}</b>'
                f'</div>',
                unsafe_allow_html=True,
            )
            cols = st.columns([3, 1])
            with cols[0]:
                if not p.initialized:
                    if st.button("🔓 Hesabı aktive et", key=f"login_{p.id}", use_container_width=True, type="primary"):
                        launch_profile_init(p)
                        st.session_state[f"init_started_{p.id}"] = time.time()
                        if VNC_ENABLED:
                            st.toast(
                                "Chromium virtual display'de açıldı. Aşağıdaki VNC linkine "
                                "tıkla, Google'a giriş yap, pencereyi kapat — gerisi otomatik.",
                                icon="🖥️",
                            )
                        else:
                            st.toast(
                                "Chromium açıldı. Google'a giriş yap, ardından pencereyi "
                                "kapat — gerisi otomatik.",
                                icon="🔓",
                            )
                else:
                    if st.button("🔄 Yeniden giriş", key=f"relogin_{p.id}", use_container_width=True):
                        # Eski/expire cookie'leri temizle — redirect döngüsünü önler
                        _pdir = PROFILES_DIR / p.id
                        if _pdir.exists():
                            shutil.rmtree(_pdir)
                        _pdir.mkdir(parents=True, exist_ok=True)
                        launch_profile_init(p)
                        st.session_state[f"init_started_{p.id}"] = time.time()
                        st.toast("Chromium açıldı.", icon="🔓")
            with cols[1]:
                if st.button("🗑", key=f"del_{p.id}", help="Profili sil", use_container_width=True):
                    ps = [x for x in load_profiles() if x.id != p.id]
                    save_profiles(ps)
                    pdir = PROFILES_DIR / p.id
                    if pdir.exists():
                        try:
                            shutil.rmtree(pdir)
                        except OSError:
                            pass
                    st.rerun()

            # VNC linki — init başlatılmışsa (sunucuda xvfb varsa)
            init_started_at = st.session_state.get(f"init_started_{p.id}", 0)
            if VNC_ENABLED and init_started_at and (time.time() - init_started_at) < 600:
                # Init son 10 dk'da başlatıldı — VNC linki göster
                # noVNC URL parametreleri:
                # - autoconnect=1: "Bağlan" tıklamaya gerek yok
                # - resize=scale: viewport'a sığ, scroll yok
                # - quality=6, compression=2: dengeli (default)
                # - show_dot=true: cursor görünmediğinde nokta göster (yazma pozisyonu)
                # - reconnect=true, reconnect_delay=2000: WebSocket koparsa
                #   2sn sonra otomatik yeniden bağlan (kopma UX'i)
                vnc_url = (
                    "/vnc/vnc.html"
                    "?autoconnect=1"
                    "&resize=scale"
                    "&quality=6"
                    "&compression=2"
                    "&show_dot=true"
                    "&reconnect=true"
                    "&reconnect_delay=2000"
                )
                st.markdown(
                    f'<a href="{vnc_url}" target="_blank" '
                    'style="display:block; text-align:center; padding:8px 10px; '
                    'background:#10B981; color:white; border-radius:6px; '
                    'text-decoration:none; font-size:0.85rem; font-weight:600; margin-top:6px;">'
                    '🖥️ VNC ekranını aç → Google login</a>'
                    '<div style="font-size:0.72rem; opacity:0.7; margin-top:4px; text-align:center;">'
                    'Login olduktan sonra Chromium penceresini kapat. Auto-init aktive eder.'
                    '</div>',
                    unsafe_allow_html=True,
                )

            # Login pending olanlar için bilgilendirme
            if not p.initialized:
                if VNC_ENABLED:
                    msg = (
                        '⏳ Login bekleniyor — VNC üzerinden Chromium\'da giriş '
                        'yapınca <b>otomatik aktive olur</b>'
                    )
                else:
                    msg = (
                        '⏳ Login bekleniyor — Chromium\'da giriş yapınca '
                        '<b>otomatik aktive olur</b>'
                    )
                st.markdown(
                    f'<div style="font-size:0.72rem; opacity:0.7; margin-top:6px; '
                    f'padding:6px 8px; background:rgba(99,102,241,0.08); border-radius:6px;">'
                    f'{msg}</div>',
                    unsafe_allow_html=True,
                )

            # --- Lokal init (Mac/PC üstünde Playwright native window) ---
            # ./deploy/login.sh (interactive, en kolay) veya ./deploy/push_auth.sh
            # (auth.json varsa tek-tık) önerilir. Manuel rsync de gösterilir
            # ama mkdir adımı eklenmiş (klasör yoksa fail eder).
            with st.expander("💻 Lokal makineden yenile (VNC gerekmez)"):
                st.markdown(
                    "**🥇 En kolay — `deploy/login.sh` (interaktif menü + auto-rsync + smoke):**",
                )
                st.code(
                    "cd /path/to/notebooklm-cinematic-studio\n"
                    "NLM_SSH_KEY=~/.ssh/dev-internal-00.pem ./deploy/login.sh",
                    language="bash",
                )
                st.markdown(
                    f"İnteraktif menüden `{p.name}` seç → Chromium açılır → "
                    "login → Cmd+Q → otomatik rsync + smoke + initialized=True. "
                    "**Bu seçenek hiçbir manuel adım gerektirmez.**"
                )

                st.markdown("---")
                st.markdown(
                    "**🥈 Auth.json zaten lokalde varsa — `push_auth.sh` (mkdir + rsync + smoke + init flip tek satırda):**"
                )
                st.code(
                    f"./deploy/push_auth.sh {p.id}",
                    language="bash",
                )

                st.markdown("---")
                st.markdown(
                    "**🛠 Tam manuel (debug/audit için) — 3 adım:**"
                )
                init_cmd = (
                    f".venv/bin/python notebooklm_automator.py --init "
                    f"--profile-dir chrome_profiles/{p.id} --authuser {p.authuser}"
                )
                st.markdown("**1.** Chromium'u native aç + login:")
                st.code(f"cd /path/to/notebooklm-cinematic-studio\n{init_cmd}",
                        language="bash")
                if LOCAL_INIT_SSH_HOST:
                    # mkdir + rsync — klasör yoksa rsync fail etmesin
                    rsync_block = (
                        f"# Önce sunucuda klasörü hazırla (yoksa oluştur)\n"
                        f"ssh -i {LOCAL_INIT_SSH_KEY} {LOCAL_INIT_SSH_HOST} \\\n"
                        f"  'mkdir -p {LOCAL_INIT_REMOTE_PATH}/chrome_profiles/{p.id}'\n\n"
                        f"# Sonra auth.json'u rsync ile gönder\n"
                        f"rsync -avz -e \"ssh -i {LOCAL_INIT_SSH_KEY}\" \\\n"
                        f"  chrome_profiles/{p.id}/auth.json \\\n"
                        f"  {LOCAL_INIT_SSH_HOST}:{LOCAL_INIT_REMOTE_PATH}/"
                        f"chrome_profiles/{p.id}/auth.json"
                    )
                    st.markdown("**2.** Klasör yarat + auth.json gönder:")
                    st.code(rsync_block, language="bash")
                else:
                    st.caption(
                        "ℹ️ `.env`'de `LOCAL_INIT_SSH_HOST=ubuntu@...` set edersen "
                        "mkdir + rsync komutları da hazır şekilde gösterilir."
                    )
                st.markdown("**3.** Aşağıdaki butona bas — smoke test + initialized=True:")
                if st.button("✅ Auth.json'um hazır, kontrol et",
                             key=f"verify_local_{p.id}", use_container_width=True):
                    # Server-side smoke test
                    try:
                        from notebooklm_client import smoke_test as _nlm_smoke
                        ok, msg = _nlm_smoke(p.id)
                    except Exception as e:
                        ok, msg = False, f"smoke import/run error: {e}"
                    if ok:
                        ps = load_profiles()
                        for x in ps:
                            if x.id == p.id:
                                x.initialized = True
                                break
                        save_profiles(ps)
                        st.success(f"✅ Aktive edildi — {msg}")
                        st.rerun()
                    else:
                        st.error(f"❌ Auth.json doğrulanamadı: {msg}")

            with st.expander("⚙️ Gelişmiş ayarlar"):
                new_name = st.text_input("İsim", value=p.name, key=f"nm_{p.id}")
                new_authuser = st.number_input(
                    "authuser index",
                    min_value=0, max_value=20, value=p.authuser, step=1,
                    key=f"au_{p.id}",
                    help="Tarayıcıda birden fazla Google hesabı aktif ise hangisini kullansın? (0 = ilk hesap)",
                )
                new_limit = st.number_input(
                    "Günlük max video",
                    min_value=0, max_value=100, value=p.daily_limit, step=1,
                    key=f"lim_{p.id}",
                    help=(
                        "Bu profilin günlük Cinematic kotası. 0 = sınırsız "
                        "(otomatik tespit yok). NotebookLM tier'ları:\n"
                        "• Ücretsiz: ~3/gün\n"
                        "• Google One AI Pro: ~10/gün\n"
                        "• Google One AI Ultra: ~20/gün\n"
                        "Yanlış set edersen dispatcher 'quota_exceeded' hatası "
                        "alıp profili o gün için otomatik pause eder — "
                        "self-correct."
                    ),
                )
                new_max_c = st.number_input(
                    "Paralel slot",
                    min_value=1, max_value=10, value=p.max_concurrent, step=1,
                    key=f"mc_{p.id}",
                )
                new_headless = st.checkbox(
                    "Arka planda çalış (görünmez)",
                    value=p.headless,
                    key=f"hl_{p.id}",
                )
                new_env = st.selectbox(
                    "Environment",
                    options=["prod", "dev"],
                    index=0 if (p.environment or "prod") == "prod" else 1,
                    key=f"env_{p.id}",
                    help=(
                        "Bu profilin hangi ortama atanacağı. Dispatcher "
                        "sadece aynı env'deki job'ları bu profile gönderir. "
                        "prod=canlı kullanım, dev=test/staging."
                    ),
                )
                if st.button("Kaydet", key=f"save_{p.id}"):
                    ps = load_profiles()
                    for x in ps:
                        if x.id == p.id:
                            x.name = new_name.strip() or x.name
                            x.authuser = int(new_authuser)
                            x.daily_limit = int(new_limit)
                            x.max_concurrent = int(new_max_c)
                            x.headless = bool(new_headless)
                            x.environment = new_env
                            break
                    save_profiles(ps)
                    st.toast("Kaydedildi.", icon="✅")
                    st.rerun()

    st.markdown('<div class="sidebar-section">+ Yeni hesap ekle</div>', unsafe_allow_html=True)
    with st.form("new_profile", clear_on_submit=True):
        np_name = st.text_input(
            "Hesap adı",
            placeholder="örn. baran@yga.org.tr",
            help="Sadece ayırt etmek için — istediğin ismi ver",
        )
        np_env = st.selectbox(
            "Environment",
            options=["prod", "dev"],
            index=0,
            help=(
                "Hangi ortama atanacak. prod=canlı kullanım, "
                "dev=test/staging. Dispatcher sadece aynı env'deki "
                "job'ları bu profile gönderir."
            ),
        )
        with st.expander("Gelişmiş (opsiyonel)"):
            np_authuser = st.number_input(
                "authuser",
                min_value=0, max_value=20, value=0, step=1,
                help="Chrome'da kaçıncı Google hesabı (0=ilk). Tek hesap kullanıyorsan 0 bırak.",
            )
            np_limit = st.number_input(
                "Günlük max video",
                min_value=0, max_value=100, value=3, step=1,
                help="NotebookLM ücretsiz limit: 3/gün. 0 = sınırsız (Pro hesap için)",
            )
            np_concurrent = st.number_input(
                "Paralel slot",
                min_value=1, max_value=10, value=1, step=1,
                help="Aynı hesapla aynı anda kaç browser açılsın",
            )
        if st.form_submit_button("➕ Hesap oluştur", use_container_width=True, type="primary"):
            if not np_name.strip():
                st.error("İsim boş olamaz.")
            else:
                ps = load_profiles()
                new_p = Profile(
                    id=uuid.uuid4().hex[:12],
                    name=np_name.strip(),
                    authuser=int(np_authuser),
                    daily_limit=int(np_limit),
                    max_concurrent=int(np_concurrent),
                    headless=True,
                    initialized=False,
                    environment=np_env,
                )
                ps.append(new_p)
                save_profiles(ps)
                st.toast("Hesap oluşturuldu. Şimdi 'Hesabı aktive et' butonuna bas.", icon="✅")
                st.rerun()


# ===== HERO HEADER (admin) =====
_init_count = sum(1 for p in load_profiles() if p.initialized)
_total_count = len(load_profiles())
st.markdown(
    f'<div class="app-hero">'
    f'<h1>🎬 Cinematic Studio  <span style="font-size:0.6em; padding:3px 10px; '
    f'background:rgba(255,255,255,0.18); border-radius:999px; vertical-align:middle; '
    f'font-weight:600; letter-spacing:0.05em;">YÖNETİM</span></h1>'
    f'<p>Toplu metin → paralel video üretimi · {_init_count}/{_total_count} hesap aktif</p>'
    f'</div>',
    unsafe_allow_html=True,
)


def render_admin_overview() -> None:
    """Admin gözlem paneli: kim ne üretti + hesap kotaları + canlı özet.
    İçerik üretimi artık kullanıcı tarafında — admin sadece izler/yönetir."""
    from collections import defaultdict
    section_header("📊 Genel Bakış", "Kim ne üretti · hesap kotaları · canlı durum")
    jobs = load_jobs()
    profiles = load_profiles()
    now = time.time()
    today = date.today()

    def _real(j):
        return not (j.title or "").startswith("[KOTA]")

    def _is_today(ts):
        try:
            return bool(ts) and datetime.fromtimestamp(ts).date() == today
        except (OSError, ValueError, OverflowError):
            return False

    def _rel(ts):
        if not ts:
            return "—"
        d = now - ts
        if d < 3600:
            return f"{int(d/60)}dk"
        if d < 86400:
            return f"{int(d/3600)}sa"
        return f"{int(d/86400)}g"

    done_all = [j for j in jobs if j.status == "done" and _real(j)]
    done_today = [j for j in done_all if _is_today(j.finished_at or j.started_at)]
    inflight = [j for j in jobs if j.status in ("running", "generating", "submitted")]
    queued = [j for j in jobs if j.status == "queued"]
    failed_today = [j for j in jobs if j.status == "failed" and _real(j) and _is_today(j.finished_at)]

    mc = st.columns(4)
    mc[0].metric("Bugün üretilen", len(done_today))
    mc[1].metric("Şu an üretiliyor", len(inflight))
    mc[2].metric("Kuyrukta", len(queued))
    mc[3].metric("Bugün hata", len(failed_today))

    # ---- Hesap kotaları ----
    section_header("🔑 Hesap kotaları (bugün)", "Üretilen · durum · son aktivite")
    cut = now - QUOTA_BLOCK_HOURS * 3600
    init_p = [p for p in profiles if p.initialized]
    h = ['<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
         '<tr style="text-align:left;opacity:0.55;border-bottom:1px solid rgba(0,0,0,0.12);">'
         '<th style="padding:6px 8px;">Hesap</th><th>Bugün</th><th>Limit</th>'
         '<th>Durum</th><th>Son üretim</th></tr>']
    for p in sorted(init_p, key=lambda x: (x.name or "")):
        pj = [j for j in jobs if j.profile_id == p.id]
        tcount = sum(1 for j in pj if j.status in COUNTED_STATUSES and _real(j)
                     and _is_today(j.started_at or j.created_at))
        active = sum(1 for j in pj if j.status in ("running", "generating", "submitted"))
        blocked = any(j.error and _is_quota_error(j.error)
                      and ((j.finished_at or j.started_at or j.created_at) > cut) for j in pj)
        last_done = max((j.finished_at or 0 for j in pj if j.status == "done"), default=0)
        if blocked:
            dlabel, dbg, dfg = "🛑 kota dolu", "#FEE2E2", "#991B1B"
        elif active:
            dlabel, dbg, dfg = f"🎬 üretiyor ({active})", "#EDE9FE", "#5B21B6"
        else:
            dlabel, dbg, dfg = "✅ açık", "#DCFCE7", "#166534"
        h.append(
            f'<tr style="border-bottom:1px solid rgba(0,0,0,0.05);">'
            f'<td style="padding:6px 8px;font-weight:500;">{_esc(p.name)}</td>'
            f'<td style="text-align:center;">{tcount}</td>'
            f'<td style="text-align:center;opacity:0.55;">{p.daily_limit or "∞"}</td>'
            f'<td><span style="background:{dbg};color:{dfg};padding:2px 9px;'
            f'border-radius:10px;font-size:0.76rem;font-weight:500;">{dlabel}</span></td>'
            f'<td style="opacity:0.7;">{_rel(last_done)}</td></tr>')
    h.append('</table>')
    st.markdown("".join(h), unsafe_allow_html=True)
    if SHUTTERSTOCK_ENABLED:
        _sub = shutterstock_subscription()
        if _sub:
            st.caption(f"🛒 Shutterstock: **{_sub.get('downloads_left','?')}**/"
                       f"{_sub.get('downloads_limit','?')} lisans indirme kaldı (bu ay)")

    # ---- Kim ne üretti ----
    section_header("👤 Kim ne üretti", "Kullanıcı bazında üretilen video")
    by_user = defaultdict(list)
    for j in done_all:
        by_user[((j.submitted_by or "").strip() or "—")].append(j)
    if not by_user:
        empty_state("👤", "Henüz üretim yok", "Kullanıcılar video ürettikçe burada görünür.")
        return
    h2 = ['<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
          '<tr style="text-align:left;opacity:0.55;border-bottom:1px solid rgba(0,0,0,0.12);">'
          '<th style="padding:6px 8px;">Kullanıcı</th><th>Toplam</th><th>Bugün</th>'
          '<th>Son video</th></tr>']
    for user, js in sorted(by_user.items(), key=lambda kv: -len(kv[1])):
        ut = sum(1 for j in js if _is_today(j.finished_at or j.started_at))
        last = max(js, key=lambda x: x.created_at)
        _t = _esc((last.title or "—")[:46])
        last_cell = (f'<a href="{_esc(last.video_remote_url)}" target="_blank">{_t}</a>'
                     if last.video_remote_url else _t)
        h2.append(
            f'<tr style="border-bottom:1px solid rgba(0,0,0,0.05);">'
            f'<td style="padding:6px 8px;font-weight:500;">{_esc(user)}</td>'
            f'<td style="text-align:center;">{len(js)}</td>'
            f'<td style="text-align:center;">{ut}</td>'
            f'<td style="opacity:0.85;">{last_cell}</td></tr>')
    h2.append('</table>')
    st.markdown("".join(h2), unsafe_allow_html=True)
    st.caption("Detaylı gün/hesap kırılımı için 📈 Üretim sekmesi · iş bazında 📊 Durum.")


# ===== ANA PANEL — TAB'LAR =====
tab_overview, tab_status, tab_videos, tab_uretim, tab_log, tab_users = st.tabs(
    ["📊  Genel Bakış", "📋  Durum", "🎬  Videolar",
     "📈  Üretim", "📜  Log", "👥  Kullanıcılar"]
)

with tab_overview:
    render_admin_overview()


# -------------------- TAB 2: DURUM --------------------
with tab_status:
    _all_jobs_raw = load_jobs()
    # [KOTA] marker'ları iç kota-takip kayıtları — sayım/listeden çıkar
    # (gerçek video değil; quota_hit_profiles tespiti _all_jobs_raw kullanır).
    jobs = [j for j in _all_jobs_raw if not (j.title or "").startswith("[KOTA]")]
    counts = {"queued": 0, "running": 0, "done": 0, "submitted": 0, "failed": 0, "stopped": 0}
    for j in jobs:
        counts[j.status] = counts.get(j.status, 0) + 1

    # Kota dolu uyarısı: bugün quota_exceeded yiyen profilleri belirgin göster.
    # _all_jobs_raw kullan — [KOTA] marker'ları kota tespitinin ana kaynağı.
    today = date.today()
    quota_hit_profiles: dict[str, str] = {}  # profile_name -> last error
    for j in _all_jobs_raw:
        if not j.error or "kota" not in j.error.lower() and "limit" not in j.error.lower():
            continue
        ts = j.finished_at or j.started_at or j.created_at
        try:
            if datetime.fromtimestamp(ts).date() == today:
                quota_hit_profiles[j.profile_name or "(bilinmiyor)"] = j.error
        except (OSError, OverflowError, ValueError):
            continue

    if quota_hit_profiles:
        names = ", ".join(f"**{n}**" for n in quota_hit_profiles.keys())
        st.warning(
            f"🚫 NotebookLM günlük Cinematic kotası dolu: {names}.  \n"
            "Bu hesap(lar) için bugün artık video üretilemez. Kota Pasifik saatiyle 00:00 "
            "(TR ~10:00) civarı resetlenir. Acil ise: yeni profil ekle (farklı Google hesabı) "
            "veya ilgili hesabı NotebookLM Pro'ya yükselt.",
            icon="🚫",
        )

    # Video harvest istatistikleri
    n_video_ready = sum(1 for j in jobs if j.video_url and j.harvest_status in ("ready", "downloaded", "uploaded"))
    n_video_uploaded = sum(1 for j in jobs if j.harvest_status == "uploaded")

    # --- Filtreli durum kartları ---
    # Karta basmak job listesini o statüye göre filtreler.
    # İkinci basış veya "Tümü" filtreyi temizler.
    if "job_filter" not in st.session_state:
        st.session_state.job_filter = None

    def _filter_btn(col, label: str, count, key: str) -> None:
        active = st.session_state.job_filter == key
        btn_type = "primary" if active else "secondary"
        display = f"{label}  **{count}**"
        if col.button(display, key=f"flt_{key}", use_container_width=True, type=btn_type):
            st.session_state.job_filter = None if active else key
            st.rerun()

    fcols = st.columns(7)
    # "Tümü" butonu
    _all_active = st.session_state.job_filter is None
    if fcols[0].button(
        f"🗂 Tümü  **{len(jobs)}**",
        key="flt_all",
        use_container_width=True,
        type="primary" if _all_active else "secondary",
    ):
        st.session_state.job_filter = None
        st.rerun()

    _filter_btn(fcols[1], "⏳ Kuyrukta", counts.get("queued", 0), "queued")
    _filter_btn(fcols[2], "▶ Çalışan", counts.get("running", 0), "running")
    _filter_btn(fcols[3], "🎬 Üretiliyor", counts.get("generating", 0) + counts.get("submitted", 0), "generating")
    _filter_btn(fcols[4], "🎬 Video hazır", n_video_ready, "video_ready")
    _filter_btn(fcols[5], "☁️ Azure'da", n_video_uploaded if AZURE_ENABLED else 0, "uploaded")
    _filter_btn(fcols[6], "✗ Hatalı", counts.get("failed", 0), "failed")

    if AZURE_ENABLED:
        st.caption(f"☁️ Azure aktif · container: `{AZURE_CONTAINER}` · prefix: `{AZURE_BLOB_PREFIX}`")
    else:
        st.caption("ℹ️ Azure kapalı (`AZURE_STORAGE_CONNECTION_STRING` env var set edilmedi)")
    if SLACK_ENABLED:
        _sc = st.columns([3, 1])
        with _sc[0]:
            st.caption(
                "🔔 Slack bildirimleri aktif · per-video (tek-script), batch "
                "özeti, auth alert, günsonu rapor"
            )
        with _sc[1]:
            if st.button("📨 Test mesajı", key="slack_test", use_container_width=True):
                ok = send_slack_message(
                    f"📨 *Test bildirimi* — admin panelden gönderildi "
                    f"({datetime.now().strftime('%H:%M')})"
                )
                if ok:
                    st.toast("Slack test mesajı gönderildi ✓", icon="📨")
                else:
                    st.toast("Slack gönderilemedi — webhook'u kontrol et", icon="⚠️")
    else:
        st.caption("🔕 Slack kapalı (`SLACK_WEBHOOK_URL` env var set edilmedi)")

    # Tüm Azure URL'leri tek blokta — toplu paylaşım için
    azure_jobs = [j for j in jobs if j.video_remote_url]
    if azure_jobs:
        with st.expander(f"📋 Tüm Azure URL'leri ({len(azure_jobs)})", expanded=False):
            st.caption("Aşağıdaki bloku tek tıkla kopyalayabilirsin (köşedeki 📋 ikonu).")
            lines = []
            for j in sorted(azure_jobs, key=lambda x: x.created_at, reverse=True):
                title_clean = j.title.replace("\n", " ")[:80]
                submitter = f" [{j.submitted_by}]" if j.submitted_by else ""
                lines.append(f"# {title_clean}{submitter}")
                lines.append(j.video_remote_url)
                lines.append("")
            st.code("\n".join(lines), language=None)

    # CSV verisi (her zaman hazır)
    csv_buf = io.StringIO()
    csv_buf.write("﻿")  # BOM, Excel UTF-8 uyumu
    csv_w = csv.writer(csv_buf)
    csv_w.writerow([
        "id", "title", "status", "submitted_by", "profile", "started",
        "duration_sec", "notebook_url",
        "harvest_status", "video_url", "video_local_path", "azure_url",
        "error",
    ])
    for j in load_jobs():
        duration = (j.finished_at or time.time()) - j.started_at if j.started_at else 0
        csv_w.writerow([
            j.id, j.title, j.status, j.submitted_by, j.profile_name,
            fmt_time(j.started_at),
            int(duration) if j.started_at else "",
            j.notebook_url,
            j.harvest_status, j.video_url, j.video_local_path, j.video_remote_url,
            j.error,
        ])

    st.markdown("&nbsp;", unsafe_allow_html=True)
    cs = st.columns([1.4, 1.4, 1.4, 4])
    with cs[0]:
        if st.button("🧹 Tamamlananları temizle", use_container_width=True):
            jobs2 = [j for j in load_jobs() if j.status not in TERMINAL_STATUSES]
            save_jobs(jobs2)
            st.rerun()
    with cs[1]:
        if st.button("🛑 Tümünü durdur", type="secondary", use_container_width=True):
            n = worker.stop_all_jobs()
            st.toast(f"{n} job durduruldu/iptal.", icon="🛑")
            st.rerun()
    with cs[2]:
        st.download_button(
            "⬇️ CSV indir",
            csv_buf.getvalue(),
            "jobs.csv",
            "text/csv",
            use_container_width=True,
        )

    st.markdown("&nbsp;", unsafe_allow_html=True)

    # Filtre uygula
    _jf = st.session_state.get("job_filter")
    if _jf == "queued":
        jobs_display = [j for j in jobs if j.status == "queued"]
    elif _jf == "running":
        jobs_display = [j for j in jobs if j.status == "running"]
    elif _jf == "generating":
        jobs_display = [j for j in jobs if j.status in ("generating", "submitted")]
    elif _jf == "video_ready":
        jobs_display = [j for j in jobs if j.video_url and j.harvest_status in ("ready", "downloaded", "uploaded")]
    elif _jf == "uploaded":
        jobs_display = [j for j in jobs if j.harvest_status == "uploaded"]
    elif _jf == "failed":
        jobs_display = [j for j in jobs if j.status == "failed"]
    else:
        jobs_display = jobs

    _filter_label = f" · filtre: {_jf}" if _jf else ""
    section_header("📋 Joblar", f"{len(jobs_display)}/{len(jobs)} kayıt{_filter_label}")

    if not jobs:
        empty_state(
            "📊",
            "Henüz job yok",
            "Hazırla sekmesinden bir içeriği kuyruğa at, durumunu burada izle.",
        )
    elif not jobs_display:
        st.info(f"Bu filtrede (`{_jf}`) gösterilecek job yok.", icon="🔍")
    else:
        # Sort key gösterilen tarihle aynı olsun — yoksa sort ve display
        # farklı timestamp'e bakar (ör. created_at vs started_at) ve user'a
        # tutarsız görünür. "En son aktif" tarihi tercih ediyoruz: finished_at
        # > started_at > created_at. Bu sayede yeni başlamış/bitmiş job'lar
        # her zaman tepede olur.
        def _job_sort_ts(j: "Job") -> float:
            return j.finished_at or j.started_at or j.created_at

        jobs_sorted = sorted(jobs_display, key=_job_sort_ts, reverse=True)

        # Kolon sırası: TARİH en sola alındı (user'ın isteği — kronolojik
        # tarama için ana gözlem ekseni). Sırasıyla:
        # TARİH | DURUM | BAŞLIK | PROFİL | SÜRE | NOTEBOOK/HATA | İŞLEM
        _col_widths = [1.2, 1.1, 3.2, 1.6, 0.9, 2.6, 1.2]

        st.markdown('<div class="job-row-wrap">', unsafe_allow_html=True)
        # Header (geniş ekranda görünür, dar ekranda CSS gizler)
        st.markdown('<div class="job-header">', unsafe_allow_html=True)
        h = st.columns(_col_widths)
        h[0].markdown("<small style='opacity:0.7; font-weight:600;'>TARİH</small>", unsafe_allow_html=True)
        h[1].markdown("<small style='opacity:0.7; font-weight:600;'>DURUM</small>", unsafe_allow_html=True)
        h[2].markdown("<small style='opacity:0.7; font-weight:600;'>BAŞLIK</small>", unsafe_allow_html=True)
        h[3].markdown("<small style='opacity:0.7; font-weight:600;'>PROFİL / GÖNDEREN</small>", unsafe_allow_html=True)
        h[4].markdown("<small style='opacity:0.7; font-weight:600;'>SÜRE</small>", unsafe_allow_html=True)
        h[5].markdown("<small style='opacity:0.7; font-weight:600;'>NOTEBOOK / HATA</small>", unsafe_allow_html=True)
        h[6].markdown("<small style='opacity:0.7; font-weight:600;'>İŞLEM</small>", unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

        for j in jobs_sorted:
            cs = st.columns(_col_widths)
            # cs[0]: TARİH (en sol)
            _ts = _job_sort_ts(j)
            cs[0].markdown(f"<span style='font-size:0.85rem;'>{fmt_datetime(_ts)}</span>", unsafe_allow_html=True)
            # cs[1]: DURUM
            cs[1].markdown(status_pill(j.status), unsafe_allow_html=True)
            # cs[2]: BAŞLIK
            title_short = j.title if len(j.title) <= 90 else j.title[:89] + "…"
            cs[2].markdown(
                f'<div style="font-size:0.92rem; line-height:1.35;" title="{_esc(j.title)}">{_esc(title_short)}</div>',
                unsafe_allow_html=True,
            )
            # cs[3]: PROFİL / GÖNDEREN
            profile_short = (j.profile_name or "—")
            if len(profile_short) > 22:
                profile_short = profile_short[:21] + "…"
            submitter = j.submitted_by or ""
            submitter_html = (
                f'<div style="font-size:0.74rem; opacity:0.65; margin-top:2px;">'
                f'gönderen: <b>{_esc(submitter)}</b></div>'
            ) if submitter else ""
            cs[3].markdown(
                f'<div style="font-size:0.85rem; opacity:0.85;" title="{_esc(j.profile_name)}">{_esc(profile_short)}</div>'
                f'{submitter_html}',
                unsafe_allow_html=True,
            )
            # cs[4]: SÜRE
            cs[4].markdown(f"<span style='font-size:0.85rem;'>{fmt_duration(j.started_at, j.finished_at)}</span>", unsafe_allow_html=True)

            with cs[5]:
                if j.notebook_url:
                    st.markdown(
                        f'<a href="{j.notebook_url}" target="_blank" '
                        f'style="font-size:0.84rem; text-decoration:none;">🔗 Notebook aç</a>',
                        unsafe_allow_html=True,
                    )
                # Phase E: Custom Prompt (NotebookLM Cinematic'e gönderilen)
                if j.custom_prompt:
                    with st.expander("📋 Custom Prompt (Cinematic'e gönderilen)", expanded=False):
                        st.text(j.custom_prompt[:8000])

                # Phase B/C: Asset listesi + seçili görseller
                if j.assets:
                    n_selected = sum(1 for a in j.assets if a.get("selected_image"))
                    label = f"🖼 Görseller · {len(j.assets)} öneri"
                    if n_selected:
                        label += f" · ✅ {n_selected} seçili"
                    with st.expander(label, expanded=False):
                        for k, a in enumerate(j.assets):
                            style = a.get("style", "photo")
                            icon = {
                                "photo": "📷", "illustration": "🎨",
                                "diagram": "📊", "archive": "🗄"
                            }.get(style, "📷")
                            pos = a.get("position", "")[:80]
                            desc = a.get("description", "")
                            query = a.get("query", "")
                            sel = a.get("selected_image")
                            sel_html = ""
                            if sel:
                                src_emoji = {"wikimedia": "🌐", "openverse": "🎨"}.get(
                                    sel.get("source", ""), "🖼"
                                )
                                thumb = sel.get("thumb_url", "")
                                lic = sel.get("license", "?")
                                sel_html = (
                                    f'<div style="display:flex; gap:8px; align-items:center; '
                                    f'margin-top:6px; padding:6px; background:rgba(16,185,129,0.08); '
                                    f'border-left:3px solid #10B981; border-radius:4px;">'
                                    f'<img src="{_esc(thumb)}" style="width:64px; height:48px; '
                                    f'object-fit:cover; border-radius:4px;" '
                                    f'onerror="this.style.display=\'none\'"/>'
                                    f'<div style="font-size:0.75rem;">'
                                    f'<b>✅ Seçili</b> · {src_emoji} {_esc(sel.get("source","?"))}<br>'
                                    f'<span style="opacity:0.7;">📜 {_esc(lic)}</span></div>'
                                    f'</div>'
                                )
                            st.markdown(
                                f'<div style="margin-bottom:8px; padding:8px 10px; '
                                f'background:rgba(99,102,241,0.05); border-radius:6px;">'
                                f'<div style="font-size:0.78rem; opacity:0.65; '
                                f'margin-bottom:2px;">{icon} #{k+1} · '
                                f'<i>{_esc(pos)}</i></div>'
                                f'<div style="font-size:0.85rem;">{_esc(desc)}</div>'
                                f'<div style="font-size:0.75rem; opacity:0.7; '
                                f'margin-top:4px; font-family:monospace;">'
                                f'🔍 <code>{_esc(query)}</code></div>'
                                f'{sel_html}'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

                # Script audit trail — kullanıcının iterasyon geçmişi
                # (Phase A: original_script + iterations[])
                if j.iterations or j.original_script:
                    n_iter = len(j.iterations or [])
                    label = (f"📜 Senaryo geçmişi · {n_iter} AI iterasyonu"
                             if n_iter else "📜 Senaryo (hiç iterasyon yok)")
                    with st.expander(label, expanded=False):
                        # Original (yapıştırılan)
                        if j.original_script and j.original_script != j.text:
                            st.markdown(
                                "<small style='opacity:0.7; font-weight:600;'>"
                                "ORİJİNAL (kullanıcının yapıştırdığı)</small>",
                                unsafe_allow_html=True,
                            )
                            st.text(j.original_script[:5000])
                            st.markdown("---")
                        # Iterations
                        for k, it in enumerate(j.iterations or []):
                            mdl = (it.get("model") or "?").split("/")[-1].replace(":free", "")
                            st.markdown(
                                f"<small style='opacity:0.7; font-weight:600;'>"
                                f"v{k+1} · <code>{_esc(mdl)}</code></small>",
                                unsafe_allow_html=True,
                            )
                            fb = it.get("feedback", "").strip()
                            if fb:
                                st.markdown(
                                    f"<div style='font-size:0.8rem; opacity:0.85; "
                                    f"padding:6px 10px; background:rgba(99,102,241,0.08); "
                                    f"border-left:2px solid #6366F1; border-radius:4px; "
                                    f"margin-bottom:4px;'>💬 {_esc(fb)}</div>",
                                    unsafe_allow_html=True,
                                )
                            with st.expander(f"v{k+1} senaryosu", expanded=False):
                                st.text(it.get("script", "")[:5000])
                            st.markdown("&nbsp;", unsafe_allow_html=True)
                        # Final (NotebookLM'e giden)
                        st.markdown(
                            "<small style='opacity:0.7; font-weight:600;'>"
                            "FİNAL (NotebookLM'e gönderilen)</small>",
                            unsafe_allow_html=True,
                        )
                        st.text(j.text[:5000])
                # Azure URL — admin için copy butonlu görünüm.
                # st.code() köşesinde built-in clipboard butonu var.
                if j.video_remote_url:
                    with st.expander("☁️ Azure URL", expanded=False):
                        st.code(j.video_remote_url, language=None)
                # Lokal NotebookLM video URL'i — short-lived ama referans için
                elif j.video_url:
                    with st.expander("▶ Video URL (NotebookLM CDN)", expanded=False):
                        st.code(j.video_url, language=None)
                        st.caption("Bu URL signed/short-lived olabilir — paylaşım için Azure URL daha güvenilir.")
                if j.error:
                    is_quota = _is_quota_error(j.error)
                    if is_quota:
                        st.markdown(
                            f'<div style="font-size:0.78rem; color:#991B1B; '
                            f'background:#FEE2E2; padding:4px 8px; border-radius:6px; '
                            f'margin-top:4px; border-left:3px solid #EF4444;">'
                            f'🚫 <b>Kota dolu</b> — {_esc(j.error[:180])}</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            f'<div style="font-size:0.78rem; opacity:0.7; margin-top:2px;">'
                            f'⚠ {_esc(j.error[:160])}</div>',
                            unsafe_allow_html=True,
                        )

            with cs[6]:
                # Video varsa video butonu öncelikli
                if j.video_remote_url:
                    st.markdown(
                        f'<a href="{j.video_remote_url}" target="_blank" '
                        f'style="display:block; text-align:center; padding:5px 8px; '
                        f'background:#10B981; color:white; border-radius:6px; '
                        f'text-decoration:none; font-size:0.78rem; font-weight:600;">'
                        f'☁️ Cloud</a>',
                        unsafe_allow_html=True,
                    )
                elif j.video_url:
                    st.markdown(
                        f'<a href="{j.video_url}" target="_blank" '
                        f'style="display:block; text-align:center; padding:5px 8px; '
                        f'background:#6366F1; color:white; border-radius:6px; '
                        f'text-decoration:none; font-size:0.78rem; font-weight:600;">'
                        f'▶ Video</a>',
                        unsafe_allow_html=True,
                    )
                elif j.notebook_url and j.status in TERMINAL_STATUSES:
                    st.link_button("🌐 Aç", j.notebook_url, help="Notebook'u tarayıcıda aç", use_container_width=True)
                # Manuel topla: notebooklm-py path'i (harvest=skip) → resume_download
                # legacy path → eski harvest cycle tetikleme
                # 2A ile 'failed/expired' yapılmış (ama notebook_url'ü duran)
                # işler de manuel resume edilebilsin — full regen yerine ucuz
                # toplama. Aksi halde tek seçenek 'Tekrar dene' (baştan üretim).
                _can_resume = (
                    (j.status in ("generating", "done", "submitted")
                     and not j.video_local_path
                     and not j.video_url
                     and j.harvest_status not in ("checking", "uploaded", "downloaded"))
                    or (j.status == "failed" and j.harvest_status == "expired"
                        and (j.notebook_url or "").strip()
                        and not (j.video_remote_url or "").strip())
                )
                if _can_resume:
                    is_nbpy = j.harvest_status in ("skip", "expired")
                    btn_label = "🛟 Manuel topla" if is_nbpy else "🔍 Şimdi tara"
                    btn_help = (
                        "notebooklm-py path: notebook'a yeniden bağlanır, "
                        "video artifact bulunur, MP4 indirilir + Azure upload. "
                        "Cinematic gen hâlâ devam ediyorsa 30dk bekler."
                        if is_nbpy else
                        "Legacy Playwright harvest cycle'ını hemen tetikle"
                    )
                    if st.button(btn_label, key=f"harvest_{j.id}",
                                 help=btn_help, use_container_width=True):
                        if is_nbpy:
                            with st.spinner("Notebook'a bağlanılıyor, MP4 indiriliyor (30dk'ya kadar)..."):
                                ok, msg = worker.resume_via_notebooklm(j.id)
                            if ok:
                                st.toast(f"✅ {msg}", icon="✅")
                            else:
                                st.error(f"❌ {msg}")
                        else:
                            worker.trigger_harvest_now(j.id)
                            st.toast("Harvest cycle tetiklendi.", icon="🔍")
                        st.rerun()
                if j.status == "running":
                    if st.button("🛑 Durdur", key=f"stop_{j.id}", help="Bu job'ı durdur", use_container_width=True):
                        worker.stop_job(j.id)
                        st.toast("Durdurma sinyali gönderildi.", icon="🛑")
                if j.status == "failed":
                    if st.button("🔄 Tekrar dene", key=f"requeue_{j.id}",
                                 help="Bu işi kuyruğa geri al — sağlıklı bir hesaba yeniden gönderilir",
                                 use_container_width=True):
                        _requeue_job(j.id)
                        st.toast("Kuyruğa geri alındı.", icon="🔄")
                        st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    if counts.get("running", 0) > 0 or counts.get("generating", 0) > 0:
        st.markdown(
            '<div style="margin-top:1rem; padding:8px 14px; border-radius:8px; '
            'background:rgba(99,102,241,0.08); border-left:3px solid #6366F1; font-size:0.85rem;">'
            '⏱ Aktif job(lar) var — sayfa ~4 sn\'de bir otomatik yenileniyor</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("💡 Manuel yenilemek için R tuşuna basabilirsin.")


# -------------------- TAB 3: VIDEOLAR --------------------
def _open_in_finder(path: Path) -> None:
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif platform.system() == "Linux":
            subprocess.Popen(["xdg-open", str(path)])
        else:
            subprocess.Popen(["explorer", str(path)])
    except Exception as e:
        st.error(f"Klasör açılamadı: {e}")


with tab_videos:
    section_header("🎬 İndirilmiş videolar", str(DOWNLOADS_DIR.relative_to(APP_DIR)))
    st.markdown(
        '<div style="font-size:0.85rem; opacity:0.75; margin-bottom:0.6rem;">'
        '💡 NotebookLM video üretimi bittikten sonra notebook\'u tarayıcıda aç → '
        'Studio panelden videoyu bu klasöre indir.</div>',
        unsafe_allow_html=True,
    )

    if st.button("📂 Finder'da klasörü aç", use_container_width=False):
        _open_in_finder(DOWNLOADS_DIR)

    videos = sorted(DOWNLOADS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not videos:
        empty_state(
            "🎬",
            "Henüz indirilmiş video yok",
            f"Notebook URL'lerini Durum sekmesinde bulabilirsin. "
            f"Manuel indirip {DOWNLOADS_DIR.name}/ klasörüne koy.",
        )
    else:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        for v in videos:
            with st.container(border=True):
                size_mb = v.stat().st_size / (1024 * 1024)
                mtime = datetime.fromtimestamp(v.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                cs = st.columns([5, 1.2])
                cs[0].markdown(
                    f'<div style="font-weight:600; font-size:0.95rem; word-break:break-all;">'
                    f'🎞️ {v.name}</div>'
                    f'<div style="font-size:0.78rem; opacity:0.65; margin-top:2px;">'
                    f'{size_mb:.1f} MB · {mtime}</div>',
                    unsafe_allow_html=True,
                )
                with cs[1]:
                    try:
                        with v.open("rb") as fh:
                            st.download_button(
                                "⬇️ İndir",
                                data=fh.read(),
                                file_name=v.name,
                                mime="video/mp4",
                                key=f"dl_{v.name}",
                                use_container_width=True,
                            )
                    except OSError:
                        st.caption("okuma hatası")


# -------------------- TAB: ÜRETİM (hesap × gün) --------------------
def render_production_stats() -> None:
    """Hesap × gün üretilen (done) video matrisi — canlı, jobs.json'dan.
    Sunucu saati Türkiye (UTC+3) olduğu için gün ayrımı doğrudan local date."""
    from collections import defaultdict, Counter
    section_header("📈 Üretim istatistikleri",
                   "Hangi hesap hangi gün kaç video üretti (tamamlanan)")
    jobs = load_jobs()
    profiles = load_profiles()

    def _short(name: str) -> str:
        n = (name or "—").split("@")[0]
        for s in (".ho", ".twin.ai"):
            n = n.replace(s, "")
        return n or "—"

    current_names = {(p.name or "") for p in profiles}
    cell: dict = defaultdict(int)
    acct_tot: Counter = Counter()
    day_tot: Counter = Counter()
    days_set: set = set()
    accts_set: set = set()
    for j in jobs:
        if j.status != "done":
            continue
        ts = j.finished_at or j.started_at or j.created_at
        if not ts:
            continue
        try:
            d = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            continue
        raw = j.profile_name or "—"
        key = _short(raw) if raw in current_names else "eski"
        cell[(d, key)] += 1
        acct_tot[key] += 1
        day_tot[d] += 1
        days_set.add(d)
        accts_set.add(key)

    if not acct_tot:
        empty_state("📈", "Henüz üretim yok",
                    "Tamamlanan video oldukça burada görünecek.")
        return

    cols = sorted([a for a in accts_set if a != "eski"], key=lambda a: -acct_tot[a])
    if "eski" in accts_set:
        cols.append("eski")
    all_days = sorted(days_set)
    days = sorted(days_set, reverse=True)[:45]  # en yeni üstte, son 45 gün
    today = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
    grand = sum(acct_tot.values())

    mc = st.columns(4)
    top_acct = cols[0] if cols else "—"
    mc[0].metric("Toplam video", grand)
    mc[1].metric("Aktif hesap", len([a for a in cols if a != "eski"]))
    mc[2].metric("Bugün", day_tot.get(today, 0))
    mc[3].metric(f"En çok · {top_acct}", acct_tot.get(top_acct, 0))

    def _c(v):
        a = min(0.75, 0.1 + 0.09 * v) if v else 0
        bg = f"background:rgba(139,48,232,{a:.2f});" if v else ""
        return (f'<td style="text-align:center;font-size:0.78rem;padding:4px 0;'
                f'border-radius:4px;{bg}">{v or "·"}</td>')

    h = ['<div style="overflow-x:auto;"><table style="border-collapse:separate;'
         'border-spacing:2px;font-size:0.78rem;width:100%;">'
         '<thead><tr><th style="text-align:left;padding:0 6px;font-weight:600;'
         'opacity:0.7;">Gün</th>']
    for c in cols:
        h.append(f'<th style="font-weight:600;opacity:0.7;" title="{_esc(c)}">'
                 f'{_esc(c[:7])}</th>')
    h.append('<th style="font-weight:700;">Σ</th></tr></thead><tbody>')
    for d in days:
        label = d[5:].replace("-", "/") + (" •" if d == today else "")
        h.append(f'<tr><td style="white-space:nowrap;opacity:0.7;padding:0 6px;">'
                 f'{label}</td>')
        for c in cols:
            h.append(_c(cell.get((d, c), 0)))
        h.append(f'<td style="text-align:center;font-weight:600;">'
                 f'{day_tot.get(d, 0)}</td></tr>')
    h.append('<tr><td style="padding:6px;font-weight:700;border-top:1px solid '
             'rgba(0,0,0,0.12);">Σ</td>')
    for c in cols:
        h.append(f'<td style="text-align:center;font-weight:700;border-top:1px '
                 f'solid rgba(0,0,0,0.12);">{acct_tot.get(c, 0)}</td>')
    h.append(f'<td style="text-align:center;font-weight:700;border-top:1px solid '
             f'rgba(0,0,0,0.12);">{grand}</td></tr></tbody></table></div>')
    st.markdown("".join(h), unsafe_allow_html=True)
    st.caption("Renk koyuluğu = o gün o hesabın ürettiği video. 'eski' = artık "
               "kullanılmayan/test profil adları. Bugün (•) hâlâ devam ediyor.")

    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(["gün"] + cols + ["toplam"])
    for d in all_days:
        w.writerow([d] + [cell.get((d, c), 0) for c in cols] + [day_tot.get(d, 0)])
    w.writerow(["TOPLAM"] + [acct_tot.get(c, 0) for c in cols] + [grand])
    st.download_button("⬇️ CSV indir (tüm günler)", buf.getvalue(),
                       "uretim_hesap_gun.csv", "text/csv")


with tab_uretim:
    render_production_stats()


# -------------------- TAB 4: LOG --------------------
with tab_log:
    section_header("📜 Loglar", "subprocess çıktıları + launcher")

    if st.button("📂 Log klasörünü aç"):
        _open_in_finder(LOGS_DIR)

    log_files: list[Path] = []
    if LAUNCHER_LOG.exists():
        log_files.append(LAUNCHER_LOG)
    log_files.extend(sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True))
    log_files = list({p.resolve(): p for p in log_files}.values())

    if not log_files:
        empty_state("📜", "Henüz log yok", "Bir job çalıştığında burada görünür.")
    else:
        choice = st.selectbox(
            "Log dosyası",
            options=log_files,
            format_func=lambda p: f"{p.name}  ({p.stat().st_size // 1024}KB)",
        )
        if choice and choice.exists():
            try:
                content = choice.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                trimmed = False
                if len(lines) > JOB_LOG_TAIL_LINES:
                    content = "\n".join(lines[-JOB_LOG_TAIL_LINES:])
                    trimmed = True
                if trimmed:
                    st.caption(f"📌 Son {JOB_LOG_TAIL_LINES} satır gösteriliyor (toplam {len(lines)})")
                st.code(content, language="log")
            except OSError as e:
                st.error(f"Log okunamadı: {e}")


# -------------------- TAB 5: KULLANICILAR --------------------
with tab_users:
    section_header("👥 Kullanıcılar", "Login + rol yönetimi")
    me = current_user()

    users = load_users()

    # Mevcut kullanıcı listesi
    for u in users:
        is_self = me and u.username.lower() == me.get("username", "").lower()
        with st.container(border=True):
            cs = st.columns([2.5, 1, 2, 1, 1])
            with cs[0]:
                st.markdown(
                    f'<div style="font-weight:600;">{_esc(u.display_name or u.username)}</div>'
                    f'<div style="font-size:0.78rem; opacity:0.7;">@{_esc(u.username)}</div>',
                    unsafe_allow_html=True,
                )
            with cs[1]:
                role_color = "#7C3AED" if u.role == "admin" else "#059669"
                st.markdown(
                    f'<span style="display:inline-block; padding:3px 10px; '
                    f'background:{role_color}1A; color:{role_color}; border-radius:999px; '
                    f'font-size:0.78rem; font-weight:600;">{u.role.upper()}</span>',
                    unsafe_allow_html=True,
                )
            with cs[2]:
                created = datetime.fromtimestamp(u.created_at).strftime("%Y-%m-%d %H:%M") if u.created_at else "—"
                st.markdown(f'<div style="font-size:0.78rem; opacity:0.65;">Oluşturuldu: {created}</div>', unsafe_allow_html=True)
            with cs[3]:
                if st.button("🔑", key=f"rotate_{u.username}", help="Şifre değiştir", use_container_width=True):
                    st.session_state[f"rotating_{u.username}"] = True
            with cs[4]:
                if is_self:
                    st.markdown('<div style="font-size:0.7rem; opacity:0.55; text-align:center; padding-top:8px;">SEN</div>', unsafe_allow_html=True)
                else:
                    if st.button("🗑", key=f"del_user_{u.username}", help="Kullanıcıyı sil", use_container_width=True):
                        new_list = [x for x in load_users() if x.username != u.username]
                        save_users(new_list)
                        st.toast(f"{u.username} silindi.", icon="🗑️")
                        st.rerun()

            # Şifre değiştirme form (toggle)
            if st.session_state.get(f"rotating_{u.username}"):
                with st.form(f"rotate_form_{u.username}"):
                    st.caption(f"@{u.username} için yeni şifre")
                    new_pw = st.text_input("Yeni şifre", type="password", key=f"newpw_{u.username}")
                    cs2 = st.columns(2)
                    with cs2[0]:
                        if st.form_submit_button("Kaydet", type="primary", use_container_width=True):
                            if len(new_pw) < 6:
                                st.error("Şifre en az 6 karakter olmalı.")
                            else:
                                ulist = load_users()
                                for x in ulist:
                                    if x.username == u.username:
                                        x.password_hash = hash_password(new_pw)
                                        break
                                save_users(ulist)
                                st.session_state[f"rotating_{u.username}"] = False
                                st.toast("Şifre güncellendi.", icon="✅")
                                st.rerun()
                    with cs2[1]:
                        if st.form_submit_button("İptal", use_container_width=True):
                            st.session_state[f"rotating_{u.username}"] = False
                            st.rerun()

    # Yeni kullanıcı ekle
    st.markdown("&nbsp;", unsafe_allow_html=True)
    section_header("➕ Yeni kullanıcı")
    with st.form("new_user_form", clear_on_submit=True):
        cs = st.columns([2, 2])
        with cs[0]:
            new_username = st.text_input("Kullanıcı adı", placeholder="örn. mustafa")
        with cs[1]:
            new_display = st.text_input("Görünen ad (opsiyonel)", placeholder="Mustafa Şapcılı")
        cs2 = st.columns([2, 2])
        with cs2[0]:
            new_password = st.text_input("Şifre", type="password", placeholder="en az 6 karakter")
        with cs2[1]:
            new_role = st.selectbox("Rol", ["user", "admin"], index=0,
                                    help="user: sadece senaryo gönderir. admin: hesap, kullanıcı, kuyruk yönetir.")
        if st.form_submit_button("➕ Kullanıcı oluştur", type="primary", use_container_width=True):
            uname = new_username.strip().lower()
            if not uname or not new_password:
                st.error("Kullanıcı adı ve şifre zorunlu.")
            elif len(new_password) < 6:
                st.error("Şifre en az 6 karakter olmalı.")
            elif find_user(uname):
                st.error(f"@{uname} zaten var.")
            else:
                ulist = load_users()
                ulist.append(User(
                    username=uname,
                    password_hash=hash_password(new_password),
                    role=new_role,
                    display_name=new_display.strip() or uname,
                ))
                save_users(ulist)
                st.toast(f"@{uname} oluşturuldu.", icon="✅")
                st.rerun()


# ---------------------------------------------------------------------------
# Auto-refresh: SADECE running job varsa rerun. queued tek başına refresh'i
# tetiklemez (worker zaten 2 sn'de bir dispatch ediyor; running'e geçince refresh).
# Bu sayede Hazırla sekmesinde içerik girerken sayfa boşuna yenilenmez.
# Init aktifken (VNC üzerinden login bekleniyor) refresh yapma — aksi hâlde
# admin form doldurmaya çalışırken sayfa sürekli sıfırlanır.
# ---------------------------------------------------------------------------
_any_init_active = any(
    st.session_state.get(f"init_started_{_p.id}", 0) > time.time() - 600
    for _p in load_profiles()
)
jobs_now = load_jobs()
if not _any_init_active:
    _active_now = any(j.status in ("running", "submitted") for j in jobs_now)
    _generating_now = any(j.status == "generating" for j in jobs_now)
    if _active_now:
        time.sleep(4)
        st.rerun()
    elif _generating_now:
        # Uzun Cinematic gen sırasında da yenile → versiyon çipleri canlı
        # güncellensin (🎬→▶). running'den uzun interval (t3 CPU-credit için
        # ölçülü; GENERATING_REFRESH_SEC env ile ayarlanabilir).
        time.sleep(max(8, int(os.environ.get("GENERATING_REFRESH_SEC", "15"))))
        st.rerun()
