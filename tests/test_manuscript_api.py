"""Offline HTTP acceptance of the existing-manuscript revision workflow."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.documents import DocumentStore
from vibenovel.core.project import ProjectStore
from vibenovel.core.toolkit import WritingToolkitStore
try:
    from test_editorial_jobs import EditorialProvider
except ModuleNotFoundError:
    from tests.test_editorial_jobs import EditorialProvider

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
    from vibenovel.studio.jobs import JobManager
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, 'Optional Studio tests: install ".[studio,test]"')
class ManuscriptAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        network = patch("socket.create_connection", side_effect=AssertionError("No external calls"))
        network.start()
        self.addCleanup(network.stop)
        projects = ProjectStore(self.workspace)
        self.project = projects.init("story", genre="adventure")
        projects.init("other", genre="campus")
        self.documents = DocumentStore(self.project)
        self.provider = EditorialProvider("他合上窗，转身收起绳索。")
        manager = JobManager(self.workspace, provider_factory=lambda _: self.provider)
        self.client = TestClient(create_app(self.workspace, job_manager=manager), base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}
        self.base = "/api/projects/story/manuscripts"
        self.original = "第一章 渡河\r\n\r\n他不知道的是，风已经停了。😀\r\n\r\n第二章 寻桥\r\n\r\n林远收起船桨。\r\n"
        self.source = self.documents.save("drafts/whole.md", self.original, None)
        self.source_request = {"source_path": self.source["path"], "source_hash": self.source["hash"]}

    def write(self, path, body):
        return self.client.post(path, json=body, headers=self.headers)

    def prepare(self):
        response = self.write(self.base, self.source_request)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def rewrite(self, session):
        section = session["sections"][0]
        document = self.documents.read(section["path"])
        start = document["content"].index("他不知道")
        end = document["content"].index("😀")
        body = {"kind": "rewrite", "document_path": section["path"], "document_hash": document["hash"],
                "selection": {"start_char": start, "end_char": end},
                "editorial": {"goals": ["remove_cliches", "preserve_voice"], "intensity": "conservative"}}
        response = self.write("/api/projects/story/jobs", body)
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        deadline = time.monotonic() + 5
        while job["state"] not in ("succeeded", "failed", "interrupted") and time.monotonic() < deadline:
            time.sleep(.01)
            job = self.client.get("/api/projects/story/jobs/" + job["id"]).json()
        self.assertEqual(job["state"], "succeeded", job)
        return job, body, document

    def test_full_workflow_preview_partition_audit_selection_accept_and_export(self):
        before = self.project.fingerprint()
        preview = self.write(self.base + "/preview", self.source_request)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(len(preview.json()["sections"]), 2)
        session = self.prepare()
        self.assertEqual(self.client.get(self.base + "/" + session["id"] + "/export").json()["content"], self.original)
        audit = self.write(self.base + "/" + session["id"] + "/audit", {})
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertEqual(audit.json()["coverage"]["analyzed"], 2)
        self.assertEqual(self.provider.calls, 0)
        job, body, document = self.rewrite(session)
        selection = body["selection"]
        merged = document["content"][:selection["start_char"]] + self.provider.text + document["content"][selection["end_char"]:]
        self.assertEqual(self.documents.read(document["path"])["hash"], document["hash"])
        accepted = self.write(self.base + "/" + session["id"] + "/accept", {
            "section_id": session["sections"][0]["id"], "base_hash": document["hash"],
            "candidate_path": job["result_path"], "candidate_hash": job["result_hash"]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()["sections"][0]["accepted"])
        self.assertEqual(self.documents.read(document["path"])["content"], merged)
        exported = self.client.get(self.base + "/" + session["id"] + "/export").json()
        self.assertEqual(exported["content"], merged + self.documents.read(session["sections"][1]["path"])["content"])
        self.assertEqual(self.documents.read(self.source["path"])["content"], self.original)
        self.assertEqual(self.project.fingerprint(), before)
        self.assertEqual(self.provider.calls, 1)
        isolated = self.client.get("/api/projects/other/manuscripts/" + session["id"])
        self.assertEqual(isolated.status_code, 404)

    def test_csrf_cross_origin_and_strict_nested_inputs_reject_before_model_call(self):
        for suffix, body in (("", self.source_request), ("/preview", self.source_request),
                             ("/" + "0" * 32 + "/audit", {}),
                             ("/" + "0" * 32 + "/accept", {"section_id": "section-001", "base_hash": "0" * 64,
                              "candidate_path": "drafts/x.md", "candidate_hash": "0" * 64})):
            self.assertEqual(self.client.post(self.base + suffix, json=body).status_code, 403)
            self.assertEqual(self.client.post(self.base + suffix, json=body,
                headers={**self.headers, "Origin": "https://untrusted.invalid"}).status_code, 403)
        for extra in ({"selection": {"start_char": True, "end_char": 5}},
                      {"selection": {"start_char": 0, "end_char": 5, "path": "chapters/ch001.md"}},
                      {"editorial": {"goals": ["detect_ai"], "intensity": "deep"}},
                      {"editorial": {"goals": ["preserve_voice"], "intensity": "deep", "approve": True}}):
            body = {"kind": "rewrite", "document_path": self.source["path"], "document_hash": self.source["hash"], **extra}
            response = self.write("/api/projects/story/jobs", body)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.provider.calls, 0)

    def test_accept_rejects_changed_editing_configuration_without_overwriting_copy(self):
        session = self.prepare()
        job, _, document = self.rewrite(session)
        WritingToolkitStore(self.project).save({"lexicon": [], "characters": [], "threads": [],
            "style": {"instructions": "新的作者风格要求", "avoid": [], "anchors": []}}, None)
        response = self.write(self.base + "/" + session["id"] + "/accept", {
            "section_id": session["sections"][0]["id"], "base_hash": document["hash"],
            "candidate_path": job["result_path"], "candidate_hash": job["result_hash"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.documents.read(document["path"])["hash"], document["hash"])


if __name__ == "__main__":
    unittest.main()
