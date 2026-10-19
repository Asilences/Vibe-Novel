"""Author blind comparisons of two explicit drafts, without model calls.

Assignments and snapshots are saved once. Source changes invalidate a ballot;
this store neither computes a model ranking nor learns a routing preference.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
from typing import Optional
from uuid import uuid4

from vibenovel.errors import VibeNovelError
from .documents import HASH, DocumentStore, _content_bytes, _read_raw, _snapshot
from .files import digest, json_bytes, tree_files
from .locking import ConflictError
from .model_artifacts import draft_target
from .project import utc_now

_ID = re.compile(r"[0-9a-f]{32}\Z")
_PATH = re.compile(r"reviews/bench-([0-9a-f]{32})\.json\Z")
DECISIONS = ("prefer_a", "prefer_b", "tie")
LIMITATIONS = [
    "这是作者对两份锁定候选的个人偏好比较，不是文学质量认证或模型能力排名。",
    "A/B次序在创建时随机并固定；界面隐藏来源文件名和模型标签，但正文自身仍可能透露来源。",
    "只使用作者明确选择的两份候选，不自动调用模型，也不自动学习路由权重。",
    "过期比较仍可查看，但不能投票；重新比较须创建新记录。投票不批准正文或记忆。",
]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class NovelBenchStore:
    def __init__(self, project):
        self.project = project
        self.documents = DocumentStore(project)
        self.storage = self.documents.storage

    @staticmethod
    def _id(identifier):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise VibeNovelError("盲评ID无效")
        return identifier

    def _path(self, identifier):
        return "reviews/bench-" + self._id(identifier) + ".json"

    def _write(self, state, value, *, before=None, provenance="AUTHOR"):
        path = self._path(value["id"])
        raw = _content_bytes(path, json_bytes(value).decode("utf-8"))
        after = copy.deepcopy(state)
        if before is not None and (not after["history"].get(path)
                                   or after["history"][path][-1]["hash"] != digest(before)):
            _snapshot(after, path, before, "EXTERNAL")
        _snapshot(after, path, raw, provenance)
        self.storage.transact(state, after, path, raw, before)
        return after, digest(raw)

    def _source(self, path, expected, state):
        draft_target(self.project, path)
        if not isinstance(expected, str) or not HASH.fullmatch(expected):
            raise VibeNovelError("盲评候选需要完整SHA256")
        doc = self.documents._document(path, state)
        if doc["hash"] != expected:
            raise ConflictError("盲评候选已变化，请刷新后创建")
        if not doc["content"].strip() or len(doc["content"]) > 200000:
            raise VibeNovelError("盲评候选不能为空且最多200000字符")
        return doc

    def create(self, candidate_a_path, candidate_a_hash, candidate_b_path, candidate_b_hash):
        """Lock the supplied pair and randomize its display assignment once."""
        if candidate_a_path == candidate_b_path:
            raise VibeNovelError("盲评需要两份不同路径的候选")
        with self.storage.locked() as state:
            docs = [self._source(candidate_a_path, candidate_a_hash, state),
                    self._source(candidate_b_path, candidate_b_hash, state)]
            after = copy.deepcopy(state)
            sources = []
            for doc in docs:
                history = after["history"].get(doc["path"], [])
                if not history or history[-1]["hash"] != doc["hash"]:
                    _snapshot(after, doc["path"], doc["content"].encode("utf-8"), "EXTERNAL")
                sources.append({"path": doc["path"], "hash": doc["hash"],
                                "version_id": after["history"][doc["path"]][-1]["id"],
                                "content": doc["content"]})
            if secrets.randbelow(2):
                sources.reverse()
            value = {"schema_version": 1, "id": uuid4().hex, "novel_id": self.project.novel_id,
                     "created_at": utc_now(), "provenance": "AUTHOR", "status": "awaiting_vote",
                     "sources": sources, "vote": None, "invalidated_at": None}
            # A single journal also commits any newly observed source snapshots.
            state, current_hash = self._write_with_source_snapshots(state, after, value)
            return self._public(value, current_hash, stale=False)

    def _write_with_source_snapshots(self, before_state, after, value):
        path = self._path(value["id"])
        raw = _content_bytes(path, json_bytes(value).decode("utf-8"))
        _snapshot(after, path, raw, "AUTHOR")
        self.storage.transact(before_state, after, path, raw, None)
        return after, digest(raw)

    def _read(self, identifier, state):
        path = self._path(identifier)
        raw = _read_raw(self.project, path)
        if raw is None:
            raise VibeNovelError("未找到此作品的盲评记录")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                               parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise VibeNovelError("盲评记录不是有效JSON") from exc
        fields = {"schema_version", "id", "novel_id", "created_at", "provenance", "status",
                  "sources", "vote", "invalidated_at"}
        if (not isinstance(value, dict) or set(value) != fields
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["id"] != identifier or value["novel_id"] != self.project.novel_id
                or value["provenance"] != "AUTHOR" or value["status"] not in ("awaiting_vote", "voted")
                or not isinstance(value["created_at"], str)
                or not isinstance(value["sources"], list) or len(value["sources"]) != 2
                or (value["invalidated_at"] is not None and not isinstance(value["invalidated_at"], str))):
            raise ConflictError("盲评记录身份、版本或状态无效")
        paths = []
        for source in value["sources"]:
            if not isinstance(source, dict) or set(source) != {"path", "hash", "version_id", "content"}:
                raise ConflictError("盲评来源字段无效")
            draft_target(self.project, source["path"])
            if (not isinstance(source["hash"], str) or not HASH.fullmatch(source["hash"])
                    or not isinstance(source["version_id"], str) or not _ID.fullmatch(source["version_id"])
                    or not isinstance(source["content"], str) or not source["content"].strip()
                    or len(source["content"]) > 200000
                    or digest(_content_bytes(source["path"], source["content"])) != source["hash"]):
                raise ConflictError("盲评来源快照无效")
            paths.append(source["path"])
        if len(set(paths)) != 2:
            raise ConflictError("盲评来源重复")
        vote = value["vote"]
        if value["status"] == "awaiting_vote" and vote is not None:
            raise ConflictError("未投票记录包含决定")
        if value["status"] == "voted":
            if (not isinstance(vote, dict) or set(vote) != {"decision", "note", "at", "actor"}
                    or vote["decision"] not in DECISIONS or not isinstance(vote["note"], str)
                    or len(vote["note"]) > 4000 or not isinstance(vote["at"], str) or vote["actor"] != "AUTHOR"):
                raise ConflictError("盲评投票无效")
        # An observed editor A -> B -> A remains stale through the version ID.
        stale = value["invalidated_at"] is not None
        for source in value["sources"]:
            try:
                current = self.documents._document(source["path"], state)
                history = state["history"].get(source["path"], [])
                stale |= (current["hash"] != source["hash"] or not history
                          or history[-1]["id"] != source["version_id"])
            except (OSError, VibeNovelError):
                stale = True
        if stale and value["invalidated_at"] is None:
            # Persist the observation so restoring bytes cannot revive a ballot.
            value["invalidated_at"] = utc_now()
            state, _ = self._write(state, value, before=raw)
            raw = json_bytes(value)
        return value, digest(raw), stale, state

    @staticmethod
    def _public(value, current_hash, stale, *, include_content=True):
        result = {key: copy.deepcopy(value[key]) for key in
                  ("schema_version", "id", "novel_id", "created_at", "status", "vote")}
        result.update(hash=current_hash, stale=stale, limitations=list(LIMITATIONS))
        if include_content:
            result["candidates"] = [{"label": label, "content": source["content"], "hash": source["hash"]}
                                    for label, source in zip(("A", "B"), value["sources"])]
        return result

    def get(self, identifier):
        with self.storage.locked() as state:
            value, current_hash, stale, _ = self._read(identifier, state)
            return self._public(value, current_hash, stale)

    def list(self):
        with self.storage.locked() as state:
            records = []
            for path in tree_files(self.project.path("reviews")):
                match = _PATH.fullmatch(path.relative_to(self.project.root).as_posix())
                if match:
                    value, current_hash, stale, state = self._read(match[1], state)
                    records.append(self._public(value, current_hash, stale, include_content=False))
            return {"benchmarks": sorted(records, key=lambda item: item["created_at"], reverse=True)}

    def vote(self, identifier, decision, note="", expected_hash: Optional[str] = None):
        if decision not in DECISIONS or not isinstance(note, str) or len(note) > 4000:
            raise VibeNovelError("盲评决定应为prefer_a/prefer_b/tie，备注最多4000字")
        if expected_hash is not None and (not isinstance(expected_hash, str) or not HASH.fullmatch(expected_hash)):
            raise VibeNovelError("盲评expected_hash需要完整SHA256")
        with self.storage.locked() as state:
            value, current_hash, stale, state = self._read(identifier, state)
            if expected_hash is not None and current_hash != expected_hash:
                raise ConflictError("盲评记录已变化，请刷新")
            if stale:
                raise ConflictError("盲评候选已过期，请创建新比较；旧记录不可投票")
            if value["status"] != "awaiting_vote":
                raise ConflictError("此盲评已经投票，不能重复决定")
            value["status"] = "voted"
            value["vote"] = {"decision": decision, "note": note, "at": utc_now(), "actor": "AUTHOR"}
            _, current_hash = self._write(state, value, before=_read_raw(self.project, self._path(identifier)))
            return self._public(value, current_hash, False)

    def export(self, identifier):
        """Return only the two locked texts and the author's optional ballot."""
        view = self.get(identifier)
        vote = view["vote"]
        content = "# 候选盲评 %s\n\n" % view["id"]
        if view["stale"]:
            content += "本比较已过期，仅供回顾。\n\n"
        for candidate in view["candidates"]:
            content += "## 候选 %s\n\n%s\n\n" % (candidate["label"], candidate["content"])
        if vote is not None:
            content += "## 作者偏好\n\n%s\n\n%s\n" % (vote["decision"], vote["note"])
        return {"filename": "bench-%s.md" % view["id"], "content": content, "stale": view["stale"]}

    def reveal(self, identifier):
        """Explicit post-ballot reveal; normal responses remain anonymous."""
        with self.storage.locked() as state:
            value, current_hash, stale, _ = self._read(identifier, state)
            if value["status"] != "voted":
                raise ConflictError("作者投票后才可显式查看盲评来源")
            return {"id": identifier, "hash": current_hash, "stale": stale,
                    "sources": [{"label": label, **{key: source[key] for key in ("path", "hash", "version_id")}}
                                for label, source in zip(("A", "B"), value["sources"])],
                    "limitations": list(LIMITATIONS)}
