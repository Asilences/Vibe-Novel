"""Offline creation jobs: bounded planning, idempotency and source identity."""
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

from vibenovel.core.creation import CREATIVE_BRIEF_PATH, CREATION_SELECTION_PATH, CreativeBriefStore, defaults
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import read_json
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import GenerationResult
from vibenovel.studio.routing import BudgetLedger, ProjectRouting
if __package__:
    from .test_blueprints import blueprint as blueprint_fixture
else:
    from test_blueprints import blueprint as blueprint_fixture


class CreationJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.project = ProjectStore(self.workspace).init("seed", genre="campus")
        self.store = CreativeBriefStore(self.project)
        self.documents = DocumentStore(self.project)
        brief = defaults("seed")
        brief["story_seed"] = "一个失去方向的学生通过修复旧照相馆重新认识家人。"
        self.source = self.store.save(brief, None, None)
        (self.workspace / "prompts/genres").mkdir(parents=True)
        (self.workspace / "prompts/genres/campus.md").write_text("校园测试规则", encoding="utf-8")
        (self.workspace / "prompt.md").write_text("通用测试规则", encoding="utf-8")
        isolated = {key: value for key, value in os.environ.items()
                    if not key.upper().startswith("VIBENOVEL_")}
        environment = patch.dict(os.environ, isolated, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def request(self, key=None):
        return {"kind": "generate_story_outline", "document_hash": self.source["hash"],
                "document_version_id": self.source["version_id"], "idempotency_key": key or uuid.uuid4().hex}

    def manager(self, provider):
        manager = JobManager(self.workspace, provider_factory=lambda kind: provider)
        self.addCleanup(manager.close)
        return manager

    def submit(self, manager, request):
        if not hasattr(self, "configuration"):
            preview = manager.creation_preview(self.project, self.request())
            candidate = preview["candidates"][0]
            self.configuration = {"policy_revision": preview["policy_revision"], "policy_hash": preview["policy_hash"],
                                  "registry_revision": preview["registry_revision"], "registry_hash": candidate["registry_hash"],
                                  "registry_legacy_marker": candidate["legacy_marker"]}
        return manager.submit(self.project, request, expected_configuration=self.configuration)

    def wait(self, manager, identity):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = manager.get(self.project, identity)
            if job["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                return job
            time.sleep(.01)
        self.fail("offline creation job did not finish")

    def change_brief(self, text):
        brief = copy.deepcopy(self.source["brief"])
        brief["story_seed"] = text
        self.source = self.store.save(brief, self.source["hash"], self.source["version_id"])

    def select(self, manager, job, *, document=None):
        document = document or self.documents.read(job["result_path"])
        version = self.documents.history(document["path"])[0]["id"]
        origin = manager.validate_creation_candidate(self.project, document["path"], document["hash"], version)
        previous = self.store.get_selection()
        return self.store.select(document["path"], document["hash"], version,
                                 brief_hash=origin["hash"], brief_version_id=origin["version_id"], job_id=origin["job_id"],
                                 expected_hash=previous["hash"], expected_version_id=previous["version_id"])

    def test_seed_alone_generates_one_independent_planner_candidate(self):
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                return GenerationResult("# 照相馆\n\n一份待作者审阅的全书总纲。", {"prompt_tokens": 8, "completion_tokens": 4})
        manager = self.manager(Provider())
        submitted = self.submit(manager, self.request())
        job = self.wait(manager, submitted["id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["route"]["role"], "planner")
        self.assertEqual(job["result_path"], "outlines/proposals/story-outline-" + job["id"] + ".md")
        self.assertEqual(job["context_sources"], {CREATIVE_BRIEF_PATH: self.source["hash"]})
        self.assertEqual(job["source_versions"][CREATIVE_BRIEF_PATH], self.source["version_id"])
        self.assertEqual(len(calls), 1)
        self.assertIn("校园测试规则", calls[0])
        self.assertIn(self.source["brief"]["story_seed"], calls[0])
        self.assertEqual(self.documents.history(job["result_path"])[0]["provenance"], "AI_SUGGESTED")
        for directory in ("chapters", "memory", "bible", "outlines/approved"):
            self.assertFalse(any(path.is_file() for path in self.project.path(directory).rglob("*")))

    def test_preview_excludes_unaccepted_ideas_and_never_enqueues_or_reserves(self):
        brief = copy.deepcopy(self.source["brief"])
        brief["tags"] = [{"id": "custom:unaccepted", "label": "未接受AI标记", "source": "ai", "decision": "pending", "dimensions": ["plot"]}]
        brief["advantage"].update(status="no", ability="不应发送的隐藏能力")
        self.source = self.store.save(brief, self.source["hash"], self.source["version_id"])
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                return GenerationResult("candidate")
        manager = self.manager(Provider())
        request = self.request()
        request.pop("idempotency_key")
        preview = manager.creation_preview(self.project, request)
        self.assertEqual(preview["role"], "planner")
        self.assertEqual(preview["creation_preview"]["version_id"], self.source["version_id"])
        self.assertEqual(preview["creation_preview"]["effective"]["advantage"], {"status": "no"})
        prepared = manager._prepare(self.project, dict(request, idempotency_key="offline-preview-key"))
        self.assertNotIn("未接受AI标记", prepared[1])
        self.assertNotIn("不应发送的隐藏能力", prepared[1])
        self.assertEqual(calls, [])
        self.assertFalse(self.project.path(".runtime/jobs").exists())
        self.assertFalse(self.project.path(".runtime/model-budget.json").exists())

    def test_duplicate_clicks_and_restart_return_same_job_without_second_call(self):
        calls, started, release = [], threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                started.set()
                release.wait(5)
                return GenerationResult("# 独立总纲")
        manager = self.manager(Provider())
        request = self.request()
        first = self.submit(manager, request)
        self.assertTrue(started.wait(3))
        ids = []
        threads = [threading.Thread(target=lambda: ids.append(self.submit(manager, request)["id"])) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(ids, [first["id"]] * 4)
        release.set()
        self.assertEqual(self.wait(manager, first["id"])["state"], "succeeded")
        manager.close()
        resumed = self.manager(Provider())
        self.assertEqual(self.submit(resumed, request)["id"], first["id"])
        self.assertEqual(len(calls), 1)

    def test_same_key_different_request_rejected_and_new_key_keeps_old_candidate(self):
        class Provider:
            def generate(self, prompt, cancel):
                return GenerationResult("# 总纲")
        manager = self.manager(Provider())
        request = self.request()
        first = self.wait(manager, self.submit(manager, request)["id"])
        changed = dict(request, instruction="改成另一份请求")
        with self.assertRaises(ConflictError):
            self.submit(manager, changed)
        second = self.wait(manager, self.submit(manager, self.request())["id"])
        self.assertNotEqual(first["result_path"], second["result_path"])
        self.assertTrue(self.project.path(first["result_path"]).is_file())
        self.assertEqual(len(manager.list(self.project)), 2)

    def test_brief_aba_stops_queued_call_and_rejects_late_publication(self):
        calls, started, release = [], threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                started.set()
                release.wait(5)
                return GenerationResult("# 过期总纲", {"prompt_tokens": 11, "completion_tokens": 7})
        manager = self.manager(Provider())
        first = self.submit(manager, self.request())
        self.assertTrue(started.wait(3))
        queued = self.submit(manager, self.request())
        original = self.source["brief"]["story_seed"]
        original_hash = self.source["hash"]
        self.change_brief("暂时改变故事种子")
        self.change_brief(original)
        self.assertEqual(self.source["hash"], original_hash)
        release.set()
        late = self.wait(manager, first["id"])
        stopped = self.wait(manager, queued["id"])
        self.assertEqual((late["state"], stopped["state"]), ("failed", "failed"))
        self.assertIsNone(late["result_path"])
        self.assertEqual(stopped["attempts"], [])
        self.assertEqual(len(calls), 1)
        summary = BudgetLedger(self.project).public(late["route"]["group_id"])
        self.assertEqual((summary["calls"], summary["tokens"]), (1, 18))

    def test_wrong_path_version_and_invalid_idempotency_are_rejected(self):
        manager = self.manager(object())
        for changes in ({"document_path": "outlines/source/other.json"},
                        {"document_version_id": "0" * 32}, {"planning_count": 1},
                        {"idempotency_key": "short"}):
            with self.subTest(changes=changes), self.assertRaises(VibeNovelError):
                self.submit(manager, dict(self.request(), **changes))
        source = self.documents.save("outlines/source/old.md", "原有大纲", None)
        preview = manager.preview(self.project, {"kind": "expand_outline", "document_path": source["path"],
            "document_hash": source["hash"], "idempotency_key": "valid-existing-kind"})
        self.assertEqual(preview["role"], "planner")
        with self.assertRaises(VibeNovelError):
            manager.preview(self.project, {"kind": "expand_outline", "document_path": source["path"],
                "document_hash": source["hash"], "idempotency_key": "short"})

    def test_author_edited_candidate_usable_until_origin_brief_version_changes(self):
        class Provider:
            def generate(self, prompt, cancel):
                return GenerationResult("# 原版总纲")
        manager = self.manager(Provider())
        job = self.wait(manager, self.submit(manager, self.request())["id"])
        edited = self.documents.save(job["result_path"], "# 作者改过的总纲", job["result_hash"])
        version = self.documents.history(edited["path"])[0]["id"]
        origin = manager.validate_creation_candidate(self.project, edited["path"], edited["hash"], version)
        self.assertEqual(origin["version_id"], self.source["version_id"])
        self.select(manager, job, document=edited)
        prepared = manager._prepare(self.project, {"kind": "build_blueprint", "document_path": edited["path"],
                                                "document_hash": edited["hash"]})
        self.assertIn(CREATIVE_BRIEF_PATH, prepared[2])
        original = self.source["brief"]["story_seed"]
        self.change_brief("另一个故事")
        self.change_brief(original)
        self.assertTrue(self.project.path(edited["path"]).is_file())
        with self.assertRaises(ConflictError):
            manager.validate_creation_candidate(self.project, edited["path"], edited["hash"], version)
        with self.assertRaises(ConflictError):
            manager._prepare(self.project, {"kind": "build_blueprint", "document_path": edited["path"],
                                          "document_hash": edited["hash"]})

    def test_expanding_generated_outline_keeps_original_brief_history_dependency(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Provider:
            def generate(self, prompt, cancel):
                if "完整输出JSON Schema" in prompt:
                    started.set()
                    release.wait(5)
                    return GenerationResult("not a blueprint; stale source must fail before repair")
                return GenerationResult("# 初版总纲")
        manager = self.manager(Provider())
        outline = self.wait(manager, self.submit(manager, self.request())["id"])
        selected = self.select(manager, outline)
        blueprint = manager.submit(self.project, {"kind": "build_blueprint", "document_path": outline["result_path"],
                                                "document_hash": outline["result_hash"]})
        self.assertTrue(started.wait(3))
        self.assertEqual(blueprint["source_versions"][CREATIVE_BRIEF_PATH], self.source["version_id"])
        self.assertEqual(blueprint["source_versions"][CREATION_SELECTION_PATH], selected["version_id"])
        self.assertEqual(blueprint["creation_origin"]["job_id"], outline["id"])
        original = self.source["brief"]["story_seed"]
        self.change_brief("不能复活总纲来源")
        self.change_brief(original)
        release.set()
        result = self.wait(manager, blueprint["id"])
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["result_path"])
        self.assertNotIn("repair_candidate", result)

    def test_generated_outline_requires_exact_explicit_selection_before_expansion(self):
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                return GenerationResult("# 初版总纲")
        manager = self.manager(Provider())
        outline = self.wait(manager, self.submit(manager, self.request())["id"])
        request = {"kind": "build_blueprint", "document_path": outline["result_path"],
                   "document_hash": outline["result_hash"]}
        with self.assertRaises(ConflictError):
            manager.submit(self.project, request)
        manual = self.documents.save("outlines/source/manual.md", "# 作者另一版总纲", None)
        self.store.select(manual["path"], manual["hash"], self.documents.history(manual["path"])[0]["id"],
                          expected_hash=None, expected_version_id=None)
        with self.assertRaises(ConflictError):
            manager.submit(self.project, request)
        selected = self.select(manager, outline)
        prepared = manager._prepare(self.project, request)
        self.assertEqual(prepared[2][CREATION_SELECTION_PATH], selected["hash"])
        self.assertEqual(len(calls), 1)

    def test_changed_selection_blocks_queued_call_and_late_blueprint_publication(self):
        calls, started, release = [], threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                if "完整输出JSON Schema" in prompt:
                    started.set()
                    release.wait(5)
                    return GenerationResult(json.dumps(blueprint_fixture(), ensure_ascii=False))
                return GenerationResult("# 初版总纲")
        manager = self.manager(Provider())
        outline = self.wait(manager, self.submit(manager, self.request())["id"])
        selected = self.select(manager, outline)
        original_record = self.documents.read(CREATION_SELECTION_PATH)
        request = {"kind": "build_blueprint", "document_path": outline["result_path"],
                   "document_hash": outline["result_hash"]}
        running = manager.submit(self.project, request)
        self.assertTrue(started.wait(3))
        queued = manager.submit(self.project, request)
        manual = self.documents.save("outlines/source/manual.md", "# 改选其他总纲", None)
        changed = self.store.select(manual["path"], manual["hash"], self.documents.history(manual["path"])[0]["id"],
                                    expected_hash=selected["hash"], expected_version_id=selected["version_id"])
        restored = self.documents.save(CREATION_SELECTION_PATH, original_record["content"], changed["hash"])
        self.assertEqual(restored["hash"], original_record["hash"])
        release.set()
        late = self.wait(manager, running["id"])
        stopped = self.wait(manager, queued["id"])
        self.assertEqual((late["state"], stopped["state"]), ("failed", "failed"))
        self.assertIsNone(late["result_path"])
        self.assertEqual(stopped["attempts"], [])
        self.assertEqual(len(calls), 2)

    def test_saved_blueprint_pending_intent_does_not_recover_after_brief_aba(self):
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                if "完整输出JSON Schema" in prompt:
                    return GenerationResult(json.dumps(blueprint_fixture(), ensure_ascii=False))
                return GenerationResult("# 初版总纲")
        manager = self.manager(Provider())
        outline = self.wait(manager, self.submit(manager, self.request())["id"])
        self.select(manager, outline)
        persist, failed_once = manager._persist, []
        def fail_completion(project, job):
            if job["kind"] == "build_blueprint" and job["state"] == "succeeded" and not failed_once:
                failed_once.append(True)
                raise OSError("offline final job persistence failure")
            return persist(project, job)
        with patch.object(manager, "_persist", side_effect=fail_completion):
            pending = self.wait(manager, manager.submit(self.project, {"kind": "build_blueprint",
                "document_path": outline["result_path"], "document_hash": outline["result_hash"]})["id"])
        self.assertTrue(failed_once)
        self.assertEqual(pending["state"], "interrupted")
        self.assertTrue(self.project.path(pending["result_path"]).is_file())
        self.assertTrue(pending["recovery_available"])
        old_hash, original = self.source["hash"], self.source["brief"]["story_seed"]
        self.change_brief("brief ABA after candidate already saved")
        self.change_brief(original)
        self.assertEqual(old_hash, self.source["hash"])
        manager.close()
        resumed = self.manager(Provider())
        recovered = resumed.get(self.project, pending["id"])
        self.assertEqual(recovered["state"], "interrupted")
        with self.assertRaises(VibeNovelError):
            resumed.recover_result(self.project, pending["id"])
        self.assertEqual(len(calls), 2)

    def test_publication_recovery_never_repeats_call_and_stale_brief_blocks_recovery(self):
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                return GenerationResult("# 保留的候选")
        manager = self.manager(Provider())
        save = DocumentStore.save
        def fail_candidate(store, path, *args, **kwargs):
            if path.startswith("outlines/proposals/story-outline-"):
                raise OSError("offline publication failure")
            return save(store, path, *args, **kwargs)
        with patch.object(DocumentStore, "save", fail_candidate):
            job = self.wait(manager, self.submit(manager, self.request())["id"])
        self.assertEqual(job["state"], "interrupted")
        self.assertTrue(job["recovery_available"])
        self.change_brief("改变后不应发布旧候选")
        manager.close()
        resumed = self.manager(Provider())
        restored = resumed.get(self.project, job["id"])
        self.assertEqual(restored["state"], "interrupted")
        with self.assertRaises(VibeNovelError):
            resumed.recover_result(self.project, job["id"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.project.path("outlines/proposals/story-outline-" + job["id"] + ".md").exists())

    def test_failed_outline_requires_explicit_new_key_instead_of_generic_retry(self):
        class Provider:
            def generate(self, prompt, cancel):
                raise RuntimeError("offline model error")
        manager = self.manager(Provider())
        request = self.request()
        job = self.wait(manager, self.submit(manager, request)["id"])
        self.assertEqual(job["state"], "failed")
        with self.assertRaises(VibeNovelError):
            manager.retry(self.project, job["id"])
        self.assertEqual(self.submit(manager, request)["id"], job["id"])
        disk = read_json(self.project.path(".runtime/jobs/" + job["id"] + ".json"))
        self.assertEqual(len(disk["attempts"]), 1)

    def test_submit_requires_preview_and_rejects_changed_preview_policy(self):
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                return GenerationResult("# 总纲")
        manager = self.manager(Provider())
        request = self.request()
        with self.assertRaises(ConflictError):
            manager.submit(self.project, request)
        preview = manager.creation_preview(self.project, request)
        candidate = preview["candidates"][0]
        configuration = {"policy_revision": preview["policy_revision"], "policy_hash": preview["policy_hash"],
                         "registry_revision": preview["registry_revision"], "registry_hash": candidate["registry_hash"],
                         "registry_legacy_marker": candidate["legacy_marker"]}
        routing = ProjectRouting(self.project, self.workspace)
        policy = routing.public()
        policy["policy"]["max_calls"] = 2
        routing.save(policy["policy"], policy["hash"])
        with self.assertRaises(ConflictError):
            manager.submit(self.project, request, expected_configuration=configuration)
        self.assertEqual(calls, [])
        self.assertEqual(manager.list(self.project), [])

    def test_queued_policy_aba_does_not_reactivate_preview(self):
        calls, started, release = [], threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                started.set()
                release.wait(5)
                return GenerationResult("# 已发送的冻结候选")
        manager = self.manager(Provider())
        first = self.submit(manager, self.request())
        self.assertTrue(started.wait(3))
        queued = self.submit(manager, self.request())
        routing = ProjectRouting(self.project, self.workspace)
        policy = routing.public()
        original = copy.deepcopy(policy["policy"])
        policy["policy"]["max_calls"] = 2
        changed = routing.save(policy["policy"], policy["hash"])
        routing.save(original, changed["hash"])
        release.set()
        self.wait(manager, first["id"])
        stopped = self.wait(manager, queued["id"])
        self.assertEqual(stopped["state"], "failed")
        self.assertEqual(stopped["attempts"], [])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
