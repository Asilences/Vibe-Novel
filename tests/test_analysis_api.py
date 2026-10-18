"""Offline HTTP acceptance for editorial tools and private API preferences."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import digest
from vibenovel.core.project import ProjectStore

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.jobs import JobManager
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


class NoGenerationProvider:
    """Local analysis and settings must never invoke a model."""

    def __init__(self):
        self.calls = 0

    def generate(self, prompt, cancel):
        self.calls += 1
        raise AssertionError("Local editorial tools must not call a model")


@unittest.skipUnless(TestClient, 'Optional Studio tests: install ".[studio,test]"')
class AnalysisAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        # Windows' asyncio loop creates a local socketpair internally; block
        # outbound HTTP connection setup without breaking that self-pipe.
        network = patch("socket.create_connection", side_effect=AssertionError("Real network is forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.projects = ProjectStore(self.workspace)
        self.project = self.projects.init("story", genre="football")
        self.other = self.projects.init("other", genre="campus")
        self.documents = DocumentStore(self.project)
        self.provider = NoGenerationProvider()
        manager = JobManager(self.workspace, provider_factory=lambda _: self.provider)
        app = create_app(self.workspace, frontend_dir=self.workspace / "web", job_manager=manager)
        self.client = TestClient(app, base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}
        self.base = "/api/projects/story/analysis"
        self.addCleanup(lambda: self.assertEqual(self.provider.calls, 0))

    def write(self, path, body, method="POST"):
        return self.client.request(method, path, headers=self.headers, json=body)

    def chapter(self, content="守门员把红色手套放在桌边。", name="ch001", project=None):
        # Fixtures represent existing author-approved material, not an HTTP write.
        project = project or self.project
        path = f"chapters/{name}.md"
        raw = content.encode("utf-8")
        project.path(path).write_bytes(raw)
        return {"path": path, "hash": digest(raw)}

    def draft(self, content="# 第二章\n\n他不知道的是，名单已经换了。"):
        return self.documents.save("drafts/ch002.md", content, None)

    def analyze(self, draft):
        response = self.write(self.base + "/reports", {"path": draft["path"], "expected_hash": draft["hash"]})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def mark(self, report, status="resolved", **changes):
        body = {"report_path": report["path"], "report_hash": report["hash"],
                "issue_id": report["issues"][0]["id"], "status": status}
        body.update(changes)
        return self.write(self.base + "/mark", body)

    def approved_plan(self):
        approvals = ApprovalStore(self.project)
        proposal = approvals.create("outlines/approved/ch002.md", "第二章计划：追查名单变动。",
                                    "作者认可测试计划", ["第二章"], None)
        approvals.decide(proposal["id"], "approve")
        approvals.apply(proposal["id"])
        return self.documents.read(proposal["target"])

    def settings(self, expected_hash=None, **changes):
        api = {"base_url": "https://example.invalid/v1", "model": "test-model",
               "api_key": "", "allow_loopback": False}
        api.update(changes)
        return self.write("/api/settings", {"expected_hash": expected_hash, "api": api}, "PUT")

    def test_baseline_report_and_mark_preserve_exact_evidence_and_canon(self):
        sample = self.chapter("# 第一章\n\n作者认可的短句。")
        draft = self.draft()
        before = self.project.fingerprint()
        empty = self.client.get(self.base + "/baseline").json()
        self.assertFalse(empty["configured"])
        selected = self.write(self.base + "/baseline", {"samples": [sample], "expected_hash": None}, "PUT")
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(selected.json()["samples"][0]["hash"], sample["hash"])
        report = self.analyze(draft)
        self.assertTrue(report["baseline"]["comparison_available"])
        self.assertFalse(report["stale"])
        issue = report["issues"][0]
        self.assertEqual((issue["start_line"], issue["end_line"]), (3, 3))
        self.assertEqual(issue["quote"], "他不知道的是，名单已经换了。")
        loaded = self.client.get(self.base + "/report", params={"path": report["path"]})
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["hash"], report["hash"])
        marked = self.mark(report)
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertEqual(marked.json()["issues"][0]["status"], "resolved")
        self.assertNotEqual(marked.json()["hash"], report["hash"])
        self.assertEqual(len(self.documents.history(report["path"])), 2)
        self.assertEqual(self.project.fingerprint(), before)
        self.assertEqual(self.documents.read(draft["path"])["content"], draft["content"])
        self.assertEqual(list(self.project.path("memory").rglob("*.md")), [])

    def test_stale_source_and_report_versions_return_409(self):
        draft = self.draft()
        stale = self.write(self.base + "/reports", {"path": draft["path"], "expected_hash": "0" * 64})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(list(self.project.path("reviews").glob("analysis-*.json")), [])
        report = self.analyze(draft)
        marked = self.mark(report, "ignored").json()
        self.assertEqual(self.mark(report).status_code, 409)
        changed = self.documents.save(draft["path"], "# 新稿\n\n套话已改掉。", draft["hash"])
        response = self.client.get(self.base + "/report", params={"path": report["path"]})
        self.assertTrue(response.json()["stale"])
        self.assertEqual(response.json()["source"]["current_hash"], changed["hash"])
        self.assertEqual(self.mark(marked).status_code, 409)
        self.assertEqual(self.mark(marked, "ignored").status_code, 200)

    def test_baseline_is_optimistic_and_keeps_stale_sample_snapshot(self):
        sample = self.chapter()
        body = {"samples": [sample], "expected_hash": None}
        selected = self.write(self.base + "/baseline", body, "PUT").json()
        self.assertEqual(self.write(self.base + "/baseline", body, "PUT").status_code, 409)
        self.project.path(sample["path"]).write_text("作者改动了样章。", encoding="utf-8")
        baseline = self.client.get(self.base + "/baseline").json()
        self.assertTrue(baseline["stale"])
        self.assertEqual(baseline["samples"][0]["hash"], sample["hash"])
        self.assertEqual(baseline["metrics"], selected["metrics"])
        body["expected_hash"] = selected["hash"]
        self.assertEqual(self.write(self.base + "/baseline", body, "PUT").status_code, 409)

    def test_history_search_and_reports_are_isolated_between_novels(self):
        sample = self.chapter("# 第一章\r\n\r\n守门员把红色手套放在桌边。")
        self.chapter("红色手套属于另一本小说的秘密角色。", project=self.other)
        draft = self.draft("他不知道的是，红色手套藏在草稿里。")
        self.project.path("memory/secret.md").write_text("红色手套来自记忆。", encoding="utf-8")
        before = self.project.fingerprint()
        response = self.client.get(self.base + "/search", params={"query": "红色手套", "limit": 2})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["mode"], "literal_ngram")
        self.assertEqual(data["scanned"], 1)
        self.assertEqual(len(data["results"]), 1)
        result = data["results"][0]
        self.assertEqual((result["path"], result["hash"], result["start_line"]),
                         (sample["path"], sample["hash"], 3))
        self.assertEqual(result["quote"], "守门员把红色手套放在桌边。")
        self.assertNotIn("秘密角色", response.text)
        report = self.analyze(draft)
        self.other.path(report["path"]).write_bytes(self.project.path(report["path"]).read_bytes())
        copied = self.client.get("/api/projects/other/analysis/report", params={"path": report["path"]})
        self.assertEqual(copied.status_code, 400)
        self.assertEqual(self.project.fingerprint(), before)

    def test_brief_requires_approved_plan_reports_gaps_and_never_writes_canon(self):
        chapter = self.chapter()
        candidate = self.documents.save("outlines/proposals/ch002.md", "未批准的章计划。", None)
        rejected = self.write(self.base + "/brief", {"plan_path": candidate["path"], "expected_hash": candidate["hash"]})
        self.assertEqual(rejected.status_code, 400)
        plan = self.approved_plan()
        before = self.project.fingerprint()
        body = {"plan_path": plan["path"], "expected_hash": plan["hash"], "mode": "compact"}
        response = self.write(self.base + "/brief", body)
        self.assertEqual(response.status_code, 200, response.text)
        brief = response.json()
        self.assertEqual(brief["sources"][chapter["path"]], chapter["hash"])
        self.assertEqual(brief["sources"][plan["path"]], plan["hash"])
        self.assertEqual(brief["chapter"], 2)
        self.assertTrue(brief["missing"])
        self.assertIn("未来策划", brief["text"])
        self.assertLessEqual(brief["budget"]["characters"], brief["budget"]["limit"])
        self.assertEqual(self.write(self.base + "/brief", {**body, "expected_hash": "0" * 64}).status_code, 409)
        self.assertEqual(self.write(self.base + "/brief", {**body, "mode": "full"}).status_code, 200)
        self.assertEqual(self.project.fingerprint(), before)
        self.assertFalse(self.project.path("chapters/ch002.md").exists())
        self.assertEqual(list(self.project.path("memory").rglob("*.md")), [])

    def test_analysis_mutations_share_csrf_and_strict_input_boundaries(self):
        mutations = [
            ("PUT", "/baseline", {"samples": [], "expected_hash": None}),
            ("POST", "/reports", {"path": "drafts/ch001.md", "expected_hash": "0" * 64}),
            ("POST", "/mark", {"report_path": "reviews/analysis-" + "a" * 32 + ".json",
                                  "report_hash": "0" * 64, "issue_id": "issue", "status": "resolved"}),
            ("POST", "/brief", {"plan_path": "outlines/approved/ch001.md", "expected_hash": "0" * 64}),
        ]
        for method, suffix, body in mutations:
            with self.subTest(route=suffix):
                self.assertEqual(self.client.request(method, self.base + suffix, json=body).status_code, 403)
                self.assertEqual(self.write(self.base + suffix, {**body, "unknown": True}, method).status_code, 422)
        for path in ("../outside.md", "memory/current_state.md", "drafts/../../other/chapters/ch001.md"):
            with self.subTest(path=path):
                response = self.write(self.base + "/reports", {"path": path, "expected_hash": "0" * 64})
                self.assertEqual(response.status_code, 400)
        invalid = [
            ("POST", "/reports", {"path": 1, "expected_hash": "0" * 64}),
            ("PUT", "/baseline", {"samples": [{"path": "chapters/ch001.md", "hash": 1}], "expected_hash": None}),
            ("POST", "/brief", {"plan_path": "outlines/approved/ch001.md", "expected_hash": "0" * 64, "mode": False}),
        ]
        for method, suffix, body in invalid:
            with self.subTest(route=suffix, body=body):
                self.assertEqual(self.write(self.base + suffix, body, method).status_code, 422)
        for params in ({"query": ""}, {"query": "测试", "limit": "true"}, {"query": "测试", "limit": 21}):
            self.assertEqual(self.client.get(self.base + "/search", params=params).status_code, 422)

    def test_settings_credentials_are_write_only_blank_retains_and_clear_removes(self):
        empty = self.client.get("/api/settings").json()
        self.assertIsNone(empty["hash"])
        self.assertEqual(empty["default_provider"], "api")
        self.assertFalse(empty["api"]["has_api_key"])
        saved = self.settings(api_key="fixture-key-never-return")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["api"]["has_api_key"])
        for response in (saved, self.client.get("/api/settings"), self.client.get("/api/bootstrap")):
            self.assertNotIn("fixture-key-never-return", response.text)
            self.assertNotIn('"api_key":', response.text)
        retained = self.settings(saved.json()["hash"], model="updated-model")
        self.assertEqual(retained.status_code, 200, retained.text)
        settings_path = self.workspace / "config/api.local.json"
        self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["api"]["api_key"], "fixture-key-never-return")
        cleared = self.settings(retained.json()["hash"], model="updated-model", clear_api_key=True)
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertFalse(cleared.json()["api"]["has_api_key"])
        self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["api"]["api_key"], "")
        self.assertEqual([path.name for path in settings_path.parent.iterdir()], ["api.local.json"])

    def test_endpoint_change_requires_explicit_new_key_or_clear(self):
        saved = self.settings(api_key="old-fixture-key").json()
        old_bytes = (self.workspace / "config/api.local.json").read_bytes()
        rejected = self.settings(saved["hash"], base_url="https://another.invalid/v1")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual((self.workspace / "config/api.local.json").read_bytes(), old_bytes)
        self.assertNotIn("old-fixture-key", rejected.text)
        replaced = self.settings(saved["hash"], base_url="https://another.invalid/v1", api_key="new-fixture-key")
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertNotIn("new-fixture-key", replaced.text)
        cleared = self.settings(replaced.json()["hash"], clear_api_key=True)
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertFalse(cleared.json()["api"]["has_api_key"])

    def test_environment_overrides_expose_only_field_names_and_preserve_file_preferences(self):
        self.settings(api_key="local-fixture-key")
        overrides = {"VIBENOVEL_API_BASE": "https://private-env.invalid/v1",
                     "VIBENOVEL_API_MODEL": "private-env-model", "VIBENOVEL_API_KEY": "private-env-key",
                     "VIBENOVEL_ALLOW_LOOPBACK_API": "1"}
        with patch.dict(os.environ, overrides):
            response = self.client.get("/api/settings")
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(set(data["environment_overrides"]), {"base_url", "model", "api_key", "allow_loopback"})
            self.assertEqual(data["api"]["base_url"], "https://example.invalid/v1")
            self.assertEqual(data["api"]["model"], "test-model")
            self.assertNotIn("private-env", response.text)
            self.assertNotIn("local-fixture-key", response.text)
            saved = self.settings(data["hash"], model="new-local-model")
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["api"]["model"], "new-local-model")
            self.assertNotIn("private-env", saved.text)

    def test_concurrent_settings_edits_have_one_winner_and_one_conflict(self):
        initial = self.settings(api_key="concurrency-fixture-key").json()
        barrier = threading.Barrier(2)

        def edit(model):
            barrier.wait(timeout=5)
            return self.settings(initial["hash"], model=model)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(edit, ["first-model", "second-model"]))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        winner = next(response.json() for response in responses if response.status_code == 200)
        self.assertEqual(self.client.get("/api/settings").json()["hash"], winner["hash"])
        for response in responses:
            self.assertNotIn("concurrency-fixture-key", response.text)

    def test_settings_validation_does_not_echo_secret_values_or_unknown_field_names(self):
        secret = "sensitive-fixture-value"
        api = {"base_url": "https://example.invalid/v1", "model": "test-model", "api_key": secret,
               "allow_loopback": False}
        invalid = [
            {"expected_hash": None, "api": api, secret: True},
            {"expected_hash": None, "api": {**api, secret: True}},
            {"expected_hash": None, "api": {**api, "api_key": {secret: True}}},
            {"expected_hash": None, "api": {**api, "allow_loopback": "true"}},
        ]
        for body in invalid:
            with self.subTest(body=body):
                response = self.write("/api/settings", body, "PUT")
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(secret, response.text)
        for changes in ({"base_url": f"https://example.invalid/v1?key={secret}"},
                        {"base_url": f"https://{secret}@example.invalid/v1"},
                        {"api_key": secret + "\n"}, {"api_key": secret, "clear_api_key": True}):
            response = self.settings(**changes)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(secret, response.text)
        self.assertFalse((self.workspace / "config/api.local.json").exists())

    def test_settings_writes_require_same_origin_csrf_and_json(self):
        body = {"expected_hash": None, "api": {"base_url": "", "model": "", "api_key": "fixture-secret"}}
        self.assertEqual(self.client.put("/api/settings", json=body).status_code, 403)
        response = self.client.put("/api/settings", json=body,
                                   headers={**self.headers, "Origin": "https://external.invalid"})
        self.assertEqual(response.status_code, 403)
        response = self.client.put("/api/settings", content=json.dumps(body),
                                   headers={**self.headers, "Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 415)
        self.assertFalse((self.workspace / "config/api.local.json").exists())


if __name__ == "__main__":
    unittest.main()
