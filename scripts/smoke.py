#!/usr/bin/env python3
"""手動驗收：用真 token 依序呼叫每個 tool，印出前 300 字摘要。

用法：
    uv run python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Awaitable, Callable

from moodle_mcp.client import close_client
from moodle_mcp.config import get_settings
from moodle_mcp.tools._common import fetch_all_courses
from moodle_mcp.tools.assignments import get_assignment, list_assignments
from moodle_mcp.tools.calendar import upcoming_deadlines
from moodle_mcp.tools.contents import search_course
from moodle_mcp.tools.courses import get_course_contents, list_courses, whoami
from moodle_mcp.tools.files import read_file
from moodle_mcp.tools.forums import get_discussion, list_announcements
from moodle_mcp.tools.grades import get_grades
from moodle_mcp.tools.system import clear_cache, notifications

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PREVIEW = 300


def preview(text: str, limit: int = PREVIEW) -> str:
    text = (text or "").replace("\r", "")
    return text[:limit] + ("…" if len(text) > limit else "")


async def run_tool(name: str, factory: Callable[[], Awaitable[str]]) -> str | None:
    print(f"\n{'=' * 60}\n▶ {name}\n{'=' * 60}")
    try:
        result = await factory()
    except Exception as exc:  # noqa: BLE001 - smoke 工具要繼續跑完其他項目
        print(f"❌ 例外：{exc.__class__.__name__}: {exc}")
        return None
    print(preview(result))
    if "⚠️" in result:
        print("（注意：此 tool 回傳錯誤訊息）")
    return result


async def main() -> int:
    settings = get_settings()
    if not settings.moodle_token:
        print("錯誤：MOODLE_TOKEN 未設定。先執行 scripts/get_token.py", file=sys.stderr)
        return 1
    print(f"站台：{settings.moodle_url}")

    failures = 0
    try:
        whoami_text = await run_tool("whoami", whoami)
        failures += bool(not whoami_text or "⚠️" in whoami_text)

        courses_text = await run_tool("list_courses", lambda: list_courses("inprogress"))
        failures += bool(not courses_text or "⚠️" in courses_text)

        courses = await fetch_all_courses()
        course_id = int(courses[0]["id"]) if courses else None

        contents_text = await run_tool(
            "get_course_contents",
            lambda: get_course_contents(course_id) if course_id else _skip("沒有課程"),
        )
        failures += bool(contents_text and "⚠️" in contents_text)

        await run_tool("upcoming_deadlines(14)", lambda: upcoming_deadlines(14))
        assignments_text = await run_tool("list_assignments", list_assignments)

        assign_match = re.search(r"\|\s*(\d+)\s*\|?\s*$", assignments_text or "", re.MULTILINE)
        if assign_match:
            await run_tool(
                "get_assignment", lambda: get_assignment(int(assign_match.group(1)))
            )
        else:
            print("\n（略過 get_assignment：找不到 assign_id）")

        await run_tool("list_announcements", list_announcements)
        discussion_match = re.search(
            r"discussion_id：(\d+)", (await list_announcements(limit=3)) or ""
        )
        if discussion_match:
            await run_tool(
                "get_discussion",
                lambda: get_discussion(int(discussion_match.group(1))),
            )

        await run_tool("get_grades（總覽）", get_grades)
        if course_id:
            await run_tool(
                "get_grades（單課）", lambda: get_grades(course_id=course_id)
            )
            await run_tool(
                "search_course", lambda: search_course(course_id, "作業")
            )

        file_match = re.search(r"file_id=([0-9a-f]{12})", contents_text or "")
        if file_match:
            await run_tool(
                "read_file", lambda: read_file(file_match.group(1), max_chars=2000)
            )
        else:
            print("\n（略過 read_file：課程內容沒有可讀檔案）")

        await run_tool("notifications", notifications)
        await run_tool("clear_cache", clear_cache)
    finally:
        await close_client()

    print(f"\n{'=' * 60}")
    print("完成。" if failures == 0 else f"有 {failures} 個主要 tool 回傳錯誤，請檢查上方輸出。")
    return 0 if failures == 0 else 1


async def _skip(reason: str) -> str:
    return f"（略過：{reason}）"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
