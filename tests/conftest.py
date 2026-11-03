"""測試共用 fixtures：隔離設定 / 快取，並用 respx 模擬 Moodle REST API。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"

SITE_INFO = "core_webservice_get_site_info"
REST_URL = "https://moodle.example.edu/webservice/rest/server.php"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeMoodle:
    """用 wsfunction 決定回應的假 Moodle。"""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.router: respx.Router | None = None

    def on(self, function: str, payload: Any) -> FakeMoodle:
        self.handlers[function] = payload
        return self

    def count(self, function: str) -> int:
        return sum(1 for request in self.requests if request["function"] == function)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        function = (body.get("wsfunction") or [""])[0]
        self.requests.append(
            {
                "function": function,
                "params": {
                    key: values
                    for key, values in body.items()
                    if key not in {"wstoken", "wsfunction", "moodlewsrestformat"}
                },
                "token": (body.get("wstoken") or [""])[0],
            }
        )

        payload = self.handlers.get(function)
        if payload is None:
            return httpx.Response(
                200,
                json={
                    "exception": "Web service function is not available",
                    "errorcode": "accessexception",
                    "message": f"Function {function} is not available",
                },
            )
        if callable(payload):
            payload = payload(body)
        return httpx.Response(200, json=payload)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """每個測試：獨立快取目錄、固定站台與 token。"""
    monkeypatch.setenv("MOODLE_URL", "https://moodle.example.edu")
    monkeypatch.setenv("MOODLE_TOKEN", "test-token-123")
    monkeypatch.setenv("MOODLE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MOODLE_TZ", "Asia/Taipei")
    monkeypatch.setenv("MOODLE_MAX_FILE_MB", "30")

    from moodle_mcp import cache, client, config

    config.reload_settings()
    client.reset_client()
    cache.reset_cache(str(tmp_path / "cache"))
    yield
    client.reset_client()


@pytest.fixture
def api():
    """回傳 FakeMoodle；預設已註冊 site_info。"""
    fake = FakeMoodle()
    fake.on(SITE_INFO, load_fixture("site_info.json"))
    with respx.mock(assert_all_called=False) as router:
        router.post(REST_URL).mock(side_effect=fake._handle)
        fake.router = router
        yield fake


@pytest.fixture
def site_info() -> dict[str, Any]:
    return load_fixture("site_info.json")
