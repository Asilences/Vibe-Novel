"""Offline checks for frozen structured-research protocol options."""
import copy
import json
import unittest
from unittest.mock import patch

from tests import test_reference_jobs as fixtures
from vibenovel.core.locking import ConflictError
from vibenovel.core.research_gateway import structured_research_request_options
from vibenovel.studio.model_registry import ModelRegistry
from vibenovel.studio.providers import GenerationResult


class ReferenceBatchProtocolTests(unittest.TestCase):
    setUp = fixtures.ReferenceBatchTests.setUp
    response = staticmethod(fixtures.ReferenceBatchTests.response)
    manager = fixtures.ReferenceBatchTests.manager
    request = fixtures.ReferenceBatchTests.request
    create = fixtures.ReferenceBatchTests.create
    wait = fixtures.ReferenceBatchTests.wait

    def configure_official_flash(self):
        registry = ModelRegistry(self.workspace)
        public = registry.public()
        profile = public["profiles"][0]
        profile.update(model="deepseek-flash", base_url="https://api.deepseek.com",
                       api_key="synthetic-unit-test-key")
        registry.save({"profiles": [profile]}, public["hash"])

    def run_with_mocked_protocol_provider(self, manager, batch):
        manager.jobs._factory = None

        def generate(prompt, cancel):
            self.calls.append(prompt)
            return GenerationResult(json.dumps(self.response(prompt), ensure_ascii=False),
                                    {"prompt_tokens": 10, "completion_tokens": 10}, "deepseek-flash", "stop")

        with patch("vibenovel.studio.reference_jobs.OpenAICompatibleProvider") as provider:
            provider.return_value.generate.side_effect = generate
            manager.control(self.book["book_id"], batch["id"], "start")
            final = self.wait(manager, batch["id"])
            self.assertEqual(final["state"], "succeeded", final.get("error"))
            self.assertEqual(len(self.calls), 3)
            return provider.call_args_list

    def test_official_flash_preview_freezes_and_executes_disabled_thinking(self):
        self.configure_official_flash()
        manager = self.manager()
        batch = self.create(manager)
        for node in batch["nodes"]:
            self.assertEqual(node["route"]["request_options"], {"thinking": "disabled"})
        self.assertFalse(self.calls)
        constructors = self.run_with_mocked_protocol_provider(manager, batch)
        self.assertEqual(len(constructors), 3)
        for call in constructors:
            self.assertEqual(call.kwargs["thinking"], "disabled")
            self.assertEqual(call.kwargs["settings"].max_output_tokens, 2048)

    def test_legacy_plan_without_options_retains_original_provider_default(self):
        self.configure_official_flash()
        manager = self.manager()
        original_route = manager._route

        def legacy_route(*args):
            route, tokens, cost = original_route(*args)
            route.pop("request_options")
            return route, tokens, cost

        # Build exactly the old frozen shape before hashing and saving, rather
        # than mutating a plan that a user has already approved.
        with patch.object(manager, "_route", side_effect=legacy_route):
            batch = self.create(manager)
        self.assertTrue(all("request_options" not in node["route"] for node in batch["nodes"]))
        constructors = self.run_with_mocked_protocol_provider(manager, batch)
        self.assertTrue(all("thinking" not in call.kwargs for call in constructors))
        saved = manager._load(self.book["book_id"], batch["id"])
        self.assertTrue(all("request_options" not in node["route"] for node in saved["nodes"]))

    def test_protocol_change_is_rejected_before_sending_new_frozen_batch(self):
        self.configure_official_flash()
        manager = self.manager()
        batch = self.create(manager)
        with patch("vibenovel.studio.reference_jobs.structured_research_request_options", return_value={}):
            with self.assertRaises(ConflictError):
                manager.control(self.book["book_id"], batch["id"], "start")
        self.assertFalse(self.calls)
        route = copy.deepcopy(batch["nodes"][0]["route"])
        route["request_options"] = {"thinking": "enabled"}
        with self.assertRaises(ConflictError):
            manager._request_options(route)

    def test_compatible_endpoints_and_other_models_keep_their_defaults(self):
        for endpoint, model in (
            ("https://example.com/v1", "deepseek-flash"),
            ("https://api.deepseek.com.example.com", "deepseek-flash"),
            ("https://api.deepseek.com", "deepseek-chat"),
            ("https://api.deepseek.com", "deepseek-reasoner"),
        ):
            with self.subTest(endpoint=endpoint, model=model):
                self.assertEqual(structured_research_request_options(endpoint, model), {})
        self.assertEqual(structured_research_request_options("https://api.deepseek.com/v1", "deepseek-flash"),
                         {"thinking": "disabled"})


if __name__ == "__main__":
    unittest.main()
