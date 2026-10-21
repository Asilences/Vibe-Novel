"""Read-only bridge from durable book research to private method candidates.

No model call or writing authority is granted here. Library publication has its
own lock and rechecks these external dependencies; it is not a book transaction.
"""
from __future__ import annotations

import copy
import re

from .files import digest, json_bytes, read_json
from .locking import ConflictError
from .reference_books import ReferenceBookStore
from vibenovel.errors import VibeNovelError

_ID = re.compile(r"[0-9a-f]{32}\Z")
_NODE = re.compile(r"(?:chapter-\d{5}|synthesis-\d{2}-\d{5})\Z")
MAX_RESEARCH_ORIGIN_BYTES = 32 * 1024
MAX_RESEARCH_TASK_DEPENDENCIES_BYTES = 256 * 1024


def _research_json(path, limit):
    try:
        return read_json(path, max_bytes=limit)
    except (OSError, VibeNovelError) as exc:
        raise ConflictError("参考研究批次或候选结果缺失/格式损坏，不能发布方法") from exc


def check_frozen_research_plan(batch):
    """Shared projection of immutable plan fields, excluding durable progress."""
    fields = ("schema_version", "book_id", "edition_id", "novel_id", "request", "chapter_ids", "nodes",
              "authorization_revision", "coverage", "budget", "warnings")
    mutable = {"attempt_id", "budget_summary", "result_path", "result_hash", "result_summary", "pending_response", "finish_reason"}
    try:
        projected = {name: copy.deepcopy(batch[name]) for name in fields}
        for node in projected["nodes"]:
            for name in mutable:
                node.pop(name, None)
            node.update(state="pending", error=None)
        if digest(json_bytes(projected)) != batch.get("preview_hash") or batch.get("hash") != batch.get("preview_hash"):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ConflictError("研究工程冻结计划、范围、模型或总预算已被修改，拒绝继续") from None


def _references(result):
    refs = []
    try:
        for section in ("structure", "expression", "methods"):
            for item in result[section]:
                refs.extend(item["source_refs"])
        for item in result["threads"]:
            refs.extend(item["setup_refs"] + item["payoff_refs"])
    except (KeyError, TypeError):
        raise ConflictError("参考研究结果证据结构无效") from None
    return refs


def _valid_ref(ref, allowed):
    if (not isinstance(ref, dict) or set(ref) != {"chapter_id", "start_char", "end_char", "quote"}
            or type(ref["start_char"]) is not int or type(ref["end_char"]) is not int
            or not isinstance(ref["quote"], str) or not 0 < len(ref["quote"]) <= 240
            or not 0 <= ref["start_char"] < ref["end_char"]):
        raise ConflictError("参考研究候选需要精确有界引文")
    if not any(candidate["chapter_id"] == ref["chapter_id"]
               and candidate["start_char"] <= ref["start_char"] < ref["end_char"] <= candidate["end_char"]
               and candidate["quote"][ref["start_char"] - candidate["start_char"]:
                                      ref["end_char"] - candidate["start_char"]] == ref["quote"]
               for candidate in allowed):
        raise ConflictError("研究候选引文不在节点实际提供的来源内")


def _preview_reference_methods(workspace, book_id, batch_id, node_id, method_indices):
    for identity in (book_id, batch_id):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise VibeNovelError("参考书与研究批次ID无效")
    if not isinstance(node_id, str) or not _NODE.fullmatch(node_id):
        raise VibeNovelError("研究节点ID无效")
    if (not isinstance(method_indices, list) or not 1 <= len(method_indices) <= 6
            or any(type(index) is not int or index < 0 for index in method_indices)
            or len(set(method_indices)) != len(method_indices)):
        raise VibeNovelError("请选择1至6个不重复的方法索引")
    store = ReferenceBookStore(workspace)
    batch = _research_json(store.path(book_id + "/batches/" + batch_id + ".json"), 8 * 1024 * 1024)
    if batch.get("schema_version") != 1 or batch.get("id") != batch_id or batch.get("book_id") != book_id:
        raise ConflictError("参考研究批次身份无效")
    check_frozen_research_plan(batch)
    detail = store.detail(book_id, batch["edition_id"], limit=1)
    if (detail["withdrawn"] or detail["outbound"] != "excerpts"
            or detail["authorization_revision"] != batch["authorization_revision"]):
        raise ConflictError("参考书许可已变更或撤回，旧研究方法不能复活")
    indexed = {node["id"]: node for node in batch["nodes"]}
    if len(indexed) != len(batch["nodes"]) or node_id not in indexed:
        raise ConflictError("研究节点不存在或身份重复")
    ordered, visiting, visited = [], set(), set()

    def visit(identity):
        if identity in visited:
            return
        if identity in visiting or identity not in indexed:
            raise ConflictError("研究依赖缺失或形成循环")
        node = indexed[identity]
        if node.get("state") != "succeeded" or not _NODE.fullmatch(identity):
            raise ConflictError("仅成功节点及完整成功依赖可转为方法候选")
        visiting.add(identity)
        for parent in node["depends_on"]:
            visit(parent)
        visiting.remove(identity)
        visited.add(identity)
        ordered.append(node)

    visit(node_id)
    raw = [node for node in ordered if node["kind"] == "chapter"]
    ranges = [[{key: ref[key] for key in ("chapter_id", "start_char", "end_char")} for ref in node["refs"]] for node in raw]
    snapshots = store.snapshots_ranges(book_id, batch["edition_id"], ranges)
    raw_snapshots = {node["id"]: snapshot for node, snapshot in zip(raw, snapshots)}
    raw_deps, result_deps, results, all_refs = [], [], {}, {}
    for node in ordered:
        identity = node["id"]
        if node["kind"] == "chapter":
            snapshot = raw_snapshots[identity]
            if snapshot["hash"] != node["snapshot_hash"]:
                raise ConflictError("研究所依赖原件、解析范围或授权版本已变化")
            allowed = snapshot["refs"]
            raw_deps.append({"node_id": identity, "snapshot_hash": snapshot["hash"],
                             "ranges": [{key: ref[key] for key in ("chapter_id", "start_char", "end_char", "hash", "range_hash")}
                                        for ref in snapshot["refs"]]})
        elif node["kind"] == "synthesis":
            allowed = [ref for parent in node["depends_on"] for ref in all_refs[parent]]
        else:
            raise ConflictError("不支持的研究节点类型")
        relative = "%s/editions/%s/analyses/%s/%s.json" % (book_id, batch["edition_id"], batch_id, identity)
        if node.get("result_path") != relative:
            raise ConflictError("研究结果路径不属于本书、本版本和本节点")
        path = store.path(relative)
        result = _research_json(path, 128 * 1024)
        if digest(path.read_bytes()) != node.get("result_hash"):
            raise ConflictError("研究结果已被修改或缺失")
        refs = _references(result)
        for ref in refs:
            _valid_ref(ref, allowed)
        results[identity], all_refs[identity] = result, refs
        result_deps.append({"node_id": identity, "result_hash": node["result_hash"]})
    result = results[node_id]
    selected, evidence = [], []
    for index in method_indices:
        if index >= len(result["methods"]):
            raise VibeNovelError("方法索引超出所选成功节点")
        method = result["methods"][index]
        if (not isinstance(method, dict) or set(method) != {"title", "steps", "limitations", "source_refs"}
                or not isinstance(method["title"], str) or not 1 <= len(method["title"]) <= 100
                or not isinstance(method["source_refs"], list) or not 1 <= len(method["source_refs"]) <= 6):
            raise ConflictError("研究方法候选格式无效")
        for name in ("steps", "limitations"):
            if (not isinstance(method[name], list) or not 1 <= len(method[name]) <= 8
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 400 for value in method[name])):
                raise ConflictError("研究方法步骤或局限格式无效")
        for ref in method["source_refs"]:
            _valid_ref(ref, all_refs[node_id])
            evidence.append(copy.deepcopy(ref))
        selected.append({"index": index, "title": method["title"], "steps": copy.deepcopy(method["steps"]),
                         "limitations": copy.deepcopy(method["limitations"]), "source_refs": copy.deepcopy(method["source_refs"])})
    origin = {"schema_version": 1, "book_id": book_id, "book_title": detail["title"], "edition_id": batch["edition_id"],
              "batch_id": batch_id, "node_id": node_id, "result_hash": indexed[node_id]["result_hash"],
              "authorization_revision": batch["authorization_revision"], "plan_hash": batch["preview_hash"],
              "method_indices": copy.deepcopy(method_indices),
              # The immutable batch pointer/plan hash owns the full ranges.
              # Seal their complete ordered projection without duplicating a
              # whole book into every card, binding and ordinary job snapshot.
              "raw_dependencies": {"count": len(raw_deps), "hash": digest(json_bytes(raw_deps))},
              "result_dependencies": {"count": len(result_deps), "hash": digest(json_bytes(result_deps))},
              "evidence": evidence}
    if len(json_bytes(origin)) > MAX_RESEARCH_ORIGIN_BYTES:
        raise VibeNovelError("研究方法来源封存超过32KiB，请减少所选方法；未截断来源")
    value = {"methods": selected, "origin": origin, "purpose": batch["request"]["purpose"],
             "warnings": ["仅发布个人库的未采用候选；引文核对不证明方法效果。", "研究证据是外部依赖，不自动视作后续模型已阅读的原文。"]}
    value["hash"] = digest(json_bytes(value))
    return value


def preview_reference_methods(workspace, book_id, batch_id, node_id, method_indices):
    try:
        return _preview_reference_methods(workspace, book_id, batch_id, node_id, method_indices)
    except (OSError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise ConflictError("参考研究批次、结果或来源依赖缺失/格式损坏，不能发布方法") from exc


def validate_research_origin(workspace, origin):
    if isinstance(origin, dict) and origin.get("source_type") == "reference_study":
        from .reference_studies import validate_study_origin
        return validate_study_origin(workspace, origin)
    if not isinstance(origin, dict) or origin.get("schema_version") != 1:
        raise ConflictError("研究方法来源标记无效")
    try:
        current = preview_reference_methods(workspace, origin["book_id"], origin["batch_id"], origin["node_id"], origin["method_indices"])
    except (KeyError, OSError) as exc:
        raise ConflictError("研究方法来源缺失或无法重新核对") from exc
    if current["origin"] != origin:
        raise ConflictError("研究方法固定来源或授权已变化，不能恢复旧方法依赖")
    return current
