"""Versioned text authority and recoverable, single-file Studio transactions.

This is deliberately not the M2 multi-file chapter-finalization protocol.
The application detects observed drift; same-user filesystem attacks, or an
external A->B->A change entirely between observations, are outside its guarantee.
"""
from __future__ import annotations

import copy
import json
import re
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Optional
from uuid import uuid4

from vibenovel.errors import MissingDocumentError, VibeNovelError
from .files import atomic_write, digest, file_digest, json_bytes, read_json, tree_files
from .imports import verify_bundle
from .locking import ConflictError, project_lock
from .project import CANONICAL_DIRS, Project, utc_now

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_PREVIEW_BYTES = 16 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024 * 1024
STATE_PATH = "approvals/.studio/state.json"
JOURNAL_PATH = "approvals/.studio/journal.json"
EDITABLE = ("drafts/", "outlines/source/", "outlines/proposals/", "reviews/")
CANONICAL = ("bible/", "chapters/", "memory/", "outlines/approved/")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def _kind(project: Project, relative: str) -> str:
    project.path(relative)  # Shared strict path/link checks, before classification.
    if relative != PurePosixPath(relative).as_posix():
        raise VibeNovelError("文档路径需要规范的 / 相对路径")
    suffix = PurePosixPath(relative).suffix.lower()
    if suffix not in (".md", ".json", ".txt"):
        raise VibeNovelError("Studio 仅支持 Markdown、JSON 和导入的 TXT 文档")
    if relative.startswith("imports/original/"):
        parts = relative.split("/")
        if len(parts) != 4 or not HASH.fullmatch(parts[2]) or parts[3] not in (
                "original.md", "original.txt", "extracted.md", "metadata.json"):
            raise VibeNovelError("只允许读取完整的已校验导入 bundle")
        return "source"
    if suffix == ".txt":
        raise VibeNovelError("TXT 仅可作为只读导入原件")
    if relative.startswith(EDITABLE):
        return "editable"
    if relative.startswith(CANONICAL):
        return "canonical"
    raise VibeNovelError("文档不在 Studio 的内容白名单中")


def _content_bytes(relative: str, content: str) -> bytes:
    if not isinstance(content, str):
        raise VibeNovelError("文档 content 必须为文本")
    try:
        raw = content.encode("utf-8")
    except UnicodeError as exc:
        raise VibeNovelError("文档包含无效 Unicode") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise VibeNovelError("文档超过 2 MiB 上限，请拆分文档")
    if relative.endswith(".json"):
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("重复 JSON 字段")
                    result[key] = value
                return result

            value = json.loads(content, object_pairs_hook=unique,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            # Also reject escaped invalid Unicode, non-finite numbers and deep trees.
            json_bytes(value)
        except (ValueError, RecursionError) as exc:
            raise VibeNovelError("JSON 文档格式错误、重复字段或包含非有限数字") from exc
    if relative.startswith("chapters/") and not content.strip():
        raise VibeNovelError("正式章节不能是空文本")
    return raw


def _read_raw(project: Project, relative: str) -> Optional[bytes]:
    path = project.path(relative)
    if not path.exists():
        return None
    if not path.is_file():
        raise VibeNovelError(f"文档不是普通文件: {relative}")
    maximum = MAX_IMPORT_PREVIEW_BYTES if relative.startswith("imports/original/") else MAX_DOCUMENT_BYTES
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise VibeNovelError(f"文档超过 {maximum // (1024 * 1024)} MiB 预览上限，请拆分文档: {relative}")
    return raw


def _decode(raw: bytes) -> str:
    try:
        # Preserve a UTF-8 BOM as a character, so snapshots retain exact bytes.
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise VibeNovelError("文档需要有效 UTF-8 编码") from exc


def _chapter_summary(path, relative: str) -> dict:
    """Describe saved chapter text without exposing its body in the file list.

    Keep malformed/external oversized files visible with unknown counts. The
    listing must not turn a byte length into a word count or grow an unbounded
    editor buffer just to populate the chapter manager.
    """
    if not relative.startswith(("chapters/", "drafts/")) or path.suffix.lower() != ".md":
        return {}
    with path.open("rb") as stream:
        raw = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        return {"title": None, "character_count": None, "summary_status": "too_large"}
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        return {"title": None, "character_count": None, "summary_status": "invalid_utf8"}
    title = None
    for line in content.lstrip("\ufeff").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.fullmatch(r"#{1,6}\s+(.+?)(?:\s+#+)?", stripped)
        if heading:
            title = heading.group(1).strip()
        elif re.match(r"第[零〇一二三四五六七八九十百千万两\d]+[章节回卷部](?:\s|[：:、.．]|$)", stripped):
            title = stripped
        break
    return {"title": title, "character_count": sum(not char.isspace() for char in content),
            "summary_status": "available"}


def _check_expected(raw: Optional[bytes], expected_hash: Optional[str]) -> None:
    if expected_hash is not None and (not isinstance(expected_hash, str) or not HASH.fullmatch(expected_hash)):
        raise VibeNovelError("expected_hash 需要 SHA256，创建新文件时为 null")
    actual = digest(raw) if raw is not None else None
    if actual != expected_hash:
        raise ConflictError("文档已变化或已存在，请重新读取后再提交")


def _snapshot(state: dict, relative: str, raw: bytes, provenance: str) -> None:
    history = state["history"].setdefault(relative, [])
    history.append({"id": uuid4().hex, "version": len(history) + 1, "path": relative,
                    "hash": digest(raw), "content": _decode(raw), "created_at": utc_now(),
                    "revision": state["revision"], "provenance": provenance})


def _provenance(value: str) -> str:
    if value not in ("AUTHOR", "AI_SUGGESTED", "EXTERNAL", "RESTORE"):
        raise VibeNovelError("未知内容来源标记")
    return value


class _Storage:
    def __init__(self, project: Project):
        self.project = project

    def _validate_state(self, state: dict) -> None:
        if (state.get("schema_version") != 1 or state.get("novel_id") != self.project.novel_id
                or type(state.get("revision")) is not int or state["revision"] < 0
                or not isinstance(state.get("fingerprint"), str)
                or not HASH.fullmatch(state["fingerprint"])
                or not isinstance(state.get("history"), dict)
                or not isinstance(state.get("proposals"), dict)
                or not isinstance(state.get("events"), list)):
            raise ConflictError("Studio 版本/作品/状态无效；已阻止写入，请检查持久化记录")

    def _write_state(self, state: dict) -> None:
        self._validate_state(state)
        raw = json_bytes(state)
        if len(raw) > MAX_STATE_BYTES:
            raise VibeNovelError("Studio 版本记录超过 64 MiB；请先归档作品")
        atomic_write(self.project.path(STATE_PATH), raw, overwrite=True)

    def _load(self) -> dict:
        path = self.project.path(STATE_PATH)
        if path.exists():
            state = read_json(path, max_bytes=MAX_STATE_BYTES)
            self._validate_state(state)
            return state
        if self.project.path(JOURNAL_PATH).exists():
            raise ConflictError("事务 journal 存在但版本状态丢失；已阻止写入，需人工恢复")
        state = {"schema_version": 1, "novel_id": self.project.novel_id, "revision": 0,
                 "fingerprint": self.project.fingerprint(), "history": {}, "proposals": {}, "events": []}
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_state(state)
        return state

    def _fingerprint_after(self, target: str, raw: bytes) -> str:
        paths = [self.project.path("project.json")]
        for directory in CANONICAL_DIRS:
            paths.extend(tree_files(self.project.path(directory)))
        hashes = {path.relative_to(self.project.root).as_posix(): file_digest(path) for path in paths}
        if target.startswith(CANONICAL):
            hashes[target] = digest(raw)
        return digest(json_bytes([{"path": path, "sha256": hashes[path]} for path in sorted(hashes)]))

    def _recover(self, state: dict) -> dict:
        journal_path = self.project.path(JOURNAL_PATH)
        if not journal_path.exists():
            return state
        try:
            # JSON escaping and the extra indentation of nested after_state can
            # exceed the UTF-8 text size; bound the full serialized envelope.
            journal = read_json(journal_path, max_bytes=2 * MAX_STATE_BYTES + 6 * MAX_DOCUMENT_BYTES)
            if journal.get("schema_version") != 1 or journal.get("novel_id") != self.project.novel_id:
                raise ValueError("journal identity")
            target = journal["target"]
            kind = _kind(self.project, target)
            if kind not in ("editable", "canonical"):
                raise ValueError("journal target")
            after = journal["after_state"]
            self._validate_state(after)
            after_raw = _content_bytes(target, journal["after_content"])
            if digest(after_raw) != journal["after_hash"] or digest(json_bytes(after)) != journal["after_state_hash"]:
                raise ValueError("journal checksum")
            state_hash = digest(json_bytes(state))
            current = _read_raw(self.project, target)
            current_hash = digest(current) if current is not None else None
            fingerprint = self.project.fingerprint()
            if (state_hash == journal["before_state_hash"] and current_hash == journal["before_hash"]
                    and fingerprint == journal["before_fingerprint"]):
                # The durable intent is already authorized; finish it exactly once.
                path = self.project.path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, after_raw, overwrite=current is not None)
                if self.project.fingerprint() != after["fingerprint"]:
                    raise ValueError("canonical changed during recovery")
                self._write_state(after)
            elif (state_hash in (journal["before_state_hash"], journal["after_state_hash"])
                  and current_hash == journal["after_hash"] and fingerprint == after["fingerprint"]):
                if state_hash != journal["after_state_hash"]:
                    self._write_state(after)
            else:
                raise ValueError("storage drift")
            journal_path.unlink()
            return after
        except (KeyError, TypeError, ValueError, OSError, VibeNovelError) as exc:
            raise ConflictError("存在未完成且无法确定恢复的事务；已阻止写入，请保留 journal 并人工检查") from exc

    @contextmanager
    def locked(self):
        with project_lock(self.project, timeout=2):
            state = self._recover(self._load())
            current = self.project.fingerprint()
            if state["fingerprint"] != current:
                state["revision"] += 1
                state["events"].append({"revision": state["revision"], "kind": "external_change",
                                        "before": state["fingerprint"], "after": current, "at": utc_now()})
                state["fingerprint"] = current
                self._write_state(state)
            yield state

    def transact(self, state: dict, after: dict, target: str, raw: bytes, before: Optional[bytes]) -> None:
        # Validate every encoded record before publishing the durable intent.
        encoded_after = json_bytes(after)
        if len(encoded_after) > MAX_STATE_BYTES:
            raise VibeNovelError("Studio 版本记录超过 64 MiB；请先归档作品")
        if self.project.fingerprint() != state["fingerprint"]:
            raise ConflictError("正式内容在提交前发生变化，请重新读取")
        _check_expected(_read_raw(self.project, target), digest(before) if before is not None else None)
        journal = {"schema_version": 1, "novel_id": self.project.novel_id, "transaction_id": uuid4().hex,
                   "target": target, "before_hash": digest(before) if before is not None else None,
                   "after_hash": digest(raw), "after_content": _decode(raw),
                   "before_fingerprint": state["fingerprint"],
                   "before_state_hash": digest(json_bytes(state)), "after_state": after,
                   "after_state_hash": digest(encoded_after)}
        atomic_write(self.project.path(JOURNAL_PATH), json_bytes(journal))
        path = self.project.path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, raw, overwrite=before is not None)
        if self.project.fingerprint() != after["fingerprint"]:
            raise ConflictError("提交时检测到外部更改；已保留 journal 等待检查")
        self._write_state(after)
        self.project.path(JOURNAL_PATH).unlink()


@contextmanager
def canonical_read(project: Project):
    """Hold the Studio lock after recovery and drift synchronization.

    Use this directly around legacy canonical readers such as export_markdown;
    do not nest it inside project_lock or call another public Studio store
    method inside the context. The yielded revision/fingerprint is read-only
    metadata for this exact canonical snapshot.
    """
    with _Storage(project).locked() as state:
        yield {"novel_id": project.novel_id, "revision": state["revision"],
               "fingerprint": state["fingerprint"]}


class DocumentStore:
    def __init__(self, project: Project):
        self.project = project
        self.storage = _Storage(project)

    def _document(self, relative: str, state: dict) -> dict:
        kind = _kind(self.project, relative)
        metadata = None
        if kind == "source":
            metadata = verify_bundle(self.project, relative.split("/")[2])
        raw = _read_raw(self.project, relative)
        if raw is None:
            raise MissingDocumentError(f"文档不存在: {relative}")
        if metadata and relative.split("/")[-1].startswith("original."):
            try:
                content = raw.decode(metadata["encoding"])
            except (UnicodeError, LookupError) as exc:
                raise VibeNovelError("导入原件解码失败") from exc
        else:
            content = _decode(raw)
        return {"path": relative, "content": content, "hash": digest(raw), "bytes": len(raw),
                "kind": kind, "readonly": kind != "editable", "revision": state["revision"],
                "novel_id": self.project.novel_id}

    def read(self, path: str) -> dict:
        with self.storage.locked() as state:
            return self._document(path, state)

    def list_documents(self) -> list[dict]:
        with self.storage.locked() as state:
            result = []
            verified_imports = set()
            for directory in ("drafts", "outlines", "reviews", "bible", "chapters", "memory", "imports/original"):
                for path in tree_files(self.project.path(directory)):
                    relative = path.relative_to(self.project.root).as_posix()
                    if path.name == ".gitkeep":
                        continue
                    kind = _kind(self.project, relative)
                    if kind == "source":
                        import_id = relative.split("/")[2]
                        if import_id not in verified_imports:
                            verify_bundle(self.project, import_id)
                            verified_imports.add(import_id)
                    # Listing is bounded independently of preview text limits;
                    # a valid M0 import may be much larger than an editor buffer.
                    result.append({"path": relative, "hash": file_digest(path), "bytes": path.stat().st_size,
                                   "kind": kind, "readonly": kind != "editable", "revision": state["revision"],
                                   "novel_id": self.project.novel_id, **_chapter_summary(path, relative)})
            return sorted(result, key=lambda item: item["path"])

    def save(self, path: str, content: str, expected_hash: Optional[str], provenance: str = "AUTHOR", *,
             expected_fingerprint: Optional[str] = None, expected_sources: Optional[dict[str, str]] = None,
             expected_revision: Optional[int] = None, expected_runtime_sources: Optional[dict] = None,
             expected_source_versions: Optional[dict] = None) -> dict:
        if _kind(self.project, path) != "editable":
            raise VibeNovelError("只允许直接编辑草稿、候选大纲和审稿；正式内容必须经提案批准")
        raw = _content_bytes(path, content)
        provenance = _provenance(provenance)
        with self.storage.locked() as state:
            if expected_source_versions is not None:
                if not isinstance(expected_source_versions, dict) or len(expected_source_versions) > 100:
                    raise VibeNovelError("来源历史版本快照无效")
                for source, version in expected_source_versions.items():
                    _kind(self.project, source)
                    history = state["history"].get(source, [])
                    if (history[-1]["id"] if history else None) != version:
                        raise ConflictError("来源文档历史版本已变化，旧候选未发布")
            if expected_runtime_sources is not None:
                if (not isinstance(expected_runtime_sources, dict)
                        or not expected_runtime_sources
                        or not set(expected_runtime_sources) <= {".runtime/style-baseline.json",
                                                               ".runtime/writing-toolkit.json"}):
                    raise VibeNovelError("仅允许固定文风样章或写作工具配置作为运行依赖")
                for runtime_path, expected in expected_runtime_sources.items():
                    _check_expected(_read_raw(self.project, runtime_path), expected)
            if expected_revision is not None:
                if type(expected_revision) is not int or expected_revision < 0:
                    raise VibeNovelError("expected_revision 需要非负整数")
                if expected_revision != state["revision"]:
                    raise ConflictError("生成期间正式版本已变化，候选未发布")
            if expected_fingerprint is not None:
                if not isinstance(expected_fingerprint, str) or not HASH.fullmatch(expected_fingerprint):
                    raise VibeNovelError("expected_fingerprint 需要完整 SHA256")
                if expected_fingerprint != state["fingerprint"]:
                    raise ConflictError("生成期间正式内容已变化，候选未发布")
            if expected_sources is not None:
                if not isinstance(expected_sources, dict) or len(expected_sources) > 100:
                    raise VibeNovelError("expected_sources 需要最多 100 项的文档路径/hash 对象")
                for source, expected in expected_sources.items():
                    if expected is None:
                        raise VibeNovelError("来源快照需要已存在文档的完整 SHA256")
                    if _kind(self.project, source) == "source":
                        verify_bundle(self.project, source.split("/")[2])
                    _check_expected(_read_raw(self.project, source), expected)
            before = _read_raw(self.project, path)
            _check_expected(before, expected_hash)
            after = copy.deepcopy(state)
            if before is not None and (not after["history"].get(path) or after["history"][path][-1]["hash"] != digest(before)):
                _snapshot(after, path, before, "EXTERNAL")
            _snapshot(after, path, raw, provenance)
            self.storage.transact(state, after, path, raw, before)
            return self._document(path, after)

    def history(self, path: str) -> list[dict]:
        _kind(self.project, path)
        with self.storage.locked() as state:
            return [{key: value for key, value in item.items() if key != "content"}
                    for item in reversed(state["history"].get(path, []))]

    def _version(self, state: dict, path: str, version_id: str) -> dict:
        if not isinstance(version_id, str) or not re.fullmatch(r"[0-9a-f]{32}", version_id):
            raise VibeNovelError("版本 ID 无效")
        version = next((item for item in state["history"].get(path, []) if item["id"] == version_id), None)
        if version is None:
            raise MissingDocumentError("未找到该文档的版本")
        if version.get("path") != path:
            raise ConflictError("历史版本所属路径不匹配，已拒绝读取")
        try:
            raw = version["content"].encode("utf-8")
        except (AttributeError, UnicodeError) as exc:
            raise ConflictError("历史快照内容不是有效 UTF-8 文本") from exc
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ConflictError("历史快照超过文档大小上限")
        if digest(raw) != version["hash"]:
            raise ConflictError("历史快照 hash 不匹配，已拒绝读取")
        result = copy.deepcopy(version)
        result.update(kind=_kind(self.project, path), readonly=True, bytes=len(raw), novel_id=self.project.novel_id)
        return result

    def version(self, path: str, version_id: str) -> dict:
        """Return one checksum-verified snapshot scoped to this exact path."""
        _kind(self.project, path)
        with self.storage.locked() as state:
            return self._version(state, path, version_id)

    def restore(self, path: str, version_id: str, expected_hash: Optional[str]) -> dict:
        if _kind(self.project, path) != "editable":
            raise VibeNovelError("正式内容的历史恢复必须创建新的提案并经批准")
        with self.storage.locked() as state:
            version = self._version(state, path, version_id)
            before = _read_raw(self.project, path)
            _check_expected(before, expected_hash)
            raw = _content_bytes(path, version["content"])
            if digest(raw) != version["hash"]:
                raise ConflictError("历史快照 hash 不匹配，已拒绝恢复")
            after = copy.deepcopy(state)
            if before is not None and after["history"][path][-1]["hash"] != digest(before):
                _snapshot(after, path, before, "EXTERNAL")
            _snapshot(after, path, raw, "RESTORE")
            after["history"][path][-1]["restored_from"] = version_id
            self.storage.transact(state, after, path, raw, before)
            return self._document(path, after)
