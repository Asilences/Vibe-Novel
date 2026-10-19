"""Offline end-to-end route/fallback/late-result contracts."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import atomic_write, json_bytes, read_json
from vibenovel.core.project import ProjectStore
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
        environment = patch.dict(os.environ, {}, clear=True)
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
