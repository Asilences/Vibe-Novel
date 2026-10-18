"""Bounded retrospective checks of an explicitly prepared revision manuscript."""
from __future__ import annotations

from .analysis import AnalysisStore, MAX_CORPUS_BYTES, MAX_HISTORY_FILES
from .documents import ConflictError, DocumentStore
from .project import utc_now
from vibenovel.errors import VibeNovelError


MAX_BATCH_TARGETS = 20
MAX_BATCH_CHARACTERS = 500000


class ManuscriptAuditStore:
    def __init__(self, project):
        self.project = project
        self.documents = DocumentStore(project)

    def analyze(self, manuscript_id, section_ids=None):
        from .manuscripts import ManuscriptStore

        manuscripts = ManuscriptStore(self.project)
        session = manuscripts.read(manuscript_id)
        sections = session["sections"]
        known = {item["id"]: item for item in sections}
        if section_ids is None:
            requested = list(known)
        else:
            if (not isinstance(section_ids, list) or not section_ids
                    or len(section_ids) > MAX_HISTORY_FILES
                    or any(not isinstance(item, str) or item not in known for item in section_ids)
                    or len(set(section_ids)) != len(section_ids)):
                raise VibeNovelError("请选择本修订会话内不重复的章节，最多80份")
            requested = section_ids
        # All current hashes belong to this manifest. Other work copies and
        # canonical chapters are never silently treated as this manuscript.
        corpus, omitted, source_hashes, total = [], [], {}, 0
        for item in sections:
            if len(corpus) >= MAX_HISTORY_FILES:
                omitted.append({"section_id": item["id"], "path": item["path"], "reason": "corpus_file_limit"})
                continue
            try:
                document = self.documents.read(item["path"])
            except (OSError, VibeNovelError):
                omitted.append({"section_id": item["id"], "path": item["path"], "reason": "missing_or_unreadable"})
                continue
            if document["hash"] != item["current_hash"]:
                raise ConflictError("修订稿在准备检查时发生变化，请重新检查")
            if total + document["bytes"] > MAX_CORPUS_BYTES:
                omitted.append({"section_id": item["id"], "path": item["path"], "reason": "corpus_byte_limit"})
                continue
            total += document["bytes"]
            corpus.append({"path": item["path"], "hash": document["hash"]})
            source_hashes[item["path"]] = document["hash"]
        reports, analyzed_characters = [], 0
        analysis = AnalysisStore(self.project)
        for identifier in requested:
            item = known[identifier]
            if item["path"] not in source_hashes:
                continue
            if len(reports) >= MAX_BATCH_TARGETS:
                omitted.append({"section_id": identifier, "path": item["path"], "reason": "target_batch_limit"})
                continue
            document = self.documents.read(item["path"])
            if document["hash"] != source_hashes[item["path"]]:
                raise ConflictError("修订稿在检查期间发生变化，请重新检查")
            if len(document["content"]) > 200000:
                omitted.append({"section_id": identifier, "path": item["path"], "reason": "target_character_limit"})
                continue
            if analyzed_characters + len(document["content"]) > MAX_BATCH_CHARACTERS:
                omitted.append({"section_id": identifier, "path": item["path"], "reason": "batch_character_limit"})
                continue
            reports.append(analysis.analyze(item["path"], document["hash"], explicit_corpus=corpus))
            analyzed_characters += len(document["content"])
        # Cross-window edits must not turn an old batch into a current result.
        latest = manuscripts.read(manuscript_id)
        if latest["hash"] != session["hash"]:
            raise ConflictError("修订会话已变化，请重新检查")
        for path, expected in source_hashes.items():
            if self.documents.read(path)["hash"] != expected:
                raise ConflictError("修订稿在检查期间发生变化；保留的单章报告仅作旧版本记录")
        for report in reports:
            if analysis.report(report["path"])["stale"]:
                raise ConflictError("检查配置或来源在批次期间已变化，请重新检查")
        return {"novel_id": self.project.novel_id, "manuscript_id": manuscript_id,
                "created_at": utc_now(), "reports": reports,
                "coverage": {"requested": len(requested), "analyzed": len(reports),
                             "corpus_scanned": len(corpus), "source_hashes": source_hashes,
                             "scope": "explicit_manuscript_working_copies", "omitted": omitted},
                "limitations": [
                    "逐章执行列明的本地规则，只对照本会话修订副本；不调用生成API。",
                    "最多20个目标/50万目标字符，每次对照最多80份/8MiB。超限、缺失和不可读材料列入omitted；可另选未查章节分批执行。",
                    "跨章字面重合可能是引用或合理呼应，不代表语义重复；未提供可靠的AI生成概率。",
                    "未自动认定全书因果、人物知情、OOC或伏笔回收；后续章仅作回顾性对照。",
                    "各单章报告独立保存，来源变化后须重新检查；未改变原稿、正式正文或故事记忆。"]}
