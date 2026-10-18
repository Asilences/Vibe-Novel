"""Strict, bounded MD/TXT decoding with reproducible source locations."""
from __future__ import annotations

import codecs
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import VibeNovelError

EXTRACTOR_VERSION = "text-v1"
MAX_SOURCE_REFS = 20000
_ENCODINGS = frozenset({"utf-8", "utf-8-sig", "utf-16", "gb18030"})
_LINE_ENDINGS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
# Match the same boundaries as str.splitlines(keepends=True), without allocating
# a list of every line. CRLF must precede the single-character alternatives.
_LINE_PATTERN = re.compile(
    r"[^\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]*"
    r"(?:\r\n|[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]|\Z)"
)


@dataclass(frozen=True)
class ParsedText:
    """Decoded reference material, preserving its original bytes and newlines.

    Paragraph spans include indentation and internal line endings, but exclude
    blank separator lines and the last line's terminating newline. Character
    offsets refer to ``text``, after BOM removal, never to ``original_bytes``.
    """

    original_bytes: bytes
    text: str
    encoding: str
    source_sha256: str
    source_name: str
    kind: str
    source_refs: list[dict]


def _paragraph_refs(text: str) -> list[dict]:
    refs: list[dict] = []
    paragraph: Optional[dict] = None
    number = 0
    for match in _LINE_PATTERN.finditer(text):
        if match.start() == match.end():
            continue  # A trailing newline does not add another logical line.
        number += 1
        offset = match.start()
        line = match.group(0)
        content = line.rstrip(_LINE_ENDINGS)
        if content.strip():
            if paragraph is None:
                if len(refs) >= MAX_SOURCE_REFS:
                    raise VibeNovelError(
                        "原件超过 {} 段来源记录上限，请分文件导入；未截断内容。".format(MAX_SOURCE_REFS)
                    )
                paragraph = {"start_line": number, "end_line": number,
                             "char_start": offset, "char_end": offset + len(content)}
            else:
                paragraph["end_line"] = number
                paragraph["char_end"] = offset + len(content)
        elif paragraph is not None:
            refs.append(paragraph)
            paragraph = None
    if paragraph is not None:
        refs.append(paragraph)
    return refs


def _select_encoding(raw: bytes, requested: Optional[str]) -> str:
    if requested is not None:
        # Deliberately avoid codec aliases: stored names form part of the
        # extraction contract and must have one of these explicit spellings.
        if not isinstance(requested, str) or requested not in _ENCODINGS:
            raise VibeNovelError("编码仅支持 utf-8、utf-8-sig、utf-16 或 gb18030。")
        if requested == "utf-8" and raw.startswith(codecs.BOM_UTF8):
            return "utf-8-sig"
        return requested
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise VibeNovelError("不支持 UTF-32；请将原件明确转换为 UTF-8 或 UTF-16。")
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    return "utf-8"


def parse_text_source(
    source: Path, *, encoding: Optional[str] = None, max_bytes: int = 10485760
) -> ParsedText:
    """Read at most ``max_bytes + 1`` bytes and decode a nonempty MD/TXT file.

    Without an explicit encoding, only UTF-8 and UTF-8/UTF-16 BOMs are
    recognized. Legacy encodings are never guessed or decoded with replacement.
    This extractor writes no files and never interprets source text as commands.
    """
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes <= 0 or max_bytes >= sys.maxsize):
        raise VibeNovelError("max_bytes 必须为可读取范围内的正整数。")
    if encoding is not None and (not isinstance(encoding, str) or encoding not in _ENCODINGS):
        raise VibeNovelError("编码仅支持 utf-8、utf-8-sig、utf-16 或 gb18030。")
    try:
        source = Path(source)
    except (TypeError, ValueError) as exc:
        raise VibeNovelError("请输入有效的 MD/TXT 文件路径。") from exc
    kind = source.suffix.lower().lstrip(".")
    if kind not in {"md", "txt"}:
        raise VibeNovelError("文本导入仅支持 .md 和 .txt 文件。")
    try:
        if not source.is_file():
            raise VibeNovelError("导入来源不是可读取的普通文件。")
        with source.open("rb") as stream:
            original = stream.read(max_bytes + 1)
    except (OSError, ValueError) as exc:
        raise VibeNovelError("无法读取导入原件：{}".format(exc)) from exc
    if len(original) > max_bytes:
        raise VibeNovelError("导入原件超过 {} 字节的读取上限。".format(max_bytes))
    selected = _select_encoding(original, encoding)
    if selected == "utf-16" and not original.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise VibeNovelError("UTF-16 原件必须带字节序 BOM；请明确转换后重试。")
    try:
        text = original.decode(selected, errors="strict")
    except UnicodeError as exc:
        raise VibeNovelError("无法按 {} 严格解码；旧编码请通过 encoding 明确指定。".format(selected)) from exc
    if "\x00" in text:
        raise VibeNovelError("原件含 NUL 字符，可能不是受支持的 MD/TXT 文本。")
    if not text.strip():
        raise VibeNovelError("原件没有非空白正文。")
    return ParsedText(
        original_bytes=original,
        text=text,
        encoding=selected,
        source_sha256=hashlib.sha256(original).hexdigest(),
        source_name=source.name,
        kind=kind,
        source_refs=_paragraph_refs(text),
    )
