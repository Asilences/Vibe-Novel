"""Isolated novel repositories. M0 never migrates or writes v2 canon."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from vibenovel.errors import VibeNovelError
from .files import DEVICES, cleanup_stage, digest, file_digest, is_link, json_bytes, read_json, safe_path, tree_files

GENRES = ("football", "adventure", "campus", "alternate_history")
DIRECTORIES = (
    "imports/original", "bible", "outlines/source", "outlines/proposals", "outlines/approved",
    "drafts", "chapters", "memory/events", "memory/states", "memory/summaries", "memory/threads",
    "approvals", "reviews", "exports", ".runtime",
)
CANONICAL_DIRS = ("bible", "outlines/approved", "chapters", "memory")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_id(novel_id: str) -> None:
    if not isinstance(novel_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", novel_id) or novel_id in DEVICES:
        raise VibeNovelError("作品 ID 需要小写字母开头的 1–64 位 ASCII slug，且不能是 Windows 设备名")


@dataclass(frozen=True)
class Project:
    workspace: Path
    novel_id: str
    metadata: dict

    @property
    def root(self) -> Path:
        return safe_path(self.workspace, f"novels/{self.novel_id}")

    def path(self, relative: str) -> Path:
        return safe_path(self.root, relative)

    def fingerprint(self) -> str:
        files = [self.path("project.json")]
        for directory in CANONICAL_DIRS:
            files.extend(tree_files(self.path(directory)))
        manifest = [{"path": path.relative_to(self.root).as_posix(), "sha256": file_digest(path)}
                    for path in sorted(files, key=lambda path: path.relative_to(self.root).as_posix())]
        return digest(json_bytes(manifest))

    def status(self) -> dict:
        counts = {}
        for name in ("chapters", "drafts", "outlines/approved", "outlines/proposals"):
            counts[name] = sum(1 for path in tree_files(self.path(name)) if path.suffix.lower() == ".md")
        bundles = self.path("imports/original")
        counts["imports"] = sum(1 for path in bundles.iterdir() if re.fullmatch(r"[0-9a-f]{64}", path.name))
        return {"novel_id": self.novel_id, "title": self.metadata["title"], "genre": self.metadata["genre"],
                "fingerprint": self.fingerprint(), "counts": counts,
                "quality_review": "not_run", "phase": "M0", "finalize_available": False}


class ProjectStore:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise VibeNovelError(f"工作目录不存在: {self.workspace}")

    def init(self, novel_id: str, *, genre: str, title: Optional[str] = None) -> Project:
        validate_id(novel_id)
        if genre not in GENRES:
            raise VibeNovelError(f"题材必须为: {', '.join(GENRES)}")
        if title is None:
            title = novel_id
        if not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(char) < 32 for char in title):
            raise VibeNovelError("书名需要 1–200 个字符且不含控制字符")
        try:
            title.encode("utf-8")
        except UnicodeError as exc:
            raise VibeNovelError("书名包含无效 Unicode 字符") from exc
        novels = safe_path(self.workspace, "novels")
        novels.mkdir(exist_ok=True)
        destination = safe_path(self.workspace, f"novels/{novel_id}")
        if os.path.lexists(destination):
            raise VibeNovelError(f"作品目录已存在，未覆盖: {destination}")
        stage = Path(tempfile.mkdtemp(prefix=".init-", dir=novels))
        try:
            metadata = {"schema_version": 1, "novel_id": novel_id, "title": title.strip(),
                        "genre": genre, "created_at": utc_now()}
            for name in DIRECTORIES:
                (stage / name).mkdir(parents=True, exist_ok=True)
            (stage / "project.json").write_bytes(json_bytes(metadata))
            os.rename(stage, destination)
        except FileExistsError as exc:
            raise VibeNovelError(f"作品目录已存在，未覆盖: {destination}") from exc
        finally:
            cleanup_stage(stage, novels, ".init-")
        return self.get(novel_id)

    def get(self, novel_id: str) -> Project:
        validate_id(novel_id)
        root = safe_path(self.workspace, f"novels/{novel_id}")
        metadata_path = safe_path(root, "project.json")
        if not metadata_path.is_file():
            raise VibeNovelError(f"未找到 v3 作品: {novel_id}")
        metadata = read_json(metadata_path)
        if (type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1
                or set(metadata) != {"schema_version", "novel_id", "title", "genre", "created_at"}):
            raise VibeNovelError("作品配置版本或字段无效，需要 schema_version=1")
        if metadata["novel_id"] != novel_id or metadata["genre"] not in GENRES:
            raise VibeNovelError("作品 ID 或题材不匹配")
        if (not isinstance(metadata["title"], str) or not metadata["title"].strip() or len(metadata["title"]) > 200
                or any(ord(char) < 32 for char in metadata["title"])):
            raise VibeNovelError("作品 title 无效")
        try:
            created_at = datetime.fromisoformat(metadata["created_at"])
            if created_at.utcoffset() is None:
                raise ValueError("missing timezone")
        except (TypeError, ValueError) as exc:
            raise VibeNovelError("作品 created_at 需要带时区的 ISO 时间") from exc
        for name in DIRECTORIES:
            if not safe_path(root, name).is_dir():
                raise VibeNovelError(f"作品目录缺失或无效: {name}")
        return Project(self.workspace, novel_id, metadata)

    def list_projects(self) -> list[Project]:
        novels = safe_path(self.workspace, "novels")
        if not novels.exists():
            return []
        projects = []
        for entry in sorted(novels.iterdir(), key=lambda path: path.name):
            if entry.name == ".gitkeep" or entry.name.startswith(".init-"):
                continue
            if is_link(entry):
                raise VibeNovelError(f"作品目录包含链接: {entry}")
            if not entry.is_dir():
                raise VibeNovelError(f"novels 中存在未知文件: {entry.name}")
            projects.append(self.get(entry.name))
        return projects
