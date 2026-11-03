"""課程相關 tools：whoami / list_courses / get_course_contents。"""

from __future__ import annotations

from typing import Any, Literal

from ..cache import TTL_CONTENTS
from ..client import get_client
from ..errors import MoodleError, tool_guard
from ..formatting import (
    format_date,
    html_to_markdown,
    html_to_plain,
    md_table,
    truncate_output,
)
from ._common import (
    course_label,
    course_module_file_line,
    fetch_courses_by_classification,
    parse_int,
)


@tool_guard
async def whoami() -> str:
    """查詢目前 Moodle token 代表的使用者與站台資訊。

    回傳姓名、userid、站台名稱與此 token 可用的 Web Service 功能數量。
    可用來確認 token 是否有效，以及判斷哪些功能（作業、公告、成績…）可用。
    """
    info = await get_client().site_info()
    functions = info.get("functions") or []
    lines = [
        "# Moodle 身分",
        "",
        f"- 姓名：{info.get('fullname') or '（未知）'}",
        f"- userid：{info.get('userid')}",
        f"- 站台：{info.get('sitename') or '（未知）'}",
        f"- 站台版本：{info.get('release') or '（未知）'}",
        f"- 可用 Web Service 功能：{len(functions)} 個",
    ]
    if info.get("username"):
        lines.insert(2, f"- 帳號：{info.get('username')}")
    return "\n".join(lines)


@tool_guard
async def list_courses(
    filter: Literal["inprogress", "past", "future", "all"] = "inprogress",
) -> str:
    """列出使用者已選的 Moodle 課程。

    Args:
        filter: 課程時間分類。`inprogress`（進行中，預設）、`past`（已結束）、
            `future`（未開始）、`all`（全部）。

    Returns:
        markdown 表格：課程 id、簡稱、全名、進度百分比、最後存取時間。
        後續可用課程 id 呼叫 get_course_contents / list_assignments / get_grades。
    """
    courses = await fetch_courses_by_classification(filter)
    if not courses:
        return f"（沒有符合 `{filter}` 的課程）"

    rows = []
    for course in courses:
        progress = course.get("progress")
        try:
            progress_text = f"{float(progress):.0f}%" if progress is not None else "—"
        except (TypeError, ValueError):
            progress_text = "—"
        rows.append(
            [
                course.get("id"),
                course_label(course),
                course.get("fullname") or "—",
                progress_text,
                format_date(course.get("lastaccess")),
            ]
        )
    table = md_table(["id", "課程簡稱", "課程全名", "進度", "最後存取"], rows)
    return truncate_output(
        f"# 我的課程（{filter}）\n\n{table}\n\n共 {len(rows)} 門課。", 12000
    )


def _section_heading(section: dict[str, Any], index: int) -> str:
    number = section.get("section")
    if number is None:
        number = index + 1
    name = html_to_plain(section.get("name") or "").strip()
    if not name or name.lower() in {"general", "一般", "課程簡介"}:
        name = name or ("課程簡介" if index == 0 else f"第 {number} 週 / 章節")
    return f"## {number}. {name}"


def _render_module(module: dict[str, Any]) -> list[str]:
    modname = module.get("modname") or module.get("modplural") or "mod"
    name = module.get("name") or "（未命名活動）"
    cmid = module.get("cmid") or module.get("id") or "?"
    lines = [f"- **[{modname}] {name}** (cmid={cmid})"]

    description = html_to_plain(module.get("description") or "", limit=400)
    if description:
        lines.append(f"  - 說明：{description}")

    for item in module.get("contents") or []:
        if isinstance(item, dict):
            lines.append("  " + course_module_file_line(item, indent="").lstrip())
    return lines


@tool_guard
async def get_course_contents(course_id: int, section: int | None = None) -> str:
    """取得一門課程的章節與教材 / 活動清單。

    Args:
        course_id: 課程 id（可由 list_courses 取得）。
        section: 只顯示某個章節（Moodle 的 section 編號，通常 0 是課程簡介、
            1 是第一週）。省略則列出全部章節。

    Returns:
        依章節列出模組（`[modname] 名稱 (cmid=…)`），檔案會附上 file_id，
        可再用 read_file(file_id) 讀取內容。
    """
    client = get_client()
    data = await client.call(
        "core_course_get_contents", courseid=int(course_id), ttl=TTL_CONTENTS
    )
    sections = data if isinstance(data, list) else []
    if not sections:
        return "（這門課沒有可顯示的內容，或課程是空的）"

    selected: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(sections):
        if not isinstance(item, dict):
            continue
        if section is not None:
            item_section = parse_int(item.get("section"), index)
            if item_section != int(section) and index != int(section):
                continue
        selected.append((index, item))

    if not selected:
        return f"（找不到 section={section} 的章節）"

    # 課程名稱（盡量從快取 / timeline 取得；失敗就算了）
    title = f"course_id={course_id}"
    try:
        from ._common import course_name_map

        course = (await course_name_map()).get(int(course_id))
        if course:
            title = f"{course_label(course)}（{course.get('fullname')}，course_id={course_id}）"
    except MoodleError:
        pass

    lines = [f"# 課程內容：{title}", ""]
    for index, item in selected:
        lines.append(_section_heading(item, index))
        summary = html_to_markdown(item.get("summary") or "")
        if summary:
            lines.append("")
            lines.append(summary)
        modules = [m for m in (item.get("modules") or []) if isinstance(m, dict)]
        if not modules:
            if not summary:
                lines.append("")
                lines.append("（此章節沒有教材）")
        else:
            lines.append("")
            for module in modules:
                lines.extend(_render_module(module))
        lines.append("")

    return truncate_output("\n".join(lines).rstrip(), 12000)


def register(mcp: Any) -> None:
    for tool in (whoami, list_courses, get_course_contents):
        mcp.add_tool(tool)


__all__ = ["whoami", "list_courses", "get_course_contents", "register"]
