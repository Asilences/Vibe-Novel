"""Offline chapter preparation, cutoff, approval and prompt snapshot checks."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.briefs import ChapterBriefStore
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import digest
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import GenerationResult


class ChapterBriefTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.briefs = ChapterBriefStore(self.project)

    def canonical(self, path, text):
        target = self.project.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return self.documents.read(path)

    def plan(self, number=3, text="批准章计划：承接上章，推进合作与冲突。"):
        target = "outlines/approved/ch%03d.md" % number
        approvals = ApprovalStore(self.project)
        proposal = approvals.create(target, text, "测试章计划", ["测试范围"], None)
        approvals.decide(proposal["id"], "approve")
        approvals.apply(proposal["id"])
        return self.documents.read(target)

    def build(self, plan, mode="compact"):
        return self.briefs.build(plan["path"], plan["hash"], mode)

    def test_approval_gate_rejects_unapproved_forged_stale_and_invalid_paths(self):
        raw = self.canonical("outlines/approved/ch003.md", "仅写到目录，不是批准")
        with self.assertRaises(ConflictError):
            self.build(raw)
        for path in ("../other/ch003.md", "config/api.local.json", "drafts/ch003.md", "outlines/approved/ch000.md"):
            with self.subTest(path=path), self.assertRaises(VibeNovelError):
                self.briefs.build(path, "0" * 64)
        plan = self.plan(2)
        with self.assertRaises(ConflictError):
            self.briefs.build(plan["path"], "0" * 64)
        self.project.path(plan["path"]).write_text("被手改", encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.build(plan)

    def test_past_cutoff_and_actual_ledgers_are_distinct_from_future_plans(self):
        previous = self.canonical("chapters/ch002.md", "上章末尾：钟声响起。")
        self.canonical("chapters/ch003.md", "未来定稿秘密不应进入过去证据")
        memory = self.canonical("memory/events/ch001.md", "已发生的合作事件")
        self.canonical("memory/events/ch004.md", "未来事件不应读取")
        self.canonical("memory/current_state.md", "未来汇总中的秘密")
        self.canonical("memory/repetition_ledger.md", "未来汇总中的重复记录")
        plan = self.plan()
        with patch.object(ApprovalStore, "approved_outline", autospec=True,
                          side_effect=ApprovalStore.approved_outline) as gate:
            brief = self.build(plan)
        self.assertGreaterEqual(gate.call_count, 1)
        self.assertEqual(brief["sources"][previous["path"]], previous["hash"])
        self.assertEqual(brief["sources"][memory["path"]], memory["hash"])
        self.assertNotIn("chapters/ch003.md", brief["sources"])
        self.assertNotIn("memory/events/ch004.md", brief["sources"])
        self.assertNotIn("未来汇总中的秘密", brief["text"])
        self.assertNotIn("未来事件不应读取", brief["text"])
        self.assertIn("未来策划", brief["text"])
        self.assertIn("未检查整部小说", brief["text"])
        self.assertTrue(any(item["id"] == "later_chapters" for item in brief["omitted"]))

    def test_previous_gap_is_explicit_and_earlier_chapter_is_not_a_replacement(self):
        self.canonical("chapters/ch001.md", "第一章，不能冒充第二章")
        plan = self.plan(3)
        brief = self.build(plan)
        previous = next(item for item in brief["sections"] if item["id"] == "previous")
        self.assertEqual(previous["status"], "missing")
        self.assertEqual(previous["paths"], [])
        self.assertIn("未查到第2章定稿", previous["content"])
        self.assertIn("chapters/ch001.md", brief["sources"])

    def test_compact_tail_full_content_and_omission_are_visible_and_bounded(self):
        body = "开头证据\n" + "中间内容" * 4000 + "\n上章结尾钩子"
        previous = self.canonical("chapters/ch002.md", body)
        self.canonical("memory/current_state.md", "角色尚未离开教室")
        self.canonical("memory/repetition_ledger.md", "同类冲突应有新代价")
        plan = self.plan()
        compact, full = self.build(plan), self.build(plan, "full")
        self.assertNotIn("开头证据", compact["text"])
        self.assertIn("上章结尾钩子", compact["text"])
        self.assertIn("开头证据", full["text"])
        self.assertEqual(compact["sources"][previous["path"]], digest(self.project.path(previous["path"]).read_bytes()))
        self.assertIn("memory/current_state.md", compact["sources"])
        self.assertIn("memory/repetition_ledger.md", compact["sources"])
        self.assertTrue(any(item["id"] == "previous" for item in compact["omitted"]))
        for brief in (compact, full):
            self.assertLessEqual(len(brief["text"]), brief["budget"]["limit"])
            self.assertEqual(brief["budget"]["characters"], len(brief["text"]))
            self.assertEqual(brief["fingerprint"], self.project.fingerprint())
            self.assertTrue(all(item["status"] in ("included", "missing", "omitted") for item in brief["sections"]))

    def test_missing_material_and_fixed_samples_are_not_silently_invented(self):
        plan = self.plan(1)
        brief = self.build(plan)
        missing = {item["id"] for item in brief["missing"]}
        self.assertTrue({"previous", "states", "events", "threads", "summaries", "repetition", "style_baseline"} <= missing)
        self.assertIn("作者尚未配置固定样章", brief["text"])
        for mode in ("unexpected", None, [], True):
            with self.subTest(mode=mode), self.assertRaises(VibeNovelError):
                self.build(plan, mode)

    def test_required_planning_is_never_silently_truncated(self):
        plan = self.plan(1, "计划" * 31000)
        with self.assertRaisesRegex(VibeNovelError, "精简"):
            self.build(plan)
        full = self.build(plan, "full")
        self.assertIn("计划" * 31000, full["text"])

    def test_snapshot_drift_during_collection_is_rejected(self):
        self.canonical("chapters/ch002.md", "过去定稿")
        plan = self.plan()
        original = DocumentStore._document

        def racing_read(store, path, state):
            result = original(store, path, state)
            if path == "chapters/ch002.md":
                store.project.path("bible/racing.md").write_text("并发新增事实", encoding="utf-8")
            return result

        with patch.object(DocumentStore, "_document", racing_read), self.assertRaises(ConflictError):
            self.build(plan)

    def test_draft_prompt_uses_the_same_brief_and_default_api(self):
        self.canonical("chapters/ch002.md", "上一章衔接")
        source = self.documents.save("outlines/source/seed.md", "来源总纲", None)
        plan = self.plan()
        brief = self.build(plan)
        manager = JobManager(self.workspace)
        request, prompt, sources, fingerprint, revision, metadata = manager._prepare(self.project, {
            "kind": "draft", "document_path": source["path"], "document_hash": source["hash"],
            "plan_path": plan["path"], "plan_hash": plan["hash"]})
        self.assertEqual(request["provider"], "api")
        self.assertEqual(request["brief_mode"], "compact")
        self.assertIn(brief["text"], prompt)
        self.assertEqual(prompt.count("上一章衔接"), 1)
        self.assertEqual(metadata["sources"], brief["sources"])
        self.assertEqual(fingerprint, brief["fingerprint"])
        self.assertEqual(revision, brief["revision"])
        self.assertTrue(set(brief["sources"]) <= set(sources))
        with self.assertRaisesRegex(VibeNovelError, "Codex"):
            manager._prepare(self.project, dict(request, provider="codex"))
        full = manager._prepare(self.project, dict(request, brief_mode="full"))
        self.assertEqual(full[5]["mode"], "full")
        with self.assertRaises(VibeNovelError):
            manager._prepare(self.project, dict(request, kind="rewrite"))

    def test_style_baseline_is_versioned_statistics_without_sample_story_text(self):
        from vibenovel.core.analysis import AnalysisStore
        sample = self.canonical("chapters/ch009.md", "后续章绝密内容绝不能作为当前角色知识。")
        baseline = AnalysisStore(self.project).set_baseline([{"path": sample["path"], "hash": sample["hash"]}])
        plan = self.plan(1)
        brief = self.build(plan)
        self.assertEqual(brief["baseline"]["hash"], baseline["hash"])
        self.assertIn("仅为作者固定样章的统计参照", brief["text"])
        self.assertNotIn(sample["content"], brief["text"])
        self.assertNotIn(sample["path"], brief["sources"])
        self.project.path(sample["path"]).write_text("作者修改样章", encoding="utf-8")
        stale = self.build(plan)
        self.assertTrue(stale["baseline"]["stale"])
        self.assertIn("固定样章已失效", stale["text"])

    def test_changed_baseline_blocks_a_prepared_draft_without_changing_canon(self):
        from vibenovel.core.analysis import AnalysisStore
        sample = self.canonical("chapters/ch001.md", "作者认可的风格样章")
        source = self.documents.save("outlines/source/seed.md", "来源总纲", None)
        plan = self.plan(2)
        manager = JobManager(self.workspace)
        request, _, sources, fingerprint, revision, metadata = manager._prepare(self.project, {
            "kind": "draft", "document_path": source["path"], "document_hash": source["hash"],
            "plan_path": plan["path"], "plan_hash": plan["hash"]})
        job = {"provider": "api", "kind": "draft", "request": request, "context_sources": sources,
               "fingerprint": fingerprint, "canonical_revision": revision, "chapter_brief": metadata}
        manager._validate_snapshot(self.project, job)
        AnalysisStore(self.project).set_baseline([{"path": sample["path"], "hash": sample["hash"]}])
        self.assertEqual(fingerprint, self.project.fingerprint())
        with self.assertRaisesRegex(VibeNovelError, "固定样章配置已变化"):
            manager._validate_snapshot(self.project, job)

    def test_draft_sample_edit_during_api_generation_blocks_candidate_publication(self):
        from vibenovel.core.analysis import AnalysisStore
        sample = self.documents.save("drafts/style.md", "未发生的未来情节，只用于固定文风统计。", None)
        baseline = AnalysisStore(self.project).set_baseline([{"path": sample["path"], "hash": sample["hash"]}])
        plan = self.plan(1)
        source = self.documents.save("outlines/source/seed.md", "来源总纲", None)
        started, release = threading.Event(), threading.Event()

        class Provider:
            def generate(inner, prompt, cancel):
                self.assertNotIn(sample["content"], prompt)
                started.set()
                if not release.wait(5):
                    raise RuntimeError("test timeout")
                return GenerationResult("旧样章统计下的候选正文")

        manager = JobManager(self.workspace, provider_factory=lambda _: Provider())
        self.addCleanup(manager.close)
        self.addCleanup(release.set)
        manager.start()
        job = manager.submit(self.project, {"kind": "draft", "document_path": source["path"],
                             "document_hash": source["hash"], "plan_path": plan["path"], "plan_hash": plan["hash"]})
        self.assertTrue(started.wait(2))
        self.assertEqual(job["context_sources"][sample["path"]], sample["hash"])
        self.documents.save(sample["path"], "作者修改了样章。", sample["hash"])
        self.assertEqual(AnalysisStore(self.project).baseline()["hash"], baseline["hash"])
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = manager.get(self.project, job["id"])
            if result["state"] in ("failed", "succeeded"):
                break
            time.sleep(.01)
        self.assertEqual(result["state"], "failed", result)
        self.assertIsNone(result["result_path"])
        self.assertEqual(list(self.project.path("drafts").glob("candidate-*")), [])
        # An already stale baseline is explicitly omitted from a fresh job;
        # it must not make all future drafting impossible.
        prepared = manager._prepare(self.project, job["request"])
        self.assertNotIn(sample["path"], prepared[2])
        self.assertIn("固定样章已失效", prepared[1])


if __name__ == "__main__":
    unittest.main()
