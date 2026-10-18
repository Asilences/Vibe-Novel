"""Bounded, shareable metadata diagnostics; no prose, prompts or error text.

The report is assembled from fixed scalar fields and known enumerations. It
never serializes arbitrary local JSON, a project title, or an exception message.
"""
from __future__ import annotations

from collections import Counter
import re

from vibenovel.errors import VibeNovelError
from .analysis import ALL_RULES, AnalysisStore
from .documents import DocumentStore
from .files import digest, read_json, safe_path, tree_files
from .project import Project
from .toolkit import WritingToolkitStore

MAX_CATALOG_FILES = 1000
MAX_CHAPTER_FILES = 80
MAX_CHAPTER_BYTES = 8 * 1024 * 1024
MAX_JOB_FILES = 200
MAX_REPORT_FILES = 100
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_METADATA_FILE_BYTES = 4 * 1024 * 1024
KINDS = ("expand_outline", "draft", "review", "rewrite", "build_blueprint", "unknown")
STATES = ("queued", "running", "paused", "awaiting_input", "succeeded", "failed", "cancelled", "interrupted", "unknown")
ISSUE_STATES = ("pending", "resolved", "ignored")


class WritingDiagnosticsStore:
    def __init__(self, project: Project):
        self.project = project
        self.storage = DocumentStore(project).storage

    def report(self) -> dict:
        omitted_counts = Counter()

        def omit(area, reason):
            omitted_counts[(area, reason)] += 1

        def catalog(directory, area):
            items = []
            try:
                for index, path in enumerate(tree_files(self.project.path(directory))):
                    if index >= MAX_CATALOG_FILES:
                        omit(area, "catalog_limit_at_least")
                        break
                    if path.name != ".gitkeep":
                        items.append(path)
            except (OSError, VibeNovelError):
                omit(area, "directory_unreadable_or_link_rejected")
            return items

        def metadata(directory, area, pattern, maximum):
            paths = []
            for path in catalog(directory, area):
                match = pattern.fullmatch(path.name)
                if path.parent != self.project.path(directory) or match is None:
                    omit(area, "unrecognized_filename")
                    continue
                try:
                    paths.append((path.stat().st_mtime_ns, path, match.group(1)))
                except OSError:
                    omit(area, "file_unreadable")
            total_bytes = 0
            for index, (_, path, identifier) in enumerate(sorted(paths, key=lambda item: (-item[0], item[1].name))):
                if index >= maximum:
                    omit(area, "file_limit")
                    continue
                try:
                    size = path.stat().st_size
                    if size > MAX_METADATA_FILE_BYTES or size + total_bytes > MAX_METADATA_BYTES:
                        omit(area, "byte_limit")
                        continue
                    total_bytes += size
                    yield identifier, read_json(path, max_bytes=MAX_METADATA_FILE_BYTES)
                except (OSError, VibeNovelError):
                    omit(area, "invalid_or_unreadable_json")

        with self.storage.locked() as state:
            totals = {"files": 0, "bytes": 0, "characters": 0, "non_whitespace_characters": 0, "lines": 0}
            chapters = []
            numbered = []
            for path in catalog("chapters", "chapters"):
                match = re.fullmatch(r"ch([0-9]{3,})\.md", path.name)
                if path.parent != self.project.path("chapters") or match is None or int(match[1]) < 1:
                    omit("chapters", "unrecognized_filename")
                else:
                    numbered.append((int(match[1]), path))
            for _, path in sorted(numbered, key=lambda item: (item[0], item[1].name)):
                if len(chapters) >= MAX_CHAPTER_FILES:
                    omit("chapters", "file_limit")
                    continue
                try:
                    size = path.stat().st_size
                    if size > 2 * 1024 * 1024 or size + totals["bytes"] > MAX_CHAPTER_BYTES:
                        omit("chapters", "byte_limit")
                        continue
                    with path.open("rb") as stream:
                        raw = stream.read(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024:
                        omit("chapters", "byte_limit")
                        continue
                    text = raw.decode("utf-8")
                except (OSError, UnicodeError):
                    omit("chapters", "invalid_or_unreadable_utf8")
                    continue
                item = {"path": path.relative_to(self.project.root).as_posix(), "hash": digest(raw),
                        "chapter": int(re.fullmatch(r"ch([0-9]{3,})\.md", path.name)[1]),
                        "bytes": len(raw), "characters": len(text),
                        "non_whitespace_characters": sum(not char.isspace() for char in text),
                        "lines": len(text.splitlines())}
                chapters.append(item)
                totals["files"] += 1
                for key in ("bytes", "characters", "non_whitespace_characters", "lines"):
                    totals[key] += item[key]

            by_kind = {kind: Counter({status: 0 for status in STATES}) for kind in KINDS}
            job_count, invalid_jobs = 0, 0
            for identifier, data in metadata(".runtime/jobs", "jobs", re.compile(r"([0-9a-f]{32})\.json"), MAX_JOB_FILES):
                if (type(data.get("schema_version")) is not int or data["schema_version"] != 1
                        or data.get("id") != identifier or data.get("novel_id") != self.project.novel_id):
                    invalid_jobs += 1
                    continue
                kind = data.get("kind") if data.get("kind") in KINDS else "unknown"
                status = data.get("state") if data.get("state") in STATES else "unknown"
                by_kind[kind][status] += 1
                job_count += 1

            report_count, stale_by_revision, invalid_reports = 0, 0, 0
            issues = Counter({status: 0 for status in ISSUE_STATES})
            rules = {rule: Counter(found=0, returned=0, truncated=0) for rule in ALL_RULES}
            for identifier, data in metadata("reviews", "analysis", re.compile(r"analysis-([0-9a-f]{32})\.json"), MAX_REPORT_FILES):
                if (type(data.get("schema_version")) is not int or data["schema_version"] not in (1, 2)
                        or data.get("id") != identifier or data.get("novel_id") != self.project.novel_id
                        or type(data.get("canonical_revision")) is not int or data["canonical_revision"] < 0
                        or not isinstance(data.get("issues"), list) or len(data["issues"]) > 100
                        or not isinstance(data.get("coverage"), dict)):
                    invalid_reports += 1
                    continue
                report_count += 1
                stale_by_revision += data["canonical_revision"] != state["revision"]
                for issue in data["issues"]:
                    if not isinstance(issue, dict):
                        continue
                    if issue.get("status") in ISSUE_STATES:
                        issues[issue["status"]] += 1
                counts = data["coverage"].get("rule_counts")
                if not isinstance(counts, dict):
                    omit("analysis", "legacy_report_without_rule_counts")
                    continue
                for rule in ALL_RULES:
                    values = counts.get(rule)
                    if not isinstance(values, dict):
                        continue
                    if any(type(values.get(key)) is not int or not 0 <= values[key] <= 200000000
                           for key in ("found", "returned", "truncated")):
                        omit("analysis", "invalid_rule_count")
                        continue
                    if values["found"] != values["returned"] + values["truncated"]:
                        omit("analysis", "inconsistent_rule_count")
                        continue
                    rules[rule].update({key: values[key] for key in ("found", "returned", "truncated")})

            configuration = {}
            for name, reader in (("baseline", AnalysisStore(self.project)._baseline),
                                 ("toolkit", WritingToolkitStore(self.project)._settings)):
                try:
                    value = reader()
                    configuration[name] = {"configured": value["configured"], "stale": value["stale"], "valid": True}
                except (OSError, VibeNovelError):
                    configuration[name] = {"configured": None, "stale": None, "valid": False}
                    omit("configuration", name + "_invalid_or_unreadable")
            try:
                from .semantic import MODEL_DIRECTORY
                model = safe_path(self.project.workspace, MODEL_DIRECTORY)
                configuration["semantic"] = {"installed": (
                    safe_path(model, "model_quantized.onnx").is_file() and safe_path(model, "tokenizer.json").is_file()),
                    "loaded": False, "check": "file_presence_only"}
            except (OSError, VibeNovelError):
                configuration["semantic"] = {"installed": False, "loaded": False, "check": "file_presence_only"}
                omit("configuration", "semantic_paths_invalid_or_unreadable")

            return {"schema_version": 1, "novel_id": self.project.novel_id, "canonical_revision": state["revision"],
                "chapters": {"items": chapters, "totals": totals},
                "jobs": {"scanned": job_count, "invalid_metadata": invalid_jobs,
                    "by_kind": [{"kind": kind, "states": dict(by_kind[kind]), "total": sum(by_kind[kind].values()),
                                 "failures": by_kind[kind]["failed"]} for kind in KINDS if sum(by_kind[kind].values())]},
                "analysis": {"scanned": report_count, "reports": report_count, "stale_by_revision": stale_by_revision,
                    "invalid_metadata": invalid_reports, "issues": dict(issues),
                    "by_rule": [{"rule": rule, **dict(values)} for rule, values in rules.items() if values["found"]]},
                "configuration": configuration,
                "omitted": [{"area": area, "reason": reason, "count": count,
                             "at_least": reason == "catalog_limit_at_least"}
                            for (area, reason), count in sorted(omitted_counts.items())],
                "limitations": [
                    "仅分享固定字段元数据，不包含正文、书名、任务说明、API错误文本、密钥或本机绝对路径。",
                    "失败汇总仅按已知任务类型和状态计数，未分析错误原因；failed以外的中断/取消单独计数。",
                    "章节只扫描有界正式正文，合计仅覆盖items列出的版本，不包含草稿，也不能代表文学质量。",
                    "stale_by_revision仅比较正式版本，不替代打开分析报告时对来源/样章/配置的完整失效核验。",
                    "分析计数来自有界本地报告元数据，旧报告可能缺少规则覆盖计数；不证明问题已解决。",
                    "每区最多枚举1000文件；章节80份/8MiB，任务200份/16MiB，分析100份/16MiB。",
                    "语义配置只检查文件存在，未加载模型、核验依赖或执行推理；真实可用性见语义检索状态。"]}
