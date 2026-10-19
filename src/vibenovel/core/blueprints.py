"""Bounded, source-bound planning candidates, separate from established canon."""
from __future__ import annotations

import copy
import json
import re
from typing import Optional

from vibenovel.errors import MissingDocumentError, VibeNovelError
from .approvals import (ApprovalStore, _check_proposal_dependencies, _dependency_versions,
                        _latest_applied_id)
from .documents import (HASH, MAX_DOCUMENT_BYTES, ConflictError, DocumentStore, _Storage,
                        _check_expected, _content_bytes, _decode, _kind, _read_raw)
from .files import digest, file_digest, json_bytes, read_json, tree_files
from .project import Project

APPROVED_BLUEPRINT = "outlines/approved/blueprint.json"
COMMON = {"id", "title", "summary", "basis", "evidence"}
EXTRA = {
    "book": set(),
    "volumes": {"number", "chapter_start", "chapter_end"},
    "chapters": {"number", "volume_id", "goal", "conflict", "turn", "outcome", "beats",
                 "character_ids", "depends_on", "thread_ids"},
    "characters": {"role", "knowledge"},
    "relationships": {"from_id", "to_id"},
    "threads": {"setup_chapter", "payoff_chapter"},
    "timeline": {"time", "chapter", "character_ids", "status"},
    "settings": {"category"},
    "style": {"rules", "avoid"},
}
LIMITS = {"volumes": 100, "chapters": 1000, "characters": 500, "relationships": 1000,
          "threads": 1000, "timeline": 1000, "settings": 500}
TOP_FIELDS = set(EXTRA) | {"schema_version", "novel_id", "source_hashes", "unknowns"}
ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")


class _StaleBlueprint(ConflictError):
    pass


def _fail(message: str) -> None:
    raise VibeNovelError(f"创作规划包无效: {message}")


def _text(value, label: str, *, maximum: int = 12000) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(f"{label} 需要 1–{maximum} 字文本")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail(f"{label} 包含无效 Unicode")


def _strings(value, label: str, *, maximum: int = 100, identifiers: bool = False) -> None:
    if not isinstance(value, list) or len(value) > maximum:
        _fail(f"{label} 需要最多 {maximum} 项的数组")
    for item in value:
        _text(item, label, maximum=80 if identifiers else 12000)
        if identifiers and not ID.fullmatch(item):
            _fail(f"{label} 含无效 ID")
    if identifiers and len(value) != len(set(value)):
        _fail(f"{label} 不允许重复引用")


def _number(value, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if type(value) is not int or not 1 <= value <= 1000000:
        _fail(f"{label} 需要 1–1000000 的整数" + ("或 null" if nullable else ""))


def _parse(text: str, *, fenced: bool = False) -> dict:
    _content_bytes("outlines/proposals/blueprint.md", text)
    if fenced:
        match = re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*", text, re.DOTALL)
        if match:
            text = match.group(1)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail(f"重复 JSON 字段: {key}")
            result[key] = value
        return result

    def constant(value):
        _fail(f"JSON 不支持 {value}")

    try:
        data = json.loads(text.lstrip("\ufeff"), object_pairs_hook=unique, parse_constant=constant)
        json_bytes(data)  # Reject escaped invalid Unicode and overflowing floats too.
    except (ValueError, RecursionError) as exc:
        raise VibeNovelError("创作规划包需要单个严格 JSON 对象") from exc
    if not isinstance(data, dict):
        _fail("顶层必须为对象")
    return data


def _sources(project: Project, hashes: dict) -> dict[str, str]:
    if not isinstance(hashes, dict) or len(hashes) > 100:
        _fail("source_hashes 需要最多 100 项的来源路径/SHA256 对象")
    sources = {}
    for path, expected in hashes.items():
        _kind(project, path)
        if not isinstance(expected, str) or not HASH.fullmatch(expected):
            _fail("来源 hash 需要完整 SHA256")
        try:
            document = DocumentStore(project)._document(path, {"revision": 0})
        except (VibeNovelError, OSError) as exc:
            raise _StaleBlueprint(f"规划来源已缺失或无法验证: {path}（{exc}）") from exc
        if document["hash"] != expected:
            raise _StaleBlueprint(f"规划来源版本已变化: {path}")
        sources[path] = document["content"]
    return sources


def _validate(project: Project, data: dict) -> None:
    if set(data) != TOP_FIELDS:
        _fail("顶层缺字段或包含未知字段")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        _fail("schema_version 必须为 1")
    if data["novel_id"] != project.novel_id:
        _fail("novel_id 与当前作品不符")
    _strings(data["unknowns"], "unknowns")
    sources = _sources(project, data["source_hashes"])
    ids = set()
    records = {}
    for section, extra in EXTRA.items():
        if section in ("book", "style"):
            entries = [data[section]]
        else:
            entries = data[section]
            if not isinstance(entries, list) or len(entries) > LIMITS[section]:
                _fail(f"{section} 需要最多 {LIMITS[section]} 项的数组")
        records[section] = entries
        for item in entries:
            if not isinstance(item, dict) or set(item) != COMMON | extra:
                _fail(f"{section} 记录缺字段或包含未知字段")
            if not isinstance(item["id"], str) or not ID.fullmatch(item["id"]) or item["id"] in ids:
                _fail("记录 ID 无效或在全包中重复")
            ids.add(item["id"])
            for field in ("title", "summary"):
                _text(item[field], f"{section}.{field}")
            if item["basis"] not in ("source_asserted", "inferred", "suggested"):
                _fail("basis 只允许 source_asserted / inferred / suggested")
            evidence = item["evidence"]
            if not isinstance(evidence, list) or len(evidence) > 20:
                _fail("每条记录最多 20 项出处")
            if item["basis"] == "source_asserted" and not evidence:
                _fail("source_asserted 必须有原文出处")
            for quote in evidence:
                if not isinstance(quote, dict) or set(quote) != {"path", "start_line", "end_line", "quote"}:
                    _fail("evidence 缺字段或包含未知字段")
                path = quote["path"]
                if not isinstance(path, str) or path not in sources:
                    _fail("出处路径不在运行时绑定的来源中")
                _number(quote["start_line"], "start_line")
                _number(quote["end_line"], "end_line")
                start, end = quote["start_line"], quote["end_line"]
                _text(quote["quote"], "quote")
                lines = sources[path].splitlines()
                if end < start or end > len(lines) or quote["quote"] != "\n".join(lines[start - 1:end]):
                    _fail("出处 quote 与绑定来源的行范围不精确匹配")
    volumes = {item["id"]: item for item in records["volumes"]}
    chapters = {item["id"]: item for item in records["chapters"]}
    characters = {item["id"] for item in records["characters"]}
    threads = {item["id"] for item in records["threads"]}

    def reference(value, known, label):
        if not isinstance(value, str) or value not in known:
            _fail(f"{label} 引用了不存在的 ID")

    numbers = set()
    ranges = []
    for item in volumes.values():
        for field in ("number", "chapter_start"):
            _number(item[field], f"volumes.{field}")
        _number(item["chapter_end"], "volumes.chapter_end", nullable=True)
        end = item["chapter_end"] if item["chapter_end"] is not None else 1000000
        if item["number"] in numbers or end < item["chapter_start"]:
            _fail("卷编号重复或范围倒置")
        numbers.add(item["number"])
        ranges.append((item["chapter_start"], end, item["number"]))
    previous_end, previous_number = 0, 0
    for start, end, number in sorted(ranges):
        if start <= previous_end or number <= previous_number:
            _fail("卷区间重叠或卷编号与范围顺序不符")
        previous_end, previous_number = end, number
    numbers = set()
    for item in chapters.values():
        _number(item["number"], "chapters.number")
    for item in chapters.values():
        _number(item["number"], "chapters.number")
        if item["number"] in numbers:
            _fail("章编号重复")
        numbers.add(item["number"])
        reference(item["volume_id"], volumes, "volume_id")
        volume = volumes[item["volume_id"]]
        if not volume["chapter_start"] <= item["number"] <= (volume["chapter_end"] or 1000000):
            _fail("章编号不在所属卷范围内")
        for field in ("goal", "conflict", "turn", "outcome"):
            _text(item[field], f"chapters.{field}")
        _strings(item["beats"], "beats")
        for field, known in (("character_ids", characters), ("thread_ids", threads), ("depends_on", chapters)):
            _strings(item[field], field, identifiers=True)
            for value in item[field]:
                reference(value, known, field)
                if field == "depends_on" and chapters[value]["number"] >= item["number"]:
                    _fail("章计划不得依赖自身或后续章")
    for item in records["characters"]:
        for field in ("role", "knowledge"):
            _text(item[field], f"characters.{field}")
    for item in records["relationships"]:
        reference(item["from_id"], characters, "from_id")
        reference(item["to_id"], characters, "to_id")
    for item in records["threads"]:
        _number(item["setup_chapter"], "setup_chapter", nullable=True)
        _number(item["payoff_chapter"], "payoff_chapter", nullable=True)
        if (item["setup_chapter"] is not None and item["payoff_chapter"] is not None
                and item["payoff_chapter"] < item["setup_chapter"]):
            _fail("伏笔回收不得早于铺设")
    for item in records["timeline"]:
        _text(item["time"], "timeline.time")
        _number(item["chapter"], "timeline.chapter", nullable=True)
        if item["status"] != "planned":
            _fail("时间线 status 只能是 planned，不能冒充已发生事件")
        _strings(item["character_ids"], "timeline.character_ids", identifiers=True)
        for value in item["character_ids"]:
            reference(value, characters, "timeline.character_ids")
    for item in records["settings"]:
        if item["category"] not in ("world", "location", "organization", "item", "rule", "domain"):
            _fail("未知设定 category")
    for field in ("rules", "avoid"):
        _strings(data["style"][field], f"style.{field}")


def prepare_blueprint(project: Project, text: str, source_hashes: dict[str, str]) -> str:
    """Normalize model JSON using only runtime-owned project/source identity."""
    data = _parse(text, fenced=True)
    if "novel_id" in data and data["novel_id"] != project.novel_id:
        _fail("模型返回了其他作品的 novel_id")
    data["novel_id"] = project.novel_id
    data["source_hashes"] = copy.deepcopy(source_hashes)
    _validate(project, data)
    content = json_bytes(data).decode("utf-8")
    _content_bytes("outlines/proposals/blueprint.json", content)
    return content


def _planning_path(project: Project, path: str) -> None:
    _kind(project, path)
    if path != APPROVED_BLUEPRINT and not (path.startswith("outlines/proposals/") and path.endswith(".json")):
        raise VibeNovelError("创作规划包路径只允许候选 JSON 或当前已批准 blueprint.json")


def _chapter_markdown(data: dict, chapter: dict) -> str:
    lines = [f"# 第 {chapter['number']} 章：{chapter['title']}", "",
             "> 规划性质：这是待执行的未来计划，不代表已发生事件或正式记忆。", "",
             f"- 规划包：{APPROVED_BLUEPRINT}", f"- 章 ID：{chapter['id']}",
             f"- 依据：{chapter['basis']}", "", "## 本章概要", "", chapter["summary"]]
    for field, label in (("goal", "目标"), ("conflict", "冲突"), ("turn", "转折"), ("outcome", "预期结尾")):
        lines.extend(["", f"## {label}", "", chapter[field]])
    for field, label in (("beats", "场景节拍"), ("character_ids", "人物 ID"),
                         ("depends_on", "依赖章 ID"), ("thread_ids", "伏笔 ID")):
        lines.extend(["", f"## {label}", "", *(f"- {value}" for value in chapter[field])])
    lines.extend(["", "## 章节原文出处", ""])
    for evidence in chapter["evidence"]:
        lines.extend([f"- {evidence['path']}:{evidence['start_line']}–{evidence['end_line']}", "",
                      *(f"> {line}" for line in evidence["quote"].splitlines()), ""])
    lines.extend(["", "## 待作者确认", "", *(f"- {value}" for value in data["unknowns"])])
    return "\n".join(lines).rstrip() + "\n"


class PlanningStore:
    def __init__(self, project: Project):
        self.project = project
        self.storage = _Storage(project)

    def _creation_origin(self, path, data, state):
        """Keep generated first-outline planning bound to its durable input versions.

        Older ordinary planning packages remain hash-compatible. New creation
        packages also carry the brief/selection in source_hashes; their original
        job is a backend-owned provenance record, not model-supplied JSON.
        """
        from .creation import CREATIVE_BRIEF_PATH, CREATION_SELECTION_PATH
        if path == APPROVED_BLUEPRINT:
            # Applied packages use the existing recorded approval dependency
            # versions, checked below; they need not retain the candidate path.
            return
        match = re.fullmatch(r"outlines/proposals/blueprint-([0-9a-f]{32})\.json", path)
        has_creation_sources = bool({CREATIVE_BRIEF_PATH, CREATION_SELECTION_PATH} & set(data["source_hashes"]))
        job = None
        if match:
            runtime_path = self.project.path(".runtime/jobs/" + match[1] + ".json")
            if runtime_path.exists():
                try:
                    job = read_json(runtime_path, max_bytes=8 * 1024 * 1024)
                except (OSError, VibeNovelError) as exc:
                    raise _StaleBlueprint("规划包原任务无法验证，已阻止重新审批") from exc
        if not has_creation_sources and not (job and job.get("creation_origin")):
            return
        if (not job or job.get("schema_version") != 1 or job.get("novel_id") != self.project.novel_id
                or not match or job.get("id") != match[1] or job.get("kind") != "build_blueprint"
                or job.get("state") != "succeeded" or job.get("result_path") != path
                or not isinstance(job.get("creation_origin"), dict)
                or job.get("context_sources") != data["source_hashes"]):
            raise _StaleBlueprint("创作规划包缺少匹配的原任务及简报选用来源，不能复活旧规划")
        versions = job.get("source_versions")
        if not isinstance(versions, dict) or set(versions) != set(data["source_hashes"]):
            raise _StaleBlueprint("创作规划包缺少完整的冻结来源历史")
        for source, expected in versions.items():
            history = state["history"].get(source, [])
            current = history[-1]["id"] if history else None
            if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{32}", expected)):
                raise _StaleBlueprint("创作规划包的冻结来源历史身份无效")
            if current != expected:
                raise _StaleBlueprint("创作规划包来源存在更新的历史版本，请重新选用并展开规划: " + source)

    def _read(self, path: str, state: dict) -> dict:
        _planning_path(self.project, path)
        result = {"path": path, "hash": None, "content": "", "data": None, "valid": False,
                  "issues": [], "approved": False, "stale": False}
        try:
            raw = _read_raw(self.project, path)
            if raw is None:
                raise MissingDocumentError(f"规划包不存在: {path}")
            result["hash"] = digest(raw)
            result["content"] = _decode(raw)
            result["data"] = _parse(result["content"])
            _validate(self.project, result["data"])
            self._creation_origin(path, result["data"], state)
            result["valid"] = True
        except MissingDocumentError:
            raise
        except (VibeNovelError, OSError) as exc:
            result["issues"].append(str(exc))
            result["stale"] = isinstance(exc, _StaleBlueprint)
            # Keep malformed and oversized candidates inspectable, with a
            # bounded preview. They can never qualify as approved plans.
            if not result["content"] and self.project.path(path).is_file():
                with self.project.path(path).open("rb") as stream:
                    result["content"] = stream.read(MAX_DOCUMENT_BYTES).decode("utf-8", errors="replace")
                result["hash"] = file_digest(self.project.path(path))
        if result["valid"] and path == APPROVED_BLUEPRINT:
            approvals = ApprovalStore(self.project)
            try:
                proposal_id = _latest_applied_id(state, path)
                if proposal_id is not None:
                    proposal = approvals._get(state, proposal_id)
                    if proposal["after_hash"] == result["hash"]:
                        _check_proposal_dependencies(self.project, state, proposal)
                        result["approved"] = True
            except (VibeNovelError, OSError) as exc:
                result["issues"].append(str(exc))
                result["valid"] = False
                result["stale"] = True
        return result

    def read(self, path: str) -> dict:
        _planning_path(self.project, path)
        with self.storage.locked() as state:
            return self._read(path, state)

    def list(self) -> list[dict]:
        with self.storage.locked() as state:
            paths = [path.relative_to(self.project.root).as_posix()
                     for path in tree_files(self.project.path("outlines/proposals")) if path.suffix == ".json"]
            if self.project.path(APPROVED_BLUEPRINT).is_file():
                paths.append(APPROVED_BLUEPRINT)
            return [{key: value for key, value in self._read(path, state).items()
                     if key not in ("content", "data")} for path in sorted(paths)]

    def propose(self, path: str, expected_hash: str, chapter_id: Optional[str] = None) -> dict:
        with self.storage.locked() as state:
            package = self._read(path, state)
            _check_expected(package["content"].encode("utf-8"), expected_hash)
            if not package["valid"] or package["stale"]:
                raise ConflictError("规划包无效或来源已变化，请先修复规划包")
            data = package["data"]
            dependencies = copy.deepcopy(data["source_hashes"])
            if chapter_id is None:
                target, content = APPROVED_BLUEPRINT, package["content"]
                reason, impact = "提交创作规划包供作者审阅", ["仅更新未来创作计划，不确认情节发生，不更新正式正文或记忆"]
            else:
                if not isinstance(chapter_id, str) or not ID.fullmatch(chapter_id):
                    raise VibeNovelError("章计划 ID 无效")
                if path != APPROVED_BLUEPRINT or not package["approved"]:
                    raise ConflictError("只有有效且已应用审批的父规划包可以提取章计划")
                chapter = next((item for item in data["chapters"] if item["id"] == chapter_id), None)
                if chapter is None:
                    raise VibeNovelError("规划包中未找到此章 ID")
                target = f"outlines/approved/ch{chapter['number']:03d}.md"
                content = _chapter_markdown(data, chapter)
                dependencies[path] = package["hash"]
                reason, impact = f"从已批准规划包提取第 {chapter['number']} 章计划", ["本章草稿须待本提案批准并应用后生成", "父规划包或来源变化会使章计划失效"]
            if target in dependencies:
                raise VibeNovelError("规划提案的目标不能同时作为来源；请从导入原件、来源大纲或候选重新规划")
            before = _read_raw(self.project, target)
            dependency_versions = _dependency_versions(state, dependencies)
        # The separate create lock rechecks both hashes and observed versions;
        # an A -> B -> A between these locks cannot revive the read snapshot.
        return ApprovalStore(self.project).create(target, content, reason, impact,
                                                  digest(before) if before is not None else None,
                                                  dependencies=dependencies,
                                                  expected_dependency_versions=dependency_versions)
