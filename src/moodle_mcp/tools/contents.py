"""課程內容搜尋（不打新 API，直接用快取）。"""

from __future__ import annotations

from typing import Any

from ..cache import TTL_CONTENTS
from ..client import get_client
from ..errors import tool_guard
from ..formatting import html_to_plain, truncate_output
from ._common import parse_int


@tool_guard
async def search_course(course_id: int, query: str) -> str:
    """在單一課程的章節名稱、模組名稱與描述中搜尋關鍵字（不分大小寫）。

    Args:
        course_id: 課程 id。
        query: 要找的字串，例如「期中考」「講義」。

    Returns:
        命中的章節與模組清單；若先前已載入課程內容，直接使用快取、不重打 API。
        找到檔案後可用 read_file(file_id) 讀取內容。
    """
    keyword = (query or "").strip().lower()
    if not keyword:
        return "⚠️ 請提供要搜尋的關鍵字（query）。"

    client = get_client()
    params = {"courseid": int(course_id)}
    data = client.peek("core_course_get_contents", **params)
    if data is None:
        data = await client.call(
            "core_course_get_contents", ttl=TTL_CONTENTS, **params
        )
    sections = data if isinstance(data, list) else []
    if not sections:
        return "（這門課沒有可搜尋的內容）"

    hits: list[str] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        number = parse_int(section.get("section"), index)
        section_name = html_to_plain(section.get("name") or "").strip() or f"章節 {number}"
        section_summary = html_to_plain(section.get("summary") or "")

        if keyword in section_name.lower() or keyword in section_summary.lower():
            snippet = _snippet(section_summary, keyword)
            hits.append(f"- **章節 {number}：{section_name}**（章節描述命中）{snippet}")

        for module in section.get("modules") or []:
            if not isinstance(module, dict):
                continue
            modname = str(module.get("modname") or "")
            name = str(module.get("name") or "")
            description = html_to_plain(module.get("description") or "")
            haystack = f"{modname} {name} {description}".lower()
            if keyword in haystack:
                snippet = _snippet(description, keyword) or _snippet(name, keyword)
                hits.append(
                    f"- [{modname}] **{name}**（章節 {number}：{section_name}，"
                    f"cmid={module.get('cmid') or module.get('id')}）{snippet}"
                )

    if not hits:
        return f"（在 course_id={course_id} 中找不到「{query}」）"
    return truncate_output(
        f"# 「{query}」搜尋結果（course_id={course_id}）\n\n"
        + "\n".join(hits[:100])
        + f"\n\n共 {len(hits)} 筆命中。",
        12000,
    )


def _snippet(text: str, keyword: str, radius: int = 60) -> str:
    if not text:
        return ""
    lower = text.lower()
    position = lower.find(keyword)
    if position < 0:
        return ""
    start = max(0, position - radius)
    end = min(len(text), position + len(keyword) + radius)
    piece = text[start:end].replace("\n", " ").strip()
    return f" …{piece}…" if piece else ""


def register(mcp: Any) -> None:
    mcp.add_tool(search_course)


__all__ = ["search_course", "register"]
