"""Existing-manuscript editing binds full sources and preserves unselected bytes."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.analysis import AnalysisStore
from vibenovel.core.documents import DocumentStore
from vibenovel.core.project import ProjectStore
from vibenovel.core.toolkit import WritingToolkitStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import GenerationResult, ProviderError
from vibenovel.studio.routing import ProjectRouting, default_policy


class EditorialProvider:
    def __init__(self, text="新的替换正文。", *, blocked=False, error=False):
        self.text, self.error = text, error
        self.started, self.release = threading.Event(), threading.Event()
        self.calls, self.prompts = 0, []
        if not blocked:
            self.release.set()

    def generate(self, prompt, cancel):
        self.calls += 1
        self.prompts.append(prompt)
        self.started.set()
        if not self.release.wait(5):
            raise RuntimeError("test provider wait exceeded")
        if self.error:
            raise ProviderError("测试模型失败")
        return GenerationResult(self.text, {"total_tokens": 12}, "offline-test")


class EditorialJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.original = "\ufeff未选择的开头😀\r\n他不知道的是，风已经停了。\r\n未选择的结尾𠀀\r\n"
        self.source = self.documents.save("drafts/manuscript.md", self.original, None)
        start = self.original.index("他不知道的是")
        self.selection = {"start_char": start, "end_char": self.original.index("\r\n", start)}

    def manager(self, provider=None):
        provider = provider or EditorialProvider()
        manager = JobManager(self.workspace, provider_factory=lambda name: provider)
        manager.start()
        self.addCleanup(manager.close)
        self.addCleanup(provider.release.set)
        return manager, provider

    def request(self, **changes):
        request = {"kind": "rewrite", "document_path": self.source["path"], "document_hash": self.source["hash"],
                   "instruction": "保持人物选择和时间，不加新事实。", "selection": dict(self.selection),
                   "editorial": {"goals": ["remove_cliches", "preserve_voice"], "intensity": "conservative"}}
        request.update(changes)
        return request

    def wait(self, manager, job_id, states=None):
        states = states or {"succeeded", "failed", "interrupted"}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = manager.get(self.project, job_id)
            if job["state"] in states:
                return job
            time.sleep(.01)
        self.fail("Editorial job failed to finish: %r" % job)

    @staticmethod
    def toolkit_data(*, anchors=None):
        return {"lexicon": [{"id": "term", "term": "门将", "aliases": [], "forbidden": ["门神"],
                             "required": False, "note": "作者用词"}], "characters": [], "threads": [],
                "style": {"instructions": "以克制的动作表达情绪。", "avoid": ["命运的齿轮"], "anchors": anchors or []}}

    def test_selected_rewrite_preserves_bom_crlf_emoji_and_all_outside_bytes_without_touching_source(self):
        manager, provider = self.manager(EditorialProvider("他把窗轻轻合上。"))
        before = self.project.fingerprint()
        job = manager.submit(self.project, self.request())
        final = self.wait(manager, job["id"])
        self.assertEqual(final["state"], "succeeded", final)
        expected = self.original[:self.selection["start_char"]] + provider.text + self.original[self.selection["end_char"]:]
        self.assertEqual(self.project.path(final["result_path"]).read_bytes(), expected.encode("utf-8"))
        self.assertEqual(self.project.path(self.source["path"]).read_bytes(), self.original.encode("utf-8"))
        self.assertEqual(self.project.fingerprint(), before)
        self.assertNotIn("未选择的开头", provider.prompts[0])
        self.assertNotIn("未选择的结尾", provider.prompts[0])
        self.assertIn("他不知道的是，风已经停了。", provider.prompts[0])
        self.assertIn("只输出此范围的替换正文", provider.prompts[0])
        self.assertIn("不保证任何检测通过", provider.prompts[0])
        self.assertEqual(final["request"]["selection"], self.selection)
        self.assertEqual(final["context_sources"][self.source["path"]], self.source["hash"])
        self.assertEqual(final["result_version_id"], self.documents.history(final["result_path"])[0]["id"])
        self.assertEqual(final["editorial_context"]["prompt_budget"]["characters"], len(provider.prompts[0]))

    def test_selected_review_returns_report_without_rewrite_assembly_or_unrelated_canon(self):
        self.project.path("chapters/ch099.md").write_text("不得混入的后续正式章秘密。", encoding="utf-8")
        self.project.path("memory/events/ch099.md").parent.mkdir(parents=True, exist_ok=True)
        self.project.path("memory/events/ch099.md").write_text("不得混入的后续记忆秘密。", encoding="utf-8")
        manager, provider = self.manager(EditorialProvider("审稿报告：这句含套话，请作者结合语境决定。"))
        final = self.wait(manager, manager.submit(self.project, self.request(kind="review"))["id"])
        self.assertEqual(final["state"], "succeeded", final)
        self.assertTrue(final["result_path"].startswith("reviews/review-"))
        self.assertEqual(self.documents.read(final["result_path"])["content"], provider.text)
        self.assertNotIn("不得混入", provider.prompts[0])
        self.assertNotIn("未选择的开头", provider.prompts[0])
        self.assertIn("只审查此范围", provider.prompts[0])
        self.assertEqual(set(final["context_sources"]), {self.source["path"]})

    def test_whole_document_rewrite_does_not_require_plan_and_defaults_to_preserving_voice(self):
        request = self.request()
        request.pop("selection")
        manager, provider = self.manager(EditorialProvider("完整的新候选正文。"))
        final = self.wait(manager, manager.submit(self.project, request)["id"])
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(self.documents.read(final["result_path"])["content"], provider.text)
        self.assertEqual(final["request"]["editorial"], request["editorial"])
        self.assertIsNone(final["editorial_context"]["selection"])
        self.assertIn(self.original, provider.prompts[0])

    def test_selection_without_explicit_goals_defaults_to_preserving_voice(self):
        request = self.request()
        request.pop("editorial")
        manager, _ = self.manager()
        final = self.wait(manager, manager.submit(self.project, request)["id"])
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(final["request"]["editorial"], {"goals": ["preserve_voice"], "intensity": "standard"})

    def test_legacy_review_keeps_limited_continuity_evidence_and_no_new_editorial_contract(self):
        self.project.path("chapters/ch001.md").write_text("保留旧版有限连续性证据。", encoding="utf-8")
        request = self.request(kind="review")
        request.pop("selection")
        request.pop("editorial")
        manager, provider = self.manager()
        final = self.wait(manager, manager.submit(self.project, request)["id"])
        self.assertEqual(final["state"], "succeeded", final)
        self.assertNotIn("editorial", final["request"])
        self.assertNotIn("editorial_context", final)
        self.assertIn("保留旧版有限连续性证据。", provider.prompts[0])
        self.assertIn("chapters/ch001.md", final["context_sources"])

    def test_invalid_ranges_goals_intensities_and_task_kinds_reject_before_model(self):
        manager, provider = self.manager()
        invalid = []
        for selection in ({"start_char": -1, "end_char": 2}, {"start_char": 0, "end_char": 0},
                          {"start_char": True, "end_char": 2}, {"start_char": 0, "end_char": len(self.original) + 1},
                          {"start_char": "0", "end_char": 2}, {"start_char": 0},
                          {"start_char": 0, "end_char": 2, "bytes": True}):
            invalid.append(self.request(selection=selection))
        for editorial in ({"goals": ["unknown"], "intensity": "standard"},
                          {"goals": ["preserve_voice", "preserve_voice"], "intensity": "standard"},
                          {"goals": "preserve_voice", "intensity": "standard"},
                          {"goals": [], "intensity": "unsafe"}, {"goals": [], "intensity": True},
                          {"goals": []}, {"goals": [], "intensity": "standard", "extra": True}):
            invalid.append(self.request(editorial=editorial))
        invalid.append(self.request(kind="expand_outline"))
        invalid.append(self.request(document_hash="0" * 64))
        invalid.append(self.request(plan_path="outlines/approved/ch001.md", plan_hash="0" * 64))
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(VibeNovelError):
                manager.submit(self.project, request)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(manager.list(self.project), [])

    def test_expression_references_are_explicit_bound_and_do_not_supply_baseline_plot(self):
        sample = self.documents.save("drafts/sample.md", "不应发送的样章剧情秘密。", None)
        anchor = self.documents.save("drafts/anchor.md", "作者认可的表达锚段。\n不选后续剧情秘密。", None)
        AnalysisStore(self.project).set_baseline([{key: sample[key] for key in ("path", "hash")}])
        WritingToolkitStore(self.project).save(self.toolkit_data(anchors=[
            {"path": anchor["path"], "hash": anchor["hash"], "start_line": 1, "end_line": 1}]), None)
        manager, provider = self.manager()
        final = self.wait(manager, manager.submit(self.project, self.request())["id"])
        self.assertEqual(final["state"], "succeeded", final)
        prompt = provider.prompts[0]
        self.assertIn("作者认可的表达锚段。", prompt)
        self.assertIn("以克制的动作表达情绪", prompt)
        self.assertIn("不得引入其剧情/人物知识", prompt)
        self.assertNotIn("不应发送的样章剧情秘密", prompt)
        self.assertNotIn("不选后续剧情秘密", prompt)
        self.assertEqual(final["context_sources"][anchor["path"]], anchor["hash"])
        self.assertEqual(final["context_sources"][sample["path"]], sample["hash"])

    def test_source_or_expression_configuration_drift_prevents_publication(self):
        toolkit = WritingToolkitStore(self.project)
        configured = toolkit.save(self.toolkit_data(), None)
        for change in ("source", "toolkit"):
            with self.subTest(change=change):
                source = self.documents.read(self.source["path"])
                manager, provider = self.manager(EditorialProvider(blocked=True))
                request = self.request(document_hash=source["hash"])
                job = manager.submit(self.project, request)
                self.assertTrue(provider.started.wait(2))
                if change == "source":
                    self.documents.save(source["path"], source["content"] + "新增作者文字", source["hash"])
                else:
                    data = self.toolkit_data()
                    data["style"]["instructions"] = "作者刚修改的偏好。"
                    toolkit.save(data, configured["hash"])
                provider.release.set()
                final = self.wait(manager, job["id"])
                self.assertEqual(final["state"], "failed", final)
                self.assertIsNone(final["result_path"])
                manager.close()

    def test_failed_request_retry_preserves_selection_and_editorial_contract(self):
        policy = default_policy()
        policy["max_calls"] = 2
        ProjectRouting(self.project, self.workspace).save(policy, None)
        manager, provider = self.manager(EditorialProvider(error=True))
        first = self.wait(manager, manager.submit(self.project, self.request())["id"])
        self.assertEqual(first["state"], "failed")
        provider.error = False
        second = self.wait(manager, manager.retry(self.project, first["id"])["id"])
        self.assertEqual(second["state"], "succeeded", second)
        self.assertEqual(second["retry_of"], first["id"])
        self.assertEqual(second["request"]["selection"], self.selection)
        self.assertEqual(second["request"]["editorial"], self.request()["editorial"])
        self.assertEqual(provider.calls, 2)

    def test_restart_recovery_publishes_assembled_candidate_once_without_reapplying_selection(self):
        manager, provider = self.manager(EditorialProvider("恢复后的替换正文。"))
        save = DocumentStore.save

        def failed_save(store, path, content, expected_hash, provenance="AUTHOR", **kwargs):
            if provenance == "AI_SUGGESTED":
                raise OSError("test failure before save")
            return save(store, path, content, expected_hash, provenance, **kwargs)

        with patch.object(DocumentStore, "save", failed_save):
            job = manager.submit(self.project, self.request())
            interrupted = self.wait(manager, job["id"])
        self.assertEqual(interrupted["state"], "interrupted", interrupted)
        expected = self.original[:self.selection["start_char"]] + provider.text + self.original[self.selection["end_char"]:]
        self.assertEqual(interrupted["recovery_candidate"], expected)
        manager.close()
        recovered, second_provider = self.manager()
        final = recovered.recover_result(self.project, job["id"])
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(self.documents.read(final["result_path"])["content"], expected)
        self.assertEqual(len(self.documents.history(final["result_path"])), 1)
        self.assertEqual(final["result_version_id"], self.documents.history(final["result_path"])[0]["id"])
        self.assertEqual(second_provider.calls, 0)
        self.assertEqual(self.documents.read(self.source["path"])["hash"], self.source["hash"])


if __name__ == "__main__":
    unittest.main()
