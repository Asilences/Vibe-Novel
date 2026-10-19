"""Offline lifecycle, stale-publication and real HTTP protocol contract tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import json_bytes, read_json
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import (GenerationResult, OpenAICompatibleProvider, ProviderError,
                                       capabilities)
from vibenovel.studio.routing import ProjectRouting, default_policy


class FakeProvider:
    def __init__(self, *, blocked=False, error=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.prompt = None
        self.error = error
        if not blocked:
            self.release.set()

    def generate(self, prompt, cancel):
        self.calls += 1
        self.prompt = prompt
        self.started.set()
        if not self.release.wait(5):
            raise RuntimeError("test provider wait exceeded")
        if self.error:
            raise ProviderError("API 请求失败（HTTP 429）；未自动重试")
        # Deliberately ignore cancellation here: manager must suppress late output.
        return GenerationResult("# 候选\n仅测试文本", {"prompt_tokens": 11, "completion_tokens": 7,
                                                   "total_tokens": 18})


class StudioJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.source = self.documents.save("outlines/source/imported.md", "待扩展的来源材料", None)
        (self.workspace / "prompts" / "genres").mkdir(parents=True)
        (self.workspace / "prompts" / "genres" / "campus.md").write_text("校园题材规则", encoding="utf-8")
        (self.workspace / "prompt.md").write_text("通用写作规则", encoding="utf-8")

    def manager(self, provider=None, **kwargs):
        provider = provider or FakeProvider()
        manager = JobManager(self.workspace, provider_factory=lambda name: provider, **kwargs)
        manager.start()
        self.addCleanup(manager.close)
        self.addCleanup(provider.release.set)
        return manager, provider

    def request(self, **kwargs):
        result = {"kind": "expand_outline", "provider": "api", "document_path": self.source["path"],
                  "document_hash": self.source["hash"], "instruction": "列出三个场景"}
        result.update(kwargs)
        return result

    def wait_state(self, manager, job_id, states):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            job = manager.get(self.project, job_id)
            if job["state"] in states:
                return job
            time.sleep(.01)
        self.fail("Job failed to reach %s: %r" % (states, job))

    def approve_plan(self):
        approvals = ApprovalStore(self.project)
        proposal = approvals.create("outlines/approved/ch001.md", "作者批准的章计划", "测试审批", ["测试"], None)
        approvals.decide(proposal["id"], "approve")
        approvals.apply(proposal["id"])
        return self.documents.read(proposal["target"])

    def test_background_candidate_history_usage_and_project_isolation(self):
        manager, provider = self.manager()
        baseline = self.project.fingerprint()
        job = manager.submit(self.project, self.request())
        result = self.wait_state(manager, job["id"], {"succeeded", "failed"})
        self.assertEqual(result["state"], "succeeded", result)
        self.assertTrue(result["result_path"].startswith("outlines/proposals/expanded-"))
        self.assertEqual(self.documents.read(result["result_path"])["content"], "# 候选\n仅测试文本")
        self.assertEqual(self.documents.history(result["result_path"])[0]["provenance"], "AI_SUGGESTED")
        self.assertEqual(result["usage"]["total_tokens"], 18)
        self.assertIsNone(result["cost_usd"])
        self.assertNotIn("_prompt", result)
        self.assertEqual(self.project.fingerprint(), baseline)
        self.assertIn("校园题材规则", provider.prompt)
        self.assertIn("不可信来源数据", provider.prompt)
        self.assertEqual(provider.calls, 1)
        other = ProjectStore(self.workspace).init("other", genre="adventure")
        self.assertEqual(manager.list(other), [])
        with self.assertRaises(VibeNovelError):
            manager.get(other, job["id"])
        with self.assertRaises(VibeNovelError):
            manager.get(self.project, "../escape")

    def test_source_and_canonical_changes_prevent_late_candidate_publication(self):
        for changed in ("source", "canonical"):
            with self.subTest(changed=changed):
                manager, provider = self.manager(FakeProvider(blocked=True))
                source = self.documents.read(self.source["path"])
                job = manager.submit(self.project, self.request(document_hash=source["hash"]))
                self.assertTrue(provider.started.wait(2))
                if changed == "source":
                    self.documents.save(source["path"], "作者改过材料", source["hash"])
                else:
                    self.project.path("bible/changed.md").write_bytes(b"new canonical fact")
                provider.release.set()
                final = self.wait_state(manager, job["id"], {"failed"})
                self.assertIsNone(final["result_path"])
                self.assertEqual(list(self.project.path("outlines/proposals").glob("expanded-*")), [])
                manager.close()

    def test_atomic_save_guard_rejects_change_between_precheck_and_write(self):
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        original = DocumentStore.save

        def racing_save(store, path, content, expected_hash, provenance="AUTHOR", **kwargs):
            if provenance == "AI_SUGGESTED":
                store.project.path("bible/racing.md").write_bytes(b"concurrent change")
            return original(store, path, content, expected_hash, provenance, **kwargs)

        with patch.object(DocumentStore, "save", racing_save):
            provider.release.set()
            final = self.wait_state(manager, job["id"], {"failed"})
        self.assertIsNone(final["result_path"])
        self.assertEqual(list(self.project.path("outlines/proposals").glob("expanded-*")), [])

    def test_observed_canonical_A_B_A_invalidates_running_result_by_revision(self):
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        fingerprint = self.project.fingerprint()
        changed = self.project.path("bible/transient.md")
        changed.write_bytes(b"observed intermediate canonical change")
        self.documents.read(self.source["path"])
        changed.unlink()
        self.documents.read(self.source["path"])
        self.assertEqual(self.project.fingerprint(), fingerprint)
        provider.release.set()
        final = self.wait_state(manager, job["id"], {"failed"})
        self.assertIsNone(final["result_path"])
        self.assertEqual(list(self.project.path("outlines/proposals").iterdir()), [])

    def test_two_workers_run_and_publish_independent_candidates_without_duplicate_calls(self):
        manager, provider = self.manager(FakeProvider(blocked=True), concurrency=2)
        first = manager.submit(self.project, self.request())
        second = manager.submit(self.project, self.request())
        deadline = time.monotonic() + 2
        while provider.calls != 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(provider.calls, 2)
        provider.release.set()
        for job in (first, second):
            final = self.wait_state(manager, job["id"], {"succeeded", "failed"})
            self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(len(list(self.project.path("outlines/proposals").glob("expanded-*"))), 2)
        self.assertEqual(provider.calls, 2)

    def test_drafting_requires_current_applied_plan_and_creates_only_candidate(self):
        manager, provider = self.manager()
        with self.assertRaises(VibeNovelError):
            manager.submit(self.project, self.request(kind="draft"))
        self.project.path("outlines/approved/ch002.md").write_bytes(b"not approved")
        with self.assertRaises(VibeNovelError):
            manager.submit(self.project, self.request(kind="draft", plan_path="outlines/approved/ch002.md",
                                                      plan_hash=self.documents.read("outlines/approved/ch002.md")["hash"]))
        plan = self.approve_plan()
        job = manager.submit(self.project, self.request(kind="draft", plan_path=plan["path"], plan_hash=plan["hash"]))
        final = self.wait_state(manager, job["id"], {"succeeded", "failed"})
        self.assertEqual(final["state"], "succeeded", final)
        self.assertTrue(final["result_path"].startswith("drafts/candidate-"))
        self.assertEqual(list(self.project.path("chapters").iterdir()), [])
        self.assertIn("作者批准的章计划", provider.prompt)

    def test_cancel_discards_upstream_output_and_explicit_retry_links_new_job(self):
        policy = default_policy()
        policy["max_calls"] = 2
        ProjectRouting(self.project, self.workspace).save(policy, None)
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        self.assertEqual(manager.cancel(self.project, job["id"])["state"], "cancelled")
        provider.release.set()
        new = manager.retry(self.project, job["id"])
        self.assertNotEqual(new["id"], job["id"])
        self.assertEqual(new["retry_of"], job["id"])
        self.assertEqual(self.wait_state(manager, new["id"], {"succeeded", "failed"})["state"], "succeeded")
        self.assertIsNone(manager.get(self.project, job["id"])["result_path"])
        self.assertEqual(provider.calls, 2)

    def test_explicit_retry_keeps_original_budget_group_instead_of_resetting_allowance(self):
        manager, provider = self.manager(FakeProvider(error=True))
        first = self.wait_state(manager, manager.submit(self.project, self.request())["id"], {"failed"})
        self.assertEqual(provider.calls, 1)
        provider.error = False
        second = self.wait_state(manager, manager.retry(self.project, first["id"])["id"], {"failed", "succeeded"})
        self.assertEqual(second["state"], "failed", second)
        self.assertEqual(second["route"]["group_id"], first["route"]["group_id"])
        self.assertEqual(second["retry_of"], first["id"])
        self.assertEqual(provider.calls, 1)
        self.assertIn("预算", second["error"])
        self.assertEqual(first["budget_summary"]["calls"], 1)

    def test_queue_is_bounded_pause_resume_and_running_pause_is_interrupted(self):
        manager, provider = self.manager(FakeProvider(blocked=True), max_pending=2)
        first = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        second = manager.submit(self.project, self.request())
        self.assertEqual(manager.pause(self.project, second["id"])["state"], "paused")
        with self.assertRaises(VibeNovelError):
            manager.submit(self.project, self.request())
        self.assertEqual(manager.pause(self.project, first["id"])["state"], "interrupted")
        with self.assertRaises(VibeNovelError):
            manager.resume(self.project, first["id"])
        provider.release.set()
        self.assertEqual(manager.resume(self.project, second["id"])["state"], "queued")
        self.assertEqual(self.wait_state(manager, second["id"], {"succeeded", "failed"})["state"], "succeeded")
        self.assertEqual(provider.calls, 2)
        self.assertIsNone(manager.get(self.project, first["id"])["result_path"])

    def test_restart_marks_running_interrupted_does_not_automatically_retry(self):
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        manager.close()
        path = self.project.path(".runtime/jobs/" + job["id"] + ".json")
        saved = read_json(path, max_bytes=8 * 1024 * 1024)
        saved["state"] = "running"
        saved["_run_token"] = "old-process"
        path.write_bytes(json_bytes(saved))
        recovered, new_provider = self.manager()
        self.assertEqual(recovered.get(self.project, job["id"])["state"], "interrupted")
        self.assertEqual(new_provider.calls, 0)
        self.assertIsNone(recovered.get(self.project, job["id"])["result_path"])

    def test_close_immediate_restart_suppresses_old_worker(self):
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        manager.close(wait=False)
        recovered, new_provider = self.manager()
        self.assertEqual(recovered.get(self.project, job["id"])["state"], "interrupted")
        provider.release.set()
        manager.close()
        self.assertEqual(recovered.get(self.project, job["id"])["state"], "interrupted")
        self.assertEqual(new_provider.calls, 0)
        self.assertEqual(list(self.project.path("outlines/proposals").glob("expanded-*")), [])

    def test_retired_codex_is_rejected_and_legacy_history_cannot_resume(self):
        manager, provider = self.manager()
        with self.assertRaises(VibeNovelError):
            manager.submit(self.project, self.request(provider="codex"))
        self.assertEqual(provider.calls, 0)
        # Represent an existing pre-upgrade record using a valid API job shape.
        job = manager.submit(self.project, self.request())
        self.wait_state(manager, job["id"], {"succeeded"})
        manager.close()
        path = self.project.path(".runtime/jobs/" + job["id"] + ".json")
        saved = read_json(path, max_bytes=8 * 1024 * 1024)
        saved.update(provider="codex", state="awaiting_input", handoff_prompt="legacy private prompt")
        saved["request"]["provider"] = "codex"
        path.write_bytes(json_bytes(saved))
        recovered, provider2 = self.manager()
        final = recovered.get(self.project, job["id"])
        self.assertEqual(final["state"], "interrupted")
        self.assertNotIn("handoff_prompt", final)
        self.assertEqual(provider2.calls, 0)
        for action in (recovered.retry, recovered.resume, recovered.recover_result):
            with self.assertRaises(VibeNovelError):
                action(self.project, job["id"])

    def test_stale_api_result_and_retry_rejected_without_writing(self):
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        self.documents.save(self.source["path"], "new author text", self.source["hash"])
        provider.release.set()
        self.wait_state(manager, job["id"], {"failed"})
        with self.assertRaises(VibeNovelError):
            manager.retry(self.project, job["id"])
        self.assertEqual(list(self.project.path("outlines/proposals").iterdir()), [])

    def test_failure_persists_safe_error_and_no_fabricated_usage_or_cost(self):
        manager, provider = self.manager(FakeProvider(error=True))
        job = manager.submit(self.project, self.request())
        final = self.wait_state(manager, job["id"], {"failed"})
        self.assertIn("429", final["error"])
        self.assertIsNone(final["usage"])
        self.assertIsNone(final["cost_usd"])
        self.assertEqual(provider.calls, 1)

    def test_workspace_owner_excludes_threads_and_processes_and_releases_on_close(self):
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        competitor = JobManager(self.workspace)
        with self.assertRaises(VibeNovelError):
            competitor.start()
        script = ("import sys; from pathlib import Path; from vibenovel.studio.jobs import JobManager; "
                  "manager=JobManager(Path(sys.argv[1])); manager.start(); manager.close()")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-c", script, str(self.workspace)], env=env,
                                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"VibeNovelError", result.stderr)
        self.assertEqual(provider.calls, 1)
        manager.close(wait=False)
        provider.release.set()
        manager.close()
        competitor.start()
        self.addCleanup(competitor.close)
        self.assertEqual(competitor.get(self.project, job["id"])["state"], "interrupted")

    def test_environment_secret_is_absent_from_job_storage_public_data_and_logs(self):
        env = {"VIBENOVEL_API_KEY": "must-never-persist-this-test-secret", "VIBENOVEL_API_MODEL": "test-model"}
        with patch.dict(os.environ, env):
            manager, provider = self.manager()
            job = manager.submit(self.project, self.request())
            final = self.wait_state(manager, job["id"], {"succeeded", "failed"})
            persisted = self.project.path(".runtime/jobs/" + job["id"] + ".json").read_text(encoding="utf-8")
            exposed = persisted + json.dumps(final) + json.dumps(manager.capabilities())
        self.assertNotIn(env["VIBENOVEL_API_KEY"], exposed)

    def test_restart_recovers_saved_candidate_after_job_success_persist_failure_exactly_once(self):
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        persist = manager._persist

        def fail_success(project, record):
            if record["state"] == "succeeded":
                raise OSError("disk failure after candidate save, before success record")
            return persist(project, record)

        with patch.object(manager, "_persist", fail_success):
            provider.release.set()
            interrupted = self.wait_state(manager, job["id"], {"interrupted"})
        self.assertTrue(interrupted["recovery_available"])
        candidate_path = "outlines/proposals/expanded-" + job["id"] + ".md"
        self.assertTrue(self.project.path(candidate_path).is_file())
        self.assertEqual(len(self.documents.history(candidate_path)), 1)
        manager.close()
        recovered, provider2 = self.manager()
        final = recovered.get(self.project, job["id"])
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(final["result_path"], candidate_path)
        self.assertEqual(final["result_hash"], self.documents.read(candidate_path)["hash"])
        self.assertEqual(final["usage"]["total_tokens"], 18)
        self.assertEqual(provider2.calls, 0)
        self.assertEqual(len(self.documents.history(candidate_path)), 1)
        recovered.close()
        again, provider3 = self.manager()
        self.assertEqual(again.get(self.project, job["id"])["state"], "succeeded")
        self.assertEqual(len(self.documents.history(candidate_path)), 1)
        self.assertEqual(provider3.calls, 0)

    def interrupted_candidate(self):
        manager, provider = self.manager(FakeProvider(blocked=True))
        job = manager.submit(self.project, self.request())
        self.assertTrue(provider.started.wait(2))
        save = DocumentStore.save

        def fail_publication(store, path, content, expected_hash, provenance="AUTHOR", **kwargs):
            if provenance == "AI_SUGGESTED":
                raise OSError("disk failure before candidate save")
            return save(store, path, content, expected_hash, provenance, **kwargs)

        with patch.object(DocumentStore, "save", fail_publication):
            provider.release.set()
            interrupted = self.wait_state(manager, job["id"], {"interrupted"})
        self.assertTrue(interrupted["recovery_available"])
        manager.close()
        return job

    def test_unpublished_candidate_is_preserved_and_explicitly_recovered_without_model_retry(self):
        job = self.interrupted_candidate()
        recovered, provider = self.manager()
        interrupted = recovered.get(self.project, job["id"])
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertIn("仅测试文本", interrupted["recovery_candidate"])
        with self.assertRaises(VibeNovelError):
            recovered.retry(self.project, job["id"])
        final = recovered.recover_result(self.project, job["id"])
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(len(self.documents.history(final["result_path"])), 1)
        with self.assertRaises(VibeNovelError):
            recovered.recover_result(self.project, job["id"])

    def test_candidate_recovery_never_overwrites_or_accepts_unrelated_content_or_provenance(self):
        for content in ("unrelated author text", "# 候选\n仅测试文本"):
            with self.subTest(content=content):
                job = self.interrupted_candidate()
                path = "outlines/proposals/expanded-" + job["id"] + ".md"
                document = self.documents.save(path, content, None, "AUTHOR")
                recovered, provider = self.manager()
                state = recovered.get(self.project, job["id"])
                self.assertEqual(state["state"], "interrupted")
                self.assertTrue(state["recovery_available"])
                with self.assertRaises(VibeNovelError):
                    recovered.recover_result(self.project, job["id"])
                self.assertEqual(self.documents.read(path)["hash"], document["hash"])
                self.assertEqual(len(self.documents.history(path)), 1)
                self.assertEqual(provider.calls, 0)
                recovered.close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                                     "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
        status, data, headers = self.server.reply
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)


class APIProviderTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.reply = (200, json.dumps({"choices": [{"message": {"content": "actual result"}}],
                           "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}).encode(), {})
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {"VIBENOVEL_API_BASE": "http://127.0.0.1:%s/v1" % self.server.server_port,
                    "VIBENOVEL_API_KEY": "secret-test-key", "VIBENOVEL_API_MODEL": "test-model",
                    "VIBENOVEL_ALLOW_LOOPBACK_API": "1"}

    def test_real_http_text_only_request_and_reported_usage(self):
        result = OpenAICompatibleProvider(self.env).generate("test prompt", threading.Event())
        self.assertEqual(result.text, "actual result")
        self.assertEqual(result.usage["total_tokens"], 6)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["authorization"], "Bearer secret-test-key")
        self.assertEqual(request["body"]["model"], "test-model")
        self.assertNotIn("tools", request["body"])
        self.assertEqual(request["body"]["messages"][-1]["content"], "test prompt")

    def test_secret_and_upstream_body_are_never_exposed_by_capability_or_error(self):
        self.server.reply = (401, b'{"error":"secret-test-key private upstream response"}', {})
        with self.assertRaises(ProviderError) as caught:
            OpenAICompatibleProvider(self.env).generate("test", threading.Event())
        exposed = str(caught.exception) + json.dumps(capabilities(self.env))
        self.assertIn("401", exposed)
        self.assertNotIn("secret-test-key", exposed)
        self.assertNotIn("private upstream response", exposed)
        self.assertEqual(len(self.server.requests), 1)

    def test_http_external_loopback_opt_in_and_embedded_credentials_rejected(self):
        for base, allowed in (("http://external.example/v1", "1"), ("http://127.0.0.1/v1", "0"),
                              ("https://localhost/v1", "0"), ("https://user:secret@example.com/v1", "1"),
                              ("https://example.com/v1?key=secret", "1")):
            env = dict(self.env, VIBENOVEL_API_BASE=base, VIBENOVEL_ALLOW_LOOPBACK_API=allowed)
            with self.subTest(base=base), self.assertRaises(ProviderError):
                OpenAICompatibleProvider(env)
            self.assertNotIn("secret", json.dumps(capabilities(env)))

    def test_redirect_is_not_followed_and_no_authorization_is_forwarded(self):
        self.server.reply = (302, b"redirect body private", {"Location": self.env["VIBENOVEL_API_BASE"] + "/other"})
        with self.assertRaises(ProviderError) as caught:
            OpenAICompatibleProvider(self.env).generate("test", threading.Event())
        self.assertIn("302", str(caught.exception))
        self.assertEqual(len(self.server.requests), 1)

    def test_malformed_missing_and_invalid_usage_responses(self):
        for payload in (b"not json", b'{}', b'{"choices":[{"message":{"content":""}}]}',
                        b'{"choices":[{"message":{"tool_calls":[{}]}}]}'):
            self.server.reply = (200, payload, {})
            with self.subTest(payload=payload), self.assertRaises(ProviderError):
                OpenAICompatibleProvider(self.env).generate("test", threading.Event())
        self.server.reply = (200, b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":true}}', {})
        self.assertIsNone(OpenAICompatibleProvider(self.env).generate("test", threading.Event()).usage)


if __name__ == "__main__":
    unittest.main()
