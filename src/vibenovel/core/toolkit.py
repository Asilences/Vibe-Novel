"""Author-owned writing constraints and evidence-only long-form diagnostics.

Registrations are reminders, never story facts. All scans are read-only and
bounded; a name/term occurrence cannot establish presence or payoff.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Optional

from vibenovel.errors import VibeNovelError
from .documents import DocumentStore, _check_expected, _decode, _read_raw
from .files import atomic_write, digest, json_bytes, tree_files, _unique_pairs, _reject_constant
from .project import Project, utc_now

TOOLKIT_PATH = ".runtime/writing-toolkit.json"
MAX_SETTINGS = 512 * 1024
MAX_AUDIT_ISSUES = 2000


def _chapter(path: str) -> Optional[int]:
    match = re.fullmatch(r"ch(\d{1,6})(?:[_-](?:v\d+|\d{4,8}))?", PurePosixPath(path).stem, re.I)
    return int(match[1]) if match and int(match[1]) > 0 else None


def _text(value, maximum=500, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip())
            or any(ord(char) < 32 and char not in "\n\t\r" for char in value)):
        raise VibeNovelError("写作工具配置含空文本、控制字符或超长字段")
    return value


def _strings(value, maximum=20):
    if not isinstance(value, list) or len(value) > maximum:
        raise VibeNovelError("词项必须为有长度限制的文本列表")
    result = [_text(item, 100) for item in value]
    if len(set(result)) != len(result):
        raise VibeNovelError("同一词项列表不能重复")
    return result


def _integer(value, minimum=1, maximum=999999):
    if type(value) is not int or not minimum <= value <= maximum:
        raise VibeNovelError("章节/跨度必须为有效整数")
    return value


class WritingToolkitStore:
    def __init__(self, project: Project):
        self.project = project
        self.documents = DocumentStore(project)
        self.storage = self.documents.storage

    def _validate(self, data: dict) -> dict:
        required = {"lexicon", "characters", "threads", "style"}
        if not isinstance(data, dict) or set(data) != required:
            raise VibeNovelError("写作工具需要 lexicon、characters、threads、style 四组配置")
        result = copy.deepcopy(data)
        specs = {"lexicon": {"id", "term", "aliases", "forbidden", "required", "note"},
                 "characters": {"id", "name", "aliases", "max_absence"},
                 "threads": {"id", "label", "terms", "start_chapter", "remind_after", "end_chapter"}}
        for group, fields in specs.items():
            entries = result[group]
            if not isinstance(entries, list) or len(entries) > 200:
                raise VibeNovelError("每类登记最多200项")
            identifiers = set()
            for item in entries:
                if not isinstance(item, dict) or set(item) != fields:
                    raise VibeNovelError(f"{group} 配置字段不完整或含未知字段")
                identifier = _text(item["id"], 100)
                if identifier in identifiers:
                    raise VibeNovelError("同类登记ID不能重复")
                identifiers.add(identifier)
                if group == "lexicon":
                    _text(item["term"], 100)
                    _strings(item["aliases"])
                    _strings(item["forbidden"])
                    _text(item["note"], 1000, empty=True)
                    if type(item["required"]) is not bool:
                        raise VibeNovelError("required必须为布尔值")
                    if item["term"] in item["aliases"] + item["forbidden"]:
                        raise VibeNovelError("规范词不能同时作为别名或禁用词")
                elif group == "characters":
                    _text(item["name"], 100)
                    _strings(item["aliases"])
                    _integer(item["max_absence"])
                else:
                    _text(item["label"], 100)
                    _strings(item["terms"])
                    _integer(item["start_chapter"])
                    _integer(item["remind_after"])
                    if item["end_chapter"] is not None:
                        _integer(item["end_chapter"], item["start_chapter"])
        style = result["style"]
        if not isinstance(style, dict) or set(style) != {"instructions", "avoid", "anchors"}:
            raise VibeNovelError("style需要instructions、avoid、anchors")
        _text(style["instructions"], 6000, empty=True)
        _strings(style["avoid"], 50)
        if not isinstance(style["anchors"], list) or len(style["anchors"]) > 5:
            raise VibeNovelError("最多选择5处作者文风锚段")
        for anchor in style["anchors"]:
            if not isinstance(anchor, dict) or set(anchor) != {"path", "hash", "start_line", "end_line", "quote"}:
                raise VibeNovelError("锚段配置字段无效")
            path = _text(anchor["path"], 1000)
            self.project.path(path)
            if not path.startswith(("chapters/", "drafts/")) or not path.endswith(".md"):
                raise VibeNovelError("锚段仅允许正式章/草稿Markdown")
            if not isinstance(anchor["hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", anchor["hash"]):
                raise VibeNovelError("锚段需要来源SHA256")
            _integer(anchor["start_line"])
            _integer(anchor["end_line"], anchor["start_line"])
            _text(anchor["quote"], 3000)
        if len(json_bytes(result)) > MAX_SETTINGS:
            raise VibeNovelError("写作工具配置超过512KiB")
        return result

    def _settings(self) -> dict:
        """Internal reader; caller holds the project lock."""
        path = self.project.path(TOOLKIT_PATH)
        data = {"lexicon": [], "characters": [], "threads": [],
                "style": {"instructions": "", "avoid": [], "anchors": []}}
        fingerprint = None
        if path.exists():
            with path.open("rb") as stream:
                raw = stream.read(MAX_SETTINGS + 1)
            if len(raw) > MAX_SETTINGS:
                raise VibeNovelError("写作工具配置超过512KiB")
            try:
                stored = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs,
                                    parse_constant=_reject_constant)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise VibeNovelError("写作工具配置需要有效UTF-8 JSON") from exc
            if not isinstance(stored, dict):
                raise VibeNovelError("写作工具配置顶层必须为对象")
            if (stored.get("schema_version") != 1 or stored.get("novel_id") != self.project.novel_id
                    or set(stored) != {"schema_version", "novel_id", "updated_at", "data"}):
                raise VibeNovelError("写作工具配置版本或作品不匹配")
            try:
                data = self._validate(stored["data"])
            except RecursionError as exc:
                raise VibeNovelError("写作工具配置结构过深") from exc
            fingerprint = digest(raw)
        stale = False
        for anchor in data["style"]["anchors"]:
            raw = None
            try:
                raw = _read_raw(self.project, anchor["path"])
                current = digest(raw) if raw is not None else None
                exact = raw is not None and current == anchor["hash"] and "\n".join(
                    _decode(raw).splitlines()[anchor["start_line"] - 1:anchor["end_line"]]) == anchor["quote"]
            except (OSError, VibeNovelError):
                current, exact = None, False
            anchor.update(current_hash=current, stale=not exact)
            stale |= anchor["stale"]
        return {"schema_version": 1, "novel_id": self.project.novel_id, "configured": fingerprint is not None,
                "hash": fingerprint, **data, "stale": stale}

    def settings(self) -> dict:
        with self.storage.locked():
            return self._settings()

    def save(self, data: dict, expected_hash: Optional[str]) -> dict:
        if (not isinstance(data, dict) or set(data) != {"lexicon", "characters", "threads", "style"}
                or not isinstance(data.get("style"), dict)):
            raise VibeNovelError("写作工具配置字段无效")
        data = copy.deepcopy(data)
        with self.storage.locked() as state:
            location = self.project.path(TOOLKIT_PATH)
            _check_expected(location.read_bytes() if location.exists() else None, expected_hash)
            anchors = data.get("style", {}).get("anchors", [])
            if not isinstance(anchors, list) or len(anchors) > 5:
                raise VibeNovelError("最多选择5处作者锚段")
            for anchor in anchors:
                if not isinstance(anchor, dict) or set(anchor) != {"path", "hash", "start_line", "end_line"}:
                    raise VibeNovelError("保存锚段只需path/hash/start_line/end_line")
                _integer(anchor["start_line"])
                _integer(anchor["end_line"], anchor["start_line"])
                doc = self.documents._document(anchor["path"], state)
                _check_expected(doc["content"].encode("utf-8"), anchor["hash"])
                lines = doc["content"].splitlines()
                if anchor["end_line"] > len(lines):
                    raise VibeNovelError("锚段行号超出原文")
                anchor["quote"] = "\n".join(lines[anchor["start_line"] - 1:anchor["end_line"]])
            validated = self._validate(data)
            raw = json_bytes({"schema_version": 1, "novel_id": self.project.novel_id,
                              "updated_at": utc_now(), "data": validated})
            if len(raw) > MAX_SETTINGS:
                raise VibeNovelError("写作工具配置超过512KiB")
            location.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(location, raw, overwrite=location.exists())
            return self._settings()

    def audit(self, text: str, *, settings=None) -> list[dict]:
        return self.audit_with_coverage(text, settings=settings)["issues"]

    def audit_with_coverage(self, text: str, *, settings=None) -> dict:
        settings = settings if settings is not None else self.settings()
        issues = []
        found = Counter()

        def issue(rule, severity, line, quote, message, suggestion, needle=None):
            found[rule] += 1
            if len(issues) < MAX_AUDIT_ISSUES:
                offset = max(0, quote.find(needle) - 200) if needle else 0
                issues.append({"rule": rule, "severity": severity, "start_line": line,
                               "end_line": line, "quote": quote[offset:offset + 2000],
                               "message": message, "suggestion": suggestion})

        lines = text.splitlines() or [""]
        # Forbidden expressions first; aliases and missing terms are advisory.
        for number, line in enumerate(lines, 1):
            for entry in settings["lexicon"]:
                for term in entry["forbidden"]:
                    if term in line:
                        issue("lexicon_forbidden", "warning", number, line,
                              f"出现作者禁用表达「{term}」（规范词：{entry['term']}）",
                              "对照作者词表确认语境；引用或刻意用法可忽略。", term)
            for term in settings["style"]["avoid"]:
                if term in line:
                    issue("style_avoid", "warning", number, line, f"出现作者文风禁用表达「{term}」",
                          "按作者风格约束复核，必要时改写；规则不代替文学判断。", term)
        for entry in settings["lexicon"]:
            for number, line in enumerate(lines, 1):
                for term in entry["aliases"]:
                    if term in line:
                        issue("lexicon_alias", "info", number, line, f"「{term}」是「{entry['term']}」的登记别名",
                              "核对本段是否应统一用语；人物口语可保留。", term)
            if entry["required"] and not any(term in text for term in [entry["term"], *entry["aliases"]]):
                issue("lexicon_required", "info", 1, lines[0], f"未找到作者必用词「{entry['term']}」或其别名",
                      "确认本章是否适用此词；不应为满足规则硬塞用词。")
        returned = Counter(item["rule"] for item in issues)
        return {"issues": issues, "rule_counts": {rule: {"found": count, "returned": returned[rule],
                "truncated": count - returned[rule]} for rule, count in found.items()}}

    def structure(self) -> dict:
        # Reuse exactly the bounded canonical corpus used by editorial search.
        from .analysis import AnalysisStore
        with self.storage.locked() as state:
            settings = self._settings()
            documents, omitted = AnalysisStore(self.project)._history()
            chapters = [(doc, _chapter(doc["path"])) for doc in documents]
            for doc, chapter in chapters:
                if chapter is None:
                    omitted.append({"path": doc["path"], "reason": "chapter_number_unknown_for_span"})
            latest = max((chapter for _, chapter in chapters if chapter is not None), default=0)

            def evidence(terms, start=1, end=None):
                mentions, quotes = [], []
                for doc, chapter in chapters:
                    if chapter is None or chapter < start or (end is not None and chapter > end):
                        continue
                    for number, line in enumerate(doc["content"].splitlines(), 1):
                        matches = [line.index(term) for term in terms if term in line]
                        if matches:
                            mentions.append(chapter)
                            if len(quotes) < 8:
                                offset = max(0, min(matches) - 200)
                                quotes.append({"path": doc["path"], "hash": doc["hash"], "start_line": number,
                                               "end_line": number, "quote": line[offset:offset + 1000]})
                            break
                return sorted(set(mentions)), quotes

            characters = []
            for entry in settings["characters"]:
                mentioned, quotes = evidence([entry["name"], *entry["aliases"]])
                last = max(mentioned) if mentioned else None
                absence = latest - last if last is not None else None
                characters.append({"id": entry["id"], "name": entry["name"], "mentioned_chapters": mentioned,
                                   "last_mentioned": last, "absence": absence,
                                   "status": "not_found" if last is None else (
                                       "attention" if absence >= entry["max_absence"] else "ok"), "evidence": quotes})
            threads = []
            for entry in settings["threads"]:
                mentioned, quotes = evidence(entry["terms"], entry["start_chapter"], entry["end_chapter"])
                last = max(mentioned) if mentioned else None
                span = max(0, min(latest, entry["end_chapter"] or latest) - (last or entry["start_chapter"]))
                threads.append({"id": entry["id"], "label": entry["label"], "start_chapter": entry["start_chapter"],
                                "end_chapter": entry["end_chapter"], "last_mentioned": last, "span": span,
                                "status": "planned" if latest < entry["start_chapter"] else (
                                    "attention" if span >= entry["remind_after"] else "ok"), "evidence": quotes})
            outline = []
            numbers = {chapter for _, chapter in chapters if chapter is not None}
            outline_bytes = 0
            for index, path in enumerate(tree_files(self.project.path("outlines/approved"))):
                if index >= 1000:
                    omitted.append({"path": "outlines/approved/", "reason": "catalog_limit"})
                    break
                if path.suffix != ".md":
                    continue
                relative = path.relative_to(self.project.root).as_posix()
                try:
                    if outline_bytes + path.stat().st_size > 8 * 1024 * 1024:
                        omitted.append({"path": relative, "reason": "outline_byte_limit"})
                        continue
                    raw = _read_raw(self.project, relative)
                except (OSError, VibeNovelError):
                    omitted.append({"path": relative, "reason": "unreadable_or_oversized_outline"})
                    continue
                if raw is None:
                    continue
                outline_bytes += len(raw)
                chapter = _chapter(relative)
                if chapter is None:
                    omitted.append({"path": relative, "reason": "outline_chapter_number_unknown"})
                outline.append({"path": relative, "hash": digest(raw), "chapter": chapter,
                                "has_chapter": chapter in numbers if chapter else None})
            counts = Counter()
            for doc in documents:
                for run in re.findall(r"[\u3400-\u9fff]+|[A-Za-z][A-Za-z0-9_]{1,30}", doc["content"]):
                    if re.match(r"[\u3400-\u9fff]", run):
                        for size in (2, 3):
                            counts.update(run[i:i + size] for i in range(len(run) - size + 1))
                    else:
                        counts[run.lower()] += 1
            return {"novel_id": self.project.novel_id, "canonical_revision": state["revision"],
                    "toolkit_hash": settings["hash"], "scanned": len(documents), "omitted": omitted,
                    "characters": characters, "threads": threads, "outline": outline,
                    "keywords": [{"term": term, "count": count} for term, count in counts.most_common(30)],
                    "limitations": ["仅统计正式章中的登记名称/词项字面出现；提及不等于人物在场、知情或伏笔回收。",
                                    "跨度为可识别章号之差；遗漏章节会影响结论，未找到不能证明不存在。",
                                    "大纲对照仅检查章号对应文件，不能证明内容遵循大纲。登记结束章也不表示已回收。",
                                    "高频双/三字组不是分词、人名识别或情节质量评分。"]}
