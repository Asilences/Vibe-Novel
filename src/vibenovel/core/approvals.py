"""Author decisions bound to exact single-file proposals and canonical revisions."""
from __future__ import annotations

import copy
import re
from typing import Optional
from uuid import uuid4

from vibenovel.errors import VibeNovelError
from .documents import (ConflictError, DocumentStore, _Storage, _check_expected,
                        _content_bytes, _decode, _kind, _provenance, _read_raw, _snapshot)
from .files import digest, json_bytes
from .project import Project, utc_now

IMMUTABLE_FIELDS = (
    "id", "novel_id", "target", "before", "after", "before_hash", "after_hash", "reason", "impact",
    "base_revision", "base_fingerprint", "created_at", "provenance",
)


def _proposal_hash(proposal: dict) -> str:
    try:
        return digest(json_bytes({key: proposal[key] for key in IMMUTABLE_FIELDS}))
    except KeyError as exc:
        raise ConflictError("提案字段缺失，已拒绝操作") from exc


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
        if (digest(_content_bytes(proposal["target"], proposal["after"])) != proposal["after_hash"]
                or (digest(proposal["before"].encode("utf-8")) if proposal["before"] is not None else None)
                != proposal["before_hash"]):
            raise ConflictError("提案快照内容校验失败")
        return proposal

    def _current(self, state: dict, proposal: dict) -> None:
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
               expected_hash: Optional[str], provenance: str = "AI_SUGGESTED") -> dict:
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
            before = _read_raw(self.project, target)
            _check_expected(before, expected_hash)
            proposal = {"id": uuid4().hex, "novel_id": self.project.novel_id, "target": target,
                        "before": _decode(before) if before is not None else None, "after": content,
                        "before_hash": digest(before) if before is not None else None,
                        "after_hash": digest(raw), "reason": reason.strip(), "impact": list(impact),
                        "base_revision": state["revision"], "base_fingerprint": state["fingerprint"],
                        "created_at": utc_now(), "provenance": provenance, "state": "pending",
                        "decision": None, "applied_at": None, "applied_revision": None}
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
            if proposal["state"] != "pending":
                raise ConflictError("此提案已作出决定，不能重复审批")
            if decision == "approve":
                self._current(state, proposal)
            after = copy.deepcopy(state)
            proposal = after["proposals"][proposal_id]
            proposal["state"] = "approved" if decision == "approve" else "rejected"
            proposal["decision"] = {"action": decision, "note": note, "at": utc_now(),
                                    "content_hash": proposal["content_hash"], "revision": state["revision"],
                                    "fingerprint": state["fingerprint"], "actor": "AUTHOR"}
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
            for proposal_id in reversed(list(state["proposals"])):
                proposal = self._get(state, proposal_id)
                if (proposal["state"] == "applied" and proposal["target"] == path
                        and proposal["after_hash"] == document["hash"]):
                    document["approval_id"] = proposal_id
                    return document
            raise ConflictError("章计划当前内容没有对应的已应用审批，请先提交并批准提案")
