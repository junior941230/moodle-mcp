"""作業 tools 測試。"""

from __future__ import annotations

import re

import pytest
from conftest import load_fixture

from moodle_mcp.cache import lookup_file
from moodle_mcp.tools.assignments import get_assignment, list_assignments


def _status_handler(body):
    assign_id = (body.get("assignid") or [""])[0]
    if assign_id == "201":
        return load_fixture("submission_status.json")
    if assign_id == "202":
        return {
            "graded": False,
            "feedback": {"grade": {"graderaw": None}},
            "lastattempt": {
                "submission": {"status": "submitted", "timemodified": 1000050000}
            },
        }
    return {
        "graded": False,
        "feedback": {"grade": {"graderaw": None}},
        "lastattempt": {"submission": {"status": "new", "timemodified": 0}},
    }


def _assignments_handler(body):
    requested = {
        values[0]
        for key, values in body.items()
        if key.startswith("courseids[")
    }
    data = load_fixture("assignments.json")
    data["courses"] = [
        course for course in data["courses"] if str(course["id"]) in requested
    ]
    return data


def _setup(api):
    api.on("mod_assign_get_assignments", _assignments_handler)
    api.on("mod_assign_get_submission_status", _status_handler)
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        load_fixture("courses_timeline.json"),
    )


@pytest.mark.asyncio
async def test_list_assignments_default_hides_past_submitted(api):
    _setup(api)
    result = await list_assignments()

    assert "作業 1：鏈結串列" in result
    assert "作業 2：排程模擬" in result
    assert "作業 0：暖身" not in result  # 過期 + 已繳 → 預設隱藏
    assert "另有 1 份" in result
    assert "已評分" in result
    assert "未繳" in result
    assert "assign_id" in result


@pytest.mark.asyncio
async def test_list_assignments_include_past(api):
    _setup(api)
    result = await list_assignments(include_past=True)
    assert "作業 0：暖身" in result
    assert "已繳" in result


@pytest.mark.asyncio
async def test_list_assignments_single_course(api):
    _setup(api)
    result = await list_assignments(course_id=102)
    assert "作業 2：排程模擬" in result
    assert "作業 1：鏈結串列" not in result
    params = [r for r in api.requests if r["function"] == "mod_assign_get_assignments"][-1]
    assert params["params"]["courseids[0]"] == ["102"]


@pytest.mark.asyncio
async def test_list_assignments_no_courses(api):
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        {"courses": []},
    )
    assert "沒有進行中的課程" in await list_assignments()


@pytest.mark.asyncio
async def test_get_assignment_detail(api):
    _setup(api)
    result = await get_assignment(201)

    assert "作業 1：鏈結串列" in result
    assert "CS2001" in result
    assert "截止日" in result
    assert "已評分" in result
    assert "88.00" in result
    assert "reverse 沒有處理空鏈結串列" in result
    assert "spec.pdf" in result
    assert "pluginfile.php" not in result
    assert "test-token-123" not in result

    match = re.search(r"file_id=([0-9a-f]{12})", result)
    assert match
    meta = lookup_file(match.group(1))
    assert meta["filename"] == "spec.pdf"


@pytest.mark.asyncio
async def test_get_assignment_not_found(api):
    _setup(api)
    result = await get_assignment(999)
    assert "找不到 assign_id=999" in result


@pytest.mark.asyncio
async def test_get_assignment_fetches_index_when_cold(api):
    _setup(api)
    # 沒有先呼叫 list_assignments，get_assignment 應自行建立索引
    result = await get_assignment(203)
    assert "作業 2：排程模擬" in result
    assert "未繳" in result
