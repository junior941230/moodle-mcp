"""寫入功能的護欄：使用者確認、上傳路徑檢查、文字轉 HTML。

確認流程（confirm_action）：
1. 用戶端支援 MCP elicitation（例如 Claude Code）時，由 server 直接跳出確認視窗問使用者；
   這個回答不經過 LLM，所以就算 LLM 被惡意內容誘導，也無法替使用者按下同意。
2. 不支援 elicitation 時，改用「預覽 + 一次性確認碼」：第一次呼叫只回傳預覽與確認碼，
   必須用**完全相同的參數**加上確認碼再呼叫一次才會執行。確認碼 10 分鐘內有效、只能用一次。
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import get_settings
from .errors import NotFoundError, PermissionError_

logger = logging.getLogger(__name__)

CONFIRM_TTL_SECONDS = 10 * 60

# confirm_code -> (動作指紋, 到期時間)
_pending: dict[str, tuple[str, float]] = {}


class ConfirmChoice(BaseModel):
    """elicitation 表單：只有一個勾選框。"""

    confirm: bool = Field(default=False, description="勾選表示確定要執行這個動作")


def _fingerprint(action: str, params: dict[str, Any]) -> str:
    payload = json.dumps(
        {"action": action, "params": params}, sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _purge_expired(now: float) -> None:
    for code in [code for code, (_, expires) in _pending.items() if expires <= now]:
        _pending.pop(code, None)


def issue_code(action: str, params: dict[str, Any]) -> str:
    now = time.time()
    _purge_expired(now)
    code = secrets.token_hex(3)
    _pending[code] = (_fingerprint(action, params), now + CONFIRM_TTL_SECONDS)
    return code


def consume_code(code: str, action: str, params: dict[str, Any]) -> bool:
    """確認碼有效、未過期、且參數與預覽時相同才回傳 True；不論成功與否都只能用一次。"""
    now = time.time()
    _purge_expired(now)
    entry = _pending.pop(str(code or "").strip().lower(), None)
    if entry is None:
        return False
    fingerprint, expires = entry
    return expires > now and secrets.compare_digest(fingerprint, _fingerprint(action, params))


def reset_pending() -> None:
    """測試用。"""
    _pending.clear()


def _supports_elicitation(ctx: Any) -> bool:
    if ctx is None:
        return False
    try:
        from mcp import types

        return bool(
            ctx.session.check_client_capability(
                types.ClientCapabilities(elicitation=types.ElicitationCapability())
            )
        )
    except Exception:  # 沒有 session（例如直接呼叫函式）或舊版 SDK
        return False


async def confirm_action(
    ctx: Any,
    *,
    action: str,
    params: dict[str, Any],
    preview: str,
    confirm_code: str = "",
) -> str | None:
    """回傳 None 表示使用者已確認、可以執行；回傳字串則是要直接交給 LLM 的訊息（預覽或取消）。"""
    if _supports_elicitation(ctx):
        try:
            result = await ctx.elicit(
                message=f"{preview}\n\n確定要執行嗎？（勾選後送出才會執行）",
                schema=ConfirmChoice,
            )
        except Exception as exc:  # 確認失敗一律視為取消
            logger.warning("elicitation 失敗：%s", exc)
            return "⚠️ 無法向使用者確認，已取消，沒有做任何變更。"
        data = getattr(result, "data", None)
        if getattr(result, "action", "") == "accept" and data is not None and data.confirm:
            return None
        return "已取消：使用者沒有同意，沒有做任何變更。"

    if confirm_code:
        if consume_code(confirm_code, action, params):
            return None
        return (
            "⚠️ confirm_code 無效、已過期、已用過，或參數和預覽時不同，沒有做任何變更。"
            "請不帶 confirm_code 重新呼叫，取得新的預覽給使用者確認。"
        )

    code = issue_code(action, params)
    return (
        f"{preview}\n\n---\n\n"
        "⚠️ **尚未執行。** 請把上面的內容完整轉述給使用者，"
        "**只有在使用者明確同意後**，才用完全相同的參數加上 "
        f'`confirm_code="{code}"` 再呼叫一次（{CONFIRM_TTL_SECONDS // 60} 分鐘內有效、只能用一次）。'
        "不要自行替使用者同意。"
    )


# ---------------------------------------------------------------------------
# 上傳路徑
# ---------------------------------------------------------------------------


def resolve_upload_path(raw: str) -> Path:
    """只允許 MOODLE_UPLOAD_DIR 裡的一般檔案（不跟隨跳出資料夾的捷徑，不允許隱藏檔）。

    這是為了防止 LLM 被誘導把 `.env`、SSH 金鑰等本機檔案上傳到 Moodle。
    """
    settings = get_settings()
    if not settings.moodle_upload_dir:
        raise PermissionError_(
            "要上傳檔案必須先在 .env 設定 MOODLE_UPLOAD_DIR（只允許上傳這個資料夾裡的檔案）。"
        )
    try:
        base = Path(settings.moodle_upload_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise NotFoundError(
            f"MOODLE_UPLOAD_DIR 指定的資料夾不存在：{settings.moodle_upload_dir}"
        ) from None

    candidate = Path(str(raw or "").strip()).expanduser()
    if not str(raw or "").strip():
        raise NotFoundError("檔案路徑不可為空")
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise NotFoundError(f"找不到檔案：{raw}") from None

    if not resolved.is_relative_to(base):
        raise PermissionError_(
            f"只允許上傳 MOODLE_UPLOAD_DIR（{base}）裡的檔案，拒絕：{raw}"
        )
    if any(part.startswith(".") for part in resolved.relative_to(base).parts):
        raise PermissionError_(f"不允許上傳隱藏檔或隱藏資料夾裡的檔案：{raw}")
    if not resolved.is_file():
        raise NotFoundError(f"不是一般檔案：{raw}")
    size = resolved.stat().st_size
    if size > settings.max_file_bytes:
        raise PermissionError_(
            f"檔案 {resolved.name} 有 {size / 1024 / 1024:.1f} MB，"
            f"超過上限 {settings.moodle_max_file_mb:g} MB"
        )
    return resolved


# ---------------------------------------------------------------------------
# 文字
# ---------------------------------------------------------------------------

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def text_to_html(text: str) -> str:
    """純文字 → 安全的 HTML（跳脫特殊字元；空行分段、換行轉 <br>）。"""
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(str(text or "").replace("\r\n", "\n"))]
    return "".join(
        "<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs if p
    )


def excerpt(text: str, limit: int = 1500) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"…（共 {len(text)} 字，只顯示前 {limit} 字）"


__all__ = [
    "ConfirmChoice",
    "confirm_action",
    "consume_code",
    "excerpt",
    "issue_code",
    "reset_pending",
    "resolve_upload_path",
    "text_to_html",
]
