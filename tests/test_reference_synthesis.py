"""Offline evidence and accounting boundaries for retrospective synthesis."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

from vibenovel.core.files import digest, json_bytes
from vibenovel.core.locking import project_lock
from vibenovel.core.reference_synthesis import ReferenceSynthesisManager, _Lock, contract, validate_result
from vibenovel.errors import VibeNovelError
from vibenovel.studio.providers import GenerationResult
from tests import test_reference_studies as study_fixtures


def candidate(payload):
    evidence = payload["evidence"][0]
    ref = {"source_id": evidence["source_id"], "quote": evidence["quote"]}
    first, last = payload["input_coverage"]["first_read_index"], payload["input_coverage"]["last_read_index"]
    return {"summary": "只覆盖当前输入范围的回顾归并", "stages": [{"title": "行动与后果", "first_read_index": first, "last_read_index": last,
        "goal": "过河", "turning_point": "寻找绕行", "outcome": "找到新路径", "source_refs": [ref]}],
        "main_lines": [{"title": "寻找路径", "development": "目标与阻力交织", "source_refs": [ref]}],
        "character_arcs": [{"name": "小明", "trajectory": "由犹豫到行动", "source_refs": [ref]}],
        "organization": [{"title": "选择与代价", "mechanism": "给出具体阻力", "limitations": ["仅当前范围"], "source_refs": [ref]}],
        "expression": [{"title": "动作细节", "technique": "具体动词", "effect": "提供可见行动", "limitations": ["短引文不能代表全书文风"], "source_refs": [ref]}],
        "methods": [{"title": "先目标后阻力", "steps": ["明确目标", "设置可见障碍"], "limitations": ["仅为候选"], "source_refs": [ref]}],
        "gaps": ["没有重新发送完整原文"]}


class Gateway:
    def __init__(self):
        self.calls, self.bad, self.callback, self.config = [], False, None, "offline-config"

    def preview(self, prompt, maximum):
        return {"hash": digest(prompt.encode()), "model_identity": "offline-v1", "model_id": "offline", "config_hash": self.config,
                "estimated_cost_usd": (len(prompt.encode()) + maximum) / 10000000, "max_output_tokens": maximum}

    def generate(self, preview, prompt, cancel, key):
        self.calls.append((prompt, key))
        if self.callback:
            self.callback()
        payload = json.loads(prompt.split("<untrusted_reading_candidates>\n", 1)[1].split("\n</untrusted_reading_candidates>", 1)[0])
        return GenerationResult("invalid" if self.bad else json.dumps(candidate(payload), ensure_ascii=False), {"prompt_tokens": 10, "completion_tokens": 20})


class SynthesisTests(unittest.TestCase):
    def setUp(self):
        self.fixture = study_fixtures.StudyTests(methodName="test_prepared_continuous_checkpoints_four_maps_and_method_origin")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.workspace
        self.study = self.fixture.run_study(self.fixture.create())
        self.assertEqual(self.study["state"], "succeeded", self.study.get("error"))
        self.gateway = Gateway()
        self.manager = ReferenceSynthesisManager(self.root, self.gateway)
        self.addCleanup(self.manager.close)

    def request(self):
        return {"study_id": self.study["id"], "stage_size": 30, "budget_max_usd": 3., "max_output_tokens": 6144}

    def create(self):
        request = self.request()
        preview, _ = self.manager.preview(request)
        return self.manager.create(request, preview["hash"], "offline-synthesis-key-001")

    def run_synthesis(self, record):
        self.manager.control(record["id"], "start")
        self.manager._threads[record["id"]].join(15)
        self.assertFalse(self.manager._threads[record["id"]].is_alive())
        return self.manager.detail(record["id"])

    def test_prepared_dependency_tree_expression_origin_and_no_automatic_adoption(self):
        record = self.create()
        self.assertEqual(record["state"], "prepared")
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(len(record["nodes"]), 2)
        self.assertEqual(record["nodes"][-1]["depends_on"], ["stage-0001"])
        final = self.run_synthesis(record)
        self.assertEqual(final["state"], "succeeded", final["error"])
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertIn("reading_observations", self.gateway.calls[0][0])
        self.assertNotIn('"reading_observations"', self.gateway.calls[1][0])
        self.assertEqual(self.manager.export_verified(record["id"])["state"], "succeeded")
        preview = self.manager.preview_methods(record["id"], [0])
        self.assertEqual(preview["origin"]["source_type"], "reference_synthesis")
        self.assertTrue(self.manager.validate_origin(preview["origin"]))
        self.assertFalse((self.root / "library/.state/manifest.json").exists())
        with self.assertRaises(VibeNovelError):
            self.manager.control(record["id"], "start")
        self.assertEqual(len(self.gateway.calls), 2)

    def test_partial_reading_cannot_be_called_finished(self):
        exported = self.manager.studies.export_verified(self.study["id"])
        exported["coverage"]["complete_selected_file"] = False
        with patch.object(self.manager.studies, "export_verified", return_value=exported):
            with self.assertRaises(VibeNovelError):
                self.manager.preview(self.request())
        self.assertEqual(self.gateway.calls, [])

    def test_31_segments_split_30_and_1_with_all_summaries_and_four_way_merges(self):
        exported = self.manager.studies.export_verified(self.study["id"])
        prototype = exported["results"][0]
        exported["results"] = [{**copy.deepcopy(prototype), "node_id": "read-%05d" % i, "read_index": i} for i in range(151)]
        exported["coverage"]["total_segments"] = 151
        with patch.object(self.manager.studies, "export_verified", return_value=exported):
            preview, payloads = self.manager.preview(self.request())
        self.assertEqual([len(payload["reading_observations"]) for payload in payloads.values()], [30, 30, 30, 30, 30, 1])
        self.assertEqual(len(preview["nodes"]), 9)
        self.assertEqual(len(preview["nodes"][6]["depends_on"]), 4)
        self.assertEqual(len(preview["nodes"][7]["depends_on"]), 2)
        self.assertEqual(preview["nodes"][-1]["depends_on"], ["merge-01-0001", "merge-01-0002"])
        self.assertEqual(self.gateway.calls, [])

    def test_stale_source_before_send_and_late_source_change_block_publication(self):
        record = self.create()
        current = self.fixture.books.detail(self.fixture.book["book_id"])
        self.fixture.books.update_policy(current["book_id"], expected_hash=current["hash"], outbound="local")
        with self.assertRaises(VibeNovelError):
            self.manager.control(record["id"], "start")
        self.assertEqual(self.gateway.calls, [])

    def test_late_authorization_change_preserves_raw_response_without_publishing(self):
        record = self.create()
        def withdraw():
            current = self.fixture.books.detail(self.fixture.book["book_id"])
            self.fixture.books.update_policy(current["book_id"], expected_hash=current["hash"], outbound="local")
        self.gateway.callback = withdraw
        final = self.run_synthesis(record)
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["results"], [])
        self.assertTrue(self.manager.response(record["id"], "stage-0001")["text"])
        self.assertEqual(len(self.gateway.calls), 1)

    def test_failed_json_offline_repair_continues_only_pending_nodes(self):
        self.gateway.bad = True
        final = self.run_synthesis(self.create())
        self.assertEqual(final["state"], "failed")
        raw = self.manager.response(final["id"], "stage-0001")
        record = self.manager._load(final["id"])
        prompt, _ = self.manager._input(record, 0)
        payload = json.loads(prompt.split("<untrusted_reading_candidates>\n", 1)[1].split("\n</untrusted_reading_candidates>", 1)[0])
        valid = json.dumps(candidate(payload), ensure_ascii=False)
        repaired = self.manager.repair(final["id"], "stage-0001", expected_hash=raw["hash"], raw=valid, reason="修复无效JSON，保留原响应")
        self.assertEqual(repaired["state"], "paused")
        self.assertEqual(len(self.gateway.calls), 1)
        self.gateway.bad = False
        result = self.run_synthesis(repaired)
        self.assertEqual(result["state"], "succeeded", result["error"])
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(self.manager.response(final["id"], "stage-0001")["text"], "invalid")

    def test_config_change_budget_and_idempotency(self):
        request = self.request()
        request["budget_max_usd"] = .000001
        with self.assertRaises(VibeNovelError):
            self.manager.preview(request)
        record = self.create()
        same = self.manager.create(self.request(), self.manager._load(record["id"])["plan_hash"], "offline-synthesis-key-001")
        self.assertEqual(same["id"], record["id"])
        self.gateway.config = "changed"
        final = self.run_synthesis(record)
        self.assertEqual(final["state"], "failed")
        self.assertEqual(self.gateway.calls, [])

    def test_restart_keeps_unknown_and_does_not_interrupt_live_lease(self):
        record = self.create()
        value = self.manager._load(record["id"])
        value["state"] = "running"
        value["nodes"][0]["state"] = "running"
        self.manager._save(value)
        entered, release = threading.Event(), threading.Event()
        def hold():
            with project_lock(_Lock(self.manager, record["id"] + "/runner"), timeout=1):
                entered.set(); release.wait(5)
        worker = threading.Thread(target=hold)
        worker.start(); self.assertTrue(entered.wait(2))
        try:
            live = ReferenceSynthesisManager(self.root, self.gateway, recover=True)
            self.addCleanup(live.close)
            self.assertEqual(live.detail(record["id"])["state"], "running")
        finally:
            release.set(); worker.join(3)
        recovered = ReferenceSynthesisManager(self.root, self.gateway, recover=True)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.detail(record["id"])["state"], "interrupted")
        with self.assertRaises(VibeNovelError):
            recovered.control(record["id"], "start")
        self.assertEqual(self.gateway.calls, [])

    def test_fake_quote_and_out_of_range_stage_rejected(self):
        _, payloads = self.manager.preview(self.request())
        payload = next(iter(payloads.values()))
        value = candidate(payload)
        refs = [{k: v for k, v in item.items() if k != "source_id"} for item in payload["evidence"]]
        value["stages"][0]["last_read_index"] = 99
        with self.assertRaises(VibeNovelError):
            validate_result(json.dumps(value, ensure_ascii=False), refs, 0, 1)
        value = candidate(payload)
        value["expression"][0]["source_refs"][0] = {"source_id": payload["evidence"][0]["source_id"], "quote": "从未发送的文字"}
        with self.assertRaises(VibeNovelError):
            validate_result(json.dumps(value, ensure_ascii=False), refs, 0, 1)

    def test_method_candidate_publish_and_four_layer_index_keep_dependency(self):
        from vibenovel.core.library import LibraryStore
        from vibenovel.core.reference_retrieval import ReferenceRetrievalStore
        from tests.test_reference_retrieval import FixtureEmbedding
        final = self.run_synthesis(self.create())
        library = LibraryStore(self.root)
        preview = library.preview_synthesis_methods(final["id"], [0])
        published = library.publish_synthesis_methods(final["id"], [0], preview["hash"], "offline-synthesis-method-001")
        self.assertTrue(published)
        entries = [library._read(library._state(), "entry", ident) for ident in library._state()["entities"]["entry"]]
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].get("adoption"))
        self.assertEqual(entries[0]["research_origin"], preview["origin"])
        retrieval = ReferenceRetrievalStore(self.root, engine=FixtureEmbedding())
        self.addCleanup(retrieval.close)
        ids, layers = [self.fixture.book["book_id"]], ["structure", "expression", "method"]
        planned = retrieval.preview(ids, layers)
        built = retrieval.build(ids, layers, planned["hash"], background=False)
        self.assertEqual(built["state"], "ready", built.get("error"))
        self.assertGreater(built["progress"]["layers"]["expression"], 0)
        self.assertEqual(built["progress"]["layers"]["method"], 0)
        manifest = retrieval._manifest(built["id"])
        documents = [item for item in retrieval._documents(manifest, []) if item["locator"]["kind"] == "synthesis_result"]
        self.assertTrue(documents)
        result = retrieval._resolve(documents[0], manifest, outbound=True)
        self.assertTrue(result)
        current = self.fixture.books.detail(self.fixture.book["book_id"])
        self.fixture.books.update_policy(current["book_id"], expected_hash=current["hash"], outbound=current["outbound"], withdrawn=True)
        with self.assertRaises(VibeNovelError):
            retrieval.search(built["id"], "行动")
        with self.assertRaises(VibeNovelError):
            library.preview_synthesis_methods(final["id"], [0])

    def test_full_1091_reading_nodes_plan_has_51_bounded_requests(self):
        exported = self.manager.studies.export_verified(self.study["id"])
        prototype = exported["results"][0]
        exported["results"] = [{**copy.deepcopy(prototype), "node_id": "read-%05d" % i, "read_index": i} for i in range(1091)]
        exported["coverage"]["total_segments"] = 1091
        with patch.object(self.manager.studies, "export_verified", return_value=exported):
            preview, payloads = self.manager.preview(self.request())
        self.assertEqual(len(payloads), 37)
        self.assertEqual(len(preview["nodes"]), 51)
        self.assertTrue(all(len(node["depends_on"]) <= 4 for node in preview["nodes"]))
        self.assertLessEqual(preview["estimated_cost_usd"], 3.)
        self.assertEqual(preview["nodes"][-1]["last_read_index"], 1090)
        # Synthetic low-price quotation proves topology and the local cap only;
        # actual-book quotations still require every frozen real observation.

    def test_http_prepare_start_export_methods_and_strict_request(self):
        from fastapi.testclient import TestClient
        from vibenovel.studio.server import create_app
        app = create_app(self.root)
        app.state.reference_syntheses.gateway = self.gateway
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            headers = {"X-VibeNovel-Token": client.get("/api/bootstrap").json()["csrf_token"]}
            prefix = "/api/reference-syntheses"
            self.assertEqual(client.post(prefix + "/preview", json=self.request()).status_code, 403)
            self.assertEqual(client.post(prefix + "/preview", json={**self.request(), "stage_size": True}, headers=headers).status_code, 422)
            response = client.post(prefix + "/preview", json=self.request(), headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            response = client.post(prefix, json={**self.request(), "expected_preview": response.json()["hash"], "idempotency_key": "http-synthesis-key-001"}, headers=headers)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(self.gateway.calls, [])
            sid = response.json()["id"]
            self.assertEqual(client.post(prefix + "/" + sid + "/start", json={}, headers=headers).status_code, 200)
            app.state.reference_syntheses._threads[sid].join(15)
            exported = client.get(prefix + "/" + sid + "/export")
            self.assertEqual(exported.status_code, 200, exported.text)
            self.assertEqual(exported.json()["state"], "succeeded")
            response = client.post(prefix + "/" + sid + "/methods/preview", json={"method_indices": [0]}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            response = client.post(prefix + "/" + sid + "/methods", json={"method_indices": [0], "expected_preview": response.json()["hash"], "idempotency_key": "http-synthesis-method-001"}, headers=headers)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertFalse(response.json()["methods"][0]["adopted"])

    def test_actual_private_gateway_enforces_shared_ceiling_and_caches_paid_node(self):
        from vibenovel.core.research_gateway import ResearchGateway
        from vibenovel.studio.model_registry import ModelRegistry
        registry = ModelRegistry(self.root)
        profile = registry.public()["profiles"][0]
        profile.update(model="offline-test", context_tokens=1000000, max_output_tokens=16000)
        registry.save({"profiles": [profile]}, None)
        outer = self
        class Provider:
            def generate(self, prompt, cancel):
                payload = json.loads(prompt.split("<untrusted_reading_candidates>\n", 1)[1].split("\n</untrusted_reading_candidates>", 1)[0])
                outer.gateway.calls.append((prompt, "actual-gateway-offline"))
                return GenerationResult(json.dumps(candidate(payload), ensure_ascii=False), {"prompt_tokens": 10, "completion_tokens": 20})
        gateway = ResearchGateway(self.root, provider_factory=lambda _: Provider())
        gateway.configure(model_id="default_api", max_cost_usd=.02, price_input_usd=1., price_output_usd=1.)
        manager = ReferenceSynthesisManager(self.root, gateway)
        self.addCleanup(manager.close)
        preview, _ = manager.preview(self.request())
        record = manager.create(self.request(), preview["hash"], "real-gateway-synthesis-offline")
        manager.control(record["id"], "start")
        manager._threads[record["id"]].join(15)
        final = manager.detail(record["id"])
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["completed_nodes"], 1)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(gateway.status()["calls"], 1)
        self.assertLessEqual(gateway.status()["reserved_cost_usd"], .02)
        saved = manager.path(record["id"] + "/requests/stage-0001.json")
        request = json.loads(saved.read_text(encoding="utf-8"))
        cached = gateway.generate(request["preview"], request["prompt"], threading.Event(), request["attempt_key"])
        self.assertTrue(cached.text)
        self.assertEqual(len(self.gateway.calls), 1)


if __name__ == "__main__":
    unittest.main()
