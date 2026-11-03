"""行事曆 / 截止日 tool。"""

from __future__ import annotations

import time
from typing import Any

from ..cache import TTL_CALENDAR
from ..client import get_client
from ..errors import FunctionUnavailable, tool_guard
from ..formatting import format_ts, md_table, truncate_output
from ._common import course_label, parse_int

_MODULE_LABELS = {
    "assign": "作業",
    "quiz": "測驗",
    "forum": "討論",
    "resource": "教材",
    "url": "連結",
    "page": "頁面",
    "folder": "資料夾",
    "feedback": "回饋",
    "workshop": "工作坊",
    "choice": "投票",
}

_EVENT_TYPE_LABELS = {
    "due": "截止",
    "close": "關閉",
    "open": "開始",
    "opens": "開放",
    "gradingdue": "評分截止",
}


def _event_type(event: dict[str, Any]) -> str:
    module = str(event.get("modulename") or "")
    label = _MODULE_LABELS.get(module, module or "其他")
    event_type = str(event.get("eventtype") or "")
    suffix = _EVENT_TYPE_LABELS.get(event_type, "")
    return f"{label}（{suffix}）" if suffix else label


def _render_events(events: list[dict[str, Any]], title: str) -> str:
    rows = []
    for event in events:
        timestamp = parse_int(event.get("timestart"))
        course = event.get("course") or {}
        rows.append(
            [
                format_ts(timestamp),
                course_label(course),
                event.get("name") or "（未命名事件）",
                _event_type(event),
            ]
        )
    rows.sort(key=lambda row: row[0])
    table = md_table(["時間", "課程", "事件名稱", "類型"], rows)
    return f"# {title}\n\n{table}\n\n共 {len(rows)} 個事件。"


@tool_guard
async def upcoming_deadlines(days: int = 14) -> str:
    """列出未來 N 天內的行事曆事件與截止日（作業、測驗、其他活動）。

    Args:
        days: 往後看幾天，預設 14 天。

    Returns:
        markdown 表格：時間、課程、事件名稱、類型（作業 / 測驗 / 其他）。
        之後可用 list_assignments 取得繳交狀態，或 get_assignment 看細節。
    """
    horizon = max(1, min(int(days), 365))
    now = int(time.time())
    end = now + horizon * 86400
    client = get_client()

    try:
        data = await client.call(
            "core_calendar_get_action_events_by_timesort",
            timesortfrom=now,
            timesortto=end,
            limitnum=50,
            ttl=TTL_CALENDAR,
        )
    except FunctionUnavailable:
        # 退回舊版 upcoming view，再自行依時間過濾
        data = await client.call(
            "core_calendar_get_calendar_upcoming_view", ttl=TTL_CALENDAR
        )

    events = data.get("events") if isinstance(data, dict) else data
    events = [e for e in (events or []) if isinstance(e, dict)]
    filtered = []
    for event in events:
        timestamp = parse_int(event.get("timestart"))
        if now - 86400 <= timestamp <= end:
            filtered.append(event)

    if not filtered:
        return f"（未來 {horizon} 天內沒有行事曆事件）"
    return truncate_output(_render_events(filtered, f"未來 {horizon} 天的截止日 / 事件"), 12000)


def register(mcp: Any) -> None:
    mcp.add_tool(upcoming_deadlines)


__all__ = ["upcoming_deadlines", "register"]
