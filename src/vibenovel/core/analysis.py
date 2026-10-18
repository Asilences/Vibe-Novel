"""Local, bounded editorial clues; never a literary or continuity verdict.

The author selects immutable sample snapshots. Reports bind every inspected
document to its exact bytes and remain separate from chapters and story memory.
This module uses only the Python standard library and makes no network calls.
"""
from __future__ import annotations

import copy
import bisect
import math
import re
from pathlib import PurePosixPath
from typing import Iterator, Optional
from uuid import uuid4

from vibenovel.errors import MissingDocumentError, VibeNovelError
from .documents import (HASH, MAX_DOCUMENT_BYTES, ConflictError, DocumentStore,
                        _Storage, _check_expected, _content_bytes, _decode,
                        _read_raw, _snapshot)
from .files import atomic_write, digest, json_bytes, read_json, tree_files
from .project import Project, utc_now
from .toolkit import TOOLKIT_PATH, WritingToolkitStore

BASELINE_PATH = ".runtime/style-baseline.json"
MAX_BASELINE_SAMPLES = 10
MAX_BASELINE_BYTES = 1024 * 1024
MAX_TARGET_CHARS = 200000
MAX_HISTORY_FILES = 80
MAX_CORPUS_BYTES = 8 * 1024 * 1024
MAX_ISSUES = 100
MAX_QUERY_CHARS = 200
MAX_CATALOG_FILES = 1000
REPORT_PATH = re.compile(r"reviews/analysis-([0-9a-f]{32})\.json\Z")
CANNED_PHRASES = ("他不知道的是", "她不知道的是", "命运的齿轮开始转动", "嘴角勾起一抹",
                  "眼中闪过一丝", "一股莫名的", "不由得倒吸一口凉气")
BASE_RULES = ("canned_phrase", "long_sentence", "literal_overlap")
STYLE_RULES = ("baseline_sentence_shift", "baseline_dialogue_shift", "baseline_simile_shift",
               "baseline_paragraph_shift", "baseline_rhythm_shift")
LEGACY_RULES = BASE_RULES + ("baseline_sentence_shift",)
TOOLKIT_RULES = ("lexicon_alias", "lexicon_forbidden", "lexicon_required", "style_avoid")
ALL_RULES = BASE_RULES + STYLE_RULES + TOOLKIT_RULES
LIMITATIONS = [
    "本地规则仅提供审稿线索，不判断文学质量、人物是否失忆、伏笔是否回收或剧情是否语义重复。",
    "统计依赖标点与词项；相似或重复文字可能是有意回响，需作者结合上下文判断。",
    "只检查 coverage 列出的文件和规则；未命中不等于没有问题，不修改任何正文或正式记忆。",
]


def _source_path(project: Project, path: str) -> None:
    project.path(path)
    if (path != PurePosixPath(path).as_posix() or not path.startswith(("chapters/", "drafts/"))
            or not path.endswith(".md")):
        raise VibeNovelError("分析和样章仅接受本作品 chapters/ 或 drafts/ 内规范的 .md 路径")


def _expected(value: str) -> None:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise VibeNovelError("来源 hash 需要完整 SHA256；请先读取文档")


def _current_hash(project: Project, path: str) -> Optional[str]:
    # Invalid or oversized replacements invalidate an old result instead of
    # preventing the author from opening that old result to see its status.
    try:
        raw = _read_raw(project, path)
    except (OSError, VibeNovelError):
        return None
    return digest(raw) if raw is not None else None


def _legacy_metrics(text: str) -> dict:
    sentences = [item.strip() for item in re.split(r"[。！？!?]+", text) if item.strip()]
    lengths = [len(re.sub(r"\s", "", sentence)) for sentence in sentences]
    total = max(1, len(text))
    return {"characters": len(text), "sentences": len(sentences), "paragraphs": sum(1 for _ in _paragraphs(text)),
            "mean_sentence_characters": round(sum(lengths) / max(1, len(lengths)), 2),
            "long_sentence_count": sum(length > 100 for length in lengths),
            "dialogue_marks_per_1000": round(sum(text.count(mark) for mark in "“”\"") / total * 1000, 2),
            "simile_terms_per_1000": round(sum(text.count(term) for term in ("仿佛", "似乎", "宛如"))
                                           / total * 1000, 2)}


def _metrics(text: str) -> dict:
    result = _legacy_metrics(text)
    lengths = [len(re.sub(r"\s", "", item)) for item in re.split(r"[。！？!?]+", text) if item.strip()]
    paragraphs = [len(re.sub(r"\s", "", item["quote"])) for item in _paragraphs(text)]
    mean = sum(lengths) / max(1, len(lengths))
    variance = sum((length - mean) ** 2 for length in lengths) / max(1, len(lengths))
    result.update(mean_paragraph_characters=round(sum(paragraphs) / max(1, len(paragraphs)), 2),
                  short_sentences_percent=round(sum(length <= 15 for length in lengths)
                                                / max(1, len(lengths)) * 100, 2),
                  sentence_length_variation=round(math.sqrt(variance) / max(1, mean), 3))
    return result


def _paragraphs(text: str) -> Iterator[dict]:
    """Retain physical line numbers, including CRLF and blank-line separators."""
    lines, start = [], None
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("#"):
            if start is None:
                start = number
            lines.append(line)
        else:
            if lines:
                yield {"start_line": start, "end_line": number - 1, "quote": "\n".join(lines)}
            lines, start = [], None
    if lines:
        yield {"start_line": start, "end_line": start + len(lines) - 1, "quote": "\n".join(lines)}


def _normal(text: str) -> str:
    return "".join(re.findall(r"[\w\u3400-\u9fff]", text.lower()))


def _terms(text: str) -> set[str]:
    """Chinese bigrams and Latin word terms; deliberately no semantic claim."""
    result = set()
    for run in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            result.update(run[index:index + 2] for index in range(len(run) - 1))
            if len(run) == 1:
                result.add(run)
        else:
            result.add(run)
    return result


def _validate_report(data: dict) -> None:
    """Treat editable review JSON as input before exposing it to the Studio UI."""
    def require(condition):
        if not condition:
            raise VibeNovelError("分析报告字段损坏或格式无效，请重新分析当前文稿")

    def fields(value, names, optional=()):
        require(isinstance(value, dict) and set(names) <= set(value)
                and not set(value) - set(names) - set(optional))

    def text(value, maximum=4000):
        require(isinstance(value, str) and len(value) <= maximum)

    def texts(value, maximum=100):
        require(isinstance(value, list) and len(value) <= maximum)
        for item in value:
            text(item)

    def number(value, maximum):
        require(type(value) is int and 0 <= value <= maximum)

    def hash_value(value, nullable=False):
        require((nullable and value is None) or (isinstance(value, str) and bool(HASH.fullmatch(value))))

    def source(value, *, sample=False, related=False):
        names = {"path", "hash"}
        if sample:
            names.update(("current_hash", "stale"))
        if related:
            names.update(("start_line", "end_line", "quote"))
        fields(value, names)
        text(value["path"], 1000)
        require(value["path"].startswith(("chapters/", "drafts/")) and value["path"].endswith(".md"))
        hash_value(value["hash"])
        if sample:
            hash_value(value["current_hash"], nullable=True)
            require(type(value["stale"]) is bool)
        if related:
            number(value["start_line"], MAX_DOCUMENT_BYTES)
            number(value["end_line"], MAX_DOCUMENT_BYTES)
            require(1 <= value["start_line"] <= value["end_line"])
            text(value["quote"], 2000)

    def metrics(value, *, legacy=False, allow_legacy=False):
        # Baseline snapshots created before v2 retain their exact seven-field
        # schema. Do not silently rewrite them on read.
        require(isinstance(value, dict))
        allowed = [set(_legacy_metrics(""))] if legacy else [set(_metrics(""))]
        if allow_legacy and not legacy:
            allowed.append(set(_legacy_metrics("")))
        require(set(value) in allowed)
        for metric in value.values():
            require(type(metric) in (int, float)
                    and 0 <= metric <= MAX_BASELINE_BYTES + MAX_BASELINE_SAMPLES * 2
                    and math.isfinite(metric))

    fields(data, {"schema_version", "id", "novel_id", "created_at", "canonical_revision", "source",
                  "dependencies", "metrics", "baseline", "issues", "coverage", "limitations"},
           {"runtime_dependencies"})
    require(type(data["schema_version"]) is int and data["schema_version"] in (1, 2))
    legacy = data["schema_version"] == 1
    if legacy:
        require("runtime_dependencies" not in data)
    else:
        require("runtime_dependencies" in data)
        fields(data["runtime_dependencies"], {BASELINE_PATH, TOOLKIT_PATH})
        for value in data["runtime_dependencies"].values():
            hash_value(value, nullable=True)
    text(data["id"], 32)
    text(data["novel_id"], 100)
    text(data["created_at"], 100)
    number(data["canonical_revision"], 2 ** 63 - 1)
    source(data["source"])
    require(isinstance(data["dependencies"], dict) and len(data["dependencies"]) <= 100)
    for path, value in data["dependencies"].items():
        source({"path": path, "hash": value})
    metrics(data["metrics"], legacy=legacy)
    texts(data["limitations"])

    baseline = data["baseline"]
    fields(baseline, {"novel_id", "configured", "path", "hash", "config_hash", "samples", "metrics",
                      "stale", "limitations", "comparison_available"},
           {"created_at"} if legacy else {"created_at", "comparison"})
    require(baseline["novel_id"] == data["novel_id"] and baseline["path"] == BASELINE_PATH)
    for key in ("configured", "stale", "comparison_available"):
        require(type(baseline[key]) is bool)
    for key in ("hash", "config_hash"):
        hash_value(baseline[key], nullable=not baseline["configured"])
    require(isinstance(baseline["samples"], list) and len(baseline["samples"]) <= MAX_BASELINE_SAMPLES)
    require(bool(baseline["samples"]) == baseline["configured"])
    for sample in baseline["samples"]:
        source(sample, sample=True)
    require(len({sample["path"] for sample in baseline["samples"]}) == len(baseline["samples"]))
    if baseline["configured"]:
        metrics(baseline["metrics"], legacy=legacy, allow_legacy=True)
    else:
        require(baseline["metrics"] is None)
    texts(baseline["limitations"])
    if "created_at" in baseline:
        text(baseline["created_at"], 100)

    if not legacy:
        require("comparison" in baseline)
        require(data["runtime_dependencies"][BASELINE_PATH] == baseline["hash"])
        comparison = baseline["comparison"]
        fields(comparison, {"excluded_paths", "samples", "metrics", "reason"})
        texts(comparison["excluded_paths"], MAX_BASELINE_SAMPLES)
        require(len(set(comparison["excluded_paths"])) == len(comparison["excluded_paths"]))
        require(all(path == data["source"]["path"] for path in comparison["excluded_paths"]))
        require(isinstance(comparison["samples"], list)
                and len(comparison["samples"]) <= MAX_BASELINE_SAMPLES)
        manifest = {sample["path"]: sample["hash"] for sample in baseline["samples"]}
        selected = set()
        for sample in comparison["samples"]:
            source(sample)
            require(sample["path"] not in selected and manifest.get(sample["path"]) == sample["hash"]
                    and sample["path"] not in comparison["excluded_paths"])
            selected.add(sample["path"])
        require(selected == set(manifest) - set(comparison["excluded_paths"]))
        if comparison["metrics"] is not None:
            metrics(comparison["metrics"])
        require((comparison["metrics"] is not None) == baseline["comparison_available"])
        text(comparison["reason"])

    rules = set(LEGACY_RULES if legacy else ALL_RULES)
    coverage = data["coverage"]
    fields(coverage, {"rules", "skipped", "source_characters", "history_scanned", "history_sources", "omitted",
                      "issue_limit", "issue_limit_reached", "baseline_compared"},
           () if legacy else {"rule_counts"})
    texts(coverage["rules"], len(rules))
    require(all(rule in rules for rule in coverage["rules"]))
    require(len(set(coverage["rules"])) == len(coverage["rules"]))
    require(isinstance(coverage["skipped"], list) and len(coverage["skipped"]) <= len(rules))
    for skipped in coverage["skipped"]:
        fields(skipped, {"rule", "reason"})
        text(skipped["rule"])
        require(skipped["rule"] in rules)
        text(skipped["reason"])
    skipped_rules = [item["rule"] for item in coverage["skipped"]]
    require(len(set(skipped_rules)) == len(skipped_rules)
            and not set(skipped_rules) & set(coverage["rules"])
            and set(skipped_rules) | set(coverage["rules"]) == rules)
    number(coverage["source_characters"], MAX_TARGET_CHARS)
    number(coverage["history_scanned"], MAX_HISTORY_FILES)
    require(isinstance(coverage["history_sources"], list)
            and len(coverage["history_sources"]) == coverage["history_scanned"])
    for historic in coverage["history_sources"]:
        source(historic)
        require(data["dependencies"].get(historic["path"]) == historic["hash"])
    require(isinstance(coverage["omitted"], list) and len(coverage["omitted"]) <= MAX_CATALOG_FILES + 1)
    for omitted in coverage["omitted"]:
        fields(omitted, {"path", "reason"})
        text(omitted["path"], 1000)
        text(omitted["reason"])
    require(type(coverage["issue_limit"]) is int and coverage["issue_limit"] == MAX_ISSUES)
    require(type(coverage["issue_limit_reached"]) is bool and type(coverage["baseline_compared"]) is bool)
    if not legacy:
        require("rule_counts" in coverage)
        fields(coverage["rule_counts"], rules)
        for rule, counts in coverage["rule_counts"].items():
            fields(counts, {"found", "returned", "truncated"})
            for value in counts.values():
                number(value, MAX_TARGET_CHARS * 1000)
            require(counts["found"] == counts["returned"] + counts["truncated"])

    require(isinstance(data["issues"], list) and len(data["issues"]) <= MAX_ISSUES)
    identifiers = set()
    for issue in data["issues"]:
        fields(issue, {"id", "rule", "severity", "start_line", "end_line", "quote", "message", "suggestion",
                       "status"}, {"related"})
        for key in ("id", "rule", "severity", "message", "suggestion", "status"):
            text(issue[key])
        require(bool(issue["id"]) and issue["id"] not in identifiers and issue["rule"] in rules
                and issue["severity"] in ("info", "warning")
                and issue["status"] in ("pending", "resolved", "ignored"))
        identifiers.add(issue["id"])
        number(issue["start_line"], MAX_DOCUMENT_BYTES)
        number(issue["end_line"], MAX_DOCUMENT_BYTES)
        require(1 <= issue["start_line"] <= issue["end_line"])
        text(issue["quote"], 2000)
        if "related" in issue:
            source(issue["related"], related=True)
            require(data["dependencies"].get(issue["related"]["path"]) == issue["related"]["hash"])
    if not legacy:
        for rule, counts in coverage["rule_counts"].items():
            require(counts["returned"] == sum(issue["rule"] == rule for issue in data["issues"]))
            if rule not in coverage["rules"]:
                require(counts["found"] == 0)
        require(coverage["issue_limit_reached"] == any(counts["truncated"]
                                                     for counts in coverage["rule_counts"].values()))


class AnalysisStore:
    def __init__(self, project: Project):
        self.project = project
        self.storage = _Storage(project)
        self.documents = DocumentStore(project)

    def _baseline(self) -> dict:
        result = {"novel_id": self.project.novel_id, "configured": False, "path": BASELINE_PATH,
                  "hash": None, "config_hash": None, "samples": [], "metrics": None, "stale": False,
                  "limitations": ["样章由作者显式选择；统计是风格参照，不是质量分数。",
                                  "样章变化后保留原快照并标记失效，不自动改用全书或新版本。"]}
        path = self.project.path(BASELINE_PATH)
        if not path.exists():
            return result
        data = read_json(path, max_bytes=MAX_DOCUMENT_BYTES)
        if (set(data) != {"schema_version", "novel_id", "created_at", "samples", "config_hash", "metrics"}
                or type(data["schema_version"]) is not int or data["schema_version"] not in (1, 2)
                or data["novel_id"] != self.project.novel_id or not isinstance(data["samples"], list)
                or not 1 <= len(data["samples"]) <= MAX_BASELINE_SAMPLES):
            raise VibeNovelError("样章基线格式或所属作品无效，请重新显式选择样章")
        manifest, texts, size, seen = [], [], 0, set()
        for sample in data["samples"]:
            if not isinstance(sample, dict) or set(sample) != {"path", "hash", "content"}:
                raise VibeNovelError("样章快照字段无效")
            _source_path(self.project, sample["path"])
            _expected(sample["hash"])
            if sample["path"] in seen or not isinstance(sample["content"], str):
                raise VibeNovelError("样章快照重复或内容无效")
            seen.add(sample["path"])
            encoded = sample["content"].encode("utf-8")
            size += len(encoded)
            if size > MAX_BASELINE_BYTES or digest(encoded) != sample["hash"]:
                raise VibeNovelError("样章快照超过限额或 hash 不符")
            current_hash = _current_hash(self.project, sample["path"])
            result["samples"].append({"path": sample["path"], "hash": sample["hash"],
                                      "current_hash": current_hash, "stale": current_hash != sample["hash"]})
            manifest.append({"path": sample["path"], "hash": sample["hash"]})
            texts.append(sample["content"])
        calculate = _legacy_metrics if data["schema_version"] == 1 else _metrics
        if data["config_hash"] != digest(json_bytes(manifest)) or data["metrics"] != calculate("\n\n".join(texts)):
            raise VibeNovelError("样章配置校验失败，请重新显式选择样章")
        result.update(configured=True, hash=digest(path.read_bytes()), config_hash=data["config_hash"],
                      metrics=data["metrics"], stale=any(sample["stale"] for sample in result["samples"]),
                      created_at=data["created_at"])
        return result

    def _comparison(self, baseline: dict, target: str) -> dict:
        """Use only the remaining author-selected snapshots, never a fallback."""
        excluded = [sample["path"] for sample in baseline["samples"] if sample["path"] == target]
        remaining = [sample for sample in baseline["samples"] if sample["path"] != target]
        result = {"excluded_paths": excluded,
                  "samples": [{"path": item["path"], "hash": item["hash"]} for item in remaining],
                  "metrics": None, "reason": "baseline_missing"}
        if not baseline["configured"]:
            return result
        if not remaining:
            result["reason"] = "baseline_self_reference_only"
            return result
        if any(sample["stale"] for sample in remaining):
            result["reason"] = "baseline_remaining_sample_stale"
            return result
        data = read_json(self.project.path(BASELINE_PATH), max_bytes=MAX_DOCUMENT_BYTES)
        # _baseline has validated all immutable snapshots under the same lock.
        contents = [sample["content"] for sample in data["samples"] if sample["path"] != target]
        result.update(metrics=_metrics("\n\n".join(contents)), reason="fixed_snapshots_excluding_target")
        return result

    def baseline(self) -> dict:
        with self.storage.locked():
            return self._baseline()

    def set_baseline(self, samples: list[dict], expected_hash: Optional[str] = None) -> dict:
        if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_BASELINE_SAMPLES:
            raise VibeNovelError(f"请显式选择 1–{MAX_BASELINE_SAMPLES} 个作者认可样章")
        with self.storage.locked():
            path = self.project.path(BASELINE_PATH)
            existing = path.read_bytes() if path.exists() else None
            _check_expected(existing, expected_hash)
            snapshots, size, seen = [], 0, set()
            for sample in samples:
                if not isinstance(sample, dict) or set(sample) != {"path", "hash"}:
                    raise VibeNovelError("样章需要 path/hash 对象")
                _source_path(self.project, sample["path"])
                _expected(sample["hash"])
                if sample["path"] in seen:
                    raise VibeNovelError("不能重复选择同一样章")
                seen.add(sample["path"])
                raw = _read_raw(self.project, sample["path"])
                _check_expected(raw, sample["hash"])
                if not _decode(raw).strip():
                    raise VibeNovelError("样章不能为空文本")
                size += len(raw)
                if size > MAX_BASELINE_BYTES:
                    raise VibeNovelError("所选样章合计超过 1 MiB，请减少样章")
                snapshots.append({**sample, "content": _decode(raw)})
            manifest = [{"path": sample["path"], "hash": sample["hash"]} for sample in snapshots]
            data = {"schema_version": 2, "novel_id": self.project.novel_id, "created_at": utc_now(),
                    "samples": snapshots, "config_hash": digest(json_bytes(manifest)),
                    "metrics": _metrics("\n\n".join(sample["content"] for sample in snapshots))}
            encoded = json_bytes(data)
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise VibeNovelError("样章快照 JSON 超过限额，请减少样章或控制字符")
            atomic_write(path, encoded, overwrite=existing is not None)
            return self._baseline()

    def _history(self, *, exclude: Optional[str] = None) -> tuple[list[dict], list[dict]]:
        documents, omitted, total = [], [], 0
        for index, path in enumerate(tree_files(self.project.path("chapters"))):
            if index >= MAX_CATALOG_FILES:
                omitted.append({"path": "chapters/", "reason": "catalog_limit_remaining_not_enumerated"})
                break
            relative = path.relative_to(self.project.root).as_posix()
            if path.suffix != ".md" or relative == exclude:
                continue
            if len(documents) >= MAX_HISTORY_FILES:
                omitted.append({"path": relative, "reason": "file_limit"})
                continue
            size = path.stat().st_size
            if size > MAX_DOCUMENT_BYTES or total + size > MAX_CORPUS_BYTES:
                omitted.append({"path": relative, "reason": "byte_limit"})
                continue
            try:
                raw = _read_raw(self.project, relative)
                if raw is None:
                    omitted.append({"path": relative, "reason": "missing"})
                    continue
                content = _decode(raw)
            except (OSError, VibeNovelError):
                omitted.append({"path": relative, "reason": "unreadable_or_invalid_utf8"})
                continue
            total += len(raw)
            documents.append({"path": relative, "hash": digest(raw), "content": content})
        return documents, omitted

    def analyze(self, path: str, expected_hash: str) -> dict:
        _source_path(self.project, path)
        _expected(expected_hash)
        with self.storage.locked() as state:
            source = self.documents._document(path, state)
            _check_expected(source["content"].encode("utf-8"), expected_hash)
            if len(source["content"]) > MAX_TARGET_CHARS:
                raise VibeNovelError(f"单次分析最多 {MAX_TARGET_CHARS} 字符，请拆分文档")
            baseline = self._baseline()
            baseline["comparison"] = self._comparison(baseline, path)
            baseline["comparison_available"] = baseline["comparison"]["metrics"] is not None
            toolkit = WritingToolkitStore(self.project)
            toolkit_settings = toolkit._settings()
            history, omitted = self._history(exclude=path)
            revision = state["revision"]
        text = source["content"]
        candidates = []
        found_counts = {rule: 0 for rule in ALL_RULES}
        sequence = 0

        def add(rule, line, quote, message, suggestion, *, severity="info", end=None, **extra):
            nonlocal sequence
            found_counts[rule] += 1
            sequence += 1
            issue = {"rule": rule, "severity": severity, "start_line": line, "end_line": end or line,
                     "quote": quote[:2000], "message": message, "suggestion": suggestion,
                     "status": "pending", **extra}
            # Keep a bounded, sorted candidate pool. Later warnings displace
            # early informational clues, rather than disappearing at the cap.
            bisect.insort(candidates, ((0 if severity == "warning" else 1, line, sequence), issue))
            if len(candidates) > MAX_ISSUES:
                candidates.pop()

        for number, line in enumerate(text.splitlines(), 1):
            for phrase in CANNED_PHRASES:
                if phrase in line:
                    offset = max(0, line.index(phrase) - 200)
                    add("canned_phrase", number, line[offset:offset + 2000], f"命中常见套话「{phrase}」；可能是有意使用。",
                        "结合视角与语境确认；可尝试用具体动作、感知或代价替代表述。")
            if any(len(re.sub(r"\s", "", sentence)) > 100 for sentence in re.split(r"[。！？!?]", line)):
                add("long_sentence", number, line, "检测到超过 100 字符的句段；仅按句末标点估计。",
                    "检查句中信息层次与阅读节奏，必要时拆句；保留有意的长句。")
        paragraphs = _paragraphs(text)
        seen = {}
        historic = {}
        for document in history:
            for paragraph in _paragraphs(document["content"]):
                normalized = _normal(paragraph["quote"])
                if 40 <= len(normalized) <= 2000:
                    historic.setdefault(normalized, {"path": document["path"], "hash": document["hash"],
                                                     **paragraph, "quote": paragraph["quote"][:2000]})
        for paragraph in paragraphs:
            normalized = _normal(paragraph["quote"])
            if not 40 <= len(normalized) <= 2000:
                continue
            match = seen.get(normalized) or historic.get(normalized)
            if match:
                add("literal_overlap", paragraph["start_line"], paragraph["quote"],
                    "发现去除标点/空白后的相同段落；这是字面重合线索，不能判定剧情重复。",
                    "核对是否为引用、前情提示或有新信息的回响，再决定保留、缩短或改写。",
                    severity="warning", end=paragraph["end_line"], related=match)
            seen.setdefault(normalized, {"path": path, "hash": expected_hash,
                                         **paragraph, "quote": paragraph["quote"][:2000]})
        metrics = _metrics(text)
        toolkit_audit = toolkit.audit_with_coverage(text, settings=toolkit_settings)
        for item in toolkit_audit["issues"]:
            add(item["rule"], item["start_line"], item["quote"], item["message"], item["suggestion"],
                severity=item["severity"], end=item["end_line"])
        for rule, counts in toolkit_audit["rule_counts"].items():
            found_counts[rule] = counts["found"]
        checked_rules = list(BASE_RULES + TOOLKIT_RULES)
        skipped = []
        if baseline["comparison_available"]:
            reference = baseline["comparison"]["metrics"]
            first_paragraph = next(_paragraphs(text), None)
            first = first_paragraph["start_line"] if first_paragraph else 1
            quote = first_paragraph["quote"] if first_paragraph else ""
            descriptions = [
                ("baseline_sentence_shift", "mean_sentence_characters", "平均句段字符数（按句末标点切分）", 0,
                 metrics["sentences"] >= 5 and reference["sentences"] >= 5),
                ("baseline_dialogue_shift", "dialogue_marks_per_1000", "每千字符引号数（不是实际对话占比）", 8,
                 metrics["characters"] >= 100 and reference["characters"] >= 100),
                ("baseline_simile_shift", "simile_terms_per_1000", "每千字符仿佛/似乎/宛如词项数", 3,
                 metrics["characters"] >= 100 and reference["characters"] >= 100),
                ("baseline_paragraph_shift", "mean_paragraph_characters", "平均段落字符数（空行分段）", 0,
                 metrics["paragraphs"] >= 3 and reference["paragraphs"] >= 3),
                ("baseline_rhythm_shift", "sentence_length_variation", "句长变异系数（标准差/均值）", .35,
                 metrics["sentences"] >= 5 and reference["sentences"] >= 5),
            ]
            for rule, key, label, minimum, eligible in descriptions:
                if not eligible:
                    skipped.append({"rule": rule, "reason": "insufficient_text_for_statistical_comparison"})
                    continue
                checked_rules.append(rule)
                actual, expected = metrics[key], reference[key]
                shifted = (actual > expected * 1.8 or actual < expected * .55) if expected else actual > 0
                if shifted and abs(actual - expected) > minimum:
                    add(rule, first, quote,
                        f"本章{label}为 {actual}，余下固定样章为 {expected}；统计差异不代表文风错误或质量高低。",
                        "结合场景、对话比例与叙事目的决定是否调整；无需机械对齐作者样章。")
        else:
            skipped = [{"rule": rule, "reason": baseline["comparison"]["reason"]} for rule in STYLE_RULES]
        issues = [{"id": f"issue-{index:03d}", **candidate[1]}
                  for index, candidate in enumerate(candidates, 1)]
        rule_counts = {rule: {"found": found, "returned": sum(issue["rule"] == rule for issue in issues),
                              "truncated": found - sum(issue["rule"] == rule for issue in issues)}
                       for rule, found in found_counts.items()}
        report_id = uuid4().hex
        report_path = f"reviews/analysis-{report_id}.json"
        dependencies = {document["path"]: document["hash"] for document in history}
        dependencies[path] = expected_hash
        if baseline["comparison_available"]:
            dependencies.update({sample["path"]: sample["hash"] for sample in baseline["comparison"]["samples"]})
        data = {"schema_version": 2, "id": report_id, "novel_id": self.project.novel_id,
                "created_at": utc_now(), "canonical_revision": revision,
                "source": {"path": path, "hash": expected_hash}, "dependencies": dependencies,
                "runtime_dependencies": {BASELINE_PATH: baseline["hash"], TOOLKIT_PATH: toolkit_settings["hash"]},
                "metrics": metrics, "baseline": baseline, "issues": issues,
                "coverage": {"rules": checked_rules, "skipped": skipped, "rule_counts": rule_counts,
                             "source_characters": len(text), "history_scanned": len(history),
                             "history_sources": [{"path": item["path"], "hash": item["hash"]} for item in history],
                             "omitted": omitted, "issue_limit": MAX_ISSUES,
                             "issue_limit_reached": any(counts["truncated"] for counts in rule_counts.values()),
                             "baseline_compared": baseline["comparison_available"]},
                "limitations": LIMITATIONS}
        self.documents.save(report_path, json_bytes(data).decode("utf-8"), None, "AI_SUGGESTED",
                            expected_sources=dependencies, expected_revision=revision,
                            expected_runtime_sources=data["runtime_dependencies"])
        return self.report(report_path)

    def _report(self, path: str, state: dict) -> dict:
        if not isinstance(path, str) or not REPORT_PATH.fullmatch(path):
            raise VibeNovelError("只接受 reviews/analysis-<id>.json 分析报告")
        document = self.documents._document(path, state)
        data = read_json(self.project.path(path), max_bytes=MAX_DOCUMENT_BYTES)
        _validate_report(data)
        if (data["novel_id"] != self.project.novel_id
                or data["id"] != REPORT_PATH.fullmatch(path).group(1)):
            raise VibeNovelError("分析报告格式或所属作品无效")
        _source_path(self.project, data["source"]["path"])
        _expected(data["source"]["hash"])
        if data["dependencies"].get(data["source"]["path"]) != data["source"]["hash"]:
            raise VibeNovelError("分析报告未绑定目标来源")
        reasons, current_hash = [], None
        for source, expected in data["dependencies"].items():
            _source_path(self.project, source)
            _expected(expected)
            actual = _current_hash(self.project, source)
            if source == data["source"]["path"]:
                current_hash = actual
            if actual != expected:
                reasons.append(f"来源已变化或缺失: {source}")
        if data["canonical_revision"] != state["revision"]:
            reasons.append("正式作品版本已变化；历史检查覆盖范围需重新确认")
        current_baseline = self._baseline()
        for runtime_path, expected in data.get("runtime_dependencies", {}).items():
            if _current_hash(self.project, runtime_path) != expected:
                reasons.append(f"写作分析配置已变化: {runtime_path}")
        if data["baseline"].get("hash") != current_baseline["hash"]:
            reasons.append("作者样章配置已变化")
        compared_paths = {sample["path"] for sample in data["baseline"].get("comparison", {}).get("samples", [])}
        baseline_stale = (any(sample["stale"] for sample in current_baseline["samples"]
                              if sample["path"] in compared_paths)
                          if data["schema_version"] == 2 else current_baseline["stale"])
        if baseline_stale:
            reasons.append("固定样章的来源已变化或缺失，请重新选择后复查")
        data.update(path=path, hash=document["hash"], stale=bool(reasons), stale_reasons=reasons)
        data["source"]["current_hash"] = current_hash
        data["baseline"]["current_hash"] = current_baseline["hash"]
        return data

    def report(self, path: str) -> dict:
        with self.storage.locked() as state:
            return self._report(path, state)

    def verify(self, report_path: str, report_hash: str) -> dict:
        """Rerun rules against current bytes without declaring prose repaired.

        A new version-bound report is saved, while the prior report (including
        manual author marks) remains immutable. Not finding evidence is never
        translated into an automatic ``resolved`` status.
        """
        _expected(report_hash)
        with self.storage.locked() as state:
            previous = self._report(report_path, state)
            if previous["hash"] != report_hash:
                raise ConflictError("分析报告已变化，请刷新后再复测")
            source_hash = _current_hash(self.project, previous["source"]["path"])
            if source_hash:
                try:
                    current = self.documents._document(previous["source"]["path"], state)
                    if len(current["content"]) > MAX_TARGET_CHARS:
                        source_hash = None
                except (OSError, VibeNovelError):
                    source_hash = None
        report = self.analyze(previous["source"]["path"], source_hash) if source_hash else None
        # A concurrent author mark invalidates the requested old report version.
        with self.storage.locked() as state:
            if self.documents._document(report_path, state)["hash"] != report_hash:
                raise ConflictError("复测期间旧报告已变化，请刷新后重试")
            if report:
                report = self._report(report["path"], state)
        results = []
        for issue in previous["issues"]:
            rule = issue["rule"]
            matches = []
            status, reason = "unchecked", "当前来源缺失或不可读取，未执行复测。"
            if report:
                checked = rule in previous["coverage"]["rules"] and rule in report["coverage"]["rules"]
                counts = report["coverage"].get("rule_counts", {}).get(rule)
                for candidate in report["issues"]:
                    if candidate["rule"] != rule:
                        continue
                    if rule.startswith("baseline_") or (rule == "lexicon_required"
                                                          and candidate["message"] == issue["message"]):
                        # These are document-level statistics, not a claim
                        # about the first paragraph used as a display anchor.
                        matches.append(candidate["id"])
                    elif (_normal(candidate["quote"]) == _normal(issue["quote"])
                          and (rule not in ("canned_phrase", *TOOLKIT_RULES)
                               or candidate["message"] == issue["message"])):
                        old_related, new_related = issue.get("related"), candidate.get("related")
                        if (not old_related or (new_related and old_related["path"] == new_related["path"]
                                               and _normal(old_related["quote"]) == _normal(new_related["quote"]))):
                            matches.append(candidate["id"])
                if report["stale"]:
                    reason = "复测报告来源已变化，请对最新版本再次检查。"
                elif not checked:
                    reason = "该规则未在前后两个报告中完整执行；可能缺少有效样章或文本量不足。"
                elif (rule.startswith("baseline_")
                      and previous["baseline"]["hash"] != report["baseline"]["hash"]):
                    reason = "作者样章配置已变化，前后统计参照不同，不能验证旧结论。"
                elif (rule in TOOLKIT_RULES
                      and previous.get("runtime_dependencies", {}).get(TOOLKIT_PATH)
                      != report["runtime_dependencies"][TOOLKIT_PATH]):
                    reason = "作者词表或风格约束已变化，前后规则配置不同，不能验证旧结论。"
                elif matches:
                    status, reason = "remaining", "当前版本仍命中同规则证据；行号以新报告为准，需作者判断。"
                elif counts is None or counts["truncated"]:
                    reason = "本规则结果发生截断或旧格式缺少覆盖计数，无法确认旧证据是否仍命中。"
                elif rule == "literal_overlap" and report["coverage"]["omitted"]:
                    reason = "历史检查存在遗漏，未命中不能排除旧证据仍在未检查范围。"
                elif (rule == "literal_overlap" and issue.get("related")
                      and issue["related"]["path"] != previous["source"]["path"]
                      and report["dependencies"].get(issue["related"]["path"]) != issue["related"]["hash"]):
                    reason = "对应历史来源已变化或未进入复测范围，旧证据无法按原版本复核。"
                else:
                    status, reason = "not_found", "新报告未命中这条旧证据；这不表示修改正确或问题已解决。"
            results.append({"issue_id": issue["id"], "rule": rule, "previous_status": issue["status"],
                            "status": status, "matched_issue_ids": matches, "reason": reason})
        return {"novel_id": self.project.novel_id, "created_at": utc_now(),
                "previous": {"path": previous["path"], "hash": previous["hash"], "source": previous["source"]},
                "report": report, "results": results,
                "coverage": {"previous": previous["coverage"], "current": report["coverage"] if report else None},
                "limitations": ["复测只重新运行列明的本地规则，不是文学质量或剧情事实验收。",
                                "not_found 表示本次未命中旧证据；unchecked 表示检查域不足或版本条件不一致。",
                                "旧报告的 resolved/ignored 仍是作者标记，不会因复测自动改变。"]}

    def mark(self, report_path: str, report_hash: str, issue_id: str, status: str) -> dict:
        if status not in ("pending", "resolved", "ignored"):
            raise VibeNovelError("问题状态仅支持 pending / resolved / ignored")
        _expected(report_hash)
        with self.storage.locked() as state:
            result = self._report(report_path, state)
            if result["hash"] != report_hash:
                raise ConflictError("分析报告已变化，请刷新后再标记")
            if status == "resolved" and result["stale"]:
                raise ConflictError("旧分析报告已失效，不能标为已解决；请对当前文稿重新分析")
            issue = next((item for item in result["issues"] if item["id"] == issue_id), None)
            if issue is None:
                raise MissingDocumentError("未找到该分析问题")
            issue["status"] = status
            # The same transaction machinery as DocumentStore.save is used
            # inside this one lock so baseline/source drift cannot slip between
            # the stale check and status publication. No nested lock is taken.
            data = read_json(self.project.path(report_path), max_bytes=MAX_DOCUMENT_BYTES)
            data["issues"] = result["issues"]
            before = _read_raw(self.project, report_path)
            raw = _content_bytes(report_path, json_bytes(data).decode("utf-8"))
            after = copy.deepcopy(state)
            if (not after["history"].get(report_path)
                    or after["history"][report_path][-1]["hash"] != digest(before)):
                _snapshot(after, report_path, before, "EXTERNAL")
            _snapshot(after, report_path, raw, "AUTHOR")
            self.storage.transact(state, after, report_path, raw, before)
            return self._report(report_path, after)

    def search(self, query: str, limit: int = 10) -> dict:
        if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS
                or any(ord(char) < 32 for char in query)):
            raise VibeNovelError(f"检索词需要 1–{MAX_QUERY_CHARS} 个字符且不含控制字符")
        try:
            query.encode("utf-8")
        except UnicodeError as exc:
            raise VibeNovelError("检索词包含无效 Unicode") from exc
        if type(limit) is not int or not 1 <= limit <= 30:
            raise VibeNovelError("检索条数需要 1–30 的整数")
        terms = _terms(query)
        if not terms:
            raise VibeNovelError("检索词需要中文文字、字母或数字")
        with self.storage.locked() as state:
            history, omitted = self._history()
            revision = state["revision"]
        results, matched = [], 0
        def sort_key(item):
            return (-item["score"], item["path"], item["start_line"], item["quote"])
        for document in history:
            for paragraph in _paragraphs(document["content"]):
                # Bound both work and returned evidence without obscuring
                # physical line locations; long paragraphs are split by line.
                for offset, line in enumerate(paragraph["quote"].splitlines()):
                    if not line.strip():
                        continue
                    # Fixed character windows handle long unbroken Chinese
                    # paragraphs without building unbounded n-gram sets.
                    for start in range(0, len(line), 800):
                        quote = line[start:start + 1000]
                        overlap = terms & _terms(quote)
                        if not overlap:
                            continue
                        score = len(overlap) / len(terms)
                        if query.strip().lower() in quote.lower():
                            score += 1
                        number = paragraph["start_line"] + offset
                        matched += 1
                        results.append({"path": document["path"], "hash": document["hash"],
                                        "start_line": number, "end_line": number, "quote": quote,
                                        "score": round(score, 4)})
                        if len(results) > limit * 2:
                            results.sort(key=sort_key)
                            del results[limit:]
        results.sort(key=sort_key)
        return {"novel_id": self.project.novel_id, "query": query, "mode": "literal_ngram",
                "results": results[:limit], "scanned": len(history), "omitted": omitted,
                "matched": matched, "canonical_revision": revision,
                "limitations": ["仅在本作品已定稿 chapters/ 中进行中文二元字组/词项匹配，不是语义检索。",
                                "分数只表示字词重合，不表示事实成立、事件相同或情节重复。",
                                f"单次最多扫描 {MAX_HISTORY_FILES} 个文件、{MAX_CORPUS_BYTES // (1024 * 1024)} MiB；超限或不可读文件列入 omitted。"]}
