"""Only fixed scalar metadata may cross the shareable diagnostics boundary."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibenovel.core.analysis import AnalysisStore
from vibenovel.core.diagnostics import WritingDiagnosticsStore
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import json_bytes
from vibenovel.core.project import ProjectStore


class WritingDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("story", genre="campus", title="不能分享的秘密书名")
        self.documents = DocumentStore(self.project)
        self.diagnostics = WritingDiagnosticsStore(self.project)

    def write(self, path, content):
        target = self.project.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))

    def job(self, number, **extra):
        identifier = "%032x" % number
        data = {"schema_version": 1, "novel_id": "story", "id": identifier,
                "kind": "draft", "state": "failed", "_prompt": "秘密API提示词正文",
                "error": "秘密错误和密钥sk-never-share", "request": {"instruction": "秘密作者要求"},
                "logs": [{"message": "不能分享的日志内容"}]}
        data.update(extra)
        self.write(".runtime/jobs/" + identifier + ".json", json_bytes(data))

    def test_statistics_and_whitelisted_counts_do_not_include_any_free_text(self):
        text = "第一章不能分享的秘密正文\n\n  两字\n"
        self.write("chapters/ch001.md", text)
        self.write("chapters/ch002.md", "另一个秘密正文。")
        self.job(1)
        self.job(2, kind="秘密未知kind", state="秘密未知state")
        self.job(3, kind="review", state="succeeded")
        self.job(4, novel_id="other")
        source = self.documents.save("drafts/checked.md", "他不知道的是，这个秘密审稿原句不能分享。", None)
        AnalysisStore(self.project).analyze(source["path"], source["hash"])
        before = self.project.fingerprint()
        report = self.diagnostics.report()
        self.assertEqual(before, self.project.fingerprint())
        self.assertEqual(report["chapters"]["totals"]["files"], 2)
        first = report["chapters"]["items"][0]
        self.assertEqual(first["chapter"], 1)
        self.assertEqual(first["characters"], len(text))
        self.assertEqual(first["lines"], len(text.splitlines()))
        self.assertEqual(first["non_whitespace_characters"], sum(not char.isspace() for char in text))
        jobs = {item["kind"]: item for item in report["jobs"]["by_kind"]}
        self.assertEqual(jobs["draft"]["failures"], 1)
        self.assertEqual(jobs["review"]["states"]["succeeded"], 1)
        self.assertEqual(jobs["unknown"]["states"]["unknown"], 1)
        self.assertEqual(report["jobs"]["invalid_metadata"], 1)
        self.assertEqual(report["analysis"]["reports"], 1)
        self.assertGreater(report["analysis"]["issues"]["pending"], 0)
        serialized = json.dumps(report, ensure_ascii=False)
        for marker in ("不能分享的秘密书名", "秘密正文", "秘密API提示词正文", "秘密错误", "sk-never-share",
                       "秘密作者要求", "不能分享的日志内容", "秘密未知kind", "秘密未知state", "秘密审稿原句", str(self.workspace)):
            self.assertNotIn(marker, serialized)

    def test_malformed_oversized_and_limited_inputs_are_explicitly_omitted(self):
        self.write("chapters/ch001.md", "一")
        self.write("chapters/ch002.md", "二")
        self.job(1)
        self.job(2)
        self.write(".runtime/jobs/" + "f" * 32 + ".json", "{")
        self.write("reviews/analysis-" + "d" * 32 + ".json", '{"schema_version":99}')
        self.write("reviews/analysis-" + "e" * 32 + ".json", b" " * 2000)
        self.write("chapters/arbitrary-secret-name.md", "未知文件名也不能分享")
        self.write(".runtime/style-baseline.json", "{")
        self.write(".runtime/writing-toolkit.json", "{")
        before = self.project.fingerprint()
        with patch("vibenovel.core.diagnostics.MAX_CHAPTER_FILES", 1), \
             patch("vibenovel.core.diagnostics.MAX_JOB_FILES", 2), \
             patch("vibenovel.core.diagnostics.MAX_METADATA_FILE_BYTES", 1000):
            report = self.diagnostics.report()
        self.assertEqual(before, self.project.fingerprint())
        reasons = {(item["area"], item["reason"]) for item in report["omitted"]}
        self.assertIn(("chapters", "file_limit"), reasons)
        self.assertIn(("chapters", "unrecognized_filename"), reasons)
        self.assertIn(("jobs", "file_limit"), reasons)
        self.assertIn(("jobs", "invalid_or_unreadable_json"), reasons)
        self.assertIn(("analysis", "byte_limit"), reasons)
        self.assertEqual(report["analysis"]["invalid_metadata"], 1)
        self.assertFalse(report["configuration"]["baseline"]["valid"])
        self.assertFalse(report["configuration"]["toolkit"]["valid"])
        self.assertNotIn("arbitrary-secret-name", json.dumps(report))

    def test_report_rule_enum_and_legacy_count_omissions_are_bounded(self):
        identifier = "c" * 32
        data = {"schema_version": 2, "id": identifier, "novel_id": "story", "canonical_revision": 0,
                "issues": [{"status": "pending", "rule": "自由规则秘密", "quote": "自由原句秘密"}],
                "coverage": {"rule_counts": {"自由规则秘密": {"found": 8, "returned": 8, "truncated": 0},
                    "canned_phrase": {"found": 7, "returned": 2, "truncated": 5}}}}
        self.write("reviews/analysis-" + identifier + ".json", json_bytes(data))
        legacy = dict(data, id="b" * 32, schema_version=1, coverage={})
        self.write("reviews/analysis-" + "b" * 32 + ".json", json_bytes(legacy))
        report = self.diagnostics.report()
        self.assertEqual(report["analysis"]["by_rule"], [
            {"rule": "canned_phrase", "found": 7, "returned": 2, "truncated": 5}])
        self.assertTrue(any(item["reason"] == "legacy_report_without_rule_counts" for item in report["omitted"]))
        self.assertNotIn("自由规则秘密", json.dumps(report, ensure_ascii=False))
        self.assertNotIn("自由原句秘密", json.dumps(report, ensure_ascii=False))

    def test_empty_workspace_diagnostics_does_not_load_or_download_semantic_model(self):
        from vibenovel.core.semantic import LocalEmbedding
        before = self.project.fingerprint()
        with patch.object(LocalEmbedding, "embed", side_effect=AssertionError("no inference")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            report = self.diagnostics.report()
        self.assertEqual(before, self.project.fingerprint())
        self.assertEqual(report["chapters"]["totals"]["characters"], 0)
        self.assertEqual(report["jobs"]["by_kind"], [])
        self.assertEqual(report["configuration"]["semantic"]["check"], "file_presence_only")
        self.assertFalse(report["configuration"]["semantic"]["loaded"])


if __name__ == "__main__":
    unittest.main()
