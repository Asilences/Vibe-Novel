"""Small filesystem operations with explicit project boundaries.

These checks reject existing links; they are not an OS security sandbox against
another process with the same permissions changing paths concurrently.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from vibenovel.errors import VibeNovelError

DEVICES = {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
DEVICES.update(f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10))


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def safe_path(root: Path, relative: str) -> Path:
    """Resolve a lexical relative path, rejecting links in every existing part."""
    if not isinstance(relative, str) or "\\" in relative:
        raise VibeNovelError("相对路径必须使用 / 分隔，不接受 Windows 绝对/UNC 路径")
    try:
        relative.encode("utf-8")
    except UnicodeError as exc:
        raise VibeNovelError("路径包含无效 Unicode 字符") from exc
    rel = Path(relative)
    if rel.is_absolute() or rel.drive or not rel.parts:
        raise VibeNovelError(f"需要作品内相对路径: {relative}")
    for part in rel.parts:
        if (part in (".", "..") or part.endswith((".", " "))
                or re.search(r'[<>:"|?*\x00-\x1f]', part)
                or part.split(".")[0].lower() in DEVICES):
            raise VibeNovelError(f"不安全的路径: {relative}")
    current = root
    for part in (None, *rel.parts):
        if part is not None:
            current = current / part
        if os.path.lexists(current) and is_link(current):
            raise VibeNovelError(f"拒绝符号链接或 junction: {current}")
    return current


def tree_files(root: Path) -> Iterator[Path]:
    """Walk without ever descending into a linked directory."""
    if not root.exists():
        return
    if is_link(root) or not root.is_dir():
        raise VibeNovelError(f"目录无效或包含链接: {root}")
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if is_link(child):
            raise VibeNovelError(f"拒绝符号链接或 junction: {child}")
        if child.is_dir():
            yield from tree_files(child)
        elif child.is_file():
            yield child
        else:
            raise VibeNovelError(f"不支持的文件类型: {child}")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise VibeNovelError("JSON 含不支持的值、过深结构或无效 Unicode") from exc


def _unique_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise VibeNovelError(f"JSON 字段重复: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise VibeNovelError(f"JSON 不支持非有限数字: {value}")


def read_json(path: Path, *, max_bytes: int = 1024 * 1024) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise VibeNovelError(f"JSON 文件超过大小上限: {path}")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise VibeNovelError(f"JSON 格式错误: {path}") from exc
    if not isinstance(value, dict):
        raise VibeNovelError(f"JSON 顶层必须为对象: {path}")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 100:
            raise VibeNovelError(f"JSON 嵌套超过 100 层: {path}")
        if isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise VibeNovelError(f"JSON 包含无效 Unicode 字符: {path}") from exc
        elif isinstance(item, float) and not math.isfinite(item):
            raise VibeNovelError(f"JSON 包含非有限数字: {path}")
    return value


def cleanup_stage(stage: Path, parent: Path, prefix: str) -> None:
    """Remove only the exact temporary child created for this operation."""
    if not stage.exists():
        return
    if (stage.parent.resolve() != parent.resolve() or not stage.name.startswith(prefix)
            or is_link(stage) or stage.resolve().parent != parent.resolve()):
        raise VibeNovelError(f"暂存路径不在预期目录内，拒绝清理: {stage}")
    shutil.rmtree(stage)


def atomic_write(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    """Publish a complete single file; default uses exclusive hard-link creation."""
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            if os.path.lexists(path) and is_link(path):
                raise VibeNovelError(f"拒绝覆盖链接: {path}")
            # Windows readers and virus scanners can briefly deny replacement
            # of a complete file. Retry only this local publish, never any
            # model request or document mutation. The old file stays intact.
            for attempt in range(6):
                try:
                    if os.path.lexists(path) and is_link(path):
                        raise VibeNovelError(f"拒绝覆盖链接: {path}")
                    os.replace(temp, path)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                        raise
                    time.sleep(min(.02 * 2 ** attempt, .2))
        else:
            try:
                os.link(temp, path)
            except FileExistsError as exc:
                raise VibeNovelError(f"文件已存在，未覆盖: {path}") from exc
    finally:
        temp.unlink(missing_ok=True)
