"""A private research account with a durable, shared monetary ceiling.

This is deliberately not a novel or a project routing policy. Credentials are
resolved only by the existing provider, never copied into the research ledger.
"""
from __future__ import annotations

import copy
import math
import re
import uuid
from urllib.parse import urlsplit
from dataclasses import replace
from pathlib import Path

from .files import atomic_write, digest, json_bytes, read_json, safe_path
from .locking import ConflictError, project_lock
from .project import utc_now
from vibenovel.errors import VibeNovelError


class _ResearchRunnerLease:
    """Use the same lease as the study/evaluation sender, never an age heuristic."""
    def __init__(self, workspace, relative):
        self.workspace, self.relative = workspace, relative

    def path(self, relative):
        path = safe_path(self.workspace, self.relative + "/" + relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def structured_research_request_options(base_url, model):
    """Protocol options for bounded research JSON, frozen with each preview.

    This applies only to the official Flash endpoint. Compatible endpoints and
    other model families keep their own defaults; never infer their protocol.
    """
    return ({"thinking": "disabled"}
            if urlsplit(base_url).hostname == "api.deepseek.com"
            and model.startswith("deepseek-flash") else {})


class ResearchGateway:
    def __init__(self, workspace, provider_factory=None):
        self.workspace = Path(workspace).resolve()
        self.provider_factory = provider_factory
        self.root = safe_path(self.workspace, "library/research/account")

    def path(self, relative):
        self.root = safe_path(self.workspace, "library/research/account")
        path = safe_path(self.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load(self):
        path = self.path("account.json")
        if not path.exists():
            return {"schema_version": 1, "revision": 0, "config": None, "attempts": {}}
        value = read_json(path, max_bytes=8 * 1024 * 1024)
        if (set(value) != {"schema_version", "revision", "config", "attempts"}
                or value["schema_version"] != 1 or not isinstance(value["attempts"], dict)):
            raise ConflictError("私人研究账本格式无效，未继续付费")
        return value

    def _save(self, value):
        value["revision"] += 1
        raw = json_bytes(value)
        if len(raw) > 8 * 1024 * 1024:
            raise VibeNovelError("私人研究账本超过8MiB，未发起请求")
        atomic_write(self.path("account.json"), raw, overwrite=True)

    @staticmethod
    def _config_hash(config):
        return digest(json_bytes(config))

    def status(self):
        with project_lock(self, timeout=2):
            value = self._load()
            attempts = value["attempts"].values()
            return {"configured": value["config"] is not None, "config": copy.deepcopy(value["config"]),
                    "config_hash": self._config_hash(value["config"]), "calls": len(value["attempts"]),
                    "reserved_cost_usd": sum(item["reserved_cost_usd"] for item in attempts),
                    "reported_cost_usd": sum(item.get("reported_cost_usd", 0) for item in attempts),
                    "uncertain_calls": sum(item["state"] in {"reserved", "unknown"} or item.get("billing_uncertain", False) for item in attempts),
                    "revision": value["revision"],
                    "pending_reconciliations": [self._public_pending(item) for item in value["attempts"].values()
                        if item["state"] in {"reserved", "unknown"}]}

    def _response_bytes(self, attempt_id):
        path = self.path("responses/" + attempt_id + ".json")
        if not path.exists():
            return None
        with path.open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ConflictError("研究缓存超过核对上限，保留原记录")
        return raw

    def _public_pending(self, item):
        raw = self._response_bytes(item["id"])
        return {name: copy.deepcopy(item.get(name)) for name in
                ("id", "state", "model", "created_at", "reserved_cost_usd")} | {
                    "attempt_hash": digest(json_bytes(item)),
                    "cached_response_hash": digest(raw) if raw is not None else None}

    def _reconciliation_owner(self, attempt_id):
        """Find a persisted sender contract; generic/unattributed calls fail closed."""
        owners = []
        for kind, folder in (("study", "library/studies"), ("evaluation", "library/evaluations"),
                             ("synthesis", "library/syntheses")):
            root = safe_path(self.workspace, folder)
            if not root.exists():
                continue
            paths = list(root.glob("*/manifest.json"))
            if len(paths) > 10000:
                raise ConflictError("研究清单过大，未推断请求归属")
            for path in paths:
                record_id = path.parent.name
                if not re.fullmatch(r"[0-9a-f]{32}", record_id):
                    continue
                record = read_json(safe_path(self.workspace, folder + "/" + record_id + "/manifest.json"), max_bytes=8 * 1024 * 1024)
                nodes = record.get("nodes", [])
                if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
                    raise ConflictError("研究执行节点格式无效，未推断请求归属")
                for node in nodes:
                    key = node.get("attempt_key")
                    if isinstance(key, str) and digest(key.encode("utf-8"))[:32] == attempt_id:
                        plan = record.get("plan")
                        planned = plan.get("nodes") if isinstance(plan, dict) else None
                        if (record.get("id") != record_id or not isinstance(record.get("plan"), dict)
                                or digest(json_bytes(record["plan"])) != record.get("plan_hash")
                                or not isinstance(planned, list)
                                or any(not isinstance(item, dict) for item in planned)
                                or [item.get("id") for item in planned] != [item.get("id") for item in nodes]
                                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", node.get("id", ""))):
                            raise ConflictError("研究归属或冻结计划校验失败，保留账本")
                        owners.append({"kind": kind, "id": record_id, "node_id": node["id"], "folder": folder,
                            "runner": folder + ("/.runners/" + record_id if kind == "study" else "/" + record_id + "/runner")})
        if len(owners) != 1:
            raise ConflictError("无法唯一确认原研究执行器，不能将请求标为停止；保留费用与原状态")
        return owners[0]

    def _frozen_reconciliation_preview(self, owner, attempt):
        request_path = safe_path(self.workspace, owner["folder"] + "/" + owner["id"] +
            "/requests/" + owner["node_id"] + ".json")
        if not request_path.exists():
            raise ConflictError("未找到原执行器保存的请求，不能核对原报价；保留费用与状态")
        request = read_json(request_path, max_bytes=8 * 1024 * 1024)
        preview = request.get("preview", {})
        if not isinstance(preview, dict):
            raise ConflictError("原请求冻结预览格式无效，保留账本")
        unhashed = {name: value for name, value in preview.items() if name != "hash"}
        if (not isinstance(request.get("prompt"), str) or not isinstance(request.get("attempt_key"), str)
                or digest(request["attempt_key"].encode("utf-8"))[:32] != attempt["id"]
                or preview.get("hash") != digest(json_bytes(unhashed))
                or preview.get("hash") != attempt.get("preview_hash")
                or preview.get("prompt_hash") != attempt.get("prompt_hash")
                or digest(request["prompt"].encode("utf-8")) != attempt.get("prompt_hash")):
            raise ConflictError("原请求或冻结报价指纹无效，不能核对缓存费用")
        for name in ("price_input_usd", "price_output_usd"):
            amount = preview.get(name)
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0:
                raise ConflictError("原请求价格未知，未改动账本")
        return preview

    def reconcile(self, attempt_id, *, action, expected_attempt_hash, expected_response_hash, reason):
        if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            raise VibeNovelError("研究请求编号无效")
        if action not in ("mark_unknown", "restore_response") or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise VibeNovelError("请选择核对动作并填写1至2000字符的核对说明")
        owner = self._reconciliation_owner(attempt_id)
        # The sender holds its runner lease throughout its network call. Acquire
        # it before the account lock (the same lock order as sending). An old
        # timestamp or a client assertion cannot substitute for this proof.
        with project_lock(_ResearchRunnerLease(self.workspace, owner["runner"]), timeout=0):
            if self._reconciliation_owner(attempt_id) != owner:
                raise ConflictError("原请求归属变化，请重新核对")
            with project_lock(self, timeout=2):
                account = self._load()
                attempt = account["attempts"].get(attempt_id)
                if attempt is None or digest(json_bytes(attempt)) != expected_attempt_hash:
                    raise ConflictError("研究账本已变化，请刷新后核对")
                if attempt["state"] not in ("reserved", "unknown"):
                    raise ConflictError("仅进行中或费用未知的请求可核对，不改写已有成功记录")
                preview = self._frozen_reconciliation_preview(owner, attempt)
                raw = self._response_bytes(attempt_id)
                actual_hash = digest(raw) if raw is not None else None
                if actual_hash != expected_response_hash:
                    raise ConflictError("原响应缓存已变化，请刷新后核对")
                before = copy.deepcopy(attempt)
                if action == "mark_unknown":
                    if raw is not None or attempt["state"] != "reserved":
                        raise ConflictError("已有缓存须核验恢复；未知请求不重复修改")
                    attempt.update(state="unknown", billing_uncertain=True,
                        error="已显式核对原执行器无活动；结果及费用仍未知，保留预留金额，不自动重发")
                else:
                    if raw is None:
                        raise ConflictError("未找到原响应缓存，不能按已完成入账")
                    saved = read_json(self.path("responses/" + attempt_id + ".json"), max_bytes=4 * 1024 * 1024)
                    if (set(saved) != {"text", "usage", "model", "finish_reason"} or not isinstance(saved["text"], str)
                            or len(saved["text"].encode("utf-8")) > 3 * 1024 * 1024
                            or saved["model"] not in (None, attempt.get("model"))
                            or saved["finish_reason"] is not None and not isinstance(saved["finish_reason"], str)
                            or saved["usage"] is not None and not isinstance(saved["usage"], dict)):
                        raise ConflictError("原响应缓存格式或模型无效，保留原费用与状态")
                    if attempt.get("response_hash") not in (None, actual_hash):
                        raise ConflictError("原响应缓存与已存指纹不一致，保留原记录")
                    usage = saved["usage"] or {}
                    valid_usage = all(type(usage.get(key)) is int and usage[key] >= 0
                                      for key in ("prompt_tokens", "completion_tokens"))
                    if any(key in usage and (type(usage[key]) is not int or usage[key] < 0)
                           for key in ("prompt_tokens", "completion_tokens")):
                        raise ConflictError("缓存Token用量无效，未修改费用")
                    attempt.update(state="succeeded", response_hash=actual_hash, completed_at=utc_now(),
                        usage=saved["usage"], finish_reason=saved["finish_reason"], billing_uncertain=not valid_usage)
                    attempt.pop("error", None)
                    if valid_usage:
                        try:
                            reported = (usage["prompt_tokens"] * preview["price_input_usd"] +
                                        usage["completion_tokens"] * preview["price_output_usd"]) / 1_000_000
                        except OverflowError:
                            raise ConflictError("缓存Token用量超出核算范围，未修改费用") from None
                        if not math.isfinite(reported):
                            raise ConflictError("缓存Token费用超出核算范围，未修改费用")
                        attempt["reported_cost_usd"] = reported
                        attempt["reserved_cost_usd"] = max(attempt["reserved_cost_usd"], reported)
                audit_id = uuid.uuid4().hex
                audit = {"id": audit_id, "attempt_id": attempt_id, "action": action, "reason": reason.strip(),
                    "at": utc_now(), "owner": {key: owner[key] for key in ("kind", "id", "node_id")},
                    "before": before, "after_state": attempt["state"], "cached_response_hash": actual_hash,
                    "reserved_cost_usd": attempt["reserved_cost_usd"], "network_requests": 0}
                atomic_write(self.path("reconciliations/" + audit_id + ".json"), json_bytes(audit))
                attempt.setdefault("reconciliations", []).append({"id": audit_id, "hash": digest(json_bytes(audit))})
                self._save(account)
        return self.status()

    def configure(self, *, model_id, max_cost_usd, price_input_usd, price_output_usd,
                  price_source="", max_calls=256, expected_hash=None):
        for amount in (max_cost_usd, price_input_usd, price_output_usd):
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0:
                raise VibeNovelError("研究预算及每百万Token价格必须为已确认的正数")
        if max_cost_usd > 10000 or type(max_calls) is not int or not 1 <= max_calls <= 4096:
            raise VibeNovelError("研究预算或调用次数超过上限")
        from vibenovel.studio.model_registry import ModelRegistry
        profile = ModelRegistry(self.workspace).snapshot(model_id)
        config = {"model_id": model_id, "max_cost_usd": float(max_cost_usd), "max_calls": max_calls,
                  "price_input_usd": float(price_input_usd), "price_output_usd": float(price_output_usd),
                  "price_source": str(price_source)[:2000], "configured_at": utc_now()}
        with project_lock(self, timeout=2):
            value = self._load()
            if expected_hash is not None and expected_hash != self._config_hash(value["config"]):
                raise ConflictError("研究账户配置已变化，请重新读取")
            if any(item["state"] == "reserved" for item in value["attempts"].values()):
                raise ConflictError("尚有研究请求进行中，不修改预算账户")
            if sum(item["reserved_cost_usd"] for item in value["attempts"].values()) > max_cost_usd:
                raise ConflictError("新预算低于已预留/已用费用，未抹去账单")
            value["config"] = config
            self._save(value)
        return self.status()

    def preview(self, prompt, max_output_tokens=4096):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 350000:
            raise VibeNovelError("研究输入需要1至350000字符，不静默截断")
        if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 32000:
            raise VibeNovelError("研究输出上限需要256至32000Token")
        from vibenovel.studio.model_registry import ModelRegistry
        with project_lock(self, timeout=2):
            account = self._load()
            config = account["config"]
        if config is None:
            raise VibeNovelError("请先配置独立研究账户的模型、价格和总预算")
        registry = ModelRegistry(self.workspace)
        public = next((item for item in registry.public()["profiles"] if item["id"] == config["model_id"]), None)
        if public is None or not public["available"] and self.provider_factory is None:
            raise VibeNovelError("研究账户模型未配置完整接口、模型及密钥")
        snapshot = registry.snapshot(config["model_id"])
        # DeepSeek Flash defaults to thinking. Reserve the output for the
        # explicit research JSON, and bind this protocol option to the preview.
        from vibenovel.studio.model_registry import resolve_snapshot
        settings = None if self.provider_factory else resolve_snapshot(self.workspace, snapshot)
        options = (structured_research_request_options(settings.base_url, snapshot["model"])
                   if settings is not None else {})
        tokens = len(prompt.encode("utf-8")) + 2048
        if max_output_tokens > snapshot["max_output_tokens"] or tokens + max_output_tokens > snapshot["context_tokens"]:
            raise VibeNovelError("研究输入或输出超过冻结模型容量，未发送")
        cost = (tokens * config["price_input_usd"] + max_output_tokens * config["price_output_usd"]) / 1_000_000
        value = {"schema_version": 1, "prompt_hash": digest(prompt.encode("utf-8")), "model_id": config["model_id"],
                 "model": snapshot["model"], "model_identity": digest(json_bytes(snapshot)), "model_snapshot": snapshot,
                 "config_hash": self._config_hash(config), "max_output_tokens": max_output_tokens,
                 "estimated_input_tokens": tokens, "estimated_cost_usd": cost,
                 "price_input_usd": config["price_input_usd"], "price_output_usd": config["price_output_usd"],
                 "max_cost_usd": config["max_cost_usd"], "price_source": config["price_source"],
                 "request_options": options}
        value["hash"] = digest(json_bytes(value))
        return value

    def generate(self, frozen_preview, prompt, cancel, idempotency_key):
        from vibenovel.studio.providers import GenerationResult, OpenAICompatibleProvider, ProviderCancelled
        from vibenovel.studio.model_registry import resolve_snapshot
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,120}", idempotency_key):
            raise VibeNovelError("研究请求需要16至120位固定幂等键")
        exact = self.preview(prompt, frozen_preview.get("max_output_tokens"))
        if exact != frozen_preview:
            raise ConflictError("研究模型、价格、预算或实际提示已变化，请重新预览")
        identity = digest(idempotency_key.encode("utf-8"))[:32]
        with project_lock(self, timeout=2):
            account = self._load()
            old = account["attempts"].get(identity)
            if old:
                if old["preview_hash"] != exact["hash"]:
                    raise ConflictError("研究请求键已绑定另一份提示或模型")
                if old["state"] == "succeeded":
                    path = self.path("responses/" + identity + ".json")
                    if digest(path.read_bytes()) != old["response_hash"]:
                        raise ConflictError("已付费研究响应被修改，未重新请求")
                    saved = read_json(path, max_bytes=4 * 1024 * 1024)
                    return GenerationResult(saved["text"], saved.get("usage"), saved.get("model"), saved.get("finish_reason"))
                raise ConflictError("该研究请求已有发送/费用记录，不自动重放；请核对已有结果")
            config = account["config"]
            if self._config_hash(config) != exact["config_hash"]:
                raise ConflictError("研究预算在费用预留前已变化，未发送")
            reserved = sum(item["reserved_cost_usd"] for item in account["attempts"].values())
            if len(account["attempts"]) >= config["max_calls"] or reserved + exact["estimated_cost_usd"] > config["max_cost_usd"]:
                raise VibeNovelError("独立研究账户达到共享总预算或调用上限，未发送")
            if cancel.is_set():
                raise ProviderCancelled("研究已取消，请求未发送", billing="not_sent")
            attempt = {"id": identity, "preview_hash": exact["hash"], "prompt_hash": exact["prompt_hash"],
                       "state": "reserved", "reserved_cost_usd": exact["estimated_cost_usd"],
                       "model_id": exact["model_id"], "model": exact["model"], "created_at": utc_now()}
            account["attempts"][identity] = attempt
            self._save(account)
        try:
            if self.provider_factory:
                provider = self.provider_factory("api")
            else:
                settings = replace(resolve_snapshot(self.workspace, exact["model_snapshot"]),
                                   max_output_tokens=exact["max_output_tokens"])
                provider = OpenAICompatibleProvider(settings=settings, timeout=180, **exact["request_options"])
            response = provider.generate(prompt, cancel)
            if not isinstance(response, GenerationResult) or len(response.text.encode("utf-8")) > 3 * 1024 * 1024:
                raise VibeNovelError("研究响应格式无效或超过保留上限")
        except Exception as exc:
            with project_lock(self, timeout=2):
                account = self._load()
                current = account["attempts"][identity]
                current["state"] = "not_sent" if getattr(exc, "billing", "unknown") == "not_sent" else "unknown"
                if current["state"] == "not_sent":
                    current["reserved_cost_usd"] = 0
                current["error"] = "请求未完成，保留费用，不自动重发"
                self._save(account)
            raise VibeNovelError("研究接口未完成，保留费用与请求状态，不自动重发") from None
        raw = json_bytes({"text": response.text, "usage": response.usage, "model": response.model,
                          "finish_reason": response.finish_reason})
        with project_lock(self, timeout=2):
            path = self.path("responses/" + identity + ".json")
            atomic_write(path, raw, overwrite=False)
            account = self._load()
            current = account["attempts"][identity]
            current.update(state="succeeded", response_hash=digest(raw), completed_at=utc_now(), usage=response.usage,
                           finish_reason=response.finish_reason)
            usage = response.usage or {}
            if all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens")):
                reported = (usage["prompt_tokens"] * exact["price_input_usd"] + usage["completion_tokens"] * exact["price_output_usd"]) / 1_000_000
                current["reported_cost_usd"] = reported
                current["reserved_cost_usd"] = max(current["reserved_cost_usd"], reported)
            else:
                current["billing_uncertain"] = True
            self._save(account)
        return response
