"""HTTP coverage for model credentials, budgets and author-advanced nodes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibenovel.core.documents import DocumentStore
from vibenovel.core.project import ProjectStore
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import GenerationResult

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


class CandidateProvider:
    def __init__(self):
        self.calls = 0

    def generate(self, prompt, cancel):
        self.calls += 1
        if "只审查冻结候选" in prompt:
            text = json.dumps({"schema_version": 1, "issues": [{"id": "i1", "severity": "info", "category": "expression",
                "quote": "候选", "start_char": 0, "end_char": 2, "explanation": "需要作者核对", "suggestion": "保留"}], "unchecked": ["未检查整书"]})
        elif "只从冻结候选抽取" in prompt:
            text = json.dumps({"schema_version": 1, "items": [{"id": "m1", "kind": "event", "statement": "候选叙述待作者核对",
                "quote": "候选", "start_char": 0, "end_char": 2}], "unchecked": ["不是正式记忆"]})
        else:
            text = "候选正文，仅供作者核对。"
        return GenerationResult(text, {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}, "offline-model")


@unittest.skipUnless(TestClient, 'Optional Studio tests: install ".[studio,test]"')
class MultiModelAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.other = ProjectStore(self.workspace).init("other", genre="campus")
        self.docs = DocumentStore(self.project)
        self.source = self.docs.save("drafts/source.md", "已有小说原稿", None)
        self.provider = CandidateProvider()
        manager = JobManager(self.workspace, provider_factory=lambda _: self.provider)
        self.client = TestClient(create_app(self.workspace, job_manager=manager), base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}
        self.base = "/api/projects/story"

    def send(self, path, body, method="POST"):
        return self.client.request(method, path, json=body, headers=self.headers)

    def configure(self):
        public = self.client.get("/api/model-registry").json()
        profile = dict(public["profiles"][0], base_url="https://offline.invalid/v1", model="offline-model", api_key="test-only-private-key")
        response = self.send("/api/model-registry", {"expected_hash": public["hash"], "profiles": [profile]}, "PUT")
        self.assertEqual(response.status_code, 200, response.text)
        policy = self.client.get(self.base + "/routing").json()
        policy["policy"].update(max_calls=10, max_tokens=1000000)
        response = self.send(self.base + "/routing", {"expected_hash": policy["hash"], "policy": policy["policy"]}, "PUT")
        self.assertEqual(response.status_code, 200, response.text)
        return profile

    def request(self):
        return {"kind": "rewrite", "document_path": self.source["path"], "document_hash": self.source["hash"],
                "editorial": {"goals": ["preserve_voice"], "intensity": "conservative"}}

    def wait_flow(self, identity, states):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            response = self.client.get(self.base + "/workflows/" + identity)
            self.assertEqual(response.status_code, 200, response.text)
            flow = response.json()
            if flow["state"] in states:
                return flow
            time.sleep(.01)
        self.fail(str(flow))

    def test_registry_secrets_conflicts_endpoint_binding_and_validation_redaction(self):
        profile = self.configure()
        state = self.client.get("/api/model-registry")
        self.assertNotIn("test-only-private-key", state.text)
        self.assertTrue(state.json()["profiles"][0]["has_api_key"])
        profile.pop("api_key")
        profile["base_url"] = "https://another.invalid"
        response = self.send("/api/model-registry", {"expected_hash": state.json()["hash"], "profiles": [profile]}, "PUT")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("test-only-private-key", response.text)
        response = self.send("/api/model-registry", {"secret-test-field": "do-not-echo"}, "PUT")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret-test-field", response.text)
        self.assertNotIn("do-not-echo", response.text)
        response = self.send("/api/model-registry", {"expected_hash": None, "profiles": state.json()["profiles"]}, "PUT")
        self.assertEqual(response.status_code, 409)

    def test_preview_and_three_node_flow_require_explicit_advance_and_keep_canon(self):
        self.configure()
        fingerprint = self.project.fingerprint()
        response = self.send(self.base + "/routes/preview", self.request())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "writer")
        self.assertEqual(self.provider.calls, 0)
        response = self.send(self.base + "/workflows", {"request": self.request(), "review": True, "memory": True})
        self.assertEqual(response.status_code, 201, response.text)
        identity = response.json()["id"]
        flow = self.wait_flow(identity, {"awaiting_advance", "failed"})
        self.assertEqual(flow["state"], "awaiting_advance", flow)
        self.assertEqual(self.provider.calls, 1)
        for state in ("awaiting_advance", "ready"):
            response = self.send(self.base + "/workflows/" + identity + "/advance", {})
            self.assertEqual(response.status_code, 200, response.text)
            flow = self.wait_flow(identity, {state, "failed"})
            self.assertEqual(flow["state"], state, flow)
        self.assertEqual(self.provider.calls, 3)
        self.assertEqual(flow["budget_summary"]["calls"], 3)
        self.assertEqual([node["kind"] for node in flow["nodes"]], ["rewrite", "review_evidence", "memory_delta"])
        memory = json.loads(self.docs.read(flow["nodes"][-1]["result_path"])["content"])
        self.assertEqual(memory["status"], "candidate")
        self.assertEqual(memory["provenance"], "AI_SUGGESTED")
        self.assertEqual(self.project.fingerprint(), fingerprint)
        self.assertEqual(list(self.project.path("memory").rglob("*.json")), [])

    def test_boundaries_cross_project_and_hidden_test_override_rejected(self):
        self.configure()
        self.assertEqual(self.client.put("/api/model-registry", json={"expected_hash": None, "profiles": []}).status_code, 403)
        response = self.send(self.base + "/routes/preview", dict(self.request(), allow_unconfigured=True))
        self.assertEqual(response.status_code, 422)
        response = self.send("/api/projects/other/routes/preview", self.request())
        self.assertNotEqual(response.status_code, 200)
        response = self.send(self.base + "/workflows", {"request": dict(self.request(), kind="draft")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.provider.calls, 0)

    def test_registry_availability_overrides_legacy_in_both_directions(self):
        # A configured legacy key must not enable an empty active registry.
        path = self.workspace / "config" / "api.local.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"default_provider": "api", "api": {
            "base_url": "https://legacy.invalid", "model": "old", "api_key": "legacy-test-key"}}), encoding="utf-8")
        profile = self.configure()
        profile.pop("api_key")
        profile["clear_api_key"] = True
        public = self.client.get("/api/model-registry").json()
        response = self.send("/api/model-registry", {"expected_hash": public["hash"], "profiles": [profile]}, "PUT")
        self.assertEqual(response.status_code, 200, response.text)
        available = lambda: self.client.get("/api/bootstrap").json()["capabilities"]["providers"][0]["available"]
        self.assertFalse(available())
        path.unlink()  # Temporary test credential only.
        profile.pop("clear_api_key")
        profile["api_key"] = "new-test-key"
        public = self.client.get("/api/model-registry").json()
        response = self.send("/api/model-registry", {"expected_hash": public["hash"], "profiles": [profile]}, "PUT")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(available())

    def test_anonymous_benchmark_vote_reveal_export_and_stale_guard(self):
        alternate = self.docs.save("drafts/alternate.md", "另一个候选", None)
        response = self.send(self.base + "/benchmarks", {"candidate_a_path": self.source["path"], "candidate_a_hash": self.source["hash"],
            "candidate_b_path": alternate["path"], "candidate_b_hash": alternate["hash"]})
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertNotIn("drafts/", response.text)
        url = self.base + "/benchmarks/" + data["id"]
        self.assertEqual(self.send(url + "/reveal", {}).status_code, 409)
        voted = self.send(url + "/vote", {"decision": "prefer_a", "note": "作者偏好", "expected_hash": data["hash"]})
        self.assertEqual(voted.status_code, 200, voted.text)
        self.assertEqual(self.send(url + "/reveal", {}).status_code, 200)
        self.assertIn("候选 A", self.client.get(url + "/export").json()["content"])
        self.docs.save(alternate["path"], "不同内容", alternate["hash"])
        self.docs.restore(alternate["path"], self.docs.history(alternate["path"])[-1]["id"], self.docs.read(alternate["path"])["hash"])
        self.assertTrue(self.client.get(url).json()["stale"])
        self.assertEqual(self.provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
