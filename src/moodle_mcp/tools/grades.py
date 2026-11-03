"""成績相關 tools。"""

from __future__ import annotations

from typing import Any

from ..cache import TTL_GRADES
from ..client import get_client
from ..errors import MoodleError, tool_guard
from ..formatting import html_to_plain, md_table, truncate_output
from ._common import course_name_map, parse_int


def _format_score(grade: Any) -> str:
    if grade is None or grade == "":
        return "—"
    return str(grade)


@tool_guard
async def get_grades(course_id: int | None = None) -> str:
    """查詢成績。省略 course_id 時列出各課程總成績；指定 course_id 時列出該課所有評分項目。

    Args:
        course_id: 課程 id；省略則回傳所有課程的總成績與排名。

    Returns:
        沒有 course_id：表格（課程、成績、排名）。
        有 course_id：表格（評分項目、類型、分數、範圍、百分比、評語）。
    """
    client = get_client()

    if course_id is None:
        data = await client.call(
            "gradereport_overview_get_course_grades", ttl=TTL_GRADES
        )
        grades = data.get("grades") if isinstance(data, dict) else data
        grades = [g for g in (grades or []) if isinstance(g, dict)]
        if not grades:
            return "（查無成績；可能成績尚未公布，或此站未開放成績 API）"

        try:
            names = await course_name_map()
        except MoodleError:
            names = {}

        rows = []
        for grade in grades:
            cid = parse_int(grade.get("courseid"))
            course = names.get(cid) or {}
            rows.append(
                [
                    cid,
                    course.get("shortname") or course.get("fullname") or f"course {cid}",
                    _format_score(grade.get("grade") or grade.get("rawgrade")),
                    grade.get("rank") or "—",
                ]
            )
        return truncate_output(
            "# 各課程總成績\n\n"
            + md_table(["course_id", "課程", "成績", "排名"], rows)
            + "\n\n可用 get_grades(course_id=…) 查看單一課程的評分項目。",
            12000,
        )

    info = await client.site_info()
    userid = info.get("userid")
    data = await client.call(
        "gradereport_user_get_grade_items",
        courseid=int(course_id),
        userid=int(userid),
        ttl=TTL_GRADES,
    )
    usergrades = data.get("usergrades") if isinstance(data, dict) else data
    usergrades = [u for u in (usergrades or []) if isinstance(u, dict)]
    if not usergrades:
        return f"（course_id={course_id} 查無成績）"

    grade_items = [
        item for item in (usergrades[0].get("gradeitems") or []) if isinstance(item, dict)
    ]
    if not grade_items:
        return f"（course_id={course_id} 沒有評分項目）"

    rows = []
    for item in grade_items:
        name = item.get("itemname") or (
            "課程總分" if item.get("itemtype") == "course" else "（未命名項目）"
        )
        raw = item.get("graderaw")
        score = item.get("gradeformatted") or _format_score(raw)
        minimum = item.get("grademin")
        maximum = item.get("grademax")
        if minimum is not None and maximum is not None:
            range_text = f"{minimum}–{maximum}"
        else:
            range_text = "—"
        percentage = item.get("percentageformatted")
        if not percentage and raw is not None and maximum:
            try:
                percentage = f"{float(raw) / float(maximum) * 100:.1f}%"
            except (TypeError, ValueError, ZeroDivisionError):
                percentage = "—"
        feedback = html_to_plain(item.get("feedback") or "", limit=120)
        item_type = item.get("itemtype") or item.get("itemmodule") or "—"
        rows.append(
            [name, item_type, score, range_text, percentage or "—", feedback or "—"]
        )

    return truncate_output(
        f"# 成績明細（course_id={course_id}）\n\n"
        + md_table(
            ["評分項目", "類型", "分數", "範圍", "百分比", "評語"], rows
        ),
        12000,
    )


def register(mcp: Any) -> None:
    mcp.add_tool(get_grades)


__all__ = ["get_grades", "register"]
