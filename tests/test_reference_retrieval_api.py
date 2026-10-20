"""HTTP retrieval remains local and keeps existing origin/CSRF boundaries."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_reference_retrieval import FixtureEmbedding
from vibenovel.core.reference_books import ReferenceBookStore

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
    from vibenovel.studio.reference_retrieval_api import install_reference_retrieval_routes
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "Optional Studio dependencies")
class RetrievalAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.book = ReferenceBookStore(self.workspace).import_book("人工.txt", "第一章\n父亲独自照料孩子。".encode(), title="人工参考")
        engine = patch("vibenovel.core.reference_retrieval.engine_for", return_value=FixtureEmbedding())
        engine.start()
        self.addCleanup(engine.stop)
        app = create_app(self.workspace)
        if not any(route.path == "/api/reference-retrieval" for route in app.routes):
            install_reference_retrieval_routes(app, self.workspace)
        self.client = TestClient(app, base_url="http://127.0.0.1:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}

    def test_preview_build_local_search_and_outbound_rejection(self):
        body = {"book_ids": [self.book["book_id"]], "layers": ["original", "structure", "expression", "method"]}
        preview = self.client.post("/api/reference-retrieval/preview", json=body, headers=self.headers)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["cost_usd"], 0)
        built = self.client.post("/api/reference-retrieval/indexes", json={**body, "expected_preview": preview.json()["hash"]}, headers=self.headers)
        self.assertEqual(built.status_code, 201, built.text)
        identity = built.json()["id"]
        for _ in range(100):
            value = self.client.get("/api/reference-retrieval/indexes/" + identity).json()
            if value["state"] != "building":
                break
            time.sleep(.01)
        self.assertEqual(value["state"], "ready", value)
        response = self.client.post(f"/api/reference-retrieval/indexes/{identity}/search", json={"query": "家庭"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["outbound"])
        self.assertIn("父亲", response.json()["results"][0]["text"])
        selected = self.client.post(f"/api/reference-retrieval/indexes/{identity}/selection/preview", json={"ids": [response.json()["results"][0]["id"]]}, headers=self.headers)
        self.assertEqual(selected.status_code, 409, selected.text)

    def test_csrf_foreign_origin_and_arbitrary_paths_rejected(self):
        body = {"book_ids": [self.book["book_id"]], "layers": ["original"]}
        self.assertEqual(self.client.post("/api/reference-retrieval/preview", json=body).status_code, 403)
        response = self.client.post("/api/reference-retrieval/preview", json=body, headers={**self.headers, "Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/reference-retrieval/preview", json={**body, "path": "D:/private"}, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.post("/api/reference-retrieval/preview", json={**body, "book_ids": ["../other"]}, headers=self.headers).status_code, 400)
