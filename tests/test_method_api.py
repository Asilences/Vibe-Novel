"""HTTP method pipeline through adopted automatic use; all providers are offline."""
from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.documents import DocumentStore
from vibenovel.core.project import ProjectStore
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import GenerationResult
from vibenovel.studio.routing import ProjectRouting

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "Optional Studio HTTP dependencies")
class MethodAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        environment = patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.upper().startswith("VIBENOVEL_")}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.project = ProjectStore(self.workspace).init("http-method", genre="general")
        policy = ProjectRouting(self.project, self.workspace).public()
        policy["policy"]["max_calls"] = 4
        ProjectRouting(self.project, self.workspace).save(policy["policy"], policy["hash"])
        self.calls, self.results = [], []
        outer = self
        class Provider:
            def generate(self, prompt, cancel):
                outer.calls.append(prompt)
                return GenerationResult(outer.results.pop(0), {"prompt_tokens": 4, "completion_tokens": 5})
        self.jobs = JobManager(self.workspace, provider_factory=lambda name: Provider())
        self.client = TestClient(create_app(self.workspace, job_manager=self.jobs), base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}
        self.base = "/api/projects/" + self.project.novel_id
        self.lib = "/api/library/book"
        self.query = "?novel_id=" + self.project.novel_id

    def send(self, path, body, method="POST"):
        return self.client.request(method, path, json=body, headers=self.headers)

    def preview(self, body):
        response = self.send(self.base + "/jobs/preview", body)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        candidate = preview["candidates"][0]
        library = preview.get("method_preview", preview.get("library_preview"))
        return {**body, "expected_configuration": {"policy_revision": preview["policy_revision"], "policy_hash": preview["policy_hash"],
                "registry_revision": preview["registry_revision"], "registry_hash": candidate["registry_hash"],
                "registry_legacy_marker": candidate["legacy_marker"]},
                "expected_library_hash": library["snapshot"]["hash"] if library and library.get("snapshot") else None}

    def submit(self, body):
        response = self.send(self.base + "/jobs", body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def wait(self, job):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            response = self.client.get(self.base + "/jobs/" + job["id"])
            self.assertEqual(response.status_code, 200, response.text)
            current = response.json()
            if current["state"] in ("succeeded", "failed", "awaiting_input", "interrupted"):
                return current
            time.sleep(.01)
        self.fail("HTTP offline job did not finish")

    def test_http_analysis_extract_adopt_auto_rewrite_with_idempotency(self):
        text = "开篇😀\r\n场景要包含选择与代价。\r\n后续未发送。"
        imported = self.send(self.lib + "/sources" + self.query, {"filename": "人工教材.md", "content_base64": base64.b64encode(text.encode()).decode(),
                                                                 "metadata": {"outbound": "excerpts"}})
        self.assertEqual(imported.status_code, 201, imported.text)
        source = imported.json()
        start, end = text.index("场景"), text.index("\r\n后续")
        ref = {"source_id": source["id"], "source_version_id": source["version_id"], "start_char": start, "end_char": end, "quote": text[start:end]}
        analysis = {"title": "场景方法分析", "reading_map": [], "observations": [{"title": "选择", "observation": "选择带来代价。", "source_refs": [ref]}],
                    "directions": [{"title": "节拍检查", "purpose": "场景", "rationale": "检查变化", "source_refs": [ref]}], "references": [], "gaps": ["后续未读"]}
        methods = {"methods": [{"title": "场景节拍方法", "summary": "检查人物选择与代价", "purpose": "检查场景变化", "applies_to": ["场景"],
                    "not_for": ["替代作者决定"], "inputs": ["场景原稿"], "steps": ["找出决定", "检查代价"], "outputs": ["缺口建议"],
                    "limitations": ["文学效果待作者判断"], "counterexamples": ["过渡场景"], "checks": ["不添加事实"], "source_refs": [ref],
                    "source_cases": [{"description": "材料以代价关联选择", "source_refs": [ref]}], "synthetic_exercises": []}],
                    "references": [], "gaps": [], "not_adopted": []}
        self.results.extend([json.dumps(analysis, ensure_ascii=False), json.dumps(methods, ensure_ascii=False), "角色握紧手中的相机，走回了门口。"])
        initial = {"kind": "analyze_reference", "library_scope": "book", "source_ranges": [{k: v for k, v in ref.items() if k != "quote"}],
                   "purpose": "scene_beats", "idempotency_key": uuid.uuid4().hex}
        analyzed = self.wait(self.submit(self.preview(initial)))
        self.assertEqual(analyzed["state"], "succeeded", analyzed["error"])
        self.assertNotIn("expected_configuration", analyzed["request"])
        self.assertNotIn("expected_library_hash", analyzed["request"])
        entry = analyzed["result_library"]["analysis"]
        extraction = {**initial, "kind": "extract_methods", "idempotency_key": uuid.uuid4().hex,
                      "analysis_entry_id": entry["id"], "analysis_version_id": entry["version_id"], "method_run_id": analyzed["id"]}
        extracted = self.wait(self.submit(self.preview(extraction)))
        self.assertEqual(extracted["state"], "succeeded", extracted["error"])
        self.assertEqual(extracted["route"]["group_id"], analyzed["route"]["group_id"])
        self.assertEqual(extracted["budget_summary"]["calls"], 2)
        method = extracted["result_library"]["methods"][0]
        adopted = self.send(self.lib + "/entries/" + method["id"] + "/adopt" + self.query, {"expected_hash": method["hash"]})
        self.assertEqual(adopted.status_code, 200, adopted.text)
        method = adopted.json()
        payload = {key: method[key] for key in ("type", "title", "content", "tags", "source_refs", "method")}
        payload.update(outbound="summary", summary="作者授权摘要：检查场景选择与代价")
        updated = self.send(self.lib + "/entries/" + method["id"] + self.query, {"entry": payload, "expected_hash": method["hash"]}, "PUT")
        self.assertEqual(updated.status_code, 200, updated.text)
        method = updated.json()
        # Editing deliberately invalidates adoption, so the final edited version
        # is explicitly adopted instead of inheriting the previous decision.
        adopted = self.send(self.lib + "/entries/" + method["id"] + "/adopt" + self.query, {"expected_hash": method["hash"]})
        self.assertEqual(adopted.status_code, 200, adopted.text)
        method = adopted.json()
        overview = self.client.get(self.lib + self.query).json()
        policy = copy.deepcopy(overview["policy"])
        policy.update(mode="auto", required_ids=[method["id"]])
        configured = self.send(self.lib + "/policy" + self.query, {"policy": policy, "expected_hash": overview["policy_hash"]}, "PUT")
        self.assertEqual(configured.status_code, 200, configured.text)
        document = DocumentStore(self.project).save("drafts/http-input.md", "角色拿着相机回到门口。", None)
        rewrite = self.preview({"kind": "rewrite", "document_path": document["path"], "document_hash": document["hash"],
                                "instruction": "只改善表达", "idempotency_key": uuid.uuid4().hex})
        first = self.submit(rewrite)
        duplicate = self.submit(rewrite)
        self.assertEqual(first["id"], duplicate["id"])
        result = self.wait(first)
        self.assertEqual(result["state"], "succeeded", result["error"])
        self.assertIn("作者授权摘要", self.calls[-1])
        self.assertNotIn("后续未发送", self.calls[-1])
        self.assertEqual(len(self.calls), 3)
        conflict = self.send(self.base + "/jobs", dict(rewrite, instruction="同键不同请求"))
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(DocumentStore(self.project).read(document["path"])["content"], "角色拿着相机回到门口。")
