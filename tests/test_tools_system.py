"""系統 tools（clear_cache / notifications）與 server 註冊測試。"""

from __future__ import annotations

import pytest
from conftest import load_fixture

from moodle_mcp.cache import get_cache, lookup_file, register_file_id
from moodle_mcp.config import get_settings
from moodle_mcp.tools.system import clear_cache, notifications


@pytest.mark.asyncio
async def test_notifications(api):
    api.on(
        "message_popup_get_popup_notifications",
        load_fixture("notifications.json"),
    )
    result = await notifications(limit=10)

    assert "作業 1 已評分" in result
    assert "新公告：期中考範圍" in result
    assert "未讀" in result
    assert "已讀" in result
    assert "test-token-123" not in result

    request = [r for r in api.requests if r["function"].startswith("message_popup")][-1]
    assert request["params"]["useridto"] == ["42"]
    assert request["params"]["limit"] == ["10"]


@pytest.mark.asyncio
async def test_notifications_empty(api):
    api.on("message_popup_get_popup_notifications", {"notifications": []})
    assert "沒有通知" in await notifications()


@pytest.mark.asyncio
async def test_clear_cache(api):
    get_cache().set("api:dummy:key", {"value": 1}, ttl=100)
    register_file_id(
        file_id="aaaabbbbcccc",
        url="https://moodle.example.edu/pluginfile.php/1/a.txt",
        filename="a.txt",
    )
    downloads = get_settings().downloads_dir
    (downloads / "aaaabbbbcccc.pdf").write_bytes(b"%PDF")
    (downloads / "ddddeeeeffff.txt.part").write_bytes(b"partial")
    assert get_cache().get("api:dummy:key") is not None
    assert lookup_file("aaaabbbbcccc") is not None

    result = await clear_cache()
    assert "已清除" in result
    assert "2 個殘留的下載檔" in result
    assert get_cache().get("api:dummy:key") is None
    assert lookup_file("aaaabbbbcccc") is None
    assert list(downloads.iterdir()) == []


def test_all_tools_registered():
    from moodle_mcp.server import mcp

    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    expected = {
        "whoami",
        "list_courses",
        "get_course_contents",
        "search_course",
        "list_assignments",
        "get_assignment",
        "upcoming_deadlines",
        "list_announcements",
        "get_discussion",
        "list_forums",
        "get_grades",
        "read_file",
        "notifications",
        "clear_cache",
    }
    assert expected <= set(tools)
    for name in expected:
        assert tools[name].description, f"{name} 缺少 docstring"


def test_no_write_tools_registered_by_default():
    from moodle_mcp.server import mcp
    from moodle_mcp.tools.write import WRITE_TOOLS

    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert not registered & {tool.__name__ for tool, _, _ in WRITE_TOOLS}


@pytest.mark.asyncio
async def test_tool_guard_returns_friendly_auth_message(api):
    from moodle_mcp.tools.courses import whoami

    api.on(
        "core_webservice_get_site_info",
        {
            "exception": "Invalid token",
            "errorcode": "invalidtoken",
            "message": "Invalid token - token expired",
        },
    )
    result = await whoami()
    assert result.startswith("⚠️")
    assert "Token 無效" in result
    assert "get_token" in result


@pytest.mark.asyncio
async def test_unavailable_tool_returns_message_not_traceback(api):
    from moodle_mcp.client import get_client
    from moodle_mcp.tools.forums import list_announcements

    info = load_fixture("site_info.json")
    info["functions"] = [
        f for f in info["functions"] if f["name"] != "mod_forum_get_forums_by_courses"
    ]
    api.on("core_webservice_get_site_info", info)
    await get_client().site_info()  # 預先載入 functions 清單

    result = await list_announcements(course_id=101)
    assert "本站未開放" in result
    assert "Traceback" not in result
