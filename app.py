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
LAUNCHER_LOG = LOGS_DIR / "launcher.log"

# Bugünkü kullanım sayımına dahil olan job durumları. Failed da sayılır —
# yoksa kullanıcı sürekli aynı profili spam'leyip limit aşabilir.
COUNTED_STATUSES = {"running", "done", "submitted", "failed"}
TERMINAL_STATUSES = {"done", "failed", "submitted", "stopped"}

DISPATCH_INTERVAL_SEC = 2.0
JOB_LOG_TAIL_LINES = 400

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
    text: str
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
        while not self._stop_evt.is_set():
            try:
                self._auto_init_check()
                self._dispatch_round()
                self._reap_finished()
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
            target.error = "NotebookLM günlük Cinematic kotası dolmuş — yarın resetlenir veya başka hesap kullan."
        elif etype == "automation_complete":
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
    proc = subprocess.Popen(
        cmd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        cwd=str(APP_DIR),
    )
    launcher_log(f"Init launched for profile {profile.name} (pid={proc.pid})")
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
# Mode detection: admin (?admin=<pw>) vs user (default)
# ---------------------------------------------------------------------------
def _admin_password() -> str:
    """ADMIN_PASSWORD env var. Boşsa: admin gizli URL devre dışı, herkes
    user view görür. Set edilmişse: ?admin=<pw> ile admin paneli."""
    return os.environ.get("ADMIN_PASSWORD", "").strip()


def _is_admin() -> bool:
    pw = _admin_password()
    if not pw:
        # Şifre set edilmediyse, ?admin=1 ile admin'e geç (lokal kullanım için).
        return st.query_params.get("admin", "") != ""
    return st.query_params.get("admin", "") == pw


def _user_name() -> str:
    """Kullanıcı kendi adını bir kez verir, session_state'te tutulur."""
    return st.session_state.get("user_name", "").strip()


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

    # İsim sorma (ilk girişte)
    if not _user_name():
        st.markdown("&nbsp;", unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown(
                '<div style="font-weight:600; font-size:1.05rem; margin-bottom:0.4rem;">'
                '👋 Hoş geldin! Adın nedir?</div>'
                '<div style="font-size:0.85rem; opacity:0.7; margin-bottom:0.6rem;">'
                'Gönderdiğin videoların burada listelenmesi için.</div>',
                unsafe_allow_html=True,
            )
            with st.form("user_name_form"):
                cs = st.columns([4, 1])
                with cs[0]:
                    name = st.text_input(
                        "Adın",
                        placeholder="örn. Mustafa",
                        label_visibility="collapsed",
                    )
                with cs[1]:
                    submitted = st.form_submit_button("Devam ➜", type="primary", use_container_width=True)
                if submitted:
                    if name.strip():
                        st.session_state["user_name"] = name.strip()
                        st.rerun()
                    else:
                        st.error("İsim boş olamaz.")
        return

    # Tüm hesaplar bloke ise uyarı (ama yine de submit göster, kuyruğa atılabilir)
    if all_blocked:
        st.warning(
            "🚫 Tüm hesapların bugünkü Cinematic kotası dolmuş. "
            "Yine de gönder — kotalar resetlenince (yarın TR ~10:00) otomatik üretilir.",
            icon="🚫",
        )

    # Submit form — büyük textarea + tek button
    st.markdown("&nbsp;", unsafe_allow_html=True)
    with st.form("user_submit", clear_on_submit=True):
        st.markdown(
            '<div style="font-size:0.95rem; font-weight:600; margin-bottom:0.3rem;">'
            '📝 Video senaryonu yapıştır</div>'
            '<div style="font-size:0.8rem; opacity:0.7; margin-bottom:0.5rem;">'
            'Uzun yazabilirsin. NotebookLM senaryonu kaynak olarak alıp Cinematic '
            'video üretecek.</div>',
            unsafe_allow_html=True,
        )
        text = st.text_area(
            "Senaryo",
            height=360,
            placeholder="Senaryon, system prompt'un veya uzun metin...",
            label_visibility="collapsed",
        )
        st.markdown("&nbsp;", unsafe_allow_html=True)
        cs = st.columns([3, 1])
        with cs[0]:
            st.markdown(
                f'<div style="font-size:0.78rem; opacity:0.65;">'
                f'Gönderen: <b>{_user_name()}</b> · '
                f'<a href="?reset_name=1" style="opacity:0.7;">değiştir</a></div>',
                unsafe_allow_html=True,
            )
        with cs[1]:
            submitted = st.form_submit_button(
                "🚀 Video üret",
                type="primary",
                use_container_width=True,
                disabled=False,
            )
        if submitted:
            if not text.strip():
                st.error("Senaryo boş olamaz.")
            else:
                title = derive_title(text)
                jobs_all = load_jobs()
                jobs_all.append(Job(
                    id=uuid.uuid4().hex[:12],
                    title=title,
                    text=text.strip(),
                    submitted_by=_user_name(),
                ))
                save_jobs(jobs_all)
                st.toast("Kuyruğa eklendi! Birkaç dakika içinde tetiklenecek.", icon="🚀")
                time.sleep(0.5)
                st.rerun()

    # Reset name link
    if st.query_params.get("reset_name", "") == "1":
        st.session_state.pop("user_name", None)
        st.query_params.clear()
        st.rerun()

    # Senin son istekleri
    st.markdown("&nbsp;", unsafe_allow_html=True)
    user = _user_name()
    my_jobs = [j for j in jobs if j.submitted_by == user]
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
                        sub = "✓ Tetiklendi! Video 25-60 dk içinde NotebookLM'de hazır olur"
                    elif j.status == "submitted":
                        sub = "📤 Tetiklendi (notebook URL'i yok). Admin loga baksın."
                    elif j.status == "failed":
                        err = j.error.lower() if j.error else ""
                        if "kota" in err or "limit" in err:
                            sub = "🚫 Kota dolu — yarın otomatik denenir"
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
                    if j.notebook_url and j.status in TERMINAL_STATUSES:
                        if st.button("🌐 Notebook'u aç", key=f"u_open_{j.id}", use_container_width=True):
                            open_in_browser(j.notebook_url)
        st.markdown('</div>', unsafe_allow_html=True)

    # Admin shortcut
    st.markdown(
        '<div style="margin-top:3rem; text-align:center; font-size:0.78rem; opacity:0.5;">'
        f'Sorun var mı? Yöneticiye haber ver. '
        f'<a href="?admin=1" style="opacity:0.7;">⚙️ Admin</a>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ===== MODE DISPATCH =====
# Admin değilse: user view render et, sonra çık. Admin UI hiç render edilmez.
if not _is_admin():
    render_user_view()
    # Auto-refresh: running job varsa yenile
    _jobs_now = load_jobs()
    if any(j.status == "running" or j.status == "queued" for j in _jobs_now):
        time.sleep(4)
        st.rerun()
    st.stop()


# ===== SIDEBAR =====
with st.sidebar:
    st.markdown(
        '<div style="padding: 0.4rem 0 0.6rem 0;">'
        '<div style="font-size: 1.3rem; font-weight: 700; letter-spacing: -0.02em;">🎬 Cinematic Studio</div>'
        '<div style="font-size: 0.78rem; opacity: 0.7; margin-top: 2px;">NotebookLM toplu video üretici</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("🌐 NotebookLM'i normal Chrome'da aç", use_container_width=True):
        open_in_browser("https://notebooklm.google.com/")

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
                        st.toast("Chromium açıldı. Google'a giriş yap, ardından pencereyi kapat — gerisi otomatik.", icon="🔓")
                else:
                    if st.button("🔄 Yeniden giriş", key=f"relogin_{p.id}", use_container_width=True):
                        launch_profile_init(p)
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

            # Login pending olanlar için bilgilendirme
            if not p.initialized:
                st.markdown(
                    '<div style="font-size:0.72rem; opacity:0.7; margin-top:6px; '
                    'padding:6px 8px; background:rgba(99,102,241,0.08); border-radius:6px;">'
                    '⏳ Login bekleniyor — Chromium\'da giriş yapınca <b>otomatik aktive olur</b>'
                    '</div>',
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
    f'<div class="app-hero" style="display:flex; align-items:center; gap:1rem;">'
    f'<div style="flex:1;">'
    f'<h1>🎬 Cinematic Studio  <span style="font-size:0.6em; padding:3px 10px; '
    f'background:rgba(255,255,255,0.18); border-radius:999px; vertical-align:middle; '
    f'font-weight:600; letter-spacing:0.05em;">YÖNETİM</span></h1>'
    f'<p>Toplu metin → paralel video üretimi · {_init_count}/{_total_count} hesap aktif</p>'
    f'</div>'
    f'<div style="text-align:right;">'
    f'<a href="?" style="color:rgba(255,255,255,0.85); text-decoration:none; '
    f'font-size:0.82rem; padding:6px 12px; border:1px solid rgba(255,255,255,0.3); '
    f'border-radius:6px;">← Kullanıcı görünümü</a>'
    f'</div>'
    f'</div>',
    unsafe_allow_html=True,
)


# ===== ANA PANEL — TAB'LAR =====
tab_prep, tab_status, tab_videos, tab_log = st.tabs(
    ["📝  Hazırla", "📊  Durum", "🎬  Videolar", "📜  Log"]
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

    cols = st.columns(5)
    cols[0].metric("⏳ Kuyrukta", counts.get("queued", 0))
    cols[1].metric("▶ Çalışan", counts.get("running", 0))
    cols[2].metric("✓ Tamamlanan", counts.get("done", 0) + counts.get("submitted", 0))
    cols[3].metric("⏱ Süresi geçti", counts.get("stopped", 0))
    cols[4].metric("✗ Hatalı", counts.get("failed", 0))

    # CSV verisi (her zaman hazır)
    csv_buf = io.StringIO()
    csv_buf.write("﻿")  # BOM, Excel UTF-8 uyumu
    csv_w = csv.writer(csv_buf)
    csv_w.writerow(["id", "title", "status", "profile", "started", "duration_sec", "notebook_url", "error"])
    for j in load_jobs():
        duration = (j.finished_at or time.time()) - j.started_at if j.started_at else 0
        csv_w.writerow([
            j.id, j.title, j.status, j.profile_name,
            fmt_time(j.started_at),
            int(duration) if j.started_at else "",
            j.notebook_url, j.error,
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
        h[2].markdown("<small style='opacity:0.7; font-weight:600;'>PROFİL</small>", unsafe_allow_html=True)
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
            cs[2].markdown(
                f'<div style="font-size:0.85rem; opacity:0.85;" title="{j.profile_name}">{profile_short}</div>',
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
                if j.notebook_url and j.status in TERMINAL_STATUSES:
                    if st.button("🌐 Aç", key=f"open_{j.id}", help="Notebook'u tarayıcıda aç (manuel indirme için)", use_container_width=True):
                        open_in_browser(j.notebook_url)
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


# ---------------------------------------------------------------------------
# Auto-refresh: SADECE running job varsa rerun. queued tek başına refresh'i
# tetiklemez (worker zaten 2 sn'de bir dispatch ediyor; running'e geçince refresh).
# Bu sayede Hazırla sekmesinde içerik girerken sayfa boşuna yenilenmez.
# ---------------------------------------------------------------------------
jobs_now = load_jobs()
if any(j.status == "running" for j in jobs_now):
    time.sleep(4)
    st.rerun()
