"""Versioned, evidence-bound narrative studies, separate from novel canon.

Reading checkpoints are append-only observations at a particular reading time.
Later discoveries can add a new checkpoint/map version; they never rewrite an
earlier reader's or character's knowledge. Exact quotes verify provenance only.
"""
from __future__ import annotations

import copy
import json
import math
import re
import uuid
from pathlib import Path

from .files import atomic_write, digest, json_bytes, read_json, safe_path
from .locking import ConflictError, project_lock
from .project import utc_now
from .reference_books import ReferenceBookStore
from vibenovel.errors import VibeNovelError

_ID = re.compile(r"[0-9a-f]{32}\Z")
_KEY = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
MAX_RECORD = 8 * 1024 * 1024
GROUPS = ("causal", "characters", "information", "rhythm")
REF_FIELDS = {"book_id", "edition_id", "chapter_id", "start_char", "end_char", "quote"}


def text(value, maximum, label, empty=False):
    if not isinstance(value, str) or len(value) > maximum or not empty and not value.strip():
        raise VibeNovelError(label + "格式或长度无效")
    value.encode("utf-8")
    return value


def identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise VibeNovelError("研究或来源ID无效")
    return value


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise VibeNovelError("图谱实体键需要1至80位字母、数字、横线或下划线")
    return value


def _items(value, maximum, label):
    if not isinstance(value, list) or len(value) > maximum:
        raise VibeNovelError(label + "项目过多或格式无效；未截断")
    return value


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise VibeNovelError("研究JSON字段不符合合同：" + ", ".join(names))


def all_refs(value):
    refs = []
    if isinstance(value, dict):
        if set(value) == REF_FIELDS:
            return [value]
        for child in value.values():
            refs.extend(all_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(all_refs(child))
    return refs


def source_id(ref):
    return "r" + digest(json_bytes({name: ref[name] for name in ("book_id", "edition_id", "chapter_id", "start_char", "end_char")}))[:16]


def resolve_response_refs(value, allowed):
    """Resolve an unambiguous quote inside a named, actually sent fragment.

    Models may supply canonical offsets instead. Never fuzzy-match text or pick
    one occurrence of an ambiguous quote. The saved result is always canonical.
    """
    if isinstance(value, dict):
        if set(value) == {"source_id", "quote"}:
            quote = text(value["quote"], 300, "引文")
            candidates = [ref for ref in allowed if source_id(ref) == value["source_id"]]
            matches = []
            for ref in candidates:
                offset = ref["quote"].find(quote)
                if offset >= 0 and ref["quote"].find(quote, offset + 1) < 0:
                    candidate = {name: ref[name] for name in ("book_id", "edition_id", "chapter_id")}
                    candidate.update(start_char=ref["start_char"] + offset, end_char=ref["start_char"] + offset + len(quote), quote=quote)
                    if candidate not in matches:
                        matches.append(candidate)
            if len(matches) != 1:
                raise VibeNovelError("引文在指定实读片段内缺失或出现多次；请选择可唯一定位的较长原句，不自动重发付费请求")
            return matches[0]
        return {name: resolve_response_refs(child, allowed) for name, child in value.items()}
    if isinstance(value, list):
        return [resolve_response_refs(child, allowed) for child in value]
    return value


def checked_refs(values, allowed, required=True):
    _items(values, 8, "原文证据")
    if required and not values:
        raise VibeNovelError("每项候选判断须有实际阅读区间内的精确引文")
    for ref in values:
        _fields(ref, REF_FIELDS)
        for field in ("book_id", "edition_id", "chapter_id"):
            identity(ref[field])
        start, end = ref["start_char"], ref["end_char"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise VibeNovelError("原文证据需要Unicode半开区间")
        text(ref["quote"], 300, "引文")
        if not any(all(ref[name] == candidate[name] for name in ("book_id", "edition_id", "chapter_id"))
                   and candidate["start_char"] <= start < end <= candidate["end_char"]
                   and candidate["quote"][start-candidate["start_char"]:end-candidate["start_char"]] == ref["quote"]
                   for candidate in allowed):
            raise ConflictError("引文不在本节点实际阅读/发送的范围，或原文不匹配")
    return copy.deepcopy(values)


def response_json(raw):
    text(raw, 128000, "研究响应")
    value = raw.strip()
    if value.startswith("```json\n") and value.endswith("```"):
        value = value[8:-3].strip()
    def unique(pairs):
        result = {}
        for name, child in pairs:
            if name in result:
                raise ValueError("duplicate field")
            result[name] = child
        return result
    try:
        return json.loads(value, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError) as exc:
        raise VibeNovelError("研究响应须是单个无重复字段的JSON对象") from exc


def reading_contract():
    ref = {"book_id": "输入真实ID", "edition_id": "输入真实ID", "chapter_id": "输入真实ID",
           "start_char": 0, "end_char": 1, "quote": "精确原句，最多300字符"}
    return {"summary": "本段发生了什么，最多800字符", "carry_forward": "给下一段的有界连续阅读笔记，最多6000字符；区分已知/猜测/未决",
            "events": [{"key": "e1", "title": "事件", "action": "选择/行动", "outcome": "结果/代价", "caused_by": [], "claim_type": "source_asserted", "source_refs": [ref]}],
            "characters": [{"key": "c1", "name": "人物", "goal": "当时目标", "state": "当前处境", "source_refs": [ref]}],
            "relationships": [{"from": "c1", "to": "c2", "change": "关系及变化", "claim_type": "inferred", "source_refs": [ref]}],
            "knowledge": [{"subject": "reader", "fact": "此次获知内容", "status": "known", "source_refs": [ref]}],
            "threads": [{"key": "t1", "title": "线索/信息差", "state": "introduced", "explanation": "首次出现/推进/解释；不能把猜测当揭晓", "setup_refs": [ref], "payoff_refs": []}],
            "rhythm": [{"label": "节拍", "tension": 2, "emotion": "情绪及其叙事作用", "valence": 0, "source_refs": [ref]}],
            "methods": [{"title": "可迁移方法", "steps": ["步骤"], "limitations": ["前提/反例"], "source_refs": [ref]}],
            "gaps": ["本段未读、无法判断、连续笔记省略的历史信息"]}


def validate_reading(value, allowed, known_characters=None, known_events=None):
    _fields(value, reading_contract())
    result = {"summary": text(value["summary"], 800, "概览"),
              "carry_forward": text(value["carry_forward"], 6000, "连续阅读笔记"),
              "verification": "exact_quotes_only", "status": "candidate", "provenance": "AI_SUGGESTED"}
    known_characters, known_events = set(known_characters or ()), set(known_events or ())
    result["events"] = []
    for event in _items(value["events"], 20, "事件"):
        _fields(event, ("key", "title", "action", "outcome", "caused_by", "claim_type", "source_refs"))
        key = _key(event["key"])
        if key in {item["key"] for item in result["events"]}:
            raise VibeNovelError("同节点事件键重复")
        if event["claim_type"] not in ("source_asserted", "inferred"):
            raise VibeNovelError("需区分原文陈述和分析推断")
        causes = [_key(item) for item in _items(event["caused_by"], 8, "因果前项")]
        if any(item not in known_events for item in causes):
            raise VibeNovelError("因果前项尚未实际读到，不能引用未来事件")
        result["events"].append({"key": key, "title": text(event["title"], 120, "事件标题"),
            "action": text(event["action"], 400, "行动"), "outcome": text(event["outcome"], 400, "结果"),
            "caused_by": causes, "claim_type": event["claim_type"], "source_refs": checked_refs(event["source_refs"], allowed)})
        known_events.add(key)
    result["characters"] = []
    for item in _items(value["characters"], 30, "人物"):
        _fields(item, ("key", "name", "goal", "state", "source_refs"))
        key = _key(item["key"])
        known_characters.add(key)
        result["characters"].append({"key": key, "name": text(item["name"], 100, "人物名"),
            "goal": text(item["goal"], 400, "人物目标"), "state": text(item["state"], 400, "人物状态"),
            "source_refs": checked_refs(item["source_refs"], allowed)})
    result["relationships"] = []
    for item in _items(value["relationships"], 30, "关系"):
        _fields(item, ("from", "to", "change", "claim_type", "source_refs"))
        if item["from"] not in known_characters or item["to"] not in known_characters:
            raise VibeNovelError("关系端点须是当前或先前实际阅读记录中的人物")
        if item["claim_type"] not in ("source_asserted", "inferred"):
            raise VibeNovelError("关系判断须区分原文和推断")
        result["relationships"].append({"from": item["from"], "to": item["to"],
            "change": text(item["change"], 500, "关系变化"), "claim_type": item["claim_type"],
            "source_refs": checked_refs(item["source_refs"], allowed)})
    result["knowledge"] = []
    for item in _items(value["knowledge"], 40, "知情变化"):
        _fields(item, ("subject", "fact", "status", "source_refs"))
        if item["subject"] != "reader" and item["subject"] not in known_characters:
            raise VibeNovelError("知情主体须是读者或已出现人物")
        if item["status"] not in ("known", "suspected", "disproved"):
            raise VibeNovelError("知情状态无效")
        result["knowledge"].append({"subject": item["subject"], "fact": text(item["fact"], 400, "知情内容"),
            "status": item["status"], "source_refs": checked_refs(item["source_refs"], allowed)})
    result["threads"] = []
    for item in _items(value["threads"], 20, "线索"):
        _fields(item, ("key", "title", "state", "explanation", "setup_refs", "payoff_refs"))
        if item["state"] not in ("introduced", "advanced", "resolved", "uncertain"):
            raise VibeNovelError("线索状态无效")
        setup = checked_refs(item["setup_refs"], allowed, required=item["state"] != "uncertain")
        payoff = checked_refs(item["payoff_refs"], allowed, required=item["state"] == "resolved")
        if item["state"] == "resolved" and not any(left != right for left in setup for right in payoff):
            raise VibeNovelError("线索回收须有不同的铺设和揭示证据端点")
        result["threads"].append({"key": _key(item["key"]), "title": text(item["title"], 120, "线索标题"),
            "state": item["state"], "explanation": text(item["explanation"], 700, "线索解释"),
            "setup_refs": setup, "payoff_refs": payoff})
    result["rhythm"] = []
    for item in _items(value["rhythm"], 20, "节奏"):
        _fields(item, ("label", "tension", "emotion", "valence", "source_refs"))
        if type(item["tension"]) is not int or not 0 <= item["tension"] <= 5 or type(item["valence"]) is not int or not -3 <= item["valence"] <= 3:
            raise VibeNovelError("张力需0至5，情绪倾向需-3至3；分数只是候选判断")
        result["rhythm"].append({"label": text(item["label"], 120, "节拍标题"), "tension": item["tension"],
            "emotion": text(item["emotion"], 500, "情绪"), "valence": item["valence"],
            "source_refs": checked_refs(item["source_refs"], allowed)})
    result["methods"] = []
    for item in _items(value["methods"], 8, "方法"):
        _fields(item, ("title", "steps", "limitations", "source_refs"))
        method = {"title": text(item["title"], 120, "方法标题"), "source_refs": checked_refs(item["source_refs"], allowed)}
        for name in ("steps", "limitations"):
            if not item[name]:
                raise VibeNovelError("方法须有步骤及适用边界")
            method[name] = [text(part, 500, "方法项") for part in _items(item[name], 8, "方法项")]
        result["methods"].append(method)
    result["gaps"] = [text(item, 700, "缺口") for item in _items(value["gaps"], 40, "缺口")]
    if not any(result[name] for name in ("events", "characters", "knowledge", "threads", "rhythm")) and not result["gaps"]:
        raise VibeNovelError("空研究结果须明确证据缺口")
    if len(json_bytes(result)) > 128 * 1024:
        raise VibeNovelError("研究结果超过128KiB；保留原响应，不自动重试")
    return result


def comparison_contract():
    ref = reading_contract()["events"][0]["source_refs"][0]
    return {"summary": "同一研究问题的比较结论", "per_book": [{"book_id": "真实ID", "solution": "此书如何解决该问题", "premises": ["前提"],
            "limitations": ["反例/不能迁移处"], "source_refs": [ref]}],
            "comparisons": [{"title": "共同问题", "similarities": "相似机制", "differences": "不同安排及其影响", "counterexamples": "边界与反例，证据不足就说明", "source_refs": [ref]}],
            "methods": [{"title": "跨书方法候选", "steps": ["可迁移步骤"], "limitations": ["前提、差异及不适用情况"], "source_refs": [ref]}],
            "gaps": ["仅对实际选中原文负责，不能代表完整作品"]}


def validate_comparison(value, allowed, books):
    _fields(value, comparison_contract())
    result = {"summary": text(value["summary"], 2000, "比较结论"), "per_book": [], "comparisons": [], "methods": [],
              "verification": "exact_quotes_only", "status": "candidate", "provenance": "AI_SUGGESTED"}
    seen = set()
    for item in _items(value["per_book"], 8, "逐书比较"):
        _fields(item, ("book_id", "solution", "premises", "limitations", "source_refs"))
        if item["book_id"] not in books or item["book_id"] in seen:
            raise VibeNovelError("逐书比较书籍ID缺失、越界或重复")
        seen.add(item["book_id"])
        refs = checked_refs(item["source_refs"], allowed)
        if any(ref["book_id"] != item["book_id"] for ref in refs):
            raise VibeNovelError("逐书解法须引用该书实际阅读的原文")
        part = {"book_id": item["book_id"], "solution": text(item["solution"], 2000, "逐书解法"), "source_refs": refs}
        for name in ("premises", "limitations"):
            if not item[name]:
                raise VibeNovelError("逐书解法须说明前提及边界")
            part[name] = [text(child, 700, "比较项") for child in _items(item[name], 12, "比较项")]
        result["per_book"].append(part)
    if seen != set(books):
        raise VibeNovelError("比较结果没有覆盖全部选中书籍；未静默丢书")
    for item in _items(value["comparisons"], 20, "比较观察"):
        _fields(item, ("title", "similarities", "differences", "counterexamples", "source_refs"))
        refs = checked_refs(item["source_refs"], allowed)
        if len({ref["book_id"] for ref in refs}) < 2:
            raise VibeNovelError("跨书判断需至少两本书的实际证据")
        result["comparisons"].append({**{name: text(item[name], 1500, "比较观察") for name in ("title", "similarities", "differences", "counterexamples")}, "source_refs": refs})
    result["gaps"] = [text(item, 700, "比较缺口") for item in _items(value["gaps"], 40, "比较缺口")]
    if not result["comparisons"] and not result["gaps"]:
        raise VibeNovelError("缺少比较观察时须说明缺口")
    for item in _items(value["methods"], 8, "跨书方法"):
        _fields(item, ("title", "steps", "limitations", "source_refs"))
        refs = checked_refs(item["source_refs"], allowed)
        if len({ref["book_id"] for ref in refs}) < 2:
            raise VibeNovelError("跨书提炼的方法须保留至少两书证据，不能只借用一书结论")
        method = {"title": text(item["title"], 120, "方法标题"), "source_refs": refs}
        for name in ("steps", "limitations"):
            if not item[name]:
                raise VibeNovelError("跨书方法须保留步骤与适用边界")
            method[name] = [text(child, 500, "方法项") for child in _items(item[name], 8, "方法项")]
        result["methods"].append(method)
    return result


class _Lock:
    def __init__(self, owner):
        self.owner = owner

    def path(self, relative):
        path = self.owner.path(".state/" + relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class ReferenceStudiesStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.root = safe_path(self.workspace, "library/studies")
        self.root.mkdir(parents=True, exist_ok=True)
        self.books = ReferenceBookStore(self.workspace)

    def path(self, relative):
        return safe_path(self.root, relative)

    def locked(self):
        return project_lock(_Lock(self), timeout=3)

    def _write(self, path, value, overwrite=False):
        raw = json_bytes(value)
        if len(raw) > MAX_RECORD:
            raise VibeNovelError("研究记录超过8MiB；未截断或覆盖")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, raw, overwrite=overwrite)

    def load(self, study_id):
        identity(study_id)
        study = read_json(self.path(study_id + "/manifest.json"), max_bytes=MAX_RECORD)
        if study.get("id") != study_id or study.get("schema_version") != 1:
            raise ConflictError("研究身份或格式无效")
        if digest(json_bytes(study["plan"])) != study.get("plan_hash"):
            raise ConflictError("研究冻结范围、模型或预算被修改")
        if (not isinstance(study.get("nodes"), list)
                or [node.get("id") for node in study["nodes"]] != [node.get("id") for node in study["plan"]["nodes"]]
                or len({node["id"] for node in study["nodes"]}) != len(study["nodes"])):
            raise ConflictError("研究进度节点与冻结计划不匹配")
        return study

    def save(self, study):
        with self.locked():
            old = self.load(study["id"])
            if old.get("revision") != study.get("revision") or old["plan_hash"] != study["plan_hash"]:
                raise ConflictError("研究已由另一进程推进，拒绝覆盖/重复发送")
            study["revision"] += 1
            study["updated_at"] = utc_now()
            self._write(self.path(study["id"] + "/manifest.json"), study, overwrite=True)

    def list_studies(self):
        result = []
        for item in sorted(self.root.iterdir(), reverse=True):
            if _ID.fullmatch(item.name):
                study = self.load(item.name)
                result.append(self.summary(study))
        if len(result) > 10000:
            raise VibeNovelError("研究清单超过10000条；需要归档，未静默截断")
        return result

    @staticmethod
    def summary(study):
        return {"id": study["id"], "kind": study["plan"]["request"]["kind"], "question": study["plan"]["request"]["question"],
                "state": study["state"], "hash": digest(json_bytes(study)), "created_at": study["created_at"],
                "completed_nodes": sum(node["state"] == "succeeded" for node in study["nodes"]), "total_nodes": len(study["nodes"]),
                "sources": copy.deepcopy(study["plan"]["sources"]), "error": study.get("error")}

    def create(self, plan, inputs, key):
        text(key, 80, "幂等键")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", key):
            raise VibeNovelError("幂等键需要16至80个字母数字横线下划线")
        fingerprint = digest(json_bytes(plan))
        with self.locked():
            for listed in self.list_studies():
                old = self.load(listed["id"])
                if old["idempotency_key"] == key:
                    if old["plan_hash"] != fingerprint:
                        raise ConflictError("同一幂等键已用于不同研究计划")
                    return old
            study_id = uuid.uuid4().hex
            for node, snapshots in zip(plan["nodes"], inputs):
                self._write(self.path(study_id + "/inputs/" + node["id"] + ".json"), {"snapshots": snapshots})
            now = utc_now()
            study = {"schema_version": 1, "id": study_id, "plan": plan, "plan_hash": fingerprint,
                     "idempotency_key": key, "state": "prepared", "revision": 1, "created_at": now, "updated_at": now,
                     "nodes": [{"id": node["id"], "state": "pending"} for node in plan["nodes"]], "error": None}
            self._write(self.path(study_id + "/manifest.json"), study)
            return study

    def node_input(self, study, index):
        node = study["plan"]["nodes"][index]
        value = read_json(self.path(study["id"] + "/inputs/" + node["id"] + ".json"), max_bytes=MAX_RECORD)
        if [item["hash"] for item in value["snapshots"]] != node["snapshot_hashes"]:
            raise ConflictError("研究原文快照与冻结计划不匹配")
        for snapshot in value["snapshots"]:
            self.books.validate_snapshot(snapshot)
        return value["snapshots"]

    def node_result(self, study, index):
        node = study["nodes"][index]
        if node["state"] != "succeeded":
            raise VibeNovelError("该节点没有成功的已验证结果")
        path = self.path(study["id"] + "/results/" + node["id"] + ".json")
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise ConflictError("研究结果超过8MiB")
        if digest(raw) != node.get("result_hash"):
            raise ConflictError("研究结果缺失或被修改")
        result = read_json(path, max_bytes=MAX_RECORD)
        if result.get("node_id") != node["id"] or result.get("study_id") != study["id"]:
            raise ConflictError("研究结果不属于当前研究节点")
        return result

    def publish_result(self, study, index, result, prior_hash):
        # The runner holds its process lock; the CAS below detects another
        # process. A late withdrawal is checked again just before publication.
        self.node_input(study, index)
        value = {"schema_version": 1, "study_id": study["id"], "node_id": study["nodes"][index]["id"],
                 "read_index": index, "prior_checkpoint_hash": prior_hash,
                 "temporal_mode": "first_read" if study["plan"]["request"]["kind"] == "reading" else "retrospective",
                 "created_at": utc_now(), "result": result}
        path = self.path(study["id"] + "/results/" + study["nodes"][index]["id"] + ".json")
        if path.exists():
            # A process may have stopped after publishing an immutable result
            # but before its manifest CAS. Explicit response repair can retain
            # that orphan and publish a new candidate without any new API call.
            orphan = self.path(study["id"] + "/orphans/" + study["nodes"][index]["id"] + "-" + uuid.uuid4().hex + ".json")
            orphan.parent.mkdir(parents=True, exist_ok=True)
            path.rename(orphan)
        self._write(path, value)
        study["nodes"][index].update(state="succeeded", result_hash=digest(json_bytes(value)), error=None)
        self.save(study)
        return value

    def export_verified(self, study_id):
        study = self.load(study_id)
        records, allowed, characters, events, previous = [], [], set(), set(), None
        for index, node in enumerate(study["nodes"]):
            if node["state"] != "succeeded":
                break  # No look-ahead through a gap in the reading chain.
            snapshots = self.node_input(study, index)
            current = [ref for snapshot in snapshots for ref in snapshot["refs"]]
            wrapper = self.node_result(study, index)
            if wrapper.get("prior_checkpoint_hash") != previous:
                raise ConflictError("连续阅读检查点链失效，不能将后文回填为初读知识")
            if study["plan"]["request"]["kind"] == "comparison":
                result = validate_comparison({key: value for key, value in wrapper["result"].items()
                    if key in comparison_contract()}, current, {item["book_id"] for item in snapshots})
            else:
                result = validate_reading({key: value for key, value in wrapper["result"].items()
                    if key in reading_contract()}, current + allowed, characters, events)
            allowed.extend(all_refs(result))
            characters.update(item["key"] for item in result.get("characters", []))
            events.update(item["key"] for item in result.get("events", []))
            records.append({"node_id": node["id"], "read_index": index, "temporal_mode": wrapper["temporal_mode"],
                            "result": result, "refs": [{key: ref[key] for key in
                            ("book_id", "edition_id", "chapter_id", "start_char", "end_char", "hash", "range_hash")} for ref in current],
                            "result_hash": node["result_hash"]})
            previous = node["result_hash"]
        graphs = compile_graphs(records)
        current_map = study.get("map_version")
        map_stale = False
        if current_map:
            version = self.map_version(study_id, current_map)
            if digest(json_bytes(version)) != study.get("map_version_hash"):
                raise ConflictError("人工图谱版本被修改，不能作为已核对研究来源")
            if version["base_results_hash"] != digest(json_bytes([node["result_hash"] for node in records])):
                # Continuing a paused study never lets an old manual map
                # masquerade as a map of newly read chapters. Keep that map
                # archived, and display the newly compiled candidate instead.
                map_stale = True
            else:
                graphs = self.validate_graphs(version["graphs"], allowed)
        return {**self.summary(study), "results": records, "graphs": graphs,
                "map_version": current_map, "map_version_stale": map_stale,
                "coverage": {**study["plan"]["coverage"], "completed_segments": len(records),
                "total_segments": len(study["nodes"]), "complete_selected_file": len(records) == len(study["nodes"]),
                "claim": "只覆盖已发送并成功校验的选定原文；分数/关系/因果均是候选判断，非完整文学理解"}}

    def map_version(self, study_id, version_id):
        identity(study_id); identity(version_id)
        return read_json(self.path(study_id + "/maps/" + version_id + ".json"), max_bytes=MAX_RECORD)

    def preview_methods(self, study_id, indices):
        export = self.export_verified(study_id)
        candidates = [method for row in export["results"] for method in row["result"].get("methods", [])]
        if (not isinstance(indices, list) or not 1 <= len(indices) <= 6 or len(set(indices)) != len(indices)
                or any(type(index) is not int or not 0 <= index < len(candidates) for index in indices)):
            raise VibeNovelError("请选择1至6个有效、不重复的方法索引")
        methods = [{"index": index, **copy.deepcopy(candidates[index])} for index in indices]
        books = []
        for source in export["sources"]:
            detail = self.books.detail(source["book_id"], source["edition_id"], limit=1)
            books.append({"book_id": source["book_id"], "edition_id": source["edition_id"],
                          "authorization_revision": detail["authorization_revision"]})
        evidence = [ref for method in methods for ref in method["source_refs"]]
        origin = {"schema_version": 1, "source_type": "reference_study", "study_id": study_id,
                  "study_hash": export["hash"], "book_id": books[0]["book_id"], "books": books,
                  "evidence": evidence, "indices": copy.deepcopy(indices)}
        value = {"methods": methods, "origin": origin, "purpose": export["question"],
                 "warnings": ["引文验证仅确认出处；方法尚未经效果评阅或作者采用", "研究范围之外的内容未精读"]}
        value["hash"] = digest(json_bytes(value))
        return value

    def validate_origin(self, origin):
        if not isinstance(origin, dict) or origin.get("source_type") != "reference_study":
            raise ConflictError("研究方法来源类型无效")
        actual = self.preview_methods(origin.get("study_id"), origin.get("indices"))["origin"]
        if actual != origin:
            raise ConflictError("研究方法所依赖的原文、授权、阅读检查点或图版本已变化")
        return True

    def validate_graphs(self, graphs, allowed):
        _fields(graphs, GROUPS)
        for group in GROUPS:
            graph = graphs[group]
            _fields(graph, ("nodes", "edges"))
            ids = set()
            for node in _items(graph["nodes"], 20000, "图节点"):
                if not isinstance(node, dict) or set(node) - {"id", "label", "description", "source_refs", "read_index", "kind", "tension", "valence", "status"}:
                    raise VibeNovelError("图节点字段无效")
                node_id = text(node.get("id"), 250, "图节点ID")
                if node_id in ids:
                    raise VibeNovelError("图节点ID重复")
                ids.add(node_id)
                text(node.get("label"), 500, "图节点标签")
                text(node.get("description", ""), 3000, "图节点说明", empty=True)
                checked_refs(node.get("source_refs"), allowed)
            for edge in _items(graph["edges"], 40000, "图边"):
                _fields(edge, ("from", "to", "label", "source_refs", "read_index"))
                if edge["from"] not in ids or edge["to"] not in ids:
                    raise VibeNovelError("图关系端点不存在")
                text(edge["label"], 1000, "关系标签")
                checked_refs(edge["source_refs"], allowed)
        if len(json_bytes(graphs)) > MAX_RECORD - 65536:
            raise VibeNovelError("四组图超过8MiB；未截断")
        return copy.deepcopy(graphs)

    def edit_graphs(self, study_id, expected_hash, graphs, reason):
        export = self.export_verified(study_id)
        if export["state"] in ("running", "pausing"):
            raise ConflictError("精读仍在运行，请先暂停并等待当前请求完成后修改候选图")
        if export["hash"] != expected_hash:
            raise ConflictError("研究版本变化，保留编辑内容并刷新")
        if not export["results"]:
            raise VibeNovelError("尚无成功精读证据，不能凭空建立已读图谱")
        allowed = [ref for row in export["results"] for ref in all_refs(row["result"])]
        checked = self.validate_graphs(graphs, allowed)
        study = self.load(study_id)
        version_id = uuid.uuid4().hex
        value = {"schema_version": 1, "id": version_id, "study_id": study_id,
                 "parent_version": study.get("map_version"), "created_at": utc_now(),
                 "reason": text(reason, 2000, "编辑说明"), "provenance": "AUTHOR_EDITED_CANDIDATE",
                 "base_results_hash": digest(json_bytes([node["result_hash"] for node in export["results"]])), "graphs": checked}
        self._write(self.path(study_id + "/maps/" + version_id + ".json"), value)
        study["map_version"] = version_id
        study["map_version_hash"] = digest(json_bytes(value))
        self.save(study)
        return self.export_verified(study_id)


def validate_study_origin(workspace, origin):
    return ReferenceStudiesStore(workspace).validate_origin(origin)


def compile_graphs(records):
    graphs = {group: {"nodes": [], "edges": []} for group in GROUPS}
    character_keys, event_keys, thread_keys = {}, {}, {}
    previous_rhythm = None
    for row in records:
        result, index, prefix = row["result"], row["read_index"], row["node_id"]
        def node(group, label, description, refs, suffix, **extra):
            value = {"id": prefix + ":" + suffix, "label": label, "description": description,
                     "source_refs": refs, "read_index": index, **extra}
            graphs[group]["nodes"].append(value)
            return value["id"]
        def edge(group, left, right, label, refs):
            graphs[group]["edges"].append({"from": left, "to": right, "label": label,
                                             "source_refs": refs, "read_index": index})
        for item in result.get("events", []):
            target = node("causal", item["title"], item["action"] + " → " + item["outcome"], item["source_refs"], "event:" + item["key"], kind=item["claim_type"])
            for cause in item["caused_by"]:
                if cause in event_keys:
                    edge("causal", event_keys[cause], target, "因果候选", item["source_refs"])
            event_keys[item["key"]] = target
        for item in result.get("characters", []):
            target = node("characters", item["name"], item["goal"] + "；" + item["state"], item["source_refs"], "character:" + item["key"], kind="character")
            if item["key"] in character_keys:
                edge("characters", character_keys[item["key"]], target, "后续状态", item["source_refs"])
            character_keys[item["key"]] = target
        for item in result.get("relationships", []):
            if item["from"] in character_keys and item["to"] in character_keys:
                edge("characters", character_keys[item["from"]], character_keys[item["to"]], item["change"], item["source_refs"])
        for number, item in enumerate(result.get("knowledge", [])):
            target = node("characters", ("读者" if item["subject"] == "reader" else item["subject"]) + "：" + item["fact"], item["fact"], item["source_refs"], "knowledge:" + str(number), kind="knowledge", status=item["status"])
            if item["subject"] in character_keys:
                edge("characters", character_keys[item["subject"]], target, item["status"], item["source_refs"])
        for item in result.get("threads", []):
            refs = item["setup_refs"] + item["payoff_refs"]
            if not refs:
                continue
            target = node("information", item["title"], item["explanation"], refs, "thread:" + item["key"], status=item["state"])
            if item["key"] in thread_keys:
                edge("information", thread_keys[item["key"]], target, item["state"], refs)
            thread_keys[item["key"]] = target
        for number, item in enumerate(result.get("rhythm", [])):
            target = node("rhythm", item["label"], item["emotion"], item["source_refs"], "rhythm:" + str(number), tension=item["tension"], valence=item["valence"])
            if previous_rhythm:
                edge("rhythm", previous_rhythm, target, "阅读顺序", item["source_refs"])
            previous_rhythm = target
    return graphs
