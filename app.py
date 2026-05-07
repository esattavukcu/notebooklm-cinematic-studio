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
LAUNCHER_LOG = LOGS_DIR / "launcher.log"

# Bugünkü kullanım sayımına dahil olan job durumları. Failed da sayılır —
# yoksa kullanıcı sürekli aynı profili spam'leyip limit aşabilir.
COUNTED_STATUSES = {"running", "done", "submitted", "failed"}
TERMINAL_STATUSES = {"done", "failed", "submitted", "stopped"}

DISPATCH_INTERVAL_SEC = 2.0
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

# OpenRouter (LLM API). Set edilmediyse AI özellikleri (script iteration, asset
# extraction) UI'da gizlenir.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "openai/gpt-oss-120b:free"
).strip()
LLM_ENABLED = bool(OPENROUTER_API_KEY)

# Kullanıcının UI'dan seçebileceği top free modeller (Mayıs 2026 itibarıyla
# OpenRouter ücretsiz katmanından el ile seçildi). Hepsi instruct + multilingual.
# Liste eskirse https://openrouter.ai/models?max_price=0 üzerinden güncelle.
# NOT: Free tier endpoint'leri sık sık unavailable olabiliyor (404, rate limit).
# Bir model çalışmazsa diğerine geç.
# --- Image search: opsiyonel free-tier API keyleri ---
# Wikimedia + Openverse key gerektirmez. Pixabay + Pexels free tier ama
# kayıt + key alımı gerekir. Set edilmezse o kaynak skip edilir.
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "").strip()


OPENROUTER_FREE_MODELS: list[tuple[str, str]] = [
    # (id, label) — sıra: probe ile doğrulanmış çalışanlar önce
    ("openai/gpt-oss-120b:free",
     "GPT-OSS 120B — OpenAI açık-ağırlık (varsayılan)"),
    ("openai/gpt-oss-20b:free",
     "GPT-OSS 20B — OpenAI, daha hızlı"),
    ("nvidia/nemotron-3-super-120b-a12b:free",
     "Nemotron 3 Super 120B — NVIDIA"),
    ("minimax/minimax-m2.5:free",
     "MiniMax M2.5 — yaratıcı, multilingual"),
    # Aşağıdakiler zaman zaman rate-limit yiyor; rate-limit mesajı çıkarsa
    # yukarıdakilerden birine geç.
    ("meta-llama/llama-3.3-70b-instruct:free",
     "Llama 3.3 70B — Meta (sık rate-limit)"),
    ("nousresearch/hermes-3-llama-3.1-405b:free",
     "Hermes 3 405B — Nous Research, devasa (sık rate-limit)"),
    ("z-ai/glm-4.5-air:free",
     "GLM 4.5 Air — Çince/multilingual (sık rate-limit)"),
    ("qwen/qwen3-next-80b-a3b-instruct:free",
     "Qwen3 Next 80B — multilingual (sık rate-limit)"),
    ("google/gemma-4-31b-it:free",
     "Gemma 4 31B — Google (sık rate-limit)"),
    ("cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
     "Dolphin Mistral 24B — uncensored (sık rate-limit)"),
]

PYTHON_BIN = sys.executable  # venv'in içindeki python

# ---------------------------------------------------------------------------
# Klasörleri ve dosyaları hazırla
# ---------------------------------------------------------------------------
for d in (DATA_DIR, LOGS_DIR, SCREENSHOTS_DIR, DOWNLOADS_DIR, PROFILES_DIR):
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
                      assets: Optional[list] = None) -> None:
    """Kullanıcının draft'ını disk'e yaz."""
    if not username:
        return
    all_drafts = _load_all_script_drafts()
    all_drafts[username] = {
        "script": script or "",
        "iterations": iterations or [],
        "assets": assets or [],
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
def _openrouter_chat(messages: list[dict], model: Optional[str] = None,
                     temperature: float = 0.7, max_tokens: int = 2000) -> tuple[bool, str]:
    """Returns (success, content_or_error)."""
    if not LLM_ENABLED:
        return False, "OPENROUTER_API_KEY .env'de set edilmemiş."
    try:
        from openai import OpenAI
    except ImportError:
        return False, "openai package kurulu değil (pip install openai)."

    try:
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
            default_headers={
                "HTTP-Referer": "https://llm.yga.tr",
                "X-Title": "Cinematic Studio",
            },
        )
        response = client.chat.completions.create(
            model=model or OPENROUTER_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return True, response.choices[0].message.content or ""
    except Exception as e:
        # OpenRouter free tier hatalarını okunaklı hale getir.
        msg = str(e)
        used_model = model or OPENROUTER_MODEL
        if "429" in msg or "rate-limited" in msg.lower():
            return False, (
                f"⏳ '{used_model}' şu an provider tarafında rate-limited. "
                f"Listeden başka bir model seç (örn. GPT-OSS 120B) ve tekrar dene."
            )
        if "404" in msg or "No endpoints found" in msg:
            return False, (
                f"❌ '{used_model}' artık ücretsiz değil ya da provider'ı yok. "
                f"Listeden başka bir model seç."
            )
        if "401" in msg or "invalid api key" in msg.lower():
            return False, "🔑 OPENROUTER_API_KEY geçersiz. Sunucudaki .env'i kontrol et."
        return False, f"LLM hatası: {msg[:300]}"


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
    """Mevcut scripti feedback'e göre yeniden üretir. (success, new_script_or_error) döner."""
    if not feedback.strip():
        return False, "Feedback boş olamaz."
    user_prompt = f"""CURRENT SCRIPT:
{current_script.strip()}

USER FEEDBACK (apply these changes):
{feedback.strip()}

Generate the revised script."""
    return _openrouter_chat(
        [
            {"role": "system", "content": SCRIPT_EDITOR_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        model=model,
        temperature=0.8,
        max_tokens=3000,
    )


# ---------------------------------------------------------------------------
# Phase B: Asset extraction — script'ten görsel listesi çıkar
# ---------------------------------------------------------------------------
ASSET_EXTRACTOR_SYSTEM = """You are a visual director for short-form factual videos (Weird Facts / explainer style).

Given a video narration script, extract a list of visual assets (images, illustrations, footage frames) that should appear during narration. Each asset must:
- Match a specific narration moment (a phrase or sentence)
- Have a CONCRETE, searchable subject — no abstract concepts, metaphors, or feelings
- Be the kind of thing findable on stock-image sites (Wikimedia Commons, Openverse, Flickr CC) OR generatable by an AI image model

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


def extract_assets(script: str, model: Optional[str] = None) -> tuple[bool, list, str]:
    """Script'ten asset listesi çıkar. (success, assets_list, error_msg) döner.

    assets_list elemanları: {position, description, query, style}.
    Hata durumunda assets_list = [].
    """
    if not script.strip():
        return False, [], "Senaryo boş olamaz."

    ok, raw = _openrouter_chat(
        [
            {"role": "system", "content": ASSET_EXTRACTOR_SYSTEM},
            {"role": "user", "content": f"SCRIPT:\n{script.strip()}\n\nExtract assets as JSON array."},
        ],
        model=model,
        temperature=0.4,  # daha deterministik — JSON parse hatası az olsun
        max_tokens=4000,
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


def _search_wikimedia(query: str, limit: int = 4) -> list[dict]:
    """Wikimedia Commons'ta görsel ara. CC-BY-SA / public domain genelde."""
    if not query.strip():
        return []
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


def _search_openverse(query: str, limit: int = 4) -> list[dict]:
    """Openverse aggregator (Flickr/CC kaynakları). API key gerekmez.

    Not: license_type filtresi koymuyoruz — Openverse zaten sadece CC içerik
    indeksliyor, ama strict "commercial" filtresi sonuçları çok daraltıyor.
    BY-NC, BY-ND da kabul (kullanım amacımıza uygun, atıf veriyoruz).
    """
    if not query.strip():
        return []
    params = {
        "q": query,
        "page_size": str(limit),
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


def _search_pixabay(query: str, limit: int = 4) -> list[dict]:
    """Pixabay — free tier (key gerek). https://pixabay.com/api/docs/

    Lisans: Pixabay License (commercial-friendly, atıf opsiyonel).
    """
    if not PIXABAY_API_KEY or not query.strip():
        return []
    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "per_page": str(max(3, limit)),  # min 3
        "image_type": "photo",
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


def _search_pexels(query: str, limit: int = 4) -> list[dict]:
    """Pexels — free tier (key gerek). https://www.pexels.com/api/documentation/

    Lisans: Pexels License (commercial-friendly, atıf opsiyonel).
    """
    if not PEXELS_API_KEY or not query.strip():
        return []
    params = {"query": query, "per_page": str(limit)}
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


def search_images(query: str, limit: int = 12) -> list[dict]:
    """Aynı query'yi tüm aktif kaynaklarda ara, sonuçları interleave et.

    Aktif kaynaklar:
    - Wikimedia (her zaman, key gerekmez)
    - Openverse (her zaman, key gerekmez)
    - Pixabay (PIXABAY_API_KEY env set ise)
    - Pexels (PEXELS_API_KEY env set ise)

    Interleave: kaynak çeşitliliği için round-robin.
    """
    if not query.strip():
        return []
    per_source = max(2, limit // 4)
    sources: list[list[dict]] = []
    sources.append(_search_wikimedia(query, limit=per_source + 2))
    sources.append(_search_openverse(query, limit=per_source + 2))
    if PIXABAY_API_KEY:
        sources.append(_search_pixabay(query, limit=per_source + 2))
    if PEXELS_API_KEY:
        sources.append(_search_pexels(query, limit=per_source + 2))

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
        """Bugün NotebookLM kota dolu mesajı yiyen profil — yeni job dispatch etme."""
        today = date.today()
        for j in jobs:
            if j.profile_id != profile_id:
                continue
            if not j.error:
                continue
            err_lower = j.error.lower()
            if "kota" not in err_lower and "limit" not in err_lower:
                continue
            ts = j.finished_at or j.started_at or j.created_at
            try:
                if datetime.fromtimestamp(ts).date() == today:
                    return True
            except (OSError, OverflowError, ValueError):
                continue
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

        # Phase E: Eğer asset'lerin selected_image'leri varsa indir, automator'a
        # local path olarak ver. Pollinations URL'leri ilk fetch'te gen yapıyor
        # (5-15s × N), spawn'dan ÖNCE burada paralel indiriyoruz ki automator
        # yürürken bekleme olmasın.
        image_paths: list[Path] = []
        if job.assets:
            launcher_log(f"Job {job.id}: indiriliyor → {sum(1 for a in job.assets if a.get('selected_image'))} selected image")
            try:
                image_paths = download_job_images(job.id, job.assets)
                launcher_log(f"Job {job.id}: {len(image_paths)} image indirildi → {JOB_ASSETS_DIR / job.id}")
            except Exception as e:
                launcher_log(f"Job {job.id}: image download hata, devam: {e}")

        cmd = [
            PYTHON_BIN,
            str(APP_DIR / "notebooklm_automator.py"),
            job.text,
            "--profile-dir", str(PROFILES_DIR / profile.id),
            "--authuser", str(profile.authuser),
            "--job-id", job.id,
            "--json-events",
            "--no-wait-input",
            "--download-dir", str(DOWNLOADS_DIR),
            "--screenshots-dir", str(SCREENSHOTS_DIR),
        ]
        cmd.append("--headless" if profile.headless else "--no-headless")
        if image_paths:
            cmd.append("--images")
            cmd.extend(str(p) for p in image_paths)

        log_path = job_log_path(job.id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            log_fp = log_path.open("w", encoding="utf-8", buffering=1)
            log_fp.write(f"# Job {job.id} — Profile {profile.name} ({profile.id})\n")
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
            # stdout reader thread başlat — JSON event'leri parse etsin
            t = threading.Thread(
                target=self._stdout_reader,
                args=(job.id, proc, log_fp),
                name=f"stdout-{job.id}",
                daemon=True,
            )
            t.start()
            launcher_log(f"Job {job.id} launched on profile {profile.name} (pid={proc.pid})")
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
                    target.status = "done" if url else "submitted"
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
            # Sadece notebook_url'i olan ve harvest'i bitmemiş done/submitted job'lar
            if j.status not in ("done", "submitted"):
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
.pill-queued    { background: #FEF3C7; color: #92400E; border-color: #FBBF24; }
.pill-running   { background: #DBEAFE; color: #1E3A8A; border-color: #60A5FA; }
.pill-done      { background: #D1FAE5; color: #065F46; border-color: #34D399; }
.pill-submitted { background: #E0E7FF; color: #3730A3; border-color: #818CF8; }
.pill-failed    { background: #FEE2E2; color: #991B1B; border-color: #F87171; }
.pill-stopped   { background: #E5E7EB; color: #374151; border-color: #9CA3AF; }

@media (prefers-color-scheme: dark) {
  .pill-queued    { background: rgba(251,191,36,0.18); color: #FCD34D; }
  .pill-running   { background: rgba(96,165,250,0.18); color: #93C5FD; }
  .pill-done      { background: rgba(52,211,153,0.18); color: #6EE7B7; }
  .pill-submitted { background: rgba(129,140,248,0.18); color: #A5B4FC; }
  .pill-failed    { background: rgba(248,113,113,0.18); color: #FCA5A5; }
  .pill-stopped   { background: rgba(156,163,175,0.20); color: #D1D5DB; }
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
    def _profile_blocked(pid: str) -> bool:
        for j in jobs:
            if j.profile_id != pid or not j.error:
                continue
            err = j.error.lower()
            if "kota" not in err and "limit" not in err:
                continue
            ts = j.finished_at or j.started_at or j.created_at
            try:
                if datetime.fromtimestamp(ts).date() == today:
                    return True
            except (OSError, OverflowError, ValueError):
                continue
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

    # İlk yüklemede disk'ten restore (refresh / yeni sekme / başka cihazdan
    # gelirken yarım kalan draft'ı geri getir). Sadece ilk run'da çalışır.
    if "_script_draft_initialized" not in st.session_state:
        _user = _user_name()
        _saved = load_script_draft(_user) if _user else None
        if _saved:
            st.session_state["script_draft"] = _saved.get("script", "")
            st.session_state["script_iterations"] = _saved.get("iterations", []) or []
            st.session_state["script_assets"] = _saved.get("assets", []) or []
            if _saved.get("script"):
                # Kullanıcıya bildir — bilinmeyen yerden draft gelmesin
                st.session_state["_script_msg"] = (
                    "ok", "Yarım kalan draft'ın geri yüklendi."
                )
        else:
            st.session_state["script_draft"] = ""
            st.session_state["script_iterations"] = []
            st.session_state["script_assets"] = []
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
    # Callback'ler arası mesaj geçişi (toast/error). Render'da tüketilir.
    if "_script_msg" not in st.session_state:
        st.session_state["_script_msg"] = None  # ("ok"|"err", text)
    # Submit niyeti — submit callback'i set eder, ana akış kuyruğa ekler.
    if "_pending_submit" not in st.session_state:
        st.session_state["_pending_submit"] = False

    # --- Persistence helper (disk autosave) ---
    def _persist_draft() -> None:
        u = _user_name()
        if u:
            save_script_draft(
                u,
                st.session_state.get("script_draft", ""),
                st.session_state.get("script_iterations", []),
                st.session_state.get("script_assets", []),
            )

    # --- Callbacks ---
    def _cb_regenerate() -> None:
        current = st.session_state.get("script_draft", "").strip()
        feedback = st.session_state.get("script_feedback", "").strip()
        model = st.session_state.get("script_model") or OPENROUTER_MODEL
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

    def _cb_submit() -> None:
        text = st.session_state.get("script_draft", "").strip()
        if not text:
            st.session_state["_script_msg"] = ("err", "Senaryo boş olamaz.")
            return
        st.session_state["_pending_submit"] = True

    # --- Phase B (asset extraction) callbacks ---
    def _cb_extract_assets() -> None:
        script = st.session_state.get("script_draft", "").strip()
        model = st.session_state.get("script_model") or OPENROUTER_MODEL
        if not script:
            st.session_state["_script_msg"] = ("err", "Önce senaryo yapıştır.")
            return
        ok, assets, err = extract_assets(script, model=model)
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
            results = search_images(q, limit=8)
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

    def _cb_use_manual_url(asset_id: str, widget_key: str) -> None:
        """Kullanıcının yapıştırdığı URL'i selected_image olarak set et."""
        url = (st.session_state.get(widget_key) or "").strip()
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            st.session_state["_script_msg"] = (
                "err", "Geçerli bir https:// URL'si yapıştır."
            )
            return
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
            # Widget'ı temizle (bir sonraki run'da)
            st.session_state[widget_key] = ""
            st.session_state["_script_msg"] = ("ok", "URL seçildi.")
            _persist_draft()
            break

    # Önceki run'da bir mesaj set edildiyse göster (callback render'dan önce çalışır)
    _msg = st.session_state.pop("_script_msg", None)

    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:0.95rem; font-weight:600; margin-bottom:0.3rem;">'
        '📝 Video senaryonu yapıştır</div>'
        '<div style="font-size:0.8rem; opacity:0.7; margin-bottom:0.5rem;">'
        'Uzun yazabilirsin. NotebookLM senaryonu kaynak olarak alıp Cinematic '
        'video üretecek.'
        + (' AI ile düzenleyip yeniden üretmek için aşağıdaki <b>✨ AI ile düzenle</b> bölümünü kullan.' if LLM_ENABLED else '')
        + '</div>',
        unsafe_allow_html=True,
    )

    st.text_area(
        "Senaryo",
        height=360,
        placeholder="Senaryon, system prompt'un veya uzun metin...",
        label_visibility="collapsed",
        key="script_draft",
        on_change=_cb_text_changed,  # blur'da disk'e autosave
    )

    # ===== AI Editor (LLM aktifse) =====
    if LLM_ENABLED:
        iter_count = len(st.session_state["script_iterations"])
        expander_label = "✨ AI ile düzenle"
        if iter_count:
            expander_label += f" — {iter_count} iterasyon"
        with st.expander(expander_label, expanded=bool(iter_count)):
            st.caption(
                "Senaryon hakkında ne değişmeli yaz, AI yeniden üretsin. "
                "Beğenmezsen model değiştir, ya da tekrar feedback ver."
            )

            # Model selector — env'deki default + UI'dan ek seçenekler
            model_ids = [m[0] for m in OPENROUTER_FREE_MODELS]
            model_labels = {m[0]: m[1] for m in OPENROUTER_FREE_MODELS}
            # Env'deki model listede yoksa başa ekle (custom override)
            if OPENROUTER_MODEL not in model_ids:
                model_ids.insert(0, OPENROUTER_MODEL)
                model_labels[OPENROUTER_MODEL] = f"{OPENROUTER_MODEL} — env'den"
            # Default seçim: önceki seçim varsa onu, yoksa env değeri
            if "script_model" not in st.session_state or st.session_state["script_model"] not in model_ids:
                st.session_state["script_model"] = OPENROUTER_MODEL
            st.selectbox(
                "Model",
                options=model_ids,
                format_func=lambda mid: model_labels.get(mid, mid),
                key="script_model",
                help="OpenRouter'ın ücretsiz katmanından. Bir model çalışmazsa "
                     "(rate limit, 404 vs.) başkasını dene.",
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

    # ===== Phase B: Asset extraction (görsel listesi) =====
    if LLM_ENABLED:
        assets = st.session_state.get("script_assets", []) or []
        n_assets = len(assets)
        b_label = "🖼 Görseller (Phase B)"
        if n_assets:
            b_label += f" — {n_assets} öneri"
        with st.expander(b_label, expanded=bool(n_assets)):
            st.caption(
                "Senaryon hazır olunca aşağıdaki butona bas — LLM, video için "
                "lazım olan görsellerin listesini çıkarır. Her birini "
                "düzenleyebilir, silebilir, manuel ekleyebilirsin. Bu liste "
                "Phase C'de image search'e girecek."
            )
            cs_b = st.columns([2, 1, 1])
            with cs_b[0]:
                if n_assets:
                    extract_label = "🔄 Yeniden çıkar (listeyi sıfırlar)"
                else:
                    extract_label = "🖼 Görselleri çıkar"
                st.button(
                    extract_label,
                    type="primary",
                    use_container_width=True,
                    on_click=_cb_extract_assets,
                    key="btn_extract_assets",
                    help="Aktif senaryoyu LLM'e gönderir, görsel listesi çıkarır."
                )
            with cs_b[1]:
                if n_assets:
                    st.button(
                        "➕ Manuel ekle",
                        use_container_width=True,
                        on_click=_cb_add_asset,
                        key="btn_add_asset",
                    )
            with cs_b[2]:
                if n_assets:
                    st.button(
                        "🗑 Tümünü sil",
                        use_container_width=True,
                        on_click=_cb_clear_assets,
                        key="btn_clear_assets",
                    )

            if n_assets:
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

    # ===== Submit button =====
    st.markdown("&nbsp;", unsafe_allow_html=True)
    cs = st.columns([3, 1])
    with cs[0]:
        st.markdown(
            f'<div style="font-size:0.78rem; opacity:0.65;">'
            f'Gönderen: <b>{_user_name()}</b></div>',
            unsafe_allow_html=True,
        )
    with cs[1]:
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
        else:
            st.error(text_msg)

    # Submit niyeti varsa şimdi işle (widget'lardan sonra, callback dışında)
    if st.session_state.get("_pending_submit"):
        st.session_state["_pending_submit"] = False
        text_submit = st.session_state.get("script_draft", "").strip()
        iterations_at_submit = list(st.session_state.get("script_iterations", []))
        assets_at_submit = list(st.session_state.get("script_assets", []))
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
                    elif j.status == "done":
                        # Harvest durumuna göre alt-başlık
                        hs = j.harvest_status
                        if hs == "uploaded":
                            sub = "🎬 Video hazır + bulutta paylaşıma açık!"
                        elif hs == "downloaded":
                            sub = "🎬 Video hazır ve indirilmiş!"
                        elif hs == "ready":
                            sub = "🎬 Video hazır — oynatabilirsin"
                        elif hs == "checking":
                            sub = f"🔍 Video kontrol ediliyor... (deneme {j.harvest_attempts}/{HARVEST_MAX_ATTEMPTS})"
                        elif hs == "expired":
                            sub = "⌛ Video kontrolü zaman aşımı — Notebook'u açıp manuel bak"
                        else:  # pending
                            if j.harvest_attempts == 0:
                                first_at = (j.finished_at or j.created_at) + HARVEST_FIRST_DELAY_SEC
                                wait_min = max(0, int((first_at - time.time()) / 60))
                                if wait_min > 0:
                                    sub = f"✓ Tetiklendi · {wait_min} dk sonra video kontrol edilecek"
                                else:
                                    sub = "✓ Tetiklendi · video kontrolü çok yakında"
                            else:
                                next_min = max(0, int((j.next_harvest_at - time.time()) / 60))
                                sub = f"🔍 Video henüz hazır değil · {next_min} dk sonra tekrar bakılacak (deneme {j.harvest_attempts}/{HARVEST_MAX_ATTEMPTS})"
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
    if any(j.status == "running" or j.status == "queued" for j in _jobs_now):
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
                    help="0 = sınırsız. NotebookLM ücretsiz: 3/gün",
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
tab_prep, tab_status, tab_videos, tab_log, tab_users = st.tabs(
    ["📝  Hazırla", "📊  Durum", "🎬  Videolar", "📜  Log", "👥  Kullanıcılar"]
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
    cols[2].metric("✓ Tetiklendi", counts.get("done", 0) + counts.get("submitted", 0))
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
                # Harvest now (admin için, status done ama henüz video yoksa)
                if (j.status == "done" and not j.video_url
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

    if counts.get("running", 0) > 0:
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
