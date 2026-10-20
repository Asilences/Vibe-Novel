"""Model configuration, frozen routes and budgets use temporary offline fixtures."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.files import json_bytes
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.model_registry import ModelRegistry, REGISTRY_PATH, ROLES, resolve_snapshot
from vibenovel.studio.routing import BudgetLedger, ProjectRouting
from vibenovel.studio.settings import SettingsError, save_settings


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.registry = ModelRegistry(self.workspace)
        self.routing = ProjectRouting(self.project, self.workspace)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def profile(self, identity="primary", **fields):
        result = {"id": identity, "label": "测试模型", "base_url": "https://example.invalid/v1", "model": "offline-model",
                  "roles": list(ROLES), "api_key": "fixture-private-secret", "allow_loopback": False,
                  "context_tokens": 1048576, "max_output_tokens": 8192,
                  "price_input_usd": 1.0, "price_output_usd": 2.0}
        result.update(fields)
        return result

    def configured(self, *profiles):
        result = self.registry.save({"profiles": list(profiles) or [self.profile()]}, self.registry.public()["hash"])
        state = self.routing.public()
        policy = state["policy"]
        for binding in policy["bindings"].values():
            binding["primary"] = result["profiles"][0]["id"]
        self.routing.save(policy, state["hash"])
        return result

    def test_missing_registry_neither_creates_nor_migrates_credentials(self):
        public = self.registry.public()
        self.assertEqual(public["revision"], 0)
        self.assertTrue(public["migration_required"])
        self.assertFalse(public["profiles"][0]["available"])
        self.assertFalse((self.workspace / REGISTRY_PATH).exists())
        with self.assertRaises(VibeNovelError):
            self.routing.preview("draft", "正文")
        route = self.routing.preview("draft", "正文", {"allow_unconfigured": True})
        self.assertEqual(route["role"], "writer")
        self.assertEqual(route["estimated_input_tokens"], len("正文".encode()) + 1024)
        self.assertEqual(route["candidates"][0]["context_tokens"], 1048576)

    def test_explicit_legacy_migration_preserves_original_bytes(self):
        path = self.workspace / "config/api.local.json"
        path.parent.mkdir()
        original = json_bytes({"default_provider": "api", "api": {"base_url": "https://old.invalid", "model": "old", "api_key": "legacy-fixture-secret"}})
        path.write_bytes(original)
        self.assertTrue(self.registry.public()["migration_required"])
        public = self.registry.migrate(None)
        self.assertFalse(public["migration_required"])
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("legacy-fixture-secret", json.dumps(public))
        self.assertEqual(resolve_snapshot(self.workspace, self.registry.snapshot("default_api")).api_key, "legacy-fixture-secret")
        with self.assertRaises(ConflictError):
            self.registry.migrate(None)

    def test_single_api_prices_feed_routes_snapshots_and_explicit_migration(self):
        saved = save_settings(self.workspace, {"base_url": "https://offline.invalid/v1", "model": "fixture-model",
            "api_key": "legacy-priced-fixture-secret", "allow_loopback": False,
            "price_input_usd": 1.25, "price_output_usd": 3.5}, None, {})
        original = (self.workspace / "config/api.local.json").read_bytes()
        route = self.routing.preview("draft", "待写场景")
        candidate = route["candidates"][0]
        self.assertEqual((candidate["price_input_usd"], candidate["price_output_usd"]), (1.25, 3.5))
        self.assertFalse((self.workspace / REGISTRY_PATH).exists())
        frozen = self.registry.snapshot("default_api")
        self.assertEqual(resolve_snapshot(self.workspace, frozen).price_output_usd, 3.5)
        self.assertNotIn("legacy-priced-fixture-secret", json.dumps(route))
        self.assertEqual((self.workspace / "config/api.local.json").read_bytes(), original)
        # Price drift invalidates frozen configuration before any invocation.
        changed = save_settings(self.workspace, {"base_url": saved["api"]["base_url"], "model": saved["api"]["model"],
            "allow_loopback": False, "price_input_usd": 2}, saved["hash"], {})
        with self.assertRaises(SettingsError):
            resolve_snapshot(self.workspace, frozen)
        before_migration = (self.workspace / "config/api.local.json").read_bytes()
        migrated = self.registry.migrate(None)
        self.assertEqual(migrated["profiles"][0]["price_input_usd"], 2)
        self.assertEqual(migrated["profiles"][0]["price_output_usd"], 3.5)
        self.assertEqual((self.workspace / "config/api.local.json").read_bytes(), before_migration)
        self.assertNotEqual(changed["hash"], saved["hash"])

    def test_environment_changed_model_cannot_reuse_single_api_quotes(self):
        save_settings(self.workspace, {"base_url": "https://offline.invalid/v1", "model": "fixture-model",
            "api_key": "legacy-priced-fixture-secret", "allow_loopback": False,
            "price_input_usd": 1.25, "price_output_usd": 3.5}, None, {})
        with patch.dict(os.environ, {"VIBENOVEL_API_MODEL": "other-model"}):
            route = self.routing.preview("draft", "正文")
        self.assertEqual(route["candidates"][0]["model"], "other-model")
        self.assertIsNone(route["candidates"][0]["price_input_usd"])
        self.assertIsNone(route["candidates"][0]["price_output_usd"])

    def test_public_no_key_and_preserves_independent_credentials(self):
        public = self.registry.save({"profiles": [self.profile("first"), self.profile("second", api_key="other-fixture-secret")]}, None)
        self.assertNotIn("fixture-private-secret", json.dumps(public))
        for profile in public["profiles"]:
            profile["model"] += "-changed"
        self.registry.save({"profiles": public["profiles"]}, public["hash"])
        self.assertEqual(resolve_snapshot(self.workspace, self.registry.snapshot("second")).api_key, "other-fixture-secret")

    def test_endpoint_change_cannot_reuse_old_key(self):
        public = self.configured()
        profile = public["profiles"][0]
        profile["base_url"] = "https://other.invalid"
        with self.assertRaisesRegex(SettingsError, "对应密钥"):
            self.registry.save({"profiles": [profile]}, public["hash"])
        profile["clear_api_key"] = True
        result = self.registry.save({"profiles": [profile]}, public["hash"])
        self.assertFalse(result["profiles"][0]["has_api_key"])

    def test_optimistic_hash_and_monotone_revision_block_configuration_aba(self):
        public = self.configured()
        snapshot = self.registry.snapshot("primary")
        changed = copy.deepcopy(public["profiles"])
        changed[0]["model"] = "changed"
        next_public = self.registry.save({"profiles": changed}, public["hash"])
        final = self.registry.save({"profiles": public["profiles"]}, next_public["hash"])
        self.assertEqual(final["revision"], 3)
        with self.assertRaises(SettingsError):
            resolve_snapshot(self.workspace, snapshot)
        with self.assertRaises(ConflictError):
            self.registry.save({"profiles": public["profiles"]}, public["hash"])

    def test_invalid_profiles_fail_without_echoing_payload(self):
        cases = [{"model": "bad\nsecret"}, {"id": []}, {"price_input_usd": float("nan")},
                 {"roles": ["writer", "writer"]}, {"base_url": "https://user:secret@example.invalid"},
                 {"context_tokens": True}, {"max_output_tokens": 1048576}, {"api_key": ["secret"]}]
        for fields in cases:
            with self.subTest(fields=tuple(fields)), self.assertRaises(SettingsError) as caught:
                self.registry.save({"profiles": [self.profile(**fields)]}, None)
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(SettingsError):
            self.registry.save({"profiles": [self.profile(), self.profile()]}, None)

    def test_malformed_local_registry_error_is_redacted(self):
        path = self.workspace / REGISTRY_PATH
        path.parent.mkdir()
        path.write_text('{"fixture-secret":NaN}', encoding="utf-8")
        with self.assertRaises(SettingsError) as caught:
            self.registry.public()
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_roles_binding_and_fallback_destination_are_explicit(self):
        self.configured(self.profile("writer", roles=["writer", "planner", "reviewer", "memory"]), self.profile("backup"))
        state = self.routing.public()
        state["policy"]["bindings"]["writer"]["fallbacks"] = ["backup"]
        state["policy"].update(max_calls=2, max_attempts=2)
        self.routing.save(state["policy"], state["hash"])
        route = self.routing.preview("rewrite", "待修改")
        self.assertEqual([item["id"] for item in route["candidates"]], ["writer", "backup"])
        self.assertEqual(route["max_attempts"], 2)
        self.assertNotIn("api_key", json.dumps(route))
        self.assertEqual(self.routing.preview("memory_delta", "正文")["role"], "memory")
        self.assertEqual(self.routing.preview("review_evidence", "正文")["role"], "reviewer")
        self.assertEqual(self.routing.preview("build_blueprint", "大纲")["role"], "planner")

    def test_disallowed_role_or_missing_model_cannot_be_saved(self):
        self.registry.save({"profiles": [self.profile(roles=["writer"])]}, None)
        state = self.routing.public()
        for binding in state["policy"]["bindings"].values():
            binding["primary"] = "primary"
        with self.assertRaises(VibeNovelError):
            self.routing.save(state["policy"], None)

    def test_context_estimate_counts_utf8_and_output_not_characters(self):
        self.configured(self.profile(context_tokens=12000))
        with self.assertRaisesRegex(VibeNovelError, "上下文"):
            self.routing.preview("draft", "汉" * 1000)
        route = self.routing.preview("draft", "abc")
        self.assertEqual(route["estimated_input_tokens"], 1027)

    def test_budget_unknown_prices_reject_dollar_limit(self):
        self.configured(self.profile(price_input_usd=None))
        route = self.routing.preview("draft", "正文")
        route["budget"]["max_cost_usd"] = 10
        with self.assertRaisesRegex(VibeNovelError, "价格"):
            BudgetLedger(self.project).reserve("flow", "attempt", route, route["candidates"][0])

    def test_budget_reservation_survives_restart_and_unknown_charges(self):
        self.configured()
        route = self.routing.preview("draft", "正文")
        ledger = BudgetLedger(self.project)
        first = ledger.reserve("flow", "one", route, route["candidates"][0])
        self.assertEqual(first["calls"], 1)
        ledger.settle("flow", "one", status="unknown")
        again = BudgetLedger(self.project)
        self.assertEqual(again.public("flow")["tokens"], first["tokens"])
        with self.assertRaises(VibeNovelError):
            again.reserve("flow", "two", route, route["candidates"][0])
        with self.assertRaises(VibeNovelError):
            again.reserve("flow", "one", route, route["candidates"][0])

    def test_budget_known_usage_and_not_sent_are_accounted_separately(self):
        self.configured()
        route = self.routing.preview("draft", "正文")
        ledger = BudgetLedger(self.project)
        ledger.reserve("flow", "one", route, route["candidates"][0])
        summary = ledger.settle("flow", "one", {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, "known")
        self.assertEqual(summary["tokens"], 30)
        self.assertAlmostEqual(summary["cost_usd"], 0.00005)
        self.assertFalse(summary["uncertain"])
        ledger.reserve("other", "one", route, route["candidates"][0])
        summary = ledger.settle("other", "one", status="not_sent")
        self.assertEqual((summary["calls"], summary["tokens"], summary["cost_usd"]), (0, 0, 0))
        self.assertEqual(ledger.reserve("other", "two", route, route["candidates"][0])["calls"], 1)

    def test_budget_token_dollar_and_frozen_group_limits(self):
        self.configured()
        route = self.routing.preview("draft", "正文")
        ledger = BudgetLedger(self.project)
        for field, amount in (("max_tokens", 1), ("max_cost_usd", 0)):
            changed = copy.deepcopy(route)
            changed["budget"][field] = amount
            with self.assertRaises(VibeNovelError):
                ledger.reserve("flow", "one", changed, changed["candidates"][0])
        ledger.reserve("flow", "one", route, route["candidates"][0])
        changed = copy.deepcopy(route)
        changed["budget"]["max_calls"] = 2
        with self.assertRaisesRegex(VibeNovelError, "冻结预算"):
            ledger.reserve("flow", "two", changed, changed["candidates"][0])

    def test_parallel_reservations_cannot_overspend_shared_budget(self):
        self.configured()
        route = self.routing.preview("draft", "正文")
        def reserve(index):
            try:
                BudgetLedger(self.project).reserve("flow", "attempt" + str(index), route, route["candidates"][0])
                return True
            except VibeNovelError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve, range(4))), 1)
        self.assertEqual(BudgetLedger(self.project).public("flow")["calls"], 1)

    def test_corrupt_budget_never_silently_resets(self):
        self.project.path(".runtime/model-budget.json").write_text('{"schema_version":1,"groups":{"bad":{"budget":{},"attempts":{}}}}', encoding="utf-8")
        with self.assertRaises(VibeNovelError):
            BudgetLedger(self.project).public("flow")

    def test_not_sent_attempt_history_limit_keeps_ledger_readable(self):
        self.configured()
        route = self.routing.preview("draft", "正文")
        ledger = BudgetLedger(self.project)
        for index in range(32):
            identity = "attempt" + str(index)
            ledger.reserve("flow", identity, route, route["candidates"][0])
            ledger.settle("flow", identity, status="not_sent")
        path = self.project.path(".runtime/model-budget.json")
        before = path.read_bytes()
        with self.assertRaisesRegex(VibeNovelError, "32次尝试记录"):
            ledger.reserve("flow", "attempt32", route, route["candidates"][0])
        self.assertEqual(path.read_bytes(), before)
        summary = BudgetLedger(self.project).public("flow")
        self.assertEqual((summary["calls"], summary["tokens"], summary["cost_usd"]), (0, 0, 0))
        self.assertFalse(summary["uncertain"])


if __name__ == "__main__":
    unittest.main()
