"""HTML → markdown、時間格式化、表格與截斷工具。"""

from __future__ import annotations

import html as html_lib
import re
import time
from datetime import datetime
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from markdownify import markdownify as _markdownify

from .config import get_settings

DEFAULT_MAX_CHARS = 12000
_WEEKDAYS = "一二三四五六日"

_BLANK_LINES = re.compile(r"\n{3,}")
_IMG_TAG = re.compile(r"(?is)<img\b[^>]*>")
_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>")
_BR = re.compile(r"(?i)<br\s*/?>")
_BLOCK_END = re.compile(r"(?i)</(p|div|li|tr|h[1-6]|section|article)>")
_TAG = re.compile(r"(?s)<[^>]+>")


def html_to_markdown(value: str | None) -> str:
    """Moodle 的 description / summary / intro 都是 HTML，轉成好讀的 markdown。"""
    if not value:
        return ""
    cleaned = _SCRIPT_STYLE.sub("", str(value))
    cleaned = _IMG_TAG.sub("", cleaned)
    try:
        text = _markdownify(
            cleaned,
            heading_style="ATX",
            bullets="-",
            strip=["img", "script", "style"],
        )
    except Exception:  # 極端 HTML 保底
        text = html_to_plain(cleaned)
    lines = [line.rstrip() for line in text.splitlines()]
    text = "\n".join(lines).strip()
    return _BLANK_LINES.sub("\n\n", text)


def html_to_plain(value: str | None, limit: int | None = None) -> str:
    """HTML 轉純文字（用於摘要），不保留 markdown 符號。"""
    if not value:
        return ""
    cleaned = _SCRIPT_STYLE.sub("", str(value))
    cleaned = _BR.sub("\n", cleaned)
    cleaned = _BLOCK_END.sub("\n", cleaned)
    cleaned = _TAG.sub(" ", cleaned)
    text = html_lib.unescape(cleaned)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    text = _BLANK_LINES.sub("\n\n", text)
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().moodle_tz or "Asia/Taipei")
    except Exception:
        return ZoneInfo("UTC")


def to_local(timestamp: int | float | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=_tz())
    except (OverflowError, OSError, ValueError):
        return None


def relative_time(timestamp: int | float | None, *, now: float | None = None) -> str:
    """回傳「3 天後」「2 小時前」這類相對時間。"""
    if not timestamp:
        return ""
    now = time.time() if now is None else now
    delta = float(timestamp) - now
    future = delta >= 0
    seconds = abs(delta)

    if seconds < 60:
        return "現在" if seconds < 30 else ("1 分鐘後" if future else "1 分鐘前")
    if seconds < 3600:
        value = int(seconds // 60)
        unit = "分鐘"
    elif seconds < 86400:
        value = int(seconds // 3600)
        unit = "小時"
    elif seconds < 86400 * 30:
        value = int(seconds // 86400)
        unit = "天"
    elif seconds < 86400 * 365:
        value = int(seconds // (86400 * 30))
        unit = "個月"
    else:
        value = int(seconds // (86400 * 365))
        unit = "年"
    return f"{value} {unit}{'後' if future else '前'}"


def format_ts(
    timestamp: int | float | None,
    *,
    with_relative: bool = True,
    empty: str = "（無期限）",
) -> str:
    """Unix 秒 → `YYYY-MM-DD HH:mm (週X)（3 天後）`。"""
    dt = to_local(timestamp)
    if dt is None:
        return empty
    text = f"{dt:%Y-%m-%d %H:%M} (週{_WEEKDAYS[dt.weekday()]})"
    if with_relative:
        rel = relative_time(timestamp)
        if rel:
            text += f"（{rel}）"
    return text


def format_date(timestamp: int | float | None, empty: str = "—") -> str:
    dt = to_local(timestamp)
    return f"{dt:%Y-%m-%d}" if dt else empty


def human_size(size: int | float | None) -> str:
    if not size:
        return "大小未知"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def md_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """產生 markdown 表格。"""
    header = "| " + " | ".join(_cell(h) for h in headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def truncate_output(
    text: str, max_chars: int = DEFAULT_MAX_CHARS, *, hint_offset: int | None = None
) -> str:
    """超過 max_chars 時截斷，並附上後續讀取提示。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars].rstrip()
    if hint_offset is not None:
        note = f"…（已截斷，用 offset={hint_offset} 繼續）"
    else:
        note = f"…（已截斷，共 {len(text)} 字）"
    return f"{cut}\n\n{note}"


def paginate_text(text: str, offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """依 offset / max_chars 分頁，並標示總字數。"""
    total = len(text)
    start = max(0, min(int(offset or 0), total))
    length = max(1, int(max_chars or DEFAULT_MAX_CHARS))
    if total and int(offset or 0) >= total:
        return f"（offset={int(offset)} 已超過總字數 {total}）"
    end = min(start + length, total)
    body = text[start:end]
    end_display = max(start, end - 1) if body else start
    if start == 0 and end >= total:
        return f"{body}\n\n（共 {total} 字）"
    parts = [body, f"（第 {start + 1}–{end_display} 字，共 {total} 字"]
    if end < total:
        parts.append(f"；用 offset={end} 繼續）")
    else:
        parts.append("）")
    return "".join(parts)
