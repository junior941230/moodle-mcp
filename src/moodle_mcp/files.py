"""檔案下載與文字抽取（PDF / DOCX / PPTX / 純文字 / HTML）。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .cache import get_file_store, lookup_file, register_file_id
from .config import get_settings
from .errors import (
    FileTooLarge,
    MoodleError,
    NotFoundError,
    PermissionError_,
    UnsupportedFileType,
)

logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_CHARS = 500_000  # 單一檔案抽出的文字上限，避免吃爆記憶體

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".log",
    ".ini",
    ".cfg",
    ".toml",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".sh",
    ".bash",
    ".ps1",
    ".sql",
    ".r",
    ".m",
    ".tex",
}

_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
}

_UNSAFE_SUFFIX = re.compile(r"[^A-Za-z0-9.]")


def make_file_id(url: str) -> str:
    """由原始 URL 產生短 ID（sha1 前 12 碼）。"""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def register_attachment(attachment: dict[str, Any]) -> str | None:
    """把 Moodle 附件登記成 file_id；回傳 file_id。"""
    url = attachment.get("fileurl") or ""
    if not url:
        return None
    filename = str(attachment.get("filename") or "未命名檔案")
    file_id = make_file_id(url)
    register_file_id(
        file_id=file_id,
        url=url,
        filename=filename,
        filesize=attachment.get("filesize"),
        mimetype=attachment.get("mimetype"),
        timemodified=attachment.get("timemodified"),
    )
    return file_id


def safe_extension(filename: str, mimetype: str = "") -> str:
    """只取安全副檔名；不用伺服器給的完整檔名組路徑。"""
    suffix = Path(filename or "").suffix.lower()
    suffix = _UNSAFE_SUFFIX.sub("", suffix)
    if suffix and len(suffix) <= 12:
        return suffix
    return _MIME_EXTENSIONS.get((mimetype or "").lower(), ".bin")


def normalize_download_url(url: str) -> str:
    """把 /pluginfile.php/ 改寫成 /webservice/pluginfile.php/ 並驗證同站。"""
    parsed = urlparse(url)
    settings = get_settings()
    if parsed.scheme not in {"http", "https"}:
        raise MoodleError("檔案 URL 格式不正確")
    if parsed.netloc.lower() != settings.host:
        raise PermissionError_(
            "基於安全考量，只允許下載 MOODLE_URL 同站的檔案"
        )
    path = parsed.path
    if path.startswith("/pluginfile.php/"):
        path = "/webservice" + path
    elif not path.startswith("/webservice/pluginfile.php/"):
        raise MoodleError("檔案 URL 不是 Moodle 的 pluginfile 路徑")
    normalized = parsed._replace(path=path, fragment="").geturl()
    separator = "&" if parsed.query else "?"
    token = settings.require_token()
    return f"{normalized}{separator}token={token}"


def _looks_extractable(path: Path | str, mimetype: str) -> bool:
    suffix = Path(path).suffix.lower()
    mime = (mimetype or "").lower()
    if suffix in {".pdf", ".docx", ".pptx", ".html", ".htm"}:
        return True
    if suffix in TEXT_EXTENSIONS:
        return True
    return mime in _MIME_EXTENSIONS or mime.startswith("text/")


async def download_file(file_id: str, url: str, filename: str) -> Path:
    """下載到快取目錄，檔名只用 file_id + 安全副檔名。"""
    settings = get_settings()
    target = settings.downloads_dir / f"{file_id}{safe_extension(filename)}"
    download_url = normalize_download_url(url)
    limit = settings.max_file_bytes
    temp = target.with_suffix(target.suffix + ".part")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0),
            follow_redirects=True,
            verify=settings.ssl_verify,
        ) as http, http.stream("GET", download_url) as response:
            if response.status_code >= 400:
                raise MoodleError(
                    f"下載檔案失敗（HTTP {response.status_code}）"
                )
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise FileTooLarge(
                    f"檔案大小 {int(declared) / 1024 / 1024:.1f} MB "
                    f"超過上限 {settings.moodle_max_file_mb:g} MB"
                )
            written = 0
            # 本機快取寫入，單次 chunk 很小；不需要為此再開 thread
            with open(temp, "wb") as handle:  # noqa: ASYNC230
                async for chunk in response.aiter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise FileTooLarge(
                            f"檔案超過上限 {settings.moodle_max_file_mb:g} MB，已中止下載"
                        )
                    handle.write(chunk)
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return target


# ---------------------------------------------------------------------------
# 文字抽取
# ---------------------------------------------------------------------------


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise UnsupportedFileType("PDF 有密碼保護，無法抽取文字") from None
    parts: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        parts.append(f"--- 第 {index} 頁 ---\n{text.strip() or '（此頁沒有可抽取的文字）'}")
    return "\n\n".join(parts)


def _extract_docx(path: Path) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    parts: list[str] = []

    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, document).text.strip()
            if text:
                parts.append(text)
        elif tag == "tbl":
            table = Table(child, document)
            rows: list[str] = []
            for row in table.rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                if any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                parts.append("\n".join(rows))

    return "\n\n".join(parts)


def _extract_pptx(path: Path) -> str:
    from pptx import Presentation

    presentation = Presentation(str(path))
    parts: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        lines: list[str] = [f"--- 投影片 {index} ---"]
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
                if text:
                    lines.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        lines.append("| " + " | ".join(cells) + " |")
        notes = ""
        try:
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
        except Exception:
            notes = ""
        if notes:
            lines.append(f"（備忘稿）{notes}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "big5", "cp950"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_html(path: Path) -> str:
    from .formatting import html_to_markdown

    return html_to_markdown(_read_text_file(path))


def extract_text(path: Path, filename: str, mimetype: str = "") -> str:
    """依副檔名 / MIME 抽取文字；同步函式（在 thread 執行）。"""
    suffix = Path(filename or path.name).suffix.lower()
    mime = (mimetype or "").lower()

    if suffix == ".pdf" or mime == "application/pdf":
        text = _extract_pdf(path)
    elif suffix == ".docx" or "wordprocessingml" in mime:
        text = _extract_docx(path)
    elif suffix == ".pptx" or "presentationml" in mime:
        text = _extract_pptx(path)
    elif suffix in {".html", ".htm"} or mime in {"text/html", "application/xhtml+xml"}:
        text = _extract_html(path)
    elif suffix in TEXT_EXTENSIONS or mime.startswith("text/"):
        text = _read_text_file(path)
    else:
        raise UnsupportedFileType(
            f"檔案類型（{mimetype or suffix or '未知'}）無法轉成文字"
        )

    text = text.strip()
    if len(text) > _MAX_DOWNLOAD_CHARS:
        text = text[:_MAX_DOWNLOAD_CHARS] + "\n\n…（檔案過長，僅保留前 50 萬字）"
    return text


async def get_file_text(file_id: str) -> tuple[dict[str, Any], str]:
    """回傳 (檔案資訊, 全文)；文字依 file_id + timemodified 快取。"""
    file_id = str(file_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{12}", file_id):
        raise NotFoundError(
            "file_id 格式不正確；請使用工具輸出的 12 碼 file_id（不要傳 URL）"
        )
    meta = lookup_file(file_id)
    if not meta:
        raise NotFoundError(
            "file_id 不存在或已失效，請先用課程內容 / 作業 / 討論工具取得新的 file_id"
        )

    filename = str(meta.get("filename") or "未命名檔案")
    mimetype = str(meta.get("mimetype") or "")
    timemodified = int(meta.get("timemodified") or 0)
    settings = get_settings()

    if int(meta.get("filesize") or 0) > settings.max_file_bytes:
        raise FileTooLarge(
            f"檔案大小 {int(meta.get('filesize') or 0) / 1024 / 1024:.1f} MB "
            f"超過上限 {settings.moodle_max_file_mb:g} MB，拒絕下載"
        )

    store = get_file_store()
    text_key = f"text:{file_id}:{timemodified}"
    cached = store.get(text_key)
    if cached is not None:
        return meta, str(cached)

    if not _looks_extractable(filename, mimetype):
        raise UnsupportedFileType(
            f"「{filename}」（{mimetype or '未知類型'}）無法轉成文字。"
            "這是影片、壓縮檔、圖片或投影片以外的二進位檔。"
        )

    path = await download_file(file_id, str(meta.get("url") or ""), filename)
    text = await asyncio.to_thread(extract_text, path, filename, mimetype)
    store.set(text_key, text, ttl=None)
    return meta, text
