"""
NotebookLM Cinematic Studio — Desktop Launcher
==============================================
Streamlit'i headless başlatır, pywebview ile native pencere açar.
Kullanıcı browser tabı görmez, "uygulama" hissi alır.

Çalıştırmak için:
    python desktop.py

(.app bundle launcher'ı bu dosyayı çağırır.)
"""
from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import webview  # pywebview

ROOT = Path(__file__).parent.resolve()
APP_PY = ROOT / "app.py"
APP_TITLE = "NotebookLM Cinematic Studio"


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout_sec: int = 90) -> bool:
    """Streamlit hazır olana kadar bekle."""
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            with urlopen(url, timeout=1) as r:
                if r.status < 500:
                    return True
        except URLError:
            pass
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _start_streamlit(port: int) -> subprocess.Popen:
    """Streamlit'i headless mode'da başlat — browser açma, telemetry kapalı."""
    env = os.environ.copy()
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    env["STREAMLIT_SERVER_HEADLESS"] = "true"

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_PY),
        "--server.port",
        str(port),
        "--server.address",
        "127.0.0.1",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--server.runOnSave",
        "false",
        "--client.toolbarMode",
        "minimal",
    ]
    # Process group ki child öldürünce streamlit de ölsün
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        # Tüm process group'a SIGTERM
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def main() -> None:
    if not APP_PY.exists():
        print(f"app.py bulunamadı: {APP_PY}", file=sys.stderr)
        sys.exit(1)

    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    print(f"Streamlit başlatılıyor: {url}")
    proc = _start_streamlit(port)
    atexit.register(_terminate, proc)

    if not _wait_for_server(url, timeout_sec=90):
        print("Streamlit 90 saniyede yanıt vermedi, kapanılıyor.", file=sys.stderr)
        _terminate(proc)
        sys.exit(2)

    # pywebview window
    webview.create_window(
        APP_TITLE,
        url,
        width=1280,
        height=900,
        min_size=(900, 650),
        text_select=True,
    )
    try:
        webview.start(debug=False)
    finally:
        _terminate(proc)


if __name__ == "__main__":
    main()
