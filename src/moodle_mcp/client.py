"""MoodleClient：唯一會打 HTTP 的地方（唯讀 Web Service API）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from .cache import TTL_COURSES, get_cache, make_key
from .config import Settings, get_settings
from .errors import (
    FunctionNotAllowed,
    FunctionUnavailable,
    MoodleError,
    error_from_payload,
    redact,
)

logger = logging.getLogger(__name__)

REST_PATH = "/webservice/rest/server.php"
SITE_INFO_FUNCTION = "core_webservice_get_site_info"

# 唯讀白名單：client.call() 只允許這些 wsfunction。
ALLOWED_FUNCTIONS = frozenset(
    {
        "core_webservice_get_site_info",
        "core_enrol_get_users_courses",
        "core_course_get_enrolled_courses_by_timeline_classification",
        "core_course_get_contents",
        "mod_assign_get_assignments",
        "mod_assign_get_submission_status",
        "core_calendar_get_action_events_by_timesort",
        "core_calendar_get_calendar_upcoming_view",
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
        "mod_forum_get_discussion_posts",
        "gradereport_user_get_grade_items",
        "gradereport_overview_get_course_grades",
        "mod_resource_get_resources_by_courses",
        "mod_page_get_pages_by_courses",
        "mod_url_get_urls_by_courses",
        "mod_quiz_get_quizzes_by_courses",
        "message_popup_get_popup_notifications",
    }
)

MAX_ATTEMPTS = 4  # 首次 + 3 次重試
RETRY_DELAYS = (0.5, 1.0, 2.0)

_client: "MoodleClient | None" = None


async def _async_sleep(seconds: float) -> None:
    """獨立函式，方便測試 patch 掉等待。"""
    await asyncio.sleep(seconds)


def flatten_params(params: Mapping[str, Any]) -> list[tuple[str, str]]:
    """把巢狀 dict / list 攤平成 Moodle REST 需要的 `a[0][b]=x` 形式。

    >>> flatten_params({"courseids": [3, 5]})
    [('courseids[0]', '3'), ('courseids[1]', '5')]
    >>> flatten_params({"options": [{"name": "x", "value": True}]})
    [('options[0][name]', 'x'), ('options[0][value]', '1')]
    """
    out: list[tuple[str, str]] = []

    def walk(prefix: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for key, sub in value.items():
                key_str = str(key)
                walk(f"{prefix}[{key_str}]" if prefix else key_str, sub)
        elif isinstance(value, (list, tuple)):
            for index, sub in enumerate(value):
                walk(f"{prefix}[{index}]", sub)
        elif isinstance(value, bool):
            out.append((prefix, "1" if value else "0"))
        else:
            out.append((prefix, str(value)))

    walk("", params)
    return out


class MoodleClient:
    """非同步、限流、重試、快取、白名單的 Moodle API client。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._http = httpx.AsyncClient(
            base_url=self.settings.moodle_url,
            timeout=httpx.Timeout(self.settings.moodle_timeout, connect=10.0),
            follow_redirects=True,
            verify=self.settings.ssl_verify,
            transport=transport,
        )
        self._semaphore = asyncio.Semaphore(self.settings.moodle_concurrency)
        self._site_info: dict[str, Any] | None = None
        self._function_names: set[str] = set()
        self._site_lock = asyncio.Lock()

    # -- 對外 API ---------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()

    async def site_info(self, *, force: bool = False) -> dict[str, Any]:
        """站台 / 使用者資訊；整個 process 只抓一次。"""
        if self._site_info is not None and not force:
            return self._site_info
        async with self._site_lock:
            if self._site_info is not None and not force:
                return self._site_info
            info = await self.call(
                SITE_INFO_FUNCTION, use_cache=False, ttl=TTL_COURSES
            )
            if not isinstance(info, dict):
                raise MoodleError("site_info 回應格式異常")
            self._site_info = info
            self._function_names = {
                str(fn.get("name"))
                for fn in (info.get("functions") or [])
                if isinstance(fn, dict) and fn.get("name")
            }
            return info

    async def available_functions(self) -> set[str]:
        try:
            await self.site_info()
        except MoodleError:
            return set()
        return set(self._function_names)

    async def is_available(self, function: str) -> bool:
        names = await self.available_functions()
        # 拿不到 functions 清單時採「樂觀允許」，讓實際呼叫回報真正錯誤
        return not names or function in names

    async def call(
        self,
        function: str,
        *,
        ttl: int | None = None,
        use_cache: bool = True,
        **params: Any,
    ) -> Any:
        """呼叫單一 wsfunction。

        - 先檢查白名單（唯讀唯一防線）
        - 若已知 functions[] 且不包含此函式 → FunctionUnavailable
        - 快取 key 不含 token
        """
        if function not in ALLOWED_FUNCTIONS:
            raise FunctionNotAllowed(
                f"拒絕呼叫非白名單函式：{function}（本 server 為唯讀）"
            )
        if self._function_names and function not in self._function_names:
            raise FunctionUnavailable(
                f"本站未開放此功能：{function}", errorcode="servicenotavailable"
            )

        cache = get_cache()
        key = make_key(function, params) if use_cache else ""
        if use_cache:
            cached = cache.get(key)
            if cached is not None:
                logger.debug("快取命中：%s", function)
                return cached

        data = await self._post(function, params)

        if use_cache:
            cache.set(key, data, ttl=ttl if ttl is not None else self.settings.moodle_cache_ttl)
        return data

    def peek(self, function: str, **params: Any) -> Any | None:
        """只讀快取，不發 HTTP（search_course 用）。"""
        if function not in ALLOWED_FUNCTIONS:
            return None
        return get_cache().get(make_key(function, params))

    # -- 內部 -------------------------------------------------------------

    async def _post(self, function: str, params: Mapping[str, Any]) -> Any:
        token = self.settings.require_token()
        form: list[tuple[str, str]] = [
            ("wstoken", token),
            ("wsfunction", function),
            ("moodlewsrestformat", "json"),
            *flatten_params(params),
        ]
        # 自己 urlencode：httpx 的 data= 只接受 Mapping，list[tuple] 會被當成 raw content
        body = urlencode(form)

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self._semaphore:
                    response = await self._http.post(
                        REST_PATH,
                        content=body,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                        },
                    )
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning(
                    "%s 網路錯誤（第 %d/%d 次）：%s",
                    function,
                    attempt + 1,
                    MAX_ATTEMPTS,
                    redact(str(exc)),
                )
                if attempt < len(RETRY_DELAYS):
                    await _async_sleep(RETRY_DELAYS[attempt])
                    continue
                break

            if response.status_code >= 500:
                last_error = MoodleError(
                    f"HTTP {response.status_code}（{function}）"
                )
                logger.warning(
                    "%s 伺服器錯誤 %d（第 %d 次）",
                    function,
                    response.status_code,
                    attempt + 1,
                )
                if attempt < len(RETRY_DELAYS):
                    await _async_sleep(RETRY_DELAYS[attempt])
                    continue
                break

            if response.status_code >= 400:
                # 4xx 不重試
                raise MoodleError(
                    f"HTTP {response.status_code}（{function}）：{response.text[:200]}"
                )

            try:
                data = response.json()
            except ValueError:
                raise MoodleError(
                    f"Moodle 回應不是 JSON（{function}）：{response.text[:200]}"
                ) from None

            if isinstance(data, dict) and (
                data.get("exception") or data.get("errorcode")
            ):
                raise error_from_payload(data)
            return data

        raise MoodleError(
            f"連線 Moodle 失敗（{function}），已重試 {MAX_ATTEMPTS - 1} 次："
            f"{redact(str(last_error))}"
        )


def get_client() -> MoodleClient:
    """全域 client（同一 process 共用連線與 site_info 快取）。"""
    global _client
    if _client is None:
        _client = MoodleClient()
    return _client


def reset_client() -> None:
    """測試 / 重新載入設定用。"""
    global _client
    _client = None


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
