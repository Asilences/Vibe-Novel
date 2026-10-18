"""Reloadable local API settings. Never return this object from an HTTP route."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from vibenovel.core.files import safe_path
from vibenovel.errors import VibeNovelError

SETTINGS_PATH = "config/api.local.json"
MAX_SETTINGS_BYTES = 64 * 1024


class SettingsError(VibeNovelError):
    """Messages intentionally contain no parsed field names or source contents."""


@dataclass(frozen=True)
class StudioSettings:
    default_provider: str = "api"
    base_url: str = field(default="", repr=False)
    model: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    allow_loopback: bool = False


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite")


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise SettingsError("本地 API 配置的字段类型或长度无效")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise SettingsError("本地 API 配置包含无效字符") from None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SettingsError("本地 API 配置包含无效控制字符")
    return value.strip()


def load_settings(workspace: Path, environment: Optional[Mapping[str, str]] = None) -> StudioSettings:
    """Load a fixed, bounded, non-linked file, then overlay each explicit env field.

    Even when environment fields are complete, an invalid existing file fails
    closed. An explicit empty environment value clears its local-file field.
    Missing/blank endpoint never silently becomes another provider's endpoint.
    """
    env = os.environ if environment is None else environment
    value = None
    present = False
    try:
        path = safe_path(Path(workspace), SETTINGS_PATH)
        if path.exists():
            present = True
            if not path.is_file():
                raise ValueError("not file")
            with path.open("rb") as stream:
                raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ValueError("oversize")
            value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique,
                               parse_constant=_reject_constant)
    except (OSError, VibeNovelError, ValueError, UnicodeError, RecursionError):
        raise SettingsError("无法读取本地 API 配置；请检查固定配置文件的路径、UTF-8 JSON 格式和大小") from None
    provider, api = "api", {"base_url": "", "model": "", "api_key": ""}
    if present:
        if (not isinstance(value, dict) or set(value) != {"default_provider", "api"}
                or value["default_provider"] not in ("api", "codex") or not isinstance(value["api"], dict)):
            raise SettingsError("本地 API 配置结构、字段或默认执行方式无效")
        provider, api = value["default_provider"], value["api"]
        required = {"base_url", "model", "api_key"}
        if not required <= set(api) or set(api) - required - {"allow_loopback"}:
            raise SettingsError("本地 API 配置结构或字段无效")
        if "allow_loopback" in api and type(api["allow_loopback"]) is not bool:
            raise SettingsError("本地 API 配置的本机接口开关需要布尔值")
        # Validate file fields even if overridden by an environment value.
        for name, limit in (("base_url", 4096), ("model", 500), ("api_key", 16384)):
            _text(api[name], limit)
    resolved = {}
    for name, variable, limit in (("base_url", "VIBENOVEL_API_BASE", 4096),
                                  ("model", "VIBENOVEL_API_MODEL", 500),
                                  ("api_key", "VIBENOVEL_API_KEY", 16384)):
        resolved[name] = _text(env[variable] if variable in env else api[name], limit)
    loopback = api.get("allow_loopback", False)
    if "VIBENOVEL_ALLOW_LOOPBACK_API" in env:
        switch = env["VIBENOVEL_ALLOW_LOOPBACK_API"]
        if switch not in ("", "0", "1"):
            raise SettingsError("API 环境配置的本机接口开关需要 0 或 1")
        loopback = switch == "1"
    return StudioSettings(default_provider=provider, allow_loopback=loopback, **resolved)
