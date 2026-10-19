"""Offline two-stage methods, exact quotations and typed dependency gates."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.creation import CreativeBriefStore, defaults
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import read_json
from vibenovel.core.library import LibraryStore, default_policy
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import GenerationResult
from vibenovel.studio.routing import BudgetLedger, ProjectRouting
from vibenovel.studio.workflows import WorkflowManager


class MethodJobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.project = ProjectStore(self.workspace).init("methods", genre="general")
        self.library = LibraryStore(self.workspace, self.project)
        self.text = "第一节😀\r\n每个场景让人物作出选择，并付出代价。\r\n另一个重复句。\r\n每个场景让人物作出选择，并付出代价。"
        self.source = self.library.import_source("人工教材.md", self.text.encode("utf-8"), metadata={"outbound": "excerpts"})
        self.start, self.end = 0, self.text.index("另一个重复句")
        env = patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.upper().startswith("VIBENOVEL_")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        routing = ProjectRouting(self.project, self.workspace)
        policy = routing.public()
        policy["policy"]["max_calls"] = 3
        routing.save(policy["policy"], policy["hash"])

    def request(self, **overrides):
        return {"kind": "analyze_reference", "provider": "api", "library_scope": "book",
                "purpose": "scene_beats", "source_ranges": [{"source_id": self.source["id"], "source_version_id": self.source["version_id"],
                                                            "start_char": self.start, "end_char": self.end}],
                "idempotency_key": uuid.uuid4().hex, **overrides}

    def ref(self):
        start = self.text.index("每个场景")
        end = self.text.index("\r\n另一个重复句")
        return {"source_id": self.source["id"], "source_version_id": self.source["version_id"],
                "start_char": start, "end_char": end, "quote": self.text[start:end]}

    def analysis(self):
        ref = self.ref()
        return {"title": "场景的选择与代价", "reading_map": [{"title": "第一节", "summary": "只读第一节的首个例句。", "source_refs": [ref]}],
                "observations": [{"title": "决定推动", "observation": "文本建议选择带来代价。", "source_refs": [ref]}],
                "directions": [{"title": "节拍检查", "purpose": "场景规划", "rationale": "可用选择与代价检查停滞。", "source_refs": [ref]}],
                "references": [], "gaps": ["后续重复句所在部分未发送。"]}

    def methods(self):
        return {"methods": [{"title": "选择与代价检查", "summary": "检查场景是否改变人物处境。", "purpose": "检查场景节拍",
                             "applies_to": ["场景规划"], "not_for": ["不能代替作者情节决定"], "inputs": ["场景计划"],
                             "steps": ["找出人物选择", "指出为选择付出的代价"], "outputs": ["缺少变化的场景与建议"],
                             "limitations": ["不是文学质量分数"], "counterexamples": ["短暂过渡不强求冲突"], "checks": ["代价有依据"],
                             "source_refs": [self.ref()], "source_cases": [{"description": "材料将选择和代价关联。", "source_refs": [self.ref()]}],
                             "synthetic_exercises": [{"prompt": "人工例：角色放弃车票去帮助朋友。", "expected": "指出帮助决定与时间代价。"}]}],
                "references": [], "gaps": [], "not_adopted": []}

    def manager(self, provider):
        manager = JobManager(self.workspace, provider_factory=lambda name: provider)
        self.addCleanup(manager.close)
        return manager

    @staticmethod
    def config(preview):
        candidate = preview["candidates"][0]
        return {"policy_revision": preview["policy_revision"], "policy_hash": preview["policy_hash"],
                "registry_revision": preview["registry_revision"], "registry_hash": candidate["registry_hash"],
                "registry_legacy_marker": candidate["legacy_marker"]}

    def submit(self, manager, request):
        preview = manager.preview(self.project, request)
        library = preview.get("method_preview", preview.get("library_preview"))
        return manager.submit(self.project, request, expected_configuration=self.config(preview),
                              expected_library_hash=(library or {}).get("snapshot", {}).get("hash"))

    def wait(self, manager, identity):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = manager.get(self.project, identity)
            if job["state"] in ("succeeded", "failed", "awaiting_input", "interrupted", "cancelled"):
                return job
            time.sleep(.01)
        self.fail("offline method job did not finish")

    def fake_provider(self, calls):
        analysis, methods = self.analysis(), self.methods()
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                value = methods if "提炼有输入" in prompt else analysis
                return GenerationResult(json.dumps(value, ensure_ascii=False), {"prompt_tokens": 10, "completion_tokens": 10})
        return Provider()

    def run_analysis(self, manager):
        job = self.wait(manager, self.submit(manager, self.request())["id"])
        self.assertEqual(job["state"], "succeeded", job["error"])
        return job

    def extract_request(self, analysis):
        return {**copy.deepcopy(analysis["request"]), "kind": "extract_methods", "idempotency_key": uuid.uuid4().hex,
                "analysis_entry_id": analysis["result_library"]["analysis"]["id"],
                "analysis_version_id": analysis["result_library"]["analysis"]["version_id"], "method_run_id": analysis["id"]}

    def test_analysis_then_extraction_share_budget_and_publish_whole_candidate_bundle(self):
        calls = []
        manager = self.manager(self.fake_provider(calls))
        analysis = self.run_analysis(manager)
        self.assertEqual(analysis["route"]["role"], "planner")
        self.assertEqual(analysis["context_sources"], {})
        self.assertIsNone(analysis["result_path"])
        self.assertNotIn(self.text[self.end:], calls[0])
        self.assertIn("完整来源Unicode", calls[0])
        report = analysis["result_library"]["analysis"]["analysis"]
        self.assertEqual(report["coverage"]["provided_fragments"][0]["end_char"], self.end)
        extracted = self.wait(manager, self.submit(manager, self.extract_request(analysis))["id"])
        self.assertEqual(extracted["state"], "succeeded", extracted["error"])
        self.assertEqual(analysis["route"]["group_id"], extracted["route"]["group_id"])
        self.assertEqual(BudgetLedger(self.project).public(analysis["route"]["group_id"])["calls"], 2)
        method = extracted["result_library"]["methods"][0]
        self.assertIsNone(method["adoption"])
        self.assertEqual((method["status"], method["verification"], method["outbound"]), ("inbox", "unverified", "local"))
        details = json.loads(method["content"])
        self.assertEqual(details["synthetic_exercises"][0]["provenance"], "SYNTHETIC_EXERCISE")
        self.assertTrue(details["source_cases"][0]["source_refs"])
        self.assertEqual(len(calls), 2)
        for name in ("chapters", "memory", "bible", "outlines/approved"):
            self.assertFalse(any(p.is_file() for p in self.project.path(name).rglob("*")))

    def test_forged_quote_or_unprovided_duplicate_cannot_publish_and_can_repair_without_call(self):
        invalid = self.analysis()
        invalid["observations"][0]["source_refs"][0]["quote"] = "伪造引文"
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                return GenerationResult(json.dumps(invalid, ensure_ascii=False))
        manager = self.manager(Provider())
        job = self.wait(manager, self.submit(manager, self.request())["id"])
        self.assertEqual(job["state"], "awaiting_input")
        self.assertEqual(self.library.overview()["entries"], [])
        repaired = manager.repair_result(self.project, job["id"], json.dumps(self.analysis(), ensure_ascii=False))
        self.assertEqual(repaired["state"], "succeeded")
        self.assertEqual(len(calls), 1)

    def test_method_requests_idempotent_even_after_restart_and_need_new_key_to_retry(self):
        calls = []
        manager = self.manager(self.fake_provider(calls))
        request = self.request()
        preview = manager.preview(self.project, request)
        kw = {"expected_configuration": self.config(preview), "expected_library_hash": preview["method_preview"]["snapshot"]["hash"]}
        first = self.wait(manager, manager.submit(self.project, request, **kw)["id"])
        self.assertEqual(manager.submit(self.project, request, **kw)["id"], first["id"])
        with self.assertRaises(ConflictError):
            manager.submit(self.project, dict(request, instruction="different"), **kw)
        manager.close()
        restarted = self.manager(self.fake_provider(calls))
        self.assertEqual(restarted.submit(self.project, request, **kw)["id"], first["id"])
        self.assertEqual(len(calls), 1)
        with self.assertRaises(VibeNovelError):
            restarted.retry(self.project, first["id"])

    def test_wrong_scope_range_local_permission_and_unconfirmed_preview_rejected(self):
        calls = []
        manager = self.manager(self.fake_provider(calls))
        for changes in ({"library_scope": "another-book"}, {"source_ranges": []}, {"source_ranges": [dict(self.request()["source_ranges"][0], start_char=-1)]}):
            with self.subTest(changes=changes), self.assertRaises(VibeNovelError):
                manager.preview(self.project, self.request(**changes))
        request = self.request()
        with self.assertRaises(ConflictError):
            manager.submit(self.project, request)
        self.library.save_source_metadata(self.source["id"], {"outbound": "local"}, self.source["hash"])
        with self.assertRaises(VibeNovelError):
            manager.preview(self.project, request)
        self.assertEqual(calls, [])

    def test_queued_and_late_requests_stop_on_source_withdrawal_aba(self):
        calls, started, release = [], threading.Event(), threading.Event()
        self.addCleanup(release.set)
        output = self.analysis()
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                started.set()
                release.wait(5)
                return GenerationResult(json.dumps(output, ensure_ascii=False))
        manager = self.manager(Provider())
        first = self.submit(manager, self.request())
        self.assertTrue(started.wait(3))
        queued = self.submit(manager, self.request())
        changed = self.library.save_source_metadata(self.source["id"], {"withdrawn": True}, self.source["hash"])
        self.library.save_source_metadata(self.source["id"], {"withdrawn": False}, changed["hash"])
        release.set()
        self.assertEqual(self.wait(manager, first["id"])["state"], "failed")
        stopped = self.wait(manager, queued["id"])
        self.assertEqual(stopped["state"], "failed")
        self.assertEqual(stopped["attempts"], [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.library.overview()["entries"], [])

    def test_source_metadata_new_version_keeps_explicit_old_source_valid(self):
        calls, started, release = [], threading.Event(), threading.Event()
        self.addCleanup(release.set)
        value = self.analysis()
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                started.set()
                release.wait(5)
                return GenerationResult(json.dumps(value, ensure_ascii=False))
        manager = self.manager(Provider())
        submitted = self.submit(manager, self.request())
        self.assertTrue(started.wait(3))
        newer = self.library.save_source_metadata(self.source["id"], {"metadata": {"outbound": "excerpts", "title": "只补充书目"}}, self.source["hash"])
        self.assertNotEqual(newer["version_id"], self.source["version_id"])
        release.set()
        completed = self.wait(manager, submitted["id"])
        self.assertEqual(completed["state"], "succeeded", completed["error"])
        self.assertEqual(completed["library_context"]["snapshot"]["source_fragments"][0]["source_version_id"], self.source["version_id"])
        self.assertEqual(len(calls), 1)

    def test_publishing_interruption_recovers_bundle_without_repeating_model(self):
        calls = []
        manager = self.manager(self.fake_provider(calls))
        publish = LibraryStore.publish_method_bundle
        def fail_publish(library, *args, **kwargs):
            raise OSError("offline manifest publish failed")
        with patch.object(LibraryStore, "publish_method_bundle", fail_publish):
            pending = self.wait(manager, self.submit(manager, self.request())["id"])
        self.assertEqual(pending["state"], "interrupted")
        self.assertEqual(self.library.overview()["entries"], [])
        manager.close()
        restarted = self.manager(self.fake_provider(calls))
        interrupted = restarted.get(self.project, pending["id"])
        self.assertTrue(interrupted["recovery_available"])
        result = restarted.recover_result(self.project, pending["id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.library.overview()["entries"]), 1)

    def test_published_bundle_completion_reconciles_without_new_api_or_duplicate_cards(self):
        calls = []
        manager = self.manager(self.fake_provider(calls))
        persist, failed = manager._persist, []
        def fail_once(project, job):
            if job["kind"] == "analyze_reference" and job["state"] == "succeeded" and not failed:
                failed.append(True)
                raise OSError("offline completion record failure")
            return persist(project, job)
        with patch.object(manager, "_persist", side_effect=fail_once):
            pending = self.wait(manager, self.submit(manager, self.request())["id"])
        self.assertEqual(pending["state"], "interrupted")
        self.assertEqual(len(self.library.overview()["entries"]), 1)
        manager.close()
        restarted = self.manager(self.fake_provider(calls))
        reconciled = restarted.get(self.project, pending["id"])
        self.assertEqual(reconciled["state"], "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.library.overview()["entries"]), 1)

    def test_analysis_archived_between_validation_and_publish_rejects_whole_methods_bundle(self):
        calls = []
        manager = self.manager(self.fake_provider(calls))
        analysis = self.run_analysis(manager)
        report = analysis["result_library"]["analysis"]
        original_publish = LibraryStore.publish_method_bundle
        archived = []
        def racing_publish(library, bundle, **kwargs):
            if bundle["methods"] and not archived:
                payload = {key: report[key] for key in ("type", "title", "content", "tags", "summary", "source_refs", "analysis")}
                payload.update(id=report["id"], status="archived")
                archived.append(library.save_entry(payload, report["hash"]))
            return original_publish(library, bundle, **kwargs)
        with patch.object(LibraryStore, "publish_method_bundle", racing_publish):
            result = self.wait(manager, self.submit(manager, self.extract_request(analysis))["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.library.overview()["entries"][0]["status"], "archived")
        self.assertEqual([item for item in self.library.overview()["entries"] if item["type"] == "method"], [])
        self.assertIsNone(self.library.method_bundle(result["id"]))
        self.assertEqual(len(calls), 2)

    def test_personal_source_uses_explicit_book_budget_without_reading_story(self):
        personal = LibraryStore(self.workspace)
        self.source = personal.import_source("个人人工资料.txt", self.text.encode("utf-8"), metadata={"outbound": "excerpts"})
        secret = "另一作品私有剧情不应进入材料"
        other = ProjectStore(self.workspace).init("another", genre="campus")
        DocumentStore(other).save("drafts/private.md", secret, None)
        calls = []
        manager = self.manager(self.fake_provider(calls))
        job = self.wait(manager, self.submit(manager, self.request(library_scope="personal"))["id"])
        self.assertEqual(job["state"], "succeeded", job["error"])
        entry = job["result_library"]["analysis"]
        self.assertEqual(entry["owner"], self.project.novel_id)
        self.assertEqual(entry["source_refs"][0]["scope"], "personal")
        self.assertNotIn(secret, calls[0])
        self.assertEqual(job["novel_id"], self.project.novel_id)

    def test_cancelled_method_response_keeps_usage_and_never_publishes(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        value = self.analysis()
        class Provider:
            def generate(self, prompt, cancel):
                started.set()
                release.wait(5)
                return GenerationResult(json.dumps(value, ensure_ascii=False), {"prompt_tokens": 9, "completion_tokens": 6})
        manager = self.manager(Provider())
        submitted = self.submit(manager, self.request())
        self.assertTrue(started.wait(3))
        manager.cancel(self.project, submitted["id"])
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get(self.project, submitted["id"])
            if job["attempts"] and job["attempts"][0]["state"] == "discarded":
                break
            time.sleep(.01)
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(self.library.overview()["entries"], [])
        self.assertEqual(BudgetLedger(self.project).public(job["route"]["group_id"])["tokens"], 15)

    def test_unprovided_repeated_quote_and_bad_extraction_stage_cannot_be_reused(self):
        calls = []
        manager = self.manager(self.fake_provider(calls))
        analysis = self.run_analysis(manager)
        from vibenovel.studio.method_jobs import validate_references
        outside = self.ref()
        outside["start_char"] = self.text.rindex("每个场景")
        outside["end_char"] = len(self.text)
        with self.assertRaises(VibeNovelError):
            validate_references([outside], analysis["library_context"]["snapshot"])
        for change in ({"purpose": "suspense"}, {"analysis_version_id": "f" * 32}, {"method_run_id": "f" * 32}):
            with self.subTest(change=change), self.assertRaises(VibeNovelError):
                manager.preview(self.project, dict(self.extract_request(analysis), **change))
        self.assertEqual(len(calls), 1)

    def test_workflow_freezes_previewed_library_and_refuses_policy_aba(self):
        entry = self.adopted()
        document = DocumentStore(self.project).save("drafts/input.md", "已存在的原始情节。", None)
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                return GenerationResult("修订后的表达，事实仍待作者核对。")
        manager = self.manager(Provider())
        workflows = WorkflowManager(manager)
        request = {"kind": "rewrite", "document_path": document["path"], "document_hash": document["hash"],
                   "library_selection": {"entry_ids": [entry["id"]]}}
        preview = manager.preview(self.project, request)
        request["expected_library_hash"] = preview["library_preview"]["snapshot"]["hash"]
        flow = workflows.create(self.project, request, review=True, memory=False)
        first = self.wait(manager, flow["nodes"][0]["job_id"])
        self.assertEqual(first["state"], "succeeded")
        policy = default_policy()
        saved = self.library.save_policy(dict(policy, mode="recommend"))
        self.library.save_policy(policy, saved["hash"])
        with self.assertRaises(VibeNovelError):
            workflows.advance(self.project, flow["id"])
        self.assertEqual(len(calls), 1)

    def test_workflow_checks_visible_model_preview_before_dispatch(self):
        document = DocumentStore(self.project).save("drafts/input.md", "原有剧情", None)
        calls = []
        manager = self.manager(self.fake_provider(calls))
        request = {"kind": "rewrite", "document_path": document["path"], "document_hash": document["hash"],
                   "idempotency_key": uuid.uuid4().hex}
        preview = manager.preview(self.project, request)
        request["expected_configuration"] = self.config(preview)
        routing = ProjectRouting(self.project, self.workspace)
        policy = routing.public()
        policy["policy"]["max_calls"] = 4
        routing.save(policy["policy"], policy["hash"])
        with self.assertRaises(ConflictError):
            WorkflowManager(manager).create(self.project, request, review=False, memory=False)
        self.assertEqual(calls, [])
        self.assertEqual(manager.list(self.project), [])

    def test_normal_optional_idempotency_survives_restart_and_explicit_retry_gets_new_key(self):
        document = DocumentStore(self.project).save("drafts/input.md", "原有剧情", None)
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                if len(calls) == 1:
                    raise RuntimeError("offline failure")
                return GenerationResult("# 独立修订候选")
        manager = self.manager(Provider())
        request = {"kind": "rewrite", "document_path": document["path"], "document_hash": document["hash"],
                   "idempotency_key": uuid.uuid4().hex}
        preview = manager.preview(self.project, request)
        configuration = self.config(preview)
        first = self.wait(manager, manager.submit(self.project, request, expected_configuration=configuration)["id"])
        self.assertEqual(first["state"], "failed")
        manager.close()
        restarted = self.manager(Provider())
        same = restarted.submit(self.project, request, expected_configuration=configuration)
        self.assertEqual(same["id"], first["id"])
        retried = self.wait(restarted, restarted.retry(self.project, first["id"])["id"])
        self.assertEqual(retried["state"], "succeeded")
        self.assertNotEqual(retried["request"]["idempotency_key"], first["request"]["idempotency_key"])
        self.assertEqual(retried["route"]["group_id"], first["route"]["group_id"])
        self.assertEqual(len(calls), 2)

    def adopted(self):
        payload = {"type": "method", "title": "场景检查", "summary": "作者摘要：检查选择及代价", "content": "人工方法正文", "outbound": "summary",
                   "method": {"purpose": "场景", "steps": ["检查选择"], "inputs": ["章计划"], "outputs": ["检查结果"], "limitations": ["只作参考"]}}
        entry = self.library.save_entry(payload)
        return self.library.adopt_method(entry["id"], entry["hash"])

    def test_adopted_manual_method_assembled_in_outline_planning_review_and_rewrite(self):
        entry = self.adopted()
        document = DocumentStore(self.project).save("drafts/input.md", "角色原有事实与动作。", None)
        source = DocumentStore(self.project).save("outlines/source/input.md", "故事大纲。", None)
        brief = defaults(self.project.novel_id)
        brief["story_seed"] = "一个人修复照相馆。"
        saved = CreativeBriefStore(self.project).save(brief, None, None)
        manager = self.manager(self.fake_provider([]))
        for kind in ("expand_outline", "build_blueprint", "review", "rewrite", "generate_story_outline"):
            request = {"kind": kind, "document_path": document["path"] if kind in ("review", "rewrite") else source["path"],
                       "document_hash": document["hash"] if kind in ("review", "rewrite") else source["hash"],
                       "library_selection": {"entry_ids": [entry["id"]]}}
            if kind == "generate_story_outline":
                request.update(document_path=saved["path"], document_hash=saved["hash"], document_version_id=saved["version_id"], idempotency_key=uuid.uuid4().hex)
            prepared = manager._prepare(self.project, request)
            self.assertIn("作者摘要：检查选择及代价", prepared[1])
            self.assertNotIn("人工方法正文", prepared[1])
            self.assertIn("library_context", prepared[5])
            self.assertNotIn(entry["id"], prepared[2])

    def test_recommend_never_sends_and_auto_only_uses_adopted_authorized_methods(self):
        entry = self.adopted()
        source = DocumentStore(self.project).save("outlines/source/input.md", "场景与代价", None)
        policy = default_policy()
        policy.update(mode="recommend", required_ids=[entry["id"]])
        configured = self.library.save_policy(policy)
        request = {"kind": "expand_outline", "document_path": source["path"], "document_hash": source["hash"], "instruction": "场景检查"}
        manager = self.manager(self.fake_provider([]))
        recommend = manager._prepare(self.project, request)
        self.assertNotIn("作者摘要", recommend[1])
        self.assertIsNone(recommend[5]["library_context"]["snapshot"])
        policy["mode"] = "auto"
        self.library.save_policy(policy, configured["hash"])
        automatic = manager._prepare(self.project, request)
        self.assertIn("作者摘要", automatic[1])
        self.assertTrue(automatic[5]["library_context"]["snapshot"]["typed_deps"])
        with self.assertRaises(ConflictError):
            manager.submit(self.project, request)


if __name__ == "__main__":
    unittest.main()
