"""行事曆 tool 測試。"""

from __future__ import annotations

import time

import pytest

from moodle_mcp.tools.calendar import upcoming_deadlines


def _event(name: str, timestamp: int, modulename: str = "assign", eventtype: str = "due"):
    return {
        "id": abs(hash(name)) % 100000,
        "name": name,
        "timestart": timestamp,
        "timeduration": 0,
        "course": {"id": 101, "shortname": "CS2001", "fullname": "資料結構與演算法"},
        "modulename": modulename,
        "eventtype": eventtype,
    }


@pytest.mark.asyncio
async def test_upcoming_deadlines_filters_window(api):
    now = int(time.time())
    api.on(
        "core_calendar_get_action_events_by_timesort",
        {
            "events": [
                _event("兩天後交作業", now + 2 * 86400),
                _event("十天前截止", now - 10 * 86400),
                _event("三天後小考", now + 3 * 86400, "quiz", "open"),
            ]
        },
    )
    result = await upcoming_deadlines(14)
    assert "兩天後交作業" in result
    assert "三天後小考" in result
    assert "十天前截止" not in result
    assert "作業" in result and "測驗" in result
    assert "CS2001" in result


@pytest.mark.asyncio
async def test_upcoming_deadlines_empty(api):
    api.on("core_calendar_get_action_events_by_timesort", {"events": []})
    result = await upcoming_deadlines(7)
    assert "未來 7 天內沒有" in result


@pytest.mark.asyncio
async def test_upcoming_deadlines_fallback(api):
    now = int(time.time())
    api.on(
        "core_calendar_get_action_events_by_timesort",
        {
            "exception": "Web service function is not available",
            "errorcode": "accessexception",
            "message": "Function core_calendar_get_action_events_by_timesort is not available",
        },
    )
    api.on(
        "core_calendar_get_calendar_upcoming_view",
        {"events": [_event("備援事件", now + 86400)]},
    )
    result = await upcoming_deadlines(7)
    assert "備援事件" in result


@pytest.mark.asyncio
async def test_upcoming_deadlines_clamps_days(api):
    now = int(time.time())
    api.on(
        "core_calendar_get_action_events_by_timesort",
        {"events": [_event("很久以後", now + 300 * 86400)]},
    )
    result = await upcoming_deadlines(9999)
    assert "未來 365 天" in result
    assert "很久以後" in result
