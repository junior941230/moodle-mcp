"""舊版 PowerPoint 97-2003（.ppt / .pps / .pot）文字抽取。

.ppt 是 OLE 複合檔，投影片資料在 ``PowerPoint Document`` stream 裡，由一棵
record 樹組成（格式見 Microsoft [MS-PPT]）。這裡只做唯讀的文字抽取：

1. 由 ``Current User`` stream 找到最新的 UserEditAtom，沿著 edit 鏈讀出
   persist directory（persist id → stream offset，新版優先，支援 fast-save）。
2. DocumentContainer 裡的 SlideListWithText（instance 0 = 投影片、2 = 備忘稿）
   依序列出每張投影片的 SlidePersistAtom 與 placeholder 文字。
3. 再到每張投影片 / 備忘稿的 container 裡收集文字方塊（ClientTextbox）的文字。

格式不完整或解析失敗時，退回「依出現順序列出所有文字」。
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# record types
RT_DOCUMENT = 0x03E8
RT_SLIDE = 0x03EE
RT_SLIDE_ATOM = 0x03EF
RT_NOTES = 0x03F0
RT_SLIDE_PERSIST_ATOM = 0x03F3
RT_SLIDE_LIST_WITH_TEXT = 0x0FF0
RT_TEXT_CHARS_ATOM = 0x0FA0
RT_TEXT_BYTES_ATOM = 0x0FA8
RT_USER_EDIT_ATOM = 0x0FF5
RT_PERSIST_DIRECTORY_ATOM = 0x1772

_TEXT_TYPES = {RT_TEXT_CHARS_ATOM, RT_TEXT_BYTES_ATOM}
_HEADER = struct.Struct("<HHI")
_MAX_EDITS = 10_000


class PptFormatError(Exception):
    """不是可解析的 .ppt。"""


@dataclass
class _Record:
    rec_type: int
    instance: int
    is_container: bool
    start: int  # record header 起點
    data_start: int
    data_end: int


@dataclass
class _Slide:
    persist_ref: int
    slide_id: int
    texts: list[str] = field(default_factory=list)


def _read_record(stream: bytes, offset: int) -> _Record:
    if offset < 0 or offset + _HEADER.size > len(stream):
        raise PptFormatError("record 超出範圍")
    ver_instance, rec_type, length = _HEADER.unpack_from(stream, offset)
    data_start = offset + _HEADER.size
    return _Record(
        rec_type=rec_type,
        instance=ver_instance >> 4,
        is_container=(ver_instance & 0x0F) == 0x0F,
        start=offset,
        data_start=data_start,
        data_end=min(data_start + length, len(stream)),
    )


def _children(stream: bytes, record: _Record) -> Iterator[_Record]:
    offset = record.data_start
    while offset + _HEADER.size <= record.data_end:
        child = _read_record(stream, offset)
        yield child
        if child.data_end <= offset:
            break
        offset = child.data_end


def _walk_texts(stream: bytes, record: _Record, depth: int = 0) -> Iterator[str]:
    """遞迴收集 container 內所有文字 atom（含 OfficeArt 文字方塊）。"""
    if depth > 64:
        return
    for child in _children(stream, record):
        if child.rec_type in _TEXT_TYPES:
            text = _decode_text(stream, child)
            if text:
                yield text
        elif child.is_container:
            yield from _walk_texts(stream, child, depth + 1)


def _decode_text(stream: bytes, record: _Record) -> str:
    raw = stream[record.data_start : record.data_end]
    if record.rec_type == RT_TEXT_CHARS_ATOM:
        text = raw.decode("utf-16-le", errors="replace")
    else:
        # TextBytesAtom：每個 byte 是 UTF-16 字元的低位元組
        text = raw.decode("latin-1")
    text = text.replace("\r", "\n").replace("\x0b", "\n").replace("\x00", "")
    lines = [line.strip() for line in text.split("\n")]
    # 單獨的 "*" 是投影片編號 / 日期欄位的佔位字元
    return "\n".join(line for line in lines if line and line != "*")


def _u32(stream: bytes, offset: int) -> int:
    if offset + 4 > len(stream):
        raise PptFormatError("資料截斷")
    return struct.unpack_from("<I", stream, offset)[0]


def _persist_directory(stream: bytes, current_user: bytes) -> tuple[dict[int, int], int]:
    """回傳 (persist id → offset, DocumentContainer 的 persist id)。"""
    # CurrentUserAtom: header(8) size(4) headerToken(4) offsetToCurrentEdit(4)
    edit_offset = _u32(current_user, 16)
    persist: dict[int, int] = {}
    doc_ref: int | None = None
    seen: set[int] = set()

    while edit_offset and edit_offset not in seen and len(seen) < _MAX_EDITS:
        seen.add(edit_offset)
        edit = _read_record(stream, edit_offset)
        if edit.rec_type != RT_USER_EDIT_ATOM:
            raise PptFormatError("找不到 UserEditAtom")
        last_edit = _u32(stream, edit.data_start + 8)
        directory_offset = _u32(stream, edit.data_start + 12)
        if doc_ref is None:
            doc_ref = _u32(stream, edit.data_start + 16)

        directory = _read_record(stream, directory_offset)
        if directory.rec_type != RT_PERSIST_DIRECTORY_ATOM:
            raise PptFormatError("找不到 PersistDirectoryAtom")
        pos = directory.data_start
        while pos + 4 <= directory.data_end:
            entry = _u32(stream, pos)
            pos += 4
            first_id = entry & 0xFFFFF
            count = entry >> 20
            for index in range(count):
                if pos + 4 > directory.data_end:
                    break
                persist.setdefault(first_id + index, _u32(stream, pos))
                pos += 4
        edit_offset = last_edit

    if doc_ref is None:
        raise PptFormatError("沒有 edit 紀錄")
    return persist, doc_ref


def _slide_lists(stream: bytes, document: _Record) -> dict[int, list[_Slide]]:
    """解析 SlideListWithText；回傳 instance → 投影片清單（含 placeholder 文字）。"""
    lists: dict[int, list[_Slide]] = {}
    for child in _children(stream, document):
        if child.rec_type != RT_SLIDE_LIST_WITH_TEXT or child.instance in lists:
            continue
        slides: list[_Slide] = []
        for item in _children(stream, child):
            if item.rec_type == RT_SLIDE_PERSIST_ATOM:
                slides.append(
                    _Slide(
                        persist_ref=_u32(stream, item.data_start),
                        slide_id=_u32(stream, item.data_start + 12),
                    )
                )
            elif item.rec_type in _TEXT_TYPES and slides:
                text = _decode_text(stream, item)
                if text:
                    slides[-1].texts.append(text)
        lists[child.instance] = slides
    return lists


def _container_at(stream: bytes, persist: dict[int, int], ref: int, rec_type: int) -> _Record | None:
    offset = persist.get(ref)
    if offset is None:
        return None
    try:
        record = _read_record(stream, offset)
    except PptFormatError:
        return None
    return record if record.rec_type == rec_type else None


def _notes_id_of(stream: bytes, slide: _Record) -> int:
    for child in _children(stream, slide):
        if child.rec_type == RT_SLIDE_ATOM:
            # SlideAtom: geom(4) rgPlaceholderTypes(8) masterIdRef(4) notesIdRef(4)
            return _u32(stream, child.data_start + 16)
    return 0


def _dedupe(texts: list[str]) -> list[str]:
    result: list[str] = []
    for text in texts:
        if text and text not in result:
            result.append(text)
    return result


def _structured(stream: bytes, current_user: bytes) -> str:
    persist, doc_ref = _persist_directory(stream, current_user)
    document = _container_at(stream, persist, doc_ref, RT_DOCUMENT)
    if document is None:
        raise PptFormatError("找不到 DocumentContainer")
    lists = _slide_lists(stream, document)
    slides = lists.get(0) or []
    if not slides:
        raise PptFormatError("沒有投影片清單")

    notes_by_id: dict[int, list[str]] = {}
    for note in lists.get(2) or []:
        texts = list(note.texts)
        container = _container_at(stream, persist, note.persist_ref, RT_NOTES)
        if container is not None:
            texts.extend(_walk_texts(stream, container))
        notes_by_id[note.slide_id] = _dedupe(texts)

    parts: list[str] = []
    for index, slide in enumerate(slides, start=1):
        texts = list(slide.texts)
        notes: list[str] = []
        container = _container_at(stream, persist, slide.persist_ref, RT_SLIDE)
        if container is not None:
            texts.extend(_walk_texts(stream, container))
            notes = notes_by_id.get(_notes_id_of(stream, container), [])
        lines = [f"--- 投影片 {index} ---", *_dedupe(texts)]
        if notes:
            lines.append("（備忘稿）" + "\n".join(notes))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _flat(stream: bytes) -> str:
    """退路：依出現順序列出所有文字。"""
    texts: list[str] = []
    offset = 0
    while offset + _HEADER.size <= len(stream):
        try:
            record = _read_record(stream, offset)
        except PptFormatError:
            break
        if record.rec_type in _TEXT_TYPES:
            texts.append(_decode_text(stream, record))
        elif record.is_container:
            texts.extend(_walk_texts(stream, record))
        if record.data_end <= offset:
            break
        offset = record.data_end
    texts = _dedupe(texts)
    if not texts:
        return ""
    return "（無法辨識投影片分頁，以下依檔案順序列出所有文字）\n\n" + "\n".join(texts)


def extract_ppt_text(path: Path) -> str:
    import olefile

    if not olefile.isOleFile(str(path)):
        raise PptFormatError("不是 PowerPoint 97-2003 格式的檔案")
    with olefile.OleFileIO(str(path)) as ole:
        if not ole.exists("PowerPoint Document"):
            raise PptFormatError("檔案裡沒有投影片資料")
        stream = ole.openstream("PowerPoint Document").read()
        current_user = (
            ole.openstream("Current User").read() if ole.exists("Current User") else b""
        )
        if ole.exists("EncryptedSummary"):
            raise PptFormatError("簡報有密碼保護，無法抽取文字")

    try:
        return _structured(stream, current_user)
    except (PptFormatError, struct.error):
        return _flat(stream)
