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

# Gemini CLI wrapper — text gen için kullanılır (OpenRouter yerine).
# Image gen halen Pollinations (OAuth tier'da nano-banana 404 dönüyor).
try:
    from gemini_client import (
        GeminiError,
        gemini_chat,
        gemini_smoke_test,
        GEMINI_MODELS,
        GEMINI_DEFAULT_MODEL,
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
PROFILES_FILE = DATA_DIR / "profiles.json"
DRAFTS_FILE = DATA_DIR / "drafts.json"
USERS_FILE = DATA_DIR / "users.json"
SCRIPT_DRAFTS_FILE = DATA_DIR / "script_drafts.json"  # Phase A in-progress drafts
JOB_ASSETS_DIR = DATA_DIR / "job_assets"  # Phase E: per-job indirilen görseller
STYLE_GUIDES_DIR = DATA_DIR / "style_guides"  # Phase E: admin-managed reusable source dosyaları
LAUNCHER_LOG = LOGS_DIR / "launcher.log"

# Bugünkü kullanım sayımına dahil olan job durumları. Failed da sayılır —
# yoksa kullanıcı sürekli aynı profili spam'leyip limit aşabilir.
COUNTED_STATUSES = {"running", "generating", "done", "submitted", "failed"}
TERMINAL_STATUSES = {"done", "failed", "submitted", "stopped"}
# generating = automator bitti, NotebookLM Cinematic video üretiyor; harvest bekliyoruz.
# done       = video harvest edildi (+ Azure'a yüklendi); gerçekten tamamlandı.
HARVEST_PICKUP_STATUSES = {"generating", "done"}  # geriye dönük uyum için "done" da

DISPATCH_INTERVAL_SEC = 2.0
# Profil NotebookLM kota hatası yedikten sonra kaç saat block kalır?
# Google'ın gerçek reset zamanı Pacific time (~07-08:00 UTC) — bizim UTC date
# rollover ile uyumsuz. 8h block + self-correct retry: max 8h overshoot,
# gerçek reset zamanını otomatik bulur.
QUOTA_BLOCK_HOURS = float(os.environ.get("QUOTA_BLOCK_HOURS", "8"))
JOB_LOG_TAIL_LINES = 400

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
          JOB_ASSETS_DIR, STYLE_GUIDES_DIR):
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
    # Audit trail — script iteration geçmişi (Phase A)
    original_script: str = ""        # Kullanıcının ilk yapıştırdığı versiyon (AI iterasyonundan önce)
    iterations: list = field(default_factory=list)  # [{script, feedback, model, ts}, ...]
    # Phase B: extracted assets — her görsel için {id, position, description, query, style}
    assets: list = field(default_factory=list)
    # Phase E: Custom Prompt + style guide source listesi (audit ve admin display için)
    custom_prompt: str = ""          # NotebookLM Cinematic Customize → Custom Prompt'a yapışan metin
    style_guides_used: list = field(default_factory=list)  # [filename, ...] — submit anında attach edilenler
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
    seed_pw = os.environ.get("ADMIN_PASSWORD", "").strip() or "changeme"
    seed = User(
        username="admin",
        password_hash=hash_password(seed_pw),
        role="admin",
        display_name="Admin",
    )
    save_users([seed])
    if seed_pw == "changeme":
        launcher_log(
            "⚠ Default admin oluşturuldu (username=admin, password=changeme). "
            ".env'ye ADMIN_PASSWORD koyup bu mesajdan kurtul."
        )
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


def load_jobs() -> list[Job]:
    raw = _read_json(JOBS_FILE, [])
    out: list[Job] = []
    for j in raw:
        try:
            out.append(Job(**j))
        except TypeError:
            continue
    return out


def save_jobs(jobs: list[Job]) -> None:
    _atomic_write_json(JOBS_FILE, [asdict(j) for j in jobs])


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
                      custom_prompt_edited: bool = False) -> None:
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

Task: Generate a cinematic video overview based on the provided Script and the Execution Guide.

Core Constraints (STRICT ADHERENCE REQUIRED):
1. Zero-Text Visuals: Absolutely no text, labels, or logos in the frame. Use visual metaphors or color-coding only.
2. Historical & Identity Fidelity: Use the Historical Accuracy & Identity Protocol (in Execution Guide) to ensure correct ethnicity, age, and real-world image integration.
3. Visual Harmony & Safety: Apply the Student Safety Guide (in Execution Guide) — high-key lighting, soft geometry, and lifted shadows. Avoid "AI slop" or fractal-like textures.
4. Compositional Logic: Follow the Spatial Simplicity Rule. Isolate one central subject per scene with significant white space to maintain clarity.
5. Style Ratio: 80/20 Animation-Heavy Model — 80% minimalist sketch / paper-cut-out, 20% realistic visuals. Do not mix photos and illustrations in a single frame.

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


def build_source_listing(script_title: str, assets: list) -> tuple[str, list[str]]:
    """Custom prompt'ta listelenecek source isimleri + numaralandırma.

    Sıra NotebookLM upload sırasıyla bire bir aynıdır:
      [1] _execution_guide.txt  (sabit kurallar — Text-Free + 80/20 + Safety + History)
      [2] _custom_prompt.txt    (Task brief — Role/Task/Constraints/Required Sources)
      [3] <Title>_Script.txt
      [4..N] Selected images

    Image'lar için description (TR) + position (script anı) eklenir → NotebookLM
    her görseli script'in hangi anında göstereceğini bilir.

    Returns (formatted_listing_text, ordered_source_names).
    """
    lines: list[str] = []
    names: list[str] = []

    # Source 1: Execution Guide (sabit, her job'a otomatik eklenen)
    lines.append(
        "Source 1: Execution Guide — STRICT visual rules "
        "(Text-Free / 80-20 Animation / Student Safety / Historical Accuracy). "
        "Apply these rules to EVERY scene."
    )
    names.append("_execution_guide")

    # Source 2: Task Brief (this prompt — Role/Task/Constraints; uploaded as source
    # so NotebookLM can reference it directly, redundancy with Customize field)
    lines.append(
        "Source 2: Task Brief (this document) — "
        "Role/Task/Constraints + this Required Sources list."
    )
    names.append("_custom_prompt")

    # Source 3: Script
    script_name = f"{script_title}_Script" if script_title else "Script"
    lines.append(f"Source 3: {script_name} — verbatim narration content")
    names.append(script_name)

    # Source 4+: Selected images with description + position mapping
    for i, a in enumerate(assets):
        sel = a.get("selected_image") or {}
        if not sel.get("full_url") and not sel.get("thumb_url"):
            continue
        base = _safe_filename_from_query(a.get("query", ""), fallback=f"image_{i+1}")
        full_name = f"{base}_{i+1:02d}"
        idx = len(names) + 1  # Source numarası (guide + brief + script + önceki image'lar sonrası)
        lines.append(_format_source_image_line(idx, a, full_name))
        names.append(full_name)

    return ("\n".join(lines), names)


def render_custom_prompt(template: str, script_title: str,
                          assets: list) -> str:
    """Template'teki {{SOURCES_LIST}} placeholder'ını doldur."""
    listing, _ = build_source_listing(script_title, assets)
    return template.replace("{{SOURCES_LIST}}", listing)


# ---------------------------------------------------------------------------
# Sabit (her zaman eklenen) execution guide. Her job submit'inde
# kullanıcının custom_prompt'unun ÜSTÜNE prepend edilir → NotebookLM
# Cinematic generation'a böyle geçer. jobs.json'da audit trail temiz kalır
# (sadece user'ın yazdığı kısım saklanır); guide her gen'de re-injected.
# ---------------------------------------------------------------------------
EXECUTION_GUIDE_PROMPT = """Narrative & Text-Free Execution Guide
Narration: The audio must go with similar examples to the original script or video, but must also be unique in it's own visuals design.
Zero-Text Policy: Absolutely no letters, numbers, labels, or titles are permitted in the frame.
Symbolic Replacement: Use color-coded icons, focal shifts, or zooms to highlight specific parts of a subject instead of using text labels.
Language Barrier: Do not generate any text overlays to ensure the video is ready for immediate localization.

The 80/20 Animation-Heavy Model
Style: 80% minimalist "sketch" style and "paper cut-out" animation styles, with 20% realistic visuals.
No Hybrid Clutter: Do not mix a photo and an illustration in the same frame; keep them as distinct scenes.
Dynamic Flow: Every scene must have motion (e.g., wide pans or macro photography) to avoid "static" talking heads.

Student Safety & Visual Harmony Guide
Soft Geometry: Prioritize rounded forms and curvilinear geometry; avoid jagged edges, or needle-like structures.
High-Key Lighting: Use bright lighting with lifted shadows to ensure no "dark voids" or threatening atmospheres exist.
Texture Control: Surfaces must be clean and continuous; avoid "high-frequency" details like scales, tiny bumps, or branching fractal patterns.
Anti-Clutter (Non-Tangle): Do not allow crossing lines, or chaotic textures to appear on screen.
Compositional Safety: Maintain a medium focal length and ensure a clear "exit point" in the background to prevent a feeling of claustrophobia.

Historical Accuracy & Identity Protocol
Authentic Representation Visual Fidelity: When the script identifies a specific historical or real-world figure, all visual depictions—regardless of artistic style—must accurately reflect that individual's documented ethnicity, age, and identity.
Contextual Accuracy: Any specific locations, tools, or environments mentioned must be represented in a way that respects the historical or geographical reality of the narrative.
Primary Source Integration Real-Image Mandate: For any specific real-world subject (person or place) featured in the script, the video must include at least 1-2 appearances of an actual primary source image (e.g., an authentic photograph, a verified portrait, or a contemporary document).
Strategic Placement: These authentic images should be timed to coincide with the introduction or a significant point of the subject within the narration.
Stylistic Continuity Visual Bridge: The "Illustrated/Animated" versions of a subject must maintain recognizable visual features consistent with real-life person.
Safety-Adjusted History Visual Correction: Primary source images that are naturally dark, high-contrast, or grainy must be adjusted to align with High-Key lighting standards. Lift shadows to ensure the image is clear and non-threatening for a student audience."""


def write_execution_guide_source(out_dir: Path) -> Optional[Path]:
    """Sabit execution guide'ı bir .txt dosyasına yaz ki NotebookLM'e
    **source** olarak (script.txt + image'ler gibi) upload edilebilsin.

    Custom prompt'un içine prepend etmek yerine source olarak eklemek daha
    etkili — NotebookLM source'lardan beslenir, Cinematic gen sırasında
    guide'daki kurallar her sahnede primer olarak kullanılır.

    out_dir: job_pack_dir veya benzer. Filename "_execution_guide.txt" sabit.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "_execution_guide.txt"
        p.write_text(EXECUTION_GUIDE_PROMPT, encoding="utf-8")
        return p
    except OSError:
        return None


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
    jobs = load_jobs()
    changed = 0
    for j in jobs:
        if j.status == "running" and not _pid_alive(j.pid):
            j.status = "failed"
            j.error = j.error or "Process kayboldu (stale state)"
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


def upload_to_azure(local_path: Path, job_id: str) -> tuple[bool, str, str]:
    """Returns (success, remote_url_or_empty, error_or_empty).
    SAS-based connection ise döndürdüğü URL'e SAS append'lenir →
    private container'da bile direkt browser'da oynatılabilir."""
    if not AZURE_ENABLED:
        return False, "", "azure_disabled"
    try:
        # azure-storage-blob opsiyonel — eksikse graceful fail
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError:
        return False, "", "azure-storage-blob package not installed"

    try:
        blob_name = f"{AZURE_BLOB_PREFIX.rstrip('/')}/{job_id}.mp4"
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
        # azure-storage-blob SAS-based credential ile init edildiğinde blob.url
        # zaten SAS içerebilir. Duplicate eklemekten kaçın: ?sv= veya &sig=
        # zaten varsa olduğu gibi döndür.
        if "sig=" in base_url:
            return True, base_url, ""
        sas = _extract_sas_from_conn(AZURE_CONN)
        if sas:
            # SAS-based connection: URL'e ekle → tarayıcıda direkt oynanabilir.
            # Note: Bu URL'i alan kişi SAS'in tüm scope'u (rwdla) ile erişir,
            # SAS expire'a kadar (2-yıl). Daha sıkı izolasyon için per-blob
            # read-only SAS gerekir (bu account key gerektirir, biz SAS-only).
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
        url = sel.get("full_url") or sel.get("thumb_url") or ""
        if not url:
            continue
        # Anlamlı dosya adı: indeks + asset id (sıralı upload için)
        name = f"{i+1:02d}_{a.get('id','asset')[:8]}"
        selected.append((url, name))
    if not selected:
        return []

    job_dir = JOB_ASSETS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Optional[Path]] = [None] * len(selected)
    from concurrent.futures import ThreadPoolExecutor

    def _dl(idx: int) -> None:
        url, name = selected[idx]
        p = download_image(url, job_dir, name)
        paths[idx] = p

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
# Worker — background dispatcher thread
# ---------------------------------------------------------------------------
class Worker:
    def __init__(self) -> None:
        self._stop_evt = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="nbworker", daemon=True)
        self._procs: dict[str, subprocess.Popen] = {}  # job_id -> Popen
        self._proc_lock = threading.Lock()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()
            launcher_log("Worker thread başladı.")

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
        while not self._stop_evt.is_set():
            try:
                self._auto_init_check()
                self._dispatch_round()
                self._reap_finished()
                # Harvest round'u her HARVEST_CHECK_INTERVAL_SEC'de bir çağır
                if time.time() - last_harvest_check >= HARVEST_CHECK_INTERVAL_SEC:
                    self._harvest_round()
                    last_harvest_check = time.time()
            except Exception as e:
                launcher_log(f"dispatch error: {e!r}")
            self._stop_evt.wait(DISPATCH_INTERVAL_SEC)

    def _auto_init_check(self) -> None:
        """auth.json yazılmış profilleri otomatik 'initialized=True' yap.
        Kullanıcının elle 'Login tamamlandı' butonuna basmasına gerek kalmaz.
        notebooklm_automator.py --init modunda framenavigated event'inde
        auth.json kaydediyor — biz burada onu polling'e bağlıyoruz."""
        profiles = load_profiles()
        changed = False
        for p in profiles:
            if p.initialized:
                continue
            auth_path = PROFILES_DIR / p.id / "auth.json"
            if auth_path.exists() and auth_path.stat().st_size > 100:
                p.initialized = True
                changed = True
                launcher_log(f"Auto-init: profile {p.name} ({p.id}) marked initialized.")
        if changed:
            save_profiles(profiles)

    def _today_count_for(self, jobs: list[Job], profile_id: str) -> int:
        today = date.today()
        n = 0
        for j in jobs:
            if j.profile_id != profile_id:
                continue
            if j.status not in COUNTED_STATUSES:
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
        return sum(1 for j in jobs if j.profile_id == profile_id and j.status == "running")

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
            err_lower = j.error.lower()
            if "kota" not in err_lower and "limit" not in err_lower:
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
        queued = [j for j in jobs if j.status == "queued"]
        if not queued:
            return

        # Round-robin: en eski kullanılan profil önce
        profiles.sort(key=lambda p: p.last_used)

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

        # Round-robin: profillerden sırayla bir job al
        any_dispatched = False
        idx = 0
        while queued and any(slot_map.get(p.id, 0) > 0 for p in profiles):
            p = profiles[idx % len(profiles)]
            idx += 1
            if slot_map.get(p.id, 0) <= 0:
                # Bu profil dolu, bir sonrakine geç
                if idx > len(profiles) * 2 and not any_dispatched:
                    break
                continue
            job = queued.pop(0)
            self._launch_job(job, p)
            slot_map[p.id] -= 1
            p.last_used = time.time()
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

        # 1.b) Sabit execution guide'ı source olarak ekle (her job'a otomatik)
        # NotebookLM source listesinin ilk öğesi olur — Cinematic gen sırasında
        # kuralları primer olarak kullanır. Custom prompt'tan ayrı bir source.
        guide_path = write_execution_guide_source(job_pack_dir)
        if guide_path:
            launcher_log(f"Job {job.id}: execution guide source eklendi ({guide_path.name})")

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
                          job.custom_prompt or "", log_fp, guide_path,
                          prompt_path),
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
                                 guide_path: Optional[Path] = None,
                                 prompt_path: Optional[Path] = None) -> None:
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
                # bu job'a dokunmasın (paralel cookie-fetch'i önle).
                try:
                    jobs_all = load_jobs()
                    for j in jobs_all:
                        if j.id == job_id:
                            j.harvest_status = "skip"
                            break
                    save_jobs(jobs_all)
                except Exception:
                    pass

        try:
            # 1. Source paket sırası:
            #    [0] Execution guide (sabit talimatlar — _execution_guide.txt)
            #    [1] Custom prompt brief (Role/Task/Constraints — _custom_prompt.txt)
            #    [2] Script (kullanıcının senaryosu)
            #    [3..N] Image'ler
            # Guide + custom prompt ikisi de source olarak en başta —
            # NotebookLM önce kuralları + task brief'i okur, sonra script'i,
            # sonra görselleri. Custom prompt ayrıca Cinematic Customize'a da
            # gider (redundancy → daha güçlü prime).
            source_paths: list[Path] = []
            if guide_path and guide_path.exists():
                source_paths.append(guide_path)
            if prompt_path and prompt_path.exists():
                source_paths.append(prompt_path)
            if script_path and script_path.exists():
                source_paths.append(script_path)
            for p in (image_paths or []):
                if isinstance(p, Path) and p.exists():
                    source_paths.append(p)
            if not source_paths:
                raise NotebookLMClientError(
                    "Hiç source dosyası yok (guide + prompt + script + images boş)",
                    stage="prep",
                )

            log_fp.write(
                f"## starting notebooklm-py pipeline: {len(source_paths)} sources "
                f"(guide={'yes' if guide_path else 'no'}, "
                f"prompt_brief={'yes' if prompt_path else 'no'}, "
                f"script={'yes' if script_path else 'no'}, "
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
                    video_timeout_sec=3600.0,  # 1h Cinematic Veo 3
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
                is_quota = (
                    e.stage in ("video_gen", "video_wait", "video_download")
                    and any(k in err_msg for k in (
                        "quota", "rate limit", "rate-limit", "rate_limit",
                        "daily limit", "limit reached", "exceeded",
                        "too many requests", "429",
                    ))
                )
                if is_quota:
                    log_fp.write(
                        f"## quota_exceeded detected (stage={e.stage}) → "
                        f"requeue job + mark profile blocked today\n"
                    )
                    log_fp.flush()
                    self._apply_event(job_id, {
                        "type": "quota_exceeded",
                        "raw": str(e)[:500],
                    })
                    return
                # Hata: job status=failed + auth ise profile init=False
                jobs_all = load_jobs()
                for j in jobs_all:
                    if j.id == job_id:
                        j.status = "failed"
                        j.error = f"{e.stage}: {str(e)[:280]}"
                        j.finished_at = time.time()
                        break
                save_jobs(jobs_all)
                if e.stage == "auth":
                    # Profile re-init gerek
                    try:
                        auth_p = auth_path_for(profile.id)
                        if auth_p.exists():
                            auth_p.unlink()
                        ps = load_profiles()
                        for p in ps:
                            if p.id == profile.id:
                                p.initialized = False
                                break
                        save_profiles(ps)
                    except Exception:
                        pass
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
            jobs_all = load_jobs()
            for j in jobs_all:
                if j.id == job_id:
                    j.notebook_url = result["notebook_url"]
                    j.video_url = ""  # notebooklm-py local download, CDN URL yok
                    try:
                        j.video_local_path = str(
                            local_mp4.resolve().relative_to(APP_DIR)
                        )
                    except (ValueError, OSError):
                        j.video_local_path = str(local_mp4)
                    j.harvest_status = "downloaded"
                    j.status = "done"  # video hazır, harvest gerek yok
                    j.finished_at = time.time()
                    break
            save_jobs(jobs_all)

            # 4. Azure upload (best-effort, fail olursa job done kalır)
            if AZURE_ENABLED and local_mp4.exists():
                log_fp.write(f"## Azure upload: {local_mp4.name}\n")
                log_fp.flush()
                ok, remote_url, err = upload_to_azure(local_mp4, job_id)
                jobs2 = load_jobs()
                for j in jobs2:
                    if j.id == job_id:
                        if ok:
                            j.video_remote_url = remote_url
                            j.harvest_status = "uploaded"
                        else:
                            j.harvest_error = f"Azure upload failed: {err}"
                        break
                save_jobs(jobs2)
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
                # Token expired ya da cookie geçersiz — Playwright init gerek
                log_fp.write(f"## auth FAIL: {e}\n")
                log_fp.flush()
                self._apply_event(job_id, {
                    "type": "automation_error",
                    "error": f"NLM auth: {str(e)[:200]}",
                })
                # Job'ı failed olarak işaretle
                jobs_all = load_jobs()
                for j in jobs_all:
                    if j.id == job_id:
                        j.status = "failed"
                        j.error = f"NLM auth: {str(e)[:200]}"
                        j.finished_at = time.time()
                        # auth.json'u sil ki "Hesabı aktive et" tekrar gözüksün
                        try:
                            auth_json.unlink()
                        except OSError:
                            pass
                        # Profile'i initialized=False yap
                        try:
                            ps = load_profiles()
                            for p in ps:
                                if p.id == profile.id:
                                    p.initialized = False
                                    break
                            save_profiles(ps)
                        except Exception:
                            pass
                        break
                save_jobs(jobs_all)
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
            # _quota_blocked_today() bu profili pas geçsin. Ama job'un kendisini
            # queued'a döndür, başka profile dispatch edilsin.
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
            # Asıl job'u queued'a geri al — Worker başka profile dene
            target.status = "queued"
            target.profile_id = ""
            target.profile_name = ""
            target.started_at = 0.0
            target.finished_at = 0.0
            target.notebook_url = ""  # eski profilin oluşturduğu notebook'u unut
            target.pid = 0
            launcher_log(f"Job {target.id} requeued: {target.profile_name} kotası dolu, başka profile denenecek.")
        elif etype in ("login_required_headless", "login_timeout"):
            target.error = (
                "Hesap login süresi geçmiş veya hiç yapılmamış. "
                "Yöneticinin admin panelinden 'Yeniden giriş' yapması gerek."
            )
            # Profili initialized=False yap ki dispatch tekrar denemesin
            try:
                ps = load_profiles()
                for p in ps:
                    if p.id == target.profile_id:
                        p.initialized = False
                        # auth.json'u sil ki yenisi yazılana kadar geçerli sayılmasın
                        auth = PROFILES_DIR / p.id / "auth.json"
                        if auth.exists():
                            try:
                                auth.unlink()
                            except OSError:
                                pass
                        break
                save_profiles(ps)
            except Exception:
                pass
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
                    ok, remote_url, err = upload_to_azure(full_path, job_id)
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
:root {
  --nlm-primary: #6366F1;
  --nlm-primary-dark: #4F46E5;
  --nlm-bg-elev: rgba(0,0,0,0.03);
  --nlm-border: rgba(0,0,0,0.08);
  --nlm-radius: 12px;
}

/* Genel container — daha geniş + biraz nefes */
.block-container {
  padding-top: 1.4rem !important;
  padding-bottom: 4rem !important;
  max-width: 1400px !important;
}

/* Hero başlık */
.app-hero {
  padding: 1.1rem 1.4rem;
  border-radius: var(--nlm-radius);
  background: linear-gradient(135deg, #1F2937 0%, #312E81 60%, #6366F1 130%);
  color: #fff;
  margin: 0 0 1.2rem 0;
  box-shadow: 0 6px 24px rgba(99,102,241,0.18);
}
.app-hero h1 {
  margin: 0; padding: 0; line-height: 1.2;
  font-size: 1.55rem; font-weight: 700; letter-spacing: -0.01em;
}
.app-hero p {
  margin: 0.35rem 0 0 0; opacity: 0.85; font-size: 0.92rem; font-weight: 400;
}

/* Section header */
.section-h {
  display: flex; align-items: center; gap: 0.55rem;
  font-size: 1.1rem; font-weight: 600; letter-spacing: -0.01em;
  margin: 0.4rem 0 0.6rem 0;
}
.section-h .section-sub {
  font-size: 0.82rem; font-weight: 400; opacity: 0.7; margin-left: auto;
}

/* Status pill */
.pill {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 0.74rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  border: 1px solid transparent;
  white-space: nowrap;
  vertical-align: middle;
}
.pill-queued     { background: #FEF3C7; color: #92400E; border-color: #FBBF24; }
.pill-running    { background: #DBEAFE; color: #1E3A8A; border-color: #60A5FA; }
.pill-generating { background: #EDE9FE; color: #5B21B6; border-color: #A78BFA; }
.pill-done       { background: #D1FAE5; color: #065F46; border-color: #34D399; }
.pill-submitted  { background: #E0E7FF; color: #3730A3; border-color: #818CF8; }
.pill-failed     { background: #FEE2E2; color: #991B1B; border-color: #F87171; }
.pill-stopped    { background: #E5E7EB; color: #374151; border-color: #9CA3AF; }

@media (prefers-color-scheme: dark) {
  .pill-queued     { background: rgba(251,191,36,0.18); color: #FCD34D; }
  .pill-running    { background: rgba(96,165,250,0.18); color: #93C5FD; }
  .pill-generating { background: rgba(167,139,250,0.20); color: #C4B5FD; }
  .pill-done       { background: rgba(52,211,153,0.18); color: #6EE7B7; }
  .pill-submitted  { background: rgba(129,140,248,0.18); color: #A5B4FC; }
  .pill-failed     { background: rgba(248,113,113,0.18); color: #FCA5A5; }
  .pill-stopped    { background: rgba(156,163,175,0.20); color: #D1D5DB; }
}

/* Job satırı */
.job-row-wrap [data-testid="stHorizontalBlock"] {
  padding: 0.55rem 0.4rem;
  border-bottom: 1px solid var(--nlm-border);
  border-radius: 6px;
  transition: background 0.12s ease;
}
.job-row-wrap [data-testid="stHorizontalBlock"]:hover {
  background: var(--nlm-bg-elev);
}

/* Profil kartı sidebar — biraz daha kompakt */
section[data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"] {
  border-radius: 10px !important;
}

/* Metric'ler biraz daha büyük */
[data-testid="stMetricValue"] {
  font-size: 1.7rem !important;
  font-weight: 700 !important;
}
[data-testid="stMetricLabel"] {
  font-weight: 500 !important;
  opacity: 0.85;
}

/* Tab başlıkları daha rahat */
button[data-baseweb="tab"] {
  font-weight: 600 !important;
  padding: 0.6rem 1.1rem !important;
}

/* Buton tıklamaları biraz daha rahat hissetsin */
.stButton button {
  border-radius: 8px !important;
  font-weight: 500;
  transition: transform 0.06s ease;
}
.stButton button:active {
  transform: translateY(1px);
}

/* Empty state */
.empty-state {
  text-align: center;
  padding: 2.5rem 1rem;
  border: 2px dashed var(--nlm-border);
  border-radius: var(--nlm-radius);
  opacity: 0.75;
}
.empty-state .es-icon { font-size: 2.2rem; margin-bottom: 0.4rem; }
.empty-state .es-title { font-weight: 600; margin-bottom: 0.25rem; }
.empty-state .es-sub { font-size: 0.85rem; opacity: 0.8; }

/* Notebook URL kısaltma */
.url-truncate {
  display: inline-block; max-width: 100%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
}

/* RESPONSIVE: dar ekranda kolon stack'le */
@media (max-width: 900px) {
  .app-hero h1 { font-size: 1.25rem; }
  .app-hero p  { font-size: 0.82rem; }
  /* Job tablosundaki başlık satırını gizle, kart görünümü */
  .job-header { display: none !important; }
  .block-container { padding-left: 0.8rem !important; padding-right: 0.8rem !important; }
}
@media (max-width: 720px) {
  /* Streamlit kolonlarını alt alta diz */
  div[data-testid="stHorizontalBlock"] {
    flex-wrap: wrap !important;
    gap: 0.4rem !important;
  }
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
    flex: 1 1 100% !important;
    width: 100% !important;
    min-width: unset !important;
  }
  /* Metric'ler 2'şerli grid'e */
  [data-testid="stMetric"] { padding: 0.4rem 0.6rem; }
}

/* Sidebar başlıkları */
.sidebar-section {
  font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em;
  font-weight: 600; opacity: 0.65; margin: 0.7rem 0 0.4rem 0;
}
</style>
"""
st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)

# Worker'ı modül yüklemesinde başlat
worker = get_worker()


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


# ---------------------------------------------------------------------------
# Auth: session_state.auth ile login/logout, rol-tabanlı routing.
# Refresh sonrası session korunsun diye URL'de short-lived token tutuyoruz;
# token → auth mapping'i process memory'sinde (servis restart'ta sıfırlanır).
# ---------------------------------------------------------------------------
import secrets  # noqa: E402


# Streamlit her sayfa render'ında app.py'yi yeniden exec ediyor → module-level
# dict her seferinde sıfırlanıyor. @st.cache_resource ile bağlayıp Streamlit'in
# process'i boyunca tek bir paylaşımlı dict tut.
@st.cache_resource
def _token_store() -> dict[str, dict]:
    return {}


def _issue_session_token(auth: dict) -> str:
    token = secrets.token_urlsafe(24)
    _token_store()[token] = auth
    return token


def _lookup_session_token(token: str) -> Optional[dict]:
    return _token_store().get(token) if token else None


def _revoke_session_token(token: str) -> None:
    _token_store().pop(token, None)


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
                    user = authenticate(username, password)
                    if user is None:
                        st.error("Kullanıcı adı veya şifre hatalı.")
                    else:
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
        '⚙ Buraya yüklediğin dosyalar (Identity Protocol, Visual Harmony Guide, '
        '80/20 Model, Narrative Execution Guide gibi) <b>her job\'da</b> '
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
        "Bir Drive klasöründeki tüm .docx dosyalarını otomatik script yap, queue'ya at",
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

    # Drive Toplu default: tek bir script + guide kullanıldığında {{SOURCES_LIST}}
    # bulk submit anında render edilemiyor (asset listesi yok). Statik versiyon —
    # NotebookLM template'i yine kuralları source'tan okur, sadece prompt'ta
    # source numaralandırması generic kalır.
    _default_template = (
        DEFAULT_CUSTOM_PROMPT_TEMPLATE
        .replace(
            "{{SOURCES_LIST}}",
            "Source 1: Execution Guide — STRICT visual rules "
            "(Text-Free / 80-20 Animation / Student Safety / Historical Accuracy). "
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
        "🔒 Sabit talimatlar (Text-Free / 80-20 Animation / Student Safety / "
        "Historical Accuracy) her job'a **ekstra source** olarak otomatik "
        "yüklenir (`_execution_guide.txt`). Custom prompt template'ine ekleme "
        "değil — NotebookLM kuralları source'tan okur, her sahnede primer "
        "olarak uygular.",
        icon="ℹ️",
    )
    with st.expander("👁 Sabit talimatları gör (read-only)"):
        st.code(EXECUTION_GUIDE_PROMPT, language=None)

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
                    from bulk_import import list_drive_folder_docx, get_docx_metadata
                    import tempfile
                    _tmp = Path(tempfile.mkdtemp(prefix="bulk_preview_"))
                    docx_paths = list_drive_folder_docx(drive_url, _tmp)
                    items = []
                    for p in docx_paths:
                        md = get_docx_metadata(p)
                        items.append({
                            "path": str(p),
                            "name": p.name,
                            "size": p.stat().st_size,
                            "modified": md.get("modified"),
                            "created": md.get("created"),
                            "author": md.get("author", ""),
                            "n_paragraphs": md.get("n_paragraphs", 0),
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
                f"✓ **{preview['count']}** adet .docx · "
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
                    st.markdown(
                        f"<div style='font-size:0.88rem; font-weight:500;'>"
                        f"📄 {it['name']}</div>",
                        unsafe_allow_html=True,
                    )
                with cs[2]:
                    mod = _fmt_dt(it.get("modified"))
                    author = it.get("author") or "—"
                    st.markdown(
                        f"<div style='font-size:0.78rem; opacity:0.75;'>"
                        f"📅 {mod} &nbsp;·&nbsp; ✍️ {author[:30]}</div>",
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
        _days = max(1, -(-n_selected // _daily_cap))
        st.caption(
            f"📅 Kapasite: {len(_profs_init)} profil × "
            f"{(_daily_cap // len(_profs_init)) if _profs_init else 0}/gün "
            f"= **{_daily_cap} job/gün** → **~{_days} gün**'de biter "
            f"({n_selected} seçili dosya için)"
        )

    # ---- Submit handler ----
    if submit_btn:
        folder_id = bulk_extract_folder_id(drive_url) if drive_url else None
        if not folder_id:
            st.error("Geçerli Drive klasör URL/ID değil.")
        elif not prompt_template.strip():
            st.error("Custom prompt boş olamaz.")
        else:
            submitter = _user_name() or "bulk"

            def _job_factory(title, text, custom_prompt, submitted_by):
                return {
                    "id": uuid.uuid4().hex[:12],
                    "title": title,
                    "text": text,
                    "custom_prompt": custom_prompt,
                    "submitted_by": submitted_by,
                    "status": "queued",
                    "assets": [],
                    "created_at": time.time(),
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
                    # Checkbox'tan seçili olanları al
                    sel_paths = []
                    for it in cached["items"]:
                        sel_key = f"{key_prefix}_bulk_sel_{it['name']}"
                        if st.session_state.get(sel_key, True):
                            p = Path(it["path"])
                            if p.exists():
                                sel_paths.append(p)
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
                for jd in result["created"]:
                    try:
                        j = Job(
                            id=jd["id"],
                            title=jd["title"],
                            text=jd["text"],
                            profile_id="",
                            profile_name="",
                            status=jd["status"],
                            submitted_by=jd["submitted_by"],
                            created_at=jd["created_at"],
                            custom_prompt=jd["custom_prompt"],
                        )
                        j.assets = []
                        created_jobs.append(j)
                    except Exception as e:
                        result["errors"].append(
                            (jd.get("title", "?"), f"Job dataclass error: {e}")
                        )
                existing.extend(created_jobs)
                save_jobs(existing)
                progress_box.empty()
                st.success(
                    f"✅ {len(created_jobs)} job kuyruğa eklendi "
                    f"(toplam {result['total_files']} dosya, "
                    f"{len(result['errors'])} hatalı)."
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
    profiles = load_profiles()
    jobs = load_jobs()
    today = date.today()

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
        status_line = "Tüm hesaplar bugün kotaya doldu — yarın resetlenir"
    else:
        status_line = f"{len(available_profiles)} hesap hazır · bugün {today_total} video tetiklendi"

    hero("Senaryonu Gönder, Video Üretelim", status_line)

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
                    f'{(_target_job.title or "(başlıksız)")[:80]}</span><br>'
                    f'<a href="{_target_job.video_remote_url}" target="_blank" '
                    f'style="font-size:0.8rem;">☁️ Mevcut videoyu oynat</a>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # Session_state key — modal kapatınca temizle
                if "revize_instructions" not in st.session_state:
                    st.session_state["revize_instructions"] = ""

                def _cb_revize_cancel() -> None:
                    st.session_state["revize_target_id"] = ""
                    st.session_state["revize_instructions"] = ""

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
                        parent_job_id=parent.id,
                        revision_instructions=instr,
                        revision_video_url=parent.video_remote_url,
                    )
                    jobs_all = load_jobs()
                    jobs_all.append(new_job)
                    save_jobs(jobs_all)
                    st.session_state["revize_target_id"] = ""
                    st.session_state["revize_instructions"] = ""
                    st.session_state["_script_msg"] = (
                        "ok", f"Revize kuyruğa eklendi: {rev_title[:50]}"
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
        """Phase D: Pollinations.ai ile 4 varyant üret, candidates'e koy."""
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
            model = st.session_state.get(f"asset_genmodel_{asset_id}") or "flux"
            style = a.get("style", "photo")
            results = generate_images(prompt, count=4, model=model, style=style)
            if not results:
                st.session_state["_script_msg"] = ("err", "Üretim başlatılamadı.")
                return
            a["candidates"] = results
            a["search_done_at"] = time.time()
            st.session_state["_script_msg"] = (
                "ok", f"{len(results)} AI varyant hazır. Yüklemesi 5-15 sn sürebilir."
            )
            _persist_draft()
            break

    # --- Phase E (custom prompt) callbacks ---
    def _cb_autofill_prompt() -> None:
        """Mevcut script title + selected assets'ten template doldur (image-script mapping)."""
        title = derive_title(st.session_state.get("script_draft", "")) or "Untitled"
        assets_full = st.session_state.get("script_assets", []) or []
        # Sadece selected_image olan asset'leri prompt'a koy
        sel_assets = [a for a in assets_full if a.get("selected_image")]
        rendered = render_custom_prompt(
            DEFAULT_CUSTOM_PROMPT_TEMPLATE, title, sel_assets
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
            '1️⃣ Senaryonu Hazırla</div>'
            '<div style="font-size:0.8rem; opacity:0.7; margin-bottom:0.5rem;">'
            'Aşağıya ya hazır <b>script</b>\'i yapıştır → <b>Çıktıyı kullan</b>, '
            'ya da <b>prompt</b>\'u yapıştır → <b>Çıktı oluştur</b> (LLM script üretir). '
            'Yardımcı: <b>🤖 Weird Facts template</b>\'ini aşağıdaki form\'la doldurabilirsin.</div>',
            unsafe_allow_html=True,
        )

        # --- Weird Facts template form (opsiyonel, kolay prompt üretme) ---
        if LLM_ENABLED:
            with st.expander("🤖 Weird Facts template kullan", expanded=False):
                st.caption(
                    "Bu form, Twin Learning Vision'ın Weird Facts script writer "
                    "prompt'unu inşa eder ve text alanına yapıştırır. Sonra "
                    "'Çıktı oluştur' butonuna bas."
                )
                wf_cs = st.columns(2)
                with wf_cs[0]:
                    st.text_input(
                        "Konu (TOPIC)",
                        key="wf_topic",
                        placeholder="örn. Işığın madde ile etkileşimi sonucunda soğurulma",
                    )
                    st.text_input(
                        "Sınıf seviyesi (GRADE)",
                        key="wf_grade",
                        placeholder="örn. 7",
                    )
                with wf_cs[1]:
                    st.selectbox(
                        "Dil",
                        options=["TR", "EN"],
                        key="wf_language",
                    )
                    st.text_area(
                        "Kazanım (LEARNING OBJECTIVE)",
                        key="wf_lo",
                        height=80,
                        placeholder="a) ...\nb) ...\nc) ...",
                    )
                st.button(
                    "📋 Template'i text alanına yapıştır",
                    on_click=_cb_apply_wf_template,
                    use_container_width=True,
                    key="btn_wf_apply",
                )

        st.text_area(
            "Senaryo / Prompt",
            height=360,
            placeholder="Senaryo veya prompt'u yapıştır...\n\n"
                        "Hazır script'in varsa direkt yapıştır + 'Çıktıyı kullan'.\n"
                        "Prompt yapıştırırsan + 'Çıktı oluştur' → LLM script üretecek.",
            label_visibility="collapsed",
            key="script_draft",
            on_change=_cb_text_changed,
        )

        # --- Step 1 action buttons ---
        if LLM_ENABLED:
            # Gemini model selector — sadece "Çıktı oluştur" için kullanılır
            model_ids = [m[0] for m in GEMINI_MODELS]
            model_labels = {m[0]: m[1] for m in GEMINI_MODELS}
            if "script_model" not in st.session_state or st.session_state["script_model"] not in model_ids:
                st.session_state["script_model"] = GEMINI_DEFAULT_MODEL

            cs_s1 = st.columns([1.2, 1.4, 1.4])
            with cs_s1[0]:
                st.selectbox(
                    "AI Model",
                    options=model_ids,
                    format_func=lambda mid: model_labels.get(mid, mid).split(" — ")[0],
                    key="script_model",
                    label_visibility="collapsed",
                    help="Flash önerilir. Pro uzun script'lerde 2-5dk sürebilir.",
                )
            with cs_s1[1]:
                st.button(
                    "🤖 Çıktı oluştur",
                    type="secondary",
                    use_container_width=True,
                    on_click=_cb_generate_output,
                    key="btn_generate_output",
                    help="Text alandaki PROMPT'u Gemini'ye gönder, script üret. "
                         "Flash ~5-30sn, Pro 2-5dk olabilir.",
                )
            with cs_s1[2]:
                st.button(
                    "✓ Çıktıyı kullan",
                    type="primary",
                    use_container_width=True,
                    on_click=_cb_use_output,
                    key="btn_use_output",
                    help="Text alandaki SCRIPT'i kabul et, Step 2'ye geç.",
                )
        else:
            # LLM kapalıysa sadece "kullan" butonu
            st.button(
                "✓ Çıktıyı kullan ve devam et",
                type="primary",
                use_container_width=True,
                on_click=_cb_use_output,
                key="btn_use_output_only",
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

    # ===== AI Editor (LLM aktifse, sadece Step 1'de görünür) =====
    # Script üretildikten sonra feedback'le ince ayar (eski Phase A iter loop).
    # Model selector Step 1 ana row'da zaten var — burada dup eklemiyoruz.
    if LLM_ENABLED and ui_step == 1:
        iter_count = len(st.session_state["script_iterations"])
        # Sadece initial-generation dışı gerçek iterasyon varsa label'da göster
        real_iters = sum(
            1 for it in st.session_state["script_iterations"]
            if it.get("feedback", "") and not it["feedback"].startswith("(initial")
        )
        expander_label = "✨ AI ile rafine et (feedback ver)"
        if real_iters:
            expander_label += f" — {real_iters} iterasyon"
        with st.expander(expander_label, expanded=False):
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

            # Gemini model selector — asset extraction için, Step 1'le ortak key
            _s2_model_ids = [m[0] for m in GEMINI_MODELS]
            _s2_model_labels = {m[0]: m[1] for m in GEMINI_MODELS}
            if "script_model" not in st.session_state or st.session_state["script_model"] not in _s2_model_ids:
                st.session_state["script_model"] = GEMINI_DEFAULT_MODEL
            st.selectbox(
                "AI Model (asset extraction için)",
                options=_s2_model_ids,
                format_func=lambda mid: _s2_model_labels.get(mid, mid).split(" — ")[0],
                key="script_model",
                help="Flash önerilir (5-30sn). Pro daha kaliteli ama uzun script'lerde "
                     "2-5dk sürebilir — gerçekten gerekirse seç.",
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
                                    help="Pollinations.ai ile 4 varyant üret (free, key gerek yok)",
                                )
                            with search_cs[2]:
                                # Pollinations model selector
                                _pmodel_ids = [m[0] for m in POLLINATIONS_MODELS]
                                _pmodel_lbls = {m[0]: m[1] for m in POLLINATIONS_MODELS}
                                _pmkey = f"asset_genmodel_{aid}"
                                if _pmkey not in st.session_state:
                                    st.session_state[_pmkey] = "flux"
                                st.selectbox(
                                    "AI modeli",
                                    options=_pmodel_ids,
                                    format_func=lambda m: _pmodel_lbls.get(m, m).split(" — ")[0],
                                    key=_pmkey,
                                    label_visibility="collapsed",
                                    help="AI üretim modeli (üret butonunda kullanılır)",
                                )

                            # Aktif kaynak listesi
                            _active = ["Wikimedia", "Openverse"]
                            if PIXABAY_API_KEY:
                                _active.append("Pixabay")
                            if PEXELS_API_KEY:
                                _active.append("Pexels")
                            st.caption(
                                f"Arama kaynakları: **{' · '.join(_active)}** "
                                f"&nbsp;·&nbsp; AI üretim: **Pollinations.ai** (free)"
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
                            is_all_ai = sources_in_cands == {"pollinations"}
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
                                    help="Beğenmediysen Pollinations'la 4 varyant üret",
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
                                            st.image(
                                                cand.get("thumb_url"),
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
    _title_now = derive_title(st.session_state.get("script_draft", "")) or "Untitled"
    _src_listing, _src_names = build_source_listing(_title_now, _selected_assets)
    _total_sources = len(_src_names)

    # Eğer custom prompt boşsa ve script var ve "user edited" değilse, otomatik doldur
    if (
        st.session_state.get("script_draft", "").strip()
        and not st.session_state.get("script_custom_prompt", "").strip()
        and not st.session_state.get("script_custom_prompt_user_edited", False)
    ):
        st.session_state["script_custom_prompt"] = render_custom_prompt(
            DEFAULT_CUSTOM_PROMPT_TEMPLATE, _title_now, _selected_assets
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
                "🔒 NotebookLM source listesi:\n"
                "1. `_execution_guide.txt` — Sabit kurallar "
                "(Text-Free / 80-20 Animation / Student Safety / History)\n"
                "2. `_custom_prompt.txt` — Bu doküman (Role/Task/Constraints)\n"
                "3. `<Title>_Script.txt` — Senaryon\n"
                "4..N. Görseller\n\n"
                "Custom prompt **hem source olarak hem Cinematic Customize alanına** "
                "gider — daha güçlü prime.",
                icon="ℹ️",
            )
            with st.expander("👁 Sabit talimatları (Execution Guide) gör"):
                st.code(EXECUTION_GUIDE_PROMPT, language=None)

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
            title = derive_title(text_submit)
            jobs_all = load_jobs()
            jobs_all.append(Job(
                id=uuid.uuid4().hex[:12],
                title=title,
                text=text_submit,
                submitted_by=_user_name(),
                original_script=original_at_submit,
                iterations=iterations_at_submit,
                assets=assets_at_submit,
                custom_prompt=custom_prompt_submit,
                # style_guides_used kaldırıldı (Phase E.3 — yanlış yorumdu)
            ))
            save_jobs(jobs_all)
            # Disk'teki yarım draft'ı temizle (artık jobs.json'da audit'le birlikte var)
            clear_script_draft(_user_name())
            # Submit sonrası alanları temizle — widget değerleri callback dışında
            # ama yeni run başlamadan önce session_state'i temizleyemeyiz çünkü
            # widget'lar zaten render edildi. Bu yüzden bayrak kullan: bir
            # sonraki run'ın başında temizle.
            st.session_state["_clear_after_submit"] = True
            st.toast("Kuyruğa eklendi! Birkaç dakika içinde tetiklenecek.", icon="🚀")
            time.sleep(0.5)
            st.rerun()


    # Drive Toplu (expander içinde, normal akışı engellemesin)
    st.markdown("&nbsp;", unsafe_allow_html=True)
    with st.expander("🗂️  Drive klasöründen toplu video üret (40+ docx'i otomatik işle)"):
        render_bulk_drive_section(key_prefix="usr")

    # Senin son istekleri
    st.markdown("&nbsp;", unsafe_allow_html=True)
    user = _user_name()
    # Hem display_name hem username ile case-insensitive eşleşme — eski URL-based
    # ?u=Mustafa job'ları ve yeni auth-based job'ları aynı user'a denk gelsin.
    auth = current_user() or {}
    user_lower = user.lower()
    username_lower = auth.get("username", "").lower()

    def _belongs_to_me(j: Job) -> bool:
        sb = (j.submitted_by or "").strip().lower()
        return sb == user_lower or sb == username_lower

    my_jobs = [j for j in jobs if _belongs_to_me(j)]
    my_jobs_sorted = sorted(my_jobs, key=lambda j: j.created_at, reverse=True)[:30]

    section_header(f"📋 Senin son isteklerin", f"{len(my_jobs)} kayıt")

    if not my_jobs_sorted:
        empty_state(
            "📭",
            "Henüz video isteğin yok",
            "Yukarıya senaryonu yapıştır ve gönder.",
        )
    else:
        st.markdown('<div class="job-row-wrap">', unsafe_allow_html=True)
        for j in my_jobs_sorted:
            with st.container(border=True):
                cs = st.columns([1.3, 5, 1.5])
                with cs[0]:
                    st.markdown(status_pill(j.status), unsafe_allow_html=True)
                with cs[1]:
                    title_short = j.title if len(j.title) <= 80 else j.title[:79] + "…"
                    st.markdown(
                        f'<div style="font-size:0.95rem; font-weight:600; line-height:1.3;" '
                        f'title="{j.title}">{title_short}</div>',
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
                        if "kota" in err or "limit" in err:
                            sub = "🚫 Kota dolu — yarın otomatik denenir"
                        elif "login" in err or "giriş" in err:
                            sub = "🔓 Hesap login süresi geçmiş — yöneticiye haber ver"
                        else:
                            sub = f"⚠ Hata: {(j.error or 'bilinmiyor')[:120]}"
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
                        if st.button("🌐 Notebook'u aç", key=f"u_open_{j.id}", use_container_width=True):
                            open_in_browser(j.notebook_url)

                    # Phase 4: Revize butonu — sadece video Azure'da hazırsa
                    if j.video_remote_url and j.status == "done":
                        if st.button("✏ Revize et",
                                      key=f"u_revize_{j.id}",
                                      use_container_width=True,
                                      help="Bu videoyu source yapıp yeni bir Cinematic üret"):
                            st.session_state["revize_target_id"] = j.id
                            st.rerun()
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
    _jobs_now = load_jobs()
    if any(j.status in ("running", "queued", "generating") for j in _jobs_now):
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
        if st.button("🌐 NotebookLM açar", use_container_width=True):
            open_in_browser("https://notebooklm.google.com/")
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

        with st.container(border=True):
            st.markdown(
                f'<div style="display:flex; align-items:center; gap:6px; margin-bottom:4px;">'
                f'<span style="font-size:0.85rem;">{dot}</span>'
                f'<span style="font-weight:600; font-size:0.92rem; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="{p.name}">{name_short}</span>'
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
                st.markdown(
                    '<a href="/vnc/" target="_blank" '
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
            # VNC açmadan, kendi makinende Chromium aç. Hazır komutları + rsync
            # önerisini göster, kullanıcı kopyalar çalıştırır, sonra 'Kontrol et'.
            with st.expander("💻 Lokal makineden yenile (VNC gerekmez)"):
                init_cmd = (
                    f".venv/bin/python notebooklm_automator.py --init "
                    f"--profile-dir chrome_profiles/{p.id} --authuser {p.authuser}"
                )
                st.markdown(
                    "**1.** Repo dizinine git ve Chromium'u native aç:",
                    unsafe_allow_html=True,
                )
                st.code(f"cd /path/to/notebooklm-cinematic-studio\n{init_cmd}", language="bash")
                st.markdown(
                    f"`{p.name}` hesabıyla giriş yap → NotebookLM ana sayfası → "
                    "auth.json otomatik kaydedilir (Chromium'u kapatabilirsin).",
                    unsafe_allow_html=True,
                )
                if LOCAL_INIT_SSH_HOST:
                    rsync_cmd = (
                        f"rsync -avz -e \"ssh -i {LOCAL_INIT_SSH_KEY}\" "
                        f"chrome_profiles/{p.id}/auth.json "
                        f"{LOCAL_INIT_SSH_HOST}:{LOCAL_INIT_REMOTE_PATH}/"
                        f"chrome_profiles/{p.id}/auth.json"
                    )
                    st.markdown("**2.** Auth.json'u server'a yolla:", unsafe_allow_html=True)
                    st.code(rsync_cmd, language="bash")
                else:
                    st.caption(
                        "ℹ️ `.env`'de `LOCAL_INIT_SSH_HOST=ubuntu@...` set edersen "
                        "rsync komutu da hazır şekilde gösterilir."
                    )
                st.markdown("**3.** Server'a ulaşınca aşağı bas — smoke test yapıp aktive eder:")
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
                if st.button("Kaydet", key=f"save_{p.id}"):
                    ps = load_profiles()
                    for x in ps:
                        if x.id == p.id:
                            x.name = new_name.strip() or x.name
                            x.authuser = int(new_authuser)
                            x.daily_limit = int(new_limit)
                            x.max_concurrent = int(new_max_c)
                            x.headless = bool(new_headless)
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


# ===== ANA PANEL — TAB'LAR =====
tab_prep, tab_bulk, tab_status, tab_videos, tab_log, tab_users = st.tabs(
    ["📝  Hazırla", "🗂️  Drive Toplu", "📊  Durum",
     "🎬  Videolar", "📜  Log", "👥  Kullanıcılar"]
)


# -------------------- TAB 1: HAZIRLA --------------------
with tab_prep:
    section_header("✍️ Yeni içerik", "Senaryon, system prompt'un veya uzun metin")
    with st.form("new_draft", clear_on_submit=True):
        d_title = st.text_input("Başlık (opsiyonel)", placeholder="boş bırakırsan ilk satırdan türetilir")
        d_content = st.text_area("İçerik", height=320, placeholder="Uzun video senaryon / system prompt'un...")
        if st.form_submit_button("➕  İçerik ekle", type="primary"):
            content = d_content.strip()
            if not content:
                st.error("İçerik boş olamaz.")
            else:
                title = d_title.strip() or derive_title(content)
                drafts = load_drafts()
                drafts.append(Draft(id=uuid.uuid4().hex[:10], title=title, content=content))
                save_drafts(drafts)
                st.toast("Kart oluşturuldu.", icon="✅")
                st.rerun()

    with st.expander("⚡ Hızlı yapıştır (her satır bir prompt)"):
        bulk = st.text_area("Her satır ayrı bir prompt olur", height=160, key="bulk_paste", label_visibility="collapsed")
        if st.button("Bulk içeriklere ekle"):
            lines = [ln.strip() for ln in bulk.splitlines() if ln.strip()]
            if not lines:
                st.error("Boş.")
            else:
                drafts = load_drafts()
                for ln in lines:
                    drafts.append(Draft(id=uuid.uuid4().hex[:10], title=derive_title(ln), content=ln))
                save_drafts(drafts)
                st.toast(f"{len(lines)} kart eklendi.", icon="✅")
                st.rerun()

    st.markdown("&nbsp;", unsafe_allow_html=True)

    drafts = load_drafts()
    section_header("📚 Hazırlanmış içerikler", f"{len(drafts)} kart")

    if not drafts:
        empty_state(
            "📝",
            "Henüz içerik yok",
            "Yukarıdaki formdan ya da hızlı yapıştır ile içerik ekle.",
        )
    else:
        if "selected_draft_ids" not in st.session_state:
            st.session_state.selected_draft_ids = set()

        c1, c2, _ = st.columns([1.5, 1.5, 5])
        with c1:
            if st.button("☑️ Tümünü seç", use_container_width=True):
                st.session_state.selected_draft_ids = {d.id for d in drafts}
                st.rerun()
        with c2:
            if st.button("⊘ Seçimi temizle", use_container_width=True):
                st.session_state.selected_draft_ids = set()
                st.rerun()

        for d in drafts:
            is_sel = d.id in st.session_state.selected_draft_ids
            with st.container(border=True):
                cs = st.columns([0.4, 5, 0.7, 0.7, 0.7])
                with cs[0]:
                    checked = st.checkbox(
                        "Seç",
                        value=is_sel,
                        key=f"chk_{d.id}",
                        label_visibility="collapsed",
                    )
                    if checked:
                        st.session_state.selected_draft_ids.add(d.id)
                    else:
                        st.session_state.selected_draft_ids.discard(d.id)
                with cs[1]:
                    words = len(d.content.split())
                    chars = len(d.content)
                    st.markdown(
                        f'<div style="font-weight:600; font-size:0.98rem; line-height:1.3;">{d.title}</div>'
                        f'<div style="font-size:0.78rem; opacity:0.65; margin-top:2px;">'
                        f'{words} kelime · {chars} karakter</div>',
                        unsafe_allow_html=True,
                    )
                    with st.expander("İçeriği gör"):
                        st.text(d.content)
                with cs[2]:
                    if st.button("✎", key=f"edit_{d.id}", help="Düzenle", use_container_width=True):
                        st.session_state[f"editing_{d.id}"] = True
                with cs[3]:
                    if st.button("➕", key=f"qadd_{d.id}", help="Tek başına kuyruğa", use_container_width=True):
                        jobs = load_jobs()
                        jobs.append(Job(id=uuid.uuid4().hex[:12], title=d.title, text=d.content))
                        save_jobs(jobs)
                        st.toast("Kuyruğa eklendi.", icon="✅")
                        st.rerun()
                with cs[4]:
                    if st.button("🗑", key=f"ddel_{d.id}", help="Sil", use_container_width=True):
                        ds = [x for x in load_drafts() if x.id != d.id]
                        save_drafts(ds)
                        st.session_state.selected_draft_ids.discard(d.id)
                        st.rerun()

                if st.session_state.get(f"editing_{d.id}"):
                    new_t = st.text_input("Başlık", value=d.title, key=f"et_{d.id}")
                    new_c = st.text_area("İçerik", value=d.content, height=200, key=f"ec_{d.id}")
                    cs2 = st.columns(2)
                    with cs2[0]:
                        if st.button("Kaydet", key=f"esave_{d.id}"):
                            ds = load_drafts()
                            for x in ds:
                                if x.id == d.id:
                                    x.title = new_t.strip() or x.title
                                    x.content = new_c
                                    break
                            save_drafts(ds)
                            st.session_state[f"editing_{d.id}"] = False
                            st.rerun()
                    with cs2[1]:
                        if st.button("İptal", key=f"ecancel_{d.id}"):
                            st.session_state[f"editing_{d.id}"] = False
                            st.rerun()

        sel_count = len(st.session_state.selected_draft_ids)
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button(
            f"🚀 Seçilenleri kuyruğa ekle  ({sel_count})",
            disabled=sel_count == 0,
            type="primary",
            use_container_width=True,
        ):
            ds = load_drafts()
            jobs = load_jobs()
            n = 0
            for d in ds:
                if d.id in st.session_state.selected_draft_ids:
                    jobs.append(Job(id=uuid.uuid4().hex[:12], title=d.title, text=d.content))
                    n += 1
            save_jobs(jobs)
            st.session_state.selected_draft_ids = set()
            st.toast(f"{n} job kuyruğa eklendi.", icon="✅")
            st.rerun()


# -------------------- TAB: DRIVE TOPLU --------------------
with tab_bulk:
    render_bulk_drive_section(key_prefix="adm")


# -------------------- TAB 2: DURUM --------------------
with tab_status:
    jobs = load_jobs()
    counts = {"queued": 0, "running": 0, "done": 0, "submitted": 0, "failed": 0, "stopped": 0}
    for j in jobs:
        counts[j.status] = counts.get(j.status, 0) + 1

    # Kota dolu uyarısı: bugün quota_exceeded yiyen profilleri belirgin göster
    today = date.today()
    quota_hit_profiles: dict[str, str] = {}  # profile_name -> last error
    for j in jobs:
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

    cols = st.columns(6)
    cols[0].metric("⏳ Kuyrukta", counts.get("queued", 0))
    cols[1].metric("▶ Çalışan", counts.get("running", 0))
    cols[2].metric("🎬 Üretiliyor", counts.get("generating", 0) + counts.get("submitted", 0))
    cols[3].metric("🎬 Video hazır", n_video_ready)
    cols[4].metric("☁️ Azure'da", n_video_uploaded if AZURE_ENABLED else "-")
    cols[5].metric("✗ Hatalı", counts.get("failed", 0))

    if AZURE_ENABLED:
        st.caption(f"☁️ Azure aktif · container: `{AZURE_CONTAINER}` · prefix: `{AZURE_BLOB_PREFIX}`")
    else:
        st.caption("ℹ️ Azure kapalı (`AZURE_STORAGE_CONNECTION_STRING` env var set edilmedi)")

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
    section_header("📋 Joblar", f"{len(jobs)} kayıt")

    if not jobs:
        empty_state(
            "📊",
            "Henüz job yok",
            "Hazırla sekmesinden bir içeriği kuyruğa at, durumunu burada izle.",
        )
    else:
        jobs_sorted = sorted(jobs, key=lambda j: j.created_at, reverse=True)

        st.markdown('<div class="job-row-wrap">', unsafe_allow_html=True)
        # Header (geniş ekranda görünür, dar ekranda CSS gizler)
        st.markdown('<div class="job-header">', unsafe_allow_html=True)
        h = st.columns([1.1, 3.2, 1.6, 1, 1, 2.6, 1.2])
        h[0].markdown("<small style='opacity:0.7; font-weight:600;'>DURUM</small>", unsafe_allow_html=True)
        h[1].markdown("<small style='opacity:0.7; font-weight:600;'>BAŞLIK</small>", unsafe_allow_html=True)
        h[2].markdown("<small style='opacity:0.7; font-weight:600;'>PROFİL / GÖNDEREN</small>", unsafe_allow_html=True)
        h[3].markdown("<small style='opacity:0.7; font-weight:600;'>BAŞLANGIÇ</small>", unsafe_allow_html=True)
        h[4].markdown("<small style='opacity:0.7; font-weight:600;'>SÜRE</small>", unsafe_allow_html=True)
        h[5].markdown("<small style='opacity:0.7; font-weight:600;'>NOTEBOOK / HATA</small>", unsafe_allow_html=True)
        h[6].markdown("<small style='opacity:0.7; font-weight:600;'>İŞLEM</small>", unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

        for j in jobs_sorted:
            cs = st.columns([1.1, 3.2, 1.6, 1, 1, 2.6, 1.2])
            cs[0].markdown(status_pill(j.status), unsafe_allow_html=True)
            title_short = j.title if len(j.title) <= 90 else j.title[:89] + "…"
            cs[1].markdown(
                f'<div style="font-size:0.92rem; line-height:1.35;" title="{j.title}">{title_short}</div>',
                unsafe_allow_html=True,
            )
            profile_short = (j.profile_name or "—")
            if len(profile_short) > 22:
                profile_short = profile_short[:21] + "…"
            submitter = j.submitted_by or ""
            submitter_html = (
                f'<div style="font-size:0.74rem; opacity:0.65; margin-top:2px;">'
                f'gönderen: <b>{submitter}</b></div>'
            ) if submitter else ""
            cs[2].markdown(
                f'<div style="font-size:0.85rem; opacity:0.85;" title="{j.profile_name}">{profile_short}</div>'
                f'{submitter_html}',
                unsafe_allow_html=True,
            )
            cs[3].markdown(f"<span style='font-size:0.85rem;'>{fmt_time(j.started_at)}</span>", unsafe_allow_html=True)
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
                                    f'<img src="{thumb}" style="width:64px; height:48px; '
                                    f'object-fit:cover; border-radius:4px;" '
                                    f'onerror="this.style.display=\'none\'"/>'
                                    f'<div style="font-size:0.75rem;">'
                                    f'<b>✅ Seçili</b> · {src_emoji} {sel.get("source","?")}<br>'
                                    f'<span style="opacity:0.7;">📜 {lic}</span></div>'
                                    f'</div>'
                                )
                            st.markdown(
                                f'<div style="margin-bottom:8px; padding:8px 10px; '
                                f'background:rgba(99,102,241,0.05); border-radius:6px;">'
                                f'<div style="font-size:0.78rem; opacity:0.65; '
                                f'margin-bottom:2px;">{icon} #{k+1} · '
                                f'<i>{pos}</i></div>'
                                f'<div style="font-size:0.85rem;">{desc}</div>'
                                f'<div style="font-size:0.75rem; opacity:0.7; '
                                f'margin-top:4px; font-family:monospace;">'
                                f'🔍 <code>{query}</code></div>'
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
                                f"v{k+1} · <code>{mdl}</code></small>",
                                unsafe_allow_html=True,
                            )
                            fb = it.get("feedback", "").strip()
                            if fb:
                                st.markdown(
                                    f"<div style='font-size:0.8rem; opacity:0.85; "
                                    f"padding:6px 10px; background:rgba(99,102,241,0.08); "
                                    f"border-left:2px solid #6366F1; border-radius:4px; "
                                    f"margin-bottom:4px;'>💬 {fb}</div>",
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
                    err_lower = j.error.lower()
                    is_quota = "kota" in err_lower or "limit" in err_lower
                    if is_quota:
                        st.markdown(
                            f'<div style="font-size:0.78rem; color:#991B1B; '
                            f'background:#FEE2E2; padding:4px 8px; border-radius:6px; '
                            f'margin-top:4px; border-left:3px solid #EF4444;">'
                            f'🚫 <b>Kota dolu</b> — {j.error[:180]}</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            f'<div style="font-size:0.78rem; opacity:0.7; margin-top:2px;">'
                            f'⚠ {j.error[:160]}</div>',
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
                    if st.button("🌐 Aç", key=f"open_{j.id}", help="Notebook'u tarayıcıda aç", use_container_width=True):
                        open_in_browser(j.notebook_url)
                # Harvest now (admin için, automator bitti ama henüz video yoksa)
                if (j.status in ("generating", "done", "submitted")
                    and not j.video_url
                    and j.harvest_status not in ("checking",)):
                    if st.button("🔍 Şimdi tara", key=f"harvest_{j.id}",
                                 help="Video harvest cycle'ını hemen tetikle", use_container_width=True):
                        worker.trigger_harvest_now(j.id)
                        st.toast("Harvest cycle tetiklendi.", icon="🔍")
                        st.rerun()
                if j.status == "running":
                    if st.button("🛑 Durdur", key=f"stop_{j.id}", help="Bu job'ı durdur", use_container_width=True):
                        worker.stop_job(j.id)
                        st.toast("Durdurma sinyali gönderildi.", icon="🛑")
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
                    f'<div style="font-weight:600;">{u.display_name or u.username}</div>'
                    f'<div style="font-size:0.78rem; opacity:0.7;">@{u.username}</div>',
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
# ---------------------------------------------------------------------------
jobs_now = load_jobs()
if any(j.status == "running" for j in jobs_now):
    time.sleep(4)
    st.rerun()
