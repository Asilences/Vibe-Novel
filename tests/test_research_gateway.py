import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.research_gateway import ResearchGateway
from vibenovel.errors import VibeNovelError
from vibenovel.studio.model_registry import ModelRegistry
from vibenovel.studio.providers import GenerationResult, ProviderError


class ResearchGatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        env = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                     if not key.upper().startswith("VIBENOVEL_")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        registry = ModelRegistry(self.workspace)
        profile = registry.public()["profiles"][0]
        profile["model"] = "offline-test"
        registry.save({"profiles": [profile]}, None)
        self.calls = []

    def gateway(self, failure=False):
        owner = self
        class Provider:
            def generate(self, prompt, cancel):
                owner.calls.append(prompt)
                if failure:
                    raise ProviderError("upstream private detail must not persist")
                return GenerationResult('{"result":"候选"}', {"prompt_tokens": 10, "completion_tokens": 5})
        gateway = ResearchGateway(self.workspace, provider_factory=lambda provider: Provider())
        return gateway

    def test_explicit_account_shared_budget_and_idempotent_response_reuse(self):
        gateway = self.gateway()
        with self.assertRaises(VibeNovelError):
            gateway.preview("独立研究")
        gateway.configure(model_id="default_api", max_cost_usd=.004, price_input_usd=1., price_output_usd=1.)
        preview = gateway.preview("独立研究", 256)
        response = gateway.generate(preview, "独立研究", threading.Event(), "same-request-key-1234")
        self.assertTrue(response.text)
        self.assertEqual(gateway.generate(preview, "独立研究", threading.Event(), "same-request-key-1234").text, response.text)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(VibeNovelError):
            self.gateway().generate(preview, "独立研究", threading.Event(), "another-request-1234")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.workspace / "novels").exists())

    def test_unknown_cost_survives_restart_and_cannot_be_replayed_or_reset(self):
        gateway = self.gateway(failure=True)
        gateway.configure(model_id="default_api", max_cost_usd=.1, price_input_usd=1., price_output_usd=1.)
        preview = gateway.preview("失败测试", 256)
        for instance in (gateway, self.gateway()):
            with self.assertRaises(VibeNovelError):
                instance.generate(preview, "失败测试", threading.Event(), "unknown-request-1234")
        status = gateway.status()
        self.assertEqual(status["calls"], 1)
        self.assertEqual(status["uncertain_calls"], 1)
        self.assertGreater(status["reserved_cost_usd"], 0)
        with self.assertRaises(VibeNovelError):
            gateway.configure(model_id="default_api", max_cost_usd=.00001, price_input_usd=1., price_output_usd=1.)
        self.assertNotIn("upstream private", gateway.path("account.json").read_text(encoding="utf-8"))

    def test_prompt_price_model_and_response_tampering_fail_closed(self):
        gateway = self.gateway()
        gateway.configure(model_id="default_api", max_cost_usd=.1, price_input_usd=1., price_output_usd=1.)
        preview = gateway.preview("固定提示", 256)
        with self.assertRaises(VibeNovelError):
            gateway.generate(preview, "另一提示", threading.Event(), "changed-prompt-1234")
        gateway.generate(preview, "固定提示", threading.Event(), "fixed-response-1234")
        response = next(gateway.path("responses").glob("*.json"))
        response.write_text('{"text":"tampered"}', encoding="utf-8")
        with self.assertRaises(VibeNovelError):
            gateway.generate(preview, "固定提示", threading.Event(), "fixed-response-1234")
        gateway.configure(model_id="default_api", max_cost_usd=.1, price_input_usd=2., price_output_usd=1.)
        with self.assertRaises(VibeNovelError):
            gateway.generate(preview, "固定提示", threading.Event(), "new-after-change-1234")
        self.assertEqual(len(self.calls), 1)

    def test_deepseek_research_protocol_is_frozen_and_disables_default_thinking(self):
        registry = ModelRegistry(self.workspace)
        profile = registry.public()["profiles"][0]
        profile.update(model="deepseek-flash", base_url="https://api.deepseek.com", api_key="synthetic-unit-test-key")
        registry.save({"profiles": [profile]}, registry.public()["hash"])
        gateway = ResearchGateway(self.workspace)
        gateway.configure(model_id="default_api", max_cost_usd=.1, price_input_usd=1., price_output_usd=1.)
        preview = gateway.preview("研究JSON输出", 256)
        self.assertEqual(preview["request_options"], {"thinking": "disabled"})
        with patch("vibenovel.studio.providers.OpenAICompatibleProvider") as factory:
            factory.return_value.generate.return_value = GenerationResult('{"ok":true}', {"prompt_tokens": 5, "completion_tokens": 5})
            gateway.generate(preview, "研究JSON输出", threading.Event(), "thinking-protocol-key-1234")
            self.assertEqual(factory.call_args.kwargs["thinking"], "disabled")
        self.assertEqual(gateway.status()["calls"], 1)


if __name__ == "__main__":
    unittest.main()
