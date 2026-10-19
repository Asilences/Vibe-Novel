"""Reloadable local API settings. Never return this object from an HTTP route."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from types import SimpleNamespace

from vibenovel.core.files import atomic_write, digest, json_bytes, safe_path
from vibenovel.core.locking import ConflictError, project_lock
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
    max_output_tokens: Optional[int] = None


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
        # Read legacy files without re-enabling the retired interactive adapter.
        api = value["api"]
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


PRESETS = [
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com",
     "models": [{"id": "deepseek-flash", "label": "DeepSeek Flash"},
                {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"}]},
    {"id": "mimo", "label": "小米 MiMo", "base_url": "https://api.xiaomimimo.com/v1",
     "models": [{"id": "mimo-v2.6-pro", "label": "MiMo V2.6 Pro"},
                {"id": "mimo-v2.6-flash", "label": "MiMo V2.6 Flash"}]},
    {"id": "glm", "label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": [{"id": "glm-5.3", "label": "GLM 5.3"}]},
    {"id": "doubao", "label": "豆包 / 火山方舟", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "models": []},
    {"id": "custom", "label": "自定义兼容接口", "base_url": "", "models": []},
]
_ENV_FIELDS = {"base_url": "VIBENOVEL_API_BASE", "model": "VIBENOVEL_API_MODEL",
               "api_key": "VIBENOVEL_API_KEY", "allow_loopback": "VIBENOVEL_ALLOW_LOOPBACK_API"}


def _settings_lock(workspace):
    runtime = safe_path(workspace, ".runtime")
    runtime.mkdir(exist_ok=True)
    owner = SimpleNamespace(path=lambda relative: safe_path(workspace, relative))
    return project_lock(owner, timeout=2)


def _current_hash(workspace):
    path = safe_path(workspace, SETTINGS_PATH)
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(MAX_SETTINGS_BYTES + 1)
    if len(raw) > MAX_SETTINGS_BYTES:
        raise SettingsError("本地 API 配置超过大小上限")
    return digest(raw)


def _public_settings(workspace, environment):
    # Expose the file's editable fields, never either source's credentials.
    settings = load_settings(workspace, {})
    if settings.base_url:
        from .providers import ProviderError, _endpoint
        try:
            _endpoint(settings.base_url, settings.allow_loopback)
        except ProviderError:
            raise SettingsError("本地 API 地址无效，请在配置文件中修正后重新打开设置") from None
    return {"hash": _current_hash(workspace), "default_provider": "api",
            "api": {"base_url": settings.base_url, "model": settings.model,
                    "has_api_key": bool(settings.api_key), "allow_loopback": settings.allow_loopback},
            "environment_overrides": [field for field, variable in _ENV_FIELDS.items() if variable in environment],
            "presets": PRESETS}


def public_settings(workspace: Path, environment: Optional[Mapping[str, str]] = None) -> dict:
    env = os.environ if environment is None else environment
    with _settings_lock(workspace):
        return _public_settings(workspace, env)


def save_settings(workspace: Path, api: dict, expected_hash: Optional[str],
                  environment: Optional[Mapping[str, str]] = None) -> dict:
    """Update one bounded secret file atomically; blank key means retain it.

    This is a local preferences write, not a model invocation or story approval.
    Errors and return values must never contain the submitted credential.
    """
    import re
    from .providers import ProviderError, _endpoint

    if expected_hash is not None and (not isinstance(expected_hash, str)
                                      or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise SettingsError("配置版本无效，请重新打开设置")
    if (not isinstance(api, dict) or not {"base_url", "model", "allow_loopback"} <= set(api)
            or set(api) - {"base_url", "model", "api_key", "clear_api_key", "allow_loopback"}
            or type(api["allow_loopback"]) is not bool
            or type(api.get("clear_api_key", False)) is not bool):
        raise SettingsError("本地 API 设置字段无效")
    base = _text(api["base_url"], 4096)
    model = _text(api["model"], 500)
    submitted_key = _text(api.get("api_key", ""), 16384)
    clear = api.get("clear_api_key", False)
    if clear and submitted_key:
        raise SettingsError("请分别执行设置密钥或清除密钥")
    if base:
        try:
            _endpoint(base, api["allow_loopback"])
        except ProviderError:
            raise SettingsError("API 地址无效；外部接口需 HTTPS，本地接口需显式启用") from None
    env = os.environ if environment is None else environment
    with _settings_lock(workspace):
        current = load_settings(workspace, {})
        if _current_hash(workspace) != expected_hash:
            raise ConflictError("API 设置已变化，请重新读取后保存")
        # A key belongs to its endpoint. Never silently carry it to a new host.
        if base != current.base_url and current.api_key and not submitted_key and not clear:
            raise SettingsError("更换 API 地址时请填写对应密钥，或明确清除原密钥")
        key = "" if clear else submitted_key or current.api_key
        value = {"default_provider": "api", "api": {"base_url": base, "model": model,
                 "api_key": key, "allow_loopback": api["allow_loopback"]}}
        raw = json_bytes(value)
        if len(raw) > MAX_SETTINGS_BYTES:
            raise SettingsError("本地 API 配置超过大小上限")
        path = safe_path(workspace, SETTINGS_PATH)
        path.parent.mkdir(exist_ok=True)
        # Includes external edits observed after validation; same-user races
        # outside this application are not an OS security boundary.
        if _current_hash(workspace) != expected_hash:
            raise ConflictError("API 设置已变化，请重新读取后保存")
        atomic_write(path, raw, overwrite=expected_hash is not None)
        return _public_settings(workspace, env)
