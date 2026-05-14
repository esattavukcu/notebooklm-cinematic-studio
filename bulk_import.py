"""
bulk_import.py — Drive klasöründen toplu script import.

Public-share edilmiş Drive klasöründeki .docx dosyalarını gdown ile indirir,
python-docx ile düz metin çıkarır ve her birini Job olarak queue'ya ekler.

Kullanım (app.py'dan):
    from bulk_import import bulk_import_from_drive
    result = bulk_import_from_drive(
        drive_url_or_id="https://drive.google.com/drive/folders/XXXX",
        custom_prompt_template="Cinematic educational ...",
        submitted_by="Mehmet",
        on_progress=lambda msg: print(msg),
    )
    # result: {"created": [job_id, ...], "skipped": [...], "errors": [...]}

Drive klasörü 'Anyone with the link' olmalı — gdown API key'siz erişir.
Private klasör için OAuth Drive scope gerekir (out of scope, future iter).
"""

from __future__ import annotations

import io
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

try:
    import gdown  # type: ignore
    _GDOWN_AVAILABLE = True
except ImportError as _gd_err:
    gdown = None  # type: ignore
    _GDOWN_AVAILABLE = False
    _GDOWN_IMP_ERR = str(_gd_err)

try:
    from docx import Document as _DocxDocument  # python-docx
    _DOCX_AVAILABLE = True
except ImportError as _doc_err:
    _DocxDocument = None  # type: ignore
    _DOCX_AVAILABLE = False
    _DOCX_IMP_ERR = str(_doc_err)


# ---------------------------------------------------------------------------
# Drive URL/ID parsing
# ---------------------------------------------------------------------------
_DRIVE_FOLDER_PATTERNS = [
    re.compile(r"drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/folderview\?id=([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/.*[?&]id=([a-zA-Z0-9_-]+)"),
    re.compile(r"^([a-zA-Z0-9_-]{20,})$"),  # plain folder ID
]


def extract_folder_id(url_or_id: str) -> Optional[str]:
    """Drive klasör URL'i veya ID'sinden plain folder ID çıkar."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    for pat in _DRIVE_FOLDER_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return None


def is_available() -> tuple[bool, str]:
    """gdown + python-docx yüklü mü?"""
    msgs = []
    if not _GDOWN_AVAILABLE:
        msgs.append(f"gdown yok: {_GDOWN_IMP_ERR}")
    if not _DOCX_AVAILABLE:
        msgs.append(f"python-docx yok: {_DOCX_IMP_ERR}")
    if msgs:
        return False, " · ".join(msgs)
    try:
        import gdown as _g  # noqa
        import docx as _d  # noqa
        return True, f"gdown v{_g.__version__} · python-docx v{_d.__version__}"
    except Exception:
        return True, "available"


# ---------------------------------------------------------------------------
# Docx parsing
# ---------------------------------------------------------------------------
def parse_docx(path: Path) -> str:
    """docx → düz metin. Paragraf ayraçları korunur, boş satırlar bir kez."""
    if not _DOCX_AVAILABLE:
        raise RuntimeError(f"python-docx yüklü değil: {_DOCX_IMP_ERR}")
    doc = _DocxDocument(str(path))
    parts: list[str] = []
    for para in doc.paragraphs:
        t = (para.text or "").strip()
        parts.append(t)
    # Çoklu boş satırları teke indir
    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def docx_to_title(filename: str, fallback_text: str = "") -> str:
    """Filename'den anlamlı title üret. '01_passion_fruit.docx' → 'Passion Fruit'."""
    stem = Path(filename).stem
    # Leading sayı + underscore/dash temizle ('01_', '01-')
    stem = re.sub(r"^\d+[_\-\.\s]+", "", stem)
    # underscore/dash → boşluk
    stem = stem.replace("_", " ").replace("-", " ").strip()
    if stem:
        return stem[:80].title()
    # Fallback: text'in ilk satırı
    if fallback_text:
        first_line = fallback_text.strip().split("\n", 1)[0][:80].strip()
        if first_line:
            return first_line
    return "Untitled"


# ---------------------------------------------------------------------------
# Drive download
# ---------------------------------------------------------------------------
def download_drive_folder(folder_id_or_url: str,
                          out_dir: Path,
                          quiet: bool = True) -> list[Path]:
    """Public Drive klasöründeki tüm dosyaları out_dir'a indir, path listesi döner.

    gdown.download_folder içeriği parse eder ve teker teker indirir. Sadece
    direkt linkli dosyalar inilir; nested folder'lar yok sayılır.
    """
    if not _GDOWN_AVAILABLE:
        raise RuntimeError(f"gdown yüklü değil: {_GDOWN_IMP_ERR}")
    folder_id = extract_folder_id(folder_id_or_url)
    if not folder_id:
        raise ValueError(
            "Geçerli Drive klasör URL'i/ID'si değil. "
            "Örnek: https://drive.google.com/drive/folders/ABCdef123..."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    # gdown 6.x: download_folder(url, output, quiet, use_cookies, ...) — no
    # remaining_ok kwarg, eski 4.x'te 50-file limit'i için gerekiyordu, 6.x'te
    # default davranış değişti. use_cookies=False = public klasör için yeterli.
    try:
        downloaded = gdown.download_folder(
            url=url,
            output=str(out_dir),
            quiet=quiet,
            use_cookies=False,
        )
    except Exception as e:
        raise RuntimeError(
            f"Drive klasör indirme hatası: {type(e).__name__}: {e}. "
            f"Klasör 'Anyone with the link' mi? ID/URL doğru mu?"
        )
    if downloaded:
        # gdown bazı versiyonlarda string list, bazılarında None döner
        try:
            return [Path(p) for p in downloaded if Path(p).exists()]
        except Exception:
            pass
    # Fallback: out_dir'daki tüm dosyaları topla
    return sorted([p for p in out_dir.rglob("*") if p.is_file()])


def list_drive_folder_docx(folder_id_or_url: str,
                            tmp_dir: Optional[Path] = None) -> list[Path]:
    """Klasördeki sadece .docx dosyalarının lokal path'lerini döner.

    Önizleme + actual import aynı download'u kullanır — gdown idempotent değil,
    o yüzden tmp_dir hem önizleme hem import için aynı kullanılmalı (caller
    yönetir). None ise her seferinde yeni tmp_dir açar.
    """
    if tmp_dir is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="bulk_drive_"))
    files = download_drive_folder(folder_id_or_url, tmp_dir)
    return [p for p in files if p.suffix.lower() == ".docx"]


# ---------------------------------------------------------------------------
# Bulk job creation
# ---------------------------------------------------------------------------
def bulk_create_jobs_from_docx_paths(
    docx_paths: list[Path],
    *,
    custom_prompt_template: str = "",
    submitted_by: str = "bulk_import",
    job_factory: Callable[..., dict],  # caller'ın Job dataclass'ı için
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Her docx için bir Job dict oluşturur. job_factory(title, text, custom_prompt,
    submitted_by) → dict döner (caller dataclasse maple). save işini caller yapar.

    Returns: {"created": [...job_dicts...], "errors": [(filename, err), ...]}
    """
    if not _DOCX_AVAILABLE:
        raise RuntimeError(f"python-docx yüklü değil: {_DOCX_IMP_ERR}")

    created: list[dict] = []
    errors: list[tuple[str, str]] = []

    for i, p in enumerate(docx_paths):
        if on_progress:
            on_progress(f"[{i+1}/{len(docx_paths)}] {p.name} işleniyor…")
        try:
            text = parse_docx(p)
            if not text or len(text.strip()) < 50:
                errors.append((p.name, f"Çok kısa veya boş ({len(text)} chars)"))
                continue
            title = docx_to_title(p.name, text)
            job = job_factory(
                title=title,
                text=text,
                custom_prompt=custom_prompt_template,
                submitted_by=submitted_by,
            )
            created.append(job)
        except Exception as e:
            errors.append((p.name, f"{type(e).__name__}: {str(e)[:200]}"))

    if on_progress:
        on_progress(
            f"Bitti: {len(created)} job oluşturuldu, {len(errors)} hatalı."
        )
    return {"created": created, "errors": errors}


# ---------------------------------------------------------------------------
# Combined: end-to-end (caller için tek-shot helper)
# ---------------------------------------------------------------------------
def bulk_import_from_drive(
    drive_url_or_id: str,
    custom_prompt_template: str,
    submitted_by: str,
    job_factory: Callable[..., dict],
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    keep_downloads: bool = False,
) -> dict:
    """Tek çağrıda Drive klasör → docx download → Job dicts.

    Returns: {
        "created": [job_dicts],
        "errors": [(filename, err), ...],
        "downloads_dir": str (keep_downloads=True ise),
        "total_files": int (klasördeki toplam docx sayısı),
    }
    """
    ok, msg = is_available()
    if not ok:
        raise RuntimeError(msg)

    out_dir = Path(tempfile.mkdtemp(prefix="bulk_drive_"))
    if on_progress:
        on_progress(f"Drive klasörü indiriliyor → {out_dir}")
    try:
        docx_paths = list_drive_folder_docx(drive_url_or_id, out_dir)
        if on_progress:
            on_progress(f"{len(docx_paths)} adet .docx bulundu, işleniyor…")
        if not docx_paths:
            return {
                "created": [],
                "errors": [(drive_url_or_id, "Klasörde .docx dosyası yok veya erişim yok.")],
                "total_files": 0,
            }
        result = bulk_create_jobs_from_docx_paths(
            docx_paths,
            custom_prompt_template=custom_prompt_template,
            submitted_by=submitted_by,
            job_factory=job_factory,
            on_progress=on_progress,
        )
        result["total_files"] = len(docx_paths)
        if keep_downloads:
            result["downloads_dir"] = str(out_dir)
        else:
            try:
                shutil.rmtree(out_dir)
            except OSError:
                pass
        return result
    except Exception:
        if not keep_downloads:
            try:
                shutil.rmtree(out_dir, ignore_errors=True)
            except OSError:
                pass
        raise


__all__ = [
    "extract_folder_id",
    "is_available",
    "parse_docx",
    "docx_to_title",
    "download_drive_folder",
    "list_drive_folder_docx",
    "bulk_create_jobs_from_docx_paths",
    "bulk_import_from_drive",
]
