"""
gemini_client.py — Google gemini-cli (OAuth) subprocess wrapper.

OpenRouter yerine Gemini 2.5/3 model'lere erişim için kullanılır. Auth:
gemini-cli'ın `~/.gemini/oauth_creds.json` dosyasındaki OAuth token (Sign-in
with Google). Lokalde browser flow ile create edilir; server'a rsync ile
taşınır. Access token expire olduğunda refresh_token otomatik kullanılır.

Bu modül SADECE TEXT generation için kullanılır. Image gen (nano-banana,
gemini-2.5-flash-image) OAuth tier'da erişilebilir DEĞİL (404 NotFound) —
Pollinations.ai için ayrı kod path'i kullanılır.

API:
- gemini_chat(prompt, *, model, system_prompt, ...) → (ok, text)
- gemini_smoke_test() → (ok, info_string)
- GEMINI_MODELS — UI selectbox için 3 öğeli liste

Binary path: env GEMINI_BIN_PATH (default 'gemini' in PATH).

Kullanım (app.py):
    from gemini_client import gemini_chat, GeminiError, gemini_smoke_test, GEMINI_MODELS

    ok, text = gemini_chat(
        "Write a haiku about cats.",
        model="flash",
        system_prompt="You are a poetry assistant.",
        timeout=60,
    )
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GEMINI_BIN: str = os.environ.get("GEMINI_BIN_PATH", "").strip() or "gemini"
GEMINI_TIMEOUT_DEFAULT: int = 300  # text gen: pro + uzun input 2-3 dk olabilir
GEMINI_DEFAULT_MODEL: str = "flash"  # alias for gemini-2.5-flash (default, fast)

# UI selectbox için — kısa labellar (en yaygın 3 model)
# NOT: 'pro' modeli "thinking" yapıyor, uzun input için 2-5 dk sürer.
# Default ve önerilen 'flash' — 5-30 sn arası, kalite genelde yeterli.
GEMINI_MODELS: list[tuple[str, str]] = [
    ("flash", "Gemini Flash (varsayılan — hızlı, 5-30sn)"),
    ("flash-lite", "Gemini Flash Lite (en hızlı, basit görevler için)"),
    ("pro", "Gemini Pro (en kaliteli ama YAVAŞ — uzun script'lerde 2-5dk)"),
]


class GeminiError(Exception):
    """gemini-cli subprocess hata wrapper'ı."""

    def __init__(self, message: str, returncode: int = 0,
                 stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self) -> str:
        base = super().__str__()
        if self.returncode:
            base += f" (exit {self.returncode})"
        return base


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------
def _build_env() -> dict[str, str]:
    """gemini-cli'ın istediği auth + trust env var'ları + parent env."""
    env = os.environ.copy()
    # OAuth Sign-in with Google flow — `~/.gemini/oauth_creds.json` kullanılır
    env["GOOGLE_GENAI_USE_GCA"] = "true"
    # Non-interactive headless mode trust check'i bypass
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    # Renkli output kapat (JSON parse'ı bozar)
    env["NO_COLOR"] = "1"
    return env


def _clean_gemini_context() -> None:
    """gemini-cli'in context-contamination kaynaklarını temizle.

    Gemini-CLI her çağrıda `~/.gemini/projects.json` (cwd registry) ve
    `~/.gemini/history/` (conversation cache) yazar. Bunlar TEKRARLI çağrılarda
    LLM'in context'ine sızabilir — bizim örneğimizde: Türkçe çevre script'i
    verdik, geriden gelen yanıt 'AI/coding workspace' temalı oldu (eski
    project cache'inden). Her çağrıdan önce sil → temiz session.

    Idempotent, side-effect free; oauth_creds.json + settings.json dokunulmaz.
    """
    try:
        import shutil
        gemini_dir = Path.home() / ".gemini"
        if not gemini_dir.exists():
            return
        # History (conversation transcripts cache)
        history_dir = gemini_dir / "history"
        if history_dir.exists():
            shutil.rmtree(history_dir, ignore_errors=True)
        # Projects registry (cwd → project_name mapping, contaminates context)
        for p in gemini_dir.glob("projects.json*"):
            try:
                p.unlink()
            except OSError:
                pass
        # Tmp / scratch dosyaları
        tmp_dir = gemini_dir / "tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        # Google account cache (server-side oauth state, bizim için irrelevant)
        ga = gemini_dir / "google_accounts.json"
        if ga.exists():
            try:
                ga.unlink()
            except OSError:
                pass
    except Exception:
        # Sessizce yut — temizlik fail olsa bile gemini çalışabilir
        pass


def _run_gemini(prompt: str, *, model: Optional[str] = None,
                timeout: int = GEMINI_TIMEOUT_DEFAULT,
                cwd: Optional[str] = None) -> dict:
    """gemini -p "" <stdin: prompt> -m <model> --output-format json

    Prompt STDIN üzerinden iletilir (argv length limit'ini bypass eder; uzun
    Turkish script'ler için kritik). Empty `-p ""` non-interactive mode'u
    tetikler, gemini-cli stdin'i prompt olarak okur.

    Returns parsed JSON dict: {"session_id", "response"?, "error"?, "stats"?}.
    """
    cmd = [
        GEMINI_BIN,
        "-p", "",          # stdin'i tetikle (gemini-cli help: "appended to input on stdin")
        "--output-format", "json",
        "--skip-trust",
        "--yolo",          # tool/edit approval auto — text-only, etki yok
    ]
    if model:
        cmd += ["-m", model]

    # Context-isolation cwd — gemini-cli current dir'i bootstrap context'e
    # ekliyor (GEMINI.md, mevcut dosyalar). Temiz tmp dizini kullanırsak
    # token overhead azalır + cross-call contamination olmaz.
    if cwd is None:
        cwd = tempfile.mkdtemp(prefix="gemini_cwd_")

    # ~/.gemini/projects.json + history/ cleanup — context contamination önler.
    # Bunlar olmadığında gemini her çağrıyı temiz session olarak işler.
    _clean_gemini_context()

    # subprocess.run timeout fırlattığında child process killlemez; Popen +
    # manuel timeout daha güvenilir, orphan node process bırakmıyor.
    # stdin=PIPE → prompt argv yerine stdin'den iletilir (argv limit bypass).
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_build_env(),
            cwd=cwd,
            start_new_session=True,  # child'ı kendi proc group'una koy → tüm tree kill edilebilir
        )
    except FileNotFoundError:
        raise GeminiError(
            f"gemini binary bulunamadı (PATH veya GEMINI_BIN_PATH): {GEMINI_BIN}. "
            f"Server'a 'npm install -g @google/gemini-cli' yapılmalı."
        )

    try:
        # Prompt'u stdin'den ver — uzun input'lar için argv limit'i (~256KB Mac,
        # ~128KB Linux) güvenle aşılır.
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Tüm process group'u öldür (gemini → node child'ları dahil)
        try:
            import signal as _signal
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise GeminiError(f"gemini timeout ({timeout}s)")
    finally:
        # Safety: hâlâ çalışıyorsa kapat + tmp cwd temizle (OS'a bırakmaktansa)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            import shutil
            if cwd and "gemini_cwd_" in cwd:
                shutil.rmtree(cwd, ignore_errors=True)
        except Exception:
            pass

    # stdout/stderr communicate()'den lokal variable olarak geldi
    stdout = stdout or ""
    stderr = stderr or ""

    # gemini-cli bazen stdout başında log/warning satırları basıyor:
    # "Warning: 256-color..." "Ripgrep is not available..."
    # JSON'ın `{` ile başladığı yeri bul.
    json_start = stdout.find("{")
    if json_start < 0:
        raise GeminiError(
            f"gemini stdout'da JSON yok. stderr: {stderr[:200]}",
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    json_text = stdout[json_start:]
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise GeminiError(
            f"gemini JSON parse hatası: {e.msg}. stdout[:300]: {stdout[:300]!r}",
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return data


# ---------------------------------------------------------------------------
# Top-level command
# ---------------------------------------------------------------------------
def gemini_chat(prompt: str, *,
                model: Optional[str] = None,
                system_prompt: Optional[str] = None,
                temperature: float = 0.7,  # kabul edilir ama gemini-cli ignore
                max_tokens: Optional[int] = None,  # kabul edilir ama gemini-cli ignore
                json_mode: bool = False,  # not used (Gemini'nin response her zaman string)
                timeout: int = GEMINI_TIMEOUT_DEFAULT,
                ) -> tuple[bool, str]:
    """Gemini'ye prompt gönder, text dön.

    (ok, text_or_error_message) döner.

    Parameters:
      - prompt: User prompt
      - model: 'flash' | 'pro' | 'flash-lite' (alias) veya full model name
      - system_prompt: prepend edilir (gemini-cli native system_prompt flag yok)
      - temperature/max_tokens: API-level değil, ignored
      - json_mode: prompt'ta zaten "JSON çıktı ver" deniyorsa True, no-op
      - timeout: subprocess timeout (saniye)

    Error mesajları kullanıcı dostu:
      - Auth eksik → "Gemini OAuth: ..."
      - Rate limit → "Gemini rate-limit: ..."
      - Model not found → "Model bulunamadı: ..."
    """
    if not (prompt or "").strip():
        return False, "prompt boş olamaz."

    # System prompt'u user prompt'a prepend et (gemini-cli native yok)
    if system_prompt and system_prompt.strip():
        full_prompt = f"{system_prompt.strip()}\n\n---\n\n{prompt.strip()}"
    else:
        full_prompt = prompt.strip()

    try:
        data = _run_gemini(full_prompt, model=model, timeout=timeout)
    except GeminiError as e:
        return False, _friendly_error(str(e), getattr(e, "stderr", "") or "")
    except Exception as e:
        return False, f"Gemini hatası: {type(e).__name__}: {str(e)[:200]}"

    # API error JSON içinde
    err = data.get("error")
    if err:
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        return False, _friendly_error(msg, "")

    response = data.get("response", "")
    if not isinstance(response, str):
        return False, f"Gemini beklenmeyen response tipi: {type(response).__name__}"

    return True, response


def _friendly_error(msg: str, stderr: str) -> str:
    """Gemini error mesajını kullanıcıya gösterilebilir formata çevir."""
    full = (msg + " " + stderr).lower()
    if "auth method" in full or "set an auth method" in full:
        return ("Gemini OAuth: auth method seçilmemiş. "
                "Settings.json'da `selectedAuthType: oauth-personal` veya "
                "env'de `GOOGLE_GENAI_USE_GCA=true` olmalı.")
    if "401" in full or "unauthorized" in full or "invalid_grant" in full:
        return ("Gemini OAuth token expired/invalid. Lokalde `gemini` ile "
                "tekrar login + ~/.gemini/oauth_creds.json server'a rsync.")
    if "rate" in full and ("limit" in full or "quota" in full):
        return f"Gemini rate-limit/quota: {msg[:200]}"
    if "modelnotfound" in full.replace(" ", "") or "model not found" in full:
        return f"Gemini model bulunamadı: {msg[:200]}"
    if "trusted directory" in full or "not running in a trusted" in full:
        return "Gemini trust check: --skip-trust flag missing (internal bug)"
    if "timeout" in full:
        return f"Gemini timeout: {msg[:200]}"
    return f"Gemini hatası: {msg[:300]}"


# ---------------------------------------------------------------------------
# Smoke test (admin diagnostic)
# ---------------------------------------------------------------------------
def gemini_smoke_test() -> tuple[bool, str]:
    """gemini binary var mı + auth OK mı? (ok, info_or_error) döner."""
    # 1. Binary check
    try:
        proc = subprocess.run(
            [GEMINI_BIN, "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except FileNotFoundError:
        return False, (
            f"gemini binary yok (PATH veya GEMINI_BIN_PATH={GEMINI_BIN!r}). "
            f"Server'a kurmak için: npm install -g @google/gemini-cli"
        )
    except Exception as e:
        return False, f"smoke binary check: {type(e).__name__}: {e}"

    if proc.returncode != 0:
        return False, f"gemini --version exit {proc.returncode}: {proc.stderr[:120]}"

    version = (proc.stdout or "").strip().splitlines()[0] if proc.stdout else "?"

    # 2. Auth check — küçük bir ping
    ok, response = gemini_chat(
        "Reply with exactly: PONG",
        model="flash-lite",  # en hızlı
        timeout=30,
    )
    if not ok:
        return False, f"gemini text gen FAIL (v{version}): {response[:200]}"

    return True, f"gemini OK (v{version}) — text ping yanıt: {response[:60]!r}"


__all__ = [
    "GeminiError",
    "GEMINI_BIN",
    "GEMINI_MODELS",
    "GEMINI_DEFAULT_MODEL",
    "gemini_chat",
    "gemini_smoke_test",
]
