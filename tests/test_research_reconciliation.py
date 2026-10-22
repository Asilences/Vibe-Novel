"""Reconcile old paid attempts offline, without claiming a stopped live sender."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from vibenovel.core.files import atomic_write, digest, json_bytes, read_json
from vibenovel.core.locking import project_lock
from vibenovel.core.research_gateway import ResearchGateway, _ResearchRunnerLease
from vibenovel.errors import VibeNovelError
from vibenovel.studio.model_registry import ModelRegistry
from vibenovel.studio.server import create_app


class ResearchReconciliationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name)
        env = patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                     if not k.upper().startswith("VIBENOVEL_")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        registry = ModelRegistry(self.workspace)
        profile = registry.public()["profiles"][0]
        profile.update(model="offline-research", base_url="https://example.invalid/v1",
                       api_key="synthetic-test-key")
        registry.save({"profiles": [profile]}, None)
        self.gateway = ResearchGateway(self.workspace, provider_factory=self.no_provider)
        self.gateway.configure(model_id="default_api", max_cost_usd=5,
                               price_input_usd=1, price_output_usd=2)
        self.number = 0

    def no_provider(self, *_):
        self.fail("reconciliation must never construct a model provider")

    def seed(self, *, kind="study", state="reserved", cache=None):
        self.number += 1
        record_id = f"{self.number:032x}"
        node_id = "read-00001"
        key = f"{kind}_{record_id}_{node_id}"
        attempt_id = digest(key.encode())[:32]
        prompt = "原冻结研究输入，不应重新发送"
        quote = self.gateway.preview(prompt, 256)
        folder = {"study": "studies", "evaluation": "evaluations", "synthesis": "syntheses"}[kind]
        root = self.workspace / "library" / folder / record_id
        (root / "requests").mkdir(parents=True)
        plan = {"nodes": [{"id": node_id}]}
        manifest = {"schema_version": 1, "id": record_id, "plan": plan,
                    "plan_hash": digest(json_bytes(plan)),
                    "nodes": [{"id": node_id, "attempt_key": key, "state": "interrupted"}]}
        atomic_write(root / "manifest.json", json_bytes(manifest))
        atomic_write(root / "requests" / f"{node_id}.json",
                     json_bytes({"prompt": prompt, "preview": quote, "attempt_key": key}))
        attempt = {"id": attempt_id, "preview_hash": quote["hash"], "prompt_hash": quote["prompt_hash"],
                   "state": state, "model_id": quote["model_id"], "model": quote["model"],
                   "reserved_cost_usd": quote["estimated_cost_usd"], "created_at": "2000-01-01T00:00:00Z"}
        account = self.gateway._load()
        account["attempts"][attempt_id] = attempt
        self.gateway._save(account)
        if cache is not None:
            atomic_write(self.gateway.path(f"responses/{attempt_id}.json"), json_bytes(cache))
        runner = (f"library/studies/.runners/{record_id}" if kind == "study"
                  else f"library/{folder}/{record_id}/runner")
        return {"id": attempt_id, "root": root, "key": key, "prompt": prompt,
                "quote": quote, "runner": runner, "reserved": attempt["reserved_cost_usd"]}

    def pending(self, fixture):
        return next(item for item in self.gateway.status()["pending_reconciliations"] if item["id"] == fixture["id"])

    def reconcile(self, fixture, action="mark_unknown", **overrides):
        item = self.pending(fixture)
        return self.gateway.reconcile(fixture["id"], action=action, reason="核对本地中断记录",
            expected_attempt_hash=overrides.get("attempt_hash", item["attempt_hash"]),
            expected_response_hash=overrides.get("response_hash", item["cached_response_hash"]))

    def test_live_sender_lock_blocks_even_ancient_attempt_for_all_sender_types(self):
        for kind in ("study", "evaluation", "synthesis"):
            fixture = self.seed(kind=kind)
            before = self.gateway.path("account.json").read_bytes()
            with project_lock(_ResearchRunnerLease(self.workspace, fixture["runner"]), timeout=0):
                with self.assertRaises(VibeNovelError):
                    self.reconcile(fixture)
            self.assertEqual(self.gateway.path("account.json").read_bytes(), before)
        self.assertFalse(self.gateway.path("reconciliations").exists())

    def test_explicit_unknown_preserves_cost_and_audit_then_allows_budget_change_without_replay(self):
        fixture = self.seed()
        status = self.reconcile(fixture)
        self.assertEqual((status["calls"], status["uncertain_calls"]), (1, 1))
        self.assertEqual(status["reserved_cost_usd"], fixture["reserved"])
        attempt = self.gateway._load()["attempts"][fixture["id"]]
        self.assertEqual(attempt["state"], "unknown")
        audit_ref = attempt["reconciliations"][0]
        raw = self.gateway.path(f"reconciliations/{audit_ref['id']}.json").read_bytes()
        self.assertEqual(digest(raw), audit_ref["hash"])
        audit = read_json(self.gateway.path(f"reconciliations/{audit_ref['id']}.json"))
        self.assertEqual((audit["before"]["state"], audit["network_requests"]), ("reserved", 0))
        self.assertEqual(audit["reason"], "核对本地中断记录")
        with self.assertRaises(VibeNovelError):
            self.gateway.generate(fixture["quote"], fixture["prompt"], threading.Event(), fixture["key"])
        with self.assertRaises(VibeNovelError):
            self.reconcile(fixture)
        after = self.gateway.configure(model_id="default_api", max_cost_usd=35,
                                       price_input_usd=1, price_output_usd=2)
        self.assertEqual(after["reserved_cost_usd"], fixture["reserved"])

    def test_cache_restoration_uses_original_frozen_prices_and_never_reduces_reserve(self):
        fixture = self.seed(state="unknown", cache={"text": "原响应", "model": "offline-research",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}, "finish_reason": "stop"})
        self.gateway.configure(model_id="default_api", max_cost_usd=35,
                               price_input_usd=7, price_output_usd=11)
        status = self.reconcile(fixture, "restore_response")
        self.assertAlmostEqual(status["reported_cost_usd"], .00012)
        self.assertEqual(status["reserved_cost_usd"], fixture["reserved"])
        self.assertEqual(status["uncertain_calls"], 0)
        self.assertEqual(status["pending_reconciliations"], [])
        self.assertEqual(self.gateway._load()["attempts"][fixture["id"]]["state"], "succeeded")

    def test_missing_usage_keeps_uncertainty_and_large_valid_usage_increases_accounted_cost(self):
        missing = self.seed(cache={"text": "原响应", "model": None, "usage": None, "finish_reason": "length"})
        status = self.reconcile(missing, "restore_response")
        self.assertEqual(status["uncertain_calls"], 1)
        self.assertEqual(status["reserved_cost_usd"], missing["reserved"])
        large = self.seed(cache={"text": "原响应", "model": None,
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}, "finish_reason": "stop"})
        status = self.reconcile(large, "restore_response")
        self.assertAlmostEqual(status["reserved_cost_usd"], 1 + missing["reserved"])
        self.assertAlmostEqual(status["reported_cost_usd"], 1)

    def test_stale_hashes_and_mutated_original_request_fail_without_audit_or_ledger_mutation(self):
        fixture = self.seed(cache={"text": "原响应", "model": None, "usage": None, "finish_reason": "stop"})
        before = self.gateway.path("account.json").read_bytes()
        for overrides in ({"attempt_hash": "f" * 64}, {"response_hash": "f" * 64}):
            with self.assertRaises(VibeNovelError):
                self.reconcile(fixture, "restore_response", **overrides)
        request_path = fixture["root"] / "requests/read-00001.json"
        request = read_json(request_path)
        request["prompt"] += "篡改"
        atomic_write(request_path, json_bytes(request), overwrite=True)
        with self.assertRaises(VibeNovelError):
            self.reconcile(fixture, "restore_response")
        self.assertEqual(self.gateway.path("account.json").read_bytes(), before)
        self.assertFalse(self.gateway.path("reconciliations").exists())

    def test_unattributed_or_ambiguous_sender_fails_closed(self):
        fixture = self.seed()
        manifest_path = fixture["root"] / "manifest.json"
        manifest = read_json(manifest_path)
        manifest_path.unlink()
        with self.assertRaises(VibeNovelError):
            self.reconcile(fixture)
        atomic_write(manifest_path, json_bytes(manifest))
        duplicate = self.workspace / "library/evaluations" / manifest["id"]
        duplicate.mkdir(parents=True)
        atomic_write(duplicate / "manifest.json", json_bytes(manifest))
        with self.assertRaises(VibeNovelError):
            self.reconcile(fixture)
        self.assertEqual(self.pending(fixture)["state"], "reserved")

    def test_missing_original_request_or_changed_plan_node_keeps_original_reserved_record(self):
        fixture = self.seed()
        request_path = fixture["root"] / "requests/read-00001.json"
        original = request_path.read_bytes()
        request_path.unlink()
        with self.assertRaises(VibeNovelError):
            self.reconcile(fixture)
        atomic_write(request_path, original)
        manifest_path = fixture["root"] / "manifest.json"
        manifest = read_json(manifest_path)
        manifest["plan"]["nodes"][0]["id"] = "another-node"
        manifest["plan_hash"] = digest(json_bytes(manifest["plan"]))
        atomic_write(manifest_path, json_bytes(manifest), overwrite=True)
        with self.assertRaises(VibeNovelError):
            self.reconcile(fixture)
        self.assertEqual(self.pending(fixture)["state"], "reserved")

    def test_malformed_cached_model_or_token_count_does_not_restore_or_discard_response(self):
        for model, usage in (("different-model", None), (None, {"prompt_tokens": -1}),
                             (None, {"completion_tokens": True}),
                             (None, {"prompt_tokens": 10**400, "completion_tokens": 1})):
            fixture = self.seed(cache={"text": "原响应", "model": model, "usage": usage, "finish_reason": "stop"})
            before = self.gateway.path("account.json").read_bytes()
            with self.assertRaises(VibeNovelError):
                self.reconcile(fixture, "restore_response")
            with self.assertRaises(VibeNovelError):
                self.reconcile(fixture, "mark_unknown")
            self.assertEqual(self.gateway.path("account.json").read_bytes(), before)

    def test_http_route_requires_csrf_reason_and_current_hash_and_exposes_no_prompt_or_key(self):
        with TestClient(create_app(self.workspace), base_url="http://127.0.0.1") as client:
            fixture = self.seed()
            headers = {"X-VibeNovel-Token": client.get("/api/bootstrap").json()["csrf_token"]}
            response = client.get("/api/research-account")
            self.assertNotIn("synthetic-test-key", response.text)
            self.assertNotIn(fixture["prompt"], response.text)
            item = response.json()["pending_reconciliations"][0]
            path = f"/api/research-account/attempts/{fixture['id']}/reconcile"
            body = {"action": "mark_unknown", "expected_attempt_hash": item["attempt_hash"],
                    "expected_response_hash": None, "reason": "核对历史中断，不重发"}
            self.assertEqual(client.post(path, json=body).status_code, 403)
            self.assertEqual(client.post(path, headers=headers, json={**body, "reason": ""}).status_code, 422)
            self.assertEqual(client.post(path, headers=headers, json={**body, "clear_cost": True}).status_code, 422)
            self.assertEqual(client.post(path, headers=headers, json={**body, "expected_attempt_hash": "f"*64}).status_code, 409)
            response = client.post(path, headers=headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["reserved_cost_usd"], fixture["reserved"])
            self.assertEqual(client.post(path, headers=headers, json=body).status_code, 409)


if __name__ == "__main__":
    unittest.main()
