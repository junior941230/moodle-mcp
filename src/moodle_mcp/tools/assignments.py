"""作業相關 tools：list_assignments / get_assignment。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..cache import (
    TTL_ASSIGNMENTS,
    TTL_SUBMISSION,
    lookup_assignment,
    register_assignment,
)
from ..client import get_client
from ..errors import AuthError, MoodleError, NotFoundError, tool_guard
from ..formatting import (
    format_ts,
    html_to_markdown,
    md_table,
    truncate_output,
)
from ._common import (
    attachment_lines,
    course_label,
    fetch_all_courses,
    fetch_courses_by_classification,
    parse_int,
)


async def _fetch_assignments(course_ids: list[int]) -> list[dict[str, Any]]:
    """取得多門課程的作業，並建立 assign_id 索引。"""
    if not course_ids:
        return []
    data = await get_client().call(
        "mod_assign_get_assignments",
        courseids=[int(cid) for cid in course_ids],
        ttl=TTL_ASSIGNMENTS,
    )
    entries: list[dict[str, Any]] = []
    courses = data.get("courses") if isinstance(data, dict) else data
    for course in courses or []:
        if not isinstance(course, dict):
            continue
        course_info = {
            "id": course.get("id"),
            "shortname": course.get("shortname"),
            "fullname": course.get("fullname"),
        }
        for assignment in course.get("assignments") or []:
            if not isinstance(assignment, dict):
                continue
            entries.append({"assignment": assignment, "course": course_info})
            register_assignment(
                parse_int(assignment.get("id")),
                {"assignment": assignment, "course": course_info},
            )
    return entries


async def _submission_status(assign_id: int) -> dict[str, Any] | None:
    try:
        data = await get_client().call(
            "mod_assign_get_submission_status",
            assignid=int(assign_id),
            ttl=TTL_SUBMISSION,
        )
        return data if isinstance(data, dict) else None
    except AuthError:
        raise
    except MoodleError:
        return None


def _status_text(data: dict[str, Any] | None) -> str:
    if not data:
        return "未知"
    feedback = data.get("feedback") or {}
    grade = feedback.get("grade") or {}
    if grade.get("graderaw") is not None:
        return "已評分"
    last_attempt = data.get("lastattempt") or {}
    submission = last_attempt.get("submission") or {}
    status = str(submission.get("status") or "")
    if status == "submitted" or data.get("submissionstatus") == "submitted":
        return "已繳"
    if status == "draft":
        return "草稿"
    return "未繳"


def _grade_text(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    feedback = data.get("feedback") or {}
    grade = feedback.get("grade") or {}
    if grade.get("graderaw") is None:
        return ""
    formatted = grade.get("gradeformatted")
    if formatted:
        return str(formatted)
    raw = grade.get("graderaw")
    maximum = grade.get("grademax")
    return f"{raw} / {maximum}" if maximum else str(raw)


def _submitted_at(data: dict[str, Any] | None) -> int:
    if not data:
        return 0
    last_attempt = data.get("lastattempt") or {}
    submission = last_attempt.get("submission") or {}
    return parse_int(submission.get("timemodified"))


async def _inprogress_course_ids() -> list[int]:
    courses = await fetch_courses_by_classification("inprogress")
    ids: list[int] = []
    for course in courses:
        course_id = parse_int(course.get("id"))
        if course_id:
            ids.append(course_id)
    return ids


@tool_guard
async def list_assignments(
    course_id: int | None = None, include_past: bool = False
) -> str:
    """列出作業、截止日與繳交狀態（依截止日排序）。

    Args:
        course_id: 只查某一門課；省略時查所有進行中的課程。
        include_past: 是否包含「已過期且已繳交」的作業（預設 False，避免雜訊）。
            已過期但尚未繳交的作業一定會顯示。

    Returns:
        markdown 表格：截止日、課程、作業名稱、狀態（未繳 / 已繳 / 已評分 / 草稿）、
        assign_id。可用 get_assignment(assign_id) 看詳細說明與附件。
    """
    if course_id is not None:
        course_ids = [int(course_id)]
    else:
        course_ids = await _inprogress_course_ids()
    if not course_ids:
        return "（目前沒有進行中的課程，無法列出作業）"

    entries = await _fetch_assignments(course_ids)
    if not entries:
        return "（這些課程沒有作業）"

    statuses = await asyncio.gather(
        *[_submission_status(parse_int(e["assignment"].get("id"))) for e in entries]
    )

    now = int(time.time())
    rows: list[tuple[int, list[Any]]] = []
    hidden = 0
    for entry, status in zip(entries, statuses):
        assignment = entry["assignment"]
        assign_id = parse_int(assignment.get("id"))
        due = parse_int(assignment.get("duedate"))
        state = _status_text(status)

        if not include_past and due and due < now and state in {"已繳", "已評分"}:
            hidden += 1
            continue

        name = str(assignment.get("name") or "（未命名作業）")
        if due:
            name += " ⏰" if due < now and state == "未繳" else ""
        rows.append(
            (
                due if due else 2**62,
                [
                    format_ts(due, empty="無截止日"),
                    course_label(entry["course"]),
                    name,
                    state,
                    assign_id,
                ],
            )
        )

    if not rows:
        suffix = f"\n\n（已隱藏 {hidden} 份過期且已繳交的作業；可用 include_past=True 顯示）" if hidden else ""
        return "（沒有需要顯示的作業）" + suffix

    rows.sort(key=lambda item: item[0])
    table = md_table(
        ["截止日", "課程", "作業名稱", "狀態", "assign_id"],
        [row for _, row in rows],
    )
    note = f"\n\n共 {len(rows)} 份作業"
    if hidden:
        note += f"（另有 {hidden} 份過期且已繳交，已隱藏；include_past=True 可顯示）"
    return truncate_output(f"# 作業清單\n\n{table}{note}", 12000)


async def _all_course_ids() -> list[int]:
    courses = await fetch_all_courses()
    ids: list[int] = []
    for course in courses:
        course_id = parse_int(course.get("id"))
        if course_id:
            ids.append(course_id)
    return ids


async def _find_assignment(assign_id: int) -> dict[str, Any]:
    entry = lookup_assignment(assign_id)
    if entry:
        return entry
    # 索引過期（例如重開 process）→ 重新抓所有已選課程（含過去課程）
    await _fetch_assignments(await _all_course_ids())
    entry = lookup_assignment(assign_id)
    if not entry:
        raise NotFoundError(
            f"找不到 assign_id={assign_id} 的作業。"
            "請先用 list_assignments 取得正確的 assign_id。"
        )
    return entry


@tool_guard
async def get_assignment(assign_id: int) -> str:
    """取得單一作業的完整資訊與自己的繳交 / 評分狀態。

    Args:
        assign_id: 作業 id（由 list_assignments 取得；不是 cmid）。

    Returns:
        作業說明（markdown）、附件（附 file_id 可用 read_file 讀取）、
        截止日、遲交截止、繳交狀態、成績與評語。
    """
    entry = await _find_assignment(int(assign_id))
    assignment = entry["assignment"]
    course = entry["course"]
    status = await _submission_status(int(assign_id))

    name = assignment.get("name") or "（未命名作業）"
    lines = [
        f"# 作業：{name}",
        "",
        f"- 課程：{course_label(course)}（{(course.get('fullname') or '')}）",
        f"- assign_id：{assign_id}　cmid：{assignment.get('cmid') or '—'}",
        f"- 開始繳交：{format_ts(assignment.get('allowsubmissionsfromdate'), empty='未設定')}",
        f"- 截止日：{format_ts(assignment.get('duedate'), empty='無截止日')}",
        f"- 遲交截止：{format_ts(assignment.get('cutoffdate'), empty='無')}",
        f"- 繳交狀態：**{_status_text(status)}**",
    ]
    submitted_at = _submitted_at(status)
    if submitted_at:
        lines.append(f"- 最後繳交時間：{format_ts(submitted_at)}")
    grade = _grade_text(status)
    if grade:
        lines.append(f"- 成績：**{grade}**")

    intro = html_to_markdown(assignment.get("intro") or "")
    lines.extend(["", "## 作業說明", "", intro or "（沒有說明）"])

    attachments = attachment_lines(assignment.get("introattachments"))
    if attachments:
        lines.extend(["", "## 作業附件", "", *attachments])

    if status:
        feedback_grade = (status.get("feedback") or {}).get("grade") or {}
        feedback_text = feedback_grade.get("feedback") if isinstance(feedback_grade, dict) else None
        if feedback_text:
            lines.extend(
                [
                    "",
                    "## 評語",
                    "",
                    html_to_markdown(str(feedback_text)) or str(feedback_text),
                ]
            )

    return truncate_output("\n".join(lines), 12000)


def register(mcp: Any) -> None:
    for tool in (list_assignments, get_assignment):
        mcp.add_tool(tool)


__all__ = ["list_assignments", "get_assignment", "register"]
