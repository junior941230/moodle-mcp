"""教材檔案讀取 tool（PDF / DOCX / PPTX / 文字 / HTML）。"""

from __future__ import annotations

from typing import Any

from ..cache import lookup_file
from ..errors import UnsupportedFileType, tool_guard
from ..files import get_file_text
from ..formatting import human_size, paginate_text, truncate_output


def _header(meta: dict[str, Any], file_id: str, total: int | None = None) -> str:
    lines = [
        f"# 檔案：{meta.get('filename') or '未命名檔案'}",
        "",
        f"- file_id：{file_id}",
        f"- 類型：{meta.get('mimetype') or '未知'}",
        f"- 大小：{human_size(meta.get('filesize'))}",
    ]
    if total is not None:
        lines.append(f"- 總字數：{total}")
    return "\n".join(lines) + "\n\n---\n\n"


@tool_guard
async def read_file(file_id: str, offset: int = 0, max_chars: int = 12000) -> str:
    """讀取課程教材 / 作業附件的內容並轉成文字。

    Args:
        file_id: 由 get_course_contents、get_assignment 或 get_discussion 產生的
            12 碼短 ID（**不要傳 URL**）。
        offset: 從第幾個字元開始（用於分頁續讀），預設 0。
        max_chars: 這次最多回傳幾個字元（500–40000，預設 12000）。

    Returns:
        檔案中繼資訊與抽取出的文字。PDF 會標示頁碼、PPTX 會標示投影片與備忘稿。
        影片、壓縮檔、圖片等無法轉文字的檔案會回傳檔名、大小、類型與說明。
    """
    try:
        meta, text = await get_file_text(file_id)
    except UnsupportedFileType as exc:
        meta = lookup_file(str(file_id).strip())
        if meta is None:
            raise
        return (
            _header(meta, str(file_id).strip())
            + "（此檔案無法轉成文字）\n\n"
            + f"{exc}\n\n"
            + "若需要內容，請自行下載後用對應的應用程式開啟。"
        )

    length = max(500, min(int(max_chars or 12000), 40000))
    header = _header(meta, str(file_id).strip(), total=len(text))

    if not text.strip():
        return header + "（檔案沒有可抽取的文字，可能是掃描影像。）"

    offset = max(0, int(offset or 0))
    if offset >= len(text):
        return header + f"（offset={offset} 已超過總字數 {len(text)}，請從 0 開始。）"

    body = paginate_text(text, offset=offset, max_chars=length)
    if len(header) + len(body) <= 42000:
        return header + body
    return truncate_output(header + body, 40000)


def register(mcp: Any) -> None:
    mcp.add_tool(read_file)


__all__ = ["read_file", "register"]
