"""Author decisions bound to exact single-file proposals and canonical revisions."""
from __future__ import annotations

import copy
import re
from typing import Optional
from uuid import uuid4

from vibenovel.errors import VibeNovelError
from .documents import (ConflictError, DocumentStore, _Storage, _check_expected,
                        HASH, _content_bytes, _decode, _kind, _provenance, _read_raw, _snapshot)
from .files import digest, json_bytes
from .imports import verify_bundle
from .project import Project, utc_now

IMMUTABLE_FIELDS = (
    "id", "novel_id", "target", "before", "after", "before_hash", "after_hash", "reason", "impact",
    "base_revision", "base_fingerprint", "created_at", "provenance",
)


def _proposal_hash(proposal: dict) -> str:
    try:
        fields = {key: proposal[key] for key in IMMUTABLE_FIELDS}
        # Old records intentionally retain their original hash. New dependency
        # maps (including an explicit empty map) are part of the signed content.
        if "dependencies" in proposal:
            fields["dependencies"] = proposal["dependencies"]
        if "dependency_versions" in proposal:
            fields["dependency_versions"] = proposal["dependency_versions"]
        return digest(json_bytes(fields))
    except KeyError as exc:
        raise ConflictError("提案字段缺失，已拒绝操作") from exc


def _check_dependencies(project: Project, dependencies: dict, *, current: bool = True) -> None:
    """Validate paths without acquiring another Studio lock."""
    if not isinstance(dependencies, dict) or len(dependencies) > 101:
        raise VibeNovelError("提案 dependencies 需要最多 101 项的路径/SHA256 对象")
    for path, expected in dependencies.items():
        kind = _kind(project, path)
        if not isinstance(expected, str) or not HASH.fullmatch(expected):
            raise VibeNovelError("提案依赖需要完整 SHA256")
        if current:
            if kind == "source":
                verify_bundle(project, path.split("/")[2])
            try:
                _check_expected(_read_raw(project, path), expected)
            except ConflictError as exc:
                raise ConflictError(f"提案依赖已变化，请重新生成或创建提案: {path}") from exc


def _latest_applied_id(state: dict, path: str) -> Optional[str]:
    """Use the committed revision, never UUID/dictionary order, as chronology."""
    applied = []
    for proposal_id, proposal in state["proposals"].items():
        if proposal.get("target") != path or proposal.get("state") != "applied":
            continue
        revision = proposal.get("applied_revision")
        if type(revision) is not int or revision < 1:
            raise ConflictError("已应用提案的版本记录无效")
        applied.append((revision, proposal_id))
    return max(applied)[1] if applied else None


def _dependency_versions(state: dict, dependencies: dict) -> dict[str, str]:
    """Bind observed Studio versions, so an app A -> B -> A is not a revival."""
    result = {}
    for path in dependencies:
        history = state["history"].get(path, [])
        result[path] = digest(json_bytes({"history": history[-1]["id"] if history else None,
                                         "approval": _latest_applied_id(state, path)}))
    return result


def _check_proposal_dependencies(project: Project, state: dict, proposal: dict) -> None:
    dependencies = proposal.get("dependencies", {})
    _check_dependencies(project, dependencies)
    if ("dependency_versions" in proposal
            and proposal["dependency_versions"] != _dependency_versions(state, dependencies)):
        raise ConflictError("提案依赖存在更新的已观测版本，请重新生成或创建提案")


class ApprovalStore:
    def __init__(self, project: Project):
        self.project = project
        self.storage = _Storage(project)

    def _get(self, state: dict, proposal_id: str) -> dict:
        if not isinstance(proposal_id, str) or not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
            raise VibeNovelError("提案 ID 无效")
        proposal = state["proposals"].get(proposal_id)
        if proposal is None:
            raise VibeNovelError("未找到此作品的提案")
        if (proposal.get("novel_id") != self.project.novel_id or proposal.get("id") != proposal_id
                or proposal.get("content_hash") != _proposal_hash(proposal)
                or proposal.get("state") not in ("pending", "approved", "rejected", "applied")):
            raise ConflictError("提案身份、内容 hash 或状态无效，已拒绝操作")
        if _kind(self.project, proposal["target"]) != "canonical":
            raise ConflictError("提案目标不是正式内容")
        if "dependencies" in proposal:
            _check_dependencies(self.project, proposal["dependencies"], current=False)
        if "dependency_versions" in proposal:
            versions = proposal["dependency_versions"]
            if (not isinstance(versions, dict) or set(versions) != set(proposal.get("dependencies", {}))
                    or any(not isinstance(value, str) or not HASH.fullmatch(value) for value in versions.values())):
                raise ConflictError("提案依赖版本无效")
        if (digest(_content_bytes(proposal["target"], proposal["after"])) != proposal["after_hash"]
                or (digest(proposal["before"].encode("utf-8")) if proposal["before"] is not None else None)
                != proposal["before_hash"]):
            raise ConflictError("提案快照内容校验失败")
        return proposal

    def _current(self, state: dict, proposal: dict) -> None:
        _check_proposal_dependencies(self.project, state, proposal)
        if (proposal["base_revision"] != state["revision"]
                or proposal["base_fingerprint"] != state["fingerprint"]
                or self.project.fingerprint() != state["fingerprint"]):
            raise ConflictError("提案基于旧的正式版本，请根据当前内容重新创建提案")
        _check_expected(_read_raw(self.project, proposal["target"]), proposal["before_hash"])

    def list(self) -> list[dict]:
        with self.storage.locked() as state:
            proposals = [copy.deepcopy(self._get(state, key)) for key in state["proposals"]]
            return sorted(proposals, key=lambda proposal: proposal["created_at"], reverse=True)

    def get(self, proposal_id: str) -> dict:
        with self.storage.locked() as state:
            return copy.deepcopy(self._get(state, proposal_id))

    def create(self, target: str, content: str, reason: str, impact: list[str],
               expected_hash: Optional[str], provenance: str = "AI_SUGGESTED",
               dependencies: Optional[dict[str, str]] = None,
               expected_dependency_versions: Optional[dict[str, str]] = None) -> dict:
        if _kind(self.project, target) != "canonical":
            raise VibeNovelError("提案目标只允许 bible、正式章节、memory 和已批准大纲")
        if target.startswith("chapters/") and not re.fullmatch(r"chapters/ch0*[1-9][0-9]*\.md", target):
            raise VibeNovelError("正式章节需要 chapters/chNNN.md 格式的正整数编号")
        if target.startswith("chapters/") and len(target.split("/")[-1][2:-3]) < 3:
            raise VibeNovelError("正式章节编号至少三位")
        raw = _content_bytes(target, content)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise VibeNovelError("提案必须填写 1–4000 字的变更理由")
        if (not isinstance(impact, list) or not impact or len(impact) > 100
                or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in impact)):
            raise VibeNovelError("提案必须列出影响（至少一条，最多 100 条，每条 1000 字）")
        _provenance(provenance)
        with self.storage.locked() as state:
            if dependencies is not None:
                _check_dependencies(self.project, dependencies)
                if target in dependencies:
                    raise VibeNovelError("提案不能把目标文件本身作为依赖；请改用独立来源，避免应用后立即失效")
            if expected_dependency_versions is not None:
                if (dependencies is None or not isinstance(expected_dependency_versions, dict)
                        or set(expected_dependency_versions) != set(dependencies)
                        or any(not isinstance(value, str) or not HASH.fullmatch(value)
                               for value in expected_dependency_versions.values())):
                    raise VibeNovelError("预期提案依赖历史快照无效")
                if expected_dependency_versions != _dependency_versions(state, dependencies):
                    raise ConflictError("准备提案期间来源历史已变化，请刷新后重新创建提案")
            before = _read_raw(self.project, target)
            _check_expected(before, expected_hash)
            proposal = {"id": uuid4().hex, "novel_id": self.project.novel_id, "target": target,
                        "before": _decode(before) if before is not None else None, "after": content,
                        "before_hash": digest(before) if before is not None else None,
                        "after_hash": digest(raw), "reason": reason.strip(), "impact": list(impact),
                        "base_revision": state["revision"], "base_fingerprint": state["fingerprint"],
                        "created_at": utc_now(), "provenance": provenance, "state": "pending",
                        "decision": None, "applied_at": None, "applied_revision": None}
            if dependencies is not None:
                proposal["dependencies"] = copy.deepcopy(dependencies)
                proposal["dependency_versions"] = _dependency_versions(state, dependencies)
            proposal["content_hash"] = _proposal_hash(proposal)
            after = copy.deepcopy(state)
            after["proposals"][proposal["id"]] = proposal
            self.storage._write_state(after)
            return copy.deepcopy(proposal)

    def decide(self, proposal_id: str, decision: str, note: str = "") -> dict:
        if decision not in ("approve", "reject"):
            raise VibeNovelError("decision 只能是 approve 或 reject")
        if not isinstance(note, str) or len(note) > 4000:
            raise VibeNovelError("审批备注最多 4000 字")
        with self.storage.locked() as state:
            proposal = self._get(state, proposal_id)
            revoking = proposal["state"] == "approved" and decision == "reject"
            if proposal["state"] != "pending" and not revoking:
                raise ConflictError("此提案已作出决定，不能重复审批")
            if decision == "approve":
                self._current(state, proposal)
            after = copy.deepcopy(state)
            proposal = after["proposals"][proposal_id]
            if revoking:
                # A stale approval must remain revocable. Keep its original
                # author decision for audit, without checking current canon or
                # dependencies and without undoing any applied content.
                proposal.setdefault("decision_history", []).append(copy.deepcopy(proposal["decision"]))
            proposal["state"] = "approved" if decision == "approve" else "rejected"
            proposal["decision"] = {"action": decision, "note": note, "at": utc_now(),
                                    "content_hash": proposal["content_hash"], "revision": state["revision"],
                                    "fingerprint": state["fingerprint"], "actor": "AUTHOR"}
            if revoking:
                after["events"].append({"revision": state["revision"], "kind": "proposal_approval_revoked",
                                        "proposal_id": proposal_id, "content_hash": proposal["content_hash"],
                                        "at": proposal["decision"]["at"]})
            self.storage._write_state(after)
            return copy.deepcopy(proposal)

    def apply(self, proposal_id: str) -> dict:
        with self.storage.locked() as state:
            proposal = self._get(state, proposal_id)
            if proposal["state"] != "approved":
                raise ConflictError("只有尚未应用的已批准提案可应用")
            decision = proposal.get("decision")
            if (not isinstance(decision, dict) or decision.get("action") != "approve"
                    or decision.get("content_hash") != proposal["content_hash"]
                    or decision.get("revision") != proposal["base_revision"]
                    or decision.get("fingerprint") != proposal["base_fingerprint"]):
                raise ConflictError("审批决定与提案版本不匹配，已拒绝应用")
            self._current(state, proposal)
            target = proposal["target"]
            before = _read_raw(self.project, target)
            raw = _content_bytes(target, proposal["after"])
            after = copy.deepcopy(state)
            if before is not None and (not after["history"].get(target) or after["history"][target][-1]["hash"] != digest(before)):
                _snapshot(after, target, before, "EXTERNAL")
            after["revision"] += 1
            after["fingerprint"] = self.storage._fingerprint_after(target, raw)
            _snapshot(after, target, raw, proposal["provenance"])
            applied = after["proposals"][proposal_id]
            applied["state"] = "applied"
            applied["applied_at"] = utc_now()
            applied["applied_revision"] = after["revision"]
            after["events"].append({"revision": after["revision"], "kind": "proposal_applied",
                                    "proposal_id": proposal_id, "content_hash": proposal["content_hash"],
                                    "at": applied["applied_at"]})
            self.storage.transact(state, after, target, raw, before)
            return copy.deepcopy(applied)

    def approved_outline(self, path: str) -> dict:
        """Only a current canonical plan backed by an applied approval is runnable."""
        if not isinstance(path, str) or not path.startswith("outlines/approved/"):
            raise VibeNovelError("生成任务必须选择 outlines/approved/ 下已存在的正式章计划")
        with self.storage.locked() as state:
            document = DocumentStore(self.project)._document(path, state)
            proposal_id = _latest_applied_id(state, path)
            if proposal_id is not None:
                proposal = self._get(state, proposal_id)
                if proposal["after_hash"] == document["hash"]:
                    _check_proposal_dependencies(self.project, state, proposal)
                    document["approval_id"] = proposal_id
                    return document
            raise ConflictError("章计划当前内容没有对应的已应用审批，请先提交并批准提案")
