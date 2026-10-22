"""Local HTTP study integration with the shared boundary and an offline gateway."""
import unittest
import uuid

from tests import test_reference_studies as fixtures
from vibenovel.core.project import ProjectStore

try:
    from fastapi.testclient import TestClient
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "Optional Studio HTTP dependencies")
class StudyAPITests(unittest.TestCase):
    request = fixtures.StudyTests.request

    def setUp(self):
        fixtures.StudyTests.setUp(self)
        self.project = ProjectStore(self.workspace).init("synthetic-author", genre="campus")
        self.canon = self.project.fingerprint()
        self.app = create_app(self.workspace)
        self.app.state.reference_studies.gateway = self.gateway
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"X-VibeNovel-Token": self.client.get("/api/bootstrap").json()["csrf_token"]}

    def post(self, suffix, payload=None):
        return self.client.post("/api/reference-studies" + suffix, json=payload or {}, headers=self.headers)

    def prepare(self, request=None):
        request = request or self.request()
        quoted = self.post("/preview", request)
        self.assertEqual(quoted.status_code, 200, quoted.text)
        response = self.post("", {**request, "expected_preview": quoted.json()["hash"], "idempotency_key": uuid.uuid4().hex})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_http_prepared_start_evidence_maps_and_method_bridge(self):
        study = self.prepare()
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.client.get("/api/reference-studies").status_code, 200)
        detail = self.client.get("/api/reference-studies/" + study["id"] + "?offset=1&limit=1")
        self.assertEqual(len(detail.json()["nodes"]), 1)
        self.assertEqual(detail.json()["nodes"][0]["order"], 1)
        started = self.post("/" + study["id"] + "/start")
        self.assertEqual(started.status_code, 200, started.text)
        self.app.state.reference_studies._threads[study["id"]].join(timeout=15)
        exported = self.client.get("/api/reference-studies/" + study["id"] + "/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(len(exported.json()["results"]), 2)
        self.assertTrue(exported.json()["graphs"]["characters"]["nodes"])
        preview = self.post("/" + study["id"] + "/methods/preview", {"method_indices": [0]})
        self.assertEqual(preview.status_code, 200, preview.text)
        published = self.post("/" + study["id"] + "/methods", {"method_indices": [0], "expected_preview": preview.json()["hash"], "idempotency_key": uuid.uuid4().hex})
        self.assertEqual(published.status_code, 201, published.text)
        self.assertFalse(published.json()["methods"][0]["adopted"])
        self.assertEqual(self.project.fingerprint(), self.canon)
        self.assertFalse((self.project.root / "library").exists())

    def test_http_strict_schema_csrf_origin_and_no_arbitrary_paths(self):
        request = self.request()
        response = self.client.post("/api/reference-studies/preview", json=request)
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/reference-studies/preview", json=request,
                                    headers={**self.headers, "Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        for changes in ({"command": "do anything"}, {"sources": [{"book_id": "../" + "a"*29, "edition_id": "b"*32}]},
                        {"max_output_tokens": True}, {"kind": "execute"}):
            response = self.post("/preview", {**request, **changes})
            self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.gateway.calls, [])

    def test_late_policy_change_invalidates_prepared_and_published_method(self):
        study = self.prepare()
        started = self.post("/" + study["id"] + "/start")
        self.assertEqual(started.status_code, 200)
        self.app.state.reference_studies._threads[study["id"]].join(timeout=15)
        current = self.books.detail(self.book["book_id"])
        self.books.update_policy(current["book_id"], expected_hash=current["hash"], outbound="local", withdrawn=True)
        exported = self.client.get("/api/reference-studies/" + study["id"] + "/export")
        self.assertEqual(exported.status_code, 409, exported.text)
        preview = self.post("/" + study["id"] + "/methods/preview", {"method_indices": [0]})
        self.assertEqual(preview.status_code, 409, preview.text)

    def test_reread_exact_range_stays_retrospective_without_rewriting_first_read(self):
        first = self.prepare()
        self.assertEqual(self.post("/" + first["id"] + "/start").status_code, 200)
        self.app.state.reference_studies._threads[first["id"]].join(timeout=15)
        url = "/api/reference-studies/" + first["id"] + "/export"
        original = self.client.get(url).json()
        self.assertEqual(original["state"], "succeeded")
        self.assertTrue(all(row["temporal_mode"] == "first_read" for row in original["results"]))
        study_root = self.app.state.reference_studies.store.path(first["id"])
        saved = {path.relative_to(study_root): path.read_bytes() for path in study_root.rglob("*") if path.is_file()}

        chapter = self.book["chapters"][1]
        request = self.request()
        request.update(kind="reread", question="回顾核对渡口信息，仅分析明确选段，不回填初读知情。")
        request["sources"][0] = {"book_id": self.book["book_id"], "edition_id": self.book["edition_id"],
                                  "ranges": [{"chapter_id": chapter["chapter_id"], "start_char": 1, "end_char": 20}]}
        calls_before = len(self.gateway.calls)
        reread = self.prepare(request)
        self.assertEqual(len(self.gateway.calls), calls_before)
        self.assertEqual(reread["state"], "prepared")
        self.assertEqual(self.post("/" + reread["id"] + "/start").status_code, 200)
        self.app.state.reference_studies._threads[reread["id"]].join(timeout=15)
        reviewed = self.client.get("/api/reference-studies/" + reread["id"] + "/export").json()
        self.assertEqual(reviewed["state"], "succeeded", reviewed.get("error"))
        self.assertEqual(len(self.gateway.calls), calls_before + 1)
        self.assertEqual(len(reviewed["results"]), 1)
        self.assertEqual(reviewed["results"][0]["temporal_mode"], "retrospective")
        self.assertEqual(reviewed["results"][0]["refs"][0]["start_char"], 1)
        self.assertEqual(reviewed["results"][0]["refs"][0]["end_char"], 20)
        self.assertFalse(reviewed["coverage"]["sources"][0]["whole_current_file"])
        self.assertEqual(self.client.get(url).json(), original)
        self.assertEqual({path.relative_to(study_root): path.read_bytes() for path in study_root.rglob("*") if path.is_file()}, saved)
        self.assertEqual(self.project.fingerprint(), self.canon)


if __name__ == "__main__":
    unittest.main()
