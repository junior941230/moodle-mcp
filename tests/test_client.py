"""MoodleClient：參數扁平化、白名單、錯誤對應、重試、快取。"""

from __future__ import annotations

import httpx
import pytest
from conftest import load_fixture

import moodle_mcp.client as client_module
from moodle_mcp.cache import make_key
from moodle_mcp.client import ALLOWED_FUNCTIONS, MoodleClient, flatten_params
from moodle_mcp.config import get_settings
from moodle_mcp.errors import (
    AuthError,
    FunctionNotAllowed,
    FunctionUnavailable,
    MoodleError,
    NotFoundError,
    PermissionError_,
    error_from_payload,
)


def test_flatten_params_nested_list_and_bool():
    params = {
        "courseids": [3, 5],
        "options": [{"name": "x", "value": True}, {"name": "y", "value": False}],
        "single": 1,
        "text": "hello",
        "skip": None,
    }
    assert flatten_params(params) == [
        ("courseids[0]", "3"),
        ("courseids[1]", "5"),
        ("options[0][name]", "x"),
        ("options[0][value]", "1"),
        ("options[1][name]", "y"),
        ("options[1][value]", "0"),
        ("single", "1"),
        ("text", "hello"),
    ]


def test_flatten_params_empty():
    assert flatten_params({}) == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"errorcode": "invalidtoken", "message": "bad"}, AuthError),
        (
            {
                "errorcode": "accessexception",
                "message": "Function core_course_get_contents is not available",
            },
            FunctionUnavailable,
        ),
        ({"errorcode": "nopermissions", "message": "no"}, PermissionError_),
        ({"errorcode": "requireloginerror", "message": "login"}, PermissionError_),
        ({"errorcode": "invalidparameter", "message": "bad param"}, NotFoundError),
        ({"errorcode": "invalidrecord", "message": "missing"}, NotFoundError),
        ({"errorcode": "servicenotavailable", "message": "no"}, FunctionUnavailable),
        ({"errorcode": "somethingelse", "message": "boom"}, MoodleError),
    ],
)
def test_error_mapping(payload, expected):
    assert isinstance(error_from_payload(payload), expected)


@pytest.mark.asyncio
async def test_whitelist_rejects_write_functions(api):
    client = MoodleClient()
    with pytest.raises(FunctionNotAllowed):
        await client.call("core_user_create_users", username="x")
    # 沒有發出任何 HTTP 請求
    assert all(request["function"] != "core_user_create_users" for request in api.requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_whitelist_contains_only_read_functions():
    assert "core_course_get_contents" in ALLOWED_FUNCTIONS
    for forbidden in (
        "core_user_create_users",
        "mod_assign_save_submission",
        "mod_forum_add_discussion",
        "core_course_update_courses",
    ):
        assert forbidden not in ALLOWED_FUNCTIONS


@pytest.mark.asyncio
async def test_http_200_with_exception_raises(api):
    api.on("core_course_get_contents", {
        "exception": "Invalid token",
        "errorcode": "invalidtoken",
        "message": "Invalid token - token expired",
    })
    client = MoodleClient()
    with pytest.raises(AuthError):
        await client.call("core_course_get_contents", courseid=1)
    await client.aclose()


@pytest.mark.asyncio
async def test_request_form_contains_token_and_flattened_params(api):
    api.on("core_course_get_contents", [])
    client = MoodleClient()
    await client.call("core_course_get_contents", courseid=101, options=[{"name": "x", "value": True}])
    request = api.requests[-1]
    assert request["function"] == "core_course_get_contents"
    assert request["token"] == "test-token-123"
    assert request["params"]["courseid"] == ["101"]
    assert request["params"]["options[0][name]"] == ["x"]
    assert request["params"]["options[0][value]"] == ["1"]
    await client.aclose()


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_request(api):
    api.on("core_course_get_contents", load_fixture("course_contents.json"))
    client = MoodleClient()
    first = await client.call("core_course_get_contents", courseid=101)
    second = await client.call("core_course_get_contents", courseid=101)
    assert first == second
    assert api.count("core_course_get_contents") == 1
    await client.aclose()


def test_cache_key_does_not_contain_token():
    key = make_key("core_course_get_contents", {"courseid": 101})
    assert "test-token" not in key
    assert key.startswith("api:core_course_get_contents:")
    # 相同參數 → 相同 key；不同參數 → 不同 key
    assert key == make_key("core_course_get_contents", {"courseid": 101})
    assert key != make_key("core_course_get_contents", {"courseid": 102})


@pytest.mark.asyncio
async def test_retry_on_5xx_then_success(monkeypatch):
    import respx

    calls = {"count": 0}

    def flaky(_request):
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json={"ok": True})

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client_module, "_async_sleep", no_sleep)
    client = MoodleClient()
    with respx.mock(assert_all_called=False) as router:
        router.post("https://moodle.example.edu/webservice/rest/server.php").mock(
            side_effect=flaky
        )
        result = await client.call("core_webservice_get_site_info")
    assert result == {"ok": True}
    assert calls["count"] == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_4xx_is_not_retried():
    # 用 httpx 直接 mock 一個 403；respx 側錄請求次數
    import respx

    with respx.mock(assert_all_called=False) as router:
        router.post("https://moodle.example.edu/webservice/rest/server.php").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        client = MoodleClient()
        with pytest.raises(MoodleError):
            await client.call("core_course_get_contents", courseid=1)
        assert len(router.calls) == 1
        await client.aclose()


@pytest.mark.asyncio
async def test_site_info_cached_and_lists_functions(api):
    client = MoodleClient()
    info = await client.site_info()
    await client.site_info()
    assert info["userid"] == 42
    assert api.count("core_webservice_get_site_info") == 1
    assert await client.is_available("core_course_get_contents")
    assert not await client.is_available("core_user_create_users")
    await client.aclose()


@pytest.mark.asyncio
async def test_peek_returns_none_for_unknown(api):
    client = MoodleClient()
    assert client.peek("core_course_get_contents", courseid=999) is None
    await client.aclose()


def test_settings_token_not_in_repr():
    settings = get_settings()
    assert "test-token-123" not in repr(settings)
