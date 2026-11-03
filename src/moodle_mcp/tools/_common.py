"""tools 共用小工具：課程索引、附件顯示。"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from ..cache import TTL_COURSES
from ..client import get_client
from ..config import get_settings
from ..errors import FunctionUnavailable, redact
from ..files import register_attachment
from ..formatting import human_size


async def fetch_all_courses(*, ttl: int = TTL_COURSES) -> list[dict[str, Any]]:
    """所有已選課程；timeline API 不可用時退回 core_enrol_get_users_courses。"""
    client = get_client()
    try:
        data = await client.call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification="all",
            ttl=ttl,
        )
    except FunctionUnavailable:
        info = await client.site_info()
        data = await client.call(
            "core_enrol_get_users_courses",
            userid=info.get("userid"),
            ttl=ttl,
        )
    if isinstance(data, dict):
        courses = data.get("courses") or []
    elif isinstance(data, list):
        courses = data
    else:
        courses = []
    return [c for c in courses if isinstance(c, dict)]


async def fetch_courses_by_classification(
    classification: str, *, ttl: int = TTL_COURSES
) -> list[dict[str, Any]]:
    """inprogress / past / future / all。"""
    client = get_client()
    try:
        data = await client.call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification=classification,
            ttl=ttl,
        )
        if isinstance(data, dict):
            return [c for c in (data.get("courses") or []) if isinstance(c, dict)]
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict)]
        return []
    except FunctionUnavailable:
        courses = await fetch_all_courses(ttl=ttl)
        if classification == "all":
            return courses
        now = int(time.time())
        selected: list[dict[str, Any]] = []
        for course in courses:
            start = int(course.get("startdate") or 0)
            end = int(course.get("enddate") or 0)
            if classification == "inprogress" and (end == 0 or end >= now) and (
                start == 0 or start <= now
            ):
                selected.append(course)
            elif classification == "past" and end and end < now:
                selected.append(course)
            elif classification == "future" and start and start > now:
                selected.append(course)
        return selected


async def course_name_map() -> dict[int, dict[str, Any]]:
    courses = await fetch_all_courses()
    result: dict[int, dict[str, Any]] = {}
    for course in courses:
        try:
            result[int(course.get("id"))] = course
        except (TypeError, ValueError):
            continue
    return result


def course_label(course: dict[str, Any] | None) -> str:
    if not course:
        return "（未知課程）"
    return str(course.get("shortname") or course.get("fullname") or course.get("id"))


def is_moodle_url(url: str) -> bool:
    try:
        return urlparse(url).netloc.lower() == get_settings().host
    except Exception:
        return False


def _is_pluginfile(url: str) -> bool:
    return "/pluginfile.php/" in url


def course_module_file_line(item: dict[str, Any], *, indent: str = "") -> str:
    """課程內容中的檔案 / 連結 → 一行 markdown；同站檔案只顯示 file_id。"""
    url = str(item.get("fileurl") or "")
    filename = str(item.get("filename") or item.get("title") or "（未命名）")
    size = item.get("filesize")

    if url and _is_pluginfile(url):
        file_id = register_attachment(
            {
                "fileurl": url,
                "filename": filename,
                "filesize": size,
                "mimetype": item.get("mimetype"),
                "timemodified": item.get("timemodified"),
            }
        )
        if file_id:
            return f"{indent}- 📄 {filename}（{human_size(size)}，file_id={file_id}）"

    if url and not is_moodle_url(url):
        return f"{indent}- 🔗 {filename} — {redact(url)}"

    if url and not _is_pluginfile(url):
        # 同站的活動頁面連結（可能帶 token），只顯示名稱不顯示 URL
        return f"{indent}- {filename}"

    return f"{indent}- {filename}"


def attachment_lines(
    attachments: list[dict[str, Any]] | None, *, indent: str = ""
) -> list[str]:
    lines: list[str] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        file_id = register_attachment(attachment)
        name = attachment.get("filename") or "未命名檔案"
        if file_id:
            lines.append(
                f"{indent}- 📎 {name}"
                f"（{human_size(attachment.get('filesize'))}，file_id={file_id}）"
            )
        else:
            lines.append(f"{indent}- 📎 {name}")
    return lines


def safe_get(data: Any, *keys: str, default: Any = None) -> Any:
    """安全地逐層取值（dict / list index）。"""
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError):
                return default
        else:
            return default
        if current is None:
            return default
    return current


def ensure_list(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "attachment_lines",
    "course_label",
    "course_module_file_line",
    "course_name_map",
    "ensure_list",
    "fetch_all_courses",
    "fetch_courses_by_classification",
    "is_moodle_url",
    "parse_int",
    "safe_get",
]
