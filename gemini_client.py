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
GEMINI_TIMEOUT_DEFAULT: int = 120  # text gen genelde 5-30s ama buffer
GEMINI_DEFAULT_MODEL: str = "flash"  # alias for gemini-2.5-flash

# UI selectbox için — kısa labellar (en yaygın 3 model)
GEMINI_MODELS: list[tuple[str, str]] = [
    ("flash", "Gemini Flash (varsayılan, hızlı + kaliteli)"),
    ("pro", "Gemini Pro (en yüksek kalite, biraz yavaş)"),
    ("flash-lite", "Gemini Flash Lite (en hızlı, basit görevler)"),
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


def _run_gemini(prompt: str, *, model: Optional[str] = None,
                timeout: int = GEMINI_TIMEOUT_DEFAULT,
                cwd: Optional[str] = None) -> dict:
    """gemini -p "<prompt>" -m <model> --output-format json --skip-trust --yolo

    Returns parsed JSON dict: {"session_id", "response"?, "error"?, "stats"?}.
    NotebookLM workspace dosyalarını context'e dahil etmemek için cwd /tmp.
    """
    cmd = [
        GEMINI_BIN,
        "-p", prompt,
        "--output-format", "json",
        "--skip-trust",
        "--yolo",  # tool/edit approval auto — text-only kullanıyoruz, etki yok
    ]
    if model:
        cmd += ["-m", model]

    # Context-isolation cwd — gemini-cli current dir'i bootstrap context'e
    # ekliyor (GEMINI.md, mevcut dosyalar). Temiz /tmp dizini kullanırsak
    # token overhead azalır.
    if cwd is None:
        # Boş bir tmp dizin oluştur (cleanup yok — /tmp zaten cleanup'ı OS yapar)
        cwd = tempfile.mkdtemp(prefix="gemini_cwd_")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_build_env(),
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError:
        raise GeminiError(
            f"gemini binary bulunamadı (PATH veya GEMINI_BIN_PATH): {GEMINI_BIN}. "
            f"Server'a 'npm install -g @google/gemini-cli' yapılmalı."
        )
    except subprocess.TimeoutExpired:
        raise GeminiError(f"gemini timeout ({timeout}s)")
    finally:
        # cwd tmp'i temizle (best-effort, hata olursa OS halleder)
        try:
            import shutil
            if cwd and "gemini_cwd_" in cwd:
                shutil.rmtree(cwd, ignore_errors=True)
        except Exception:
            pass

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

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
