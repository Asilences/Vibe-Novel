"""Offline HTTP coverage of private references and author method decisions."""
from __future__ import annotations

import base64
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibenovel.core.project import ProjectStore

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, 'Optional Studio tests: install ".[studio,test]"')
class LibraryAPITests(unittest.TestCase):
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
        projects = ProjectStore(self.workspace)
        self.project = projects.init("story", genre="campus")
        self.other = projects.init("other", genre="football")
        self.fingerprint = self.project.fingerprint()
        self.client = TestClient(create_app(self.workspace), base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}

    def url(self, suffix="", scope="personal", novel_id=None):
        result = "/api/library/" + scope + suffix
        if novel_id:
            result += ("&" if "?" in result else "?") + "novel_id=" + novel_id
        return result

    def send(self, suffix, body, method="POST", scope="personal", novel_id=None):
        return self.client.request(method, self.url(suffix, scope, novel_id), json=body, headers=self.headers)

    def source(self, text="# 教材\r\n动作😀先于解释。\r\n\r\n每个场景应有变化。", scope="personal"):
        response = self.send("/sources", {"filename": "人工测试.md",
            "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "metadata": {"title": "人工写作教材", "outbound": "local"}},
            scope=scope, novel_id="story" if scope == "book" else None)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def entry(self, kind="material", scope="personal", **extra):
        value = {"type": kind, "title": "清晨训练素材", "content": "训练场的鞋钉擦过湿草。",
                 "tags": ["足球", "训练"], "outbound": "excerpts", **extra}
        response = self.send("/entries", {"entry": value, "expected_hash": None},
                             scope=scope, novel_id="story" if scope == "book" else None)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json(), value

    def test_scope_is_explicit_and_does_not_scan_other_books(self):
        self.entry(scope="book")
        self.assertEqual(self.client.get(self.url()).json()["entries"], [])
        self.assertEqual(self.client.get(self.url(scope="book", novel_id="other")).json()["entries"], [])
        self.assertEqual(self.client.get(self.url(scope="book")).status_code, 400)
        self.assertEqual(self.client.get(self.url(novel_id="story")).status_code, 400)
        self.assertEqual(self.client.get("/api/library/unknown").status_code, 422)
        self.assertEqual(self.project.fingerprint(), self.fingerprint)

    def test_source_preserves_text_and_metadata_history(self):
        source = self.source()
        self.assertIn("\r\n", source["text"])
        self.assertIn("😀", source["text"])
        self.assertEqual(source["outbound"], "local")
        metadata = dict(source["metadata"], outbound="excerpts")
        updated = self.send("/sources/" + source["id"], {
            "metadata": metadata, "expected_hash": source["hash"], "withdrawn": False,
        }, "PUT")
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["text"], source["text"])
        original = self.client.get(self.url("/sources/" + source["id"] + "?version_id=" + source["version_id"])).json()
        self.assertEqual(original["outbound"], "local")
        history = self.client.get(self.url("/sources/" + source["id"] + "/history")).json()["versions"]
        self.assertEqual(len(history), 2)
        stale = self.send("/sources/" + source["id"], {"metadata": metadata,
                         "expected_hash": source["hash"]}, "PUT")
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_source_rejects_invalid_bytes_paths_and_extra_fields(self):
        for filename, content in (("../escape.md", b"test"), ("book.pdf", b"test"), ("book.md", b"\xff")):
            result = self.send("/sources", {"filename": filename,
                "content_base64": base64.b64encode(content).decode("ascii")})
            self.assertEqual(result.status_code, 400, result.text)
        bad = self.send("/sources", {"filename": "x.md", "content_base64": "!not-base64!"})
        self.assertEqual(bad.status_code, 400)
        extra = self.send("/sources", {"filename": "x.md", "content_base64": "eA==", "path": "chapters/ch001.md"})
        self.assertEqual(extra.status_code, 422)
        missing_token = self.client.post(self.url("/entries"), json={"entry": {}, "expected_hash": None})
        self.assertEqual(missing_token.status_code, 403)

    def test_entry_revisions_search_collections_and_research(self):
        entry, value = self.entry()
        value["content"] += "清晨的雾气尚未散去。"
        newer = self.send("/entries/" + entry["id"], {"entry": value, "expected_hash": entry["hash"]}, "PUT")
        self.assertEqual(newer.status_code, 200, newer.text)
        self.assertNotEqual(newer.json()["version_id"], entry["version_id"])
        stale = self.send("/entries/" + entry["id"], {"entry": value, "expected_hash": entry["hash"]}, "PUT")
        self.assertEqual(stale.status_code, 409)
        history = self.client.get(self.url("/entries/" + entry["id"] + "/history")).json()["versions"]
        self.assertEqual(len(history), 2)
        query = self.client.get(self.url("/search?q=清晨训练"))
        self.assertEqual(query.status_code, 200, query.text)
        self.assertIn(entry["id"], [item["id"] for item in query.json()["results"]])
        collection = self.send("/collections", {"collection": {"title": "训练场", "entry_ids": [entry["id"]]}, "expected_hash": None})
        self.assertEqual(collection.status_code, 201, collection.text)
        question = self.send("/research", {"question": {"title": "场地考据", "question": "雨天是否暂停训练？",
                             "status": "disputed", "entry_ids": [entry["id"]]}, "expected_hash": None})
        self.assertEqual(question.status_code, 201, question.text)
        self.assertEqual(self.project.fingerprint(), self.fingerprint)

    def test_personal_binding_is_fixed_and_policy_is_book_scoped(self):
        entry, payload = self.entry()
        binding = self.send("/bindings", {"entry_id": entry["id"], "version_id": entry["version_id"]},
                            scope="book", novel_id="story")
        self.assertEqual(binding.status_code, 201, binding.text)
        payload["content"] = "新的素材版本。"
        self.assertEqual(self.send("/entries/" + entry["id"], {"entry": payload, "expected_hash": entry["hash"]}, "PUT").status_code, 200)
        book = self.client.get(self.url(scope="book", novel_id="story")).json()
        self.assertEqual(book["bindings"][0]["snapshot"]["content"], entry["content"])
        policy = copy.deepcopy(book["policy"])
        policy.update(mode="auto", required_ids=[binding.json()["id"]])
        result = self.send("/policy", {"policy": policy, "expected_hash": book["policy_hash"]},
                           "PUT", scope="book", novel_id="story")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.send("/policy", {"policy": policy, "expected_hash": None}, "PUT").status_code, 400)
        self.assertEqual(self.project.fingerprint(), self.fingerprint)

    def test_method_adoption_is_a_separate_versioned_author_action(self):
        method, payload = self.entry(kind="method", scope="book", title="场景变化检查",
            method={"purpose": "检查场景", "steps": ["标记人物行动前后的处境"],
                    "inputs": ["场景草稿"], "outputs": ["变化清单"]})
        self.assertFalse(method["adopted"])
        adopted = self.send("/entries/" + method["id"] + "/adopt", {"expected_hash": method["hash"]},
                            scope="book", novel_id="story")
        self.assertEqual(adopted.status_code, 200, adopted.text)
        self.assertTrue(adopted.json()["adopted"])
        changed = self.send("/entries/" + method["id"], {"entry": payload, "expected_hash": adopted.json()["hash"]},
                            "PUT", scope="book", novel_id="story")
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertFalse(changed.json()["adopted"])
        self.assertEqual(self.project.fingerprint(), self.fingerprint)


if __name__ == "__main__":
    unittest.main()
