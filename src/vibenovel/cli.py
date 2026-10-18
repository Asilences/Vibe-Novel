"""Offline M0 CLI. Successful responses are JSON; errors have nonzero status."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .config import load_config
from .core.export import export_markdown
from .core.imports import import_outline, verify_project
from .core.project import GENRES, ProjectStore
from .errors import VibeNovelError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="VibeNovel v3 M0 — 离线作品管理与保真导入")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--workspace", type=Path, default=Path.cwd(), help="包含 novels 的工作目录（默认当前目录）")
    sub = result.add_subparsers(dest="command", required=True)
    create = sub.add_parser("init", help="创建独立作品，不覆盖旧资料")
    create.add_argument("novel_id")
    create.add_argument("--genre", required=True, choices=GENRES)
    create.add_argument("--title")
    sub.add_parser("list", help="列出 v3 作品")
    sub.add_parser("config", help="查看已校验的离线配置")
    status = sub.add_parser("status", help="作品计数与正式内容指纹")
    status.add_argument("novel_id")
    verify = sub.add_parser("verify", help="校验磁盘完整性，不作文学审查")
    verify.add_argument("novel_id")
    importer = sub.add_parser("import-outline", help="保真导入 MD/TXT 来源，非自动批准大纲")
    importer.add_argument("novel_id")
    importer.add_argument("source", type=Path)
    importer.add_argument("--encoding", choices=("utf-8", "utf-8-sig", "utf-16", "gb18030"))
    exporter = sub.add_parser("export", help="仅导出正式章节 Markdown")
    exporter.add_argument("novel_id")
    exporter.add_argument("--format", choices=("md",), default="md")
    exporter.add_argument("--start", type=int)
    exporter.add_argument("--end", type=int)
    exporter.add_argument("--output", help="exports 中的文件名，不接受路径")
    exporter.add_argument("--force", action="store_true", help="允许替换同名导出文件")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    # Windows redirected streams may default to GBK. JSON and emoji must remain
    # printable after a successful write, without turning success into an error.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parser().parse_args(argv)
    try:
        store = ProjectStore(args.workspace)
        config = load_config(store.workspace)
        exit_code = 0
        if args.command == "config":
            response = config
        elif args.command == "init":
            project = store.init(args.novel_id, genre=args.genre, title=args.title)
            response = {"project": project.metadata, "path": str(project.root)}
        elif args.command == "list":
            response = {"projects": [project.metadata for project in store.list_projects()]}
        else:
            project = store.get(args.novel_id)
            if args.command == "status":
                response = project.status()
            elif args.command == "verify":
                response = verify_project(project)
                exit_code = 0 if response["ok"] else 1
            elif args.command == "import-outline":
                response = import_outline(project, args.source, encoding=args.encoding, max_bytes=config["imports"]["max_bytes"])
            else:
                response = export_markdown(project, start=args.start, end=args.end, output=args.output, force=args.force)
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return exit_code
    except (VibeNovelError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
