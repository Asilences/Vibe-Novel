"""Read finalized chapter files and export Markdown, without canon writes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from vibenovel.errors import VibeNovelError
from .files import atomic_write, digest, tree_files
from .project import Project


def chapter_files(project: Project) -> list[tuple[int, Path]]:
    result = []
    seen = set()
    root = project.path("chapters")
    for path in tree_files(root):
        if path.name == ".gitkeep":
            continue
        match = re.fullmatch(r"ch([0-9]{3,})\.md", path.name)
        if not match or path.parent != root or int(match.group(1)) < 1:
            raise VibeNovelError(f"正式章节文件命名不规范: {path.relative_to(root)}")
        number = int(match.group(1))
        if number in seen:
            raise VibeNovelError(f"章节编号重复: {number}")
        seen.add(number)
        result.append((number, path))
    return sorted(result, key=lambda pair: pair[0])


def export_markdown(project: Project, *, start: Optional[int] = None, end: Optional[int] = None,
                    output: Optional[str] = None, force: bool = False) -> dict:
    for value in (start, end):
        if value is not None and (type(value) is not int or value < 1):
            raise VibeNovelError("章节范围必须为正整数")
    chapters = chapter_files(project)
    if not chapters:
        raise VibeNovelError("尚无正式章节可导出；草稿不参与导出")
    lower = start if start is not None else 1
    upper = end if end is not None else chapters[-1][0]
    if lower > upper:
        raise VibeNovelError("起始章节不能大于结束章节")
    selected = [(number, path) for number, path in chapters if lower <= number <= upper]
    if len(selected) != upper - lower + 1:
        raise VibeNovelError("所选范围存在缺章或尚未定稿的章节，请缩小导出范围")
    filename = output if output is not None else f"{project.novel_id}.md"
    if not isinstance(filename, str) or "/" in filename or "\\" in filename or not filename.endswith(".md"):
        raise VibeNovelError("导出文件名必须是 exports 内的单个 .md 文件名")
    destination = project.path(f"exports/{filename}")
    sections = [f"# {project.metadata['title']}"]
    for _, path in selected:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise VibeNovelError(f"正式章节不是有效 UTF-8: {path.name}") from exc
        if not text.strip():
            raise VibeNovelError(f"正式章节为空: {path.name}")
        sections.append(text.strip())
    data = ("\n\n---\n\n".join(sections) + "\n").encode("utf-8")
    atomic_write(destination, data, overwrite=force)
    return {"novel_id": project.novel_id, "path": str(destination), "chapter_count": len(selected),
            "start": lower, "end": upper, "sha256": digest(data)}
