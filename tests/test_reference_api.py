"""Offline reference-book HTTP boundaries, distinct from author manuscripts."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
import time
import uuid
from unittest.mock import patch

from vibenovel.core.project import ProjectStore

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, 'Optional Studio tests: install ".[studio,test]"')
class ReferenceAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                              if not key.upper().startswith("VIBENOVEL_")}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.project = ProjectStore(self.workspace).init("author", genre="campus")
        self.fingerprint = self.project.fingerprint()
        self.client = TestClient(create_app(self.workspace), base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}

    def send(self, suffix, value=None, method="POST"):
        return self.client.request(method, "/api/reference-books" + suffix, json=value or {}, headers=self.headers)

    def import_book(self):
        text = "第一章 初遇\n雨落在操场上。😀\n\n第二章 约定\n两人商量明日的训练。"
        response = self.send("/import", {"filename": "测试参考.txt", "title": "测试参考",
                             "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_import_browse_search_and_preserve_author_canon(self):
        value = self.import_book()
        self.assertEqual(value["outbound"], "local")
        listed = self.client.get("/api/reference-books").json()["books"]
        self.assertEqual(len(listed), 1)
        url = "/api/reference-books/" + value["book_id"]
        page = self.client.get(url + "?offset=0&limit=1").json()
        self.assertEqual(len(page["chapters"]), 1)
        chapter = self.client.get(url + "/chapters/" + page["chapters"][0]["chapter_id"] + "?offset=0&limit=4")
        self.assertEqual(chapter.status_code, 200, chapter.text)
        self.assertLessEqual(len(chapter.json()["text"]), 4)
        search = self.client.get(url + "/search", params={"q": "训练"})
        self.assertEqual(search.status_code, 200, search.text)
        self.assertTrue(search.json()["results"])
        self.assertEqual(self.send("/" + value["book_id"] + "/verify").status_code, 200)
        self.assertEqual(self.project.fingerprint(), self.fingerprint)
        self.assertFalse((self.project.root / "library").exists())

    def test_local_policy_revision_conflict_and_no_paid_preview(self):
        value = self.import_book()
        suffix = "/" + value["book_id"] + "/policy"
        changed = self.send(suffix, {"expected_hash": value["hash"], "outbound": "excerpts"}, "PUT")
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertGreater(changed.json()["authorization_revision"], value["authorization_revision"])
        stale = self.send(suffix, {"expected_hash": value["hash"], "outbound": "local"}, "PUT")
        self.assertEqual(stale.status_code, 409, stale.text)
        preview = self.send("/" + value["book_id"] + "/batches/preview", {
            "novel_id": "author", "chapter_ids": [], "purpose": "分析结构", "budget_max_usd": 1.0,
            "max_output_tokens": 1024})
        self.assertEqual(preview.status_code, 400, preview.text)
        self.assertEqual(self.project.fingerprint(), self.fingerprint)

    def test_import_only_large_request_exception_csrf_and_strict_fields(self):
        body = {"filename": "book.txt", "content_base64": "eA=="}
        self.assertEqual(self.client.post("/api/reference-books/import", json=body).status_code, 403)
        self.assertEqual(self.send("/import", dict(body, path="D:/private.txt")).status_code, 422)
        self.assertEqual(self.send("/import", dict(body, filename="../escape.txt")).status_code, 400)
        self.assertEqual(self.send("/import", dict(body, content_base64="!bad!")).status_code, 400)
        self.assertEqual(self.send("/import", dict(body, filename="book.exe")).status_code, 400)
        with patch("vibenovel.studio.server.MAX_REQUEST_BYTES", 64):
            ordinary = self.client.post("/api/projects", json={"title": "x" * 100}, headers=self.headers)
            self.assertEqual(ordinary.status_code, 413)
            self.assertEqual(self.send("/import", body).status_code, 201)
        with patch("vibenovel.studio.server.MAX_BOOK_REQUEST_BYTES", 16):
            self.assertEqual(self.send("/import", body).status_code, 413)
        foreign = self.client.post("/api/reference-books/import", json=body,
                                  headers={**self.headers, "Origin": "https://example.com"})
        self.assertEqual(foreign.status_code, 403)

    def test_research_methods_http_preview_publish_then_author_adoption(self):
        from vibenovel.studio.model_registry import ModelRegistry
        from vibenovel.studio.providers import GenerationResult
        registry = ModelRegistry(self.workspace)
        profile = registry.public()["profiles"][0]
        profile.update(model="offline-bridge", price_input_usd=.1, price_output_usd=.2)
        registry.save({"profiles": [profile]}, None)
        calls = []

        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                material = json.loads(prompt.split("<reference_material>\n", 1)[1].rsplit("\n</reference_material>", 1)[0])
                ref = (material["source_refs"][0] if isinstance(material, dict)
                       else material[0]["methods"][0]["source_refs"][0])
                quote = ref["quote"][:10]
                evidence = {"chapter_id": ref["chapter_id"], "start_char": ref["start_char"],
                            "end_char": ref["start_char"] + len(quote), "quote": quote}
                result = {"title": "有界观察", "summary": "只研究实际输入。", "structure": [], "expression": [],
                          "threads": [], "methods": [{"title": "用选择推进场景", "steps": ["比较行动的后果"],
                          "limitations": ["适用效果待作者评阅"], "source_refs": [evidence]}], "gaps": ["未作全书文学结论"]}
                return GenerationResult(json.dumps(result, ensure_ascii=False),
                                        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})

        self.client.app.state.jobs._factory = lambda provider: Provider()
        book = self.import_book()
        prefix = "/" + book["book_id"]
        policy = self.send(prefix + "/policy", {"expected_hash": book["hash"], "outbound": "excerpts"}, "PUT")
        self.assertEqual(policy.status_code, 200, policy.text)
        request = {"novel_id": "author", "chapter_ids": [], "purpose": "研究选择与后果", "budget_max_usd": 1.0,
                   "max_output_tokens": 1024}
        preview = self.send(prefix + "/batches/preview", request)
        self.assertEqual(preview.status_code, 200, preview.text)
        created = self.send(prefix + "/batches", {**request, "expected_preview": preview.json()["hash"],
                            "idempotency_key": uuid.uuid4().hex})
        self.assertEqual(created.status_code, 201, created.text)
        batch_path = prefix + "/batches/" + created.json()["id"]
        self.assertEqual(self.send(batch_path + "/start").status_code, 200)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            batch = self.client.get("/api/reference-books" + batch_path).json()
            if batch["state"] in {"succeeded", "failed", "interrupted"}:
                break
            time.sleep(.05)
        self.assertEqual(batch["state"], "succeeded", batch.get("error"))
        method_path = batch_path + "/nodes/" + batch["nodes"][-1]["id"] + "/methods"
        before = len(calls)
        selection = {"method_indices": [0]}
        self.assertEqual(self.client.post("/api/reference-books" + method_path + "/preview", json=selection).status_code, 403)
        self.assertEqual(self.send(method_path + "/preview", {"method_indices": [True]}).status_code, 422)
        self.assertEqual(self.send(method_path + "/preview", {**selection, "content": "伪造方法"}).status_code, 422)
        proposed = self.send(method_path + "/preview", selection)
        self.assertEqual(proposed.status_code, 200, proposed.text)
        command = {**selection, "expected_preview": proposed.json()["hash"], "idempotency_key": uuid.uuid4().hex}
        saved = self.send(method_path, command)
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(len(calls), before, "桥接预览与发布均不调用模型")
        method = saved.json()["methods"][0]
        self.assertEqual(method["outbound"], "local")
        self.assertFalse(method.get("adopted") or method.get("adoption"))
        self.assertEqual(method["source_refs"], [])
        self.assertEqual(self.send(method_path, command).json()["bundle_id"], saved.json()["bundle_id"])
        adopted = self.client.post("/api/library/personal/entries/" + method["id"] + "/adopt",
                                   json={"expected_hash": method["hash"]}, headers=self.headers)
        self.assertEqual(adopted.status_code, 200, adopted.text)
        self.assertTrue(adopted.json()["adopted"])
        self.assertEqual(self.project.fingerprint(), self.fingerprint)


if __name__ == "__main__":
    unittest.main()
