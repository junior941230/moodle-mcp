"""公告與討論區 tools。"""

from __future__ import annotations

import asyncio
from typing import Any

from ..cache import TTL_ANNOUNCEMENTS
from ..client import get_client
from ..errors import MoodleError, tool_guard
from ..formatting import (
    format_ts,
    html_to_markdown,
    html_to_plain,
    truncate_output,
)
from ._common import (
    attachment_lines,
    course_label,
    course_name_map,
    fetch_courses_by_classification,
    parse_int,
)


async def _target_course_ids(course_id: int | None) -> list[int]:
    if course_id is not None:
        return [int(course_id)]
    courses = await fetch_courses_by_classification("inprogress")
    return [parse_int(c.get("id")) for c in courses if parse_int(c.get("id"))]


async def _fetch_forums(course_ids: list[int]) -> list[dict[str, Any]]:
    client = get_client()
    try:
        data = await client.call(
            "mod_forum_get_forums_by_courses",
            courseids=course_ids,
            ttl=TTL_ANNOUNCEMENTS,
        )
    except MoodleError:
        # 較舊的 Moodle 不接受 courseids，改抓全部再自行過濾
        data = await client.call(
            "mod_forum_get_forums_by_courses", ttl=TTL_ANNOUNCEMENTS
        )
    forums = data if isinstance(data, list) else (data.get("forums") if isinstance(data, dict) else [])
    return [forum for forum in (forums or []) if isinstance(forum, dict)]


async def _forum_discussions(forum_id: int, limit: int) -> list[dict[str, Any]]:
    data = await get_client().call(
        "mod_forum_get_forum_discussions",
        forumid=int(forum_id),
        sortorder=1,
        perpage=max(1, min(int(limit), 50)),
        ttl=TTL_ANNOUNCEMENTS,
    )
    return [d for d in ((data.get("discussions") or []) if isinstance(data, dict) else []) if isinstance(d, dict)]


@tool_guard
async def list_announcements(
    course_id: int | None = None, limit: int = 10
) -> str:
    """列出課程公告（Moodle 討論區中 type=news 的「公告」），依時間新到舊。

    Args:
        course_id: 只查某一門課；省略時查所有進行中的課程。
        limit: 最多顯示幾則公告（1–50，預設 10）。

    Returns:
        每則公告的時間、課程、標題、作者、discussion_id 與前 200 字摘要。
        想看完整內容可呼叫 get_discussion(discussion_id)。
    """
    limit = max(1, min(int(limit), 50))
    course_ids = await _target_course_ids(course_id)
    if not course_ids:
        return "（目前沒有進行中的課程，無法列出公告）"

    forums = await _fetch_forums(course_ids)
    news_forums = [
        forum
        for forum in forums
        if str(forum.get("type") or "").lower() == "news"
        and parse_int(forum.get("course")) in course_ids
    ]
    if not news_forums:
        return "（這些課程沒有公告討論區）"

    discussions_lists = await asyncio.gather(
        *[_forum_discussions(parse_int(f.get("id")), limit) for f in news_forums]
    )

    course_names: dict[int, dict[str, Any]] = {}
    try:
        course_names = await course_name_map()
    except MoodleError:
        course_names = {}

    items: list[dict[str, Any]] = []
    for forum, discussions in zip(news_forums, discussions_lists):
        course_id_value = parse_int(forum.get("course"))
        course = course_names.get(course_id_value) or {
            "id": course_id_value,
            "shortname": forum.get("courseshortname"),
            "fullname": forum.get("coursefullname"),
        }
        for discussion in discussions:
            enriched = dict(discussion)
            enriched["_course"] = course
            enriched["_forum_name"] = forum.get("name")
            items.append(enriched)

    items.sort(key=lambda d: parse_int(d.get("created")), reverse=True)
    items = items[:limit]
    if not items:
        return "（目前沒有公告）"

    lines = [f"# 最新公告（{len(items)} 則）", ""]
    for index, discussion in enumerate(items, start=1):
        subject = html_to_plain(
            str(discussion.get("subject") or discussion.get("name") or "")
        ) or "（無標題）"
        discussion_id = discussion.get("discussion") or discussion.get("id")
        author = discussion.get("userfullname") or "（未知作者）"
        lines.append(f"### {index}. {subject}")
        lines.append(
            f"- 時間：{format_ts(discussion.get('created'))}　"
            f"課程：{course_label(discussion.get('_course'))}　"
            f"作者：{author}　discussion_id：{discussion_id}"
        )
        summary = html_to_plain(discussion.get("message") or "", limit=200)
        if summary:
            lines.append(f"- 摘要：{summary}")
        lines.append("")

    return truncate_output("\n".join(lines).rstrip(), 12000)


@tool_guard
async def get_discussion(discussion_id: int) -> str:
    """取得一整串討論 / 公告的所有貼文（依時間排序）。

    Args:
        discussion_id: 討論串 id（由 list_announcements 或其他工具取得）。

    Returns:
        標題、每則貼文的作者、時間、post_id、內文（markdown）與附件 file_id。
        附件可用 read_file(file_id) 讀取。
    """
    data = await get_client().call(
        "mod_forum_get_discussion_posts",
        discussionid=int(discussion_id),
        ttl=TTL_ANNOUNCEMENTS,
    )
    posts = data.get("posts") if isinstance(data, dict) else data
    posts = [post for post in (posts or []) if isinstance(post, dict)]
    if not posts:
        return f"（discussion_id={discussion_id} 沒有貼文）"

    posts.sort(key=lambda post: parse_int(post.get("created")))
    first_subject = next(
        (
            html_to_plain(str(p.get("subject")))
            for p in posts
            if p.get("subject")
        ),
        f"討論 {discussion_id}",
    )
    lines = [f"# {first_subject}", "", f"- discussion_id：{discussion_id}　共 {len(posts)} 則貼文", ""]

    for index, post in enumerate(posts, start=1):
        author = (post.get("author") or {}).get("fullname") or "（未知作者）"
        lines.append(f"## {index}. {author}（{format_ts(post.get('created'))}）")
        lines.append(f"- post_id：{post.get('id')}")
        lines.append("")
        message = html_to_markdown(post.get("message") or "")
        lines.append(message or "（沒有內容）")
        attachments = attachment_lines(post.get("attachments"))
        if attachments:
            lines.extend(["", *attachments])
        lines.append("")

    return truncate_output("\n".join(lines).rstrip(), 12000)


_FORUM_TYPES = {
    "news": "公告",
    "general": "一般討論",
    "single": "單一討論",
    "eachuser": "每人一則",
    "qanda": "問與答",
    "blog": "部落格格式",
}


async def find_forum(forum_id: int) -> dict[str, Any] | None:
    """在所有已選課程的討論區中找 forum_id。"""
    from ._common import fetch_all_courses

    courses = await fetch_all_courses()
    course_ids = [parse_int(c.get("id")) for c in courses if parse_int(c.get("id"))]
    if not course_ids:
        return None
    for forum in await _fetch_forums(course_ids):
        if parse_int(forum.get("id")) == int(forum_id):
            return forum
    return None


@tool_guard
async def list_forums(course_id: int) -> str:
    """列出某門課的所有討論區（含公告區）。

    Args:
        course_id: 課程 id（由 list_courses 取得）。

    Returns:
        markdown 表格：forum_id、名稱、類型。
        想看某個討論區的討論串可用 list_announcements（公告區）或 get_discussion。
    """
    forums = [
        forum
        for forum in await _fetch_forums([int(course_id)])
        if parse_int(forum.get("course")) == int(course_id)
    ]
    if not forums:
        return f"（course_id={course_id} 沒有討論區）"

    from ..formatting import md_table

    rows = []
    for forum in forums:
        forum_type = str(forum.get("type") or "")
        can_post = forum.get("cancreatediscussions")
        rows.append(
            [
                parse_int(forum.get("id")),
                html_to_plain(str(forum.get("name") or "")) or "（未命名）",
                _FORUM_TYPES.get(forum_type, forum_type or "—"),
                "—" if can_post is None else ("可" if can_post else "不可"),
            ]
        )
    table = md_table(["forum_id", "名稱", "類型", "可發新討論"], rows)
    return f"# 討論區（course_id={course_id}）\n\n{table}"


def register(mcp: Any) -> None:
    for tool in (list_announcements, get_discussion, list_forums):
        mcp.add_tool(tool)


__all__ = ["find_forum", "get_discussion", "list_announcements", "list_forums", "register"]
