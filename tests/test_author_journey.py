"""Offline author journey through the real HTTP workflow, with no external API."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from vibenovel.core.creation import CREATIVE_BRIEF_PATH
from vibenovel.core.documents import DocumentStore
from vibenovel.core.project import ProjectStore
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import GenerationResult

from tests.test_blueprints import blueprint

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, 'Optional Studio tests: install ".[studio,test]"')
class AuthorJourneyTests(unittest.TestCase):
    def test_outline_to_export_requires_separate_author_decisions(self):
        """A book choice, package approval and chapter approval are distinct gates."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            isolated = {key: value for key, value in os.environ.items()
                        if not key.upper().startswith("VIBENOVEL_")}
            with patch.dict(os.environ, isolated, clear=True):
                project = ProjectStore(workspace).init("journey", genre="football", title="旧球场新赛季")
                documents = DocumentStore(project)
                (workspace / "prompts/genres").mkdir(parents=True)
                (workspace / "prompt.md").write_text("作者决定定稿；规划不是事实。", encoding="utf-8")
                (workspace / "prompts/genres/football.md").write_text(
                    "独立的合成足球经营故事，不使用真实赛事或其他小说内容。", encoding="utf-8")
                outline = "# 旧球场新赛季\n\n沈禾接手社区球场，先修理旧球门，再筹备周末友谊赛。"
                plan = blueprint()
                plan["book"].update(title="旧球场新赛季", summary="修缮社区球场并筹备友谊赛的经营故事")
                plan["volumes"][0].update(title="重开球场", summary="先获得训练场地，再筹备球队")
                plan["chapters"][0].update(title="旧球门", summary="沈禾检查球门并选择先修缮再招生",
                    goal="让球场能安全开放", conflict="可用经费不足", turn="暂缓招生，先修理球门",
                    outcome="付出维修费用，保住安全底线", beats=["检查松动的球门", "决定先修缮再招生"])
                plan["characters"][0].update(title="沈禾", role="社区球场经营者", knowledge="规划：第一章才发现球门松动")
                plan["threads"][0].update(title="维修清单", summary="后续核对维修清单里的遗漏")
                plan["settings"][0].update(title="社区球场", summary="等待修缮的社区足球场")
                plan["timeline"][0].update(title="检查旧球门", time="周一清晨", summary="准备修缮球场")
                plan["unknowns"] = ["友谊赛对手留待作者决定"]
                draft = "# 第一章 旧球门\n\n沈禾扶住门柱，螺栓在掌心转了半圈。他把招生海报卷起来，先拨通了维修师傅的电话。"
                review = "# 独立审稿候选\n\n本章以修缮选择表现经营底线；后续对手未定，不能视为已经参赛。"
                responses, prompts = [outline, json.dumps(plan, ensure_ascii=False), draft, review], []

                class OfflineProvider:
                    def generate(self, prompt, cancel):
                        prompts.append(prompt)
                        if not responses:
                            raise AssertionError("Unexpected model call in author journey")
                        return GenerationResult(responses.pop(0),
                            {"prompt_tokens": 40, "completion_tokens": 60, "total_tokens": 100}, "offline-journey")

                manager = JobManager(workspace, provider_factory=lambda _: OfflineProvider())
                with TestClient(create_app(workspace, job_manager=manager), base_url="http://127.0.0.1:8000") as client:
                    headers = {"X-VibeNovel-Token": client.get("/api/bootstrap").json()["csrf_token"]}
                    base = "/api/projects/journey"

                    def send(suffix, body, method="POST", status=200):
                        result = client.request(method, base + suffix, json=body, headers=headers)
                        self.assertEqual(result.status_code, status, result.text)
                        return result.json()

                    def execute(request):
                        preview = send("/jobs/preview", request)
                        first = preview["candidates"][0]
                        configured = dict(request, idempotency_key=uuid4().hex,
                            expected_configuration={"policy_revision": preview["policy_revision"],
                                "policy_hash": preview["policy_hash"], "registry_revision": preview["registry_revision"],
                                "registry_hash": first["registry_hash"], "registry_legacy_marker": first["legacy_marker"]})
                        submitted = send("/jobs", configured, status=201)
                        until = time.monotonic() + 8
                        while time.monotonic() < until:
                            job = client.get(base + "/jobs/" + submitted["id"]).json()
                            if job["state"] not in ("queued", "running"):
                                self.assertEqual(job["state"], "succeeded", job)
                                return job, documents.read(job["result_path"])
                            time.sleep(.01)
                        self.fail("Offline journey task did not settle")

                    def assert_no_story_facts():
                        self.assertFalse(any(project.path("chapters").glob("*.md")))
                        for directory in ("memory", "bible"):
                            self.assertFalse(any(path.is_file() for path in project.path(directory).rglob("*")))

                    def approve_and_apply(proposal):
                        endpoint = "/approvals/" + proposal["id"]
                        blocked = client.post(base + endpoint + "/apply", json={}, headers=headers)
                        self.assertEqual(blocked.status_code, 409, blocked.text)
                        send(endpoint + "/decision", {"decision": "approve"})
                        self.assertFalse(project.path(proposal["target"]).exists())
                        applied = send(endpoint + "/apply", {})
                        self.assertEqual(applied["state"], "applied")

                    registry = client.get("/api/model-registry").json()
                    profile = dict(registry["profiles"][0], base_url="https://offline.invalid/v1",
                                   model="offline-journey", api_key="offline-journey-fake-key")
                    saved = client.put("/api/model-registry", json={"expected_hash": registry["hash"],
                                       "profiles": [profile]}, headers=headers)
                    self.assertEqual(saved.status_code, 200, saved.text)
                    routing = client.get(base + "/routing").json()
                    routing["policy"].update(max_calls=8, max_tokens=1000000)
                    send("/routing", {"expected_hash": routing["hash"], "policy": routing["policy"]}, "PUT")
                    current = client.get(base + "/creative-brief").json()
                    brief = copy.deepcopy(current["brief"])
                    brief["story_seed"] = "沈禾接手社区足球场，在经费紧张时坚持先修好球门，再筹备友谊赛。"
                    source = send("/creative-brief", {"brief": brief, "expected_hash": current["hash"],
                        "expected_version_id": current["version_id"]}, "PUT")
                    self.assertEqual(prompts, [])
                    _, chosen = execute({"kind": "generate_story_outline", "document_path": CREATIVE_BRIEF_PATH,
                        "document_hash": source["hash"], "document_version_id": source["version_id"]})
                    self.assertEqual(len(prompts), 1)
                    assert_no_story_facts()

                    build = {"kind": "build_blueprint", "document_path": chosen["path"],
                             "document_hash": chosen["hash"], "planning_start": 1, "planning_count": 1}
                    blocked = client.post(base + "/jobs/preview", json=build, headers=headers)
                    self.assertEqual(blocked.status_code, 409, blocked.text)
                    self.assertEqual(len(prompts), 1)
                    selection = client.get(base + "/creation-selection").json()
                    send("/creation-selection", {"document_path": chosen["path"], "document_hash": chosen["hash"],
                        "document_version_id": documents.history(chosen["path"])[0]["id"],
                        "expected_hash": selection["hash"], "expected_version_id": selection["version_id"]}, "PUT")
                    _, package = execute(build)
                    self.assertEqual(len(prompts), 2)
                    assert_no_story_facts()
                    package_proposal = send("/planning/proposal", {"path": package["path"],
                                            "expected_hash": package["hash"]}, status=201)
                    approve_and_apply(package_proposal)
                    applied_package = client.get(base + "/planning/package", params={"path": package_proposal["target"]}).json()
                    self.assertTrue(applied_package["approved"])
                    chapter_proposal = send("/planning/proposal", {"path": applied_package["path"],
                        "expected_hash": applied_package["hash"], "chapter_id": "chapter-one"}, status=201)
                    draft_request = {"kind": "draft", "document_path": chosen["path"], "document_hash": chosen["hash"],
                        "plan_path": chapter_proposal["target"], "plan_hash": chapter_proposal["after_hash"]}
                    blocked = client.post(base + "/jobs/preview", json=draft_request, headers=headers)
                    self.assertIn(blocked.status_code, (400, 404, 409), blocked.text)
                    self.assertEqual(len(prompts), 2)
                    approve_and_apply(chapter_proposal)
                    assert_no_story_facts()
                    _, draft_document = execute(draft_request)
                    self.assertEqual(len(prompts), 3)
                    assert_no_story_facts()
                    _, report = execute({"kind": "review", "document_path": draft_document["path"],
                                         "document_hash": draft_document["hash"]})
                    self.assertEqual(len(prompts), 4)
                    self.assertIn("独立审稿候选", report["content"])
                    assert_no_story_facts()
                    final = send("/approvals", {"target": "chapters/ch001.md", "content": draft_document["content"],
                        "expected_hash": None, "reason": "隔离验收中作者明确选择此稿定稿", "impact": ["仅第1章"]}, status=201)
                    approve_and_apply(final)
                    exported = send("/export", {})
                    self.assertIn(draft, exported["content"])
                    self.assertNotIn("独立审稿候选", exported["content"])
                    self.assertNotIn("友谊赛对手留待作者决定", exported["content"])
                    self.assertEqual(responses, [])
                    self.assertEqual(len(prompts), 4)
                    self.assertFalse(any(path.is_file() for path in project.path("memory").rglob("*")))


if __name__ == "__main__":
    unittest.main()
