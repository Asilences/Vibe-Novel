"""Immutable manuscript sources and author-controlled revision working copies.

Preparation publishes its manifest last. A failed preparation may leave versioned
draft copies, but it never exposes a complete session or changes story canon.
"""
from __future__ import annotations

import copy
import re
from typing import Optional
from uuid import uuid4

from vibenovel.errors import VibeNovelError
from .documents import (HASH, DocumentStore, _check_expected, _content_bytes,
                        _read_raw, _snapshot)
from .files import digest, json_bytes, read_json, tree_files
from .locking import ConflictError
from .project import Project, utc_now

MAX_SECTIONS = 80
MAX_SECTION_CHARACTERS = 200000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")
SECTION_ID = re.compile(r"section-([0-9]{3})\Z")
MANIFEST_PATH = re.compile(r"reviews/manuscript-([0-9a-f]{32})\.json\Z")
HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?"
    r"(?:第[0-9零〇一二三四五六七八九十百千万两]{1,16}[章回](?:[ \t：:、.．].*)?"
    r"|chapter[ \t]+[0-9]{1,6}(?:[ \t：:、.．-].*)?)[ \t]*$", re.I)
LIMITATIONS = [
    "这是已有稿的独立修订工作副本，不是正式章节、已确认设定或已发生事实。",
    "分章标题检测只是建议；请作者核对前言、番外、重复标题与正文内伪标题。",
    "降AI味指作者选择的表达编辑偏好，不提供AI生成检测概率或通过检测保证。",
    "原件和未选范围保留，API只产生候选；正式正文/记忆仍须作者另行审批。",
    "单会话最多80段、8MiB，每段最多200000字符；范围与遗漏必须明确。",
    "导出只在相邻非空工作副本均无换行分隔时补一个换行，避免章标题与上章末句粘连。",
]


class ManuscriptStore:
    def __init__(self, project: Project):
        self.project = project
        self.documents = DocumentStore(project)
        self.storage = self.documents.storage

    def _id(self, identifier):
        if not isinstance(identifier, str) or not SESSION_ID.fullmatch(identifier):
            raise VibeNovelError("修订会话ID无效")
        return identifier

    def _source(self, path, expected, state):
        imported = isinstance(path, str) and re.fullmatch(r"imports/original/[0-9a-f]{64}/extracted\.md", path)
        ordinary = isinstance(path, str) and path.endswith(".md") and path.startswith(
            ("drafts/", "chapters/", "outlines/source/", "bible/", "memory/"))
        if not imported and not ordinary:
            raise VibeNovelError("已有小说修订只接受导入抽取文本或已保存的Markdown文稿")
        if not isinstance(expected, str) or not HASH.fullmatch(expected):
            raise VibeNovelError("来源需要完整SHA256，请先读取文稿")
        document = self.documents._document(path, state)
        if document["hash"] != expected:
            raise ConflictError("来源文稿已变化，请刷新后重新分章")
        if len(document["content"].encode("utf-8")) > MAX_SOURCE_BYTES:
            raise VibeNovelError("单次已有稿最多8MiB，请先按卷拆分导入")
        if not document["content"].strip():
            raise VibeNovelError("修订来源不能为空")
        return document

    def _preview(self, source_path, source_hash, start_lines, state):
        source = self._source(source_path, source_hash, state)
        text = source["content"]
        lines = text.splitlines(keepends=True)
        warnings = []
        if start_lines is None:
            starts = [number for number, line in enumerate(lines, 1) if HEADING.fullmatch(line.rstrip("\r\n"))]
            if not starts:
                warnings.append("未识别到明确章标题，当前按整稿单段展示；可手工指定起始行。")
        else:
            if (not isinstance(start_lines, list) or len(start_lines) > MAX_SECTIONS
                    or any(type(number) is not int or not 1 <= number <= len(lines) for number in start_lines)
                    or start_lines != sorted(set(start_lines))):
                raise VibeNovelError("start_lines需要最多80个严格升序且不重复的有效起始行号")
            starts = list(start_lines)
        if not starts or starts[0] != 1:
            if starts:
                warnings.append("首个章标题之前的内容作为独立前言保留，没有遗漏。")
            starts.insert(0, 1)
        if len(starts) > MAX_SECTIONS:
            raise VibeNovelError("检测到超过80段，请手工合并分段或按卷导入")
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        sections = []
        for index, start in enumerate(starts):
            end = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines)
            left, right = offsets[start - 1], offsets[end]
            if right - left > MAX_SECTION_CHARACTERS:
                raise VibeNovelError("单段超过200000字符，请在预览中增加起始行或先拆分来源")
            first = next((line.strip().lstrip("#").strip() for line in lines[start - 1:end] if line.strip()), "空白段")
            sections.append({"id": "section-%03d" % (index + 1), "order": index + 1,
                "title": first[:200], "start_line": start, "end_line": end,
                "start_char": left, "end_char": right, "characters": right - left})
        return {"novel_id": self.project.novel_id,
            "source": {"path": source_path, "hash": source_hash, "characters": len(text), "lines": len(lines)},
            "sections": sections, "warnings": warnings, "limitations": LIMITATIONS}, text

    def preview(self, source_path: str, source_hash: str, start_lines: Optional[list[int]] = None) -> dict:
        with self.storage.locked() as state:
            return self._preview(source_path, source_hash, start_lines, state)[0]

    def prepare(self, source_path: str, source_hash: str, start_lines: Optional[list[int]] = None) -> dict:
        with self.storage.locked() as state:
            preview, text = self._preview(source_path, source_hash, start_lines, state)
        identifier = uuid4().hex
        sections = []
        for entry in preview["sections"]:
            content = text[entry["start_char"]:entry["end_char"]]
            path = "drafts/manuscript-%s/ch%03d.md" % (identifier, entry["order"])
            document = self.documents.save(path, content, None, "AUTHOR",
                                           expected_sources={source_path: source_hash})
            sections.append({**entry, "path": path, "original_hash": document["hash"]})
        manifest = {"schema_version": 1, "id": identifier, "novel_id": self.project.novel_id,
            "created_at": utc_now(), "source": preview["source"], "sections": sections,
            "warnings": preview["warnings"], "limitations": LIMITATIONS}
        self.documents.save("reviews/manuscript-%s.json" % identifier, json_bytes(manifest).decode("utf-8"), None,
                            "AUTHOR", expected_sources={source_path: source_hash,
                            **{entry["path"]: entry["original_hash"] for entry in sections}})
        return self.read(identifier)

    def _manifest(self, identifier, state):
        identifier = self._id(identifier)
        path = "reviews/manuscript-%s.json" % identifier
        document = self.documents._document(path, state)
        data = read_json(self.project.path(path), max_bytes=2 * 1024 * 1024)
        if (set(data) != {"schema_version", "id", "novel_id", "created_at", "source", "sections", "warnings", "limitations"}
                or type(data.get("schema_version")) is not int or data["schema_version"] != 1
                or data.get("id") != identifier or data.get("novel_id") != self.project.novel_id
                or not isinstance(data.get("created_at"), str) or not data["created_at"].strip()
                or len(data["created_at"]) > 100
                or any(not isinstance(data.get(key), list) or len(data[key]) > 100
                       or any(not isinstance(item, str) or not item.strip() or len(item) > 2000
                              for item in data[key]) for key in ("warnings", "limitations"))
                or not isinstance(data.get("source"), dict)
                or set(data["source"]) != {"path", "hash", "characters", "lines"}
                or not isinstance(data.get("sections"), list) or not 1 <= len(data["sections"]) <= MAX_SECTIONS):
            raise VibeNovelError("修订清单格式或所属作品无效")
        source = data["source"]
        if (not isinstance(source["path"], str) or len(source["path"]) > 4096
                or not isinstance(source["hash"], str) or not HASH.fullmatch(source["hash"])
                or type(source["characters"]) is not int or not 1 <= source["characters"] <= MAX_SOURCE_BYTES
                or type(source["lines"]) is not int or not 1 <= source["lines"] <= source["characters"]):
            raise VibeNovelError("修订清单来源字段无效")
        self.project.path(source["path"])
        offset, line = 0, 1
        for index, section in enumerate(data["sections"], 1):
            fields = {"id", "order", "title", "start_line", "end_line", "start_char", "end_char", "characters", "path", "original_hash"}
            if (not isinstance(section, dict) or set(section) != fields
                    or section["id"] != "section-%03d" % index or type(section["order"]) is not int or section["order"] != index
                    or section["path"] != "drafts/manuscript-%s/ch%03d.md" % (identifier, index)
                    or not isinstance(section["title"], str) or not 1 <= len(section["title"]) <= 200
                    or not isinstance(section["original_hash"], str) or not HASH.fullmatch(section["original_hash"])
                    or any(type(section[key]) is not int for key in ("start_line", "end_line", "start_char", "end_char", "characters"))
                    or section["start_char"] != offset or section["start_line"] != line
                    or section["end_char"] <= offset or section["end_line"] < line
                    or section["characters"] != section["end_char"] - offset
                    or section["characters"] > MAX_SECTION_CHARACTERS):
                raise VibeNovelError("修订清单分段路径、次序或来源范围无效")
            offset, line = section["end_char"], section["end_line"] + 1
        if offset != source["characters"] or line != source["lines"] + 1:
            raise VibeNovelError("修订清单未精确覆盖来源全文")
        history = state["history"].get(path, [])
        if not history or history[0]["hash"] != document["hash"] or history[-1]["id"] != history[0]["id"]:
            raise ConflictError("修订清单已被编辑或回退，请重新创建独立修订会话")
        return data, document

    def _read(self, identifier, state):
        data, document = self._manifest(identifier, state)
        result = copy.deepcopy(data)
        reasons = []
        try:
            self._source(data["source"]["path"], data["source"]["hash"], state)
        except (OSError, VibeNovelError):
            reasons.append("原始来源已变化或缺失；工作副本仍保留，请核对后重新创建会话。")
        for section in result["sections"]:
            raw = _read_raw(self.project, section["path"])
            current = digest(raw) if raw is not None else None
            section.update(current_hash=current, changed=current != section["original_hash"], accepted=False)
            if raw is None:
                reasons.append("修订工作副本缺失：" + section["id"])
            history = state["history"].get(section["path"], [])
            section["accepted"] = bool(history and history[-1]["hash"] == current and history[-1].get("accepted_candidate"))
        result.update(path=document["path"], hash=document["hash"], stale=bool(reasons), stale_reasons=reasons)
        return result

    def read(self, identifier: str) -> dict:
        with self.storage.locked() as state:
            return self._read(identifier, state)

    def list(self) -> dict:
        with self.storage.locked() as state:
            sessions, omitted = [], []
            for index, path in enumerate(tree_files(self.project.path("reviews"))):
                if index >= 1000:
                    omitted.append({"reason": "catalog_limit"})
                    break
                match = MANIFEST_PATH.fullmatch(path.relative_to(self.project.root).as_posix())
                if match is None:
                    continue
                if len(sessions) >= 100:
                    omitted.append({"reason": "session_limit"})
                    break
                try:
                    sessions.append(self._read(match[1], state))
                except (OSError, VibeNovelError):
                    omitted.append({"id": match[1], "reason": "invalid_or_incomplete_session"})
            return {"manuscripts": sorted(sessions, key=lambda value: value["created_at"], reverse=True), "omitted": omitted}

    def accept(self, identifier: str, section_id: str, base_hash: str,
               candidate_path: str, candidate_hash: str) -> dict:
        with self.storage.locked() as state:
            session = self._read(identifier, state)
            if session["stale"]:
                raise ConflictError("修订来源或工作副本已失效，请先核对")
            section = next((entry for entry in session["sections"] if entry["id"] == section_id), None)
            if section is None:
                raise VibeNovelError("修订章节ID不在当前清单")
            base = self.documents._document(section["path"], state)
            if base["hash"] != base_hash:
                raise ConflictError("修订工作副本已变化，不能接受旧候选")
            match = re.fullmatch(r"drafts/rewrite-([0-9a-f]{32})\.md", candidate_path) if isinstance(candidate_path, str) else None
            if match is None:
                raise VibeNovelError("仅接受成功改写任务生成的独立候选")
            job = read_json(self.project.path(".runtime/jobs/%s.json" % match[1]), max_bytes=8 * 1024 * 1024)
            if (job.get("schema_version") != 1 or job.get("novel_id") != self.project.novel_id
                    or job.get("id") != match[1] or job.get("state") != "succeeded" or job.get("kind") != "rewrite"
                    or job.get("provider") != "api" or job.get("result_path") != candidate_path
                    or job.get("result_hash") != candidate_hash or not isinstance(job.get("request"), dict)
                    or job["request"].get("document_path") != section["path"]
                    or job["request"].get("document_hash") != base_hash
                    or not isinstance(job.get("context_sources"), dict)
                    or job["context_sources"].get(section["path"]) != base_hash
                    or len(job["context_sources"]) > 100):
                raise ConflictError("候选不是当前工作副本的成功改写结果，请重新提交")
            if (job.get("canonical_revision") != state["revision"]
                    or job.get("fingerprint") != state["fingerprint"]):
                raise ConflictError("候选生成后正式资料版本已变化，请重新审稿")
            for path, expected in job["context_sources"].items():
                document = self.documents._document(path, state)
                if document["hash"] != expected:
                    raise ConflictError("改写候选的输入来源已变化，请重新审阅并生成")
            editorial = job.get("editorial_context", {})
            if not isinstance(editorial, dict):
                raise VibeNovelError("改写候选的编辑上下文无效")
            runtime = editorial.get("runtime_sources", {})
            if not isinstance(runtime, dict) or set(runtime) - {
                    ".runtime/style-baseline.json", ".runtime/writing-toolkit.json"}:
                raise VibeNovelError("改写候选的配置依赖无效")
            for path, expected in runtime.items():
                _check_expected(_read_raw(self.project, path), expected)
            candidate = self.documents._document(candidate_path, state)
            history = state["history"].get(candidate_path, [])
            if (candidate["hash"] != candidate_hash or not history
                    or not isinstance(job.get("result_version_id"), str)
                    or history[-1]["id"] != job["result_version_id"]
                    or history[-1]["hash"] != candidate_hash or history[-1]["provenance"] != "AI_SUGGESTED"):
                raise ConflictError("候选已被编辑、回退或不是原始AI候选版本；未覆盖工作副本")
            raw = _content_bytes(section["path"], candidate["content"])
            before = _read_raw(self.project, section["path"])
            _check_expected(before, base_hash)
            after = copy.deepcopy(state)
            if not after["history"].get(section["path"]) or after["history"][section["path"]][-1]["hash"] != base_hash:
                _snapshot(after, section["path"], before, "EXTERNAL")
            _snapshot(after, section["path"], raw, "AUTHOR")
            after["history"][section["path"]][-1]["accepted_candidate"] = {
                "path": candidate_path, "hash": candidate_hash, "version_id": job["result_version_id"]}
            self.storage.transact(state, after, section["path"], raw, before)
            return self._read(identifier, after)

    def export(self, identifier: str) -> dict:
        with self.storage.locked() as state:
            session = self._read(identifier, state)
            if session["stale"]:
                raise ConflictError("修订工作副本或来源已失效，请核对后再导出")
            parts = []
            trailing = None
            for section in session["sections"]:
                text = self.documents._document(section["path"], state)["content"]
                if trailing is not None and text and trailing not in ("\r", "\n") and not text.startswith(("\r", "\n")):
                    parts.append("\n")
                parts.append(text)
                if text:
                    trailing = text[-1]
            content = "".join(parts)
            return {"filename": "manuscript-%s-revised.md" % identifier, "content": content,
                    "hash": digest(content.encode("utf-8")), "characters": len(content), "sections": len(session["sections"])}
