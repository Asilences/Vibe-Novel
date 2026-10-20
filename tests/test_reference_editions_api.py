"""Local HTTP curation boundaries with synthetic reference books only."""
import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
    from vibenovel.studio.reference_editions_api import install_edition_routes
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "Optional Studio dependencies")
class ReferenceEditionAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.upper().startswith("VIBENOVEL_")}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temp.name)
        app = create_app(self.root)
        if not any(getattr(r, "path", "").endswith("/edition-audit") for r in app.routes):
            install_edition_routes(app, self.root)
        self.client = TestClient(app, base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}
        raw = "第一章 测试\n甲\x00乙。\n第二章 后续\n丙丁。".encode()
        response = self.post("/api/reference-books/import", {"filename": "合成.txt", "content_base64": base64.b64encode(raw).decode()})
        self.assertEqual(response.status_code, 201, response.text)
        self.book = response.json()
        self.url = "/api/reference-books/" + self.book["book_id"]

    def post(self, url, body):
        return self.client.post(url, json=body, headers=self.headers)

    def test_preview_create_publish_select_and_map(self):
        audit = self.client.get(self.url + "/edition-audit").json()
        request = audit["proposal"]
        preview = self.post(self.url + "/edition-candidates/preview", request)
        self.assertEqual(preview.status_code, 200, preview.text)
        body = {**request, "expected_preview": preview.json()["hash"], "idempotency_key": "api-edition-000001"}
        saved = self.post(self.url + "/edition-candidates", body)
        self.assertEqual(saved.status_code, 201, saved.text)
        candidate = saved.json()
        self.assertEqual(self.client.get(self.url).json()["edition_id"], self.book["edition_id"])
        published = self.post(self.url + "/edition-candidates/" + candidate["id"] + "/publish", {"expected_candidate_hash": candidate["hash"], "expected_hash": self.book["hash"], "make_current": True})
        self.assertEqual(published.status_code, 200, published.text)
        edition = published.json()
        self.assertEqual(edition["counts"]["control_characters"], 0)
        mapped = self.client.get(self.url + "/editions/" + edition["edition_id"] + "/map").json()
        self.assertTrue(mapped["ranges"])
        selected = self.post(self.url + "/editions/" + self.book["edition_id"] + "/select", {"expected_hash": edition["hash"]})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(selected.json()["edition_id"], self.book["edition_id"])

    def test_csrf_strict_input_and_stale_hash(self):
        request = self.client.get(self.url + "/edition-audit").json()["proposal"]
        self.assertEqual(self.client.post(self.url + "/edition-candidates/preview", json=request).status_code, 403)
        self.assertEqual(self.post(self.url + "/edition-candidates/preview", {**request, "path": "C:/private"}).status_code, 422)
        self.assertEqual(self.post(self.url + "/edition-candidates/preview", {**request, "expected_hash": "0" * 64}).status_code, 409)
        request["chapters"][0]["segments"][0]["start_char"] = True
        self.assertEqual(self.post(self.url + "/edition-candidates/preview", request).status_code, 422)


if __name__ == "__main__":
    unittest.main()
