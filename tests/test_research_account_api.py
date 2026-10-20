import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from vibenovel.studio.server import create_app
from vibenovel.studio.model_registry import ModelRegistry

class AccountAPITests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup);self.workspace=Path(folder.name)
        registry=ModelRegistry(self.workspace);profile=registry.public()["profiles"][0]
        profile.update(model="offline-research-model",base_url="https://api.example.test/v1",api_key="synthetic-test-only-secret")
        registry.save({"profiles":[profile]},None)
        self.client=TestClient(create_app(self.workspace),base_url="http://127.0.0.1");self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.headers={"X-VibeNovel-Token":self.client.get("/api/bootstrap").json()["csrf_token"]}

    def payload(self):
        return {"model_id":"default_api","max_cost_usd":5,"max_calls":20,"price_input_usd":.3,"price_output_usd":1.2,
                "price_source":"synthetic-price-confirmed","expected_hash":self.client.get("/api/research-account").json()["config_hash"]}

    def test_account_is_separate_and_exposes_no_credentials(self):
        response=self.client.put("/api/research-account",headers=self.headers,json=self.payload())
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn("synthetic-test-only-secret",response.text)
        self.assertEqual(response.json()["calls"],0)
        self.assertFalse((self.workspace/"novels").exists())

    def test_no_csrf_unknown_fields_or_old_configuration_can_expand_budget(self):
        body=self.payload()
        self.assertEqual(self.client.put("/api/research-account",json=body).status_code,403)
        self.assertEqual(self.client.put("/api/research-account",headers=self.headers,json={**body,"api_key":"should-never-be-a-research-field"}).status_code,422)
        self.assertEqual(self.client.put("/api/research-account",headers=self.headers,json=body).status_code,200)
        self.assertEqual(self.client.put("/api/research-account",headers=self.headers,json={**body,"max_cost_usd":10}).status_code,409)
