"""Offline end-to-end route/fallback/late-result contracts."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.documents import DocumentStore
from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.files import atomic_write, json_bytes, read_json
from vibenovel.core.locking import ConflictError, project_lock
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.model_registry import ModelRegistry, ROLES
from vibenovel.studio.providers import GenerationResult, OpenAICompatibleProvider, ProviderError
from vibenovel.studio.routing import BudgetLedger, ProjectRouting
from vibenovel.studio.settings import StudioSettings


class RoutedJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.source = self.documents.save("outlines/source/imported.md", "作者提供的测试大纲", None)
        (self.workspace / "prompts/genres").mkdir(parents=True)
        (self.workspace / "prompts/genres/campus.md").write_text("校园规则", encoding="utf-8")
        (self.workspace / "prompt.md").write_text("通用规则", encoding="utf-8")
        # Keep the Windows runtime variables used by Winsock and urllib. Emptying
        # the entire environment also removes NO_PROXY, which makes Windows
        # urllib unexpectedly fall back to machine/registry proxy settings.
        # Isolate model configuration while retaining the operating environment.
        isolated = {name: value for name, value in os.environ.items()
                    if not name.upper().startswith("VIBENOVEL_")}
        isolated.update(NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
        environment = patch.dict(os.environ, isolated, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.registry = ModelRegistry(self.workspace)
        profiles = [{"id": name, "label": name, "model": name, "base_url": "https://" + name + ".invalid/v1",
                     "api_key": name + "-offline-key", "allow_loopback": False, "roles": list(ROLES),
                     "context_tokens": 1048576, "max_output_tokens": 4096,
                     "price_input_usd": 1, "price_output_usd": 2} for name in ("primary", "backup")]
        self.registry.save({"profiles": profiles}, None)
        routing = ProjectRouting(self.project, self.workspace)
        state = routing.public()
        for binding in state["policy"]["bindings"].values():
            binding.update(primary="primary", fallbacks=["backup"])
        state["policy"].update(max_calls=2, max_attempts=2)
        routing.save(state["policy"], None)

    def request(self):
        return {"kind": "expand_outline", "provider": "api", "document_path": self.source["path"],
                "document_hash": self.source["hash"], "instruction": "仅测试"}

    def manager(self, factory):
        mocked = patch("vibenovel.studio.jobs.OpenAICompatibleProvider", side_effect=factory)
        mocked.start()
        self.addCleanup(mocked.stop)
        manager = JobManager(self.workspace)
        manager.start()
        self.addCleanup(manager.close)
        return manager

    def wait(self, manager, job_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            job = manager.get(self.project, job_id)
            if job["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                return job
            time.sleep(0.01)
        self.fail("offline route did not finish")

    def test_429_fallback_only_to_explicit_frozen_backup(self):
        calls = []
        class Provider:
            def __init__(self, settings):
                self.settings = settings
            def generate(self, prompt, cancel):
                calls.append(self.settings.model)
                if self.settings.model == "primary":
                    raise ProviderError("限流", retryable=True, http_status=429)
                return GenerationResult("# 备用候选", {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}, "backup")
        manager = self.manager(lambda *, settings, timeout: Provider(settings))
        submitted = manager.submit(self.project, self.request())
        job = self.wait(manager, submitted["id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(calls, ["primary", "backup"])
        self.assertEqual([item["profile_id"] for item in job["attempts"]], calls)
        self.assertEqual(job["attempts"][0]["billing"], "unknown")
        self.assertTrue(BudgetLedger(self.project).public(job["route"]["group_id"])["uncertain"])
        self.assertNotIn("offline-key", json.dumps(job))

    def test_transport_auth_and_invalid_output_do_not_fallback(self):
        for failure in (ProviderError("连接超时"), ProviderError("认证失败", http_status=401), ProviderError("无效文本")):
            calls = []
            class Provider:
                def generate(self, prompt, cancel):
                    calls.append(True)
                    raise failure
            # One manager at a time, because it owns the local persistent queue.
            with patch("vibenovel.studio.jobs.OpenAICompatibleProvider", return_value=Provider()):
                manager = JobManager(self.workspace)
                try:
                    submitted = manager.submit(self.project, self.request())
                    job = self.wait(manager, submitted["id"])
                    self.assertEqual(job["state"], "failed")
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(len(job["attempts"]), 1)
                    self.assertTrue(BudgetLedger(self.project).public(job["route"]["group_id"])["uncertain"])
                finally:
                    manager.close()

    def test_truncated_draft_never_publishes_or_falls_back_and_known_cost_survives_restart(self):
        approvals = ApprovalStore(self.project)
        proposal = approvals.create("outlines/approved/ch001.md", "已批准的人工章计划", "验收", ["测试"], None)
        approvals.decide(proposal["id"], "approve"); approvals.apply(proposal["id"])
        plan = self.documents.read("outlines/approved/ch001.md")
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(True)
                return GenerationResult("他推开门，正要", {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}, "primary", "length")
        manager = self.manager(lambda **kwargs: Provider())
        request = {**self.request(), "kind": "draft", "plan_path": plan["path"], "plan_hash": plan["hash"]}
        submitted = manager.submit(self.project, request)
        job = self.wait(manager, submitted["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(job["result_path"])
        self.assertEqual(job["response_candidate"], "他推开门，正要")
        self.assertEqual(job["finish_reason"], "length")
        self.assertEqual(job["usage"]["total_tokens"], 18)
        self.assertEqual(job["attempts"][0]["billing"], "reported")
        self.assertEqual(len(calls), 1)  # A truncated response is not a fallback trigger.
        summary = BudgetLedger(self.project).public(job["route"]["group_id"])
        self.assertFalse(summary["uncertain"])
        self.assertGreater(summary["cost_usd"], 0)
        self.assertEqual(list(self.project.path("drafts").glob("*.md")), [])
        manager.close()
        restarted = JobManager(self.workspace)
        restarted.start(); self.addCleanup(restarted.close)
        retained = restarted.get(self.project, job["id"])
        self.assertEqual(retained["response_candidate"], job["response_candidate"])
        self.assertEqual(retained["usage"], job["usage"])
        self.assertFalse(BudgetLedger(self.project).public(job["route"]["group_id"])["uncertain"])
        with self.assertRaises(VibeNovelError):
            restarted.recover_result(self.project, job["id"])
        self.assertEqual(len(calls), 1)

    def test_empty_length_response_keeps_bill_without_publishing_an_empty_candidate(self):
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(True)
                return GenerationResult("", {"prompt_tokens": 11, "completion_tokens": 4096}, "primary", "length")
        manager = self.manager(lambda **kwargs: Provider())
        submitted = manager.submit(self.project, self.request())
        job = self.wait(manager, submitted["id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["response_candidate"], "")
        self.assertIn("思考", job["error"])
        self.assertEqual(job["attempts"][0]["billing"], "reported")
        self.assertFalse(BudgetLedger(self.project).public(job["route"]["group_id"])["uncertain"])
        self.assertEqual(len(calls), 1)

    def test_paid_complete_review_staled_by_canon_remains_readonly_archive_after_restart(self):
        source = self.documents.save("drafts/imported.md", "候选章节原文不变。", None)
        started, release = threading.Event(), threading.Event()
        calls = []
        returned = "# 独立审稿\n仅针对冻结的旧版本提出建议。"
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt); started.set()
                if not release.wait(5):
                    raise RuntimeError("fixture response was not released")
                return GenerationResult(returned, {"prompt_tokens": 5, "completion_tokens": 9}, "primary", "stop")
        manager = self.manager(lambda **kwargs: Provider())
        self.addCleanup(release.set)
        submitted = manager.submit(self.project, {"kind": "review", "provider": "api",
                                    "document_path": source["path"], "document_hash": source["hash"]})
        self.assertTrue(started.wait(3))
        self.project.path("bible/changed.md").write_text("期间正式设定变化。", encoding="utf-8")
        self.documents.read(source["path"])
        release.set()
        failed = self.wait(manager, submitted["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertIn("正式内容已变化", failed["error"])
        self.assertTrue(failed["response_archived"])
        self.assertEqual(failed["response_candidate"], returned)
        self.assertEqual(failed["finish_reason"], "stop")
        self.assertIsNone(failed["result_path"])
        self.assertFalse(failed["recovery_available"])
        self.assertEqual(failed["attempts"][0]["billing"], "reported")
        self.assertNotIn("_provider_response", failed)
        self.assertNotIn("_prompt", failed)
        self.assertEqual(list(self.project.path("reviews").glob("*.md")), [])
        self.assertEqual(self.documents.read(source["path"])["hash"], source["hash"])
        before = BudgetLedger(self.project).public(failed["route"]["group_id"])
        manager.close()
        restarted = JobManager(self.workspace)
        restarted.start(); self.addCleanup(restarted.close)
        archived = restarted.get(self.project, failed["id"])
        self.assertTrue(archived["response_archived"])
        self.assertEqual(archived["response_candidate"], returned)
        self.assertEqual(restarted.list(self.project)[0]["response_candidate"], returned)
        with self.assertRaises(VibeNovelError):
            restarted.recover_result(self.project, failed["id"])
        with self.assertRaises(VibeNovelError):
            restarted.repair_result(self.project, failed["id"], returned)
        self.assertEqual(BudgetLedger(self.project).public(failed["route"]["group_id"]), before)
        self.assertEqual(len(calls), 1)

    def test_response_archive_projection_requires_valid_content_and_failed_api_task(self):
        content = "<script>untrusted material remains text</script>"
        base = {"provider": "api", "state": "failed", "_provider_response": {
                "text": content, "hash": hashlib.sha256(content.encode()).hexdigest()},
                "response_candidate": "unverified persisted projection", "response_archived": True}
        self.assertEqual(JobManager._public(base)["response_candidate"], content)
        for state in ("succeeded", "running", "interrupted", "cancelled"):
            projected = JobManager._public({**base, "state": state})
            self.assertNotIn("response_archived", projected)
            self.assertNotIn("response_candidate", projected)
        for bad in ({"text": content + "changed", "hash": base["_provider_response"]["hash"]},
                    {"text": 123, "hash": "0" * 64}, {"text": "bad\ud800text", "hash": "0" * 64}):
            projected = JobManager._public({**base, "_provider_response": bad})
            self.assertNotIn("response_archived", projected)
            self.assertNotIn("response_candidate", projected)
        self.assertNotIn("response_archived", JobManager._public({**base, "provider": "codex"}))

    def test_large_escaped_paid_response_is_restartable_with_separate_archive(self):
        # This is within both the 2MiB candidate and 4MiB upstream JSON bounds,
        # but two inline copies plus a valid instruction exceeded the 8MiB
        # task reader limit and prevented the entire service from restarting.
        content = "\\" * 2090000
        usage = {"prompt_tokens": 10, "completion_tokens": 10}
        upstream = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": usage}
        self.assertLess(len(json.dumps(upstream).encode()), 4 * 1024 * 1024)
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(True)
                return GenerationResult(content, usage, "primary", "stop")
        manager = self.manager(lambda **kwargs: Provider())
        job = self.wait(manager, manager.submit(self.project, {**self.request(), "instruction": "汉" * 16000})["id"])
        self.assertEqual(job["state"], "succeeded", job.get("error"))
        manifest = self.project.path(".runtime/jobs/" + job["id"] + ".json")
        saved = read_json(manifest, max_bytes=8 * 1024 * 1024)
        self.assertNotIn("text", saved["_provider_response"])
        self.assertEqual(self.project.path(saved["_provider_response"]["path"]).read_text(encoding="utf-8"), content)
        before = BudgetLedger(self.project).public(job["route"]["group_id"])
        manager.close()
        restarted = JobManager(self.workspace)
        restarted.start(); self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(self.project, job["id"])["state"], "succeeded")
        self.assertEqual(BudgetLedger(self.project).public(job["route"]["group_id"]), before)
        self.assertEqual(len(calls), 1)

    def test_oversized_legacy_inline_archive_is_preserved_and_deduplicated_on_restart(self):
        content = "\\" * 2090000
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(True)
                return GenerationResult(content, {"prompt_tokens": 10, "completion_tokens": 10}, "primary", "stop")
        manager = self.manager(lambda **kwargs: Provider())
        job = self.wait(manager, manager.submit(self.project, {**self.request(), "instruction": "汉" * 16000})["id"])
        manager.close()
        manifest = self.project.path(".runtime/jobs/" + job["id"] + ".json")
        legacy = read_json(manifest, max_bytes=8 * 1024 * 1024)
        reference = legacy["_provider_response"]
        legacy["_provider_response"] = {k: v for k, v in reference.items() if k not in {"path", "bytes"}}
        legacy["_provider_response"]["text"] = content
        original_bytes = json_bytes(legacy)
        self.assertGreater(len(original_bytes), 8 * 1024 * 1024)
        atomic_write(manifest, original_bytes, overwrite=True)
        before = BudgetLedger(self.project).public(job["route"]["group_id"])
        restarted = JobManager(self.workspace)
        restarted.start(); self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(self.project, job["id"])["state"], "succeeded")
        saved = read_json(manifest, max_bytes=8 * 1024 * 1024)
        self.assertEqual(saved["_provider_response"]["hash"], reference["hash"])
        self.assertEqual(self.project.path(saved["_provider_response"]["path"]).read_bytes(), content.encode())
        backup = self.project.path(".runtime/provider-responses/" + job["id"] + "-legacy.json")
        self.assertEqual(backup.read_bytes(), original_bytes)
        self.assertEqual(BudgetLedger(self.project).public(job["route"]["group_id"]), before)
        self.assertEqual(len(calls), 1)

    def test_separate_response_projection_rejects_wrong_scope_path_size_or_content(self):
        job = {"id": "f" * 32, "novel_id": self.project.novel_id, "provider": "api", "state": "failed"}
        content = "原始付费响应，只供查看。"
        reference = JobManager._archive_response(self.project, job, GenerationResult(content, finish_reason="stop"))
        job["_provider_response"] = reference
        self.assertEqual(JobManager._public(job, self.project)["response_candidate"], content)
        self.assertNotIn("response_candidate", JobManager._public(job))
        other = ProjectStore(self.workspace).init("other", genre="campus")
        self.assertNotIn("response_candidate", JobManager._public(job, other))
        for bad in ({**reference, "path": "../private.txt"}, {**reference, "hash": "0" * 64},
                    {**reference, "bytes": True}, {**reference, "bytes": 4 * 1024 * 1024 + 1}):
            self.assertNotIn("response_candidate", JobManager._public({**job, "_provider_response": bad}, self.project))
        path = self.project.path(reference["path"])
        path.write_bytes(content.encode() + b"changed")
        self.assertNotIn("response_candidate", JobManager._public(job, self.project))
        with self.assertRaises(ConflictError):
            JobManager._archive_response(self.project, job, GenerationResult(content))
        self.assertTrue(path.read_bytes().endswith(b"changed"))

    def test_saved_response_survives_task_record_failure_without_another_call(self):
        calls = []
        content = "模型已返回；归档不能因随后任务写入失败而丢失。"
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(True)
                return GenerationResult(content, {"prompt_tokens": 10, "completion_tokens": 10}, "primary", "stop")
        manager = self.manager(lambda **kwargs: Provider())
        original_persist = manager._persist
        failure = [False]
        def fail_once(project, job):
            if job.get("_provider_response") and not failure[0]:
                failure[0] = True
                raise OSError("test task-record failure after raw response archival")
            return original_persist(project, job)
        with patch.object(manager, "_persist", side_effect=fail_once):
            job = self.wait(manager, manager.submit(self.project, self.request())["id"])
        self.assertTrue(failure[0])
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["response_archived"])
        self.assertEqual(job["response_candidate"], content)
        self.assertEqual(job["attempts"][0]["billing"], "reported")
        before = BudgetLedger(self.project).public(job["route"]["group_id"])
        manager.close()
        restarted = JobManager(self.workspace)
        restarted.start(); self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(self.project, job["id"])["response_candidate"], content)
        self.assertEqual(BudgetLedger(self.project).public(job["route"]["group_id"]), before)
        self.assertEqual(len(calls), 1)

    def test_selected_rewrite_waits_for_concurrent_reader_without_repeating_api(self):
        source = self.documents.save("drafts/imported.md", "开头🙂。旧的说明。结尾不变。", None)
        start = source["content"].index("旧的说明")
        end = start + len("旧的说明")
        ready, held, release = threading.Event(), threading.Event(), threading.Event()
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                return GenerationResult("新的行动", {"prompt_tokens": 5, "completion_tokens": 4}, "primary", "stop")
        manager = self.manager(lambda **kwargs: Provider())
        read = DocumentStore.read
        first = True
        def coordinated_read(store, path):
            nonlocal first
            if first and threading.current_thread().name.startswith("vibenovel-job-"):
                first = False
                ready.set()
                if not held.wait(5):
                    raise RuntimeError("reader lock fixture did not start")
            return read(store, path)
        def concurrent_reader():
            with project_lock(self.project, timeout=2):
                held.set()
                release.wait(8)
        request = {"kind": "rewrite", "provider": "api", "document_path": source["path"],
                   "document_hash": source["hash"], "selection": {"start_char": start, "end_char": end}}
        self.addCleanup(release.set)
        with patch.object(DocumentStore, "read", coordinated_read):
            submitted = manager.submit(self.project, request)
            self.assertTrue(ready.wait(3))
            reader = threading.Thread(target=concurrent_reader)
            reader.start()
            self.assertTrue(held.wait(3))
            # Store methods time out at two seconds. The task must survive
            # one real contention timeout without sending or retrying HTTP.
            timer = threading.Timer(2.4, release.set)
            timer.start()
            try:
                job = self.wait(manager, submitted["id"])
            finally:
                release.set(); timer.cancel(); reader.join(3)
        self.assertEqual(job["state"], "succeeded", job.get("error"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(job["attempts"]), 1)
        self.assertEqual(self.documents.read(source["path"])["hash"], source["hash"])
        candidate = self.documents.read(job["result_path"])["content"]
        self.assertEqual(candidate, source["content"][:start] + "新的行动" + source["content"][end:])

    def test_queued_model_configuration_aba_cannot_reactivate_snapshot(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(True)
                started.set()
                if not release.wait(5):
                    raise RuntimeError("fixture release timeout")
                return GenerationResult("# 原冻结模型候选")
        manager = self.manager(lambda **kwargs: Provider())
        self.addCleanup(release.set)
        first = manager.submit(self.project, self.request())
        self.assertTrue(started.wait(3))
        queued = manager.submit(self.project, self.request())
        public = self.registry.public()
        original = json.loads(json.dumps(public["profiles"]))
        public["profiles"][0]["model"] = "changed-model"
        changed = self.registry.save({"profiles": public["profiles"]}, public["hash"])
        self.registry.save({"profiles": original}, changed["hash"])
        release.set()
        self.wait(manager, first["id"])
        failed = self.wait(manager, queued["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(failed["attempts"], [])

    def test_cancelled_late_result_never_publishes_and_keeps_accounting(self):
        started, release = threading.Event(), threading.Event()
        class Provider:
            def generate(self, prompt, cancel):
                started.set()
                release.wait(5)
                return GenerationResult("# 迟到候选", {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
        manager = self.manager(lambda **kwargs: Provider())
        self.addCleanup(release.set)
        submitted = manager.submit(self.project, self.request())
        self.assertTrue(started.wait(3))
        manager.cancel(self.project, submitted["id"])
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get(self.project, submitted["id"])
            if job["attempts"] and job["attempts"][0]["state"] == "discarded":
                break
            time.sleep(0.01)
        self.assertEqual(job["state"], "cancelled")
        self.assertIsNone(job["result_path"])
        summary = BudgetLedger(self.project).public(job["route"]["group_id"])
        self.assertEqual(summary["tokens"], 14)
        self.assertEqual(summary["calls"], 1)

    def test_adapter_sends_frozen_max_tokens_on_real_local_http(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = b'{"choices":[{"message":{"content":"offline"}}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = StudioSettings(base_url="http://127.0.0.1:" + str(server.server_port), model="frozen-model",
                                      api_key="offline-fixture-key", allow_loopback=True, max_output_tokens=123)
            result = OpenAICompatibleProvider(settings=settings).generate("仅测试", threading.Event())
            self.assertEqual(result.text, "offline")
            self.assertEqual(received[0]["max_tokens"], 123)
            self.assertEqual(received[0]["model"], "frozen-model")
            self.assertIsNone(result.model)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_response_model_records_only_valid_server_metadata(self):
        responses = ["served-model-version", None, 42, "invalid\nmodel", "x" * 501,
                     "echo-offline-fixture-key", "bad\ud800model"]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                value = {"choices": [{"message": {"content": "offline"}}]}
                reported = responses.pop(0)
                if reported is not None:
                    value["model"] = reported
                body = json.dumps(value).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = StudioSettings(base_url="http://127.0.0.1:" + str(server.server_port), model="requested-alias",
                                      api_key="offline-fixture-key", allow_loopback=True, max_output_tokens=123)
            provider = OpenAICompatibleProvider(settings=settings)
            models = [provider.generate("仅测试", threading.Event()).model for _ in range(7)]
            self.assertEqual(models, ["served-model-version"] + [None] * 6)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_real_http_fixture_bypasses_unavailable_ambient_proxies(self):
        self.assertFalse(any(name.upper().startswith("VIBENOVEL_") for name in os.environ))
        # Both local HTTP contracts must still reach their real socket servers
        # even on runners with an unusable machine proxy or HTTP_PROXY preset.
        proxy = "http://127.0.0.1:1"
        with patch.dict(os.environ, {"HTTP_PROXY": proxy, "http_proxy": proxy,
                                     "HTTPS_PROXY": proxy, "https_proxy": proxy}), \
                patch("urllib.request.getproxies_registry", return_value={"http": proxy}, create=True) as registry:
            self.test_adapter_sends_frozen_max_tokens_on_real_local_http()
            self.test_response_model_records_only_valid_server_metadata()
        registry.assert_not_called()

    def test_restart_preserves_unknown_reservation_without_replaying_request(self):
        calls = []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(True)
                raise ProviderError("模拟发送后连接中断")
        with patch("vibenovel.studio.jobs.OpenAICompatibleProvider", return_value=Provider()):
            first_manager = JobManager(self.workspace)
            try:
                submitted = first_manager.submit(self.project, self.request())
                job = self.wait(first_manager, submitted["id"])
            finally:
                first_manager.close()
            # Recreate the last durable state of a process killed after sending:
            # running intent remains on disk, and money is still uncertain.
            path = self.project.path(".runtime/jobs/" + job["id"] + ".json")
            saved = read_json(path)
            saved["state"] = "running"
            saved["_run_token"] = "offline-crash-fixture"
            saved["attempts"][0].update(state="running", billing="reserved")
            atomic_write(path, json_bytes(saved), overwrite=True)
            before = BudgetLedger(self.project).public(job["route"]["group_id"])
            recovered = JobManager(self.workspace)
            try:
                recovered.start()
                restored = recovered.get(self.project, job["id"])
                self.assertEqual(restored["state"], "interrupted")
                self.assertEqual(len(calls), 1)
                after = BudgetLedger(self.project).public(job["route"]["group_id"])
                self.assertEqual(after, before)
                self.assertTrue(after["uncertain"])
                self.assertIsNone(after["cost_usd"])
            finally:
                recovered.close()


if __name__ == "__main__":
    unittest.main()
