"""Versioned, offline configuration. No environment values or API keys read."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .core.files import read_json, safe_path
from .errors import VibeNovelError

DEFAULT_CONFIG = {
    "schema_version": 1,
    "execution": {
        "mode": "codex_interactive",
        "approval_policy": "human_for_L3",
        "plan_approval": "per_chapter",
        "default_max_draft_chapters": 1,
    },
    "imports": {"max_bytes": 10485760},
}


def load_config(workspace: Path) -> dict:
    path = safe_path(workspace, "config/app.json")
    value = read_json(path) if path.exists() else deepcopy(DEFAULT_CONFIG)
    if set(value) != set(DEFAULT_CONFIG) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise VibeNovelError("app.json 不支持的 schema_version 或字段；需要 schema_version=1")
    execution = value.get("execution")
    imports = value.get("imports")
    if not isinstance(execution, dict) or set(execution) != set(DEFAULT_CONFIG["execution"]):
        raise VibeNovelError("execution 配置字段不完整或含未知字段")
    if execution["mode"] != "codex_interactive":
        raise VibeNovelError("M0 仅支持离线 codex_interactive 工作目录，API 适配器尚未实现")
    if execution["approval_policy"] != "human_for_L3" or execution["plan_approval"] != "per_chapter":
        raise VibeNovelError("M0 保留 L3 人工批准与逐章计划批准")
    count = execution["default_max_draft_chapters"]
    if type(count) is not int or count < 1 or count > 100:
        raise VibeNovelError("default_max_draft_chapters 必须是 1–100 的整数")
    if not isinstance(imports, dict) or set(imports) != {"max_bytes"}:
        raise VibeNovelError("imports 配置需要且仅允许 max_bytes")
    limit = imports["max_bytes"]
    if type(limit) is not int or not 1 <= limit <= 104857600:
        raise VibeNovelError("imports.max_bytes 必须是 1–104857600 的整数")
    return value
