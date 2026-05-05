"""
NotebookLM Cinematic — Streamlit UI
====================================
Çoklu Google profili yönetimi + metin kuyruğu + paralel job dağıtımı.

Çalıştırmak için:
    streamlit run app.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import streamlit as st

# ------------------------------------------------------------------
# Yollar
# ------------------------------------------------------------------
ROOT = Path(__file__).parent.resolve()
PROFILES_DIR = ROOT / "chrome_profiles"
DATA_DIR = ROOT / "data"
JOBS_FILE = DATA_DIR / "jobs.json"
PROFILES_FILE = DATA_DIR / "profiles.json"
DRAFTS_FILE = DATA_DIR / "drafts.json"
DOWNLOADS_DIR = DATA_DIR / "downloads"
LOGS_DIR = DATA_DIR / "logs"
AUTOMATOR = ROOT / "notebooklm_automator.py"

PROFILES_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR.mkdir(exist_ok=True)


# ------------------------------------------------------------------
# Data modelleri (basit JSON dosyalar)
# ------------------------------------------------------------------
@dataclass
class Profile:
    id: str
    name: str
    note: str = ""
    created_at: float = field(default_factory=time.time)
    last_used: Optional[float] = None
    initialized: bool = False  # ilk login yapıldı mı
    daily_limit: int = 3  # NotebookLM hesap başına günlük video limiti
    max_concurrent: int = 1  # auth.json varsa 2-3 yapılabilir (paralel mod)
    headless: bool = True  # Job çalışırken Chromium görünmez olsun (focus stealing yok)

    @property
    def dir(self) -> Path:
        return PROFILES_DIR / self.id

    @property
    def has_auth(self) -> bool:
        return (self.dir / "auth.json").exists()


@dataclass
class Draft:
    id: str
    title: str
    content: str
    created_at: float = field(default_factory=time.time)
    last_modified: float = field(default_factory=time.time)


@dataclass
class Job:
    id: str
    text: str
    profile_id: Optional[str] = None
    status: str = "queued"  # queued | running | done | submitted | failed
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    notebook_url: Optional[str] = None
    pid: Optional[int] = None
    title: Optional[str] = None  # draft'tan geldiyse başlığı
    video_path: Optional[str] = None  # indirilen .mp4 dosya yolu
    followup_attempts: int = 0  # follow-up worker kaç kere denedi


# ------------------------------------------------------------------
# Kalıcı state — JSON file lock ile paralel-güvenli
# ------------------------------------------------------------------
_LOCK = threading.RLock()


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_profiles() -> list[Profile]:
    with _LOCK:
        raw = _read_json(PROFILES_FILE, [])
        valid_keys = {f.name for f in Profile.__dataclass_fields__.values()}
        return [Profile(**{k: v for k, v in p.items() if k in valid_keys}) for p in raw]


def save_profiles(profiles: list[Profile]) -> None:
    with _LOCK:
        _write_json(PROFILES_FILE, [asdict(p) for p in profiles])


def load_jobs() -> list[Job]:
    with _LOCK:
        raw = _read_json(JOBS_FILE, [])
        # Eski state dosyalarında yeni alanlar eksik olabilir — defaults ile uyumlu
        valid_keys = {f.name for f in Job.__dataclass_fields__.values()}
        return [Job(**{k: v for k, v in j.items() if k in valid_keys}) for j in raw]


def save_jobs(jobs: list[Job]) -> None:
    with _LOCK:
        _write_json(JOBS_FILE, [asdict(j) for j in jobs])


def load_drafts() -> list[Draft]:
    with _LOCK:
        raw = _read_json(DRAFTS_FILE, [])
        return [Draft(**d) for d in raw]


def save_drafts(drafts: list[Draft]) -> None:
    with _LOCK:
        _write_json(DRAFTS_FILE, [asdict(d) for d in drafts])


def update_job(job_id: str, **fields) -> None:
    with _LOCK:
        jobs = load_jobs()
        for j in jobs:
            if j.id == job_id:
                for k, v in fields.items():
                    setattr(j, k, v)
                break
        save_jobs(jobs)


# ------------------------------------------------------------------
# Limit / sayım yardımcıları
# ------------------------------------------------------------------
COUNTED_STATUSES = {"running", "done", "submitted", "failed"}


def _today_start_ts() -> float:
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))


def usage_today_by_profile(jobs: list[Job]) -> dict[str, int]:
    """Bugün her profile kaç job atandığını döner (limit hesabı için)."""
    today = _today_start_ts()
    counts: dict[str, int] = {}
    for j in jobs:
        if not j.profile_id or not j.started_at:
            continue
        if j.started_at < today:
            continue
        if j.status in COUNTED_STATUSES:
            counts[j.profile_id] = counts.get(j.profile_id, 0) + 1
    return counts


# ------------------------------------------------------------------
# Worker — arka planda çalışır, queued job'ları boş profile dağıtır
# ------------------------------------------------------------------
class Worker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # profile_id -> set of running job_ids (multiple if max_concurrent > 1)
        self._busy: dict[str, set[str]] = {}
        self._busy_lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def busy_count(self, profile_id: str) -> int:
        with self._busy_lock:
            return len(self._busy.get(profile_id, set()))

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._dispatch_round()
            except Exception as e:
                print(f"[worker] loop error: {e}", flush=True)
            time.sleep(2)

    def _dispatch_round(self):
        profiles = [p for p in load_profiles() if p.initialized]
        if not profiles:
            return
        jobs = load_jobs()
        queued = [j for j in jobs if j.status == "queued"]
        if not queued:
            return

        # Bugünkü kullanım — günlük limit kontrolü için
        today_counts = usage_today_by_profile(jobs)

        with self._busy_lock:
            busy_snapshot = {pid: len(ids) for pid, ids in self._busy.items()}

        # Profile başına kalan slot sayısı (concurrency + günlük limit)
        def slots_left(p: Profile) -> int:
            running = busy_snapshot.get(p.id, 0)
            cap = max(0, p.max_concurrent - running)
            limit = p.daily_limit if p.daily_limit > 0 else 10**9
            remaining_today = max(0, limit - today_counts.get(p.id, 0))
            return min(cap, remaining_today)

        # Round-robin: tüm uygun (slotu olan) profilleri last_used ascending sırala
        # Her round'da N kez gez (N = max(slots_left)) — fair sharing
        available = [p for p in profiles if slots_left(p) > 0]
        if not available:
            return

        available.sort(key=lambda p: p.last_used or 0)

        # Job'ları profillere round-robin dağıt
        # Profil başına bir job, sonra döngü tekrar
        local_slots = {p.id: slots_left(p) for p in available}
        idx = 0
        while queued and any(s > 0 for s in local_slots.values()):
            prof = available[idx % len(available)]
            idx += 1
            if local_slots[prof.id] <= 0:
                continue
            job = queued.pop(0)
            local_slots[prof.id] -= 1
            self._launch(job, prof)

    def _launch(self, job: Job, prof: Profile):
        log_path = LOGS_DIR / f"{job.id}.log"
        update_job(
            job.id,
            status="running",
            profile_id=prof.id,
            started_at=time.time(),
        )
        # Profile last_used update
        with _LOCK:
            profs = load_profiles()
            for p in profs:
                if p.id == prof.id:
                    p.last_used = time.time()
            save_profiles(profs)

        with self._busy_lock:
            self._busy.setdefault(prof.id, set()).add(job.id)

        def runner():
            try:
                with log_path.open("w", encoding="utf-8") as logf:
                    cmd = [
                        sys.executable,
                        str(AUTOMATOR),
                        "--profile-dir",
                        str(prof.dir),
                        "--no-wait-input",
                        "--json-events",
                        "--timeout-min",
                        "60",
                        "--download-dir",
                        str(DOWNLOADS_DIR),
                    ]
                    if prof.headless:
                        cmd.append("--headless")
                    cmd.append(job.text)
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        cwd=str(ROOT),
                    )
                    update_job(job.id, pid=proc.pid)

                    notebook_url = None
                    error_msg = None
                    video_ready = True
                    video_path = None
                    for line in proc.stdout:  # type: ignore
                        logf.write(line)
                        logf.flush()
                        if "##JSON##" in line:
                            try:
                                payload = json.loads(line.split("##JSON##", 1)[1].strip())
                                ev = payload.get("event")
                                if ev == "job_done":
                                    notebook_url = payload.get("notebook_url")
                                    video_ready = payload.get("video_ready", True)
                                    if payload.get("video_path"):
                                        video_path = payload.get("video_path")
                                elif ev == "video_downloaded":
                                    video_path = payload.get("path")
                                elif ev == "job_failed":
                                    error_msg = payload.get("error")
                                elif ev == "timeout_soft":
                                    video_ready = False
                            except Exception:
                                pass
                    rc = proc.wait()

                    if rc == 0:
                        update_job(
                            job.id,
                            status="done" if video_ready else "submitted",
                            finished_at=time.time(),
                            notebook_url=notebook_url,
                            video_path=video_path,
                            error=None if video_ready else "Süre doldu — follow-up worker indirmeyi deneyecek",
                        )
                    else:
                        update_job(
                            job.id,
                            status="failed",
                            finished_at=time.time(),
                            error=error_msg
                            or f"Process exit code {rc} — log: {log_path.name}",
                        )
            except Exception as e:
                update_job(job.id, status="failed", finished_at=time.time(), error=str(e))
            finally:
                with self._busy_lock:
                    if prof.id in self._busy:
                        self._busy[prof.id].discard(job.id)
                        if not self._busy[prof.id]:
                            self._busy.pop(prof.id, None)

        threading.Thread(target=runner, daemon=True).start()


# ------------------------------------------------------------------
# Follow-up Worker — submitted job'ları periyodik kontrol et
# ------------------------------------------------------------------
FOLLOWUP_INTERVAL_SEC = 600  # 10 dakika
FOLLOWUP_MAX_ATTEMPTS = 12   # ~ 2 saat (12 * 10 dk)


class FollowupWorker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._busy: set[str] = set()  # in-progress job_ids
        self._lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        # İlk denemeden önce 30 sn bekle (uygulama yeni başlamış olabilir)
        for _ in range(30 // 5):
            if self._stop.is_set():
                return
            time.sleep(5)
        while not self._stop.is_set():
            try:
                self._round()
            except Exception as e:
                print(f"[followup] {e}", flush=True)
            # her 10 dk'da bir
            for _ in range(FOLLOWUP_INTERVAL_SEC // 5):
                if self._stop.is_set():
                    return
                time.sleep(5)

    def _round(self):
        jobs = load_jobs()
        candidates = [
            j for j in jobs
            if j.status == "submitted"
            and j.notebook_url
            and j.profile_id
            and j.followup_attempts < FOLLOWUP_MAX_ATTEMPTS
        ]
        if not candidates:
            return

        profiles = {p.id: p for p in load_profiles()}
        for job in candidates:
            with self._lock:
                if job.id in self._busy:
                    continue
                self._busy.add(job.id)
            prof = profiles.get(job.profile_id)
            if not prof or not prof.initialized:
                with self._lock:
                    self._busy.discard(job.id)
                continue
            self._launch_followup(job, prof)

    def _launch_followup(self, job: Job, prof: Profile):
        log_path = LOGS_DIR / f"{job.id}.followup.log"
        attempt = job.followup_attempts + 1

        def runner():
            try:
                with log_path.open("a", encoding="utf-8") as logf:
                    logf.write(f"\n--- Follow-up attempt {attempt} @ {time.strftime('%H:%M:%S')} ---\n")
                    logf.flush()
                    cmd = [
                        sys.executable,
                        str(AUTOMATOR),
                        "--profile-dir",
                        str(prof.dir),
                        "--check-download",
                        job.notebook_url,
                        "--filename-hint",
                        (job.title or job.text[:50]),
                        "--download-dir",
                        str(DOWNLOADS_DIR),
                        "--json-events",
                        "--no-wait-input",
                    ]
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        cwd=str(ROOT),
                    )
                    video_path = None
                    for line in proc.stdout:  # type: ignore
                        logf.write(line)
                        logf.flush()
                        if "##JSON##" in line:
                            try:
                                payload = json.loads(line.split("##JSON##", 1)[1].strip())
                                ev = payload.get("event")
                                if ev in ("video_downloaded", "followup_done"):
                                    video_path = payload.get("path") or payload.get("video_path")
                            except Exception:
                                pass
                    rc = proc.wait()

                if video_path:
                    update_job(
                        job.id,
                        status="done",
                        video_path=video_path,
                        finished_at=time.time(),
                        followup_attempts=attempt,
                        error=None,
                    )
                else:
                    update_job(job.id, followup_attempts=attempt)
            except Exception as e:
                update_job(job.id, followup_attempts=attempt, error=f"followup: {e}")
            finally:
                with self._lock:
                    self._busy.discard(job.id)

        threading.Thread(target=runner, daemon=True).start()


# Singleton'lar — Streamlit script reload'larında bile yaşasınlar
@st.cache_resource
def get_worker() -> Worker:
    w = Worker()
    w.start()
    return w


@st.cache_resource
def get_followup_worker() -> FollowupWorker:
    w = FollowupWorker()
    w.start()
    return w


# ------------------------------------------------------------------
# UI yardımcıları
# ------------------------------------------------------------------
def fmt_time(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def fmt_duration(j: Job) -> str:
    if not j.started_at:
        return "—"
    end = j.finished_at or time.time()
    secs = int(end - j.started_at)
    return f"{secs // 60:02d}:{secs % 60:02d}"


def status_emoji(s: str) -> str:
    return {
        "queued": "⏳",
        "running": "▶",
        "done": "✓",
        "submitted": "⏱",
        "failed": "✗",
    }.get(s, "?")


# ------------------------------------------------------------------
# Streamlit UI
# ------------------------------------------------------------------
st.set_page_config(page_title="NotebookLM Cinematic Studio", layout="wide")

# Worker'ları başlat (singleton, cache_resource sayesinde reload'larda yaşar)
worker = get_worker()
followup = get_followup_worker()

st.title("NotebookLM Cinematic Studio")
st.caption(
    "Birden fazla Google profili üzerinden NotebookLM'de Cinematic videolar üretir."
)

# ---------------- Sidebar: Profile yönetimi ----------------
with st.sidebar:
    # Hızlı erişim: NotebookLM'i normal Chrome'da aç (manuel indirme için)
    st.markdown("### 🚀 Hızlı erişim")
    if st.button(
        "🌐 NotebookLM'i normal Chrome'da aç",
        use_container_width=True,
        help="Mac'in default browser'ında NotebookLM açılır. Manuel video indirmek için en kolay yol — download ~/Downloads'a düşer."
    ):
        subprocess.Popen(["open", "https://notebooklm.google.com"])
        st.success("Açıldı! Normal Chrome'da videoları manuel indirebilirsin.")

    st.divider()

    st.header("Hesap profilleri")
    profiles = load_profiles()
    today_counts = usage_today_by_profile(load_jobs())

    if not profiles:
        st.info("Henüz profil yok. Aşağıdan ekle.")

    for p in profiles:
        with st.container(border=True):
            cols = st.columns([3, 1])
            with cols[0]:
                badge = "🟢" if p.initialized else "⚪"
                auth_badge = "⚡" if p.has_auth else ""
                used = today_counts.get(p.id, 0)
                limit_txt = f"{used}/{p.daily_limit}" if p.daily_limit > 0 else f"{used}/∞"
                limit_color = "🔴" if p.daily_limit > 0 and used >= p.daily_limit else "•"
                st.markdown(f"**{badge} {p.name}** {auth_badge}  \n{limit_color} bugün: {limit_txt}  ·  paralel: ×{p.max_concurrent}")
                if p.note:
                    st.caption(p.note)
                st.caption(f"Son kullanım: {fmt_time(p.last_used)}")
            with cols[1]:
                if st.button("🗑", key=f"del_{p.id}", help="Profili sil"):
                    if p.dir.exists():
                        shutil.rmtree(p.dir, ignore_errors=True)
                    save_profiles([x for x in profiles if x.id != p.id])
                    st.rerun()
            # Ayarlar (collapsible)
            with st.expander("Ayarlar", expanded=False):
                new_limit = st.number_input(
                    "Günlük max video (0 = sınırsız)",
                    min_value=0,
                    max_value=100,
                    value=int(p.daily_limit),
                    step=1,
                    key=f"limit_{p.id}",
                )
                max_conc_help = (
                    "Aynı hesapta paralel kaç job çalışsın. "
                    + ("⚡ auth.json hazır, 2-3 deneyebilirsin." if p.has_auth else "Login yapmadan yalnızca 1.")
                )
                new_conc = st.number_input(
                    "Paralel job (max_concurrent)",
                    min_value=1,
                    max_value=5,
                    value=int(p.max_concurrent),
                    step=1,
                    key=f"conc_{p.id}",
                    help=max_conc_help,
                )
                new_headless = st.checkbox(
                    "Arka planda çalış (görünmez)",
                    value=bool(p.headless),
                    key=f"hl_{p.id}",
                    help="Job çalışırken Chromium görünmez. Ekranı bozmaz, focus çalmaz. "
                         "Hata ayıklamak için kapatabilirsin.",
                )
                if (new_limit != p.daily_limit
                        or new_conc != p.max_concurrent
                        or new_headless != p.headless):
                    if st.button("Ayarları kaydet", key=f"save_set_{p.id}"):
                        profs = load_profiles()
                        for x in profs:
                            if x.id == p.id:
                                x.daily_limit = int(new_limit)
                                x.max_concurrent = int(new_conc)
                                x.headless = bool(new_headless)
                        save_profiles(profs)
                        st.rerun()
            if not p.initialized:
                if st.button("Login başlat", key=f"login_{p.id}", type="primary"):
                    # Subprocess'i ayrı log dosyasına yaz, hata olursa görelim
                    init_log = LOGS_DIR / f"init_{p.id}.log"
                    init_log.parent.mkdir(parents=True, exist_ok=True)
                    with init_log.open("w", encoding="utf-8") as lf:
                        lf.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                        lf.write(f"sys.executable: {sys.executable}\n")
                        lf.write(f"AUTOMATOR: {AUTOMATOR}\n")
                        lf.write(f"profile_dir: {p.dir}\n")
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            str(AUTOMATOR),
                            "--profile-dir",
                            str(p.dir),
                            "--init",
                            "--json-events",
                        ],
                        cwd=str(ROOT),
                        stdout=init_log.open("a", encoding="utf-8"),
                        stderr=subprocess.STDOUT,
                    )
                    st.session_state[f"init_pid_{p.id}"] = proc.pid
                    # Hızlı bir kontrol: 2 sn sonra hâlâ canlı mı?
                    time.sleep(2)
                    if proc.poll() is not None:
                        st.error(
                            f"Tarayıcı başlatılamadı (subprocess exit code {proc.returncode}). "
                            f"Log: {init_log}"
                        )
                        st.code(init_log.read_text(encoding="utf-8")[-2000:], language="text")
                    else:
                        st.info(
                            f"Tarayıcı açıldı (pid={proc.pid}). Google'a giriş yap, sonra "
                            "pencereyi kapat. Bittiğinde 'Login tamamlandı ✓' butonuna bas. "
                            f"Log: `{init_log.name}` (Log sekmesinde görülebilir)"
                        )
                if st.button("Login tamamlandı ✓", key=f"done_{p.id}"):
                    profs = load_profiles()
                    for x in profs:
                        if x.id == p.id:
                            x.initialized = True
                    save_profiles(profs)
                    st.rerun()
            else:
                if st.button("Tekrar login (re-auth)", key=f"relogin_{p.id}"):
                    init_log = LOGS_DIR / f"init_{p.id}.log"
                    init_log.parent.mkdir(parents=True, exist_ok=True)
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            str(AUTOMATOR),
                            "--profile-dir",
                            str(p.dir),
                            "--init",
                            "--json-events",
                        ],
                        cwd=str(ROOT),
                        stdout=init_log.open("a", encoding="utf-8"),
                        stderr=subprocess.STDOUT,
                    )
                    time.sleep(2)
                    if proc.poll() is not None:
                        st.error(
                            f"Tarayıcı başlatılamadı (exit {proc.returncode}). Log: {init_log}"
                        )
                        st.code(init_log.read_text(encoding="utf-8")[-2000:], language="text")
                    else:
                        st.info(f"Tarayıcı açıldı (pid={proc.pid}).")

    st.divider()
    st.subheader("Yeni profil ekle")
    with st.form("new_profile", clear_on_submit=True):
        new_name = st.text_input("Etiket (örn: esat-kurumsal)")
        new_note = st.text_input(
            "Not (opsiyonel, örn: hangi e-posta)", placeholder="esat@yga.org.tr"
        )
        new_limit = st.number_input(
            "Günlük max video (0 = sınırsız)",
            min_value=0,
            max_value=100,
            value=3,
            step=1,
            help="NotebookLM ücretsiz hesaplarda günlük ~3 video Cinematic limiti var.",
        )
        submitted = st.form_submit_button("Ekle")
        if submitted:
            name = new_name.strip()
            if not name:
                st.error("Etiket boş olamaz.")
            else:
                pid = uuid.uuid4().hex[:8]
                p = Profile(
                    id=pid,
                    name=name,
                    note=new_note.strip(),
                    daily_limit=int(new_limit),
                )
                p.dir.mkdir(parents=True, exist_ok=True)
                profs = load_profiles()
                profs.append(p)
                save_profiles(profs)
                st.success(f"'{name}' eklendi. Şimdi 'Login başlat' butonuna bas.")
                st.rerun()


# ---------------- Ana panel ----------------
profiles = load_profiles()
ready_profiles = [p for p in profiles if p.initialized]

tab_compose, tab_status, tab_videos, tab_logs = st.tabs(
    ["📝 Hazırla", "📊 Durum", "🎬 Videolar", "📜 Log"]
)


def _auto_title(content: str, max_len: int = 60) -> str:
    """İçerikten otomatik başlık türet (ilk anlamlı satır)."""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # SYSTEM PROMPT, INPUT, vb. yapısal başlıkları atla
        upper = line.upper()
        if upper in ("SYSTEM PROMPT", "INPUT", "USER", "ASSISTANT"):
            continue
        if len(line) > max_len:
            return line[:max_len].rstrip() + "…"
        return line
    return "(başlıksız)"


with tab_compose:
    cap = len(ready_profiles)
    today_counts = usage_today_by_profile(load_jobs())
    daily_capacity_remaining = sum(
        max(0, (p.daily_limit if p.daily_limit > 0 else 10**9) - today_counts.get(p.id, 0))
        for p in ready_profiles
    )
    parallel_capacity = sum(p.max_concurrent for p in ready_profiles)

    if cap == 0:
        st.warning(
            "Henüz login olmuş profil yok. Soldan bir profil ekleyip 'Login başlat'a tıkla."
        )
    else:
        cap_str = "∞" if daily_capacity_remaining > 10**8 else str(daily_capacity_remaining)
        st.caption(
            f"{cap} hazır profil · {parallel_capacity} paralel slot · "
            f"bugünkü kalan kapasite: **{cap_str}** video. "
            "Round-robin ile en az kullanılan hesaba öncelik."
        )

    drafts = load_drafts()

    # ----- Yeni içerik ekle -----
    st.subheader("Yeni içerik ekle")
    editing_id = st.session_state.get("editing_draft_id")
    editing_draft = next((d for d in drafts if d.id == editing_id), None) if editing_id else None

    with st.form("draft_form", clear_on_submit=not editing_draft):
        default_title = editing_draft.title if editing_draft else ""
        default_content = editing_draft.content if editing_draft else ""
        new_title = st.text_input(
            "Başlık (opsiyonel — boş bırakırsan içerikten türetilir)",
            value=default_title,
        )
        new_content = st.text_area(
            "İçerik (NotebookLM'e gönderilecek metin — system prompt + input dahil)",
            value=default_content,
            height=320,
            placeholder="SYSTEM PROMPT\nYou are...\n\nINPUT\nTOPIC: ...\n",
        )
        submit_label = "Güncelle" if editing_draft else "İçerik ekle"
        submitted = st.form_submit_button(submit_label, type="primary")
        if submitted:
            content = new_content.strip()
            if not content:
                st.error("İçerik boş olamaz.")
            else:
                title = new_title.strip() or _auto_title(content)
                ds = load_drafts()
                if editing_draft:
                    for d in ds:
                        if d.id == editing_draft.id:
                            d.title = title
                            d.content = content
                            d.last_modified = time.time()
                    save_drafts(ds)
                    st.session_state["editing_draft_id"] = None
                    st.success("Güncellendi.")
                else:
                    ds.append(Draft(id=uuid.uuid4().hex[:10], title=title, content=content))
                    save_drafts(ds)
                    st.success(f"'{title}' eklendi.")
                st.rerun()

    if editing_draft:
        if st.button("Düzenlemeyi iptal et"):
            st.session_state["editing_draft_id"] = None
            st.rerun()

    st.divider()

    # ----- Hazırlanmış içerik listesi -----
    drafts = load_drafts()  # form sonrası yenile
    st.subheader(f"Hazırlanmış içerikler ({len(drafts)})")

    if not drafts:
        st.info("Henüz içerik eklenmedi. Yukarıdaki formdan ekle.")
    else:
        # Bulk actions
        action_cols = st.columns([1, 1, 1, 3])
        with action_cols[0]:
            select_all = st.checkbox("Hepsini seç", key="select_all_drafts")
        with action_cols[1]:
            queue_after_keep = st.checkbox(
                "Kuyruğa ekleyince sil", value=True, key="delete_after_queue"
            )
        with action_cols[2]:
            if st.button("Seçilenleri sil"):
                selected = [
                    d.id for d in drafts
                    if st.session_state.get(f"draft_chk_{d.id}", False)
                ]
                if selected:
                    remaining = [d for d in drafts if d.id not in selected]
                    save_drafts(remaining)
                    # Widget key'leri (draft_chk_*) instantiate edildikten
                    # sonra session_state'ten silinemez. Drafts silindikçe
                    # checkbox'lar bir sonraki rerun'da otomatik kaybolur.
                    st.success(f"{len(selected)} içerik silindi.")
                    st.rerun()

        # Liste
        st.write("")
        for d in sorted(drafts, key=lambda x: x.last_modified, reverse=True):
            with st.container(border=True):
                row = st.columns([0.5, 5, 1, 1, 1])
                with row[0]:
                    default_chk = select_all
                    chk = st.checkbox(
                        "",
                        value=st.session_state.get(f"draft_chk_{d.id}", default_chk),
                        key=f"draft_chk_{d.id}",
                        label_visibility="collapsed",
                    )
                with row[1]:
                    st.markdown(f"**{d.title}**")
                    word_count = len(d.content.split())
                    char_count = len(d.content)
                    st.caption(
                        f"{word_count} kelime · {char_count} karakter · "
                        f"güncellendi: {fmt_time(d.last_modified)}"
                    )
                    with st.expander("İçeriği gör"):
                        st.code(d.content, language="text")
                with row[2]:
                    if st.button("✏️", key=f"edit_{d.id}", help="Düzenle"):
                        st.session_state["editing_draft_id"] = d.id
                        st.rerun()
                with row[3]:
                    if st.button(
                        "▶ Kuyruğa",
                        key=f"queue_one_{d.id}",
                        disabled=cap == 0,
                        help="Bunu tek başına kuyruğa ekle",
                    ):
                        jobs = load_jobs()
                        jobs.append(Job(
                            id=uuid.uuid4().hex[:10],
                            text=d.content,
                            title=d.title,
                        ))
                        save_jobs(jobs)
                        if queue_after_keep:
                            save_drafts([x for x in drafts if x.id != d.id])
                            st.session_state.pop(f"draft_chk_{d.id}", None)
                        st.success("Kuyruğa eklendi.")
                        st.rerun()
                with row[4]:
                    if st.button("🗑", key=f"del_d_{d.id}", help="Sil"):
                        save_drafts([x for x in drafts if x.id != d.id])
                        st.session_state.pop(f"draft_chk_{d.id}", None)
                        st.rerun()

        # Bulk add to queue
        st.write("")
        st.divider()
        selected_ids = [
            d.id for d in drafts
            if st.session_state.get(f"draft_chk_{d.id}", False)
        ]
        sel_count = len(selected_ids)
        bulk_cols = st.columns([2, 1, 3])
        with bulk_cols[0]:
            if st.button(
                f"▶ Seçilenleri kuyruğa ekle ({sel_count})",
                type="primary",
                disabled=sel_count == 0 or cap == 0,
            ):
                jobs = load_jobs()
                drafts_now = load_drafts()
                drafts_by_id = {d.id: d for d in drafts_now}
                added = 0
                for sid in selected_ids:
                    d = drafts_by_id.get(sid)
                    if not d:
                        continue
                    jobs.append(Job(
                        id=uuid.uuid4().hex[:10],
                        text=d.content,
                        title=d.title,
                    ))
                    added += 1
                save_jobs(jobs)
                if queue_after_keep:
                    remaining = [d for d in drafts_now if d.id not in selected_ids]
                    save_drafts(remaining)
                # NOT: widget key'leri (select_all_drafts, draft_chk_*) widget
                # instantiate edildikten sonra session_state'ten modify edilemez.
                # queue_after_keep=True ise drafts zaten silindi → checkbox'lar
                # otomatik kaybolur. False ise checkbox'lar işaretli kalır,
                # kullanıcı isterse kendisi açar.
                st.success(f"{added} içerik kuyruğa eklendi.")
                st.rerun()

    st.divider()
    # Hızlı yapıştır (eski mod) — kısa one-liner'lar için
    with st.expander("Hızlı yapıştır (her satır = bir kısa prompt)"):
        quick_text = st.text_area(
            "Tek satırlık kısa prompt'lar — uzun system prompt'lar için yukarıdaki formu kullan",
            height=120,
            placeholder="örümcekler nasıl yürür\nkahve nasıl yapılır",
            key="quick_paste",
        )
        if st.button("Hızlı kuyruğa ekle", disabled=cap == 0):
            lines = [l.strip() for l in quick_text.splitlines() if l.strip()]
            if not lines:
                st.warning("Boş.")
            else:
                jobs = load_jobs()
                for line in lines:
                    jobs.append(Job(id=uuid.uuid4().hex[:10], text=line, title=line[:60]))
                save_jobs(jobs)
                st.success(f"{len(lines)} kısa prompt eklendi.")
                st.rerun()


with tab_status:
    jobs = load_jobs()
    counts = {"queued": 0, "running": 0, "done": 0, "submitted": 0, "failed": 0}
    for j in jobs:
        counts[j.status] = counts.get(j.status, 0) + 1
    cols = st.columns(5)
    cols[0].metric("⏳ Kuyrukta", counts["queued"])
    cols[1].metric("▶ Çalışan", counts["running"])
    cols[2].metric("✓ Tamamlanan", counts["done"])
    cols[3].metric("⏱ Süresi geçti", counts["submitted"])
    cols[4].metric("✗ Hatalı", counts["failed"])

    cols2 = st.columns([1, 1, 4])
    with cols2[0]:
        if st.button("Tamamlananları temizle", key="clean_done"):
            jobs_now = [
                j for j in load_jobs() if j.status not in ("done", "failed")
            ]
            save_jobs(jobs_now)
            st.rerun()
    with cols2[1]:
        if st.button("Submitted'ları yeniden dene", key="retry_submitted"):
            # followup_attempts'i sıfırla, follow-up worker tekrar denesin
            js = load_jobs()
            for j in js:
                if j.status == "submitted":
                    j.followup_attempts = 0
            save_jobs(js)
            st.success("Tüm submitted job'lar tekrar denenecek (10 dk içinde).")
            st.rerun()

    if not jobs:
        st.info("Henüz iş yok.")
    else:
        rows = []
        prof_by_id = {p.id: p for p in load_profiles()}
        for j in sorted(jobs, key=lambda x: x.created_at, reverse=True):
            prof_name = prof_by_id[j.profile_id].name if j.profile_id and j.profile_id in prof_by_id else "—"
            display_text = j.title or (
                (j.text[:60] + "…") if len(j.text) > 60 else j.text
            )
            video_status = "✓" if j.video_path else "—"
            rows.append(
                {
                    "Durum": f"{status_emoji(j.status)} {j.status}",
                    "Başlık / Metin": display_text,
                    "Profil": prof_name,
                    "Başlangıç": fmt_time(j.started_at),
                    "Süre": fmt_duration(j),
                    "Video": video_status,
                    "Notebook": j.notebook_url or "",
                    "Hata": (j.error or "")[:80],
                }
            )
        st.dataframe(
            rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Notebook": st.column_config.LinkColumn(
                    "Notebook",
                    display_text="🌐 Aç",
                    help="Tıkla → varsayılan tarayıcında NotebookLM açılır, oradan elle indirebilirsin",
                ),
            },
        )

        # Notebook URL olan job'lar için ayrıca tek tıkla aç butonu
        jobs_with_url = [j for j in jobs if j.notebook_url]
        if jobs_with_url:
            st.divider()
            st.caption("📥 Manuel indirme — bu butonlar varsayılan tarayıcında açar:")
            for j in sorted(jobs_with_url, key=lambda x: x.created_at, reverse=True)[:10]:
                title = j.title or (j.text[:50] + "…" if len(j.text) > 50 else j.text)
                cols = st.columns([5, 1])
                with cols[0]:
                    st.markdown(f"**{status_emoji(j.status)} {title}**")
                    st.caption(j.notebook_url)
                with cols[1]:
                    if st.button("Aç", key=f"open_{j.id}"):
                        subprocess.Popen(["open", j.notebook_url])
                        st.toast(f"Açıldı: {title[:30]}")

        # CSV export
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Durum", "Metin", "Profil", "Olusturulma", "Baslangic",
            "Bitis", "Sure_sn", "Notebook_URL", "Hata",
        ])
        for j in sorted(jobs, key=lambda x: x.created_at):
            prof_name = (
                prof_by_id[j.profile_id].name
                if j.profile_id and j.profile_id in prof_by_id
                else ""
            )
            duration = (
                int((j.finished_at or time.time()) - j.started_at)
                if j.started_at
                else ""
            )
            writer.writerow([
                j.status,
                j.text,
                prof_name,
                fmt_time(j.created_at),
                fmt_time(j.started_at),
                fmt_time(j.finished_at),
                duration,
                j.notebook_url or "",
                j.error or "",
            ])
        st.download_button(
            "📥 Tüm joblari CSV olarak indir",
            data=buf.getvalue().encode("utf-8-sig"),  # BOM ile Excel TR uyumlu
            file_name=f"notebooklm_jobs_{time.strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )

    # Otomatik refresh: 3 saniyede bir yenilen
    if any(j.status in ("queued", "running") for j in jobs):
        time.sleep(3)
        st.rerun()


with tab_videos:
    st.subheader("İndirilen videolar")

    # Job'lardan video_path olanları al + dosya sisteminden gerçek dosyaları al
    jobs = load_jobs()
    job_videos = [j for j in jobs if j.video_path and Path(j.video_path).exists()]
    job_video_paths = {Path(j.video_path).resolve() for j in job_videos}

    # downloads/ klasöründe bulunan ama job'da olmayan dosyalar (manuel kopyalanmış olabilir)
    fs_videos = sorted(DOWNLOADS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    orphan_videos = [
        p for p in fs_videos if p.resolve() not in job_video_paths
    ]

    total = len(job_videos) + len(orphan_videos)
    total_size = sum(
        (Path(j.video_path).stat().st_size for j in job_videos if Path(j.video_path).exists()),
        0,
    ) + sum(p.stat().st_size for p in orphan_videos)

    cols = st.columns(3)
    cols[0].metric("Toplam video", total)
    cols[1].metric("Toplam boyut", f"{total_size / 1024 / 1024:.1f} MB")
    cols[2].metric("Submitted bekleyen", sum(1 for j in jobs if j.status == "submitted"))

    if total == 0:
        st.info(
            "Henüz video indirilmedi. Job'lar tamamlanınca videolar otomatik olarak "
            "data/downloads/ klasörüne iner ve burada listelenir."
        )
    else:
        st.caption(f"Klasör: {DOWNLOADS_DIR}")
        # Job'a bağlı videolar
        for j in sorted(job_videos, key=lambda x: x.finished_at or 0, reverse=True):
            vp = Path(j.video_path)
            with st.container(border=True):
                cols = st.columns([4, 1])
                with cols[0]:
                    st.markdown(f"**🎬 {j.title or j.text[:60]}**")
                    size_mb = vp.stat().st_size / 1024 / 1024
                    st.caption(
                        f"{vp.name} · {size_mb:.1f} MB · "
                        f"bitti: {fmt_time(j.finished_at)}"
                    )
                    if j.notebook_url:
                        st.caption(f"Notebook: {j.notebook_url}")
                with cols[1]:
                    with vp.open("rb") as f:
                        st.download_button(
                            "📥 İndir",
                            data=f.read(),
                            file_name=vp.name,
                            mime="video/mp4",
                            key=f"dl_{j.id}",
                        )

        # Orphan dosyalar
        if orphan_videos:
            st.divider()
            st.caption("Job kaydı olmayan dosyalar:")
            for vp in orphan_videos:
                with st.container(border=True):
                    cols = st.columns([4, 1])
                    with cols[0]:
                        st.markdown(f"**🎬 {vp.name}**")
                        size_mb = vp.stat().st_size / 1024 / 1024
                        st.caption(f"{size_mb:.1f} MB")
                    with cols[1]:
                        with vp.open("rb") as f:
                            st.download_button(
                                "📥 İndir",
                                data=f.read(),
                                file_name=vp.name,
                                mime="video/mp4",
                                key=f"dl_orph_{vp.name}",
                            )


with tab_logs:
    # launcher.log + init log'ları + job log'ları hepsini bir arada göster
    log_options: list[tuple[str, Path]] = []

    # 1) launcher.log (auto-update + venv kurulum)
    launcher_log = ROOT / "launcher.log"
    if launcher_log.exists():
        log_options.append(("[launcher] launcher.log", launcher_log))

    # 2) data/logs altındaki tüm .log dosyaları (job + init + followup)
    if LOGS_DIR.exists():
        for f in sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            label = f.name
            if label.startswith("init_"):
                label = f"[init] {label}"
            elif label.endswith(".followup.log"):
                label = f"[followup] {label}"
            else:
                label = f"[job] {label}"
            log_options.append((label, f))

    if not log_options:
        st.info("Henüz log yok.")
    else:
        choice_idx = st.selectbox(
            "Log dosyası",
            options=range(len(log_options)),
            format_func=lambda i: log_options[i][0],
        )
        chosen_path = log_options[choice_idx][1]
        try:
            content = chosen_path.read_text(encoding="utf-8", errors="replace")
            st.caption(f"{chosen_path}  ·  {len(content)} karakter")
            st.code(content[-8000:], language="text")
        except Exception as e:
            st.error(f"Log okunamadı: {e}")

        # Finder'da göster butonu (kolay erişim için)
        if st.button("📂 Finder'da log klasörünü aç"):
            subprocess.Popen(["open", str(chosen_path.parent)])
