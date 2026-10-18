"""Version-aware rechecks and bounded editorial ranking, without API calls."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.analysis import (AnalysisStore, BASELINE_PATH, LEGACY_RULES,
                                     _legacy_metrics)
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import digest, json_bytes, read_json
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.core.toolkit import TOOLKIT_PATH, WritingToolkitStore
from vibenovel.errors import VibeNovelError


PARAGRAPH = "窗外的雨还在下，守门员把磨损的手套放到桌边，重新核对下午训练的名单，随后把缺席球员的名字圈了出来。"


class EditorialVerificationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.project = ProjectStore(Path(folder.name).resolve()).init("audit", genre="football")
        self.documents = DocumentStore(self.project)
        self.analysis = AnalysisStore(self.project)

    def draft(self, text, path="drafts/ch002.md"):
        return self.documents.save(path, text, None)

    def chapter(self, text, path="chapters/ch001.md"):
        self.project.path(path).write_bytes(text.encode("utf-8"))
        return {"path": path, "hash": digest(text.encode("utf-8"))}

    def test_later_warning_displaces_early_info_and_counts_each_rule(self):
        historic = self.chapter(PARAGRAPH)
        draft = self.draft("\n".join(f"他不知道的是，第{index}次训练改期。" for index in range(130))
                           + "\n\n" + PARAGRAPH)
        report = self.analysis.analyze(draft["path"], draft["hash"])
        self.assertEqual(len(report["issues"]), 100)
        self.assertEqual(report["issues"][0]["rule"], "literal_overlap")
        self.assertEqual(report["issues"][0]["related"]["hash"], historic["hash"])
        self.assertEqual(report["coverage"]["rule_counts"]["canned_phrase"],
                         {"found": 130, "returned": 99, "truncated": 31})
        self.assertEqual(report["coverage"]["rule_counts"]["literal_overlap"],
                         {"found": 1, "returned": 1, "truncated": 0})
        self.assertTrue(report["coverage"]["issue_limit_reached"])

    def test_exactly_full_pool_is_not_claimed_truncated(self):
        draft = self.draft("他不知道的是，训练改期。\n" * 100)
        report = self.analysis.analyze(draft["path"], draft["hash"])
        self.assertFalse(report["coverage"]["issue_limit_reached"])
        self.assertEqual(report["coverage"]["rule_counts"]["canned_phrase"]["truncated"], 0)

    def test_self_sample_uses_remaining_fixed_snapshots_without_rewriting_baseline(self):
        target = self.draft(("字" * 80 + "。\n\n") * 6)
        sample = self.chapter("短句。\n\n另一句。\n\n守门。\n\n发球。\n\n跑开。\n\n结束。")
        baseline = self.analysis.set_baseline([{"path": target["path"], "hash": target["hash"]}, sample])
        stored = self.project.path(BASELINE_PATH).read_bytes()
        report = self.analysis.analyze(target["path"], target["hash"])
        comparison = report["baseline"]["comparison"]
        self.assertTrue(report["baseline"]["comparison_available"])
        self.assertEqual(comparison["excluded_paths"], [target["path"]])
        self.assertEqual(comparison["samples"], [sample])
        self.assertEqual(comparison["metrics"]["mean_sentence_characters"], 2.17)
        self.assertTrue(any(issue["rule"] == "baseline_sentence_shift" for issue in report["issues"]))
        self.assertTrue(any(issue["rule"] == "baseline_paragraph_shift" for issue in report["issues"]))
        self.assertEqual(self.project.path(BASELINE_PATH).read_bytes(), stored)
        self.assertEqual(self.analysis.baseline()["hash"], baseline["hash"])
        self.assertFalse(report["stale"])

    def test_multiple_explainable_style_dimensions_are_statistics_not_quality(self):
        sample = self.chapter(("今天训练正常开展，教练逐个观察球员跑动。\n\n") * 8)
        self.analysis.set_baseline([sample])
        target = self.draft(("“仿佛宛如似乎，球来了。”\n\n" + "他看见" + "字" * 110 + "。\n\n") * 5)
        report = self.analysis.analyze(target["path"], target["hash"])
        rules = {issue["rule"] for issue in report["issues"]}
        self.assertTrue({"baseline_dialogue_shift", "baseline_simile_shift", "baseline_rhythm_shift"} <= rules)
        self.assertGreater(report["metrics"]["sentence_length_variation"], 0)
        self.assertIn("short_sentences_percent", report["metrics"])
        for issue in report["issues"]:
            if issue["rule"].startswith("baseline_"):
                self.assertIn("质量高低", issue["message"])
        self.assertNotIn("quality_score", report)

    def test_verify_binds_old_and_new_versions_without_changing_author_marks_or_canon(self):
        draft = self.draft("他不知道的是，训练已经改期。")
        old = self.analysis.analyze(draft["path"], draft["hash"])
        marked = self.analysis.mark(old["path"], old["hash"], old["issues"][0]["id"], "resolved")
        before = self.project.path(marked["path"]).read_bytes()
        unchanged = self.analysis.verify(marked["path"], marked["hash"])
        self.assertEqual(unchanged["results"][0]["status"], "remaining")
        self.assertEqual(unchanged["results"][0]["previous_status"], "resolved")
        changed = self.documents.save(draft["path"], "教练把新的训练通知贴在门边。", draft["hash"])
        fingerprint = self.project.fingerprint()
        result = self.analysis.verify(marked["path"], marked["hash"])
        self.assertEqual(result["previous"]["hash"], marked["hash"])
        self.assertEqual(result["previous"]["source"]["hash"], draft["hash"])
        self.assertEqual(result["report"]["source"]["hash"], changed["hash"])
        self.assertEqual(result["results"][0]["status"], "not_found")
        self.assertIn("不表示", result["results"][0]["reason"])
        self.assertEqual(self.project.path(marked["path"]).read_bytes(), before)
        self.assertEqual(self.project.fingerprint(), fingerprint)
        self.assertEqual(self.documents.read(draft["path"])["content"], changed["content"])
        self.assertEqual(list(self.project.path("memory").rglob("*.md")), [])
        with self.assertRaises(ConflictError):
            self.analysis.verify(marked["path"], old["hash"])

    def test_verify_matches_line_movement_and_rejects_truncated_absence(self):
        draft = self.draft("他不知道的是，唯一的一条训练通知。")
        old = self.analysis.analyze(draft["path"], draft["hash"])
        moved = self.documents.save(draft["path"], "# 新标题\n\n" + draft["content"], draft["hash"])
        result = self.analysis.verify(old["path"], old["hash"])
        self.assertEqual(result["results"][0]["status"], "remaining")
        self.assertEqual(result["report"]["issues"][0]["start_line"], 3)
        crowded = self.documents.save(draft["path"], moved["content"] + "\n\n" + "\n\n".join([PARAGRAPH] * 102), moved["hash"])
        result = self.analysis.verify(old["path"], old["hash"])
        self.assertEqual(result["results"][0]["status"], "unchecked")
        self.assertEqual(result["report"]["source"]["hash"], crowded["hash"])
        self.assertGreater(result["report"]["coverage"]["rule_counts"]["canned_phrase"]["truncated"], 0)

    def test_verify_missing_source_or_changed_reference_is_unchecked(self):
        historic = self.chapter(PARAGRAPH)
        draft = self.draft(PARAGRAPH)
        report = self.analysis.analyze(draft["path"], draft["hash"])
        self.project.path(historic["path"]).write_text("历史段落改动，已不能按原版本比较。", encoding="utf-8")
        result = self.analysis.verify(report["path"], report["hash"])
        self.assertEqual(result["results"][0]["status"], "unchecked")
        self.assertIn("历史来源", result["results"][0]["reason"])
        self.project.path(draft["path"]).unlink()
        result = self.analysis.verify(report["path"], report["hash"])
        self.assertIsNone(result["report"])
        self.assertEqual(result["results"][0]["status"], "unchecked")

    def test_legacy_baseline_and_report_remain_readable_without_silent_upgrade(self):
        sample = self.chapter("作者认可的旧版本样章。")
        self.analysis.set_baseline([sample])
        baseline = read_json(self.project.path(BASELINE_PATH))
        baseline["schema_version"] = 1
        baseline["metrics"] = _legacy_metrics(sample_text := baseline["samples"][0]["content"])
        self.assertTrue(sample_text)
        self.project.path(BASELINE_PATH).write_bytes(json_bytes(baseline))
        before = self.project.path(BASELINE_PATH).read_bytes()
        self.assertEqual(self.analysis.baseline()["metrics"], baseline["metrics"])
        draft = self.draft("他不知道的是，球员正在路上。")
        report = self.analysis.analyze(draft["path"], draft["hash"])
        legacy = read_json(self.project.path(report["path"]))
        legacy["schema_version"] = 1
        legacy["metrics"] = _legacy_metrics(draft["content"])
        legacy["baseline"].pop("comparison")
        legacy.pop("runtime_dependencies", None)
        legacy["coverage"].pop("rule_counts")
        legacy["coverage"]["rules"] = [rule for rule in legacy["coverage"]["rules"] if rule in LEGACY_RULES]
        legacy["coverage"]["skipped"] = [item for item in legacy["coverage"]["skipped"] if item["rule"] in LEGACY_RULES]
        self.project.path(report["path"]).write_bytes(json_bytes(legacy))
        loaded = self.analysis.report(report["path"])
        self.assertEqual(loaded["schema_version"], 1)
        result = self.analysis.verify(loaded["path"], loaded["hash"])
        self.assertEqual(result["results"][0]["status"], "remaining")
        self.assertEqual(result["report"]["schema_version"], 2)
        self.assertEqual(self.project.path(BASELINE_PATH).read_bytes(), before)

    def test_new_coverage_and_comparison_schema_rejects_forged_counts_and_samples(self):
        draft = self.draft("他不知道的是，训练已经改期。")
        report = self.analysis.analyze(draft["path"], draft["hash"])
        original = read_json(self.project.path(report["path"]))
        for change in ("count", "unknown_rule", "comparison", "missing_new_metric", "nan"):
            data = copy.deepcopy(original)
            if change == "count":
                data["coverage"]["rule_counts"]["canned_phrase"]["found"] = 0
            elif change == "unknown_rule":
                data["coverage"]["rule_counts"]["invented"] = {"found": 0, "returned": 0, "truncated": 0}
            elif change == "comparison":
                data["baseline"]["comparison"]["samples"] = [{"path": draft["path"], "hash": draft["hash"]}]
            elif change == "missing_new_metric":
                del data["metrics"]["sentence_length_variation"]
            else:
                data["metrics"]["mean_sentence_characters"] = float("nan")
            self.project.path(report["path"]).write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            with self.subTest(change=change), self.assertRaises(VibeNovelError):
                self.analysis.report(report["path"])

    def test_toolkit_rules_bind_configuration_and_count_the_internal_candidate_cap(self):
        toolkit = WritingToolkitStore(self.project)
        settings = {"lexicon": [{"id": "name", "term": "门将", "aliases": ["守门员"],
                                   "forbidden": ["门神"], "required": True, "note": "作者用词"}],
                    "characters": [], "threads": [], "style": {"instructions": "", "avoid": [], "anchors": []}}
        configured = toolkit.save(settings, None)
        draft = self.draft("门神冲了出去。\n" * 2100)
        report = self.analysis.analyze(draft["path"], draft["hash"])
        self.assertEqual(report["runtime_dependencies"][TOOLKIT_PATH], configured["hash"])
        self.assertEqual(report["coverage"]["rule_counts"]["lexicon_forbidden"],
                         {"found": 2100, "returned": 100, "truncated": 2000})
        self.assertEqual(report["coverage"]["rule_counts"]["lexicon_required"]["found"], 1)
        self.assertFalse(report["stale"])
        changed = copy.deepcopy(settings)
        changed["lexicon"][0]["forbidden"] = []
        toolkit.save(changed, configured["hash"])
        self.assertTrue(self.analysis.report(report["path"])["stale"])
        verification = self.analysis.verify(report["path"], report["hash"])
        self.assertTrue(all(item["status"] == "unchecked" for item in verification["results"]))
        self.assertTrue(all("规则配置不同" in item["reason"] for item in verification["results"]))

    def test_new_toolkit_configuration_during_publication_rejects_report(self):
        draft = self.draft("他不知道的是，训练已经改期。")
        original_save = self.analysis.documents.save

        def configure_before_publish(*args, **kwargs):
            WritingToolkitStore(self.project).save({"lexicon": [], "characters": [], "threads": [],
                                                   "style": {"instructions": "短句", "avoid": [], "anchors": []}}, None)
            return original_save(*args, **kwargs)

        with patch.object(self.analysis.documents, "save", side_effect=configure_before_publish):
            with self.assertRaises(ConflictError):
                self.analysis.analyze(draft["path"], draft["hash"])
        self.assertEqual(list(self.project.path("reviews").glob("analysis-*.json")), [])

    def test_verify_invalid_current_utf8_is_unchecked_instead_of_a_false_pass(self):
        draft = self.draft("他不知道的是，训练已经改期。")
        old = self.analysis.analyze(draft["path"], draft["hash"])
        self.project.path(draft["path"]).write_bytes(b"\xff\xfe")
        result = self.analysis.verify(old["path"], old["hash"])
        self.assertIsNone(result["report"])
        self.assertEqual(result["results"][0]["status"], "unchecked")


if __name__ == "__main__":
    unittest.main()
