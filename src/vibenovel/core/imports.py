"""Immutable SOURCE bundles, atomically published as one directory."""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from vibenovel.errors import VibeNovelError
from vibenovel.importers.text import EXTRACTOR_VERSION, parse_text_source
from .files import cleanup_stage, digest, is_link, json_bytes, read_json, tree_files
from .project import Project, utc_now

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 100 * 1024 * 1024
METADATA_KEYS = {
    "schema_version", "novel_id", "import_id", "original_filename", "original_file", "source_sha256",
    "source_bytes", "extracted_sha256", "encoding", "kind", "extractor_version", "imported_at",
    "provenance", "trust", "canonical", "source_refs",
}


def verify_bundle(project: Project, import_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", import_id):
        raise VibeNovelError("导入 ID 必须为完整小写 SHA256")
    relative = f"imports/original/{import_id}"
    bundle = project.path(relative)
    metadata = read_json(project.path(f"{relative}/metadata.json"), max_bytes=MAX_METADATA_BYTES)
    if (set(metadata) != METADATA_KEYS or type(metadata.get("schema_version")) is not int
            or metadata["schema_version"] != 1):
        raise VibeNovelError(f"导入 metadata 版本或字段错误: {import_id}")
    if metadata["novel_id"] != project.novel_id or metadata["import_id"] != import_id or metadata["source_sha256"] != import_id:
        raise VibeNovelError(f"导入作品 ID 或原件 SHA256 不匹配: {import_id}")
    if (metadata["provenance"] != "SOURCE" or metadata["trust"] != "untrusted"
            or metadata["canonical"] is not False or metadata["extractor_version"] != EXTRACTOR_VERSION):
        raise VibeNovelError(f"来源/信任/抽取器标记不合法: {import_id}")
    kind = metadata["kind"]
    if kind not in ("md", "txt") or metadata["original_file"] != f"original.{kind}":
        raise VibeNovelError(f"原件文件名不合法: {import_id}")
    original_name = metadata["original_filename"]
    if (not isinstance(original_name, str) or not original_name or "/" in original_name or "\\" in original_name
            or any(ord(char) < 32 for char in original_name)):
        raise VibeNovelError(f"来源文件名不合法: {import_id}")
    try:
        if datetime.fromisoformat(metadata["imported_at"]).utcoffset() is None:
            raise ValueError("missing timezone")
    except (TypeError, ValueError) as exc:
        raise VibeNovelError(f"导入时间不合法: {import_id}") from exc
    actual_files = {path.relative_to(bundle).as_posix() for path in tree_files(bundle)}
    if (actual_files != {f"original.{kind}", "extracted.md", "metadata.json"}
            or {entry.name for entry in bundle.iterdir()} != actual_files):
        raise VibeNovelError(f"导入 bundle 文件布局错误: {import_id}")
    source = project.path(f"{relative}/original.{kind}")
    parsed = parse_text_source(source, encoding=metadata["encoding"], max_bytes=MAX_SOURCE_BYTES)
    if parsed.source_sha256 != import_id:
        raise VibeNovelError(f"原件 SHA256 校验失败: {import_id}")
    if type(metadata["source_bytes"]) is not int or metadata["source_bytes"] != len(parsed.original_bytes):
        raise VibeNovelError(f"原件大小记录错误: {import_id}")
    expected = parsed.text.encode("utf-8")
    extracted = project.path(f"{relative}/extracted.md")
    with extracted.open("rb") as stream:
        actual = stream.read(len(expected) + 1)
    if actual != expected or metadata["extracted_sha256"] != digest(expected):
        raise VibeNovelError(f"抽取文本 SHA256 校验失败: {import_id}")
    if json_bytes(metadata["source_refs"]) != json_bytes(parsed.source_refs):
        raise VibeNovelError(f"来源区间校验失败: {import_id}")
    return metadata


def import_outline(project: Project, source: Path, *, encoding: Optional[str] = None, max_bytes: int = 10485760) -> dict:
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SOURCE_BYTES:
        raise VibeNovelError("导入大小上限需要 1–104857600 字节")
    parsed = parse_text_source(source, encoding=encoding, max_bytes=max_bytes)
    import_id = parsed.source_sha256
    parent = project.path("imports/original")
    destination = project.path(f"imports/original/{import_id}")

    def existing() -> dict:
        previous = verify_bundle(project, import_id)
        if previous["encoding"] != parsed.encoding:
            raise VibeNovelError("相同原件已用不同编码导入，不允许静默重解释")
        return {"novel_id": project.novel_id, "import_id": import_id, "reused": True, "metadata": previous}

    if destination.exists():
        return existing()
    metadata = {
        "schema_version": 1, "novel_id": project.novel_id, "import_id": import_id,
        "original_filename": parsed.source_name, "original_file": f"original.{parsed.kind}",
        "source_sha256": import_id, "source_bytes": len(parsed.original_bytes),
        "extracted_sha256": digest(parsed.text.encode("utf-8")), "encoding": parsed.encoding,
        "kind": parsed.kind, "extractor_version": EXTRACTOR_VERSION, "imported_at": utc_now(),
        "provenance": "SOURCE", "trust": "untrusted", "canonical": False, "source_refs": parsed.source_refs,
    }
    encoded_metadata = json_bytes(metadata)
    if len(encoded_metadata) > MAX_METADATA_BYTES:
        raise VibeNovelError("来源区间 metadata 超过 8 MiB，请将大纲分文件导入")
    stage = Path(tempfile.mkdtemp(prefix=".import-", dir=parent))
    try:
        for name, data in ((f"original.{parsed.kind}", parsed.original_bytes),
                           ("extracted.md", parsed.text.encode("utf-8")), ("metadata.json", encoded_metadata)):
            with (stage / name).open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        # Recheck paths after preparing output. Does not claim same-user isolation.
        destination = project.path(f"imports/original/{import_id}")
        try:
            os.rename(stage, destination)
        except OSError:
            if destination.exists():
                return existing()
            raise
    finally:
        cleanup_stage(stage, parent, ".import-")
    return {"novel_id": project.novel_id, "import_id": import_id, "reused": False, "metadata": metadata}


def verify_project(project: Project) -> dict:
    findings = []
    count = 0
    # Walk every project directory, including candidates, before trusting counts.
    try:
        list(tree_files(project.root))
    except (VibeNovelError, OSError) as exc:
        findings.append({"severity": "ERROR", "message": str(exc)})
    for entry in sorted(project.path("imports/original").iterdir(), key=lambda path: path.name):
        try:
            if is_link(entry):
                raise VibeNovelError(f"导入目录中存在链接: {entry.name}")
            if not re.fullmatch(r"[0-9a-f]{64}", entry.name) or not entry.is_dir():
                raise VibeNovelError(f"发现未知或未完成的导入: {entry.name}")
            verify_bundle(project, entry.name)
            count += 1
        except (VibeNovelError, OSError) as exc:
            findings.append({"severity": "ERROR", "path": entry.name, "message": str(exc)})
    try:
        from .export import chapter_files
        chapters = chapter_files(project)
        if any(number != expected for expected, (number, _) in enumerate(chapters, start=1)):
            findings.append({"severity": "ERROR", "message": "正式章节存在缺章，请确认遗漏或尚未定稿"})
        for _, chapter in chapters:
            try:
                text = chapter.read_text(encoding="utf-8-sig")
                if not text.strip():
                    raise VibeNovelError(f"正式章节为空: {chapter.name}")
            except (VibeNovelError, UnicodeError, OSError) as exc:
                findings.append({"severity": "ERROR", "path": chapter.name, "message": str(exc)})
    except (VibeNovelError, OSError) as exc:
        findings.append({"severity": "ERROR", "message": str(exc)})
    try:
        fingerprint = project.fingerprint()
    except (VibeNovelError, OSError) as exc:
        fingerprint = None
        findings.append({"severity": "ERROR", "message": str(exc)})
    return {"novel_id": project.novel_id, "ok": not findings, "checked_imports": count,
            "fingerprint": fingerprint, "findings": findings, "quality_review": "not_run"}
