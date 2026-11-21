"""舊版 .ppt 文字抽取測試：直接組出 PowerPoint Document stream 的 record。"""

from __future__ import annotations

import struct

import pytest

from moodle_mcp import ppt_legacy as P
from moodle_mcp.errors import UnsupportedFileType
from moodle_mcp.files import extract_text


def rec(rec_type: int, data: bytes = b"", *, instance: int = 0, container: bool = False) -> bytes:
    ver = 0x0F if container else 0
    return struct.pack("<HHI", (instance << 4) | ver, rec_type, len(data)) + data


def chars(text: str) -> bytes:
    return rec(P.RT_TEXT_CHARS_ATOM, text.encode("utf-16-le"))


def textbox(text: str) -> bytes:
    # OfficeArt SpContainer(0xF004) > ClientTextbox(0xF00D) > TextCharsAtom
    return rec(0xF004, rec(0xF00D, chars(text), container=True), container=True)


def slide_persist(persist_ref: int, slide_id: int) -> bytes:
    return rec(P.RT_SLIDE_PERSIST_ATOM, struct.pack("<IIiII", persist_ref, 0, 1, slide_id, 0))


def slide(notes_id: int, *body: bytes) -> bytes:
    atom = rec(P.RT_SLIDE_ATOM, struct.pack("<I8sIIHH", 0, b"\0" * 8, 0, notes_id, 0, 0))
    return rec(P.RT_SLIDE, atom + b"".join(body), container=True)


def user_edit(last_edit: int, directory: int, doc_ref: int) -> bytes:
    return rec(P.RT_USER_EDIT_ATOM, struct.pack("<IHBBIII", 0, 0, 0, 3, last_edit, directory, doc_ref) + b"\0" * 8)


def persist_dir(first_id: int, offsets: list[int]) -> bytes:
    entry = first_id | (len(offsets) << 20)
    return rec(P.RT_PERSIST_DIRECTORY_ATOM, struct.pack(f"<I{len(offsets)}I", entry, *offsets))


def current_user(edit_offset: int) -> bytes:
    return rec(0x0FF6, struct.pack("<III", 20, 0xE391C05F, edit_offset))


class StreamBuilder:
    def __init__(self) -> None:
        self.data = b""

    def add(self, blob: bytes) -> int:
        offset = len(self.data)
        self.data += blob
        return offset


def build_basic() -> tuple[StreamBuilder, int, int]:
    """兩張投影片；第一張有文字方塊與備忘稿。回傳 (builder, 第一次 edit offset, doc offset)。"""
    s = StreamBuilder()
    slide1 = s.add(slide(300, textbox("文字方塊內容")))
    slide2 = s.add(slide(0))
    notes = s.add(rec(P.RT_NOTES, textbox("這是備忘稿"), container=True))
    document = s.add(
        rec(
            P.RT_DOCUMENT,
            rec(
                P.RT_SLIDE_LIST_WITH_TEXT,
                slide_persist(1, 256)
                + chars("第一頁標題")
                + rec(P.RT_TEXT_BYTES_ATOM, b"Hello\r*")
                + slide_persist(2, 257)
                + chars("第二頁標題"),
                container=True,
            )
            + rec(
                P.RT_SLIDE_LIST_WITH_TEXT,
                slide_persist(3, 300),
                instance=2,
                container=True,
            ),
            container=True,
        )
    )
    directory = s.add(persist_dir(1, [slide1, slide2, notes, document]))
    edit = s.add(user_edit(0, directory, 4))
    return s, edit, document


def test_structured_slides_textboxes_and_notes():
    s, edit, _ = build_basic()
    text = P._structured(s.data, current_user(edit))
    first, second = text.split("\n\n")
    assert first.splitlines() == [
        "--- 投影片 1 ---",
        "第一頁標題",
        "Hello",  # TextBytesAtom；"*" 欄位佔位字元被濾掉
        "文字方塊內容",
        "（備忘稿）這是備忘稿",
    ]
    assert second.splitlines() == ["--- 投影片 2 ---", "第二頁標題"]


def test_fast_save_uses_newest_slide_version():
    s, first_edit, _ = build_basic()
    new_slide = s.add(slide(300, textbox("修改後的內容")))
    directory = s.add(persist_dir(1, [new_slide]))
    edit = s.add(user_edit(first_edit, directory, 4))

    text = P._structured(s.data, current_user(edit))
    assert "修改後的內容" in text
    assert "文字方塊內容" not in text
    assert "這是備忘稿" in text  # 沒被改到的 persist 仍沿用舊版


def test_falls_back_to_flat_text_when_structure_is_broken():
    s, _, _ = build_basic()
    with pytest.raises(P.PptFormatError):
        P._structured(s.data, current_user(len(s.data) + 100))
    text = P._flat(s.data)
    assert text.startswith("（無法辨識投影片分頁")
    for expected in ("第一頁標題", "第二頁標題", "文字方塊內容", "這是備忘稿"):
        assert expected in text


def test_extract_text_rejects_non_ole_ppt(tmp_path):
    path = tmp_path / "fake.ppt"
    path.write_bytes(b"not an ole file")
    with pytest.raises(UnsupportedFileType):
        extract_text(path, "fake.ppt", "application/vnd.ms-powerpoint")
