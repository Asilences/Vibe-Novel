"""Editorial clues retain exact evidence and cannot acquire canon authority."""
from __future__ import annotations

import tempfile
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.analysis import AnalysisStore, BASELINE_PATH, MAX_BASELINE_SAMPLES
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import digest, json_bytes, read_json
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


REPEATED = "窗外的雨还在下，守门员把磨损的手套放到桌边，重新核对下午训练的名单，随后把缺席球员的名字圈了出来。"


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.projects = ProjectStore(self.workspace)
        self.project = self.projects.init("editorial", genre="football")
        self.documents = DocumentStore(self.project)
        self.analysis = AnalysisStore(self.project)

    def draft(self, content="他不知道的是，名单已经换了。", path="drafts/ch001.md"):
        return self.documents.save(path, content, None)

    def chapter(self, name, content):
        # Test fixtures represent pre-existing author-approved chapters.
        path = self.project.path(f"chapters/{name}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return {"path": f"chapters/{name}.md", "hash": digest(content.encode("utf-8"))}

    def test_explicit_fixed_samples_do_not_follow_drift_or_fallback_to_other_chapters(self):
        sample = self.chapter("ch001", "这是作者认可的固定样章。\n短句有节奏。")
        self.chapter("ch002", "另一章不能自动变成作者认可的样章。")
        before = self.project.fingerprint()
        empty = self.analysis.baseline()
        self.assertFalse(empty["configured"])
        self.assertIsNone(empty["hash"])
        selected = self.analysis.set_baseline([sample], None)
        self.assertEqual(selected["samples"][0]["hash"], sample["hash"])
        self.assertFalse(selected["stale"])
        stored = self.project.path(BASELINE_PATH).read_bytes()
        self.assertEqual(before, self.project.fingerprint())
        self.project.path(sample["path"]).write_text("改变后的样章非常非常长。", encoding="utf-8")
        stale = self.analysis.baseline()
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["metrics"], selected["metrics"])
        self.assertEqual(stale["hash"], selected["hash"])
        self.assertEqual(stale["samples"][0]["hash"], sample["hash"])
        self.assertNotEqual(stale["samples"][0]["current_hash"], sample["hash"])
        self.assertEqual(self.project.path(BASELINE_PATH).read_bytes(), stored)
        self.assertEqual(len(stale["samples"]), 1)
        self.project.path(sample["path"]).unlink()
        self.assertIsNone(self.analysis.baseline()["samples"][0]["current_hash"])

    def test_baseline_selection_requires_source_and_configuration_optimistic_hashes(self):
        sample = self.chapter("ch001", "固定样章。")
        selected = self.analysis.set_baseline([sample])
        with self.assertRaises(ConflictError):
            self.analysis.set_baseline([sample], None)
        with self.assertRaises(ConflictError):
            self.analysis.set_baseline([{**sample, "hash": "0" * 64}], selected["hash"])
        self.assertEqual(self.analysis.baseline()["hash"], selected["hash"])
        refreshed = self.analysis.set_baseline([sample], selected["hash"])
        self.assertEqual(refreshed["config_hash"], selected["config_hash"])

    def test_baseline_limits_invalid_sources_and_corruption_rejected(self):
        sample = self.chapter("ch001", "可用样章。")
        for samples in (None, [], [sample] * (MAX_BASELINE_SAMPLES + 1), [sample, sample],
                        [{"path": "../outside.md", "hash": sample["hash"]}],
                        [{"path": "memory/current_state.md", "hash": sample["hash"]}],
                        [{**sample, "approved": True}], [{**sample, "hash": "not-a-hash"}]):
            with self.subTest(samples=samples), self.assertRaises(VibeNovelError):
                self.analysis.set_baseline(samples)
        selected = self.analysis.set_baseline([sample])
        data = read_json(self.project.path(BASELINE_PATH))
        data["samples"][0]["content"] = "被修改的快照。"
        self.project.path(BASELINE_PATH).write_bytes(json_bytes(data))
        with self.assertRaises(VibeNovelError):
            self.analysis.baseline()
        # An explicit replacement can repair a corrupted configuration.
        actual_hash = digest(self.project.path(BASELINE_PATH).read_bytes())
        repaired = self.analysis.set_baseline([sample], actual_hash)
        self.assertEqual(repaired["config_hash"], selected["config_hash"])
        huge = self.draft("字" * 400000, "drafts/large.md")
        with self.assertRaises(VibeNovelError):
            self.analysis.set_baseline([{"path": huge["path"], "hash": huge["hash"]}], repaired["hash"])

    def test_line_evidence_literal_overlap_and_no_canonical_writes(self):
        historic = self.chapter("ch001", "# 第一章\n\n" + REPEATED + "\n")
        content = "# 第二章\r\n\r\n他不知道的是，名单已经换了。\r\n\r\n" + REPEATED + "\r\n\r\n" + REPEATED + "\r\n"
        draft = self.draft(content)
        before = self.project.fingerprint()
        report = self.analysis.analyze(draft["path"], draft["hash"])
        self.assertFalse(report["stale"])
        self.assertEqual(report["source"]["current_hash"], draft["hash"])
        self.assertEqual(report["coverage"]["history_scanned"], 1)
        self.assertEqual(report["coverage"]["history_sources"], [historic])
        issue = next(item for item in report["issues"] if item["rule"] == "canned_phrase")
        self.assertEqual((issue["start_line"], issue["end_line"]), (3, 3))
        self.assertEqual(issue["quote"], content.splitlines()[2])
        repeated = [item for item in report["issues"] if item["rule"] == "literal_overlap"]
        self.assertEqual([item["start_line"] for item in repeated], [5, 7])
        self.assertEqual(repeated[0]["related"]["path"], historic["path"])
        self.assertEqual(repeated[0]["related"]["start_line"], 3)
        self.assertIn("不能判定剧情重复", repeated[0]["message"])
        self.assertTrue(all(item["status"] == "pending" for item in report["issues"]))
        self.assertEqual(self.project.fingerprint(), before)
        self.assertEqual(self.documents.read(draft["path"])["content"], content)
        self.assertEqual(list(self.project.path("memory").rglob("*.md")), [])
        self.assertEqual(len(self.documents.history(report["path"])), 1)
        self.assertEqual(self.analysis.report(report["path"])["hash"], report["hash"])

    def test_mark_is_versioned_optimistic_and_stale_source_cannot_resolve(self):
        draft = self.draft()
        report = self.analysis.analyze(draft["path"], draft["hash"])
        issue_id = report["issues"][0]["id"]
        marked = self.analysis.mark(report["path"], report["hash"], issue_id, "resolved")
        self.assertEqual(marked["issues"][0]["status"], "resolved")
        self.assertEqual(len(self.documents.history(report["path"])), 2)
        with self.assertRaises(ConflictError):
            self.analysis.mark(report["path"], report["hash"], issue_id, "ignored")
        changed = self.documents.save(draft["path"], "# 新标题\n旧问题已移到下一行。", draft["hash"])
        stale = self.analysis.report(report["path"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["source"]["current_hash"], changed["hash"])
        with self.assertRaises(ConflictError):
            self.analysis.mark(report["path"], marked["hash"], issue_id, "resolved")
        ignored = self.analysis.mark(report["path"], marked["hash"], issue_id, "ignored")
        self.assertTrue(ignored["stale"])
        self.assertEqual(ignored["issues"][0]["status"], "ignored")
        with self.assertRaises(VibeNovelError):
            self.analysis.mark(report["path"], ignored["hash"], issue_id, "verified")

    def test_canonical_history_or_baseline_changes_invalidate_old_report(self):
        sample = self.chapter("ch001", "这是一个固定的作者样章。")
        baseline = self.analysis.set_baseline([sample])
        draft = self.draft()
        report = self.analysis.analyze(draft["path"], draft["hash"])
        self.assertTrue(report["baseline"]["comparison_available"])
        self.chapter("ch002", "新定稿章节也会改变检查覆盖范围。")
        stale = self.analysis.report(report["path"])
        self.assertTrue(stale["stale"])
        self.assertTrue(any("正式作品版本" in reason for reason in stale["stale_reasons"]))
        rerun = self.analysis.analyze(draft["path"], draft["hash"])
        self.analysis.set_baseline([sample], baseline["hash"])
        self.assertTrue(self.analysis.report(rerun["path"])["stale"])
        with self.assertRaises(ConflictError):
            self.analysis.mark(rerun["path"], rerun["hash"], rerun["issues"][0]["id"], "resolved")

    def test_target_is_not_compared_against_itself_and_no_threshold_verdict(self):
        draft = self.draft("短句。\n下句。\n第三句。\n第四句。\n第五句。")
        self.analysis.set_baseline([{"path": draft["path"], "hash": draft["hash"]}])
        result = self.analysis.analyze(draft["path"], draft["hash"])
        self.assertFalse(result["baseline"]["comparison_available"])
        self.assertFalse(result["coverage"]["baseline_compared"])
        self.assertEqual(result["issues"], [])
        self.assertNotIn("quality_score", result)
        self.assertTrue(any("不判断文学质量" in text for text in result["limitations"]))

    def test_source_drift_during_analysis_prevents_publication(self):
        draft = self.draft()
        original_save = self.analysis.documents.save

        def drift_before_publish(*args, **kwargs):
            self.documents.save(draft["path"], "分析期间改稿。", draft["hash"])
            return original_save(*args, **kwargs)

        with patch.object(self.analysis.documents, "save", side_effect=drift_before_publish):
            with self.assertRaises(ConflictError):
                self.analysis.analyze(draft["path"], draft["hash"])
        self.assertEqual(list(self.project.path("reviews").glob("analysis-*.json")), [])

    def test_baseline_configuration_drift_during_analysis_prevents_publication(self):
        sample = self.chapter("ch001", "作者认可的固定样章。")
        draft = self.draft()
        original_save = self.analysis.documents.save

        def configure_before_publish(*args, **kwargs):
            self.analysis.set_baseline([sample])
            return original_save(*args, **kwargs)

        # This includes absent -> configured drift: None is a bound state.
        with patch.object(self.analysis.documents, "save", side_effect=configure_before_publish):
            with self.assertRaises(ConflictError):
                self.analysis.analyze(draft["path"], draft["hash"])
        self.assertEqual(list(self.project.path("reviews").glob("analysis-*.json")), [])

    def test_selected_draft_sample_drift_during_analysis_prevents_publication(self):
        sample = self.draft("作者选定的草稿样章。", "drafts/sample.md")
        self.analysis.set_baseline([{"path": sample["path"], "hash": sample["hash"]}])
        draft = self.draft()
        original_save = self.analysis.documents.save

        def change_sample_before_publish(*args, **kwargs):
            self.documents.save(sample["path"], "样章已修改。", sample["hash"])
            return original_save(*args, **kwargs)

        with patch.object(self.analysis.documents, "save", side_effect=change_sample_before_publish):
            with self.assertRaises(ConflictError):
                self.analysis.analyze(draft["path"], draft["hash"])
        self.assertEqual(list(self.project.path("reviews").glob("analysis-*.json")), [])

    def test_search_only_scans_this_novels_formal_chapters_and_retains_exact_lines(self):
        chapter = self.chapter("ch001", "# 第一章\r\n\r\n守门员把红色手套放在桌边。\r\n\r\n下午重新核对训练名单。")
        self.draft("守门员把红色手套放在桌边。", "drafts/unapproved.md")
        other = self.projects.init("another", genre="football")
        other.path("chapters/ch001.md").write_text("红色手套来自另一本小说。", encoding="utf-8")
        self.project.path("memory/hidden.md").write_text("红色手套不能作为搜索的正文。", encoding="utf-8")
        result = self.analysis.search("红色手套", 2)
        self.assertEqual(result["mode"], "literal_ngram")
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["results"][0]["path"], chapter["path"])
        self.assertEqual(result["results"][0]["hash"], chapter["hash"])
        self.assertEqual(result["results"][0]["start_line"], 3)
        self.assertEqual(result["results"][0]["quote"], "守门员把红色手套放在桌边。")
        self.assertTrue(any("不是语义检索" in text for text in result["limitations"]))
        self.assertEqual(result["omitted"], [])

    def test_search_and_analysis_surface_limits_and_unreadable_sources(self):
        self.chapter("ch001", "守门员戴着红色手套。")
        self.chapter("ch002", "另一双红色手套。")
        with patch("vibenovel.core.analysis.MAX_HISTORY_FILES", 1):
            result = self.analysis.search("红色手套")
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["omitted"], [{"path": "chapters/ch002.md", "reason": "file_limit"}])
        self.project.path("chapters/ch002.md").write_bytes(b"\xff\xfe")
        result = self.analysis.search("红色手套")
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["omitted"][0]["reason"], "unreadable_or_invalid_utf8")
        draft = self.draft("他不知道的是，雨没有停。\n" * 150)
        result = self.analysis.analyze(draft["path"], draft["hash"])
        self.assertEqual(len(result["issues"]), 100)
        self.assertTrue(result["coverage"]["issue_limit_reached"])
        self.assertTrue(result["coverage"]["omitted"])
        huge = self.draft("字" * 200001, "drafts/huge.md")
        with self.assertRaises(VibeNovelError):
            self.analysis.analyze(huge["path"], huge["hash"])

    def test_bad_inputs_and_copied_cross_novel_reports_rejected(self):
        draft = self.draft()
        for query in (None, "", " ", "!", "词\n项", "字" * 201, "\ud800"):
            with self.subTest(query=repr(query)), self.assertRaises(VibeNovelError):
                self.analysis.search(query)
        for limit in (True, 0, 31, "10"):
            with self.subTest(limit=limit), self.assertRaises(VibeNovelError):
                self.analysis.search("测试", limit)
        for path in ("../outside.md", "memory/state.md", "drafts/a//b.md", "drafts/a.json"):
            with self.subTest(path=path), self.assertRaises(VibeNovelError):
                self.analysis.analyze(path, draft["hash"])
        with self.assertRaises(ConflictError):
            self.analysis.analyze(draft["path"], "0" * 64)
        report = self.analysis.analyze(draft["path"], draft["hash"])
        other = self.projects.init("another", genre="football")
        other.path(report["path"]).write_bytes(self.project.path(report["path"]).read_bytes())
        with self.assertRaises(VibeNovelError):
            AnalysisStore(other).report(report["path"])
        with self.assertRaises(VibeNovelError):
            self.analysis.report("reviews/not-analysis.json")

    def test_corrupted_report_fields_are_rejected_before_reaching_the_ui(self):
        self.chapter("ch001", REPEATED)
        draft = self.draft("他不知道的是，名单已经换了。\n\n" + REPEATED)
        report = self.analysis.analyze(draft["path"], draft["hash"])
        original = read_json(self.project.path(report["path"]))
        mutations = [
            ((), "coverage", None), ((), "metrics", None), ((), "limitations", {"bad": True}),
            ((), "unexpected", "not a report field"),
            (("source",), "path", []), (("source",), "hash", False),
            (("coverage",), "rules", {"bad": True}), (("coverage",), "skipped", [False]),
            (("coverage",), "history_sources", [None]), (("coverage",), "omitted", [{"path": {}}]),
            (("coverage",), "history_scanned", True), (("coverage",), "issue_limit_reached", []),
            (("metrics",), "characters", {"bad": True}), (("metrics",), "sentences", True),
            (("baseline",), "comparison_available", {}), (("baseline",), "hash", []),
            (("baseline",), "samples", [None]), (("baseline",), "limitations", [None]),
            (("issues", 0), "message", {"bad": True}), (("issues", 0), "suggestion", []),
            (("issues", 0), "rule", []), (("issues", 0), "unexpected", {}),
            (("issues", 0), "related", {"path": "chapters/ch001.md", "hash": "0" * 64}),
            (("issues", 1, "related"), "quote", {}),
        ]
        for parents, field, value in mutations:
            with self.subTest(parents=parents, field=field):
                damaged = copy.deepcopy(original)
                target = damaged
                for parent in parents:
                    target = target[parent]
                target[field] = value
                self.project.path(report["path"]).write_bytes(json_bytes(damaged))
                with self.assertRaises(VibeNovelError):
                    self.analysis.report(report["path"])
                with self.assertRaises(VibeNovelError):
                    self.analysis.mark(report["path"], report["hash"], report["issues"][0]["id"], "resolved")
        # Missing required nested fields are invalid too, rather than a report
        # with undefined values that can crash React's coverage renderer.
        for parent, field in (((), "coverage"), (("coverage",), "rules"), (("issues", 0), "message")):
            damaged = copy.deepcopy(original)
            target = damaged
            for key in parent:
                target = target[key]
            del target[field]
            self.project.path(report["path"]).write_bytes(json_bytes(damaged))
            with self.assertRaises(VibeNovelError):
                self.analysis.report(report["path"])


if __name__ == "__main__":
    unittest.main()
