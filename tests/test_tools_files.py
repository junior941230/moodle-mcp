"""檔案下載與文字抽取測試。"""

from __future__ import annotations

import httpx
import pytest

from moodle_mcp.cache import register_file_id, strip_url_token
from moodle_mcp.config import reload_settings
from moodle_mcp.errors import UnsupportedFileType
from moodle_mcp.files import extract_text, normalize_download_url
from moodle_mcp.tools.files import read_file


def test_extract_docx(tmp_path):
    from docx import Document

    document = Document()
    document.add_heading("Lesson 1", level=1)
    document.add_paragraph("第一段內容")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    path = tmp_path / "sample.docx"
    document.save(path)

    text = extract_text(
        path,
        "sample.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert "Lesson 1" in text
    assert "第一段內容" in text
    assert "| A | B |" in text


def test_extract_pptx_with_notes(tmp_path):
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "投影片標題"
    slide.placeholders[1].text = "重點一\n重點二"
    slide.notes_slide.notes_text_frame.text = "這是備忘稿"
    path = tmp_path / "sample.pptx"
    presentation.save(path)

    text = extract_text(
        path,
        "sample.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    assert "--- 投影片 1 ---" in text
    assert "投影片標題" in text
    assert "重點一" in text
    assert "這是備忘稿" in text


def test_extract_pdf_page_markers(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    path = tmp_path / "sample.pdf"
    with open(path, "wb") as handle:
        writer.write(handle)

    text = extract_text(path, "sample.pdf", "application/pdf")
    assert "--- 第 1 頁 ---" in text
    assert "--- 第 2 頁 ---" in text
    assert "沒有可抽取的文字" in text


def test_extract_text_big5(tmp_path):
    path = tmp_path / "big5.txt"
    path.write_bytes("中文測試 big5".encode("big5"))
    assert extract_text(path, "big5.txt", "text/plain") == "中文測試 big5"


def test_extract_html(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<h1>標題</h1><p>內容</p>", encoding="utf-8")
    text = extract_text(path, "page.html", "text/html")
    assert "# 標題" in text
    assert "內容" in text


def test_extract_unsupported(tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04dummy")
    with pytest.raises(UnsupportedFileType):
        extract_text(path, "archive.zip", "application/zip")


def test_strip_url_token():
    assert (
        strip_url_token("https://moodle.example.edu/a.pdf?token=abc&x=1")
        == "https://moodle.example.edu/a.pdf?x=1"
    )
    assert (
        strip_url_token("https://moodle.example.edu/a.pdf?wstoken=abc")
        == "https://moodle.example.edu/a.pdf"
    )
    assert strip_url_token("https://moodle.example.edu/a.pdf") == (
        "https://moodle.example.edu/a.pdf"
    )


def test_register_file_strips_token():
    register_file_id(
        file_id="abcdefabcdef",
        url="https://moodle.example.edu/pluginfile.php/1/a.pdf?token=secret",
        filename="a.pdf",
    )
    from moodle_mcp.cache import lookup_file

    assert "secret" not in lookup_file("abcdefabcdef")["url"]


def test_normalize_download_url_rewrites_and_rejects():
    url = normalize_download_url(
        "https://moodle.example.edu/pluginfile.php/1/mod_resource/content/1/a.pdf"
    )
    assert "/webservice/pluginfile.php/" in url
    assert "token=test-token-123" in url

    with pytest.raises(Exception) as excinfo:
        normalize_download_url(
            "https://evil.example.com/pluginfile.php/1/mod_resource/content/1/a.pdf"
        )
    assert "同站" in str(excinfo.value)


def _register(file_id: str, url: str, filename: str, size: int = 12, mime: str = "text/plain"):
    register_file_id(
        file_id=file_id,
        url=url,
        filename=filename,
        filesize=size,
        mimetype=mime,
        timemodified=123,
    )


@pytest.mark.asyncio
async def test_read_file_txt(api):
    file_id = "aaaabbbbcccc"
    _register(
        file_id,
        "https://moodle.example.edu/pluginfile.php/1/mod_resource/content/1/note.txt",
        "note.txt",
        size=10,
    )
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content="你好 hello".encode("utf-8"))

    api.router.get(url__regex=r"https://moodle\.example\.edu/webservice/pluginfile\.php/.*").mock(
        side_effect=handler
    )

    result = await read_file(file_id)
    assert "note.txt" in result
    assert "你好 hello" in result
    assert "共 " in result
    assert "test-token-123" not in result
    # 下載時確實帶了 token
    assert "token=test-token-123" in seen["url"]


@pytest.mark.asyncio
async def test_read_file_caches_extracted_text(api):
    file_id = "ddddeeeeffff"
    _register(
        file_id,
        "https://moodle.example.edu/pluginfile.php/1/mod_resource/content/1/cache.txt",
        "cache.txt",
    )
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, content="cache me".encode("utf-8"))

    api.router.get(url__regex=r"https://moodle\.example\.edu/webservice/pluginfile\.php/.*").mock(
        side_effect=handler
    )

    await read_file(file_id)
    await read_file(file_id)
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_read_file_missing_id(api):
    result = await read_file("000000000000")
    assert "不存在或已失效" in result


@pytest.mark.asyncio
async def test_read_file_rejects_external_host(api):
    file_id = "111122223333"
    _register(file_id, "https://evil.example.com/pluginfile.php/1/a.txt", "a.txt")
    result = await read_file(file_id)
    assert "同站" in result


@pytest.mark.asyncio
async def test_read_file_rejects_too_large(api, monkeypatch):
    monkeypatch.setenv("MOODLE_MAX_FILE_MB", "1")
    reload_settings()
    file_id = "444455556666"
    _register(
        file_id,
        "https://moodle.example.edu/pluginfile.php/1/big.pdf",
        "big.pdf",
        size=2 * 1024 * 1024,
        mime="application/pdf",
    )
    result = await read_file(file_id)
    assert "超過上限" in result
    assert "拒絕下載" in result


@pytest.mark.asyncio
async def test_read_file_unsupported_type(api):
    file_id = "777788889999"
    _register(
        file_id,
        "https://moodle.example.edu/pluginfile.php/1/movie.mp4",
        "movie.mp4",
        size=1024,
        mime="video/mp4",
    )
    result = await read_file(file_id)
    assert "無法轉成文字" in result
    assert "movie.mp4" in result
    assert "video/mp4" in result
    assert "1.0 KB" in result
