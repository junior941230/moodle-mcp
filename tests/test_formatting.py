"""formatting 與 redact 的單元測試。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from moodle_mcp.errors import redact
from moodle_mcp.formatting import (
    format_ts,
    html_to_markdown,
    html_to_plain,
    human_size,
    md_table,
    paginate_text,
    relative_time,
    truncate_output,
)


def test_html_to_markdown_strips_img_and_script():
    html = '<h2>標題</h2><p>內容 <b>粗體</b></p><img src="x.png"><script>evil()</script>'
    result = html_to_markdown(html)
    assert "## 標題" in result
    assert "**粗體**" in result
    assert "img" not in result
    assert "evil" not in result


def test_html_to_markdown_collapses_blank_lines():
    result = html_to_markdown("<p>a</p><br><br><br><p>b</p>")
    assert "\n\n\n" not in result
    assert "a" in result and "b" in result


def test_html_to_plain_and_limit():
    result = html_to_plain("<p>Hello <b>world</b> 你好</p>", limit=10)
    assert result.startswith("Hello wor")
    assert result.endswith("…")


def test_format_ts_timezone_and_weekday():
    # 2026-11-03 12:34 Asia/Taipei 是星期二
    dt = datetime(2026, 11, 3, 12, 34, tzinfo=ZoneInfo("Asia/Taipei"))
    result = format_ts(int(dt.timestamp()), with_relative=False)
    assert result == "2026-11-03 12:34 (週二)"


def test_format_ts_cross_day_relative():
    dt = datetime(2026, 11, 3, 23, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    ts = int(dt.timestamp())
    result = format_ts(ts, with_relative=True)
    assert result.startswith("2026-11-03 23:30 (週二)（")
    assert any(unit in result for unit in ("前", "後", "現在"))


def test_format_ts_empty():
    assert format_ts(0, empty="無截止日") == "無截止日"


def test_relative_time_units():
    now = 1_800_000_000
    assert relative_time(now + 30, now=now) in {"現在", "1 分鐘後"}
    assert relative_time(now + 3 * 3600, now=now) == "3 小時後"
    assert relative_time(now + 3 * 86400, now=now) == "3 天後"
    assert relative_time(now - 2 * 86400, now=now) == "2 天前"


def test_paginate_text():
    text = "a" * 100
    page = paginate_text(text, offset=0, max_chars=40)
    assert page.startswith("a" * 40)
    assert "第 1–40 字" in page
    assert "offset=40" in page
    second = paginate_text(text, offset=40, max_chars=40)
    assert "第 41–80 字" in second
    assert "offset=80" in second
    last = paginate_text(text, offset=80, max_chars=40)
    assert "第 81–100 字" in last
    assert "共 100 字" in last


def test_truncate_output():
    text = "x" * 100
    result = truncate_output(text, max_chars=20)
    assert result.startswith("x" * 20)
    assert "已截斷" in result
    assert truncate_output("short", 100) == "short"


def test_md_table_escapes_pipe():
    table = md_table(["a", "b"], [["x|y", "line\nbreak"]])
    assert "x\\|y" in table
    assert "line break" in table
    assert table.startswith("| a | b |")


def test_human_size():
    assert human_size(0) == "大小未知"
    assert human_size(512) == "512 B"
    assert human_size(1536).startswith("1.5 KB")
    assert human_size(2 * 1024 * 1024) == "2.0 MB"


@pytest.mark.parametrize(
    "text",
    [
        "https://moodle.example.edu/x?token=abc123&y=1",
        "https://moodle.example.edu/x?wstoken=abc123",
        '{"token": "abc123"}',
    ],
)
def test_redact_masks_tokens(text):
    masked = redact(text)
    assert "abc123" not in masked
    assert "***" in masked


def test_redact_keeps_plain_text():
    assert redact("hello world") == "hello world"


def _capture_log(logger_name: str):
    import io
    import logging

    from moodle_mcp.errors import RedactingFilter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_redacting_filter_masks_exception_args_and_traceback():
    logger, stream = _capture_log("moodle_mcp.test_redact")
    exc = ValueError("bad https://x/webservice/pluginfile.php/1?token=SECRET123")

    logger.error("錯誤：%s", exc)
    try:
        raise exc
    except ValueError:
        logger.exception("例外 %s", "wstoken=SECRET456")

    output = stream.getvalue()
    assert "SECRET123" not in output
    assert "SECRET456" not in output
    assert "token=***" in output
    assert "Traceback" in output  # traceback 仍保留，只是遮罩過


def test_redacting_filter_keeps_non_string_args():
    logger, stream = _capture_log("moodle_mcp.test_redact_args")
    logger.info("第 %d 次，共 %s 筆", 3, 10)
    assert "第 3 次，共 10 筆" in stream.getvalue()
