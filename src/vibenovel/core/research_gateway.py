"""A private research account with a durable, shared monetary ceiling.

This is deliberately not a novel or a project routing policy. Credentials are
resolved only by the existing provider, never copied into the research ledger.
"""
from __future__ import annotations

import copy
import math
import re
from urllib.parse import urlsplit
from dataclasses import replace
from pathlib import Path

from .files import atomic_write, digest, json_bytes, read_json, safe_path
from .locking import ConflictError, project_lock
from .project import utc_now
from vibenovel.errors import VibeNovelError


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
                    "revision": value["revision"]}

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
