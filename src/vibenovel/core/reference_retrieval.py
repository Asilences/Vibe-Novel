"""Private four-layer retrieval, with real offline embeddings and exact evidence.

SQLite and vectors are disposable caches, never source authority. Retrieval is
local; explicit, bounded selection and a second source check are needed before
anything can join a model task. No provider calls or automatic model downloads.
"""
from __future__ import annotations

import array
import copy
import json
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from vibenovel.errors import VibeNovelError
from .files import atomic_write, digest, file_digest, json_bytes, read_json, safe_path, tree_files
from .locking import ConflictError, project_lock
from .project import utc_now
from .reference_books import ReferenceBookStore
from .reference_methods import check_frozen_research_plan, _references, _valid_ref
from .semantic import MODEL, REVISION, engine_for

LAYERS = ("original", "structure", "expression", "method")
ROOT = "library/.retrieval"
MAX_CHUNKS = 100000
MAX_INDEX_BYTES = 512 * 1024 * 1024
MAX_ALL_INDEX_BYTES = 2 * 1024 * 1024 * 1024
MAX_TASK_CHARACTERS = 24000
CHUNK_SIZE, CHUNK_STRIDE = 240, 200
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_locks = {}
_workers = {}
_locks_guard = threading.Lock()


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise VibeNovelError("检索索引或书籍ID无效")
    return value


def _terms(text):
    # FTS5's default tokenizer does not split Chinese. Preserve Latin words and
    # index Chinese single characters/bigrams explicitly; query remains data.
    value = text.lower()
    result = re.findall(r"[a-z0-9_]+", value)
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        result.extend(run)
        result.extend(run[i:i + 2] for i in range(len(run) - 1))
    return result


def _parts(text):
    for start in range(0, len(text), CHUNK_STRIDE):
        end = min(start + CHUNK_SIZE, len(text))
        if text[start:end].strip():
            yield start, end, text[start:end]
        if end == len(text):
            break


def _record(layer, text, locator, title, refs=None, *, book_id="", related=None):
    payload = {"layer": layer, "text": text, "locator": locator, "title": title,
               "refs": refs or [], "book_id": book_id, "related": related or []}
    payload["hash"] = digest(json_bytes(payload))
    payload["id"] = digest(json_bytes(locator) + layer.encode("ascii"))[:32]
    return payload


def _vector(values):
    if (not isinstance(values, list) or len(values) != 512
            or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1.01 for v in values)
            or not .98 <= sum(v * v for v in values) <= 1.02):
        raise VibeNovelError("本地语义模型未返回有效的512维单位向量")
    return array.array("f", values).tobytes()


def _process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _IndexOwner:
    def __init__(self, root):
        self.root = root

    def path(self, relative):
        return safe_path(self.root, relative)


class ReferenceRetrievalStore:
    def __init__(self, workspace, *, engine=None):
        self.workspace = Path(workspace)
        self.engine = engine if engine is not None else engine_for(self.workspace)
        key = str(self.workspace.resolve())
        with _locks_guard:
            self.lock = _locks.setdefault(key, threading.RLock())
            self.workers = _workers.setdefault(key, {})

    def path(self, relative):
        return safe_path(self.workspace, ROOT + "/" + relative)

    def _manifest(self, identity):
        _identity(identity)
        try:
            value = read_json(self.path(identity + "/manifest.json"), max_bytes=2 * 1024 * 1024)
        except (OSError, VibeNovelError) as exc:
            raise ConflictError("检索索引清单缺失或损坏，请重新建立") from exc
        if value.get("id") != identity or value.get("schema_version") != 1:
            raise ConflictError("检索索引清单身份无效")
        try:
            plan = {key: value[key] for key in ("book_ids", "layers", "sources", "methods", "studies", "model", "revision", "chunk_size", "chunk_stride")}
        except (KeyError, TypeError) as exc:
            raise ConflictError("检索索引冻结计划缺失") from exc
        if digest(json_bytes(plan)) != value.get("plan_hash"):
            raise ConflictError("检索索引冻结计划已被修改")
        return value

    def _save(self, value):
        path = self.path(value["id"] + "/manifest.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_path(path.parent, ".runtime").mkdir(exist_ok=True)
        with project_lock(_IndexOwner(path.parent), timeout=2):
            if path.exists():
                old = self._manifest(value["id"])
                # Cancellation is monotonic across progress writes, including
                # another local process cancelling during an embedding batch.
                value["cancel_requested"] = bool(value.get("cancel_requested") or old.get("cancel_requested"))
                if value["state"] == "ready" and value["cancel_requested"]:
                    value.update(state="cancelled", error="索引取消请求已生效，未发布可检索结果。")
            atomic_write(path, json_bytes(value), overwrite=path.exists())

    def _selections(self, book_ids, layers):
        if (not isinstance(book_ids, list) or not 1 <= len(book_ids) <= 16
                or len(set(book_ids)) != len(book_ids)):
            raise VibeNovelError("请选择1至16本不重复的参考书")
        for value in book_ids:
            _identity(value)
        if not isinstance(layers, list) or not layers or set(layers) - set(LAYERS) or len(set(layers)) != len(layers):
            raise VibeNovelError("请选择有效且不重复的资料层")
        return sorted(book_ids), [layer for layer in LAYERS if layer in layers]

    def _methods(self, book_ids):
        from .library import LibraryStore
        library = LibraryStore(self.workspace)
        # Opening an empty retrieval view must not create a personal library.
        if not library.path(".state/manifest.json").exists():
            return []
        result = []
        # Immutable records are checked against one atomically published
        # manifest. Do not recursively take the personal-library write lock
        # when validating a retrieval dependency from inside that same lock.
        state = library._state()
        for identity in state["entities"]["entry"]:
            entry = library._read(state, "entry", identity)
            if entry["type"] != "method" or not entry.get("adoption") or entry.get("withdrawn") or entry["status"] == "archived":
                continue
            origin = entry.get("research_origin")
            if origin:
                origin_books = ({item.get("book_id") for item in origin.get("books", [])}
                                if origin.get("source_type") == "reference_study" else {origin.get("book_id")})
                if not origin_books or not origin_books <= set(book_ids):
                    continue
            try:
                library._validate_research_entry(entry)
                refs = library._refs(entry["source_refs"], state)
            except VibeNovelError:
                continue
            source_authorizations = []
            for ref in refs:
                original = library._source_from_ref(ref, state)
                current = library._current_source_record(original, state)
                source_authorizations.append({"id": current["id"], "hash": current["hash"],
                    "authorization_revision": current["authorization_revision"], "outbound": current["outbound"]})
            result.append({"id": entry["id"], "version_id": entry["version_id"], "hash": entry["hash"],
                           "authorization_revision": entry["authorization_revision"], "source_authorizations": source_authorizations})
        return sorted(result, key=lambda item: item["id"])

    def _method_entry(self, identity):
        from .library import LibraryStore
        library = LibraryStore(self.workspace)
        entry = library._read(library._state(), "entry", identity)
        library._validate_research_entry(entry)
        return entry

    def _studies(self, book_ids):
        try:
            from .reference_studies import ReferenceStudiesStore
        except ImportError:
            return []
        result = []
        studies = ReferenceStudiesStore(self.workspace)
        for item in studies.list_studies():
            if item.get("completed_nodes", 0) <= 0:
                continue
            try:
                exported = studies.export_verified(item["id"])
            except VibeNovelError:
                continue
            ids = {source.get("book_id") for source in exported.get("sources", [])}
            if ids and ids <= set(book_ids) and exported.get("results"):
                coverage = exported.get("coverage", {})
                result.append({"id": exported["id"], "hash": exported["hash"], "state": exported["state"],
                               "kind": exported["kind"], "question": exported["question"],
                               "book_ids": sorted(ids),
                               "completed_segments": coverage.get("completed_segments", len(exported["results"])),
                               "total_segments": coverage.get("total_segments", len(exported["results"]))})
        if len(result) > 100:
            raise VibeNovelError("有效研究超过单索引100项上限，请先归档研究范围；未静默截断")
        return sorted(result, key=lambda item: item["id"])

    def _plan(self, book_ids, layers):
        book_ids, layers = self._selections(book_ids, layers)
        books = ReferenceBookStore(self.workspace)
        sources, warnings, characters, chapters = [], [], 0, 0
        for identity in book_ids:
            detail = books.detail(identity, limit=1)
            if detail["withdrawn"]:
                raise ConflictError("参考书已撤回，不能加入索引")
            sources.append({"book_id": identity, "edition_id": detail["edition_id"], "hash": detail["hash"],
                            "authorization_revision": detail["authorization_revision"], "title": detail["title"]})
            characters += detail["counts"].get("characters", 0)
            chapters += detail["total_chapters"]
            warnings.extend({"book_id": identity, "code": issue.get("code"), "message": issue.get("message")} for issue in detail.get("issues", []))
        methods = self._methods(book_ids) if "method" in layers else []
        studies = self._studies(book_ids) if "structure" in layers or "expression" in layers else []
        plan = {"book_ids": book_ids, "layers": layers, "sources": sources, "methods": methods, "studies": studies,
                "model": MODEL, "revision": REVISION, "chunk_size": CHUNK_SIZE, "chunk_stride": CHUNK_STRIDE}
        if len(json_bytes(plan)) > 512 * 1024:
            raise VibeNovelError("索引来源及研究计划超过512KiB，请减少所选书籍或整理方法；未截断依赖")
        # This is an upper estimate for source text only. Actual counts and all
        # derived layers are bounded again while building, without silent cuts.
        estimated = math.ceil(characters / CHUNK_STRIDE) + chapters
        return plan, {"source_characters": characters, "source_sections": chapters,
                      "estimated_original_chunks_upper": estimated, "adopted_methods": len(methods),
                      "verified_studies": len(studies), "studies": copy.deepcopy(studies),
                      "warnings": warnings[:200], "warning_count": len(warnings)}

    def preview(self, book_ids, layers):
        plan, coverage = self._plan(book_ids, layers)
        if "original" in layers and coverage["estimated_original_chunks_upper"] > MAX_CHUNKS:
            raise VibeNovelError("所选原文超过单索引10万片段上限，请分书建立")
        value = {**plan, "coverage": coverage, "engine": self.engine.status(), "cost_usd": 0,
                 "max_chunks": MAX_CHUNKS, "max_index_bytes": MAX_INDEX_BYTES,
                 "warnings": ["全部计算仅在本机进行；建索引不会调用付费API或授权外发。",
                              "原文异常章段与无效研究会明确列为遗漏；四层都可选不代表每层已有内容。"]}
        value["hash"] = digest(json_bytes(value))
        return value

    def build(self, book_ids, layers, expected_preview, *, background=True):
        with self.lock:
            preview = self.preview(book_ids, layers)
            if preview["hash"] != expected_preview:
                raise ConflictError("索引范围、来源或模型状态已变化，请重新预览")
            if not self.engine.status().get("available"):
                raise VibeNovelError("本地语义模型不可用；请先安装，不会生成伪语义向量")
            if any(self.get(item["id"]).get("state") == "building" for item in self.list_indexes()):
                raise ConflictError("已有索引正在建立，请完成或取消后再建")
            if self._all_bytes() >= MAX_ALL_INDEX_BYTES:
                raise VibeNovelError("本地检索缓存已达到2GiB配额，请先归档旧派生索引")
            plan = {key: preview[key] for key in ("book_ids", "layers", "sources", "methods", "studies", "model", "revision", "chunk_size", "chunk_stride")}
            value = {"schema_version": 1, "id": uuid4().hex, **plan, "plan_hash": digest(json_bytes(plan)),
                     "state": "building", "created_at": utc_now(), "updated_at": utc_now(),
                     "progress": {"indexed_chunks": 0, "indexed_characters": 0, "layers": {layer: 0 for layer in LAYERS}},
                     "coverage": preview["coverage"], "omitted": [], "cancel_requested": False, "error": None,
                     "worker_pid": os.getpid()}
            self._save(value)
            if background:
                thread = threading.Thread(target=self._build, args=(value["id"],), daemon=True, name="reference-retrieval")
                self.workers[value["id"]] = thread
                thread.start()
            else:
                self._build(value["id"])
            return self.get(value["id"])

    def list_indexes(self):
        root = self.path("indexes-sentinel").parent
        if not root.exists():
            return []
        result = []
        for directory in root.iterdir():
            if _ID.fullmatch(directory.name):
                try:
                    result.append(self._manifest(directory.name))
                except (OSError, VibeNovelError):
                    continue
        return sorted(result, key=lambda value: value["created_at"], reverse=True)[:30]

    def get(self, identity):
        # A different local process may be building the same private cache.
        # Never infer that its worker disappeared merely from this instance.
        value = self._manifest(identity)
        if value["state"] == "building" and not _process_alive(value.get("worker_pid")):
            value.update(state="interrupted", error="本地建索引进程已结束；请重新建立，不会续接未发布向量。")
            self._save(value)
        if value["state"] == "ready":
            try:
                self._check_sources(value)
            except VibeNovelError as exc:
                value = {**value, "state": "stale", "error": str(exc)}
        return value

    def _all_bytes(self):
        root = self.path("indexes-sentinel").parent
        return sum(path.stat().st_size for path in tree_files(root)) if root.exists() else 0

    def cancel(self, identity):
        with self.lock:
            value = self._manifest(identity)
            if value["state"] == "building":
                value["cancel_requested"] = True
                if identity not in self.workers:
                    value["state"] = "cancelled"
                    value["error"] = "已请求取消；重启后不会自动续建或使用未发布向量。"
                self._save(value)
            return value

    def _check_sources(self, manifest):
        books = ReferenceBookStore(self.workspace)
        for source in manifest["sources"]:
            current = books.detail(source["book_id"], limit=1)
            if (current["withdrawn"] or current["hash"] != source["hash"]
                    or current["edition_id"] != source["edition_id"]
                    or current["authorization_revision"] != source["authorization_revision"]):
                raise ConflictError("索引书籍版本、授权或撤回状态已变化，请重建；旧向量没有返回")
        if "method" in manifest["layers"] and self._methods(manifest["book_ids"]) != manifest["methods"]:
            raise ConflictError("已采用方法的版本或采用状态已变化，请重建索引")
        for study in manifest["studies"]:
            from .reference_studies import ReferenceStudiesStore
            exported = ReferenceStudiesStore(self.workspace).export_verified(study["id"])
            if exported["hash"] != study["hash"]:
                raise ConflictError("连续阅读或图谱研究已变化，请重建索引")

    def _raw_documents(self, source, omitted):
        books = ReferenceBookStore(self.workspace)
        with books._locked() as catalog:
            book = books._book(catalog, source["book_id"])
            root, metadata = books._edition(book, source["edition_id"])
            chapters = books._chapters(root, metadata)
        for chapter in chapters:
            if chapter.get("incomplete") or chapter.get("needs_review"):
                omitted.append({"book_id": source["book_id"], "chapter_id": chapter["chapter_id"],
                                "reason": "原文缺口或控制字符待修订，整章未建立向量", "characters": chapter["characters"]})
                continue
            with books._locked():
                text = books._chapter_text(root, chapter)
            for start, end, chunk in _parts(text):
                locator = {"kind": "original", "book_id": source["book_id"], "edition_id": source["edition_id"],
                           "chapter_id": chapter["chapter_id"], "start_char": start, "end_char": end,
                           "chapter_hash": chapter["hash"]}
                refs = [{"book_id": source["book_id"], "edition_id": source["edition_id"], "chapter_id": chapter["chapter_id"],
                         "start_char": start, "end_char": end, "quote": chunk}]
                yield _record("original", chunk, locator, source["title"] + " · " + chapter["title"], refs, book_id=source["book_id"])

    def _batch_documents(self, source, layers, omitted):
        books = ReferenceBookStore(self.workspace)
        directory = books.path(source["book_id"] + "/batches")
        if not directory.exists():
            return
        paths = sorted(directory.iterdir())
        if len(paths) > 100:
            raise VibeNovelError("单书研究批次超过100，请先整理研究归档；未静默遗漏")
        for path in paths:
            if not path.name.endswith(".json") or not _ID.fullmatch(path.stem):
                continue
            batch = read_json(books.path(source["book_id"] + "/batches/" + path.name), max_bytes=8 * 1024 * 1024)
            if batch.get("edition_id") != source["edition_id"] or batch.get("authorization_revision") != source["authorization_revision"]:
                omitted.append({"book_id": source["book_id"], "batch_id": path.stem, "reason": "旧解析版或授权时期研究未入索引"})
                continue
            check_frozen_research_plan(batch)
            indexed = {node["id"]: node for node in batch["nodes"]}
            checked, visiting = {}, set()

            def result(identity):
                if identity in checked:
                    return checked[identity]
                if identity in visiting or identity not in indexed:
                    raise ConflictError("参考研究依赖缺失或形成循环")
                node = indexed[identity]
                if node["state"] != "succeeded":
                    return None
                visiting.add(identity)
                if node["kind"] == "chapter":
                    snapshot = books.snapshot_ranges(source["book_id"], source["edition_id"],
                        [{key: ref[key] for key in ("chapter_id", "start_char", "end_char")} for ref in node["refs"]])
                    if snapshot["hash"] != node["snapshot_hash"]:
                        raise ConflictError("研究原文依赖已变化")
                    allowed = snapshot["refs"]
                else:
                    parents = [result(parent) for parent in node["depends_on"]]
                    if any(parent is None for parent in parents):
                        return None
                    allowed = [ref for parent in parents for ref in _references(parent)]
                relative = "%s/editions/%s/analyses/%s/%s.json" % (source["book_id"], source["edition_id"], batch["id"], identity)
                if node.get("result_path") != relative:
                    raise ConflictError("研究结果不属于当前书籍节点")
                target = books.path(relative)
                if file_digest(target) != node.get("result_hash"):
                    raise ConflictError("研究结果指纹已变化")
                value = read_json(target, max_bytes=128 * 1024)
                for ref in _references(value):
                    _valid_ref(ref, allowed)
                checked[identity] = value
                visiting.remove(identity)
                return value

            for identity, node in indexed.items():
                if node["state"] != "succeeded":
                    continue
                try:
                    value = result(identity)
                except VibeNovelError as exc:
                    omitted.append({"book_id": source["book_id"], "batch_id": batch["id"], "node_id": identity, "reason": str(exc)})
                    continue
                if value is None:
                    continue
                for layer in ("structure", "expression"):
                    if layer not in layers:
                        continue
                    for number, item in enumerate(value.get(layer, [])):
                        text = json.dumps({key: val for key, val in item.items() if key != "source_refs"}, ensure_ascii=False)
                        for start, end, chunk in _parts(text):
                            locator = {"kind": "batch", "book_id": source["book_id"], "batch_id": batch["id"],
                                       "node_id": identity, "result_hash": node["result_hash"], "section": layer,
                                       "item": number, "start_char": start, "end_char": end}
                            refs = [{**ref, "book_id": source["book_id"], "edition_id": source["edition_id"]} for ref in item["source_refs"]]
                            yield _record(layer, chunk, locator, source["title"] + " · " + item.get("title", "研究观察"), refs, book_id=source["book_id"])

    def _study_documents(self, manifest):
        if not manifest["studies"]:
            return
        from .reference_studies import ReferenceStudiesStore
        for study in manifest["studies"]:
            exported = ReferenceStudiesStore(self.workspace).export_verified(study["id"])
            for row in exported.get("results", []):
                for section in ("events", "characters", "relationships", "knowledge", "threads", "rhythm", "per_book", "comparisons"):
                    layer = "expression" if section == "rhythm" else "structure"
                    if layer not in manifest["layers"]:
                        continue
                    for number, item in enumerate(row.get("result", {}).get(section, [])):
                        refs = item.get("source_refs", item.get("setup_refs", []) + item.get("payoff_refs", []))
                        if not refs:
                            continue
                        text = json.dumps({key: val for key, val in item.items() if key not in ("source_refs", "setup_refs", "payoff_refs")}, ensure_ascii=False)
                        related = []
                        group, suffix = {"events": ("causal", "event:" + str(item.get("key", ""))),
                                         "characters": ("characters", "character:" + str(item.get("key", ""))),
                                         "threads": ("information", "thread:" + str(item.get("key", ""))),
                                         "rhythm": ("rhythm", "rhythm:" + str(number))}.get(section, ("", ""))
                        graph_id = row["node_id"] + ":" + suffix
                        if group and any(node["id"] == graph_id for node in exported.get("graphs", {}).get(group, {}).get("nodes", [])):
                            related = [{"node_id": graph_id, "graph": group, "label": "同一研究条目的图节点"}]
                        for start, end, chunk in _parts(text):
                            locator = {"kind": "study_result", "study_id": study["id"], "study_hash": study["hash"],
                                       "node_id": row["node_id"], "section": section, "item": number,
                                       "start_char": start, "end_char": end}
                            yield _record(layer, chunk, locator, "连续研究 · " + str(item.get("title", item.get("name", item.get("label", section)))), refs, related=related)
            for graph_name, graph in exported.get("graphs", {}).items():
                nodes = {item["id"]: item for item in graph.get("nodes", [])}
                for node_id, node in nodes.items():
                    layer = "expression" if graph_name == "rhythm" else "structure"
                    if layer not in manifest["layers"]:
                        continue
                    related = []
                    for edge in graph.get("edges", []):
                        if edge.get("from") == node_id and edge.get("to") in nodes and edge.get("source_refs"):
                            related.append({"node_id": edge["to"], "label": edge.get("label", "候选关系"), "graph": graph_name})
                        if edge.get("to") == node_id and edge.get("from") in nodes and edge.get("source_refs"):
                            related.append({"node_id": edge["from"], "label": edge.get("label", "候选关系"), "graph": graph_name})
                    text = str(node.get("label", "")) + "\n" + str(node.get("description", ""))
                    if not text.strip() or not node.get("source_refs"):
                        continue
                    for start, end, chunk in _parts(text):
                        locator = {"kind": "study", "study_id": study["id"], "study_hash": study["hash"],
                                   "graph": graph_name, "node_id": node_id, "start_char": start, "end_char": end}
                        yield _record(layer, chunk, locator, "研究图谱 · " + node.get("label", graph_name),
                                      node["source_refs"], related=related)

    def _documents(self, manifest, omitted):
        for source in manifest["sources"]:
            if "original" in manifest["layers"]:
                yield from self._raw_documents(source, omitted)
            if "structure" in manifest["layers"] or "expression" in manifest["layers"]:
                yield from self._batch_documents(source, manifest["layers"], omitted)
        if "method" in manifest["layers"]:
            from .library import LibraryStore
            library = LibraryStore(self.workspace)
            for selected in manifest["methods"]:
                entry = self._method_entry(selected["id"])
                text = entry["content"] + "\n" + json.dumps(entry.get("method", {}), ensure_ascii=False)
                for start, end, chunk in _parts(text):
                    locator = {"kind": "method", "entry_id": entry["id"], "version_id": entry["version_id"],
                               "entry_hash": entry["hash"], "start_char": start, "end_char": end}
                    yield _record("method", chunk, locator, entry["title"], entry.get("source_refs", []),
                                  book_id=(entry.get("research_origin") or {}).get("book_id", ""))
        yield from self._study_documents(manifest)

    def _build(self, identity):
        value = self._manifest(identity)
        temporary = self.path(identity + "/building.sqlite3")
        connection = None
        try:
            self._check_sources(value)
            connection = sqlite3.connect(temporary)
            connection.execute("CREATE TABLE docs(id TEXT PRIMARY KEY, layer TEXT, book_id TEXT, record TEXT, vector BLOB)")
            connection.execute("CREATE VIRTUAL TABLE lexical USING fts5(id UNINDEXED, terms)")
            pending = []

            def flush():
                if not pending:
                    return
                if self._manifest(identity).get("cancel_requested"):
                    raise InterruptedError("索引已取消，原书未改变")
                vectors = self.engine.embed([item["text"] for item in pending])
                if len(vectors) != len(pending):
                    raise VibeNovelError("本地语义结果数量不完整")
                for item, vector in zip(pending, vectors):
                    connection.execute("INSERT INTO docs VALUES(?,?,?,?,?)", (item["id"], item["layer"], item["book_id"], json.dumps(item, ensure_ascii=False), _vector(vector)))
                    connection.execute("INSERT INTO lexical(id,terms) VALUES(?,?)", (item["id"], " ".join(_terms(item["text"]))))
                    value["progress"]["indexed_chunks"] += 1
                    value["progress"]["indexed_characters"] += len(item["text"])
                    value["progress"]["layers"][item["layer"]] += 1
                connection.commit()
                value["updated_at"] = utc_now()
                # Preserve a cancel received during embedding before saving.
                value["cancel_requested"] = self._manifest(identity).get("cancel_requested", False)
                self._save(value)
                pending.clear()
                if temporary.stat().st_size > MAX_INDEX_BYTES:
                    raise VibeNovelError("索引超过512MiB上限，未发布；请缩小所选书籍范围")
                if self._all_bytes() > MAX_ALL_INDEX_BYTES:
                    raise VibeNovelError("所有检索缓存超过2GiB总配额，未发布本次索引")

            for item in self._documents(value, value["omitted"]):
                if value["progress"]["indexed_chunks"] + len(pending) >= MAX_CHUNKS:
                    raise VibeNovelError("实际索引超过10万片段上限，未发布不完整索引")
                pending.append(item)
                if len(pending) >= 128:
                    flush()
            flush()
            if self._manifest(identity).get("cancel_requested"):
                raise InterruptedError("索引已取消")
            self._check_sources(value)
            connection.close()
            connection = None
            target = self.path(identity + "/cache.sqlite3")
            os.replace(temporary, target)
            value.update(state="ready", cache_hash=file_digest(target), cache_bytes=target.stat().st_size,
                         completed_at=utc_now(), updated_at=utc_now())
            value["coverage"]["claim"] = "仅覆盖本索引列出的当前文件、成功研究与已采用方法；索引不表示文学理解或完整出版版本。"
        except InterruptedError as exc:
            value.update(state="cancelled", error=str(exc))
        except Exception as exc:
            value.update(state="failed", error=str(exc)[:1000])
        finally:
            if connection is not None:
                connection.close()
            if temporary.exists():
                temporary.unlink()
            self._save(value)
            self.workers.pop(identity, None)

    @contextmanager
    def _connect(self, manifest):
        path = self.path(manifest["id"] + "/cache.sqlite3")
        if (not path.is_file() or path.stat().st_size > MAX_INDEX_BYTES or file_digest(path) != manifest.get("cache_hash")):
            raise ConflictError("检索派生缓存损坏，请重建；未从旧向量返回内容")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _resolve(self, item, manifest, *, outbound=False, derived_cache=None, policy_cache=None):
        """Reconstruct result text from authority, never from the SQLite quote."""
        locator = item["locator"]
        kind = locator["kind"]
        if kind == "original":
            book = ReferenceBookStore(self.workspace)
            current = book.detail(locator["book_id"], limit=1)
            chapter = book.chapter(locator["book_id"], locator["chapter_id"], locator["edition_id"], offset=locator["start_char"], limit=locator["end_char"] - locator["start_char"])
            if outbound:
                snapshot = book.snapshot_ranges(locator["book_id"], locator["edition_id"], [{key: locator[key] for key in ("chapter_id", "start_char", "end_char")}], max_characters=MAX_TASK_CHARACTERS)
                text = snapshot["text"]
            else:
                if current["withdrawn"] or current["edition_id"] != locator["edition_id"]:
                    raise ConflictError("原文检索结果来源已失效")
                text = chapter["text"]
            expected_locator = {"kind": "original", "book_id": locator["book_id"], "edition_id": current["edition_id"],
                                "chapter_id": chapter["chapter_id"], "start_char": chapter["start_char"],
                                "end_char": chapter["end_char"], "chapter_hash": chapter["hash"]}
            refs = [{"book_id": locator["book_id"], "edition_id": current["edition_id"], "chapter_id": chapter["chapter_id"],
                     "start_char": chapter["start_char"], "end_char": chapter["end_char"], "quote": text}]
            expected = _record("original", text, expected_locator, current["title"] + " · " + chapter["title"], refs, book_id=locator["book_id"])
            if expected != item:
                raise ConflictError("检索原文范围、出处或缓存不一致")
        else:
            # Derived observations are few; rebuild and compare exact records
            # using the same dependency validator used when indexing.
            if outbound and kind in ("batch", "study", "study_result"):
                study_books = set()
                if kind in ("study", "study_result"):
                    frozen = next((study for study in manifest["studies"] if study["id"] == locator["study_id"]), None)
                    if frozen is None:
                        raise ConflictError("研究不在索引冻结范围内")
                    study_books = set(frozen.get("book_ids", []))
                    if not study_books:
                        from .reference_studies import ReferenceStudiesStore
                        exported = ReferenceStudiesStore(self.workspace).export_verified(locator["study_id"])
                        study_books = {source["book_id"] for source in exported["sources"]}
                for source in manifest["sources"]:
                    if source["book_id"] in study_books or source["book_id"] == locator.get("book_id"):
                        if policy_cache is None:
                            policy_cache = {}
                        if source["book_id"] not in policy_cache:
                            policy_cache[source["book_id"]] = ReferenceBookStore(self.workspace).detail(source["book_id"], limit=1)
                        detail = policy_cache[source["book_id"]]
                        if detail["outbound"] != "excerpts":
                            raise ConflictError("研究来源未授权外发，不能发送分析及证据")
            if kind == "method" and outbound:
                from .library import LibraryStore
                library = LibraryStore(self.workspace)
                entry = self._method_entry(locator["entry_id"])
                if entry["outbound"] == "local" or not entry.get("adoption") or entry.get("withdrawn"):
                    raise ConflictError("方法未采用、已撤回或未授权外发")
                if entry["outbound"] == "summary":
                    raise ConflictError("方法仅允许摘要外发；请在方法库显式选择摘要，不发送正文检索片段")
                library._validate_research_entry(entry)
                state = library._state()
                for ref in library._refs(entry["source_refs"], state):
                    source = library._source_from_ref(ref, state)
                    if library._current_source_record(source, state)["outbound"] != "excerpts":
                        raise ConflictError("方法引用来源未允许摘录外发，请通过方法库选择摘要")
            if derived_cache is None:
                derived_cache = {}
            if item["layer"] not in derived_cache:
                iterator = self._documents({**manifest, "layers": [item["layer"]]}, [])
                derived_cache[item["layer"]] = {record["id"]: record for record in iterator}
            fresh = derived_cache[item["layer"]].get(item["id"])
            if fresh != item:
                raise ConflictError("研究观察、图谱或方法结果来源已变化")
        return copy.deepcopy(item)

    def search(self, index_id, query, *, layers=None, limit=12, semantic=True, relationships=True):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or any(ord(c) < 32 for c in query):
            raise VibeNovelError("检索问题需要1至200字符且不含控制字符")
        if type(limit) is not int or not 1 <= limit <= 30 or type(semantic) is not bool or type(relationships) is not bool:
            raise VibeNovelError("检索数量或模式无效")
        manifest = self._manifest(index_id)
        if manifest["state"] != "ready":
            raise ConflictError("索引尚未成功建立")
        self._check_sources(manifest)
        layers = manifest["layers"] if layers is None else layers
        if not isinstance(layers, list) or not layers or set(layers) - set(manifest["layers"]):
            raise VibeNovelError("检索层未包含在此索引中")
        with self._connect(manifest) as connection:
            rows = connection.execute("SELECT id,layer,record,vector FROM docs").fetchall()
            records = {row[0]: json.loads(row[2]) for row in rows if row[1] in layers}
            terms = list(dict.fromkeys(_terms(query)))[:100]
            lexical = []
            if terms:
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                lexical = [(identity, float(score)) for identity, score in connection.execute(
                    "SELECT id,bm25(lexical) AS score FROM lexical WHERE lexical MATCH ? ORDER BY score LIMIT 200", (expression,)).fetchall() if identity in records]
            semantic_scores = []
            if semantic:
                vector = self.engine.embed([query.strip()], query=True)[0]
                _vector(vector)
                for identity, layer, _, raw in rows:
                    if identity not in records:
                        continue
                    values = array.array("f")
                    values.frombytes(raw)
                    if len(values) != 512:
                        raise ConflictError("索引向量维度损坏")
                    score = sum(a * b for a, b in zip(vector, values))
                    if not math.isfinite(score):
                        raise ConflictError("索引向量包含无效值")
                    semantic_scores.append((identity, score))
                semantic_scores.sort(key=lambda value: (-value[1], value[0]))
                semantic_scores = semantic_scores[:200]
        scores, channels = {}, {}
        for channel, ranking in (("lexical", lexical), ("semantic", semantic_scores)):
            for rank, (identity, score) in enumerate(ranking, 1):
                scores[identity] = scores.get(identity, 0) + 1 / (60 + rank)
                channels.setdefault(identity, {})[channel] = {"rank": rank, "score": round(score, 6)}
        relation_candidates = {}
        if relationships:
            for identity in sorted(scores, key=lambda key: -scores[key])[:12]:
                item = records[identity]
                if item["locator"]["kind"] not in ("study", "study_result"):
                    continue
                for edge in item.get("related", []):
                    for candidate_id, candidate in records.items():
                        locator = candidate["locator"]
                        if locator.get("study_id") == item["locator"]["study_id"] and locator.get("node_id") == edge["node_id"] and locator.get("graph") == edge["graph"]:
                            relation_candidates.setdefault(candidate_id, []).append({"from_result": identity, "label": edge["label"]})
            for rank, identity in enumerate(sorted(relation_candidates), 1):
                scores[identity] = scores.get(identity, 0) + .5 / (60 + rank)
                channels.setdefault(identity, {})["relationship"] = {"rank": rank, "evidence_edges": relation_candidates[identity]}
        selected = sorted(scores, key=lambda identity: (-scores[identity], identity))[:limit]
        derived_cache = {}
        results = [self._resolve(records[identity], manifest, derived_cache=derived_cache) | {"score": round(scores[identity], 8), "channels": channels[identity]} for identity in selected]
        self._check_sources(manifest)
        return {"index_id": index_id, "query": query.strip(), "mode": "hybrid_rrf" if semantic else "lexical_rrf",
                "fusion": {"algorithm": "RRF", "k": 60, "lexical_weight": 1, "semantic_weight": 1, "relationship_weight": .5},
                "model": MODEL if semantic else None, "results": results, "coverage": manifest["coverage"],
                "indexed_layers": manifest["progress"]["layers"], "omitted": manifest["omitted"],
                "relationship_candidates": len(relation_candidates), "outbound": False,
                "limitations": ["召回分数只表示候选相关性，不表示情节事实或方法有效。",
                                "关系扩展仅使用已验证研究中有证据的边，不根据相似词虚构关系。",
                                "当前检索仅在本地；所选内容仍须按精确范围预览并通过外发授权检查。"]}

    def selection(self, index_id, ids, *, outbound=True):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 30 or len(ids) != len(set(ids)):
            raise VibeNovelError("请选择1至30个不重复检索片段")
        for identity in ids:
            _identity(identity)
        manifest = self._manifest(index_id)
        if manifest["state"] != "ready":
            raise ConflictError("检索索引未就绪")
        self._check_sources(manifest)
        with self._connect(manifest) as connection:
            rows = connection.execute("SELECT id,record FROM docs WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
        records = {identity: json.loads(raw) for identity, raw in rows}
        if set(records) != set(ids):
            raise ConflictError("所选检索结果不在当前索引中")
        derived_cache, policy_cache = {}, {}
        selected = [self._resolve(records[identity], manifest, outbound=outbound, derived_cache=derived_cache, policy_cache=policy_cache) for identity in ids]
        text = "\n\n".join("<reference_retrieval layer='%s' id='%s'>\n%s\n</reference_retrieval>" % (item["layer"], item["id"], item["text"]) for item in selected)
        if len(text) > MAX_TASK_CHARACTERS:
            raise VibeNovelError("所选检索资料超过24000字符，未静默截断，请减少片段")
        self._check_sources(manifest)
        payload = {"schema_version": 1, "index_id": index_id, "plan_hash": manifest["plan_hash"], "ids": ids,
                   "text": text, "characters": len(text), "records": selected, "outbound": outbound}
        if len(json_bytes(payload)) > 256 * 1024:
            raise VibeNovelError("所选检索证据依赖超过256KiB，请减少片段；未截断出处")
        payload["hash"] = digest(json_bytes(payload))
        return payload

    def task_selection(self, request):
        if (not isinstance(request, dict) or set(request) != {"index_id", "ids", "expected_preview"}
                or not isinstance(request["expected_preview"], str) or not _HASH.fullmatch(request["expected_preview"])):
            raise VibeNovelError("任务检索资料需要精确索引、选中片段及预览指纹")
        value = self.selection(request["index_id"], request["ids"], outbound=True)
        if value["hash"] != request["expected_preview"]:
            raise ConflictError("检索资料预览已变化，请重新核对")
        return value

    def validate_task_selection(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ConflictError("检索资料依赖无效")
        expected = self.selection(snapshot.get("index_id"), snapshot.get("ids"), outbound=True)
        if expected != snapshot:
            raise ConflictError("任务检索资料来源、授权或选中范围已变化")
        return expected

    def close(self):
        for identity in list(self.workers):
            self.cancel(identity)
