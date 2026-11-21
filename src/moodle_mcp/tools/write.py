"""寫入類 tools。每一項都要在 .env 用獨立設定開啟，否則不會註冊、也無法呼叫。

| tool                            | 設定                              | 需要使用者確認 |
|---------------------------------|-----------------------------------|----------------|
| mark_notifications_read         | MOODLE_ALLOW_MARK_READ            | 否（低風險）   |
| save_assignment_draft           | MOODLE_ALLOW_SAVE_SUBMISSION      | 是             |
| submit_assignment_for_grading   | MOODLE_ALLOW_SUBMIT_FOR_GRADING   | 是             |
| reply_to_discussion             | MOODLE_ALLOW_FORUM_REPLY          | 是             |
| create_discussion               | MOODLE_ALLOW_FORUM_POST           | 是             |
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from ..client import get_client
from ..config import WRITE_FEATURES, get_settings, write_env_name
from ..errors import FunctionNotAllowed, NotFoundError, tool_guard
from ..formatting import format_ts, html_to_plain, human_size
from ..write_guard import confirm_action, excerpt, resolve_upload_path, text_to_html
from ._common import course_label, parse_int
from .assignments import _find_assignment, _status_text, _submission_status
from .forums import find_forum


def _require(feature: str) -> None:
    """執行期再檢查一次（就算 tool 被註冊了，設定關掉也不能用）。"""
    if not get_settings().write_enabled(feature):
        raise FunctionNotAllowed(
            f"寫入功能「{WRITE_FEATURES[feature]}」未開啟。"
            f"需要時請在 .env 設定 {write_env_name(feature)}=true 後重新啟動 server。"
        )


def _warnings(result: Any) -> list[str]:
    """Moodle 寫入 API 常以 warnings 陣列表示失敗（HTTP 200、沒有 exception）。"""
    items: Any = result
    if isinstance(result, dict):
        items = result.get("warnings") or []
    if not isinstance(items, list):
        return []
    messages = []
    for item in items:
        if isinstance(item, dict):
            messages.append(str(item.get("message") or item.get("warningcode") or item))
        elif item:
            messages.append(str(item))
    return messages


def _warning_reply(action: str, messages: list[str]) -> str:
    joined = "\n".join(f"- {html_to_plain(m)}" for m in messages)
    return f"⚠️ Moodle 沒有完成「{action}」：\n\n{joined}"


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------


@tool_guard
async def mark_notifications_read(
    notification_id: int | None = None, mark_all: bool = False
) -> str:
    """把 Moodle 通知標記為已讀。只在使用者明確要求時使用。

    Args:
        notification_id: 要標記的通知 id（由 notifications 取得）。
        mark_all: 設為 True 時把**所有**通知標記為已讀（忽略 notification_id）。

    Returns:
        執行結果。
    """
    _require("mark_read")
    client = get_client()
    if mark_all:
        info = await client.site_info()
        await client.write(
            "core_message_mark_all_notifications_as_read",
            useridto=int(info.get("userid")),
        )
        return "✅ 已把所有通知標記為已讀。"
    if notification_id is None:
        return "⚠️ 請提供 notification_id，或設定 mark_all=True。"
    await client.write(
        "core_message_mark_notification_read",
        notificationid=int(notification_id),
        timeread=int(time.time()),
    )
    return f"✅ 已把通知 {int(notification_id)} 標記為已讀。"


# ---------------------------------------------------------------------------
# 作業
# ---------------------------------------------------------------------------


def _enabled_submission_plugins(assignment: dict[str, Any]) -> set[str] | None:
    """從 configs 找出啟用的繳交方式（onlinetext / file）；沒有 configs 時回傳 None（未知）。"""
    configs = assignment.get("configs")
    if not isinstance(configs, list):
        return None
    enabled: set[str] = set()
    for config in configs:
        if (
            isinstance(config, dict)
            and config.get("subtype") == "assignsubmission"
            and config.get("name") == "enabled"
            and str(config.get("value")) == "1"
        ):
            enabled.add(str(config.get("plugin")))
    return enabled


def _saves_as_final(assignment: dict[str, Any]) -> bool:
    """submissiondrafts=0 代表沒有「送出評分」步驟，儲存即繳交。欄位缺漏時保守視為會直接繳交。"""
    value = assignment.get("submissiondrafts")
    return value is None or not parse_int(value)


def _assignment_header(entry: dict[str, Any], assign_id: int, status: dict[str, Any] | None) -> list[str]:
    assignment = entry["assignment"]
    return [
        f"- 作業：**{assignment.get('name') or '（未命名作業）'}**（{course_label(entry['course'])}）",
        f"- assign_id：{assign_id}",
        f"- 截止日：{format_ts(assignment.get('duedate'), empty='無截止日')}",
        f"- 目前狀態：{_status_text(status)}",
    ]


@tool_guard
async def save_assignment_draft(
    assign_id: int,
    text: str | None = None,
    files: list[str] | None = None,
    confirm_code: str = "",
    ctx: Context | None = None,
) -> str:
    """儲存作業的繳交內容（線上文字及 / 或上傳檔案）。只在使用者明確要求時使用。

    會先顯示預覽並請使用者確認，確認後才真的儲存。
    若作業有「送出評分」步驟，這裡只會存成草稿；若作業沒有這個步驟，儲存就等同繳交
    （此時還需要開啟 MOODLE_ALLOW_SUBMIT_FOR_GRADING）。

    Args:
        assign_id: 作業 id（由 list_assignments 取得）。
        text: 線上文字內容（純文字，會轉成 HTML）；會取代目前的線上文字。
        files: 要上傳的檔案路徑，必須在 MOODLE_UPLOAD_DIR 資料夾內（可用相對路徑）；
            會取代目前草稿中的所有檔案。
        confirm_code: 用戶端不支援確認視窗時，預覽會給一組確認碼；
            **使用者同意後**才帶著相同參數與確認碼再呼叫一次。

    Returns:
        預覽（等待確認）或執行結果。
    """
    _require("save_submission")
    text = (text or "").strip()
    files = [f for f in (files or []) if str(f).strip()]
    if not text and not files:
        return "⚠️ 請提供 text（線上文字）或 files（要上傳的檔案）至少一項。"

    entry = await _find_assignment(int(assign_id))
    assignment = entry["assignment"]
    settings = get_settings()

    final = _saves_as_final(assignment)
    if final and not settings.write_enabled("submit_for_grading"):
        return (
            "⚠️ 這份作業沒有「送出評分」步驟，儲存後就等同正式繳交。"
            f"因此除了 {write_env_name('save_submission')}，還需要開啟 "
            f"{write_env_name('submit_for_grading')}=true 才能使用。沒有做任何變更。"
        )

    plugins = _enabled_submission_plugins(assignment)
    if plugins is not None:
        if text and "onlinetext" not in plugins:
            return "⚠️ 這份作業沒有開放「線上文字」繳交，沒有做任何變更。"
        if files and "file" not in plugins:
            return "⚠️ 這份作業沒有開放「檔案」繳交，沒有做任何變更。"

    resolved: list[Path] = []
    for raw in files:
        path = resolve_upload_path(raw)
        if path not in resolved:
            resolved.append(path)

    status = await _submission_status(int(assign_id), fresh=True)
    lines = ["# 確認：儲存作業繳交內容", "", *_assignment_header(entry, int(assign_id), status)]
    if text:
        lines.extend(["", "## 線上文字（會取代目前的內容）", "", excerpt(text)])
    if resolved:
        lines.extend(["", "## 上傳檔案（會取代目前草稿中的所有檔案）", ""])
        lines.extend(f"- {p.name}（{human_size(p.stat().st_size)}）" for p in resolved)
    lines.append("")
    if final:
        lines.append("⚠️ **這份作業沒有「送出評分」步驟：儲存後就等同正式繳交。**")
    else:
        lines.append("只會存成草稿，不會送出評分。")

    params = {
        "assign_id": int(assign_id),
        "text": text,
        "files": [[str(p), p.stat().st_size, p.stat().st_mtime_ns] for p in resolved],
    }
    pending = await confirm_action(
        ctx,
        action="save_assignment_draft",
        params=params,
        preview="\n".join(lines),
        confirm_code=confirm_code,
    )
    if pending is not None:
        return pending

    client = get_client()
    plugindata: dict[str, Any] = {}
    if resolved:
        plugindata["files_filemanager"] = await client.upload_draft_files(resolved)
    if text:
        plugindata["onlinetext_editor"] = {"text": text_to_html(text), "format": 1, "itemid": 0}
    result = await client.write(
        "mod_assign_save_submission", assignmentid=int(assign_id), plugindata=plugindata
    )
    messages = _warnings(result)
    if messages:
        return _warning_reply("儲存作業", messages)
    done = "已儲存並繳交" if final else "已儲存為草稿（尚未送出評分）"
    return f"✅ {done}：{assignment.get('name') or assign_id}"


def _submission_summary(status: dict[str, Any] | None) -> list[str]:
    submission = ((status or {}).get("lastattempt") or {}).get("submission") or {}
    lines: list[str] = []
    for plugin in submission.get("plugins") or []:
        if not isinstance(plugin, dict):
            continue
        for area in plugin.get("fileareas") or []:
            for file in (area or {}).get("files") or []:
                if isinstance(file, dict) and file.get("filename"):
                    lines.append(f"- 📎 {file['filename']}（{human_size(file.get('filesize'))}）")
        for field in plugin.get("editorfields") or []:
            text = html_to_plain((field or {}).get("text") or "", limit=300)
            if text:
                lines.append(f"- 線上文字：{text}")
    return lines


@tool_guard
async def submit_assignment_for_grading(
    assign_id: int,
    accept_statement: bool = False,
    confirm_code: str = "",
    ctx: Context | None = None,
) -> str:
    """把作業草稿正式送出評分。送出後通常就不能再修改。只在使用者明確要求時使用。

    會先顯示預覽並請使用者確認，確認後才真的送出。

    Args:
        assign_id: 作業 id（由 list_assignments 取得）。
        accept_statement: 作業要求同意「繳交聲明」時必須為 True。
            **只有使用者本人讀過並明確同意聲明時才能設為 True，不可代為同意。**
        confirm_code: 用戶端不支援確認視窗時，預覽會給一組確認碼；
            **使用者同意後**才帶著相同參數與確認碼再呼叫一次。

    Returns:
        預覽（等待確認）或執行結果。
    """
    _require("submit_for_grading")
    entry = await _find_assignment(int(assign_id))
    assignment = entry["assignment"]

    if _saves_as_final(assignment) and assignment.get("submissiondrafts") is not None:
        return "這份作業沒有「送出評分」步驟，儲存就算繳交，不需要再送出。沒有做任何變更。"

    status = await _submission_status(int(assign_id), fresh=True)
    if status is None:
        return "⚠️ 無法取得這份作業的繳交狀態，為了安全起見不送出。沒有做任何變更。"
    submission = (status.get("lastattempt") or {}).get("submission") or {}
    state = str(submission.get("status") or "")
    if state == "submitted":
        return "這份作業已經送出評分了，沒有做任何變更。"
    if state != "draft":
        return (
            f"目前沒有可以送出的草稿（狀態：{state or '未繳'}）。"
            "請先用 save_assignment_draft 儲存內容。沒有做任何變更。"
        )

    statement = html_to_plain(str(assignment.get("submissionstatement") or ""))
    if parse_int(assignment.get("requiresubmissionstatement")) and not accept_statement:
        return (
            "⚠️ 這份作業要求同意繳交聲明才能送出。請把聲明完整給使用者看，"
            "**由使用者本人同意後**，再以 accept_statement=True 呼叫。沒有做任何變更。"
            + (f"\n\n> {statement}" if statement else "")
        )

    lines = ["# 確認：送出作業評分", "", *_assignment_header(entry, int(assign_id), status)]
    contents = _submission_summary(status)
    lines.extend(["", "## 草稿內容", "", *(contents or ["（無法取得草稿內容明細）"])])
    if accept_statement:
        lines.extend(["", "## 繳交聲明（使用者已同意）", "", f"> {statement or '（未提供聲明內容）'}"])
    lines.extend(["", "⚠️ **送出後通常就不能再修改，除非老師退回。**"])

    pending = await confirm_action(
        ctx,
        action="submit_assignment_for_grading",
        params={"assign_id": int(assign_id), "accept_statement": bool(accept_statement)},
        preview="\n".join(lines),
        confirm_code=confirm_code,
    )
    if pending is not None:
        return pending

    result = await get_client().write(
        "mod_assign_submit_for_grading",
        assignmentid=int(assign_id),
        acceptsubmissionstatement=bool(accept_statement),
    )
    messages = _warnings(result)
    if messages:
        return _warning_reply("送出評分", messages)
    return f"✅ 已送出評分：{assignment.get('name') or assign_id}"


# ---------------------------------------------------------------------------
# 討論區
# ---------------------------------------------------------------------------


@tool_guard
async def reply_to_discussion(
    discussion_id: int,
    message: str,
    post_id: int | None = None,
    subject: str = "",
    confirm_code: str = "",
    ctx: Context | None = None,
) -> str:
    """在討論串中回覆。老師和同學都看得到。只在使用者明確要求時使用。

    會先顯示預覽並請使用者確認，確認後才真的發文。

    Args:
        discussion_id: 討論串 id（由 list_announcements 或 get_discussion 取得）。
        message: 回覆內容（純文字，會轉成 HTML）。
        post_id: 要回覆的貼文 id（由 get_discussion 取得）；省略時回覆討論串的第一則貼文。
        subject: 回覆標題；省略時用「Re: 原標題」。
        confirm_code: 用戶端不支援確認視窗時，預覽會給一組確認碼；
            **使用者同意後**才帶著相同參數與確認碼再呼叫一次。

    Returns:
        預覽（等待確認）或執行結果（含新貼文的 post_id）。
    """
    _require("forum_reply")
    message = (message or "").strip()
    if not message:
        return "⚠️ 回覆內容不可為空。"

    data = await get_client().call(
        "mod_forum_get_discussion_posts", discussionid=int(discussion_id), use_cache=False
    )
    posts = data.get("posts") if isinstance(data, dict) else data
    posts = sorted(
        (p for p in (posts or []) if isinstance(p, dict)),
        key=lambda p: parse_int(p.get("created") or p.get("timecreated")),
    )
    if not posts:
        raise NotFoundError(f"找不到 discussion_id={discussion_id} 的貼文")
    if post_id is None:
        target = posts[0]
    else:
        target = next((p for p in posts if parse_int(p.get("id")) == int(post_id)), None)
        if target is None:
            raise NotFoundError(f"討論串 {discussion_id} 中沒有 post_id={post_id}")

    target_id = parse_int(target.get("id"))
    parent_subject = html_to_plain(str(target.get("subject") or ""))
    subject = (subject or "").strip() or str(target.get("replysubject") or "").strip()
    if not subject:
        subject = parent_subject if parent_subject.lower().startswith("re:") else f"Re: {parent_subject}"

    author = (target.get("author") or {}).get("fullname") or "（未知作者）"
    lines = [
        "# 確認：回覆討論串（老師和同學都看得到）",
        "",
        f"- discussion_id：{discussion_id}　回覆 post_id：{target_id}",
        f"- 回覆對象：{author}（{format_ts(target.get('created') or target.get('timecreated'))}）",
        f"> {html_to_plain(target.get('message') or '', limit=300)}",
        "",
        f"- 標題：{subject}",
        "",
        "## 回覆內容",
        "",
        excerpt(message),
    ]
    pending = await confirm_action(
        ctx,
        action="reply_to_discussion",
        params={"post_id": target_id, "subject": subject, "message": message},
        preview="\n".join(lines),
        confirm_code=confirm_code,
    )
    if pending is not None:
        return pending

    result = await get_client().write(
        "mod_forum_add_discussion_post",
        postid=target_id,
        subject=subject,
        message=text_to_html(message),
    )
    messages = _warnings(result)
    new_id = result.get("postid") if isinstance(result, dict) else None
    if messages and not new_id:
        return _warning_reply("回覆討論串", messages)
    return f"✅ 已回覆討論串 {discussion_id}（新貼文 post_id：{new_id or '未知'}）。"


@tool_guard
async def create_discussion(
    forum_id: int,
    subject: str,
    message: str,
    confirm_code: str = "",
    ctx: Context | None = None,
) -> str:
    """在討論區發一則新討論。老師和同學都看得到。只在使用者明確要求時使用。

    會先顯示預覽並請使用者確認，確認後才真的發文。

    Args:
        forum_id: 討論區 id（由 list_forums 取得）。
        subject: 標題。
        message: 內容（純文字，會轉成 HTML）。
        confirm_code: 用戶端不支援確認視窗時，預覽會給一組確認碼；
            **使用者同意後**才帶著相同參數與確認碼再呼叫一次。

    Returns:
        預覽（等待確認）或執行結果（含新討論串的 discussion_id）。
    """
    _require("forum_post")
    subject = (subject or "").strip()
    message = (message or "").strip()
    if not subject or not message:
        return "⚠️ 標題和內容都不可為空。"

    forum = await find_forum(int(forum_id))
    if forum is None:
        raise NotFoundError(
            f"在你的課程中找不到 forum_id={forum_id}，請先用 list_forums 取得正確的 id。"
        )

    lines = [
        "# 確認：發新討論（老師和同學都看得到）",
        "",
        f"- 討論區：**{html_to_plain(str(forum.get('name') or ''))}**（forum_id：{int(forum_id)}，"
        f"course_id：{forum.get('course')}）",
        f"- 標題：{subject}",
        "",
        "## 內容",
        "",
        excerpt(message),
    ]
    if str(forum.get("type") or "") == "news":
        lines.extend(["", "注意：這是公告區，通常只有老師能發文，Moodle 可能會拒絕。"])

    pending = await confirm_action(
        ctx,
        action="create_discussion",
        params={"forum_id": int(forum_id), "subject": subject, "message": message},
        preview="\n".join(lines),
        confirm_code=confirm_code,
    )
    if pending is not None:
        return pending

    result = await get_client().write(
        "mod_forum_add_discussion",
        forumid=int(forum_id),
        subject=subject,
        message=text_to_html(message),
    )
    messages = _warnings(result)
    new_id = result.get("discussionid") if isinstance(result, dict) else None
    if messages and not new_id:
        return _warning_reply("發新討論", messages)
    return f"✅ 已發出新討論（discussion_id：{new_id or '未知'}）。"


# ---------------------------------------------------------------------------
# 註冊
# ---------------------------------------------------------------------------

# tool -> (寫入功能, annotations)
WRITE_TOOLS: tuple[tuple[Any, str, ToolAnnotations], ...] = (
    (
        mark_notifications_read,
        "mark_read",
        ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    ),
    (
        save_assignment_draft,
        "save_submission",
        ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    ),
    (
        submit_assignment_for_grading,
        "submit_for_grading",
        ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    ),
    (
        reply_to_discussion,
        "forum_reply",
        ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    ),
    (
        create_discussion,
        "forum_post",
        ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    ),
)


def register(mcp: Any) -> None:
    """只註冊設定中已開啟的寫入 tool；沒開的 LLM 完全看不到。"""
    settings = get_settings()
    for tool, feature, hints in WRITE_TOOLS:
        if settings.write_enabled(feature):
            mcp.add_tool(tool, annotations=hints)


__all__ = [
    "WRITE_TOOLS",
    "create_discussion",
    "mark_notifications_read",
    "register",
    "reply_to_discussion",
    "save_assignment_draft",
    "submit_assignment_for_grading",
]
