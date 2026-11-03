"""課程相關 tools 測試。"""

from __future__ import annotations

import re

import pytest
from conftest import SITE_INFO, load_fixture

from moodle_mcp.cache import lookup_file
from moodle_mcp.tools.contents import search_course
from moodle_mcp.tools.courses import get_course_contents, list_courses, whoami


@pytest.mark.asyncio
async def test_whoami(api):
    result = await whoami()
    assert "Test User" in result
    assert "userid：42" in result
    assert "Example Moodle (test)" in result
    assert "可用 Web Service 功能" in result


@pytest.mark.asyncio
async def test_list_courses_inprogress(api):
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        load_fixture("courses_timeline.json"),
    )
    result = await list_courses("inprogress")
    assert "CS2001" in result
    assert "資料結構與演算法" in result
    assert "CS3002" in result
    assert "62%" in result or "63%" in result


@pytest.mark.asyncio
async def test_list_courses_fallback_to_enrol_api(api):
    info = load_fixture("site_info.json")
    info["functions"] = [
        f
        for f in info["functions"]
        if f["name"] != "core_course_get_enrolled_courses_by_timeline_classification"
    ]
    api.on(SITE_INFO, info)
    api.on(
        "core_enrol_get_users_courses",
        load_fixture("courses_timeline.json")["courses"],
    )
    result = await list_courses("all")
    assert "CS2001" in result
    assert "共 2 門課" in result


@pytest.mark.asyncio
async def test_list_courses_empty(api):
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        {"courses": []},
    )
    assert "沒有符合" in await list_courses("past")


@pytest.mark.asyncio
async def test_get_course_contents(api):
    api.on("core_course_get_contents", load_fixture("course_contents.json"))
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        load_fixture("courses_timeline.json"),
    )
    result = await get_course_contents(101)

    assert "CS2001" in result
    assert "[resource] Week 1 講義" in result
    assert "cmid=1102" in result
    assert "第 1 週：陣列與鏈結串列" in result
    assert "週一" not in result  # 不應洩漏任何 token 或未轉換的時間格式

    # 檔案只顯示 file_id，不顯示原始 pluginfile URL
    assert "pluginfile.php" not in result
    assert "test-token-123" not in result
    match = re.search(r"file_id=([0-9a-f]{12})", result)
    assert match, result

    meta = lookup_file(match.group(1))
    assert meta is not None
    assert meta["filename"] in {"week1.pdf", "week1-slides.pptx", "syllabus.pdf"}
    assert meta["url"].startswith("https://moodle.example.edu/pluginfile.php/")

    # 外部連結可顯示（不含 token）
    assert "youtube.com" in result


@pytest.mark.asyncio
async def test_get_course_contents_section_filter(api):
    api.on("core_course_get_contents", load_fixture("course_contents.json"))
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        {"courses": []},
    )
    result = await get_course_contents(101, section=2)
    assert "第 2 週：堆疊與佇列" in result
    assert "Week 1 講義" not in result
    assert "此章節沒有教材" in result


@pytest.mark.asyncio
async def test_get_course_contents_missing_section(api):
    api.on("core_course_get_contents", load_fixture("course_contents.json"))
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        {"courses": []},
    )
    result = await get_course_contents(101, section=99)
    assert "找不到 section=99" in result


@pytest.mark.asyncio
async def test_search_course_uses_cache(api):
    api.on("core_course_get_contents", load_fixture("course_contents.json"))
    await get_course_contents(101)
    assert api.count("core_course_get_contents") == 1

    result = await search_course(101, "鏈結")
    assert "搜尋結果" in result
    assert "第 1 週" in result
    # 沒有再打一次 API
    assert api.count("core_course_get_contents") == 1


@pytest.mark.asyncio
async def test_search_course_fetches_when_cache_empty(api):
    api.on("core_course_get_contents", load_fixture("course_contents.json"))
    result = await search_course(101, "小考")
    assert "[quiz] **小考 1**" in result
    assert api.count("core_course_get_contents") == 1


@pytest.mark.asyncio
async def test_search_course_no_match(api):
    api.on("core_course_get_contents", load_fixture("course_contents.json"))
    result = await search_course(101, "不存在的字串")
    assert "找不到" in result
