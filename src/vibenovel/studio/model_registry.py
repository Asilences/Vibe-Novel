"""Bounded local model registry; public snapshots never contain credentials."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

from vibenovel.core.files import atomic_write, digest, json_bytes, read_json, safe_path
from vibenovel.core.locking import ConflictError
from .settings import PRESETS, SettingsError, StudioSettings, _settings_lock, _text, load_settings

REGISTRY_PATH = "config/models.local.json"
ROLES = ("planner", "writer", "reviewer", "memory")
MAX_REGISTRY_BYTES = 512 * 1024
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
PROFILE_FIELDS = {"id", "label", "base_url", "model", "allow_loopback", "roles", "context_tokens",
                  "max_output_tokens", "price_input_usd", "price_output_usd"}


def _number(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


def _profile(value, old=None):
    from .providers import ProviderError, _endpoint
    if (not isinstance(value, dict) or not PROFILE_FIELDS <= set(value)
            or set(value) - PROFILE_FIELDS - {"api_key", "clear_api_key", "has_api_key", "available"}):
        raise SettingsError("模型配置字段无效")
    result = {name: value[name] for name in PROFILE_FIELDS}
    if not isinstance(result["id"], str) or not _ID.fullmatch(result["id"]):
        raise SettingsError("模型配置 ID 无效")
    for name, limit in (("label", 120), ("base_url", 4096), ("model", 500)):
        result[name] = _text(result[name], limit)
    if not result["label"] or type(result["allow_loopback"]) is not bool:
        raise SettingsError("模型名称或接口开关无效")
    roles = result["roles"]
    if (not isinstance(roles, list) or not roles or len(roles) > len(ROLES)
            or any(not isinstance(role, str) or role not in ROLES for role in roles)
            or len(set(roles)) != len(roles)):
        raise SettingsError("模型允许角色无效")
    for name, lower, upper in (("context_tokens", 256, 2_000_000), ("max_output_tokens", 1, 131_072)):
        if type(result[name]) is not int or not lower <= result[name] <= upper:
            raise SettingsError("模型上下文或输出上限无效")
    if result["max_output_tokens"] >= result["context_tokens"]:
        raise SettingsError("模型输出上限必须小于上下文容量")
    for name in ("price_input_usd", "price_output_usd"):
        if result[name] is not None and not _number(result[name], 1_000_000):
            raise SettingsError("模型价格需要非负有限数值或留空")
    if result["base_url"]:
        try:
            _endpoint(result["base_url"], result["allow_loopback"])
        except ProviderError:
            raise SettingsError("模型接口地址无效；外部接口需 HTTPS") from None
    supplied = _text(value.get("api_key", ""), 16384)
    clear = value.get("clear_api_key", False)
    if type(clear) is not bool or clear and supplied:
        raise SettingsError("模型密钥操作无效")
    old_key = old.get("api_key", "") if old else ""
    if old and result["base_url"] != old["base_url"] and old_key and not supplied and not clear:
        raise SettingsError("更换模型接口时请填写对应密钥，或明确清除原密钥")
    result["api_key"] = "" if clear else supplied or old_key
    return result


def _public_profile(profile):
    return {**{name: copy.deepcopy(profile[name]) for name in PROFILE_FIELDS},
            "has_api_key": bool(profile["api_key"])}


class ModelRegistry:
    def __init__(self, workspace):
        self.workspace = Path(workspace)

    def _path(self):
        return safe_path(self.workspace, REGISTRY_PATH)

    def _load(self):
        try:
            path = self._path()
            if not path.exists():
                return None, None
            value = read_json(path, max_bytes=MAX_REGISTRY_BYTES)
            if (set(value) != {"schema_version", "revision", "profiles"} or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or type(value["revision"]) is not int or value["revision"] < 1
                    or not isinstance(value["profiles"], list) or not 1 <= len(value["profiles"]) <= 16):
                raise ValueError()
            profiles = [_profile(item) for item in value["profiles"]]
            if len({item["id"] for item in profiles}) != len(profiles):
                raise ValueError()
            value["profiles"] = profiles
            return value, digest(path.read_bytes())
        except (OSError, ValueError, TypeError, KeyError, SettingsError, ConflictError):
            raise SettingsError("无法读取模型注册表；请检查本地配置格式、路径和大小") from None
        except Exception as exc:
            from vibenovel.errors import VibeNovelError
            if isinstance(exc, VibeNovelError):
                raise SettingsError("无法读取模型注册表；请检查本地配置格式、路径和大小") from None
            raise

    def _legacy(self):
        settings = load_settings(self.workspace)
        # The legacy file is never rewritten. File metadata makes an observed
        # A→B→A edit invalidate a queued legacy configuration as well.
        path = safe_path(self.workspace, "config/api.local.json")
        stamp = [path.stat().st_mtime_ns, path.stat().st_size] if path.exists() else None
        profile = {"id": "default_api", "label": "现有 API", "base_url": settings.base_url,
                   "model": settings.model, "allow_loopback": settings.allow_loopback,
                   "roles": list(ROLES), "context_tokens": 1048576, "max_output_tokens": 8192,
                   "price_input_usd": settings.price_input_usd,
                   "price_output_usd": settings.price_output_usd, "api_key": settings.api_key}
        marker = digest(json_bytes({"profile": {key: val for key, val in profile.items() if key != "api_key"},
                                    "file_stamp": stamp}))
        return _profile(profile), marker

    def _public(self):
        data, hashed = self._load()
        legacy = data is None
        if legacy:
            profile, marker = self._legacy()
            profiles, revision = [profile], 0
        else:
            profiles, revision, marker = data["profiles"], data["revision"], None
        return {"hash": hashed, "revision": revision, "migration_required": legacy,
                "legacy_marker": marker, "profiles": [{**_public_profile(item),
                    "available": bool(item["base_url"] and item["model"] and item["api_key"])} for item in profiles],
                "presets": copy.deepcopy(PRESETS)}

    def public(self):
        with _settings_lock(self.workspace):
            return self._public()

    def save(self, payload, expected_hash):
        if (not isinstance(payload, dict) or set(payload) != {"profiles"}
                or not isinstance(payload["profiles"], list) or not 1 <= len(payload["profiles"]) <= 16):
            raise SettingsError("模型注册表需包含 1–16 个独立配置")
        with _settings_lock(self.workspace):
            old, current_hash = self._load()
            if current_hash != expected_hash:
                raise ConflictError("模型配置已变化，请重新读取后保存")
            old_profiles = {item["id"]: item for item in old["profiles"]} if old else {}
            profiles = [_profile(item, old_profiles.get(item.get("id")) if isinstance(item, dict) and isinstance(item.get("id"), str) else None)
                        for item in payload["profiles"]]
            if len({item["id"] for item in profiles}) != len(profiles):
                raise SettingsError("模型配置 ID 不得重复")
            value = {"schema_version": 1, "revision": old["revision"] + 1 if old else 1, "profiles": profiles}
            raw = json_bytes(value)
            if len(raw) > MAX_REGISTRY_BYTES:
                raise SettingsError("模型注册表超过大小上限")
            path = self._path()
            path.parent.mkdir(exist_ok=True)
            atomic_write(path, raw, overwrite=old is not None)
            return self._public()

    def migrate(self, expected_hash):
        with _settings_lock(self.workspace):
            old, hashed = self._load()
            if old is not None or expected_hash is not None or hashed is not None:
                raise ConflictError("模型注册表已存在，请重新读取；不重复迁移")
            profile, _ = self._legacy()
            raw = json_bytes({"schema_version": 1, "revision": 1, "profiles": [profile]})
            path = self._path()
            path.parent.mkdir(exist_ok=True)
            atomic_write(path, raw)
            return self._public()

    def snapshot(self, profile_id):
        public = self.public()
        matches = [item for item in public["profiles"] if item["id"] == profile_id]
        if not matches:
            raise SettingsError("所选模型配置不存在")
        profile = matches[0]
        result = {name: profile[name] for name in PROFILE_FIELDS}
        result.update(registry_revision=public["revision"], registry_hash=public["hash"],
                      legacy_marker=public["legacy_marker"])
        return result


def resolve_snapshot(workspace, snapshot):
    """Resolve credentials internally only after verifying frozen nonsecret config."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str):
        raise SettingsError("任务模型快照无效")
    registry = ModelRegistry(workspace)
    with _settings_lock(registry.workspace):
        data, hashed = registry._load()
        if data is None:
            profile, marker = registry._legacy()
            revision = 0
        else:
            matches = [item for item in data["profiles"] if item["id"] == snapshot["id"]]
            if not matches:
                raise SettingsError("任务绑定的模型配置已移除，请重新提交")
            profile, marker, revision = matches[0], None, data["revision"]
        expected = {name: profile[name] for name in PROFILE_FIELDS}
        expected.update(registry_revision=revision, registry_hash=hashed, legacy_marker=marker)
        if snapshot != expected:
            raise SettingsError("任务绑定的模型配置已变化，请重新提交")
        return StudioSettings(base_url=profile["base_url"], model=profile["model"],
                              api_key=profile["api_key"], allow_loopback=profile["allow_loopback"],
                              max_output_tokens=profile["max_output_tokens"],
                              price_input_usd=profile["price_input_usd"], price_output_usd=profile["price_output_usd"])
