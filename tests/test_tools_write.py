"""寫入功能測試：獨立開關、確認流程、不重試、上傳路徑限制。"""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from conftest import REST_URL, SITE_INFO, load_fixture

from moodle_mcp import client as client_module
from moodle_mcp.cache import get_cache
from moodle_mcp.client import ALLOWED_FUNCTIONS, WRITE_FUNCTIONS, MoodleClient
from moodle_mcp.config import WRITE_FEATURES, reload_settings, write_env_name
from moodle_mcp.errors import FunctionNotAllowed, NotFoundError, PermissionError_
from moodle_mcp.write_guard import (
    reset_pending,
    resolve_upload_path,
    text_to_html,
)

UPLOAD_URL = "https://moodle.example.edu/webservice/upload.php"


@pytest.fixture(autouse=True)
def _clean_pending():
    reset_pending()
    yield
    reset_pending()


def enable(monkeypatch, *features: str, upload_dir: str | None = None) -> None:
    for feature in features:
        monkeypatch.setenv(write_env_name(feature), "true")
    if upload_dir is not None:
        monkeypatch.setenv("MOODLE_UPLOAD_DIR", upload_dir)
    reload_settings()
    client_module.reset_client()


@pytest.fixture
def wapi(api):
    """site_info 的 functions 加上寫入函式，並準備讀取用的 fixtures。"""
    info = load_fixture("site_info.json")
    info["functions"] = [*info["functions"], *({"name": n, "version": "4.3"} for n in WRITE_FUNCTIONS)]
    api.on(SITE_INFO, info)
    api.on(
        "core_course_get_enrolled_courses_by_timeline_classification",
        load_fixture("courses_timeline.json"),
    )
    api.on("mod_assign_get_assignments", load_fixture("assignments.json"))
    api.on("mod_forum_get_forums_by_courses", load_fixture("forums.json"))
    api.on("mod_forum_get_discussion_posts", load_fixture("discussion_posts.json"))
    return api


def draft_status(**submission: Any) -> dict[str, Any]:
    base = {"id": 8001, "assignment": 201, "status": "draft", "plugins": [
        {"type": "file", "fileareas": [{"area": "submission_files", "files": [
            {"filename": "hw1.pdf", "filesize": 2048}
        ]}]},
    ]}
    base.update(submission)
    return {"lastattempt": {"submission": base}, "submissionstatus": base["status"]}


def code_from(preview: str) -> str:
    match = re.search(r'confirm_code="([0-9a-f]+)"', preview)
    assert match, preview
    return match.group(1)


# ---------------------------------------------------------------------------
# 設定與 client
# ---------------------------------------------------------------------------


def test_every_write_function_maps_to_a_feature():
    assert set(WRITE_FUNCTIONS.values()) <= set(WRITE_FEATURES)
    # 寫入函式絕不能出現在唯讀白名單
    assert not set(WRITE_FUNCTIONS) & ALLOWED_FUNCTIONS


def test_write_features_default_off():
    settings = reload_settings()
    assert settings.enabled_write_features() == []


def test_each_feature_is_independent(monkeypatch):
    enable(monkeypatch, "forum_reply")
    settings = reload_settings()
    assert settings.enabled_write_features() == ["forum_reply"]
    assert not settings.write_enabled("forum_post")
    assert not settings.write_enabled("submit_for_grading")


@pytest.mark.asyncio
async def test_write_rejected_when_feature_off(wapi):
    client = MoodleClient()
    with pytest.raises(FunctionNotAllowed, match=write_env_name("forum_post")):
        await client.write("mod_forum_add_discussion", forumid=1, subject="x", message="y")
    assert wapi.count("mod_forum_add_discussion") == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_call_never_runs_write_functions(wapi, monkeypatch):
    enable(monkeypatch, "forum_post")
    client = MoodleClient()
    with pytest.raises(FunctionNotAllowed):
        await client.call("mod_forum_add_discussion", forumid=1)
    assert wapi.count("mod_forum_add_discussion") == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_write_unknown_function_rejected(wapi, monkeypatch):
    enable(monkeypatch, *WRITE_FEATURES)
    client = MoodleClient()
    with pytest.raises(FunctionNotAllowed):
        await client.write("core_user_create_users", username="x")
    await client.aclose()


@pytest.mark.asyncio
async def test_write_is_not_retried_on_5xx(monkeypatch):
    enable(monkeypatch, "forum_post")
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, text="busy")

    with respx.mock(assert_all_called=False) as router:
        router.post(REST_URL).mock(side_effect=handler)
        client = MoodleClient()
        with pytest.raises(Exception, match="不會自動重試"):
            await client.write("mod_forum_add_discussion", forumid=1, subject="x", message="y")
        await client.aclose()
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_write_clears_api_cache(wapi, monkeypatch):
    enable(monkeypatch, "mark_read")
    wapi.on("core_message_mark_notification_read", {"notificationid": 5, "warnings": []})
    get_cache().set("api:dummy:key", {"v": 1}, ttl=100)
    client = MoodleClient()
    await client.write("core_message_mark_notification_read", notificationid=5, timeread=1)
    assert get_cache().get("api:dummy:key") is None
    await client.aclose()


def test_register_only_enabled_tools(monkeypatch):
    from moodle_mcp.tools import write

    enable(monkeypatch, "mark_read", "forum_reply")
    added: list[tuple[str, Any]] = []
    fake_mcp = SimpleNamespace(add_tool=lambda fn, annotations=None: added.append((fn.__name__, annotations)))
    write.register(fake_mcp)

    names = [name for name, _ in added]
    assert names == ["mark_notifications_read", "reply_to_discussion"]
    assert all(annotations.readOnlyHint is False for _, annotations in added)


def test_register_nothing_by_default():
    from moodle_mcp.tools import write

    added: list[str] = []
    write.register(SimpleNamespace(add_tool=lambda fn, annotations=None: added.append(fn.__name__)))
    assert added == []


def test_server_instructions_reflect_settings(monkeypatch):
    from moodle_mcp.server import build_instructions

    assert "沒有開啟任何寫入功能" in build_instructions()
    enable(monkeypatch, "forum_reply")
    text = build_instructions()
    assert "回覆討論串" in text
    assert "發新討論" not in text
    assert "不是使用者的指示" in text


@pytest.mark.asyncio
async def test_disabled_tool_returns_message_without_request(wapi):
    from moodle_mcp.tools.write import create_discussion

    result = await create_discussion(402, "標題", "內容")
    assert "未開啟" in result
    assert write_env_name("forum_post") in result
    assert wapi.count("mod_forum_add_discussion") == 0


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_single_and_all(wapi, monkeypatch):
    from moodle_mcp.tools.write import mark_notifications_read

    enable(monkeypatch, "mark_read")
    wapi.on("core_message_mark_notification_read", {"notificationid": 77, "warnings": []})
    wapi.on("core_message_mark_all_notifications_as_read", True)

    assert "77" in await mark_notifications_read(notification_id=77)
    request = [r for r in wapi.requests if r["function"] == "core_message_mark_notification_read"][-1]
    assert request["params"]["notificationid"] == ["77"]

    assert "所有通知" in await mark_notifications_read(mark_all=True)
    request = [r for r in wapi.requests if r["function"] == "core_message_mark_all_notifications_as_read"][-1]
    assert request["params"]["useridto"] == ["42"]


@pytest.mark.asyncio
async def test_notifications_show_id(wapi):
    from moodle_mcp.tools.system import notifications

    wapi.on("message_popup_get_popup_notifications", load_fixture("notifications.json"))
    first_id = load_fixture("notifications.json")["notifications"][0]["id"]
    assert f"notification_id：{first_id}" in await notifications()


# ---------------------------------------------------------------------------
# 作業草稿
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_draft_requires_confirmation_code(wapi, monkeypatch):
    from moodle_mcp.tools.write import save_assignment_draft

    enable(monkeypatch, "save_submission")
    wapi.on("mod_assign_get_submission_status", draft_status(status="new", plugins=[]))
    wapi.on("mod_assign_save_submission", [])

    preview = await save_assignment_draft(201, text="我的答案 <b>")
    assert "尚未執行" in preview
    assert "只會存成草稿" in preview
    assert wapi.count("mod_assign_save_submission") == 0

    code = code_from(preview)
    # 參數不同 → 確認碼無效，而且已被用掉
    changed = await save_assignment_draft(201, text="別的答案", confirm_code=code)
    assert "confirm_code 無效" in changed
    assert wapi.count("mod_assign_save_submission") == 0
    reused = await save_assignment_draft(201, text="我的答案 <b>", confirm_code=code)
    assert "confirm_code 無效" in reused

    code = code_from(await save_assignment_draft(201, text="我的答案 <b>"))
    done = await save_assignment_draft(201, text="我的答案 <b>", confirm_code=code)
    assert "已儲存為草稿" in done
    request = [r for r in wapi.requests if r["function"] == "mod_assign_save_submission"][-1]
    assert request["params"]["assignmentid"] == ["201"]
    assert request["params"]["plugindata[onlinetext_editor][text]"] == ["<p>我的答案 &lt;b&gt;</p>"]
    assert request["params"]["plugindata[onlinetext_editor][format]"] == ["1"]


@pytest.mark.asyncio
async def test_save_draft_uploads_files(wapi, monkeypatch, tmp_path):
    from moodle_mcp.tools.write import save_assignment_draft

    upload_dir = tmp_path / "hand-in"
    upload_dir.mkdir()
    (upload_dir / "hw1.pdf").write_bytes(b"%PDF-1.4 fake")
    enable(monkeypatch, "save_submission", upload_dir=str(upload_dir))
    wapi.on("mod_assign_get_submission_status", draft_status(status="new", plugins=[]))
    wapi.on("mod_assign_save_submission", [])

    seen: dict[str, bytes] = {}

    def upload(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json=[{"itemid": 555, "filename": "hw1.pdf"}])

    wapi.router.post(UPLOAD_URL).mock(side_effect=upload)

    preview = await save_assignment_draft(201, files=["hw1.pdf"])
    assert "hw1.pdf" in preview
    assert "body" not in seen  # 預覽階段不上傳

    result = await save_assignment_draft(201, files=["hw1.pdf"], confirm_code=code_from(preview))
    assert "已儲存為草稿" in result
    assert b"test-token-123" in seen["body"]
    assert b'filename="hw1.pdf"' in seen["body"]
    assert b"%PDF-1.4 fake" in seen["body"]
    request = [r for r in wapi.requests if r["function"] == "mod_assign_save_submission"][-1]
    assert request["params"]["plugindata[files_filemanager]"] == ["555"]
    assert "test-token-123" not in result


@pytest.mark.asyncio
async def test_save_draft_without_draft_step_needs_submit_flag(wapi, monkeypatch):
    from moodle_mcp.tools.write import save_assignment_draft

    wapi.on("mod_assign_get_submission_status", draft_status(status="new", plugins=[]))
    enable(monkeypatch, "save_submission")
    # 作業 202 沒有 submissiondrafts → 儲存即繳交
    refused = await save_assignment_draft(202, text="答案")
    assert write_env_name("submit_for_grading") in refused
    assert "尚未執行" not in refused

    enable(monkeypatch, "save_submission", "submit_for_grading")
    preview = await save_assignment_draft(202, text="答案")
    assert "等同正式繳交" in preview


@pytest.mark.asyncio
async def test_save_draft_warnings_reported(wapi, monkeypatch):
    from moodle_mcp.tools.write import save_assignment_draft

    enable(monkeypatch, "save_submission")
    wapi.on("mod_assign_get_submission_status", draft_status(status="new", plugins=[]))
    wapi.on("mod_assign_save_submission", [{"item": "assign", "warningcode": "couldnotsavesubmission",
                                            "message": "Could not save submission."}])
    code = code_from(await save_assignment_draft(201, text="x"))
    result = await save_assignment_draft(201, text="x", confirm_code=code)
    assert "沒有完成" in result
    assert "Could not save submission." in result


# ---------------------------------------------------------------------------
# 上傳路徑
# ---------------------------------------------------------------------------


def test_upload_requires_upload_dir(tmp_path):
    with pytest.raises(PermissionError_, match="MOODLE_UPLOAD_DIR"):
        resolve_upload_path(str(tmp_path / "a.txt"))


def test_upload_path_restrictions(tmp_path, monkeypatch):
    base = tmp_path / "hand-in"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "ok.txt").write_text("ok")
    (base / ".env").write_text("MOODLE_TOKEN=x")
    (base / ".git").mkdir()
    (base / ".git" / "config").write_text("x")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    monkeypatch.setenv("MOODLE_UPLOAD_DIR", str(base))
    reload_settings()

    assert resolve_upload_path("sub/ok.txt") == (base / "sub" / "ok.txt").resolve()
    assert resolve_upload_path(str(base / "sub" / "ok.txt")).name == "ok.txt"

    with pytest.raises(PermissionError_):
        resolve_upload_path(str(secret))
    with pytest.raises(PermissionError_):
        resolve_upload_path("../secret.txt")
    with pytest.raises(PermissionError_):
        resolve_upload_path(".env")
    with pytest.raises(PermissionError_):
        resolve_upload_path(".git/config")
    with pytest.raises(NotFoundError):
        resolve_upload_path("missing.txt")
    with pytest.raises(NotFoundError):
        resolve_upload_path("sub")


@pytest.mark.skipif(os.name == "nt", reason="Windows 建立 symlink 需要額外權限")
def test_upload_rejects_symlink_escape(tmp_path, monkeypatch):
    base = tmp_path / "hand-in"
    base.mkdir()
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    (base / "innocent.pdf").symlink_to(secret)
    monkeypatch.setenv("MOODLE_UPLOAD_DIR", str(base))
    reload_settings()
    with pytest.raises(PermissionError_):
        resolve_upload_path("innocent.pdf")


def test_upload_rejects_too_large(tmp_path, monkeypatch):
    base = tmp_path / "hand-in"
    base.mkdir()
    (base / "big.bin").write_bytes(b"0" * (2 * 1024 * 1024))
    monkeypatch.setenv("MOODLE_UPLOAD_DIR", str(base))
    monkeypatch.setenv("MOODLE_MAX_FILE_MB", "1")
    reload_settings()
    with pytest.raises(PermissionError_, match="超過上限"):
        resolve_upload_path("big.bin")


def test_text_to_html_escapes():
    assert text_to_html("a <script>\nb\n\nc & d") == "<p>a &lt;script&gt;<br>b</p><p>c &amp; d</p>"


# ---------------------------------------------------------------------------
# 送出評分
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_for_grading_flow(wapi, monkeypatch):
    from moodle_mcp.tools.write import submit_assignment_for_grading

    enable(monkeypatch, "submit_for_grading")
    wapi.on("mod_assign_get_submission_status", draft_status())
    wapi.on("mod_assign_submit_for_grading", [])

    preview = await submit_assignment_for_grading(201)
    assert "hw1.pdf" in preview
    assert "不能再修改" in preview
    assert wapi.count("mod_assign_submit_for_grading") == 0

    result = await submit_assignment_for_grading(201, confirm_code=code_from(preview))
    assert "已送出評分" in result
    request = [r for r in wapi.requests if r["function"] == "mod_assign_submit_for_grading"][-1]
    assert request["params"]["assignmentid"] == ["201"]
    assert request["params"]["acceptsubmissionstatement"] == ["0"]


@pytest.mark.asyncio
async def test_submit_for_grading_noop_states(wapi, monkeypatch):
    from moodle_mcp.tools.write import submit_assignment_for_grading

    enable(monkeypatch, "submit_for_grading")
    wapi.on("mod_assign_get_submission_status", draft_status(status="submitted"))
    assert "已經送出" in await submit_assignment_for_grading(201)

    wapi.on("mod_assign_get_submission_status", draft_status(status="new"))
    assert "沒有可以送出的草稿" in await submit_assignment_for_grading(201)
    assert wapi.count("mod_assign_submit_for_grading") == 0


@pytest.mark.asyncio
async def test_submit_requires_statement(wapi, monkeypatch):
    from moodle_mcp.tools.write import submit_assignment_for_grading

    enable(monkeypatch, "submit_for_grading")
    data = load_fixture("assignments.json")
    data["courses"][0]["assignments"][0].update(
        requiresubmissionstatement=1, submissionstatement="<p>這是我自己完成的作業。</p>"
    )
    wapi.on("mod_assign_get_assignments", data)
    wapi.on("mod_assign_get_submission_status", draft_status())
    wapi.on("mod_assign_submit_for_grading", [])

    result = await submit_assignment_for_grading(201)
    assert "繳交聲明" in result
    assert "這是我自己完成的作業" in result
    assert "confirm_code" not in result

    preview = await submit_assignment_for_grading(201, accept_statement=True)
    await submit_assignment_for_grading(201, accept_statement=True, confirm_code=code_from(preview))
    request = [r for r in wapi.requests if r["function"] == "mod_assign_submit_for_grading"][-1]
    assert request["params"]["acceptsubmissionstatement"] == ["1"]


# ---------------------------------------------------------------------------
# 討論區
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_to_discussion(wapi, monkeypatch):
    from moodle_mcp.tools.write import reply_to_discussion

    enable(monkeypatch, "forum_reply")
    wapi.on("mod_forum_add_discussion_post", {"postid": 999, "warnings": []})

    preview = await reply_to_discussion(501, "謝謝老師！")
    assert "Re: 期中考範圍" in preview
    assert "回覆 post_id：601" in preview
    assert wapi.count("mod_forum_add_discussion_post") == 0

    result = await reply_to_discussion(501, "謝謝老師！", confirm_code=code_from(preview))
    assert "999" in result
    request = [r for r in wapi.requests if r["function"] == "mod_forum_add_discussion_post"][-1]
    assert request["params"]["postid"] == ["601"]
    assert request["params"]["subject"] == ["Re: 期中考範圍"]
    assert request["params"]["message"] == ["<p>謝謝老師！</p>"]


@pytest.mark.asyncio
async def test_reply_to_unknown_post(wapi, monkeypatch):
    from moodle_mcp.tools.write import reply_to_discussion

    enable(monkeypatch, "forum_reply")
    assert "沒有 post_id=12345" in await reply_to_discussion(501, "hi", post_id=12345)


@pytest.mark.asyncio
async def test_create_discussion(wapi, monkeypatch):
    from moodle_mcp.tools.write import create_discussion

    enable(monkeypatch, "forum_post")
    wapi.on("mod_forum_add_discussion", {"discussionid": 888, "warnings": []})

    preview = await create_discussion(402, "請問作業 1", "第 2 題的輸入格式是？")
    assert "一般討論區" in preview
    result = await create_discussion(
        402, "請問作業 1", "第 2 題的輸入格式是？", confirm_code=code_from(preview)
    )
    assert "888" in result
    request = [r for r in wapi.requests if r["function"] == "mod_forum_add_discussion"][-1]
    assert request["params"]["forumid"] == ["402"]
    assert request["params"]["subject"] == ["請問作業 1"]

    assert "找不到 forum_id=999999" in await create_discussion(999999, "a", "b")


@pytest.mark.asyncio
async def test_list_forums_and_post_ids(wapi):
    from moodle_mcp.tools.forums import get_discussion, list_forums

    forums = await list_forums(101)
    assert "402" in forums and "一般討論區" in forums
    assert "403" not in forums  # 別門課的討論區
    assert "post_id：601" in await get_discussion(501)


# ---------------------------------------------------------------------------
# elicitation（用戶端直接問使用者）
# ---------------------------------------------------------------------------


class FakeCtx:
    def __init__(self, action: str, confirm: bool) -> None:
        self.session = SimpleNamespace(check_client_capability=lambda _caps: True)
        self._result = SimpleNamespace(action=action, data=SimpleNamespace(confirm=confirm))
        self.messages: list[str] = []

    async def elicit(self, message: str, schema: Any) -> Any:
        self.messages.append(message)
        return self._result


@pytest.mark.asyncio
async def test_elicitation_accept_runs_without_code(wapi, monkeypatch):
    from moodle_mcp.tools.write import create_discussion

    enable(monkeypatch, "forum_post")
    wapi.on("mod_forum_add_discussion", {"discussionid": 777, "warnings": []})
    ctx = FakeCtx("accept", True)
    result = await create_discussion(402, "標題", "內容", ctx=ctx)
    assert "777" in result
    assert "一般討論區" in ctx.messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "confirm"), [("decline", False), ("cancel", False), ("accept", False)])
async def test_elicitation_decline_does_nothing(wapi, monkeypatch, action, confirm):
    from moodle_mcp.tools.write import create_discussion

    enable(monkeypatch, "forum_post")
    wapi.on("mod_forum_add_discussion", {"discussionid": 777, "warnings": []})
    result = await create_discussion(402, "標題", "內容", ctx=FakeCtx(action, confirm))
    assert "已取消" in result
    assert wapi.count("mod_forum_add_discussion") == 0


@pytest.mark.asyncio
async def test_elicitation_ignores_confirm_code(wapi, monkeypatch):
    """用戶端支援確認視窗時，LLM 給的 confirm_code 沒有用，一定要使用者按同意。"""
    from moodle_mcp.tools.write import create_discussion

    enable(monkeypatch, "forum_post")
    wapi.on("mod_forum_add_discussion", {"discussionid": 777, "warnings": []})
    code = code_from(await create_discussion(402, "標題", "內容"))
    result = await create_discussion(402, "標題", "內容", confirm_code=code, ctx=FakeCtx("decline", False))
    assert "已取消" in result
    assert wapi.count("mod_forum_add_discussion") == 0
