"""FastMCP server：註冊 tools / resources / prompts 並啟動 stdio。"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP

from .client import close_client, get_client
from .config import get_settings
from .errors import RedactingFilter, redact

logger = logging.getLogger("moodle_mcp")

INSTRUCTIONS = """\
這是一個唯讀的 Moodle MCP server。你可以查詢使用者自己的課程、教材、
作業與截止日、公告、成績，並讀取教材檔案（PDF / DOCX / PPTX / 文字）。
所有工具都回傳 markdown；時間已轉成使用者時區並附相對時間。
本 server 不提供任何寫入功能（不能交作業、發文或改設定）。
遇到「本站未開放此功能」時，請告訴使用者該功能未開放，不要重試。
"""


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    settings = get_settings()
    if settings.moodle_token:
        try:
            info = await get_client().site_info()
            logger.info(
                "已連線 Moodle：%s（使用者：%s，userid=%s）",
                info.get("sitename") or settings.moodle_url,
                info.get("fullname") or "未知",
                info.get("userid"),
            )
        except Exception as exc:  # 啟動失敗不應讓 server 完全無法啟動
            logger.warning("啟動時無法取得 site_info：%s", redact(str(exc)))
    else:
        logger.warning(
            "MOODLE_TOKEN 未設定；tools 會回傳錯誤。"
            "請在 .env 設定或執行 scripts/get_token.py。"
        )
    try:
        yield {}
    finally:
        await close_client()


mcp = FastMCP(
    "moodle-mcp",
    instructions=INSTRUCTIONS,
    lifespan=_lifespan,
)


def _register_tools() -> None:
    from .tools import (
        assignments,
        calendar,
        contents,
        courses,
        files,
        forums,
        grades,
        system,
    )

    for module in (
        courses,
        contents,
        assignments,
        calendar,
        forums,
        grades,
        files,
        system,
    ):
        module.register(mcp)


_register_tools()


@mcp.resource("moodle://courses", mime_type="text/markdown")
async def courses_resource() -> str:
    """目前進行中的課程清單（同 list_courses）。"""
    from .tools.courses import list_courses

    return await list_courses("inprogress")


@mcp.resource("moodle://course/{course_id}", mime_type="text/markdown")
async def course_resource(course_id: str) -> str:
    """單一課程的章節與教材（同 get_course_contents）。"""
    from .tools.courses import get_course_contents

    try:
        return await get_course_contents(int(course_id))
    except (TypeError, ValueError):
        return f"⚠️ course_id 必須是整數，收到：{course_id}"


@mcp.prompt(
    name="weekly_briefing",
    description="整理本週待辦：先查未來 7 天截止日，再查最新公告。",
)
def weekly_briefing() -> str:
    """產生「本週待辦」的提示詞。"""
    return (
        "請幫我整理本週的 Moodle 待辦事項。步驟：\n"
        "1. 呼叫 upcoming_deadlines(days=7)，列出未來 7 天的截止日。\n"
        "2. 呼叫 list_announcements(limit=10)，看有沒有重要公告。\n"
        "3. 若有作業即將到期且我不確定是否已繳，呼叫 list_assignments() 確認狀態。\n"
        "請用繁體中文，依時間排序，把『還沒交且最急』的項目放在最前面，"
        "並附上課程、截止時間與 assign_id。"
    )


def setup_logging() -> None:
    """log 一律寫 stderr；stdout 是 MCP stdio 通道，絕對不能碰。"""
    settings = get_settings()
    level = getattr(logging, str(settings.moodle_log_level).upper(), logging.INFO)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # 這些套件的 INFO 太吵，而且可能夾帶請求細節
    for noisy in ("httpx", "httpcore", "mcp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    settings = get_settings()
    if not settings.moodle_token:
        print(
            "錯誤：MOODLE_TOKEN 未設定。\n"
            "請在專案目錄建立 .env（可複製 .env.example），"
            "或執行 `uv run python scripts/get_token.py` 取得 token。",
            file=sys.stderr,
        )
        raise SystemExit(1)

    logger.info("啟動 moodle-mcp（%s）", settings.moodle_url)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
