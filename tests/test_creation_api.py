"""Offline HTTP proof of the empty-work to first-outline author boundary."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibenovel.core.creation import CREATIVE_BRIEF_PATH, CREATION_SELECTION_PATH, catalog
from vibenovel.core.documents import DocumentStore
from vibenovel.core.project import ProjectStore
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import GenerationResult

try:
    from test_blueprints import blueprint
except ModuleNotFoundError:
    from tests.test_blueprints import blueprint

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


class OutlineProvider:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, cancel):
        self.prompts.append(prompt)
        return GenerationResult("# 初版总纲候选\n\n一个教练面对失败，再次选择带领新人。\n\n## 待决定\n结局未定。",
                                {"prompt_tokens": 40, "completion_tokens": 60, "total_tokens": 100},
                                "offline-outline")


@unittest.skipUnless(TestClient, 'Optional Studio tests: install ".[studio,test]"')
class CreationAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        isolated = {name: value for name, value in os.environ.items()
                    if not name.upper().startswith("VIBENOVEL_")}
        isolated.update(NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
        environment = patch.dict(os.environ, isolated, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.docs = DocumentStore(self.project)
        self.legacy_raw = self.project.path("project.json").read_bytes()
        self.fingerprint = self.project.fingerprint()
        (self.workspace / "prompts/genres").mkdir(parents=True)
        (self.workspace / "prompt.md").write_text("通用规则。", encoding="utf-8")
        for genre in ("general", "campus"):
            (self.workspace / ("prompts/genres/" + genre + ".md")).write_text("创作规则。", encoding="utf-8")
        self.provider = OutlineProvider()
        manager = JobManager(self.workspace, provider_factory=lambda _: self.provider)
        self.client = TestClient(create_app(self.workspace, job_manager=manager), base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}
        self.base = "/api/projects/story"

    def send(self, suffix, body, method="PUT"):
        return self.client.request(method, self.base + suffix, json=body, headers=self.headers)

    def configure(self):
        state = self.client.get("/api/model-registry").json()
        profile = dict(state["profiles"][0], base_url="https://offline.invalid/v1",
                       model="offline-outline", api_key="creation-test-only-key")
        response = self.client.put("/api/model-registry", json={"expected_hash": state["hash"], "profiles": [profile]},
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        policy = self.client.get(self.base + "/routing").json()
        policy["policy"].update(max_calls=8, max_tokens=1000000)
        response = self.send("/routing", {"expected_hash": policy["hash"], "policy": policy["policy"]})
        self.assertEqual(response.status_code, 200, response.text)

    def brief(self, seed="一位失败的教练回到母校，决定带领没有经验的新人。"):
        current = self.client.get(self.base + "/creative-brief").json()
        value = copy.deepcopy(current["brief"])
        value["story_seed"] = seed
        response = self.send("/creative-brief", {"brief": value, "expected_hash": current["hash"],
                                               "expected_version_id": current["version_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def request(brief):
        return {"kind": "generate_story_outline", "document_path": CREATIVE_BRIEF_PATH,
                "document_hash": brief["hash"], "document_version_id": brief["version_id"],
                "idempotency_key": uuid4().hex}

    def wait(self, identifier):
        until = time.monotonic() + 8
        job = None
        while time.monotonic() < until:
            response = self.client.get(self.base + "/jobs/" + identifier)
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                return job
            time.sleep(.01)
        self.fail("Offline generation did not finish: " + str(job))

    def generate(self):
        self.configure()
        brief = self.brief()
        request = self.request(brief)
        request["expected_configuration"] = self.configuration(request)
        response = self.send("/jobs", request, "POST")
        self.assertEqual(response.status_code, 201, response.text)
        job = self.wait(response.json()["id"])
        self.assertEqual(job["state"], "succeeded", job)
        doc = self.docs.read(job["result_path"])
        version = self.docs.history(doc["path"])[0]["id"]
        return brief, request, job, {"document_path": doc["path"], "document_hash": doc["hash"],
                                     "document_version_id": version}

    def configuration(self, request):
        response = self.send("/jobs/preview", request, "POST")
        self.assertEqual(response.status_code, 200, response.text)
        route = response.json()
        return {"policy_revision": route["policy_revision"], "policy_hash": route["policy_hash"],
                "registry_revision": route["registry_revision"], "registry_hash": route["candidates"][0]["registry_hash"],
                "registry_legacy_marker": route["candidates"][0]["legacy_marker"]}

    def selection(self, source):
        current = self.client.get(self.base + "/creation-selection").json()
        return self.send("/creation-selection", {**source, "expected_hash": current["hash"],
                                                "expected_version_id": current["version_id"]})

    def planning(self):
        brief, _, _, source = self.generate()
        selected = self.selection(source)
        self.assertEqual(selected.status_code, 200, selected.text)
        self.provider.generate = lambda prompt, cancel: GenerationResult(json.dumps(blueprint(), ensure_ascii=False),
            {"prompt_tokens": 40, "completion_tokens": 60, "total_tokens": 100}, "offline-planning")
        request = {"kind": "build_blueprint", "document_path": source["document_path"],
                   "document_hash": source["document_hash"], "planning_count": 1}
        response = self.send("/jobs", request, "POST")
        self.assertEqual(response.status_code, 201, response.text)
        job = self.wait(response.json()["id"])
        self.assertEqual(job["state"], "succeeded", job)
        return brief, self.docs.read(job["result_path"])

    def test_catalog_empty_defaults_and_save_without_model(self):
        response = self.client.get("/api/creation/catalog")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), catalog())
        self.assertNotIn("api_key", response.text)
        response = self.client.get(self.base + "/creative-brief")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["hash"])
        self.assertIsNone(response.json()["version_id"])
        self.assertEqual(self.brief("")["brief"]["story_seed"], "")
        self.assertEqual(self.provider.prompts, [])
        self.assertEqual(self.project.fingerprint(), self.fingerprint)

    def test_new_project_auto_id_general_schema2_and_legacy_remains_schema1(self):
        response = self.client.post("/api/projects", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.assertRegex(project["novel_id"], r"^story-[0-9a-f]{12}$")
        self.assertEqual(project["genre"], "general")
        self.assertEqual(project["schema_version"], 2)
        self.assertEqual(project["title"], "未命名作品")
        self.assertEqual(self.client.get(self.base).json()["project"]["schema_version"], 1)
        self.assertEqual(self.project.path("project.json").read_bytes(), self.legacy_raw)
        created = ProjectStore(self.workspace).get(project["novel_id"])
        self.assertEqual(created.metadata["schema_version"], 2)

    def test_concurrent_save_unknown_fields_and_cross_project_identity_rejected(self):
        first = self.brief()
        next_value = copy.deepcopy(first["brief"])
        next_value["story_seed"] += "新的方向。"
        body = {"brief": next_value, "expected_hash": first["hash"], "expected_version_id": first["version_id"]}
        self.assertEqual(self.send("/creative-brief", body).status_code, 200)
        self.assertEqual(self.send("/creative-brief", body).status_code, 409)
        current = self.client.get(self.base + "/creative-brief").json()
        body = {"brief": current["brief"], "expected_hash": current["hash"], "expected_version_id": current["version_id"]}
        self.assertEqual(self.send("/creative-brief", dict(body, provider_override="secret")).status_code, 422)
        body["brief"]["new_untrusted_field"] = True
        self.assertEqual(self.send("/creative-brief", body).status_code, 400)
        body["brief"].pop("new_untrusted_field")
        body["brief"]["novel_id"] = "another"
        self.assertEqual(self.send("/creative-brief", body).status_code, 400)
        self.assertEqual(self.provider.prompts, [])

    def test_managed_json_cannot_bypass_validation_by_generic_save_restore_or_csrf(self):
        brief = self.brief()
        for path in (CREATIVE_BRIEF_PATH, CREATION_SELECTION_PATH):
            with self.subTest(path=path):
                body = {"path": path, "content": "{}", "expected_hash": brief["hash"] if path == CREATIVE_BRIEF_PATH else None}
                self.assertEqual(self.send("/document", body).status_code, 400)
                self.assertEqual(self.send("/restore", {"path": path, "version_id": brief["version_id"],
                                                        "expected_hash": brief["hash"]}, "POST").status_code, 400)
        body = {"brief": brief["brief"], "expected_hash": brief["hash"], "expected_version_id": brief["version_id"]}
        self.assertEqual(self.client.put(self.base + "/creative-brief", json=body).status_code, 403)
        self.assertEqual(self.client.put(self.base + "/creative-brief", json=body,
            headers={**self.headers, "Origin": "https://untrusted.invalid"}).status_code, 403)
        self.assertEqual(self.client.get(self.base + "/creative-brief").json()["version_id"], brief["version_id"])

    def test_preview_is_free_and_repeated_idempotency_only_sends_once(self):
        self.configure()
        brief = self.brief()
        request = self.request(brief)
        preview = dict(request)
        preview.pop("idempotency_key")
        response = self.send("/jobs/preview", preview, "POST")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "planner")
        self.assertEqual(response.json()["creation_preview"]["version_id"], brief["version_id"])
        self.assertEqual(self.provider.prompts, [])
        self.assertEqual(self.client.get(self.base + "/jobs").json()["jobs"], [])
        request["expected_configuration"] = self.configuration(request)
        first = self.send("/jobs", request, "POST")
        self.assertEqual(first.status_code, 201, first.text)
        job = self.wait(first.json()["id"])
        self.assertEqual(job["state"], "succeeded", job)
        repeat = self.send("/jobs", request, "POST")
        self.assertEqual(repeat.status_code, 201, repeat.text)
        self.assertEqual(repeat.json()["id"], job["id"])
        self.assertEqual(len(self.provider.prompts), 1)
        self.assertEqual(job["creative_brief"]["version_id"], brief["version_id"])
        self.assertRegex(job["result_path"], r"^outlines/proposals/story-outline-[0-9a-f]{32}\.md$")
        self.assertEqual(self.project.fingerprint(), self.fingerprint)
        self.assertEqual(list(self.project.path("chapters").glob("*")), [])
        self.assertEqual(list(self.project.path("memory").rglob("*.json")), [])

    def test_generate_request_extra_fields_missing_version_and_aba_are_rejected(self):
        self.configure()
        brief = self.brief()
        request = self.request(brief)
        for change in ({"planning_count": 1}, {"document_path": "drafts/free.md"}, {"document_version_id": "0" * 32}):
            with self.subTest(change=change):
                self.assertIn(self.send("/jobs", dict(request, **change), "POST").status_code, (400, 409))
        missing = dict(request)
        missing.pop("document_version_id")
        self.assertEqual(self.send("/jobs", missing, "POST").status_code, 400)
        self.assertEqual(self.send("/jobs", dict(request, response_model="forged"), "POST").status_code, 422)
        self.brief("中间变化")
        restored = self.brief(brief["brief"]["story_seed"])
        self.assertEqual(restored["hash"], brief["hash"])
        self.assertNotEqual(restored["version_id"], brief["version_id"])
        self.assertEqual(self.send("/jobs", request, "POST").status_code, 409)
        self.assertEqual(self.provider.prompts, [])

    def test_generation_requires_preview_and_rejects_policy_drift_before_send(self):
        self.configure()
        request = self.request(self.brief())
        self.assertEqual(self.send("/jobs", request, "POST").status_code, 409)
        request["expected_configuration"] = self.configuration(request)
        policy = self.client.get(self.base + "/routing").json()
        policy["policy"]["max_calls"] += 1
        changed = self.send("/routing", {"policy": policy["policy"], "expected_hash": policy["hash"]})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.send("/jobs", request, "POST").status_code, 409)
        self.assertEqual(self.provider.prompts, [])

    def test_generated_selection_derives_job_and_brief_even_when_client_omits_them(self):
        brief, _, job, source = self.generate()
        wrong = self.selection(dict(source, job_id="0" * 32))
        self.assertEqual(wrong.status_code, 409, wrong.text)
        response = self.selection(source)
        self.assertEqual(response.status_code, 200, response.text)
        selected = response.json()["selection"]
        self.assertEqual(selected["job_id"], job["id"])
        self.assertEqual(selected["brief_hash"], brief["hash"])
        self.assertEqual(selected["brief_version_id"], brief["version_id"])
        self.assertFalse(response.json()["stale"])
        self.brief("原简报的作者意图变化")
        self.assertTrue(self.client.get(self.base + "/creation-selection").json()["stale"])
        self.assertEqual(self.selection(source).status_code, 409)

    def test_manual_outline_selects_without_job_and_forged_origin_is_rejected(self):
        doc = self.docs.save("outlines/source/author.md", "# 手写总纲\n作者已经有自己的想法。", None)
        source = {"document_path": doc["path"], "document_hash": doc["hash"],
                  "document_version_id": self.docs.history(doc["path"])[0]["id"]}
        response = self.selection(source)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["selection"]["job_id"])
        self.assertIsNone(response.json()["selection"]["brief_hash"])
        self.assertEqual(self.selection(dict(source, job_id="a" * 32)).status_code, 400)
        self.assertEqual(self.provider.prompts, [])
        self.assertEqual(self.project.fingerprint(), self.fingerprint)

    def test_forged_generated_path_does_not_become_unassociated_manual_outline(self):
        doc = self.docs.save("outlines/proposals/story-outline-" + "a" * 32 + ".md", "伪装成模型输出的大纲。", None)
        source = {"document_path": doc["path"], "document_hash": doc["hash"],
                  "document_version_id": self.docs.history(doc["path"])[0]["id"]}
        response = self.selection(source)
        self.assertIn(response.status_code, (400, 404, 409), response.text)
        self.assertIsNone(self.client.get(self.base + "/creation-selection").json()["selection"])

    def test_published_creation_planning_cannot_revive_after_brief_aba(self):
        brief, package = self.planning()
        self.brief("中间改变了作者意图")
        restored = self.brief(brief["brief"]["story_seed"])
        self.assertEqual(restored["hash"], brief["hash"])
        self.assertNotEqual(restored["version_id"], brief["version_id"])
        view = self.client.get(self.base + "/planning/package", params={"path": package["path"]})
        self.assertEqual(view.status_code, 200, view.text)
        self.assertFalse(view.json()["valid"])
        self.assertTrue(view.json()["stale"])
        proposed = self.send("/planning/proposal", {"path": package["path"], "expected_hash": package["hash"]}, "POST")
        self.assertEqual(proposed.status_code, 409, proposed.text)
        self.assertEqual(self.project.fingerprint(), self.fingerprint)

    def test_author_edit_cannot_strip_original_creation_dependencies(self):
        _, package = self.planning()
        data = json.loads(package["content"])
        data["source_hashes"].pop(CREATIVE_BRIEF_PATH)
        data["source_hashes"].pop(CREATION_SELECTION_PATH)
        altered = self.docs.save(package["path"], json.dumps(data), package["hash"])
        proposed = self.send("/planning/proposal", {"path": altered["path"], "expected_hash": altered["hash"]}, "POST")
        self.assertEqual(proposed.status_code, 409, proposed.text)

    def test_applied_creation_package_retains_dependency_history_at_formal_path(self):
        brief, package = self.planning()
        proposed = self.send("/planning/proposal", {"path": package["path"], "expected_hash": package["hash"]}, "POST")
        self.assertEqual(proposed.status_code, 201, proposed.text)
        proposal = proposed.json()
        decision = self.send("/approvals/" + proposal["id"] + "/decision", {"decision": "approve"}, "POST")
        self.assertEqual(decision.status_code, 200, decision.text)
        applied = self.send("/approvals/" + proposal["id"] + "/apply", {}, "POST")
        self.assertEqual(applied.status_code, 200, applied.text)
        path = "outlines/approved/blueprint.json"
        view = self.client.get(self.base + "/planning/package", params={"path": path})
        self.assertTrue(view.json()["approved"], view.text)
        self.brief("另一个方向")
        self.brief(brief["brief"]["story_seed"])
        view = self.client.get(self.base + "/planning/package", params={"path": path})
        self.assertTrue(view.json()["stale"], view.text)
        self.assertFalse(view.json()["approved"], view.text)
        proposed = self.send("/planning/proposal", {"path": path, "expected_hash": view.json()["hash"],
                                                   "chapter_id": "chapter-one"}, "POST")
        self.assertEqual(proposed.status_code, 409, proposed.text)
        self.assertEqual(list(self.project.path("chapters").glob("*")), [])
        self.assertEqual(list(self.project.path("memory").rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
