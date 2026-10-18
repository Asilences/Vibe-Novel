"""Offline HTTP regressions for new writing-toolkit and retrieval routes."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

try:  # tools/run_tests.py discovers modules directly from tests/.
    import test_analysis_api as fixtures
    from test_semantic import FakeEngine
except ModuleNotFoundError:
    from tests import test_analysis_api as fixtures
    from tests.test_semantic import FakeEngine


@unittest.skipUnless(fixtures.TestClient, 'Optional Studio tests: install ".[studio,test]"')
class ToolkitAPITests(unittest.TestCase):
    setUp = fixtures.AnalysisAPITests.setUp
    write = fixtures.AnalysisAPITests.write
    draft = fixtures.AnalysisAPITests.draft
    analyze = fixtures.AnalysisAPITests.analyze
    mark = fixtures.AnalysisAPITests.mark
    chapter = fixtures.AnalysisAPITests.chapter

    def config(self, expected_hash=None):
        return {"expected_hash": expected_hash,
                "lexicon": [{"id": "club", "term": "标准名称", "aliases": ["登记别名"],
                             "forbidden": ["不推荐写法"], "required": False, "note": "使用说明"}],
                "characters": [{"id": "hero", "name": "人物姓名", "aliases": [], "max_absence": 5}],
                "threads": [{"id": "clue", "label": "伏笔名称", "terms": ["线索关键词"],
                             "start_chapter": 1, "remind_after": 5, "end_chapter": None}],
                "style": {"instructions": "保持克制", "avoid": [], "anchors": []}}

    def test_toolkit_examples_anchor_roundtrip_and_optimistic_conflict_preserve_canon(self):
        draft = self.draft("人物姓名走进训练场。\n\n线索关键词仍在笔记里。")
        before = self.project.fingerprint()
        config = self.config()
        config["style"]["anchors"] = [{"path": draft["path"], "hash": draft["hash"],
                                        "start_line": 1, "end_line": 1}]
        saved = self.write(self.base + "/toolkit", config, "PUT")
        self.assertEqual(saved.status_code, 200, saved.text)
        value = saved.json()
        self.assertEqual(value["style"]["anchors"][0]["quote"], "人物姓名走进训练场。")
        self.assertFalse(value["stale"])
        self.assertEqual(self.write(self.base + "/toolkit", config, "PUT").status_code, 409)
        fresh = {key: copy.deepcopy(value[key]) for key in ("lexicon", "characters", "threads", "style")}
        fresh["expected_hash"] = value["hash"]
        fresh["style"]["anchors"] = [{key: anchor[key] for key in ("path", "hash", "start_line", "end_line")}
                                       for anchor in fresh["style"]["anchors"]]
        updated = self.write(self.base + "/toolkit", fresh, "PUT")
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(self.project.fingerprint(), before)
        self.documents.save(draft["path"], "原句已经改写。", draft["hash"])
        current = self.client.get(self.base + "/toolkit").json()
        self.assertTrue(current["stale"])
        self.assertEqual(current["style"]["anchors"][0]["quote"], "人物姓名走进训练场。")
        structure = self.client.get(self.base + "/structure")
        self.assertEqual(structure.status_code, 200, structure.text)
        self.assertEqual(structure.json()["scanned"], 0, "Draft anchors must not become canonical events")

    def test_verify_keeps_author_mark_in_old_report_and_does_not_claim_resolution(self):
        draft = self.draft()
        report = self.analyze(draft)
        marked = self.mark(report, "ignored").json()
        self.documents.save(draft["path"], "# 第二章\n\n队长收起名单。", draft["hash"])
        before = self.project.fingerprint()
        response = self.write(self.base + "/verify", {"report_path": marked["path"], "report_hash": marked["hash"]})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertIsNotNone(value["report"])
        self.assertTrue(any(item["status"] == "not_found" for item in value["results"]))
        self.assertTrue(all(item["status"] in ("remaining", "not_found", "unchecked") for item in value["results"]))
        self.assertEqual(value["results"][0]["previous_status"], "ignored")
        old = self.client.get(self.base + "/report", params={"path": marked["path"]}).json()
        self.assertEqual(old["hash"], marked["hash"])
        self.assertEqual(old["issues"][0]["status"], "ignored")
        self.assertEqual(self.project.fingerprint(), before)
        stale = self.write(self.base + "/verify", {"report_path": marked["path"], "report_hash": report["hash"]})
        self.assertEqual(stale.status_code, 409)

    def test_semantic_missing_dependencies_are_explicit_and_never_fallback(self):
        engine = FakeEngine(available=False)
        with patch("vibenovel.core.semantic.engine_for", return_value=engine):
            status = self.client.get(self.base + "/semantic/status")
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()["available"])
            response = self.write(self.base + "/semantic/search", {"query": "手套", "limit": 10})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("测试依赖缺失", response.json()["detail"])
        self.assertEqual(engine.calls, [])
        self.assertFalse(self.project.path(".runtime/semantic-index.json").exists())

    def test_new_mutating_routes_require_csrf_and_block_cross_origin(self):
        bodies = [("/toolkit", "PUT", self.config()),
                  ("/verify", "POST", {"report_path": "reviews/analysis-" + "0" * 32 + ".json", "report_hash": "0" * 64}),
                  ("/semantic/search", "POST", {"query": "手套", "limit": 10})]
        for suffix, method, body in bodies:
            with self.subTest(route=suffix):
                self.assertEqual(self.client.request(method, self.base + suffix, json=body).status_code, 403)
                self.assertEqual(self.client.request(method, self.base + suffix, json=body,
                    headers={**self.headers, "Origin": "https://untrusted.invalid"}).status_code, 403)
        self.assertFalse(self.project.path(".runtime/writing-toolkit.json").exists())

    def test_fake_semantic_http_search_keeps_exact_source_and_project_revision(self):
        chapter = self.chapter("# 第一章\n\n红色手套仍在柜子里。")
        engine = FakeEngine()
        with patch("vibenovel.core.semantic.engine_for", return_value=engine):
            response = self.write(self.base + "/semantic/search", {"query": "手套", "limit": 10})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["mode"], "local_embedding")
        self.assertEqual(value["results"][0]["hash"], chapter["hash"])
        self.assertEqual(value["results"][0]["start_line"], 3)
        self.assertEqual(value["results"][0]["quote"], "红色手套仍在柜子里。")
        self.assertIn("canonical_revision", value)


if __name__ == "__main__":
    unittest.main()
