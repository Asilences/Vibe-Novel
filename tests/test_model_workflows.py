"""Candidate workflows make each paid continuation explicit and recover safely."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import json_bytes, read_json
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.model_registry import ModelRegistry, ROLES
from vibenovel.studio.providers import GenerationResult
from vibenovel.studio.routing import BudgetLedger, ProjectRouting, default_policy
from vibenovel.studio.workflows import WorkflowManager


TEXT = "# 第一章\n他推门。房内静。\n"


class ScriptedProvider:
    def __init__(self):
        self.calls = 0
        self.responses = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def generate(self, prompt, cancel):
        self.calls += 1
        self.started.set()
        if not self.release.wait(5):
            raise RuntimeError("test wait exceeded")
        text = self.responses.pop(0) if self.responses else TEXT
        return GenerationResult(text, {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}, "test-model")


class ModelWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.source = self.documents.save("drafts/old.md", "原来的章稿。", None)
        self.registry = ModelRegistry(self.workspace)
        self.registry.save({"profiles": [self.profile("writer"), self.profile("reviewer", model="review-model")]}, None)
        self.policy = ProjectRouting(self.project, self.workspace)
        policy = default_policy()
        policy.update(max_calls=9, max_tokens=5_000_000)
        policy["bindings"] = {role: {"primary": "reviewer" if role == "reviewer" else "writer", "fallbacks": []} for role in ROLES}
        self.policy.save(policy, None)
        self.provider = ScriptedProvider()
        self.jobs = self.manager(self.provider)
        self.flows = WorkflowManager(self.jobs)
        self.before = self.project.fingerprint()

    @staticmethod
    def profile(identity, model="test-model"):
        return {"id": identity, "label": identity, "base_url": "https://example.invalid/v1", "model": model,
                "api_key": "offline-test-key", "allow_loopback": False, "roles": list(ROLES),
                "context_tokens": 1_000_000, "max_output_tokens": 4096,
                "price_input_usd": 1, "price_output_usd": 2}

    def manager(self, provider):
        manager = JobManager(self.workspace, provider_factory=lambda name: provider)
        manager.start()
        self.addCleanup(manager.close)
        self.addCleanup(provider.release.set)
        return manager

    def request(self, **changes):
        request = {"kind": "rewrite", "provider": "api", "document_path": self.source["path"],
                   "document_hash": self.source["hash"], "instruction": "保留事实，调整表达。",
                   "editorial": {"goals": ["preserve_voice"], "intensity": "conservative"}}
        request.update(changes)
        return request

    def wait(self, flow):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.flows.get(self.project, flow["id"])
            if current["state"] not in ("queued", "running"):
                return current
            time.sleep(.01)
        self.fail("workflow did not finish: %r" % current)

    def review(self, quote="他推门。", text=TEXT, issues=None):
        start = text.index(quote)
        issue = {"id": "issue-1", "severity": "warning", "category": "表达", "quote": quote,
                 "start_char": start, "end_char": start + len(quote), "explanation": "可斟酌动作表达。",
                 "suggestion": "由作者选择是否修改。"}
        return json.dumps({"schema_version": 1, "issues": issues if issues is not None else [issue], "unchecked": ["未检查其他章。"]}, ensure_ascii=False)

    @staticmethod
    def memory():
        return json.dumps({"schema_version": 1, "items": [{"id": "event-1", "kind": "event", "statement": "候选中人物推门。",
                    "quote": "他推门。", "start_char": TEXT.index("他推门。"), "end_char": TEXT.index("他推门。") + 4}],
                    "unchecked": ["候选未经作者定稿，事件不构成正式事实。"]}, ensure_ascii=False)

    def first_reviewed(self, **create_options):
        first = self.wait(self.flows.create(self.project, self.request(), **create_options))
        self.assertEqual(first["state"], "awaiting_advance", first)
        self.provider.responses.append(self.review())
        reviewed = self.wait(self.flows.advance(self.project, first["id"]))
        self.assertEqual(reviewed["state"], "awaiting_advance", reviewed)
        return reviewed

    def test_three_node_candidate_loop_only_pays_on_explicit_advance(self):
        flow = self.wait(self.flows.create(self.project, self.request(), independent_review=True))
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(flow["state"], "awaiting_advance")
        for _ in range(3):
            self.flows.list(self.project)
            self.flows.get(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 1)
        self.provider.responses.append(self.review())
        flow = self.wait(self.flows.advance(self.project, flow["id"]))
        self.assertEqual(self.provider.calls, 2)
        self.provider.responses.append(self.memory())
        flow = self.wait(self.flows.advance(self.project, flow["id"]))
        self.assertEqual(flow["state"], "ready", flow)
        self.assertEqual(self.provider.calls, 3)
        self.assertEqual(flow["budget_summary"]["calls"], 3)
        self.assertEqual(BudgetLedger(self.project).public(flow["id"])["tokens"], 9)
        self.assertEqual(self.project.fingerprint(), self.before)
        self.assertEqual(list(self.project.path("memory").rglob("*.json")), [])
        self.assertEqual(list(self.project.path("chapters").glob("*.md")), [])
        for node in flow["nodes"][1:]:
            artifact = json.loads(self.documents.read(node["result_path"])["content"])
            self.assertEqual(artifact["status"], "candidate")
            self.assertTrue(node["result_path"].startswith("reviews/"))
        with self.assertRaises(VibeNovelError):
            self.flows.advance(self.project, flow["id"])

    def test_draft_workflow_requires_applied_chapter_plan(self):
        request = self.request(kind="draft")
        request.pop("editorial")
        with self.assertRaises(VibeNovelError):
            self.flows.create(self.project, request)
        self.assertEqual(self.provider.calls, 0)
        approvals = ApprovalStore(self.project)
        proposal = approvals.create("outlines/approved/ch001.md", "第一章：人物开门后作出决定。", "临时测试", ["规划"], None)
        approvals.decide(proposal["id"], "approve")
        request.update(plan_path=proposal["target"], plan_hash=proposal["after_hash"])
        with self.assertRaises(VibeNovelError):
            self.flows.create(self.project, request)
        approvals.apply(proposal["id"])
        flow = self.wait(self.flows.create(self.project, request))
        self.assertEqual(flow["state"], "awaiting_advance", flow)
        self.assertEqual(self.provider.calls, 1)

    def test_budget_is_shared_across_all_nodes_and_extra_revision_cannot_call(self):
        current = self.policy.public()
        policy = current["policy"]
        policy["max_calls"] = 3
        self.policy.save(policy, current["hash"])
        reviewed = self.first_reviewed()
        self.provider.responses.append(self.memory())
        ready = self.wait(self.flows.advance(self.project, reviewed["id"]))
        self.assertEqual(ready["state"], "ready")
        stopped = self.wait(self.flows.revise(self.project, ready["id"], ["issue-1"]))
        self.assertEqual(stopped["state"], "failed")
        self.assertEqual(self.provider.calls, 3)
        self.assertEqual(stopped["budget_summary"]["calls"], 3)

    def test_restart_does_not_start_successor_and_can_explicitly_continue(self):
        flow = self.wait(self.flows.create(self.project, self.request()))
        self.jobs.close()
        replacement = ScriptedProvider()
        self.jobs = self.manager(replacement)
        self.flows = WorkflowManager(self.jobs)
        recovered = self.flows.get(self.project, flow["id"])
        self.assertEqual(recovered["state"], "awaiting_advance")
        self.assertEqual(replacement.calls, 0)
        replacement.responses.append(self.review())
        reviewed = self.wait(self.flows.advance(self.project, flow["id"]))
        self.assertEqual(reviewed["state"], "awaiting_advance")
        self.assertEqual(replacement.calls, 1)

    def test_source_round_trip_and_original_input_drift_block_successors(self):
        flow = self.wait(self.flows.create(self.project, self.request()))
        changed = self.documents.save(self.source["path"], "来源改变", self.source["hash"])
        self.documents.save(self.source["path"], self.source["content"], changed["hash"])
        view = self.flows.get(self.project, flow["id"])
        self.assertTrue(view["stale"])
        self.assertIn("历史版本", view["drift"])
        with self.assertRaises(VibeNovelError):
            self.flows.advance(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 1)

    def test_approved_plan_change_after_draft_prevents_review_continuation(self):
        approvals = ApprovalStore(self.project)
        proposal = approvals.create("outlines/approved/ch001.md", "人物开门。", "临时计划", ["规划"], None)
        approvals.decide(proposal["id"], "approve")
        approvals.apply(proposal["id"])
        request = self.request(kind="draft", plan_path=proposal["target"], plan_hash=proposal["after_hash"])
        request.pop("editorial")
        flow = self.wait(self.flows.create(self.project, request))
        next_plan = approvals.create(proposal["target"], "人物留在门外。", "改变计划", ["规划"], proposal["after_hash"])
        approvals.decide(next_plan["id"], "approve")
        approvals.apply(next_plan["id"])
        self.assertTrue(self.flows.get(self.project, flow["id"])["stale"])
        with self.assertRaises(VibeNovelError):
            self.flows.advance(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 1)

    def test_output_round_trip_blocks_old_evidence_and_continuation(self):
        flow = self.wait(self.flows.create(self.project, self.request()))
        writer = flow["nodes"][0]
        doc = self.documents.read(writer["result_path"])
        changed = self.documents.save(doc["path"], "另一版输出", doc["hash"])
        self.documents.save(doc["path"], doc["content"], changed["hash"])
        self.assertTrue(self.flows.get(self.project, flow["id"])["stale"])
        with self.assertRaises(VibeNovelError):
            self.flows.advance(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 1)

    def test_model_configuration_round_trip_is_stale_without_more_calls(self):
        flow = self.wait(self.flows.create(self.project, self.request()))
        current = self.registry.public()
        profiles = current["profiles"]
        profiles[0]["label"] = "改名"
        changed = self.registry.save({"profiles": profiles}, current["hash"])
        profiles[0]["label"] = "writer"
        self.registry.save({"profiles": profiles}, changed["hash"])
        self.assertTrue(self.flows.get(self.project, flow["id"])["stale"])
        with self.assertRaises(VibeNovelError):
            self.flows.advance(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 1)

    def test_different_profile_alias_for_same_endpoint_model_is_not_independent(self):
        current = self.registry.public()
        profiles = current["profiles"]
        profiles[1]["model"] = profiles[0]["model"]
        profiles[1]["base_url"] = "https://EXAMPLE.INVALID:443/v1/"
        profiles[1]["api_key"] = "offline-test-key"
        self.registry.save({"profiles": profiles}, current["hash"])
        flow = self.wait(self.flows.create(self.project, self.request(), independent_review=True))
        with self.assertRaisesRegex(VibeNovelError, "别名"):
            self.flows.advance(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 1)

    def test_local_revision_uses_evidence_range_and_stops_after_two_rounds(self):
        flow = self.first_reviewed()
        for round_number, sentence in enumerate(("他叩门。", "他停步。"), 1):
            self.provider.responses.append(sentence)
            revised = self.wait(self.flows.revise(self.project, flow["id"], ["issue-1"], "只修改这个动作。"))
            self.assertEqual(revised["revision_rounds"], round_number)
            request = self.jobs.get(self.project, revised["nodes"][-1]["job_id"])["request"]
            self.assertEqual(request["selection"], {"start_char": TEXT.index("他推门。"), "end_char": TEXT.index("他推门。") + 4})
            new_text = TEXT.replace("他推门。", sentence)
            self.assertEqual(self.documents.read(revised["nodes"][-1]["result_path"])["content"], new_text)
            self.provider.responses.append(self.review(quote=sentence, text=new_text))
            flow = self.wait(self.flows.advance(self.project, flow["id"]))
        with self.assertRaisesRegex(VibeNovelError, "最多两轮"):
            self.flows.revise(self.project, flow["id"], ["issue-1"])
        self.assertEqual(self.project.fingerprint(), self.before)

    def test_disjoint_evidence_ranges_do_not_expand_edit_into_unselected_text(self):
        first = self.wait(self.flows.create(self.project, self.request()))
        issues = []
        for identity, quote in (("one", "他"), ("two", "静")):
            start = TEXT.index(quote)
            issues.append({"id": identity, "severity": "info", "category": "表达", "quote": quote,
                "start_char": start, "end_char": start + len(quote), "explanation": "复核。", "suggestion": "按作者偏好。"})
        self.provider.responses.append(self.review(issues=issues))
        flow = self.wait(self.flows.advance(self.project, first["id"]))
        with self.assertRaisesRegex(VibeNovelError, "不连续"):
            self.flows.revise(self.project, flow["id"], ["one", "two"])
        with self.assertRaises(VibeNovelError):
            self.flows.revise(self.project, flow["id"], ["missing"])
        self.assertEqual(self.provider.calls, 2)

    def test_pause_waiting_resume_and_cancel_never_start_hidden_successors(self):
        flow = self.wait(self.flows.create(self.project, self.request()))
        self.assertEqual(self.flows.stop(self.project, flow["id"], "pause")["state"], "paused")
        self.assertEqual(self.flows.get(self.project, flow["id"])["state"], "paused")
        self.assertEqual(self.provider.calls, 1)
        self.provider.responses.append(self.review())
        reviewed = self.wait(self.flows.advance(self.project, flow["id"]))
        self.assertEqual(reviewed["state"], "awaiting_advance")
        self.assertEqual(self.flows.stop(self.project, flow["id"], "cancel")["state"], "cancelled")
        with self.assertRaises(VibeNovelError):
            self.flows.advance(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 2)

    def test_dispatch_crash_after_submit_reconciles_exact_link_without_resending(self):
        original = self.flows._save
        calls = 0

        def crash_after_attach(project, flow):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("crash after job submit")
            return original(project, flow)

        with patch.object(self.flows, "_save", side_effect=crash_after_attach), self.assertRaises(OSError):
            self.flows.create(self.project, self.request())
        records = self.flows.list(self.project)["workflows"]
        self.assertEqual(len(records), 1)
        recovered = self.wait(records[0])
        self.assertEqual(len(recovered["nodes"]), 1)
        self.assertEqual(recovered["state"], "awaiting_advance")
        self.assertEqual(self.provider.calls, 1)

    def test_policy_change_between_dispatch_check_and_submit_never_sends_new_route(self):
        original = self.jobs.preview
        previews = 0

        def change_policy_after_check(project, request):
            nonlocal previews
            previews += 1
            if previews == 2:  # create's first preview preceded the frozen flow
                state = self.policy.public()
                state["policy"]["bindings"]["writer"]["primary"] = "reviewer"
                self.policy.save(state["policy"], state["hash"])
            return original(project, request)

        with patch.object(self.jobs, "preview", side_effect=change_policy_after_check):
            with self.assertRaisesRegex(VibeNovelError, "提交期间变化"):
                self.flows.create(self.project, self.request(), review=False, memory=False)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.jobs.list(self.project), [])
        self.assertFalse(self.project.path(".runtime/model-budget.json").exists())

    def test_registry_change_between_dispatch_check_and_submit_never_sends_new_model(self):
        original = self.jobs.submit

        def change_registry_before_submit(project, request, **kwargs):
            state = self.registry.public()
            state["profiles"][0]["model"] = "new-unapproved-model"
            self.registry.save({"profiles": state["profiles"]}, state["hash"])
            return original(project, request, **kwargs)

        with patch.object(self.jobs, "submit", side_effect=change_registry_before_submit):
            with self.assertRaisesRegex(VibeNovelError, "提交期间变化"):
                self.flows.create(self.project, self.request(), review=False, memory=False)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.jobs.list(self.project), [])

    def test_queued_workflow_policy_change_stops_before_another_paid_call(self):
        self.provider.release.clear()
        first = self.jobs.submit(self.project, self.request())
        self.assertTrue(self.provider.started.wait(3))
        flow = self.flows.create(self.project, self.request(), review=False, memory=False)
        job = self.jobs.get(self.project, flow["nodes"][0]["job_id"])
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["expected_configuration"]["policy_revision"], flow["policy_revision"])
        state = self.policy.public()
        state["policy"]["bindings"]["writer"]["primary"] = "reviewer"
        self.policy.save(state["policy"], state["hash"])
        self.provider.release.set()
        failed = self.wait(flow)
        self.assertEqual(failed["state"], "failed")
        job = self.jobs.get(self.project, job["id"])
        self.assertEqual(job["attempts"], [])
        self.assertEqual(self.provider.calls, 1)
        self.assertIsNone(BudgetLedger(self.project).public(flow["id"]))

    def test_uncertain_missing_dispatch_is_not_overwritten_by_previous_success(self):
        flow = self.wait(self.flows.create(self.project, self.request()))
        path = self.flows._path(self.project, flow["id"])
        record = read_json(path)
        record["dispatching"] = 1
        path.write_bytes(json_bytes(record))
        for _ in range(2):
            failed = self.flows.get(self.project, flow["id"])
            self.assertEqual(failed["state"], "failed")
            self.assertTrue(failed["dispatch_uncertain"])
        with self.assertRaises(VibeNovelError):
            self.flows.advance(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 1)

    def test_duplicate_dispatch_links_stop_instead_of_picking_one_or_replaying(self):
        flow = self.wait(self.flows.create(self.project, self.request()))
        path = self.flows._path(self.project, flow["id"])
        record = read_json(path)
        record["dispatching"] = 1
        path.write_bytes(json_bytes(record))
        ambiguous = {"workflow_id": flow["id"], "workflow_node": 1, "kind": "review_evidence"}
        with patch.object(self.jobs, "list", return_value=[ambiguous, copy.deepcopy(ambiguous)]):
            failed = self.flows.get(self.project, flow["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["dispatch_uncertain"])
        self.assertEqual(self.provider.calls, 1)

    def test_running_pause_discards_late_candidate_without_automatic_retry(self):
        self.provider.release.clear()
        flow = self.flows.create(self.project, self.request())
        self.assertTrue(self.provider.started.wait(5))
        stopped = self.flows.stop(self.project, flow["id"], "pause")
        self.assertEqual(stopped["state"], "paused")
        self.provider.release.set()
        with self.assertRaises(VibeNovelError):
            self.flows.advance(self.project, flow["id"])
        self.assertEqual(self.provider.calls, 1)
        self.assertIsNone(self.jobs.get(self.project, flow["nodes"][0]["job_id"])["result_path"])

    def test_generic_job_retry_cannot_restart_cancelled_workflow_node(self):
        self.provider.release.clear()
        flow = self.flows.create(self.project, self.request())
        self.assertTrue(self.provider.started.wait(5))
        stopped = self.flows.stop(self.project, flow["id"], "cancel")
        self.assertEqual(stopped["state"], "cancelled")
        self.provider.release.set()
        with self.assertRaisesRegex(VibeNovelError, "工作流节点"):
            self.jobs.retry(self.project, flow["nodes"][0]["job_id"])
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(len(self.jobs.list(self.project)), 1)

    def assert_pending_artifact_source_drift_survives_restart(self, kind):
        quote = self.source["content"]
        item = {"id": "source-1", "quote": quote, "start_char": 0, "end_char": len(quote)}
        if kind == "review_evidence":
            item.update(severity="info", category="表达", explanation="候选仅供复核。", suggestion="由作者决定。")
        else:
            item.update(kind="event", statement="只记录候选中的表达，不认定正式事实。")
        self.provider.responses.append(json.dumps({"schema_version": 1, "issues" if kind == "review_evidence" else "items": [item],
                                                   "unchecked": ["未检查其他来源。"]}, ensure_ascii=False))
        request = {"kind": kind, "provider": "api", "document_path": self.source["path"], "document_hash": self.source["hash"]}
        save = DocumentStore.save

        def fail_publication(store, path, content, expected_hash, provenance="AUTHOR", **kwargs):
            if provenance == "AI_SUGGESTED":
                raise OSError("simulated crash after durable artifact intent")
            return save(store, path, content, expected_hash, provenance, **kwargs)

        with patch.object(DocumentStore, "save", fail_publication):
            submitted = self.jobs.submit(self.project, request)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                interrupted = self.jobs.get(self.project, submitted["id"])
                if interrupted["state"] == "interrupted":
                    break
                time.sleep(.01)
            else:
                self.fail("artifact did not preserve its publication intent: %r" % interrupted)
        canonical = interrupted["recovery_candidate"]
        self.assertTrue(interrupted["recovery_available"])
        self.assertEqual(json.loads(canonical)["target_hash"], self.source["hash"])
        self.jobs.close()
        changed = self.documents.save(self.source["path"], "作者临时修改来源。", self.source["hash"])
        self.documents.save(self.source["path"], self.source["content"], changed["hash"])
        replacement = ScriptedProvider()
        # Startup revalidates the old artifact; a stale one must not block the
        # whole Studio or be mistaken for permission to call the provider again.
        self.jobs = self.manager(replacement)
        recovered = self.jobs.get(self.project, submitted["id"])
        self.assertEqual(recovered["state"], "interrupted")
        self.assertTrue(recovered["recovery_available"])
        self.assertEqual(recovered["recovery_candidate"], canonical)
        self.assertIn("失效", recovered["error"])
        self.assertEqual(replacement.calls, 0)
        self.assertEqual(len(self.jobs.list(self.project)), 1)
        persisted = read_json(self.project.path(".runtime/jobs/" + submitted["id"] + ".json"))
        self.assertEqual(persisted["_pending_result"]["content"], canonical)
        self.assertFalse(self.project.path(persisted["_pending_result"]["path"]).exists())
        with self.assertRaises(VibeNovelError):
            self.jobs.recover_result(self.project, submitted["id"])
        self.assertEqual(replacement.calls, 0)

    def test_pending_review_target_round_trip_preserves_text_and_does_not_block_startup(self):
        self.assert_pending_artifact_source_drift_survives_restart("review_evidence")

    def test_pending_memory_target_round_trip_preserves_text_and_does_not_block_startup(self):
        self.assert_pending_artifact_source_drift_survives_restart("memory_delta")


if __name__ == "__main__":
    unittest.main()
