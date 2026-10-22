"""Versioned private research libraries. Resources never acquire canon rights.

Immutable objects are prepared before one manifest is published. The journal
only finishes local publication; it never replays a model request. This is a
single-library transaction, not a transaction spanning libraries or novels.
"""
from __future__ import annotations

import codecs
import copy
import json
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from vibenovel.errors import VibeNovelError
from vibenovel.importers.text import EXTRACTOR_VERSION, _paragraph_refs, _select_encoding
from .files import atomic_write, digest, json_bytes, read_json, safe_path
from .locking import ConflictError, project_lock
from .project import Project, ProjectStore, utc_now
from .reference_methods import (preview_reference_methods as _preview_reference_methods,
                                validate_research_origin, MAX_RESEARCH_TASK_DEPENDENCIES_BYTES)

ENTRY_TYPES = ("literature", "material", "technique", "style", "term", "method")
OUTBOUND = ("local", "summary", "excerpts")
PURPOSES = ("reference", "writing", "planning", "review", "rewrite", "method_extraction")
KINDS = ("source", "entry", "binding", "collection", "question", "policy", "bundle")
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_TASK_CHARACTERS = 60000
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_POLICY_ID = "0" * 32
_METHOD_FIELDS = ("steps", "inputs", "outputs", "limitations", "applies_to", "counterexamples", "checks")


def _text(value, maximum=2000, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise VibeNovelError("资料字段需要有界文本")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise VibeNovelError("资料含无效Unicode字符") from exc
    if "\x00" in value:
        raise VibeNovelError("资料含NUL字符")
    return value


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise VibeNovelError("资料ID需要32位小写十六进制")
    return value


def _strings(value, maximum=50, length=200):
    if not isinstance(value, list) or len(value) > maximum:
        raise VibeNovelError("资料列表超过上限或格式无效")
    return [_text(item, length, empty=False) for item in value]


def default_policy():
    return {"mode": "manual", "types": list(ENTRY_TYPES), "purposes": list(PURPOSES),
            "collections": [], "required_ids": [], "excluded_ids": [],
            "max_items": 8, "max_characters": 12000}


def _policy(payload):
    if not isinstance(payload, dict) or set(payload) != set(default_policy()):
        raise VibeNovelError("资料检索策略字段无效")
    value = copy.deepcopy(payload)
    if value["mode"] not in ("manual", "recommend", "auto"):
        raise VibeNovelError("资料检索模式无效")
    for name, allowed in (("types", ENTRY_TYPES), ("purposes", PURPOSES)):
        items = _strings(value[name])
        if len(set(items)) != len(items) or any(item not in allowed for item in items):
            raise VibeNovelError("资料策略类型或用途无效")
    for name in ("collections", "required_ids", "excluded_ids"):
        items = _strings(value[name], 100)
        if len(set(items)) != len(items):
            raise VibeNovelError("资料策略ID重复")
        for item in items:
            _identity(item)
    if set(value["required_ids"]) & set(value["excluded_ids"]):
        raise VibeNovelError("必选资料不能同时排除")
    for name, upper in (("max_items", 30), ("max_characters", MAX_TASK_CHARACTERS)):
        if type(value[name]) is not int or not 1 <= value[name] <= upper:
            raise VibeNovelError("资料策略预算无效")
    return value


class _LockOwner:
    def __init__(self, root):
        self.root = root

    def path(self, relative):
        return safe_path(self.root, ".state/" + relative)


class LibraryStore:
    def __init__(self, workspace: Path, project: Project = None):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise VibeNovelError("资料库工作区不存在")
        if project is not None:
            project = ProjectStore(self.workspace).get(project.novel_id)
        self.project = project
        self.scope = "book" if project else "personal"
        self.owner = project.novel_id if project else "personal"
        self.root = project.path("library") if project else safe_path(self.workspace, "library")

    def path(self, relative):
        # Revalidate the root from the trusted workspace on every operation.
        self.root = (self.project.path("library") if self.project else
                     safe_path(self.workspace, "library"))
        return safe_path(self.root, relative)

    @contextmanager
    def _locked(self):
        self.path(".state/.runtime").mkdir(parents=True, exist_ok=True)
        with project_lock(_LockOwner(self.root), timeout=2):
            self._recover()
            yield self._state()

    def _state(self):
        path = self.path(".state/manifest.json")
        if not path.exists():
            return {"schema_version": 1, "library_id": None, "scope": self.scope,
                    "owner": self.owner, "revision": 0, "entities": {kind: {} for kind in KINDS}}
        value = read_json(path, max_bytes=MAX_STATE_BYTES)
        if (set(value) != {"schema_version", "library_id", "scope", "owner", "revision", "entities"}
                or value["schema_version"] != 1 or value["scope"] != self.scope or value["owner"] != self.owner
                or type(value["revision"]) is not int or value["revision"] < 1
                or not isinstance(value["library_id"], str) or not _ID.fullmatch(value["library_id"])
                or not isinstance(value["entities"], dict) or set(value["entities"]) != set(KINDS)):
            raise VibeNovelError("资料库清单版本或所属范围无效")
        count = 0
        for kind, entries in value["entities"].items():
            if not isinstance(entries, dict):
                raise VibeNovelError("资料库清单格式无效")
            for identity, versions in entries.items():
                _identity(identity)
                if not isinstance(versions, list) or not versions:
                    raise VibeNovelError("资料历史清单无效")
                seen = set()
                for version in versions:
                    count += 1
                    if (not isinstance(version, dict) or set(version) != {"version_id", "hash"}
                            or not isinstance(version["hash"], str) or not _HASH.fullmatch(version["hash"])):
                        raise VibeNovelError("资料历史版本格式无效")
                    _identity(version["version_id"])
                    if version["version_id"] in seen:
                        raise VibeNovelError("资料历史版本重复")
                    seen.add(version["version_id"])
        if count > 10000:
            raise VibeNovelError("资料库最多保留10000个记录版本，请拆分资料")
        return value

    def _manifest_hash(self):
        path = self.path(".state/manifest.json")
        return digest(path.read_bytes()) if path.exists() else None

    def _record_path(self, kind, identity, version):
        if kind not in KINDS:
            raise VibeNovelError("资料记录类型无效")
        return self.path("records/%s/%s/%s.json" % (kind, _identity(identity), _identity(version)))

    def _recover(self):
        journal_path = self.path(".state/journal.json")
        if not journal_path.exists():
            return
        journal = read_json(journal_path, max_bytes=2 * MAX_STATE_BYTES)
        if (set(journal) != {"schema_version", "scope", "owner", "before_hash", "after_hash", "after"}
                or journal["schema_version"] != 1 or journal["scope"] != self.scope
                or journal["owner"] != self.owner or digest(json_bytes(journal["after"])) != journal["after_hash"]):
            raise ConflictError("资料库恢复意图无效，未覆盖清单")
        current = self._manifest_hash()
        if current not in (journal["before_hash"], journal["after_hash"]):
            raise ConflictError("资料库清单与恢复意图冲突，未覆盖")
        after = journal["after"]
        if (not isinstance(after, dict) or after.get("scope") != self.scope or after.get("owner") != self.owner
                or not isinstance(after.get("entities"), dict) or set(after["entities"]) != set(KINDS)):
            raise ConflictError("资料库恢复目标无效")
        for kind, entries in after["entities"].items():
            for identity, versions in entries.items():
                for version in versions:
                    path = self._record_path(kind, identity, version["version_id"])
                    if not path.is_file() or digest(path.read_bytes()) != version["hash"]:
                        raise ConflictError("资料库待发布记录缺失或损坏，未发布半套资料")
        if current != journal["after_hash"]:
            before_state = self._state()
            # A different library can revoke a personal source while a book's
            # local publication is interrupted. Recheck pending candidate input
            # before exposing the whole bundle; do not replay any generation.
            try:
                for identity, versions in after["entities"]["bundle"].items():
                    if identity in before_state["entities"]["bundle"]:
                        continue
                    record = read_json(self._record_path("bundle", identity, versions[-1]["version_id"]), max_bytes=MAX_RECORD_BYTES)
                    if record["data"].get("research_origin") is not None:
                        validate_research_origin(self.workspace, record["data"]["research_origin"])
                    self._check_analysis(before_state, record["data"].get("expected_analysis"))
                    snapshot = record["data"].get("task_snapshot")
                    if snapshot is not None:
                        origin = self._snapshot_store(snapshot)
                        if origin is self:
                            self._validate_snapshot_state(snapshot, before_state)
                        else:
                            origin.validate_task_snapshot(snapshot)
            except VibeNovelError as exc:
                rejected = self.path(".state/rejected-" + uuid4().hex + ".json")
                atomic_write(rejected, json_bytes(journal))
                journal_path.unlink()
                raise ConflictError("待发布资料来源已失效，保留拒绝记录与候选对象，未发布半套资料") from exc
            atomic_write(self.path(".state/manifest.json"), json_bytes(after), overwrite=current is not None)
        self._state()  # Strict schema validation before removing recovery evidence.
        journal_path.unlink()

    def _read(self, state, kind, identity, version_id=None):
        _identity(identity)
        versions = state["entities"][kind].get(identity, [])
        selected = next((v for v in versions if v["version_id"] == version_id), None) if version_id else (versions[-1] if versions else None)
        if selected is None:
            raise VibeNovelError("未找到所选资料版本")
        path = self._record_path(kind, identity, selected["version_id"])
        record = read_json(path, max_bytes=MAX_RECORD_BYTES)
        if (digest(path.read_bytes()) != selected["hash"] or record.get("schema_version") != 1
                or record.get("library_id") != state["library_id"] or record.get("scope") != self.scope
                or record.get("owner") != self.owner or record.get("id") != identity
                or record.get("kind") != kind or record.get("version_id") != selected["version_id"]
                or not isinstance(record.get("data"), dict)):
            raise ConflictError("资料版本内容或所属范围校验失败")
        return {**copy.deepcopy(record["data"]), "id": identity, "kind": kind, "scope": self.scope,
                "owner": self.owner, "version_id": record["version_id"], "hash": selected["hash"],
                "revision": record["revision"], "created_at": record["created_at"]}

    def _check_expected(self, state, kind, identity, expected_hash):
        versions = state["entities"][kind].get(identity, [])
        actual = versions[-1]["hash"] if versions else None
        if actual != expected_hash:
            raise ConflictError("资料版本已变化，请刷新后保存")

    def _commit(self, state, changes):
        after = copy.deepcopy(state)
        after["library_id"] = after["library_id"] or uuid4().hex
        after["revision"] += 1
        outputs = []
        for kind, identity, data in changes:
            version_id = uuid4().hex
            record = {"schema_version": 1, "library_id": after["library_id"], "scope": self.scope,
                      "owner": self.owner, "id": identity, "kind": kind, "version_id": version_id,
                      "revision": after["revision"], "created_at": utc_now(), "data": data}
            raw = json_bytes(record)
            if len(raw) > MAX_RECORD_BYTES:
                raise VibeNovelError("资料记录超过16MiB上限")
            path = self._record_path(kind, identity, version_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, raw)
            after["entities"][kind].setdefault(identity, []).append({"version_id": version_id, "hash": digest(raw)})
            outputs.append((kind, identity, version_id))
        encoded = json_bytes(after)
        if len(encoded) > MAX_STATE_BYTES or sum(len(v) for items in after["entities"].values() for v in items.values()) > 10000:
            raise VibeNovelError("资料库历史达到上限，未发布本次变更")
        before_hash = self._manifest_hash()
        journal = {"schema_version": 1, "scope": self.scope, "owner": self.owner,
                   "before_hash": before_hash, "after_hash": digest(encoded), "after": after}
        atomic_write(self.path(".state/journal.json"), json_bytes(journal))
        # Recheck after staging immutable objects and before manifest publish.
        # A concurrent book-store policy change still is not a cross-lock
        # transaction; journal recovery independently repeats this guard.
        for kind, _, data in changes:
            if kind == "bundle" and data.get("research_origin") is not None:
                validate_research_origin(self.workspace, data["research_origin"])
        atomic_write(self.path(".state/manifest.json"), encoded, overwrite=before_hash is not None)
        self.path(".state/journal.json").unlink()
        return [self._read(after, kind, identity, version) for kind, identity, version in outputs]

    def _data(self, record):
        return {k: copy.deepcopy(v) for k, v in record.items() if k not in
                {"id", "kind", "scope", "owner", "version_id", "hash", "revision", "created_at", "adopted", "adoption_version_id"}}

    def overview(self):
        with self._locked() as state:
            result = {"scope": self.scope, "owner": self.owner, "library_id": state["library_id"],
                      "revision": state["revision"], "hash": self._manifest_hash()}
            for kind, key in (("entry", "entries"), ("source", "sources"), ("collection", "collections"),
                              ("question", "questions"), ("binding", "bindings")):
                result[key] = [self._read(state, kind, identity) for identity in state["entities"][kind]]
            result["entries"] = [self._decorate_entry(entry) for entry in result["entries"]]
            # Source full text is retrieved through its dedicated endpoint.
            for source in result["sources"]:
                source.pop("text", None)
            policy = self._policy_record(state)
            result.update(policy=policy["policy"], policy_hash=policy["hash"], policy_version_id=policy["version_id"])
            return result

    def import_source(self, filename, content: bytes, encoding=None, metadata=None):
        filename = _text(filename, 255, empty=False)
        if "/" in filename or "\\" in filename or any(ord(c) < 32 for c in filename):
            raise VibeNovelError("来源文件名无效")
        suffix = Path(filename).suffix.lower()
        if suffix not in (".md", ".txt"):
            raise VibeNovelError("资料原件暂只支持MD/TXT")
        if not isinstance(content, bytes) or not content or len(content) > MAX_SOURCE_BYTES:
            raise VibeNovelError("资料原件需要1字节至10MiB")
        selected = _select_encoding(content, encoding)
        if selected == "utf-16" and not content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            raise VibeNovelError("UTF-16原件需要BOM")
        try:
            text = content.decode(selected, errors="strict")
        except UnicodeError as exc:
            raise VibeNovelError("资料原件不能按指定编码严格解码") from exc
        _text(text, MAX_SOURCE_BYTES, empty=False)
        metadata = self._source_metadata(metadata or {})
        extracted = text.encode("utf-8")
        if len(extracted) > MAX_SOURCE_BYTES:
            raise VibeNovelError("UTF-8抽取文本超过10MiB上限，请分文件导入；未发布来源")
        original_hash, text_hash = digest(content), digest(extracted)
        with self._locked() as state:
            # Same bytes may have distinct bibliographic identities. Blob bytes
            # are deduplicated, but first-import metadata never replaces another.
            for key in (original_hash, text_hash):
                raw = content if key == original_hash else extracted
                path = self.path("blobs/" + key)
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    if digest(path.read_bytes()) != key:
                        raise ConflictError("资料原件缓存损坏，未覆盖")
                else:
                    atomic_write(path, raw)
            data = {"filename": filename, "format": suffix[1:], "encoding": selected,
                    "original_sha256": original_hash, "text_sha256": text_hash, "source_bytes": len(content),
                    "extractor_version": EXTRACTOR_VERSION, "source_refs": _paragraph_refs(text),
                    "metadata": metadata, "outbound": metadata.get("outbound", "local"),
                    "withdrawn": False, "withdrawal_reason": "", "authorization_revision": uuid4().hex}
            source = self._commit(state, [("source", uuid4().hex, data)])[0]
            return self._source_text(source)

    @staticmethod
    def _source_metadata(payload):
        allowed = {"title", "author", "edition", "url", "doi", "license", "notes", "outbound", "summary"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise VibeNovelError("书目信息含不支持字段")
        value = {k: _text(v, 12000 if k in ("notes", "summary") else 2000) for k, v in payload.items()}
        if value.get("outbound", "local") not in OUTBOUND:
            raise VibeNovelError("资料外发方式无效")
        return value

    def _source_text(self, source):
        for field in ("original_sha256", "text_sha256"):
            hashed = source.get(field)
            if not isinstance(hashed, str) or not _HASH.fullmatch(hashed):
                raise ConflictError("资料原件hash无效")
            path = self.path("blobs/" + hashed)
            if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES or digest(path.read_bytes()) != hashed:
                raise ConflictError("资料原件或抽取文本缺失/损坏")
        result = copy.deepcopy(source)
        result["text"] = self.path("blobs/" + source["text_sha256"]).read_bytes().decode("utf-8")
        if _paragraph_refs(result["text"]) != source["source_refs"]:
            raise ConflictError("资料来源区间不匹配")
        return result

    def source(self, identity, version_id=None):
        with self._locked() as state:
            return self._source_text(self._read(state, "source", identity, version_id))

    def save_source_metadata(self, identity, payload, expected_hash):
        if not isinstance(payload, dict) or set(payload) - {"metadata", "outbound", "withdrawn", "withdrawal_reason"}:
            raise VibeNovelError("来源更新字段无效")
        with self._locked() as state:
            self._check_expected(state, "source", identity, expected_hash)
            data = self._data(self._read(state, "source", identity))
            metadata = self._source_metadata(payload.get("metadata", data["metadata"]))
            outbound = payload.get("outbound", metadata.get("outbound", data["outbound"]))
            if outbound not in OUTBOUND or type(payload.get("withdrawn", data["withdrawn"])) is not bool:
                raise VibeNovelError("来源外发或撤回状态无效")
            metadata["outbound"] = outbound
            if (outbound, payload.get("withdrawn", data["withdrawn"])) != (data["outbound"], data["withdrawn"]):
                data["authorization_revision"] = uuid4().hex
            data.update(metadata=metadata, outbound=outbound,
                        withdrawn=payload.get("withdrawn", data["withdrawn"]),
                        withdrawal_reason=_text(payload.get("withdrawal_reason", data["withdrawal_reason"])))
            return self._source_text(self._commit(state, [("source", identity, data)])[0])

    def _refs(self, refs, state):
        if not isinstance(refs, list) or len(refs) > 40:
            raise VibeNovelError("来源引用最多40条")
        outputs = []
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) - {"scope", "owner", "source_id", "source_version_id", "start_char", "end_char", "quote", "start_line", "end_line", "hash"}:
                raise VibeNovelError("资料引用字段无效")
            scope, owner = ref.get("scope", self.scope), ref.get("owner", self.owner)
            if scope == self.scope and owner == self.owner:
                source = self._source_text(self._read(state, "source", ref.get("source_id"), ref.get("source_version_id")))
                current = self._read(state, "source", source["id"])
            elif scope == "personal" and owner == "personal" and self.project is not None:
                personal = LibraryStore(self.workspace)
                source = personal.source(ref.get("source_id"), ref.get("source_version_id"))
                current = personal.source(source["id"])
            else:
                raise VibeNovelError("来源引用不能读取另一作品或未声明范围")
            if source["withdrawn"] or current["withdrawn"]:
                raise ConflictError("所引用来源已撤回")
            start, end = ref.get("start_char"), ref.get("end_char")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source["text"]) or end - start > MAX_TASK_CHARACTERS:
                raise VibeNovelError("来源引用需要有效Unicode字符半开区间")
            quote = source["text"][start:end]
            if "quote" in ref and ref["quote"] != quote:
                raise ConflictError("来源引用原句与版本区间不一致")
            line_start = len(source["text"][:start].splitlines()) + (1 if start and source["text"][:start].endswith(tuple("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")) else 0)
            line_end = len(source["text"][:end].splitlines()) or 1
            outputs.append({"scope": scope, "owner": owner, "source_id": source["id"], "source_version_id": source["version_id"],
                            "hash": source["hash"], "start_char": start, "end_char": end,
                            "start_line": max(1, line_start), "end_line": line_end, "quote": quote})
        return outputs

    def _entry_payload(self, payload, state, actor):
        allowed = {"id", "type", "title", "content", "tags", "summary", "outbound", "status", "verification",
                   "provenance", "source_refs", "method", "analysis", "withdrawn", "withdrawal_reason"}
        if not isinstance(payload, dict) or set(payload) - allowed or actor not in ("AUTHOR", "AI_SUGGESTED"):
            raise VibeNovelError("资料卡字段或操作者无效")
        if payload.get("type") not in ENTRY_TYPES:
            raise VibeNovelError("资料卡类型无效")
        value = {"type": payload["type"], "title": _text(payload.get("title"), 300, empty=False),
                 "content": _text(payload.get("content", ""), 60000),
                 "tags": _strings(payload.get("tags", []), 30, 100), "summary": _text(payload.get("summary", ""), 12000),
                 "outbound": payload.get("outbound", "local"), "status": payload.get("status", "inbox"),
                 "verification": payload.get("verification", "unverified"), "provenance": actor,
                 "source_refs": self._refs(payload.get("source_refs", []), state),
                 "withdrawn": payload.get("withdrawn", False), "withdrawal_reason": _text(payload.get("withdrawal_reason", "")),
                 "adoption": None}
        if (value["outbound"] not in OUTBOUND or value["status"] not in ("inbox", "ready", "archived")
                or value["verification"] not in ("unverified", "checked", "disputed", "needs_evidence")
                or type(value["withdrawn"]) is not bool):
            raise VibeNovelError("资料卡状态/外发方式无效")
        if actor == "AI_SUGGESTED":
            value.update(outbound="local", status="inbox", verification="unverified", withdrawn=False, withdrawal_reason="")
        if payload["type"] == "method":
            method = payload.get("method", {})
            if not isinstance(method, dict) or set(method) - {"purpose", *_METHOD_FIELDS}:
                raise VibeNovelError("方法卡仅接受声明式方法字段")
            value["method"] = {"purpose": _text(method.get("purpose", ""), 4000),
                               **{key: _strings(method.get(key, []), 40, 3000) for key in _METHOD_FIELDS}}
        elif "method" in payload:
            raise VibeNovelError("只有方法卡接受method字段")
        if "analysis" in payload:
            if payload["type"] != "technique" or not isinstance(payload["analysis"], dict) or len(json_bytes(payload["analysis"])) > 128 * 1024:
                raise VibeNovelError("来源分析需要有界technique分析对象")
            value["analysis"] = copy.deepcopy(payload["analysis"])
        return value

    def save_entry(self, payload, expected_hash=None, actor="AUTHOR"):
        with self._locked() as state:
            if not isinstance(payload, dict):
                raise VibeNovelError("资料卡字段无效")
            identity = payload.get("id") or uuid4().hex
            self._check_expected(state, "entry", identity, expected_hash)
            previous = self._read(state, "entry", identity) if identity in state["entities"]["entry"] else None
            supplied_origin = payload.get("research_origin")
            origin = previous.get("research_origin") if previous else None
            if "research_origin" in payload and (origin is None or supplied_origin != origin):
                raise VibeNovelError("研究来源只能由成功研究节点发布，不能自填、复制或删除")
            editable = {key: value for key, value in payload.items() if key != "research_origin"}
            if origin is not None:
                if editable.get("type") != "method":
                    raise VibeNovelError("研究方法不能改为普通资料卡以移除来源约束")
                try:
                    validate_research_origin(self.workspace, origin)
                except VibeNovelError:
                    # A withdrawn/broken external source must not trap an
                    # author in an unmanageable inbox. This branch permits
                    # only a terminal restriction, never content repair,
                    # source removal, permission restoration or adoption.
                    data = self._restrict_invalid_research_entry(previous, editable, actor)
                else:
                    data = self._entry_payload(editable, state, actor)
            else:
                data = self._entry_payload(editable, state, actor)
            if origin is not None:
                data["research_origin"] = copy.deepcopy(origin)
            same_permission = previous is not None and (previous["outbound"], previous["withdrawn"]) == (data["outbound"], data["withdrawn"])
            data["authorization_revision"] = previous["authorization_revision"] if same_permission else uuid4().hex
            return self._decorate_entry(self._commit(state, [("entry", identity, data)])[0])

    def _restrict_invalid_research_entry(self, previous, payload, actor):
        restricted = {"outbound", "status", "withdrawn", "withdrawal_reason"}
        accepted = {"id", "type", "title", "content", "tags", "summary", "outbound", "status", "verification",
                    "provenance", "source_refs", "method", "analysis", "withdrawn", "withdrawal_reason"}
        if (actor != "AUTHOR" or set(payload) - accepted
                or any(value != previous.get(key) for key, value in payload.items() if key not in restricted)):
            raise ConflictError("研究来源失效后仅可收紧外发、归档或撤回；不能修改内容或删除来源")
        value = {name: payload.get(name, previous[name]) for name in restricted}
        value["withdrawal_reason"] = _text(value["withdrawal_reason"])
        if (value["outbound"] not in OUTBOUND or value["status"] not in ("inbox", "ready", "archived")
                or type(value["withdrawn"]) is not bool
                or OUTBOUND.index(value["outbound"]) > OUTBOUND.index(previous["outbound"])
                or value["status"] != previous["status"] and value["status"] != "archived"
                or previous["withdrawn"] and not value["withdrawn"]
                or not (value["outbound"] == "local" or value["status"] == "archived" or value["withdrawn"])):
            raise ConflictError("失效研究方法只能保留或收紧权限，不能恢复为可用状态")
        data = self._data(previous)
        data.update(value, adoption=None, provenance="AUTHOR")
        return data

    @staticmethod
    def _decorate_entry(entry):
        entry["adopted"] = entry.get("adoption") is not None
        entry["adoption_version_id"] = entry.get("adoption", {}).get("id") if entry.get("adoption") else None
        return entry

    def entry(self, identity, version_id=None):
        with self._locked() as state:
            return self._decorate_entry(self._read(state, "entry", identity, version_id))

    def history(self, kind, identity):
        if kind not in KINDS:
            raise VibeNovelError("资料历史类型无效")
        with self._locked() as state:
            return [self._read(state, kind, identity, v["version_id"]) for v in reversed(state["entities"][kind].get(_identity(identity), []))]

    def _save_list_record(self, kind, payload, expected_hash):
        allowed = ({"id", "title", "description", "entry_ids"} if kind == "collection" else
                   {"id", "title", "question", "notes", "status", "entry_ids"})
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise VibeNovelError("专题或研究问题字段无效")
        data = {"title": _text(payload.get("title"), 300, empty=False), "entry_ids": _strings(payload.get("entry_ids", []), 200)}
        if kind == "collection":
            data["description"] = _text(payload.get("description", ""), 12000)
        else:
            data.update(question=_text(payload.get("question", ""), 12000, empty=False),
                        notes=_text(payload.get("notes", ""), 30000), status=payload.get("status", "open"))
            if data["status"] not in ("open", "resolved", "disputed"):
                raise VibeNovelError("研究问题状态无效")
        with self._locked() as state:
            for identity in data["entry_ids"]:
                self._read(state, "entry" if identity in state["entities"]["entry"] else "binding", identity)
            identity = payload.get("id") or uuid4().hex
            self._check_expected(state, kind, identity, expected_hash)
            return self._commit(state, [(kind, identity, data)])[0]

    def save_collection(self, payload, expected_hash=None):
        return self._save_list_record("collection", payload, expected_hash)

    def save_question(self, payload, expected_hash=None):
        return self._save_list_record("question", payload, expected_hash)

    def collections(self):
        return self.overview()["collections"]

    def questions(self):
        return self.overview()["questions"]

    def _policy_record(self, state):
        if not state["entities"]["policy"].get(_POLICY_ID):
            return {"policy": default_policy(), "hash": None, "version_id": None}
        return self._read(state, "policy", _POLICY_ID)

    def save_policy(self, payload, expected_hash=None):
        value = _policy(payload)
        with self._locked() as state:
            self._check_expected(state, "policy", _POLICY_ID, expected_hash)
            for identity in value["collections"]:
                self._read(state, "collection", identity)
            for identity in value["required_ids"] + value["excluded_ids"]:
                if identity not in state["entities"]["entry"] and identity not in state["entities"]["binding"]:
                    raise VibeNovelError("策略引用的资料不存在")
            return self._commit(state, [("policy", _POLICY_ID, {"policy": value})])[0]

    def bind(self, entry_id, expected_hash=None, *, version_id=None, source_scope="personal", purpose="reference", binding_id=None):
        if self.project is None or source_scope != "personal" or purpose not in PURPOSES:
            raise VibeNovelError("仅本书可从个人库固定选用资料")
        personal = LibraryStore(self.workspace)
        entry = personal.entry(entry_id, version_id)
        current_entry = personal.entry(entry_id)
        personal._validate_research_entry(entry)
        personal._validate_research_entry(current_entry)
        if entry.get("withdrawn") or current_entry["withdrawn"]:
            raise ConflictError("所选资料已撤回")
        if OUTBOUND.index(current_entry["outbound"]) < OUTBOUND.index(entry["outbound"]):
            raise ConflictError("所选旧版本超出当前资料外发权限")
        source_keys = dict.fromkeys((ref["source_id"], ref["source_version_id"]) for ref in entry["source_refs"])
        sources = [personal.source(identity, version) for identity, version in source_keys]
        source_authorizations = {}
        for source in sources:
            current_source = personal.source(source["id"])
            source_authorizations[source["id"]] = current_source["authorization_revision"]
            if current_source["withdrawn"]:
                raise ConflictError("所选资料来源已撤回")
            if entry["outbound"] == "excerpts" and current_source["outbound"] != "excerpts":
                raise ConflictError("所选资料引用来源尚未允许摘录外发")
        with self._locked() as state:
            identity = binding_id or uuid4().hex
            self._check_expected(state, "binding", identity, expected_hash)
            return self._commit(state, [("binding", identity, {"source_scope": "personal", "source_owner": "personal",
                "source_library_id": personal.overview()["library_id"], "entry_id": entry["id"], "entry_version_id": entry["version_id"],
                "entry_hash": entry["hash"], "authorization_revision": current_entry["authorization_revision"],
                "purpose": purpose, "snapshot": entry, "sources": sources, "source_authorizations": source_authorizations,
                "withdrawn": False})])[0]

    def binding(self, identity, version_id=None):
        with self._locked() as state:
            return self._read(state, "binding", identity, version_id)

    def _source_from_ref(self, ref, state):
        if ref.get("scope", self.scope) == self.scope and ref.get("owner", self.owner) == self.owner:
            return self._read(state, "source", ref["source_id"], ref["source_version_id"])
        if ref.get("scope") == "personal" and ref.get("owner") == "personal" and self.project is not None:
            return LibraryStore(self.workspace).source(ref["source_id"], ref["source_version_id"])
        raise ConflictError("资料引用所属范围无效")

    def _current_source_record(self, source, state):
        if source["scope"] == self.scope and source["owner"] == self.owner:
            return self._read(state, "source", source["id"])
        if self.project and source["scope"] == "personal" and source["owner"] == "personal":
            return LibraryStore(self.workspace).source(source["id"])
        raise ConflictError("来源许可范围无效")

    @staticmethod
    def _terms(text):
        words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
        return {part for word in words for part in ([word] if not re.match(r"[\u3400-\u9fff]", word) or len(word) < 2 else
                                                   [word[i:i + 2] for i in range(len(word) - 1)])}

    def search(self, query, limit=20, types=None):
        _text(query, 200, empty=False)
        if type(limit) is not int or not 1 <= limit <= 100 or types is not None and (not isinstance(types, list) or any(t not in ENTRY_TYPES for t in types)):
            raise VibeNovelError("资料检索范围无效")
        wanted = self._terms(query)
        results, omitted, scanned, scanned_bytes = [], [], 0, 0
        with self._locked() as state:
            for kind in ("entry", "binding", "source"):
                for identity in state["entities"][kind]:
                    newest = state["entities"][kind][identity][-1]
                    size = self._record_path(kind, identity, newest["version_id"]).stat().st_size
                    if scanned >= 400 or scanned_bytes + size > 8 * 1024 * 1024:
                        omitted.append({"kind": kind, "id": identity, "reason": "资料检索数量/8MiB预算上限"})
                        continue
                    item = self._read(state, kind, identity)
                    extracted_size = self.path("blobs/" + item["text_sha256"]).stat().st_size if kind == "source" else 0
                    if scanned_bytes + size + extracted_size > 8 * 1024 * 1024:
                        omitted.append({"kind": kind, "id": identity, "reason": "来源全文超出本次8MiB检索预算"})
                        continue
                    scanned += 1
                    scanned_bytes += size + extracted_size
                    candidate = item.get("snapshot", item)
                    if item.get("withdrawn") or candidate.get("withdrawn") or candidate.get("status") == "archived":
                        continue
                    if types and candidate.get("type") not in types:
                        continue
                    haystack = (" ".join([candidate.get("title", ""), candidate.get("content", ""), candidate.get("summary", ""), *candidate.get("tags", []),
                                          json.dumps(candidate.get("method", {}), ensure_ascii=False)])
                                if kind != "source" else self._source_text(item)["text"] + " " + json.dumps(item["metadata"], ensure_ascii=False))
                    matches = wanted & self._terms(haystack)
                    exact = query.casefold() in haystack.casefold()
                    if matches or exact:
                        offset = min([haystack.casefold().find(term) for term in matches] or [haystack.casefold().find(query.casefold())])
                        results.append({"kind": kind, "id": identity, "version_id": item["version_id"], "hash": item["hash"],
                                        "title": candidate.get("title", item.get("filename", "")), "type": candidate.get("type", "literature"),
                                        "score": len(matches) + (10 if exact else 0), "quote": haystack[max(0, offset - 100):max(0, offset - 100) + 1200]})
            return {"scope": self.scope, "owner": self.owner, "mode": "chinese_terms", "query": query,
                    "revision": state["revision"], "results": sorted(results, key=lambda r: (-r["score"], r["id"]))[:limit],
                    "scanned": scanned, "scanned_bytes": scanned_bytes, "omitted": omitted,
                    "limitations": ["中文二元词项及精确匹配只用于资料候选召回，不代表事实核实或文学质量判断。"]}

    def adopt_method(self, identity, expected_hash):
        with self._locked() as state:
            self._check_expected(state, "entry", identity, expected_hash)
            current = self._read(state, "entry", identity)
            if current["type"] != "method" or current["withdrawn"] or not current["method"]["steps"]:
                raise VibeNovelError("只能采用未撤回且有明确步骤的方法卡")
            self._refs(current["source_refs"], state)
            self._validate_research_entry(current)
            data = self._data(current)
            data["adoption"] = {"id": uuid4().hex, "actor": "AUTHOR", "at": utc_now(), "source_version_id": current["version_id"]}
            return self._decorate_entry(self._commit(state, [("entry", identity, data)])[0])

    def _dep(self, record, authorization_revision=None):
        value = {key: record[key] for key in ("kind", "scope", "owner", "id", "version_id", "hash")}
        if record["kind"] == "source":
            value["authorization_revision"] = authorization_revision or record["authorization_revision"]
        return value

    def _validate_research_entry(self, entry):
        origin = entry.get("research_origin")
        if origin is not None:
            if entry.get("type") != "method":
                raise ConflictError("研究来源只能附属于方法卡")
            validate_research_origin(self.workspace, origin)
        return origin

    @staticmethod
    def _research_dep(origin):
        return {"kind": "reference_research", "scope": "reference_books", "owner": origin["book_id"],
                "origin": copy.deepcopy(origin)}

    def select_for_task(self, payload):
        allowed = {"purpose", "source_ranges", "source_ids", "entry_ids", "binding_ids", "query", "max_characters", "automatic", "retrieval_selection"}
        if not isinstance(payload, dict) or set(payload) - allowed or payload.get("purpose", "reference") not in PURPOSES:
            raise VibeNovelError("任务资料选择字段无效")
        purpose = payload.get("purpose", "reference")
        ceiling = payload.get("max_characters", MAX_TASK_CHARACTERS)
        if type(ceiling) is not int or not 1 <= ceiling <= MAX_TASK_CHARACTERS:
            raise VibeNovelError("任务资料字符预算无效")
        if payload.get("source_ids"):
            raise VibeNovelError("来源必须显式指定source_ranges，不能默发整部资料")
        entries = _strings(payload.get("entry_ids", []), 30)
        bindings = _strings(payload.get("binding_ids", []), 30)
        # Term recall is deterministic and does not call a provider.
        automatic = payload.get("automatic", False)
        if type(automatic) is not bool:
            raise VibeNovelError("自动选择开关需要布尔值")
        retrieval = None
        if payload.get("retrieval_selection") is not None:
            from .reference_retrieval import ReferenceRetrievalStore
            retrieval = ReferenceRetrievalStore(self.workspace).task_selection(payload["retrieval_selection"])
        if automatic:
            overview = self.overview()
            policy = overview["policy"]
            if policy["mode"] != "auto" or purpose not in policy["purposes"]:
                raise VibeNovelError("本书尚未授权此用途的自动资料选择")
            recalled = self.search(payload.get("query") or purpose, types=policy["types"])["results"]
            chosen = list(policy["required_ids"])
            if len(chosen) > policy["max_items"]:
                raise VibeNovelError("固定必选资料数超过预算，未截掉必选项")
            collection_ids = set()
            for collection in overview["collections"]:
                if collection["id"] in policy["collections"]:
                    collection_ids.update(collection["entry_ids"])
            chosen.extend(r["id"] for r in recalled if r["kind"] in ("entry", "binding") and r["id"] not in chosen
                          and r["id"] not in policy["excluded_ids"]
                          and (not policy["collections"] or r["id"] in collection_ids))
            for identity in chosen[:policy["max_items"]]:
                (entries if any(e["id"] == identity for e in overview["entries"]) else bindings).append(identity)
            ceiling = min(ceiling, policy["max_characters"])
        with self._locked() as state:
            policy = self._policy_record(state)
            deps, manifest, fragments, parts, total = [], [], [], [], 0

            def attach(record, text, reason):
                nonlocal total
                title = (record.get("title") or record.get("snapshot", {}).get("title")
                         or record.get("metadata", {}).get("title") or record.get("filename", "资料"))
                if total + len(text) > ceiling:
                    if automatic and record["id"] not in policy["policy"]["required_ids"]:
                        manifest.append({"kind": record["kind"], "id": record["id"], "title": title, "version_id": record["version_id"],
                                         "hash": record["hash"], "purpose": purpose, "included": False, "reason": "可选资料超过剩余字符预算，未发送"})
                        return False
                    raise VibeNovelError("所选任务资料超出字符预算，请缩小范围；未静默截断必选材料")
                total += len(text)
                deps.append(self._dep(record, self._current_source_record(record, state)["authorization_revision"] if record["kind"] == "source" else None))
                manifest.append({"kind": record["kind"], "id": record["id"], "title": title, "version_id": record["version_id"],
                                 "hash": record["hash"], "purpose": purpose, "reason": reason})
                parts.append("<library_resource kind='%s' id='%s' version='%s' purpose='%s'>\n%s\n</library_resource>" %
                             (record["kind"], record["id"], record["version_id"], purpose, text))
                return True

            for identity in dict.fromkeys(entries):
                entry = self._read(state, "entry", identity)
                if entry["withdrawn"] or entry["status"] == "archived" or entry["outbound"] == "local":
                    if automatic and identity not in policy["policy"]["required_ids"]:
                        continue
                    raise ConflictError("资料已撤回/归档或未允许外发")
                if entry["type"] == "method" and not entry.get("adoption"):
                    if automatic and identity not in policy["policy"]["required_ids"]:
                        continue
                    raise ConflictError("方法卡尚未由作者采用")
                try:
                    research_origin = self._validate_research_entry(entry)
                except VibeNovelError:
                    if not automatic or identity in policy["policy"]["required_ids"]:
                        raise
                    manifest.append({"kind": "entry", "id": identity, "included": False,
                                     "reason": "可选研究方法的固定来源已失效，未发送"})
                    continue
                refs = self._refs(entry["source_refs"], state)
                reference_deps = []
                for ref in refs:
                    src = self._source_from_ref(ref, state)
                    current_source = self._current_source_record(src, state)
                    if entry["outbound"] == "excerpts" and current_source["outbound"] != "excerpts":
                        raise ConflictError("引用来源未允许原句外发")
                    reference_deps.append(self._dep(src, current_source["authorization_revision"]))
                if entry["outbound"] == "summary":
                    text = entry["summary"]
                    if not text.strip():
                        raise VibeNovelError("仅摘要外发的资料尚无作者摘要")
                else:
                    text = entry["content"] + ("\n" + json.dumps(entry["method"], ensure_ascii=False) if entry["type"] == "method" else "")
                if attach(entry, text, "自动词项选材" if automatic else "作者明确选择"):
                    deps.extend(reference_deps)
                    if research_origin is not None:
                        deps.append(self._research_dep(research_origin))
                        manifest[-1]["evidence_note"] = "参考研究原文仅作为封存依赖，未自动发送或计为模型实读引文。"
                    if entry["outbound"] == "excerpts":
                        # A card's citations are dependencies, not automatically
                        # transmitted source text. Only a verbatim quote that is
                        # actually present in this payload can be claimed read.
                        fragments.extend(ref for ref in refs if ref["quote"] in text)
                        omitted = len(refs) - sum(ref["quote"] in text for ref in refs)
                        if omitted:
                            manifest[-1]["evidence_note"] = "%d条来源仅绑定依赖，原句未在卡片实发正文中出现。" % omitted
            for identity in dict.fromkeys(bindings):
                bound = self._read(state, "binding", identity)
                try:
                    self._validate_binding(bound)
                except VibeNovelError:
                    if not automatic or identity in policy["policy"]["required_ids"]:
                        raise
                    manifest.append({"kind": "binding", "id": identity, "included": False,
                                     "reason": "可选固定资料的来源或授权已失效，未发送"})
                    continue
                entry = bound["snapshot"]
                if bound["purpose"] not in (purpose, "reference") or entry["outbound"] == "local" or (entry["type"] == "method" and not entry.get("adoption")):
                    if automatic and identity not in policy["policy"]["required_ids"]:
                        continue
                    raise ConflictError("本书选用未授权此用途/外发或方法未采用")
                if entry["outbound"] == "summary":
                    text = entry["summary"]
                    if not text.strip():
                        raise VibeNovelError("所选资料尚无可外发摘要")
                else:
                    text = entry["content"] + ("\n" + json.dumps(entry["method"], ensure_ascii=False) if entry["type"] == "method" else "")
                if attach(bound, text, "本书固定选用版本"):
                    if entry.get("research_origin") is not None:
                        deps.append(self._research_dep(entry["research_origin"]))
                        manifest[-1]["evidence_note"] = "参考研究原文仅作为封存依赖，未自动发送或计为模型实读引文。"
                    if entry["outbound"] == "excerpts":
                        refs = entry["source_refs"]
                        fragments.extend(ref for ref in refs if ref["quote"] in text)
                        omitted = len(refs) - sum(ref["quote"] in text for ref in refs)
                        if omitted:
                            manifest[-1]["evidence_note"] = "%d条来源仅绑定依赖，原句未在卡片实发正文中出现。" % omitted
            refs = self._refs(payload.get("source_ranges", []), state)
            for ref in refs:
                src = self._source_from_ref(ref, state)
                if self._current_source_record(src, state)["outbound"] != "excerpts":
                    raise ConflictError("来源未允许摘录外发；仅摘要政策不能发送原句")
                fragments.append(ref)
                attach(src, ref["quote"], "作者明确选定来源区间")
            if retrieval is not None:
                if len("\n\n".join(parts + [retrieval["text"]])) > min(ceiling, 24000):
                    raise VibeNovelError("所选检索与卡片资料合计超过24000字符，请减少范围；未静默截断")
                from .reference_retrieval import ReferenceRetrievalStore
                ReferenceRetrievalStore(self.workspace).validate_task_selection(retrieval)
                parts.append(retrieval["text"])
                deps.append({"kind": "reference_retrieval", "scope": "reference_books", "owner": retrieval["index_id"], "snapshot": retrieval})
                manifest.extend({"kind": "reference_retrieval", "id": item["id"], "title": item["title"], "layer": item["layer"],
                                 "hash": item["hash"], "purpose": purpose, "reason": "作者明确选中检索结果并预览外发范围",
                                 "evidence_note": "仅文本块实际发送；检索证据列表未自动计为模型实读。"} for item in retrieval["records"])
            if not parts and not automatic:
                raise VibeNovelError("未选择可用于本次任务的资料")
            if automatic and not parts:
                manifest.append({"kind": "selection", "included": False,
                                 "reason": "本次自动检索未找到范围、外发授权和预算内可用资料，未发送资料正文"})
            value = {"schema_version": 1, "scope": self.scope, "owner": self.owner, "library_id": state["library_id"],
                     "text": "\n\n".join(parts), "manifest": manifest, "source_fragments": fragments,
                     "typed_deps": deps, "policy": {"version_id": policy["version_id"], "hash": policy["hash"]}}
            if len(json_bytes([dep for dep in deps if dep["kind"] == "reference_research"])) > MAX_RESEARCH_TASK_DEPENDENCIES_BYTES:
                raise VibeNovelError("任务参考研究依赖超过256KiB，请减少方法；未截断来源")
            value["hash"] = digest(json_bytes(value))
            return value

    def _validate_binding(self, bound):
        if bound["withdrawn"] or bound.get("source_scope") != "personal":
            raise ConflictError("本书资料选用已撤回或范围无效")
        personal = LibraryStore(self.workspace)
        with personal._locked() as state:
            if state["library_id"] != bound["source_library_id"]:
                raise ConflictError("个人库身份已变化")
            current = personal._read(state, "entry", bound["entry_id"])
            personal._validate_research_entry(current)
            if current["withdrawn"] or current["authorization_revision"] != bound["authorization_revision"]:
                raise ConflictError("个人库资料外发/撤回授权已变化，请重新显式选用")
            frozen = personal._decorate_entry(personal._read(state, "entry", bound["entry_id"], bound["entry_version_id"]))
            personal._validate_research_entry(frozen)
            if frozen["hash"] != bound["entry_hash"] or frozen != bound["snapshot"]:
                raise ConflictError("固定资料选用快照损坏")
            for source in bound["sources"]:
                current_source = personal._read(state, "source", source["id"])
                if current_source["withdrawn"] or current_source["authorization_revision"] != bound["source_authorizations"].get(source["id"]):
                    raise ConflictError("固定资料来源外发/撤回授权已变化")
                personal._source_text(personal._read(state, "source", source["id"], source["version_id"]))

    def validate_task_snapshot(self, snapshot):
        with self._locked() as state:
            return self._validate_snapshot_state(snapshot, state)

    def _validate_snapshot_state(self, snapshot, state):
        if not isinstance(snapshot, dict) or set(snapshot) != {"schema_version", "scope", "owner", "library_id", "text", "manifest", "source_fragments", "typed_deps", "policy", "hash"}:
            raise ConflictError("任务资料快照格式无效")
        unsigned = {k: v for k, v in snapshot.items() if k != "hash"}
        if (snapshot["schema_version"] != 1 or snapshot["scope"] != self.scope or snapshot["owner"] != self.owner
                or digest(json_bytes(unsigned)) != snapshot["hash"] or len(snapshot["text"]) > 100000):
            raise ConflictError("任务资料快照身份或hash不匹配")
        if state["library_id"] != snapshot["library_id"]:
            raise ConflictError("任务资料库身份已变化")
        policy = self._policy_record(state)
        if snapshot["policy"] != {"version_id": policy["version_id"], "hash": policy["hash"]}:
            raise ConflictError("任务资料检索策略版本已变化")
        if not isinstance(snapshot["typed_deps"], list) or len(snapshot["typed_deps"]) > 200:
            raise ConflictError("任务资料依赖列表无效")
        if len(json_bytes([dep for dep in snapshot["typed_deps"] if isinstance(dep, dict) and dep.get("kind") == "reference_research"])) > MAX_RESEARCH_TASK_DEPENDENCIES_BYTES:
            raise ConflictError("任务参考研究依赖超过256KiB")
        for dep in snapshot["typed_deps"]:
            if isinstance(dep, dict) and dep.get("kind") == "reference_retrieval":
                if (set(dep) != {"kind", "scope", "owner", "snapshot"} or dep["scope"] != "reference_books"
                        or not isinstance(dep["snapshot"], dict) or dep["owner"] != dep["snapshot"].get("index_id")
                        or len(json_bytes(dep)) > 256 * 1024):
                    raise ConflictError("任务检索资料依赖范围或体积无效")
                from .reference_retrieval import ReferenceRetrievalStore
                ReferenceRetrievalStore(self.workspace).validate_task_selection(dep["snapshot"])
                continue
            if isinstance(dep, dict) and dep.get("kind") == "reference_research":
                if (set(dep) != {"kind", "scope", "owner", "origin"} or dep["scope"] != "reference_books"
                        or not isinstance(dep["origin"], dict) or dep["owner"] != dep["origin"].get("book_id")):
                    raise ConflictError("任务参考研究依赖所属范围无效")
                validate_research_origin(self.workspace, dep["origin"])
                continue
            if (not isinstance(dep, dict) or set(dep) != ({"kind", "scope", "owner", "id", "version_id", "hash", "authorization_revision"}
                                                         if dep.get("kind") == "source" else {"kind", "scope", "owner", "id", "version_id", "hash"})
                    or dep["kind"] not in ("source", "entry", "binding")):
                raise ConflictError("任务资料依赖所属范围无效")
            if dep["scope"] == self.scope and dep["owner"] == self.owner:
                current = self._read(state, dep["kind"], dep["id"])
            elif dep["scope"] == "personal" and dep["owner"] == "personal" and self.project and dep["kind"] == "source":
                current = LibraryStore(self.workspace).source(dep["id"])
            else:
                raise ConflictError("任务资料依赖不能读取另一作品")
            if dep["kind"] == "source":
                historical = (self._read(state, "source", dep["id"], dep["version_id"]) if dep["scope"] == self.scope else
                              LibraryStore(self.workspace).source(dep["id"], dep["version_id"]))
                if historical["hash"] != dep["hash"] or current["authorization_revision"] != dep["authorization_revision"] or current["withdrawn"]:
                    raise ConflictError("任务来源许可/撤回已变化，旧任务不能复活")
            elif current["version_id"] != dep["version_id"] or current["hash"] != dep["hash"] or current.get("withdrawn"):
                raise ConflictError("任务资料选择历史已变化，旧任务不能复活")
            if dep["kind"] == "binding":
                self._validate_binding(current)
            if dep["kind"] == "entry":
                self._validate_research_entry(current)
            if dep["kind"] == "source" and dep["scope"] == self.scope:
                self._source_text(current)
        return copy.deepcopy(snapshot)

    def _snapshot_store(self, snapshot):
        if snapshot.get("scope") == self.scope and snapshot.get("owner") == self.owner:
            return self
        if self.project is not None and snapshot.get("scope") == "personal" and snapshot.get("owner") == "personal":
            return LibraryStore(self.workspace)
        raise ConflictError("方法来源快照不能读取另一作品")

    def _check_analysis(self, state, expected):
        if expected is None:
            return
        if not isinstance(expected, dict) or set(expected) != {"id", "version_id", "hash"}:
            raise VibeNovelError("方法分析依赖身份无效")
        current = self._read(state, "entry", expected["id"])
        if (current["type"] != "technique" or current["version_id"] != expected["version_id"]
                or current["hash"] != expected["hash"] or current["withdrawn"] or current["status"] == "archived"):
            raise ConflictError("方法分析依赖已变化、归档或撤回，候选包未发布")

    def preview_reference_methods(self, book_id, batch_id, node_id, method_indices):
        if self.project is not None:
            raise VibeNovelError("参考书研究方法须先发布个人库，再显式选用于本书")
        return _preview_reference_methods(self.workspace, book_id, batch_id, node_id, method_indices)

    def publish_reference_methods(self, book_id, batch_id, node_id, method_indices, expected_preview, idempotency_key):
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", idempotency_key):
            raise VibeNovelError("研究方法发布需要16至80位幂等键")
        preview = self.preview_reference_methods(book_id, batch_id, node_id, method_indices)
        if preview["hash"] != expected_preview:
            raise ConflictError("研究方法范围、结果或授权已变化，请重新预览")
        request_hash = digest(json_bytes({"preview_hash": expected_preview, "book_id": book_id, "batch_id": batch_id,
                                          "node_id": node_id, "method_indices": method_indices}))
        return self._publish_research_preview(preview, idempotency_key, request_hash)

    def preview_study_methods(self, study_id, method_indices):
        if self.project is not None:
            raise VibeNovelError("精读研究方法先发布个人库，再由作者选入本书")
        from .reference_studies import ReferenceStudiesStore
        return ReferenceStudiesStore(self.workspace).preview_methods(study_id, method_indices)

    def publish_study_methods(self, study_id, method_indices, expected_preview, idempotency_key):
        preview = self.preview_study_methods(study_id, method_indices)
        if preview["hash"] != expected_preview:
            raise ConflictError("精读方法或来源已变化，请重新预览")
        request_hash = digest(json_bytes({"study_id": study_id, "preview_hash": expected_preview,
                                         "method_indices": method_indices}))
        return self._publish_research_preview(preview, idempotency_key, request_hash)

    def preview_synthesis_methods(self, synthesis_id, method_indices):
        if self.project is not None:
            raise VibeNovelError("归并方法先保存个人候选库，再由作者选入本书")
        from .reference_synthesis import ReferenceSynthesisManager
        return ReferenceSynthesisManager(self.workspace).preview_methods(synthesis_id, method_indices)

    def publish_synthesis_methods(self, synthesis_id, method_indices, expected_preview, idempotency_key):
        preview = self.preview_synthesis_methods(synthesis_id, method_indices)
        if preview["hash"] != expected_preview:
            raise ConflictError("归并方法、精读结果或来源已变化，请重新预览")
        request_hash = digest(json_bytes({"synthesis_id": synthesis_id, "preview_hash": expected_preview,
                                         "method_indices": method_indices}))
        return self._publish_research_preview(preview, idempotency_key, request_hash)

    def _publish_research_preview(self, preview, idempotency_key, request_hash):
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", idempotency_key):
            raise VibeNovelError("研究方法发布需要16至80位固定幂等键")
        with self._locked() as state:
            validate_research_origin(self.workspace, preview["origin"])
            for identity in state["entities"]["bundle"]:
                existing = self._read(state, "bundle", identity)
                if existing.get("reference_idempotency_key") == idempotency_key:
                    if existing["request_hash"] != request_hash or existing.get("research_origin") != preview["origin"]:
                        raise ConflictError("方法发布幂等键已绑定另一研究选择")
                    return self._bundle_result(state, existing)
            changes, ids = [], []
            for method in preview["methods"]:
                data = self._entry_payload({"type": "method", "title": method["title"], "content": "参考书研究提炼的方法候选；文学效果待作者评阅。",
                                           "tags": ["参考书研究"], "source_refs": [],
                                           "method": {"purpose": preview["purpose"], "steps": method["steps"],
                                                      "limitations": method["limitations"]}}, state, "AI_SUGGESTED")
                origin = copy.deepcopy(preview["origin"])
                if origin.get("source_type") not in ("reference_study", "reference_synthesis"):
                    origin["method_indices"] = [method["index"]]
                    origin["evidence"] = copy.deepcopy(method["source_refs"])
                data.update(research_origin=origin, authorization_revision=uuid4().hex)
                identity = uuid4().hex
                changes.append(("entry", identity, data))
                ids.append({"id": identity, "role": "method"})
            bundle_id = uuid4().hex
            changes.append(("bundle", bundle_id, {"job_id": None, "reference_idempotency_key": idempotency_key,
                "request_hash": request_hash, "snapshot_hash": None, "task_snapshot": None, "expected_analysis": None,
                "research_origin": copy.deepcopy(preview["origin"]), "entries": ids}))
            records = self._commit(state, changes)
            return self._bundle_result(self._state(), records[-1])

    def publish_method_bundle(self, bundle, task_snapshot=None, job_id=None, expected_analysis=None):
        if task_snapshot is not None:
            self._snapshot_store(task_snapshot).validate_task_snapshot(task_snapshot)
            if any(dep.get("kind") in ("reference_research", "reference_retrieval") for dep in task_snapshot["typed_deps"]):
                raise VibeNovelError("参考研究方法的再提炼尚无来源继承流程，请从原研究节点发布候选")
        if not isinstance(bundle, dict) or set(bundle) - {"analysis", "methods"} or not isinstance(bundle.get("methods", []), list) or len(bundle.get("methods", [])) > 20:
            raise VibeNovelError("方法候选包格式无效")
        if job_id is not None:
            _identity(job_id)
        with self._locked() as state:
            self._check_analysis(state, expected_analysis)
            if task_snapshot is not None:
                origin = self._snapshot_store(task_snapshot)
                if origin is self:
                    self._validate_snapshot_state(task_snapshot, state)
                else:
                    origin.validate_task_snapshot(task_snapshot)
            if job_id:
                for identity in state["entities"]["bundle"]:
                    old = self._read(state, "bundle", identity)
                    if old.get("job_id") == job_id:
                        if (old["request_hash"] != digest(json_bytes(bundle)) or old.get("snapshot_hash") != (task_snapshot or {}).get("hash")
                                or old.get("expected_analysis") != expected_analysis):
                            raise ConflictError("同一方法任务已绑定另一候选包")
                        return self._bundle_result(state, old)
            items = []
            analysis = bundle.get("analysis")
            if analysis is not None:
                if not isinstance(analysis, dict) or analysis.get("type", "technique") != "technique":
                    raise VibeNovelError("资料分析必须是technique候选")
                items.append(("analysis", {**analysis, "type": "technique"}))
            for method in bundle.get("methods", []):
                if not isinstance(method, dict) or method.get("type", "method") != "method":
                    raise VibeNovelError("方法包包含非方法条目")
                items.append(("method", {**method, "type": "method"}))
            if not items:
                raise VibeNovelError("方法候选包不能为空")
            changes, ids = [], []
            for role, item in items:
                if "id" in item:
                    raise VibeNovelError("模型不得指定既有资料ID")
                if task_snapshot is not None:
                    item = copy.deepcopy(item)
                    for ref in item.get("source_refs", []):
                        origins = {(fragment.get("scope", task_snapshot["scope"]), fragment.get("owner", task_snapshot["owner"]))
                                   for fragment in task_snapshot["source_fragments"]
                                   if fragment["source_id"] == ref.get("source_id") and fragment["source_version_id"] == ref.get("source_version_id")}
                        if len(origins) > 1 and "scope" not in ref:
                            raise VibeNovelError("方法引用跨范围同名来源，需明确scope/owner")
                        origin_scope, origin_owner = next(iter(origins), (task_snapshot["scope"], task_snapshot["owner"]))
                        ref.setdefault("scope", origin_scope)
                        ref.setdefault("owner", origin_owner)
                data = self._entry_payload(item, state, "AI_SUGGESTED")
                data["authorization_revision"] = uuid4().hex
                if task_snapshot is not None:
                    for ref in data["source_refs"]:
                        if not any(ref["scope"] == fragment.get("scope", task_snapshot["scope"]) and ref["owner"] == fragment.get("owner", task_snapshot["owner"])
                                   and ref["source_id"] == fragment["source_id"] and ref["source_version_id"] == fragment["source_version_id"]
                                   and fragment["start_char"] <= ref["start_char"] < ref["end_char"] <= fragment["end_char"]
                                   for fragment in task_snapshot["source_fragments"]):
                            raise VibeNovelError("方法引用超出任务实际提供的来源区间")
                identity = uuid4().hex
                changes.append(("entry", identity, data))
                ids.append({"id": identity, "role": role})
            bundle_id = uuid4().hex
            data = {"job_id": job_id, "request_hash": digest(json_bytes(bundle)),
                    "snapshot_hash": (task_snapshot or {}).get("hash"), "task_snapshot": task_snapshot,
                    "expected_analysis": copy.deepcopy(expected_analysis), "entries": ids}
            changes.append(("bundle", bundle_id, data))
            records = self._commit(state, changes)
            return self._bundle_result(self._state(), records[-1])

    def method_bundle(self, bundle_id=None, *, job_id=None):
        lookup = job_id or bundle_id
        _identity(lookup)
        with self._locked() as state:
            for identity in state["entities"]["bundle"]:
                bundle = self._read(state, "bundle", identity)
                if identity == lookup or bundle.get("job_id") == lookup:
                    return self._bundle_result(state, bundle)
            return None

    def _bundle_result(self, state, bundle):
        entries = []
        for ref in bundle["entries"]:
            # Resolve the version created in the bundle's one manifest revision,
            # so a replay never silently returns an author-edited newer card.
            versions = state["entities"]["entry"][ref["id"]]
            entry = next((self._read(state, "entry", ref["id"], v["version_id"]) for v in versions
                          if self._read(state, "entry", ref["id"], v["version_id"])["revision"] == bundle["revision"]), None)
            if entry is None:
                raise ConflictError("方法候选包条目版本缺失")
            entries.append((ref["role"], self._decorate_entry(entry)))
        return {"bundle_id": bundle["id"], "hash": bundle["hash"], "version_id": bundle["version_id"],
                "request_hash": bundle["request_hash"], "snapshot_hash": bundle.get("snapshot_hash"),
                "expected_analysis": bundle.get("expected_analysis"),
                "analysis": next((e for role, e in entries if role == "analysis"), None),
                "methods": [e for role, e in entries if role == "method"], "entries": [e for _, e in entries],
                **({"research_origin": copy.deepcopy(bundle["research_origin"])} if bundle.get("research_origin") is not None else {})}
