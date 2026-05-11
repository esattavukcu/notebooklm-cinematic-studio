"""
nlm_client.py — tmc/nlm Go CLI wrapper.

NotebookLM ile etkileşim için Playwright DOM scraping yerine bu modül
kullanılır. Playwright `auth.json` (storage_state) dosyasından cookie
çıkarır, tmc/nlm binary'sini subprocess olarak çalıştırır.

Desteklenen işlemler:
- nlm_create_notebook(title)              → notebook_id
- nlm_source_add(nb_id, path)             → source_id (text, image, video, pdf, audio)
- nlm_create_video(nb_id, custom_prompt)  → Cinematic Video Overview tetikler

VİDEO DOWNLOAD desteklenmez — `nlm video download` CDN auth nedeniyle
manual-fallback only. Harvest hâlâ Playwright cookie-fetch ile yapılır.

Binary path:
- Default: PATH'te `nlm`
- Override: NLM_BIN_PATH env var

Kullanım:
    from nlm_client import (
        extract_nlm_cookies, nlm_create_notebook, nlm_source_add,
        nlm_create_video, notebook_web_url, NlmError,
    )

    cookies = extract_nlm_cookies(Path("chrome_profiles/abc/auth.json"))
    nb_id = nlm_create_notebook("My Video", cookies)
    nlm_source_add(nb_id, Path("script.txt"), cookies)
    nlm_source_add(nb_id, Path("image1.jpg"), cookies)
    nlm_create_video(nb_id, "Role: ... Custom prompt here ...", cookies)
    print(notebook_web_url(nb_id, authuser=0))
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
NLM_BIN: str = os.environ.get("NLM_BIN_PATH", "").strip() or "nlm"
NLM_TIMEOUT_DEFAULT: int = 90  # saniye — çoğu komut için

# nlm CLI Google session cookie'lerini ister. SID/HSID/SSID core auth.
# APISID/SAPISID API çağrıları için. __Secure-* tokenlar 2FA / OAuth flow'da
# bazen kritik — varsa ekliyoruz, yoksa core 5'i yeterli olmalı.
_REQUIRED_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"]
_OPTIONAL_COOKIES = [
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "NID",
]


class NlmError(Exception):
    """nlm subprocess hata wrapper'ı — exit code, stdout, stderr içerir."""

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
# Cookie extraction (Playwright auth.json → nlm --cookies string)
# ---------------------------------------------------------------------------
def extract_nlm_cookies(auth_json_path: Path) -> str:
    """Playwright storage_state JSON'dan nlm-uyumlu cookie string'i üretir.

    Format: 'name1=value1; name2=value2; ...'
    En az SID + HSID + SSID + APISID + SAPISID bulunmalı; yoksa NlmError.
    """
    if not auth_json_path.exists():
        raise NlmError(f"auth.json bulunamadı: {auth_json_path}")
    try:
        with auth_json_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise NlmError(f"auth.json okunamadı: {e}")

    cookies = data.get("cookies") or []
    pairs: dict[str, str] = {}
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        domain = c.get("domain", "")
        # Sadece Google domain'lerindeki cookie'leri al
        if not domain or "google" not in domain:
            continue
        if name in _REQUIRED_COOKIES or name in _OPTIONAL_COOKIES:
            # Aynı isimle birden çok domain'de varsa, sonuncusu kazansın
            pairs[name] = value

    missing = [n for n in _REQUIRED_COOKIES if n not in pairs]
    if missing:
        raise NlmError(
            f"Gerekli cookie eksik: {', '.join(missing)} "
            f"({auth_json_path.name}). Profile re-init gerekebilir."
        )
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------
def _run_nlm(args: list[str], cookies: str, *,
             input_data: Optional[bytes] = None,
             timeout: int = NLM_TIMEOUT_DEFAULT,
             authuser: int = 0) -> tuple[str, str]:
    """nlm CLI çağrı wrapper'ı. (stdout, stderr) döner. Hata varsa NlmError."""
    cmd = [NLM_BIN, "--cookies", cookies]
    if authuser:
        cmd += ["--authuser", str(authuser)]
    cmd += args
    try:
        proc = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise NlmError(
            f"nlm binary bulunamadı (PATH veya NLM_BIN_PATH): {NLM_BIN}. "
            f"Server'a 'go install github.com/tmc/nlm/cmd/nlm@latest' "
            f"yapman gerek."
        )
    except subprocess.TimeoutExpired:
        # args'ın ilk 2'sini logla (sensitive değil)
        cmd_str = " ".join(args[:2])
        raise NlmError(f"nlm timeout ({timeout}s): {cmd_str}")

    stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")

    if proc.returncode != 0:
        msg = stderr.strip()[:300] or stdout.strip()[:300] or "(empty output)"
        raise NlmError(
            f"nlm exit {proc.returncode}: {msg}",
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return stdout, stderr


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------
def nlm_create_notebook(title: str, cookies: str, authuser: int = 0) -> str:
    """Yeni notebook oluştur, notebook_id döner.

    `nlm notebook create <title>` çıktı: tek satır project ID.
    """
    if not (title or "").strip():
        raise NlmError("title boş olamaz")
    stdout, _ = _run_nlm(
        ["notebook", "create", title.strip()],
        cookies, authuser=authuser, timeout=60,
    )
    # Output: tek satır ProjectId (notebook ID)
    nb_id = stdout.strip().split("\n")[-1].strip()
    if not nb_id or len(nb_id) < 8 or " " in nb_id:
        raise NlmError(f"notebook ID parse edilemedi: stdout={stdout!r}")
    return nb_id


def nlm_source_add(nb_id: str, file_path: Path, cookies: str,
                   authuser: int = 0, timeout: int = 300) -> str:
    """Notebook'a dosya source olarak ekle, source_id döner.

    `nlm source add <id> <path>` — text/PDF/image/audio/video kabul.
    Büyük dosyalar için timeout 300s (5 dk).
    """
    if not file_path.exists():
        raise NlmError(f"source dosyası yok: {file_path}")
    stdout, _ = _run_nlm(
        ["source", "add", nb_id, str(file_path.resolve())],
        cookies, authuser=authuser, timeout=timeout,
    )
    # Output: bir veya daha fazla source ID (her satır bir ID)
    src_id = stdout.strip().split("\n")[-1].strip()
    if not src_id:
        raise NlmError(f"source ID parse edilemedi: stdout={stdout!r}")
    return src_id


def nlm_create_video(nb_id: str, custom_prompt: str, cookies: str,
                     authuser: int = 0, timeout: int = 60) -> str:
    """Cinematic Video Overview üretimini tetikle.

    `nlm create-video <id> "<prompt>"` — Cinematic style default.
    Geri dönüş: stdout (artifact ID veya bilgi mesajı), uzun string.
    """
    if not nb_id:
        raise NlmError("notebook_id boş")
    prompt = (custom_prompt or "").strip() or "Generate a cinematic video overview."
    stdout, _ = _run_nlm(
        ["create-video", nb_id, prompt],
        cookies, authuser=authuser, timeout=timeout,
    )
    return stdout.strip()


def notebook_web_url(nb_id: str, authuser: int = 0) -> str:
    """nb_id → https://notebooklm.google.com/notebook/<id>?authuser=N"""
    return f"https://notebooklm.google.com/notebook/{nb_id}?authuser={authuser}"


# ---------------------------------------------------------------------------
# Smoke test (admin için)
# ---------------------------------------------------------------------------
def nlm_smoke_test() -> tuple[bool, str]:
    """nlm binary var mı, çalışıyor mu? (ok, info_or_error) döner."""
    try:
        proc = subprocess.run(
            [NLM_BIN, "--help"],
            capture_output=True, timeout=10, check=False,
        )
        # --help bazen exit code 2 dönebilir (cobra/flag stdlib), ikisi de OK
        if proc.returncode in (0, 2):
            # İlk satırı al
            first_line = (proc.stdout or proc.stderr).decode(
                "utf-8", errors="replace"
            ).strip().split("\n")[0][:120]
            return True, f"nlm OK ({NLM_BIN}): {first_line}"
        return False, (
            f"nlm exit {proc.returncode}: "
            f"{(proc.stderr or b'').decode(errors='replace')[:120]}"
        )
    except FileNotFoundError:
        return False, (
            f"nlm binary bulunamadı (PATH veya NLM_BIN_PATH={NLM_BIN!r}). "
            "Server'a kurmak için: go install github.com/tmc/nlm/cmd/nlm@latest"
        )
    except Exception as e:
        return False, f"nlm smoke test hata: {type(e).__name__}: {e}"


__all__ = [
    "NlmError",
    "NLM_BIN",
    "extract_nlm_cookies",
    "nlm_create_notebook",
    "nlm_source_add",
    "nlm_create_video",
    "notebook_web_url",
    "nlm_smoke_test",
]
