"""Explicit role routing and durable per-workflow budget reservations."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

from vibenovel.core.files import atomic_write, digest, json_bytes, read_json
from vibenovel.core.locking import ConflictError, project_lock
from vibenovel.errors import VibeNovelError
from .model_registry import ModelRegistry, ROLES, resolve_snapshot

POLICY_PATH = ".runtime/model-routing.json"
LEDGER_PATH = ".runtime/model-budget.json"
KIND_ROLES = {"expand_outline": "planner", "build_blueprint": "planner", "generate_story_outline": "planner", "draft": "writer",
              "rewrite": "writer", "review": "reviewer", "review_evidence": "reviewer", "memory_delta": "memory",
              "analyze_reference": "planner", "extract_methods": "planner"}
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,80}")


def default_policy():
    return {"bindings": {role: {"primary": "default_api", "fallbacks": []} for role in ROLES},
            "max_calls": 1, "max_tokens": 1_000_000, "max_cost_usd": None, "max_attempts": 1}


def _validate_budget(value):
    if not isinstance(value, dict) or set(value) != {"max_calls", "max_tokens", "max_cost_usd"}:
        raise VibeNovelError("调用预算格式无效")
    for name, lower, upper in (("max_calls", 1, 32), ("max_tokens", 1, 32_000_000)):
        if type(value[name]) is not int or not lower <= value[name] <= upper:
            raise VibeNovelError("调用次数或 Token 预算无效")
    cost = value["max_cost_usd"]
    if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost <= 1_000_000):
        raise VibeNovelError("美元预算需为非负有限值或留空")
    return copy.deepcopy(value)


def _policy(value):
    if (not isinstance(value, dict) or set(value) != {"bindings", "max_calls", "max_tokens", "max_cost_usd", "max_attempts"}
            or not isinstance(value["bindings"], dict) or set(value["bindings"]) != set(ROLES)):
        raise VibeNovelError("角色路由策略字段无效")
    _validate_budget({name: value[name] for name in ("max_calls", "max_tokens", "max_cost_usd")})
    if type(value["max_attempts"]) is not int or not 1 <= value["max_attempts"] <= 3:
        raise VibeNovelError("每节点最多允许 1–3 次技术尝试")
    for binding in value["bindings"].values():
        if (not isinstance(binding, dict) or set(binding) != {"primary", "fallbacks"}
                or not isinstance(binding["primary"], str) or not _IDENTITY.fullmatch(binding["primary"])
                or not isinstance(binding["fallbacks"], list) or len(binding["fallbacks"]) > 2
                or any(not isinstance(item, str) or not _IDENTITY.fullmatch(item) for item in binding["fallbacks"])
                or len(set([binding["primary"]] + binding["fallbacks"])) != 1 + len(binding["fallbacks"])):
            raise VibeNovelError("角色主备模型绑定无效或重复")
    return copy.deepcopy(value)


class ProjectRouting:
    def __init__(self, project, workspace):
        self.project, self.workspace = project, Path(workspace)

    def _load(self):
        path = self.project.path(POLICY_PATH)
        if not path.exists():
            return {"revision": 0, "policy": default_policy()}, None
        try:
            value = read_json(path, max_bytes=64 * 1024)
            if (set(value) != {"schema_version", "revision", "policy"} or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or type(value["revision"]) is not int or value["revision"] < 1):
                raise ValueError()
            value["policy"] = _policy(value["policy"])
            return value, digest(path.read_bytes())
        except (OSError, ValueError, VibeNovelError):
            raise VibeNovelError("作品模型策略无效，请检查固定策略文件") from None

    def _public(self):
        value, hashed = self._load()
        return {"hash": hashed, "revision": value["revision"], "policy": value["policy"]}

    def public(self):
        self.project.path(".runtime").mkdir(exist_ok=True)
        with project_lock(self.project, timeout=2):
            return self._public()

    def save(self, policy, expected_hash):
        policy = _policy(policy)
        registry = ModelRegistry(self.workspace).public()
        models = {item["id"]: item for item in registry["profiles"]}
        for role, binding in policy["bindings"].items():
            for identity in [binding["primary"]] + binding["fallbacks"]:
                if identity not in models or role not in models[identity]["roles"]:
                    raise VibeNovelError("绑定模型不存在或未允许该角色")
        self.project.path(".runtime").mkdir(exist_ok=True)
        with project_lock(self.project, timeout=2):
            old, hashed = self._load()
            if hashed != expected_hash:
                raise ConflictError("作品模型策略已变化，请重新读取后保存")
            value = {"schema_version": 1, "revision": old["revision"] + 1, "policy": policy}
            atomic_write(self.project.path(POLICY_PATH), json_bytes(value), overwrite=hashed is not None)
            return self._public()

    def preview(self, kind, prompt, overrides=None):
        overrides = {} if overrides is None else overrides
        if not isinstance(overrides, dict) or set(overrides) - {"allow_unconfigured", "group_id"}:
            raise VibeNovelError("任务路由覆盖参数无效")
        role = KIND_ROLES.get(kind)
        if role is None or not isinstance(prompt, str):
            raise VibeNovelError("任务类型或提示格式无效")
        try:
            estimated = len(prompt.encode("utf-8")) + 1024
        except UnicodeError:
            raise VibeNovelError("任务文本包含无效字符") from None
        state = self.public()
        policy = state["policy"]
        registry = ModelRegistry(self.workspace)
        public = registry.public()
        profiles = {item["id"]: item for item in public["profiles"]}
        binding = policy["bindings"][role]
        candidates = []
        for identity in [binding["primary"]] + binding["fallbacks"]:
            profile = profiles.get(identity)
            if profile is None or role not in profile["roles"]:
                raise VibeNovelError("任务绑定的模型不存在或不允许该角色")
            if not profile["available"] and not overrides.get("allow_unconfigured"):
                raise VibeNovelError("任务模型尚未配置完整地址、模型和密钥")
            if estimated + profile["max_output_tokens"] > profile["context_tokens"]:
                raise VibeNovelError("保守 Token 估算超过模型上下文；请减少材料或调整容量")
            snapshot = registry.snapshot(identity)
            if snapshot["registry_revision"] != public["revision"] or snapshot["registry_hash"] != public["hash"]:
                raise ConflictError("模型配置在预览期间变化，请重新预览")
            candidates.append(snapshot)
        budget = {name: policy[name] for name in ("max_calls", "max_tokens", "max_cost_usd")}
        route = {"schema_version": 1, "role": role, "policy_revision": state["revision"],
                 "policy_hash": state["hash"], "registry_revision": public["revision"],
                 "candidates": candidates, "estimated_input_tokens": estimated,
                 "max_output_tokens": candidates[0]["max_output_tokens"], "budget": budget,
                 "max_attempts": min(policy["max_attempts"], len(candidates)),
                 "reason": "作者显式角色绑定；备用仅限已列出的接口与技术故障"}
        if overrides.get("group_id") is not None:
            if not isinstance(overrides["group_id"], str) or not _IDENTITY.fullmatch(overrides["group_id"]):
                raise VibeNovelError("预算流程 ID 无效")
            route["group_id"] = overrides["group_id"]
        return route


class BudgetLedger:
    def __init__(self, project):
        self.project = project

    def _load(self):
        path = self.project.path(LEDGER_PATH)
        if not path.exists():
            return {"schema_version": 1, "groups": {}}
        try:
            value = read_json(path, max_bytes=4 * 1024 * 1024)
            if set(value) != {"schema_version", "groups"} or type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(value["groups"], dict):
                raise ValueError()
            if len(value["groups"]) > 4096:
                raise ValueError()
            for group in value["groups"].values():
                if (not isinstance(group, dict) or set(group) != {"budget", "attempts"}
                        or not isinstance(group["attempts"], dict) or len(group["attempts"]) > 32):
                    raise ValueError()
                _validate_budget(group["budget"])
                for item in group["attempts"].values():
                    if (not isinstance(item, dict) or set(item) != {"tokens", "calls", "status", "cost_usd", "price_input_usd", "price_output_usd"}
                            or type(item.get("tokens")) is not int or item["tokens"] < 0
                            or item.get("calls") not in (0, 1) or type(item.get("calls")) is not int
                            or item.get("status") not in ("reserved", "known", "unknown", "not_sent")
                            or item.get("cost_usd") is not None and (type(item["cost_usd"]) not in (int, float)
                                or not math.isfinite(item["cost_usd"]) or item["cost_usd"] < 0)):
                        raise ValueError()
                    for name in ("price_input_usd", "price_output_usd"):
                        price = item[name]
                        if price is not None and (type(price) not in (int, float) or not math.isfinite(price) or price < 0):
                            raise ValueError()
            return value
        except (OSError, ValueError, TypeError, VibeNovelError):
            raise VibeNovelError("持久预算记录无效；不会自动清空或继续调用") from None

    def _write(self, value):
        raw = json_bytes(value)
        if len(raw) > 4 * 1024 * 1024:
            raise VibeNovelError("预算记录超过上限，请保留记录并新建作品")
        path = self.project.path(LEDGER_PATH)
        atomic_write(path, raw, overwrite=path.exists())

    @staticmethod
    def _summary(group):
        attempts = list(group["attempts"].values())
        return {"calls": sum(item["calls"] for item in attempts),
                "tokens": sum(item["tokens"] for item in attempts),
                "cost_usd": sum(item["cost_usd"] or 0 for item in attempts) if all(item["cost_usd"] is not None and item["status"] in ("known", "not_sent") for item in attempts) else None,
                "reserved_cost_usd": sum(item["cost_usd"] or 0 for item in attempts),
                "uncertain": any(item["status"] in ("reserved", "unknown") or item["calls"] and item["cost_usd"] is None for item in attempts),
                "budget": copy.deepcopy(group["budget"])}

    def reserve(self, group_id, attempt_id, route, candidate):
        for identity in (group_id, attempt_id):
            if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
                raise VibeNovelError("预算流程或尝试 ID 无效")
        budget = _validate_budget(route["budget"])
        input_tokens = route["estimated_input_tokens"]
        output_tokens = candidate["max_output_tokens"]
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
            raise VibeNovelError("预算 Token 估算无效")
        prices = (candidate["price_input_usd"], candidate["price_output_usd"])
        cost = None if any(value is None for value in prices) else (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
        if budget["max_cost_usd"] is not None and cost is None:
            raise VibeNovelError("设置美元硬预算时，所选模型须填写输入和输出价格")
        self.project.path(".runtime").mkdir(exist_ok=True)
        with project_lock(self.project, timeout=2):
            data = self._load()
            if group_id not in data["groups"] and len(data["groups"]) >= 4096:
                raise VibeNovelError("预算流程数量达到本作品上限")
            group = data["groups"].setdefault(group_id, {"budget": budget, "attempts": {}})
            if group["budget"] != budget:
                raise VibeNovelError("同一流程不能改变已冻结预算")
            if attempt_id in group["attempts"]:
                raise VibeNovelError("调用预算已预留，拒绝重复发送")
            if len(group["attempts"]) >= 32:
                raise VibeNovelError("本流程已保留32次尝试记录，未发送请求；请保留记录并新建明确流程")
            summary = self._summary(group)
            if summary["calls"] + 1 > budget["max_calls"] or summary["tokens"] + input_tokens + output_tokens > budget["max_tokens"]:
                raise VibeNovelError("调用次数或 Token 预算不足，未发送请求")
            if budget["max_cost_usd"] is not None and summary["reserved_cost_usd"] + cost > budget["max_cost_usd"]:
                raise VibeNovelError("美元预算不足，未发送请求")
            group["attempts"][attempt_id] = {"calls": 1, "tokens": input_tokens + output_tokens,
                "cost_usd": cost, "status": "reserved", "price_input_usd": prices[0], "price_output_usd": prices[1]}
            self._write(data)
            return self._summary(group)

    def settle(self, group_id, attempt_id, usage=None, status="unknown"):
        if status not in ("known", "unknown", "not_sent"):
            raise VibeNovelError("预算结算状态无效")
        self.project.path(".runtime").mkdir(exist_ok=True)
        with project_lock(self.project, timeout=2):
            data = self._load()
            group = data["groups"].get(group_id)
            if not group or attempt_id not in group["attempts"]:
                raise VibeNovelError("未找到调用预算预留")
            record = group["attempts"][attempt_id]
            if record["status"] not in ("reserved", "unknown"):
                return self._summary(group)
            if status == "not_sent":
                record.update(calls=0, tokens=0, cost_usd=0, status=status)
            else:
                counts = usage if isinstance(usage, dict) else {}
                valid = {key: value for key, value in counts.items() if key in ("prompt_tokens", "completion_tokens", "total_tokens") and type(value) is int and value >= 0}
                if status == "known" and "prompt_tokens" in valid and "completion_tokens" in valid:
                    record["tokens"] = max(valid["prompt_tokens"] + valid["completion_tokens"], valid.get("total_tokens", 0))
                    prices = (record["price_input_usd"], record["price_output_usd"])
                    record["cost_usd"] = None if None in prices else (valid["prompt_tokens"] * prices[0] + valid["completion_tokens"] * prices[1]) / 1_000_000
                    record["status"] = "known"
                elif status == "known" and "total_tokens" in valid:
                    record["tokens"] = valid["total_tokens"]
                    record["status"] = "unknown"  # no split: keep conservative monetary reservation
                else:
                    record["status"] = "unknown"  # retain possible charges across restart
            self._write(data)
            return self._summary(group)

    def public(self, group_id):
        self.project.path(".runtime").mkdir(exist_ok=True)
        with project_lock(self.project, timeout=2):
            data = self._load()
            group = data["groups"].get(group_id)
            return None if group is None else self._summary(group)
