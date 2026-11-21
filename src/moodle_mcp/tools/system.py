"""系統 tools：clear_cache / notifications。"""

from __future__ import annotations

from typing import Any

from ..cache import TTL_ANNOUNCEMENTS, clear_all_caches
from ..client import get_client
from ..errors import tool_guard
from ..files import clear_downloads
from ..formatting import format_ts, html_to_plain, truncate_output


@tool_guard
async def clear_cache() -> str:
    """清除本機的 API 快取、檔案對應表與殘留的下載檔（強制下次重新向 Moodle 取得最新資料）。

    當使用者說「資料是舊的」「重新整理」「清除快取」時使用。
    只動本機快取目錄，不會影響 Moodle 上的任何資料。
    """
    clear_all_caches()
    removed = clear_downloads()
    return (
        "✅ 已清除本機快取"
        + (f"（另刪除 {removed} 個殘留的下載檔）" if removed else "")
        + "。\n\n"
        "file_id 對應表已清空，請重新用課程 / 作業工具取得新的 file_id。"
    )


@tool_guard
async def notifications(limit: int = 20) -> str:
    """列出 Moodle 通知（唯讀，不會標記為已讀）。

    Args:
        limit: 最多顯示幾則（1–50，預設 20）。

    Returns:
        每則通知的時間、主旨、是否已讀、notification_id 與摘要。
    """
    limit = max(1, min(int(limit), 50))
    client = get_client()
    info = await client.site_info()
    data = await client.call(
        "message_popup_get_popup_notifications",
        useridto=int(info.get("userid")),
        limit=limit,
        ttl=TTL_ANNOUNCEMENTS,
    )
    notifications_list = data.get("notifications") if isinstance(data, dict) else data
    notifications_list = [
        item for item in (notifications_list or []) if isinstance(item, dict)
    ]
    if not notifications_list:
        return "（沒有通知）"

    lines = [f"# 通知（{len(notifications_list)} 則）", ""]
    for item in notifications_list:
        read_state = "已讀" if item.get("read") else "**未讀**"
        lines.append(f"### {item.get('subject') or '（無主旨）'}")
        lines.append(
            f"- 時間：{format_ts(item.get('timecreated'))}　狀態：{read_state}"
            f"　notification_id：{item.get('id')}"
        )
        summary = html_to_plain(
            item.get("fullmessage") or item.get("text") or "", limit=200
        )
        if summary:
            lines.append(f"- 內容：{summary}")
        lines.append("")

    return truncate_output("\n".join(lines).rstrip(), 12000)


def register(mcp: Any) -> None:
    for tool in (clear_cache, notifications):
        mcp.add_tool(tool)


__all__ = ["clear_cache", "notifications", "register"]
