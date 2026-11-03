"""例外類別、Moodle 錯誤碼對應、token 遮罩與工具錯誤處理。"""

from __future__ import annotations

import functools
import logging
import re
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

# 任何 token 形式都要遮罩：wstoken=xxx、token=xxx、token%3Dxxx、"token": "xxx"
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\b(wstoken|token)(=|%3D)([^&\s\"'<>]+)"),
    re.compile(r"""(?i)["']?(wstoken|token)["']?\s*:\s*["']([^"']+)["']"""),
)


def redact(text: str) -> str:
    """把字串中的 token 換成 ***，用於 log、例外訊息與工具輸出。"""
    if not text:
        return text
    out = text
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups == 3:
            out = pattern.sub(r"\1\2***", out)
        else:
            out = pattern.sub(r'"\1": "***"', out)
    return out


class MoodleError(Exception):
    """所有 Moodle 相關錯誤的基底類別。訊息會自動遮罩 token。"""

    def __init__(self, message: str = "", *, errorcode: str | None = None) -> None:
        self.errorcode = errorcode
        super().__init__(redact(message))


class AuthError(MoodleError):
    """Token 無效 / 過期。"""


class PermissionError_(MoodleError):
    """已登入但沒有權限。"""


class NotFoundError(MoodleError):
    """找不到資源，或參數錯誤。"""


class FunctionUnavailable(MoodleError):
    """本站（或此 token 的 service）未開放該 wsfunction。"""


class FunctionNotAllowed(MoodleError):
    """呼叫了白名單以外的 wsfunction（程式錯誤或惡意誘導）。"""


class FileTooLarge(MoodleError):
    """下載檔案超過 MOODLE_MAX_FILE_MB。"""


class UnsupportedFileType(MoodleError):
    """檔案類型無法轉成文字。"""


def error_from_payload(payload: dict[str, Any]) -> MoodleError:
    """把 Moodle 的錯誤 JSON（HTTP 200）轉成對應例外。"""
    errorcode = str(payload.get("errorcode") or "")
    message = str(payload.get("message") or payload.get("exception") or "").strip()
    lower_msg = message.lower()
    detail = message or "Moodle 回傳未知錯誤"

    if errorcode == "invalidtoken":
        return AuthError(
            "Token 無效或已過期，請重新執行 `scripts/get_token.py` 取得新 token。"
            f"（Moodle: {detail}）",
            errorcode=errorcode,
        )

    # accessexception 有兩種：service 沒開這個 function，或真的沒權限
    looks_like_function = "function" in lower_msg or "wsfunction" in lower_msg
    if errorcode == "servicenotavailable" or (
        errorcode == "accessexception" and looks_like_function
    ):
        return FunctionUnavailable(
            f"本站未開放此功能（{errorcode}）：{detail}", errorcode=errorcode
        )

    if errorcode in {"accessexception", "nopermissions", "requireloginerror"}:
        return PermissionError_(
            f"沒有權限存取這個資源（{errorcode}）：{detail}", errorcode=errorcode
        )

    if errorcode in {"invalidrecord", "invalidparameter", "invalidargument"}:
        return NotFoundError(
            f"找不到資源或參數錯誤（{errorcode}）：{detail}", errorcode=errorcode
        )

    return MoodleError(f"{errorcode + '：' if errorcode else ''}{detail}", errorcode=errorcode)


class RedactingFilter(logging.Filter):
    """logging filter：任何 log record 都先遮罩 token。

    直接遮罩「組好的完整訊息」，而不是只處理字串參數；
    否則以例外物件當參數（`logger.error("%s", exc)`）或附帶的 traceback
    都會繞過遮罩。
    """

    _exc_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 格式字串與參數不符時，保底用原始 msg
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None

        # Formatter 只在 exc_text 為空時才自行格式化 traceback，
        # 所以先在這裡產生並遮罩，Formatter 就會直接使用遮罩後的版本。
        if record.exc_info and not record.exc_text:
            record.exc_text = self._exc_formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


F = TypeVar("F", bound=Callable[..., Awaitable[str]])


def tool_guard(func: F) -> F:
    """MCP tool 的錯誤護欄：任何例外都變成 LLM 看得懂的 markdown，不噴 stack trace。"""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except FunctionUnavailable as exc:
            logger.warning("%s: 功能未開放：%s", func.__name__, exc)
            return (
                "⚠️ 本站未開放此功能（此 token 的 Web Service 沒有對應函式）。"
                f"\n\n> {redact(str(exc))}"
            )
        except AuthError as exc:
            logger.warning("%s: 認證失敗", func.__name__)
            return f"⚠️ {redact(str(exc))}"
        except PermissionError_ as exc:
            return f"⚠️ {redact(str(exc))}"
        except NotFoundError as exc:
            return f"⚠️ {redact(str(exc))}"
        except MoodleError as exc:
            return f"⚠️ Moodle 錯誤：{redact(str(exc))}"
        except Exception as exc:  # noqa: BLE001 - 工具必須永遠回傳字串
            logger.exception("%s: 未預期的錯誤：%s", func.__name__, exc)
            return f"⚠️ 未預期的錯誤：{redact(str(exc))}"

    return wrapper  # type: ignore[return-value]
