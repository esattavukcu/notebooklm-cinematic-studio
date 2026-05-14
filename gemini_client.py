"""
gemini_client.py — Google Gemini text generation wrapper.

İki path destekler, env-driven:

1. **API mode (önerilen)**: `GEMINI_API_KEY` set ise `google-genai` SDK ile
   doğrudan API'ye gider. Hızlı (subprocess yok), reliable, native rate-limit.
   API key: https://aistudio.google.com/apikey

2. **CLI mode (fallback)**: API key yoksa eski gemini-cli (OAuth Sign-in)
   subprocess yolu — `~/.gemini/oauth_creds.json`. Refresh token expire
   olursa lokalde 'gemini' ile re-login + rsync gerekir.

Public API aynı kalır (gemini_chat, gemini_smoke_test, GEMINI_MODELS) —
app.py'da import değişmez, env'e GEMINI_API_KEY eklenince otomatik API'ye geçer.

API mode avantajları:
- subprocess yok (Node.js + 100MB RAM tasarrufu)
- settings.json / projects.json / history cleanup yok
- temperature + max_tokens + json_mode artık gerçekten çalışıyor
- Native exception types (rate limit, auth, model_not_found ayrı catch'lenebilir)
- Senkron çağrı 5-30sn vs CLI ortalama 30-60sn

Bu modül SADECE TEXT generation için. Image gen (nano-banana) ayrı path —
Pollinations veya AI Studio image API (key gerekirse) separate file.

API:
- gemini_chat(prompt, *, model, system_prompt, temperature, max_tokens,
              json_mode, timeout) → (ok, text)
- gemini_smoke_test() → (ok, info_string)
- GEMINI_MODELS — UI selectbox için 3 öğeli liste
- GeminiError — exception wrapper

Kullanım (app.py — değişmedi):
    from gemini_client import gemini_chat, GeminiError, gemini_smoke_test, GEMINI_MODELS
    ok, text = gemini_chat(
        "Write a haiku about cats.",
        model="flash",
        system_prompt="You are a poetry assistant.",
        json_mode=False,
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
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_BIN: str = os.environ.get("GEMINI_BIN_PATH", "").strip() or "gemini"
GEMINI_TIMEOUT_DEFAULT: int = 120  # API mode 5-30sn, CLI mode 30-300sn
GEMINI_DEFAULT_MODEL: str = "flash"  # alias for gemini-2.5-flash

# Model alias map (kısa ad → full API model name)
GEMINI_MODEL_ALIAS: dict[str, str] = {
    "flash": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",
    "flash-lite": "gemini-2.5-flash-lite",
}

# UI selectbox için
GEMINI_MODELS: list[tuple[str, str]] = [
    ("flash", "Gemini 2.5 Flash (varsayılan — hızlı, 5-30sn)"),
    ("flash-lite", "Gemini 2.5 Flash Lite (en hızlı, basit görevler için)"),
    ("pro", "Gemini 2.5 Pro (en kaliteli ama yavaş — 30-120sn)"),
]

# SDK import — opsiyonel, sadece API mode için
try:
    from google import genai as _genai
    from google.genai import types as _genai_types  # type: ignore
    _SDK_AVAILABLE = True
except ImportError as _sdk_err:
    _genai = None  # type: ignore
    _genai_types = None  # type: ignore
    _SDK_AVAILABLE = False
    _SDK_IMP_ERR = str(_sdk_err)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------
def _api_mode_active() -> bool:
    """API key var + SDK yüklü → API mode kullan."""
    return bool(GEMINI_API_KEY) and _SDK_AVAILABLE


def get_mode() -> str:
    """Hangi mod aktif? UI'da göstermek için."""
    if _api_mode_active():
        return "api"
    return "cli"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------
class GeminiError(Exception):
    """Gemini hata wrapper'ı — hem API hem CLI yolu için."""

    def __init__(self, message: str, *, returncode: int = 0,
                 stdout: str = "", stderr: str = "",
                 status_code: Optional[int] = None):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.status_code = status_code


def _resolve_model(model: Optional[str]) -> str:
    """'flash' → 'gemini-2.5-flash'. Full model name verilirse aynı dön."""
    m = (model or GEMINI_DEFAULT_MODEL).strip()
    return GEMINI_MODEL_ALIAS.get(m, m)


# ---------------------------------------------------------------------------
# API mode (google-genai SDK)
# ---------------------------------------------------------------------------
_CLIENT_CACHE: Optional[object] = None


def _get_genai_client():
    """Lazy-init genai.Client. Singleton — connection pool reuse."""
    global _CLIENT_CACHE
    if _CLIENT_CACHE is None:
        if not _SDK_AVAILABLE:
            raise GeminiError(f"google-genai SDK yok: {_SDK_IMP_ERR}")
        if not GEMINI_API_KEY:
            raise GeminiError("GEMINI_API_KEY env değişkeni boş.")
        _CLIENT_CACHE = _genai.Client(api_key=GEMINI_API_KEY)
    return _CLIENT_CACHE


def _gemini_chat_api(prompt: str, *,
                     model: Optional[str],
                     system_prompt: Optional[str],
                     temperature: float,
                     max_tokens: Optional[int],
                     json_mode: bool,
                     timeout: int) -> tuple[bool, str]:
    """SDK ile API çağrısı. Returns (ok, text_or_error)."""
    try:
        client = _get_genai_client()
    except GeminiError as e:
        return False, str(e)

    # Config build
    cfg_kwargs: dict = {
        "temperature": float(temperature),
    }
    if system_prompt and system_prompt.strip():
        cfg_kwargs["system_instruction"] = system_prompt.strip()
    if max_tokens and max_tokens > 0:
        cfg_kwargs["max_output_tokens"] = int(max_tokens)
    if json_mode:
        cfg_kwargs["response_mime_type"] = "application/json"

    try:
        config = _genai_types.GenerateContentConfig(**cfg_kwargs)
    except Exception as e:
        return False, f"Gemini config oluşturma hatası: {e}"

    try:
        response = client.models.generate_content(  # type: ignore
            model=_resolve_model(model),
            contents=prompt,
            config=config,
        )
    except Exception as e:
        return False, _friendly_api_error(e)

    # response.text → direkt string. Boş ise candidates'a bak (safety filter vs.)
    text = getattr(response, "text", None)
    if text:
        return True, text
    # Bazı durumlarda response.candidates[0].content.parts[0].text yolu lazım
    try:
        cands = getattr(response, "candidates", None) or []
        for c in cands:
            content = getattr(c, "content", None)
            if not content:
                continue
            parts = getattr(content, "parts", None) or []
            for p in parts:
                t = getattr(p, "text", None)
                if t:
                    return True, t
        # Hala boş — finish_reason'u kontrol et
        if cands:
            fr = getattr(cands[0], "finish_reason", "?")
            return False, (
                f"Gemini boş yanıt verdi (finish_reason={fr}). "
                "Safety filter, MAX_TOKENS aşımı veya model kararsızlığı olabilir."
            )
    except Exception:
        pass
    return False, "Gemini beklenmeyen response: text alanı boş."


def _friendly_api_error(exc: Exception) -> str:
    """SDK exception'ı kullanıcı dostu mesaja çevir."""
    msg = str(exc)
    low = msg.lower()
    cls = type(exc).__name__
    # google.genai.errors içeren tipler: APIError, ClientError, ServerError vb.
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in (401, 403) or "permission" in low or "api key" in low:
        return ("Gemini API key invalid veya permission yok. "
                ".env'deki GEMINI_API_KEY'i kontrol et. "
                "Key al: https://aistudio.google.com/apikey")
    if status == 429 or "quota" in low or "rate" in low:
        return f"Gemini rate-limit/quota aşıldı: {msg[:200]}"
    if status == 404 or "not found" in low or "model" in low and "not" in low:
        return f"Gemini model bulunamadı: {msg[:200]}"
    if "deadline" in low or "timeout" in low:
        return f"Gemini timeout: {msg[:200]}"
    if "safety" in low or "blocked" in low:
        return f"Gemini safety filter response'u bloke etti: {msg[:200]}"
    return f"Gemini API hatası ({cls}): {msg[:300]}"


# ---------------------------------------------------------------------------
# CLI mode (fallback — gemini-cli subprocess)
# ---------------------------------------------------------------------------
GEMINI_CLI_TIMEOUT_DEFAULT: int = 300


def _build_env() -> dict[str, str]:
    """CLI subprocess için env. OAuth flag + trusted dir."""
    env = dict(os.environ)
    env["GOOGLE_GENAI_USE_GCA"] = "true"
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    return env


def _ensure_gemini_settings() -> None:
    """CLI için ~/.gemini/settings.json — maxOutputTokens override."""
    try:
        gemini_dir = Path.home() / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        settings_path = gemini_dir / "settings.json"
        settings = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                settings = {}
        if not isinstance(settings, dict):
            settings = {}
        if settings.get("selectedAuthType") != "oauth-personal":
            settings["selectedAuthType"] = "oauth-personal"
        if "modelConfigs" not in settings:
            settings["modelConfigs"] = {}
        overrides = settings.get("modelConfigs", {}).get("overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
        if overrides.get("maxOutputTokens") != 32768:
            overrides["maxOutputTokens"] = 32768
            settings["modelConfigs"]["overrides"] = overrides
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _clean_gemini_context() -> None:
    """CLI önceki conversation context'i kullanıyor — temizle."""
    try:
        gemini_dir = Path.home() / ".gemini"
        if not gemini_dir.exists():
            return
        projects = gemini_dir / "projects.json"
        if projects.exists():
            try:
                projects.unlink()
            except Exception:
                pass
        history = gemini_dir / "history"
        if history.exists() and history.is_dir():
            import shutil
            try:
                shutil.rmtree(history)
            except Exception:
                pass
    except Exception:
        pass


def _run_gemini_cli(prompt: str, *, model: Optional[str], timeout: int) -> dict:
    """CLI subprocess çağırır, JSON parse eder. (Eski path, fallback için.)"""
    _ensure_gemini_settings()
    _clean_gemini_context()

    cmd = [GEMINI_BIN, "-p", "-", "--output-format", "json",
           "--skip-trust"]
    full_model = _resolve_model(model)
    if full_model:
        cmd.extend(["-m", full_model])

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_build_env(),
            start_new_session=True,
        )
    except FileNotFoundError:
        raise GeminiError(
            f"gemini binary yok (GEMINI_BIN_PATH={GEMINI_BIN!r}). "
            f"İki seçenek: (1) API key kullan — `GEMINI_API_KEY=...` env'e ekle. "
            f"(2) gemini-cli kur: `npm install -g @google/gemini-cli`."
        )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            pass
        raise GeminiError(
            f"Gemini CLI timeout ({timeout}sn). Pro model uzun input'larda "
            "yavaş — flash kullan veya API key'e geç.",
            returncode=-9,
        )

    if proc.returncode != 0:
        raise GeminiError(
            f"gemini exit {proc.returncode}: {(stderr or '')[:300]}",
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )

    try:
        return json.loads(stdout or "{}")
    except json.JSONDecodeError as e:
        raise GeminiError(
            f"gemini JSON parse fail: {e}. Stdout: {(stdout or '')[:200]}",
            stdout=stdout or "",
            stderr=stderr or "",
        )


def _gemini_chat_cli(prompt: str, *,
                     model: Optional[str],
                     system_prompt: Optional[str],
                     timeout: int) -> tuple[bool, str]:
    """CLI mode chat. system_prompt prepend (CLI native yok)."""
    full = prompt
    if system_prompt and system_prompt.strip():
        full = f"{system_prompt.strip()}\n\n---\n\n{prompt.strip()}"
    try:
        data = _run_gemini_cli(full, model=model, timeout=timeout)
    except GeminiError as e:
        return False, _friendly_cli_error(str(e), e.stderr or "")
    except Exception as e:
        return False, f"Gemini CLI hatası: {type(e).__name__}: {str(e)[:200]}"
    err = data.get("error")
    if err:
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        return False, _friendly_cli_error(msg, "")
    response = data.get("response", "")
    if not isinstance(response, str):
        return False, f"Gemini CLI beklenmeyen response: {type(response).__name__}"
    return True, response


def _friendly_cli_error(msg: str, stderr: str) -> str:
    full = (msg + " " + stderr).lower()
    if "auth method" in full:
        return ("Gemini CLI auth method yok. "
                "API key kullanmak için GEMINI_API_KEY env'e ekle.")
    if "401" in full or "unauthorized" in full or "invalid_grant" in full:
        return ("Gemini CLI OAuth token expired. Lokalde `gemini` ile login + "
                "~/.gemini/ rsync. Veya GEMINI_API_KEY env ile API mode'a geç.")
    if "rate" in full and ("limit" in full or "quota" in full):
        return f"Gemini CLI rate-limit/quota: {msg[:200]}"
    if "modelnotfound" in full.replace(" ", "") or "model not found" in full:
        return f"Gemini model bulunamadı: {msg[:200]}"
    if "timeout" in full:
        return f"Gemini CLI timeout: {msg[:200]}"
    return f"Gemini CLI hatası: {msg[:300]}"


# ---------------------------------------------------------------------------
# Public API — gemini_chat (dispatch by mode)
# ---------------------------------------------------------------------------
def gemini_chat(prompt: str, *,
                model: Optional[str] = None,
                system_prompt: Optional[str] = None,
                temperature: float = 0.7,
                max_tokens: Optional[int] = None,
                json_mode: bool = False,
                timeout: int = GEMINI_TIMEOUT_DEFAULT,
                ) -> tuple[bool, str]:
    """Gemini'ye prompt gönder, (ok, text_or_error_message) döndür.

    Mode otomatik seçilir:
      - GEMINI_API_KEY set + google-genai yüklü → API (önerilen)
      - Aksi → gemini-cli subprocess (fallback, daha yavaş)

    Parameters:
      - prompt: User prompt (zorunlu).
      - model: 'flash' | 'pro' | 'flash-lite' alias veya full model name.
      - system_prompt: System instruction (API'de native, CLI'de prepend).
      - temperature: API'de gerçekten kullanılır; CLI'de ignored.
      - max_tokens: API'de gerçekten kullanılır; CLI'de settings.json'la yönetilir.
      - json_mode: API'de response_mime_type=application/json; CLI'de no-op.
      - timeout: API için 120sn default, CLI için 300sn default.
    """
    if not (prompt or "").strip():
        return False, "prompt boş olamaz."

    if _api_mode_active():
        return _gemini_chat_api(
            prompt,
            model=model,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            timeout=timeout,
        )
    # CLI fallback — timeout'u uzat
    return _gemini_chat_cli(
        prompt,
        model=model,
        system_prompt=system_prompt,
        timeout=max(timeout, GEMINI_CLI_TIMEOUT_DEFAULT),
    )


# ---------------------------------------------------------------------------
# Smoke test (admin diagnostic)
# ---------------------------------------------------------------------------
def gemini_smoke_test() -> tuple[bool, str]:
    """Hangi mod aktif + ping başarılı mı? (ok, info_or_error) döner."""
    mode = get_mode()
    if mode == "api":
        if not GEMINI_API_KEY:
            return False, "API mode bekleniyor ama GEMINI_API_KEY boş."
        ok, response = gemini_chat(
            "Reply with exactly: PONG",
            model="flash-lite",
            timeout=20,
        )
        if not ok:
            return False, f"Gemini API smoke FAIL: {response[:200]}"
        return True, f"Gemini API OK — text ping yanıt: {response[:60]!r}"

    # CLI mode smoke
    try:
        proc = subprocess.run(
            [GEMINI_BIN, "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except FileNotFoundError:
        return False, (
            f"Gemini ne API key ne CLI hazır: "
            f"GEMINI_API_KEY boş + gemini binary yok ({GEMINI_BIN!r}). "
            f"En kolay yol: GEMINI_API_KEY=AIza... env'e ekle "
            f"(https://aistudio.google.com/apikey)."
        )
    except Exception as e:
        return False, f"Gemini CLI version check: {type(e).__name__}: {e}"
    if proc.returncode != 0:
        return False, f"gemini --version exit {proc.returncode}: {proc.stderr[:120]}"
    version = (proc.stdout or "").strip().splitlines()[0] if proc.stdout else "?"
    ok, response = gemini_chat(
        "Reply with exactly: PONG",
        model="flash-lite",
        timeout=30,
    )
    if not ok:
        return False, f"Gemini CLI text gen FAIL (v{version}): {response[:200]}"
    return True, f"Gemini CLI OK (v{version}) — text ping yanıt: {response[:60]!r}"


__all__ = [
    "GeminiError",
    "GEMINI_BIN",
    "GEMINI_API_KEY",
    "GEMINI_MODELS",
    "GEMINI_DEFAULT_MODEL",
    "GEMINI_MODEL_ALIAS",
    "gemini_chat",
    "gemini_smoke_test",
    "get_mode",
]
