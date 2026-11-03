"""TTL 快取（diskcache）與 file_id 對應表。

快取 key 只由 wsfunction + 參數組成，**絕不包含 token**。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import diskcache

from .config import get_settings

logger = logging.getLogger(__name__)

# 各類資料的 TTL（秒）；None 表示整個 process 生命週期（由 client 另外記憶體快取）
TTL_SITE_INFO = 24 * 60 * 60
TTL_COURSES = 60 * 60
TTL_CONTENTS = 10 * 60
TTL_ASSIGNMENTS = 10 * 60
TTL_CALENDAR = 5 * 60
TTL_ANNOUNCEMENTS = 5 * 60
TTL_SUBMISSION = 2 * 60
TTL_GRADES = 2 * 60

_file_cache: diskcache.Cache | None = None
_api_cache: diskcache.Cache | None = None


class TTLCache:
    """diskcache 的薄包裝；值可以是任意可 pickle 的 Python 物件。"""

    def __init__(self, directory: str | Path) -> None:
        Path(directory).mkdir(parents=True, exist_ok=True)
        self._cache = diskcache.Cache(str(directory))

    def get(self, key: str) -> Any | None:
        try:
            return self._cache.get(key, default=None)
        except Exception:  # 損壞的資料視同快取未命中
            logger.debug("快取讀取失敗：%s", key, exc_info=True)
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            # expire=None -> 永不過期；ttl=None 時給預設
            self._cache.set(key, value, expire=ttl)
        except Exception:
            logger.debug("快取寫入失敗：%s", key, exc_info=True)

    def delete(self, key: str) -> None:
        try:
            self._cache.delete(key)
        except Exception:
            pass

    def clear(self) -> int:
        try:
            return int(self._cache.clear())
        except Exception:
            return 0

    def close(self) -> None:
        try:
            self._cache.close()
        except Exception:
            pass


def make_key(function: str, params: dict[str, Any]) -> str:
    """快取 key = wsfunction + 排序後的參數（不含 token）。"""
    payload = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"api:{function}:{digest}"


def get_cache() -> TTLCache:
    global _api_cache
    if _api_cache is None:
        _api_cache = TTLCache(get_settings().cache_dir / "api")
    return _api_cache


def get_file_store() -> TTLCache:
    """file_id -> {url, filename, ...} 的持久對應表（與 API 快取分開，清 API 快取不影響）。"""
    global _file_cache
    if _file_cache is None:
        _file_cache = TTLCache(get_settings().cache_dir / "files")
    return _file_cache


def reset_cache(directory: str | Path | None = None) -> None:
    """測試用：重建快取（可指定暫存目錄）。"""
    global _api_cache, _file_cache
    for cache in (_api_cache, _file_cache):
        if cache is not None:
            cache.close()
    root = Path(directory) if directory is not None else get_settings().cache_dir
    _api_cache = TTLCache(root / "api")
    _file_cache = TTLCache(root / "files")


def clear_all_caches() -> None:
    get_cache().clear()
    get_file_store().clear()


# ---------------------------------------------------------------------------
# file_id 對應表
# ---------------------------------------------------------------------------

FILE_KEY_PREFIX = "file:"


def strip_url_token(url: str) -> str:
    """移除 URL 上的 token / wstoken 參數（避免把 token 存進快取、顯示給 LLM）。"""
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"token", "wstoken"}
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def register_file_id(
    *,
    file_id: str,
    url: str,
    filename: str,
    filesize: int | None = None,
    mimetype: str | None = None,
    timemodified: int | None = None,
) -> None:
    """登記 file_id → URL（只存在本機快取，不給 LLM 任意 URL）。"""
    get_file_store().set(
        FILE_KEY_PREFIX + file_id,
        {
            "file_id": file_id,
            "url": strip_url_token(url),
            "filename": filename,
            "filesize": int(filesize or 0),
            "mimetype": mimetype or "",
            "timemodified": int(timemodified or 0),
        },
        ttl=None,
    )


def lookup_file(file_id: str) -> dict[str, Any] | None:
    if not file_id or not isinstance(file_id, str):
        return None
    return get_file_store().get(FILE_KEY_PREFIX + file_id.strip())


def register_assignment(assign_id: int, data: dict[str, Any]) -> None:
    """作業索引：assign_id -> {assignment, course}，供 get_assignment 使用。"""
    get_file_store().set(f"assign:{int(assign_id)}", data, ttl=TTL_ASSIGNMENTS)


def lookup_assignment(assign_id: int) -> dict[str, Any] | None:
    return get_file_store().get(f"assign:{int(assign_id)}")
