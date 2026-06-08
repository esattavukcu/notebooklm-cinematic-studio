"""
notebooklm_client.py — teng-lin/notebooklm-py Python wrapper.

tmc/nlm Go CLI + Playwright harvest cycle'ı tek bir Python kütüphanesiyle
değiştirir. Native MP4 download → cookie-fetch harvest döngüsüne ihtiyaç yok.

Auth: Mevcut chrome_profiles/<id>/auth.json (Playwright storage_state) doğrudan
kabul ediliyor (NotebookLMClient.from_storage). Init flow değişmiyor.

Sync facade: Streamlit Worker thread'inde asyncio.run() ile async API çağrılır,
caller sync görünür. Tüm pipeline (notebook create → sources upload → cinematic
generate → wait → download) tek bir coroutine'de.

API:
- submit_job(profile_id, title, source_paths, custom_prompt, on_event)
    Full pipeline. on_event(event, **payload) callback ile her aşama bildirilir.
    NOT: callback'in ilk positional parametresi `event` olmalı — `name`
    kullanma, çünkü payload'da `name=<source filename>` gibi kwarg geliyor
    ve TypeError ("multiple values for 'name'") verir.
- smoke_test(profile_id) → (ok, info_string)
- NotebookLMClientError — wrapper error class

Kullanım (app.py worker thread):
    result = submit_job(
        profile_id="1ee0e5c16713",
        title="Bush Passion Fruit",
        source_paths=[Path("script.txt"), Path("img1.jpg"), ...],
        custom_prompt="Role: ... Sources: ...",
        on_event=lambda event, **kw: log_fp.write(f"{event}: {kw}\\n"),
    )
    # result: {"notebook_id", "notebook_url", "task_id",
    #          "local_mp4", "duration_sec"}
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from notebooklm import NotebookLMClient
    from notebooklm.exceptions import NotebookLMError as _UpstreamError
    _AVAILABLE = True
except ImportError as _imp_err:
    NotebookLMClient = None  # type: ignore
    _UpstreamError = Exception  # type: ignore
    _AVAILABLE = False
    _IMPORT_ERR = str(_imp_err)


PROFILES_DIR_DEFAULT = Path(__file__).parent / "chrome_profiles"


class NotebookLMClientError(Exception):
    """notebooklm-py kütüphanesi hata wrapper'ı."""

    def __init__(self, message: str, stage: str = "", cause: Optional[Exception] = None):
        super().__init__(message)
        self.stage = stage  # 'auth' | 'notebook_create' | 'source_add' | 'video_gen' | 'video_wait' | 'video_download'
        self.cause = cause

    def __str__(self) -> str:
        base = super().__str__()
        if self.stage:
            base = f"[{self.stage}] {base}"
        return base


def auth_path_for(profile_id: str,
                  profiles_dir: Optional[Path] = None) -> Path:
    """chrome_profiles/<id>/auth.json path'ini döner. Kontrol etmez."""
    base = profiles_dir or PROFILES_DIR_DEFAULT
    return base / profile_id / "auth.json"


def is_available() -> tuple[bool, str]:
    """notebooklm-py kütüphanesi yüklü mü?"""
    if _AVAILABLE:
        try:
            import notebooklm
            return True, f"notebooklm-py v{getattr(notebooklm, '__version__', '?')}"
        except Exception:
            return True, "notebooklm-py installed"
    return False, f"notebooklm-py import hatası: {_IMPORT_ERR}"


# ---------------------------------------------------------------------------
# Core async pipeline
# ---------------------------------------------------------------------------
async def _submit_job_async(
    auth_path: Path,
    title: str,
    source_paths: list[Path],
    custom_prompt: str,
    on_event: Callable[..., None],
    *,
    language: str = "tr",
    video_timeout_sec: float = 3600.0,  # 1h — Cinematic Veo 3 30-40dk
    source_wait_timeout: float = 180.0,
) -> dict[str, Any]:
    """Full pipeline: create notebook + upload sources + generate cinematic
    + wait + download. on_event callback her aşamada çağrılır."""
    if not _AVAILABLE:
        raise NotebookLMClientError(
            "notebooklm-py kütüphanesi yok. pip install notebooklm-py",
            stage="import",
        )
    if not auth_path.exists():
        raise NotebookLMClientError(
            f"auth.json yok: {auth_path}", stage="auth",
        )

    t_start = time.time()
    on_event("client_starting", auth=str(auth_path))

    # keepalive=300 → cookie SIDTS rotation otomatik (long-running 30dk+).
    # v0.5.0: from_storage() doğrudan async-context-manager wrapper döndürür
    # (await deprecated, v1.0'da kaldırılacak). Auth/connection lifecycle
    # hataları async-with __aenter__ aşamasında raise edilir, body try'larında
    # yakalanır veya caller'a fırlar.
    try:
        client_ctx = NotebookLMClient.from_storage(
            path=str(auth_path), keepalive=300, keepalive_min_interval=60,
        )
    except Exception as e:
        raise NotebookLMClientError(
            f"client creation: {type(e).__name__}: {e}",
            stage="auth", cause=e,
        )

    async with client_ctx as c:
        on_event("client_connected")

        # 1. Notebook create
        try:
            notebook = await c.notebooks.create(title or "Untitled")
        except Exception as e:
            raise NotebookLMClientError(
                f"notebook create: {type(e).__name__}: {e}",
                stage="notebook_create", cause=e,
            )
        nb_id = notebook.id
        nb_url = f"https://notebooklm.google.com/notebook/{nb_id}"
        on_event("notebook_created", id=nb_id, url=nb_url, title=title)

        # 2. Sources upload (serial — concurrency limiti Google tarafında düşük)
        source_ids: list[str] = []
        for i, sp in enumerate(source_paths):
            if not sp.exists():
                on_event("source_skipped_missing", path=str(sp))
                continue
            on_event("source_uploading", idx=i + 1, total=len(source_paths),
                     name=sp.name, size=sp.stat().st_size)
            try:
                s = await c.sources.add_file(nb_id, str(sp), wait=False)
            except Exception as e:
                on_event("source_failed", name=sp.name, error=str(e)[:200])
                continue  # partial OK
            source_ids.append(s.id)
            on_event("source_added", id=s.id, name=sp.name)

        if not source_ids:
            raise NotebookLMClientError(
                "Hiç source yüklenemedi — generation anlamsız.",
                stage="source_add",
            )

        # 3. Sources'ın processing'i bitmesini bekle.
        # NOT: wait_for_sources imzası (notebook_id, source_ids, timeout=...) —
        # source_ids ZORUNLU pozisyonel (0.6.0 + 0.7.1 aynı). Eskiden geçilmiyordu
        # → her çağrı TypeError → bekleme TAMAMEN atlanıyordu (gen, source'lar
        # processing bitmeden başlayabiliyordu). Artık source_ids geçiliyor →
        # source'lar gerçekten hazır olana kadar beklenir → daha güvenilir gen.
        # Timeout/processing-fail → SourceTimeoutError → aşağıdaki except yakalar,
        # best-effort devam (eski davranış korunur).
        on_event("sources_waiting", count=len(source_ids))
        try:
            await c.sources.wait_for_sources(
                nb_id, source_ids, timeout=source_wait_timeout,
            )
        except Exception as e:
            # Timeout veya bazıları işlenemedi → yine de devam et
            on_event("sources_wait_partial", error=str(e)[:200])
        on_event("sources_ready", count=len(source_ids))

        # 4. Cinematic video generate
        on_event("video_gen_starting", language=language,
                 prompt_chars=len(custom_prompt or ""))
        try:
            gen = await c.artifacts.generate_cinematic_video(
                nb_id,
                source_ids=None,  # Tüm sources'ı kullan
                language=language,
                instructions=(custom_prompt or None),
            )
        except Exception as e:
            # Bazı hesaplarda 'Cinematic' Ultra subscription gerektirir → fallback
            err_msg = str(e).lower()
            if "ultra" in err_msg or "subscription" in err_msg or "403" in err_msg:
                on_event("cinematic_unavailable_fallback_to_standard")
                try:
                    from notebooklm import VideoFormat, VideoStyle
                    gen = await c.artifacts.generate_video(
                        nb_id,
                        video_format=VideoFormat.EXPLAINER,
                        video_style=VideoStyle.AUTO_SELECT,
                        language=language,
                        instructions=(custom_prompt or None),
                    )
                except Exception as e2:
                    raise NotebookLMClientError(
                        f"video gen (standard fallback): {type(e2).__name__}: {e2}",
                        stage="video_gen", cause=e2,
                    )
            else:
                raise NotebookLMClientError(
                    f"video gen: {type(e).__name__}: {e}",
                    stage="video_gen", cause=e,
                )
        task_id = gen.task_id
        on_event("video_gen_started", task_id=task_id)

        # 5. Wait for completion (Cinematic: 30-40dk)
        # Library'nin "artifact removed... may indicate quota/rate limit OR
        # invalid notebook ID OR transient API issue" hatası MUĞLAK — bunu
        # kesin quota sayıp gen'i terk etmek FALSE-POSITIVE yaratıyordu
        # (Google videoyu aslında tamamlıyor). Bu yüzden wait/download hata
        # verse bile aşağıda list_video ile GERÇEKTEN tamamlanmış video var mı
        # kontrol edip indiriyoruz. Sadece hiç video yoksa hata fırlatıyoruz.
        on_event("video_waiting", timeout_sec=video_timeout_sec)
        wait_err = None
        try:
            final = await c.artifacts.wait_for_completion(
                nb_id, task_id,
                initial_interval=15.0,
                max_interval=60.0,
                timeout=video_timeout_sec,
            )
            if not getattr(final, "is_complete", False):
                wait_err = f"is_complete=False (error={getattr(final, 'error', '?')})"
        except Exception as e:
            wait_err = f"{type(e).__name__}: {e}"

        download_dir = Path(__file__).parent / "data" / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)
        out_path = download_dir / f"{nb_id}.mp4"
        downloaded_artifact = task_id

        def _ok() -> bool:
            return out_path.exists() and out_path.stat().st_size >= 10_000

        # 6a. Normal yol — wait başarılıysa task_id ile indir
        if wait_err is None:
            on_event("video_downloading", path=str(out_path))
            try:
                await c.artifacts.download_video(
                    nb_id, str(out_path), artifact_id=task_id,
                )
            except Exception as e:
                wait_err = f"download: {type(e).__name__}: {e}"

        # 6b. FALLBACK — wait veya download hata verdiyse: list_video ile
        # gerçekten tamamlanmış (status=3 / is_complete) video var mı bak,
        # varsa onu indir. Bu, muğlak hata + Google'ın tamamladığı durumu
        # kurtarır (false-positive quota'yı önler).
        if not _ok():
            on_event("video_verify_via_list", reason=str(wait_err)[:120])
            try:
                vids = await c.artifacts.list_video(nb_id)
                vids = list(vids) if vids else []
                done_v = [
                    v for v in vids
                    if getattr(v, "status", None) == 3
                    or getattr(v, "is_complete", False)
                ]
                if done_v:
                    v = done_v[0]
                    downloaded_artifact = getattr(v, "id", task_id)
                    await c.artifacts.download_video(
                        nb_id, str(out_path), artifact_id=downloaded_artifact,
                    )
                    if _ok():
                        on_event("video_recovered", artifact_id=downloaded_artifact)
            except Exception as e:
                on_event("video_verify_failed", error=str(e)[:120])

        # 6c. Hâlâ indirilemedi → gerçekten video yok (quota/fail).
        if not _ok():
            raise NotebookLMClientError(
                f"video unavailable: {wait_err or 'no completed artifact'}",
                stage="video_wait",
            )

        on_event("video_downloaded", path=str(out_path),
                 size_mb=out_path.stat().st_size // (1024 * 1024))

        return {
            "notebook_id": nb_id,
            "notebook_url": nb_url,
            "task_id": downloaded_artifact,
            "source_ids": source_ids,
            "local_mp4": str(out_path),
            "duration_sec": int(time.time() - t_start),
        }


# ---------------------------------------------------------------------------
# Sync facade (Streamlit Worker thread için)
# ---------------------------------------------------------------------------
def submit_job(
    profile_id: str,
    title: str,
    source_paths: list[Path],
    custom_prompt: str,
    on_event: Callable[..., None],
    *,
    profiles_dir: Optional[Path] = None,
    language: str = "tr",
    video_timeout_sec: float = 3600.0,
) -> dict[str, Any]:
    """Sync wrapper — Worker thread'inde çağrılır. asyncio.run ile coroutine
    çalıştırır, tek bir event loop instance kullanılır."""
    auth_path = auth_path_for(profile_id, profiles_dir)
    return asyncio.run(_submit_job_async(
        auth_path=auth_path,
        title=title,
        source_paths=source_paths,
        custom_prompt=custom_prompt,
        on_event=on_event,
        language=language,
        video_timeout_sec=video_timeout_sec,
    ))


# ---------------------------------------------------------------------------
# Smoke test (admin diagnostic)
# ---------------------------------------------------------------------------
async def _smoke_async(auth_path: Path) -> tuple[bool, str]:
    """Quick connectivity + auth check: notebook list çağırır."""
    if not _AVAILABLE:
        return False, f"notebooklm-py yüklü değil: {_IMPORT_ERR}"
    if not auth_path.exists():
        return False, f"auth.json yok: {auth_path}"
    try:
        async with NotebookLMClient.from_storage(path=str(auth_path)) as c:
            notebooks = await c.notebooks.list()
            nb_list = list(notebooks) if hasattr(notebooks, "__iter__") else notebooks
            return True, f"OK: {len(nb_list)} notebook görüldü."
    except Exception as e:
        return False, f"smoke FAIL: {type(e).__name__}: {str(e)[:200]}"


def smoke_test(profile_id: str,
               profiles_dir: Optional[Path] = None) -> tuple[bool, str]:
    """Senkron smoke test (admin sidebar widget'ı için)."""
    auth_path = auth_path_for(profile_id, profiles_dir)
    try:
        return asyncio.run(_smoke_async(auth_path))
    except Exception as e:
        return False, f"smoke loop error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Resume harvest — stuck "generating" job'lar için.
# notebook_url biliniyor ama thread öldü (server restart vs.) → notebook'a
# yeniden bağlan, var olan video artifact'ı bul, indir.
# ---------------------------------------------------------------------------
async def _resume_download_async(
    auth_path: Path,
    notebook_id: str,
    out_path: Path,
    *,
    wait_if_processing: bool = True,
    wait_timeout_sec: float = 1800.0,
) -> dict[str, Any]:
    """Var olan notebook'tan video artifact'ı bul, indir.

    Senaryo: thread öldü (restart vs.), notebook NotebookLM'de hâlâ duruyor,
    video gen tamamlanmış veya devam ediyor. Yeniden bağlan, listele, indir.
    """
    if not _AVAILABLE:
        raise NotebookLMClientError(
            "notebooklm-py kütüphanesi yok.", stage="import",
        )
    if not auth_path.exists():
        raise NotebookLMClientError(f"auth.json yok: {auth_path}", stage="auth")

    # v0.5.0: from_storage() async-context-manager döndürür, await istemez.
    try:
        client_ctx = NotebookLMClient.from_storage(
            path=str(auth_path), keepalive=300, keepalive_min_interval=60,
        )
    except Exception as e:
        raise NotebookLMClientError(
            f"client: {type(e).__name__}: {e}", stage="auth", cause=e,
        )

    async with client_ctx as c:
        # Video artifact listesi
        try:
            videos = await c.artifacts.list_video(notebook_id)
        except Exception as e:
            raise NotebookLMClientError(
                f"list_video: {type(e).__name__}: {e}",
                stage="resume_list", cause=e,
            )
        videos = list(videos) if videos else []
        if not videos:
            raise NotebookLMClientError(
                f"Notebook'ta video artifact yok ({notebook_id}). "
                f"Gen başlamadı veya silinmiş.",
                stage="resume_list",
            )
        # Tamamlanmış (ArtifactStatus.COMPLETED=3) artifact'ı tercih et; yoksa
        # en yeni (videos[0]). ÖNCEDEN sadece videos[0] alınıp string-status
        # kontrol ediliyordu → status int=3 (COMPLETED) tanınmıyordu → bitmiş
        # video "generating" sanılıp harvest edilemiyordu (kayıp video!).
        def _is_done(v) -> bool:
            if getattr(v, "is_complete", False):
                return True
            st = getattr(v, "status", None)
            try:
                if int(st) == 3:  # ArtifactStatus.COMPLETED
                    return True
            except (TypeError, ValueError):
                pass
            return str(st).lower() in (
                "ready", "complete", "done", "completed", "success",
            )

        completed = [v for v in videos if _is_done(v)]
        target = completed[0] if completed else videos[0]
        artifact_id = target.id

        is_ready = _is_done(target)
        if not is_ready and wait_if_processing:
            try:
                final = await c.artifacts.wait_for_completion(
                    notebook_id, artifact_id,
                    initial_interval=15.0, max_interval=60.0,
                    timeout=wait_timeout_sec,
                )
                if not getattr(final, "is_complete", False):
                    err = getattr(final, "error", "?")
                    raise NotebookLMClientError(
                        f"video gen failed: {err}", stage="resume_wait",
                    )
            except NotebookLMClientError:
                raise
            except Exception as e:
                raise NotebookLMClientError(
                    f"wait: {type(e).__name__}: {e}",
                    stage="resume_wait", cause=e,
                )

        # Download
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await c.artifacts.download_video(
                notebook_id, str(out_path), artifact_id=artifact_id,
            )
        except Exception as e:
            raise NotebookLMClientError(
                f"download: {type(e).__name__}: {e}",
                stage="resume_download", cause=e,
            )
        if not out_path.exists() or out_path.stat().st_size < 10_000:
            raise NotebookLMClientError(
                f"download produced empty/missing file: {out_path}",
                stage="resume_download",
            )

        return {
            "notebook_id": notebook_id,
            "artifact_id": artifact_id,
            "local_mp4": str(out_path),
            "size_mb": out_path.stat().st_size // (1024 * 1024),
        }


def resume_download(
    profile_id: str,
    notebook_id: str,
    out_path: Path,
    *,
    profiles_dir: Optional[Path] = None,
    wait_if_processing: bool = True,
    wait_timeout_sec: float = 1800.0,
) -> dict[str, Any]:
    """Sync wrapper — stuck job kurtarmak için."""
    auth_path = auth_path_for(profile_id, profiles_dir)
    return asyncio.run(_resume_download_async(
        auth_path=auth_path,
        notebook_id=notebook_id,
        out_path=out_path,
        wait_if_processing=wait_if_processing,
        wait_timeout_sec=wait_timeout_sec,
    ))


def notebook_id_from_url(url: str) -> Optional[str]:
    """https://notebooklm.google.com/notebook/<UUID> → UUID."""
    import re as _re
    m = _re.search(r"/notebook/([a-f0-9-]{16,})", url or "")
    return m.group(1) if m else None


__all__ = [
    "NotebookLMClientError",
    "auth_path_for",
    "is_available",
    "submit_job",
    "smoke_test",
    "resume_download",
    "notebook_id_from_url",
]
