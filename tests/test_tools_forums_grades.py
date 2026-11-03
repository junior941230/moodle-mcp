"""公告 / 討論區與成績 tools 測試。"""

from __future__ import annotations

import re

import pytest
from conftest import load_fixture

from moodle_mcp.cache import lookup_file
from moodle_mcp.tools.forums import get_discussion, list_announcements
from moodle_mcp.tools.grades import get_grades


@pytest.mark.asyncio
async def test_list_announcements(api):
    api.on("mod_forum_get_forums_by_courses", load_fixture("forums.json"))
    api.on(
        "mod_forum_get_forum_discussions",
        load_fixture("forum_discussions.json"),
    )
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        load_fixture("courses_timeline.json"),
    )
    result = await list_announcements(course_id=101)

    assert "期中考範圍" in result
    assert "Teacher A" in result
    assert "discussion_id：501" in result
    assert "涵蓋第 1–6 週" in result
    assert "作業上傳問題" in result
    assert "test-token-123" not in result

    # 只查一門課 → 只打一次討論區 API
    assert api.count("mod_forum_get_forum_discussions") == 1


@pytest.mark.asyncio
async def test_list_announcements_no_forum(api):
    api.on("mod_forum_get_forums_by_courses", [])
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        load_fixture("courses_timeline.json"),
    )
    result = await list_announcements()
    assert "沒有公告討論區" in result


@pytest.mark.asyncio
async def test_get_discussion(api):
    api.on("mod_forum_get_discussion_posts", load_fixture("discussion_posts.json"))
    result = await get_discussion(501)

    assert "期中考範圍" in result
    assert "Teacher A" in result
    assert "Test User" in result
    assert "請問樹的平衡也會考嗎" in result
    assert "review.pdf" in result
    assert "pluginfile.php" not in result

    match = re.search(r"file_id=([0-9a-f]{12})", result)
    assert match
    assert lookup_file(match.group(1))["filename"] == "review.pdf"


@pytest.mark.asyncio
async def test_get_discussion_empty(api):
    api.on("mod_forum_get_discussion_posts", {"posts": []})
    assert "沒有貼文" in await get_discussion(123)


@pytest.mark.asyncio
async def test_get_grades_overview(api):
    api.on(
        "gradereport_overview_get_course_grades",
        load_fixture("grades_overview.json"),
    )
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        load_fixture("courses_timeline.json"),
    )
    result = await get_grades()
    assert "各課程總成績" in result
    assert "CS2001" in result
    assert "86.50" in result
    assert "CS3002" in result
    assert "排名" in result


@pytest.mark.asyncio
async def test_get_grades_course_detail(api):
    api.on("gradereport_user_get_grade_items", load_fixture("grade_items.json"))
    result = await get_grades(course_id=101)

    assert "成績明細（course_id=101）" in result
    assert "作業 1：鏈結串列" in result
    assert "88.00" in result
    assert "88.00 %" in result
    assert "結構正確" in result
    assert "課程總分" in result

    request = [r for r in api.requests if r["function"] == "gradereport_user_get_grade_items"][-1]
    assert request["params"]["userid"] == ["42"]
    assert request["params"]["courseid"] == ["101"]


@pytest.mark.asyncio
async def test_get_grades_empty(api):
    api.on("gradereport_overview_get_course_grades", {"grades": []})
    assert "查無成績" in await get_grades()
