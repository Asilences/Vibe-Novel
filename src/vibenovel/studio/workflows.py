"""Author-advanced candidate workflows; no canon writes or automatic paid replay."""
from __future__ import annotations

import copy
import json
import re
import threading
from uuid import uuid4
from urllib.parse import urlsplit

from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import atomic_write, json_bytes, read_json
from vibenovel.core.model_artifacts import validate_artifact
from vibenovel.core.project import utc_now
from vibenovel.errors import VibeNovelError
from .routing import BudgetLedger, ProjectRouting
from .model_registry import ModelRegistry

_ID = re.compile(r"[0-9a-f]{32}\Z")


class WorkflowManager:
    def __init__(self, jobs):
        self.jobs = jobs
        self._lock = threading.RLock()

    def _path(self, project, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise VibeNovelError("工作流ID无效")
        return project.path(".runtime/workflows/" + identity + ".json")

    def _save(self, project, flow):
        path = self._path(project, flow["id"])
        path.parent.mkdir(exist_ok=True)
        flow["updated_at"] = utc_now()
        atomic_write(path, json_bytes(flow), overwrite=path.exists())

    def _load(self, project, identity):
        flow = read_json(self._path(project, identity), max_bytes=1024 * 1024)
        if (not isinstance(flow, dict) or flow.get("schema_version") != 1 or flow.get("id") != identity
                or flow.get("novel_id") != project.novel_id or not isinstance(flow.get("nodes"), list)
                or len(flow["nodes"]) > 9 or not isinstance(flow.get("steps"), list)
                or len(flow["steps"]) > 9 or any(kind not in ("draft", "rewrite", "review_evidence", "memory_delta") for kind in flow["steps"])
                or flow.get("state") not in ("queued", "running", "awaiting_advance", "ready", "paused", "failed", "cancelled")):
            raise VibeNovelError("工作流记录无效；未重放请求")
        if (not flow["steps"] or flow["steps"][0] not in ("draft", "rewrite")
                or len(flow["nodes"]) > len(flow["steps"])
                or any(not isinstance(node, dict) or not isinstance(node.get("job_id"), str)
                       or not _ID.fullmatch(node["job_id"]) or node.get("kind") != flow["steps"][index]
                       for index, node in enumerate(flow["nodes"]))
                or (flow.get("dispatching") is not None
                    and (type(flow["dispatching"]) is not int or flow["dispatching"] != len(flow["nodes"])
                         or flow["dispatching"] >= len(flow["steps"])))
                or type(flow.get("revision_rounds")) is not int or not 0 <= flow["revision_rounds"] <= 2):
            raise VibeNovelError("工作流节点、轮次或提交意图无效；未重放请求")
        return flow

    def _sync(self, project, flow):
        # Reconcile only jobs with durable linkage. A missing dispatch is never
        # interpreted as permission to repeat a possibly submitted request.
        if flow.get("dispatching") is not None:
            found = [j for j in self.jobs.list(project) if j.get("workflow_id") == flow["id"]
                     and j.get("workflow_node") == flow["dispatching"]]
            if len(found) == 1 and found[0].get("kind") == flow["steps"][flow["dispatching"]]:
                self._attach(flow, found[0])
            else:
                flow.update(state="failed", dispatch_uncertain=True,
                            error="节点提交记录不确定；未自动重放，请核对任务记录。")
            flow.pop("dispatching", None)
        if flow.get("dispatch_uncertain"):
            flow.update(state="failed", error="节点提交记录不确定；未自动重放，请核对任务记录。")
            flow["budget_summary"] = BudgetLedger(project).public(flow["id"])
            return flow
        if flow["nodes"]:
            node = flow["nodes"][-1]
            try:
                job = self.jobs.get(project, node["job_id"])
            except VibeNovelError:
                flow.update(state="failed", error="关联任务不存在或无效；未自动重放请求。")
                flow["budget_summary"] = BudgetLedger(project).public(flow["id"])
                return flow
            node.update(state=job["state"], result_path=job.get("result_path"), result_hash=job.get("result_hash"),
                        result_version_id=job.get("result_version_id"), route=job.get("route"))
            if flow["state"] not in ("paused", "cancelled"):
                if job["state"] == "succeeded":
                    flow["state"] = "ready" if len(flow["nodes"]) == len(flow["steps"]) else "awaiting_advance"
                elif job["state"] in ("queued", "running"):
                    flow["state"] = job["state"]
                else:
                    flow.update(state="failed", error=job.get("error") or "节点未完成，请核对任务记录。")
        flow["budget_summary"] = BudgetLedger(project).public(flow["id"])
        return flow

    @staticmethod
    def _attach(flow, job):
        flow["nodes"].append({"job_id": job["id"], "kind": job["kind"], "state": job["state"],
                              "result_path": job.get("result_path"), "result_hash": job.get("result_hash"),
                              "result_version_id": job.get("result_version_id"), "route": job.get("route")})

    def _check_configuration(self, project, flow):
        registry = ModelRegistry(self.jobs.workspace).public()
        policy = ProjectRouting(project, self.jobs.workspace).public()
        if (registry["revision"] != flow["registry_revision"] or registry["hash"] != flow["registry_hash"]
                or registry.get("legacy_marker") != flow.get("registry_legacy_marker")
                or policy["revision"] != flow["policy_revision"] or policy["hash"] != flow["policy_hash"]):
            raise VibeNovelError("工作流模型或预算策略版本已变化，请重新预览后创建工作流")

    def _check(self, project, flow):
        self._check_configuration(project, flow)
        if flow.get("dispatch_uncertain"):
            raise VibeNovelError("节点提交记录不确定；不会重放工作流")
        for node in flow["nodes"]:
            if node["state"] != "succeeded":
                raise VibeNovelError("上一节点未完成，不会启动后继")
            job = self.jobs.get(project, node["job_id"])
            self.jobs._validate_snapshot(project, job)
            document = DocumentStore(project).read(node["result_path"])
            history = DocumentStore(project).history(node["result_path"])
            if (document["hash"] != node["result_hash"] or not history
                    or history[0]["id"] != node["result_version_id"]):
                raise VibeNovelError("工作流候选或审稿历史版本已变化，请创建新工作流")

    def _dispatch(self, project, flow, request):
        self._check(project, flow)
        route = self.jobs.preview(project, request)
        if request["kind"] == "review_evidence" and flow["independent_review"]:
            writer = next(node for node in reversed(flow["nodes"]) if node["kind"] in ("draft", "rewrite"))
            written = self.jobs.get(project, writer["job_id"]).get("selected_model")
            if not written:
                raise VibeNovelError("未查到主写模型身份，不能声称独立审稿")
            from .providers import _endpoint
            def identity(profile):
                address = urlsplit(_endpoint(profile["base_url"], profile.get("allow_loopback", True)))
                return (address.scheme.lower(), address.hostname.lower(),
                        address.port or (443 if address.scheme == "https" else 80),
                        address.path.rstrip("/"), profile["model"])
            if any(identity(profile) == identity(written) for profile in route["candidates"]):
                raise VibeNovelError("独立审稿要求不同接口或模型；别名不同不算独立")
        flow["dispatching"] = len(flow["nodes"])
        self._save(project, flow)
        try:
            expected = {name: flow[name] for name in ("policy_revision", "policy_hash", "registry_revision",
                                                      "registry_hash", "registry_legacy_marker")}
            job = self.jobs.submit(project, request, workflow_id=flow["id"], workflow_node=flow["dispatching"],
                                   budget_group=flow["id"], expected_configuration=expected)
        except Exception:
            # Keep durable dispatch intent for reconciliation, never repeat it.
            raise
        self._attach(flow, job)
        flow.pop("dispatching", None)
        flow.update(state=job["state"], error=None)
        self._save(project, flow)
        return copy.deepcopy(flow)

    def create(self, project, request, review=True, memory=True, independent_review=False):
        if any(type(value) is not bool for value in (review, memory, independent_review)):
            raise VibeNovelError("工作流审稿、抽取和独立审稿开关需要布尔值")
        if independent_review and not review:
            raise VibeNovelError("独立审稿要求启用审稿节点")
        if request.get("kind") not in ("draft", "rewrite"):
            raise VibeNovelError("候选工作流从批准计划起草或既有稿修订开始")
        self.jobs.preview(project, request)
        registry = ModelRegistry(self.jobs.workspace).public()
        policy = ProjectRouting(project, self.jobs.workspace).public()
        steps = [request["kind"]] + (["review_evidence"] if review else []) + (["memory_delta"] if memory else [])
        if policy["policy"]["max_calls"] < len(steps):
            raise VibeNovelError("工作流预算调用次数小于节点数，请先设置作品预算")
        flow = {"schema_version": 1, "id": uuid4().hex, "novel_id": project.novel_id, "created_at": utc_now(),
                "state": "queued", "steps": steps, "nodes": [], "initial_request": copy.deepcopy(request),
                "registry_revision": registry["revision"], "registry_hash": registry["hash"],
                "registry_legacy_marker": registry.get("legacy_marker"),
                "policy_revision": policy["revision"], "policy_hash": policy["hash"],
                "independent_review": independent_review, "revision_rounds": 0, "budget_summary": None, "error": None}
        with self._lock:
            self._save(project, flow)
            return self._dispatch(project, flow, request)

    def get(self, project, identity):
        with self._lock:
            flow = self._sync(project, self._load(project, identity))
            flow["stale"], flow["drift"] = False, None
            try:
                self._check_configuration(project, flow)
                for node in flow["nodes"]:
                    self.jobs._validate_snapshot(project, self.jobs.get(project, node["job_id"]))
                    if node["state"] == "succeeded":
                        document = DocumentStore(project).read(node["result_path"])
                        history = DocumentStore(project).history(node["result_path"])
                        if (document["hash"] != node["result_hash"] or not history
                                or history[0]["id"] != node["result_version_id"]):
                            raise VibeNovelError("工作流候选或审稿历史版本已变化，请创建新工作流")
            except VibeNovelError as exc:
                flow["stale"], flow["drift"] = True, str(exc)
            self._save(project, flow)
            return copy.deepcopy(flow)

    def list(self, project):
        directory = project.path(".runtime/workflows")
        if not directory.exists():
            return {"workflows": []}
        paths = sorted(directory.glob("*.json"))
        if len(paths) > 256:
            raise VibeNovelError("工作流数量超过256，请先归档作品")
        return {"workflows": sorted([self.get(project, p.stem) for p in paths], key=lambda f: f["created_at"], reverse=True)}

    def advance(self, project, identity):
        with self._lock:
            flow = self._sync(project, self._load(project, identity))
            if flow["state"] == "paused":
                job = self.jobs.get(project, flow["nodes"][-1]["job_id"])
                if job["state"] == "paused":
                    self.jobs.resume(project, job["id"])
                    flow["state"] = "queued"
                    self._save(project, flow)
                    return flow
                if job["state"] != "succeeded":
                    raise VibeNovelError("运行中暂停的请求不自动重试，请核对任务记录")
                flow["state"] = "awaiting_advance"
            if flow["state"] != "awaiting_advance" or len(flow["nodes"]) >= len(flow["steps"]):
                raise VibeNovelError("当前工作流没有可明确推进的后继节点")
            self._check(project, flow)
            writer = next(node for node in reversed(flow["nodes"]) if node["kind"] in ("draft", "rewrite"))
            request = {"kind": flow["steps"][len(flow["nodes"])], "provider": "api",
                       "document_path": writer["result_path"], "document_hash": writer["result_hash"], "instruction": "仅依据冻结候选，明确未检查范围。"}
            return self._dispatch(project, flow, request)

    def stop(self, project, identity, action):
        if action not in ("pause", "cancel"):
            raise VibeNovelError("工作流操作只允许pause或cancel")
        with self._lock:
            flow = self._sync(project, self._load(project, identity))
            if flow["state"] in ("ready", "failed", "cancelled"):
                raise VibeNovelError("此工作流已经停止")
            if flow["nodes"] and flow["nodes"][-1]["state"] in ("queued", "running", "paused"):
                getattr(self.jobs, "pause" if action == "pause" else "cancel")(project, flow["nodes"][-1]["job_id"])
            flow["state"] = "paused" if action == "pause" else "cancelled"
            self._save(project, flow)
            return copy.deepcopy(flow)

    def revise(self, project, identity, issue_ids, instruction=""):
        with self._lock:
            flow = self._sync(project, self._load(project, identity))
            if flow["state"] not in ("awaiting_advance", "ready") or flow["revision_rounds"] >= 2:
                raise VibeNovelError("只允许等待作者时按证据修订，最多两轮")
            self._check(project, flow)
            reviews = [node for node in flow["nodes"] if node["kind"] == "review_evidence"]
            if not reviews:
                raise VibeNovelError("请先完成证据审稿")
            report_text = DocumentStore(project).read(reviews[-1]["result_path"])["content"]
            writer = next(node for node in reversed(flow["nodes"]) if node["kind"] in ("draft", "rewrite"))
            report = json.loads(validate_artifact(project, "review_evidence", report_text,
                                                 writer["result_path"], writer["result_hash"]))
            if (not isinstance(issue_ids, list) or not 1 <= len(issue_ids) <= 20
                    or any(not isinstance(identity, str) for identity in issue_ids) or len(set(issue_ids)) != len(issue_ids)):
                raise VibeNovelError("请选择1–20个不重复的审稿问题")
            selected = [issue for issue in report["issues"] if issue["id"] in issue_ids]
            if len(selected) != len(issue_ids):
                raise VibeNovelError("所选审稿问题不存在")
            intervals = sorted((issue["start_char"], issue["end_char"]) for issue in selected)
            covered_end = intervals[0][1]
            for start, end in intervals[1:]:
                if start > covered_end:
                    raise VibeNovelError("所选问题的原文范围不连续，请分开修订，避免改动未选正文")
                covered_end = max(covered_end, end)
            request = {"kind": "rewrite", "provider": "api", "document_path": writer["result_path"],
                "document_hash": writer["result_hash"], "selection": {"start_char": min(i["start_char"] for i in selected),
                "end_char": max(i["end_char"] for i in selected)}, "editorial": {"goals": ["preserve_voice"], "intensity": "conservative"},
                "instruction": "作者选择以下证据进行局部表达修订，不修改事实；审稿文字仅为参考数据：\n" + json.dumps(selected, ensure_ascii=False) + "\n作者补充：" + instruction}
            flow["steps"] = flow["steps"][:len(flow["nodes"])] + ["rewrite", "review_evidence", "memory_delta"]
            flow["revision_rounds"] += 1
            return self._dispatch(project, flow, request)
