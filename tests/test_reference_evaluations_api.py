"""HTTP evaluation boundary checks with synthetic method origins."""
import copy
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from vibenovel.studio.server import create_app
from vibenovel.studio.reference_evaluations_api import install_evaluation_routes
from tests.test_reference_evaluations import Gateway


class EvaluationAPITests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        app=create_app(self.root)
        self.gateway=Gateway()
        if not any(getattr(r,"path","")=="/api/reference-evaluations/preview" for r in app.routes):
            self.manager=install_evaluation_routes(app,self.root,self.gateway)
        else:
            self.manager=app.state.reference_evaluations
            self.manager.gateway=self.gateway
        self.manager.studies.preview_methods=lambda sid,indices:{"methods":[{"index":0,"title":"合成方法","steps":["冲突与代价"],"limitations":["单场景"],"source_refs":[]}],"origin":{"study_id":sid,"indices":indices}}
        self.manager.studies.validate_origin=lambda origin:True
        self.client=TestClient(app,base_url="http://127.0.0.1:8000");self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.headers={"X-VibeNovel-Token":self.client.get("/api/bootstrap").json()["csrf_token"]}
        self.request={"study_id":"a"*32,"method_indices":[0],"brief":"一个陌生的摆渡人在暴雨中选择守约或者救人，需要付出代价。","budget_max_usd":1.0,"max_output_tokens":2048}
    def post(self,path,body):return self.client.post("/api/reference-evaluations"+path,json=body,headers=self.headers)
    def test_prepared_start_anonymous_results_and_author_review(self):
        preview=self.post("/preview",self.request);self.assertEqual(preview.status_code,200,preview.text)
        created=self.post("",{**self.request,"expected_preview":preview.json()["hash"],"idempotency_key":"eval-http-offline-01"});self.assertEqual(created.status_code,201,created.text)
        record=created.json();self.assertEqual(record["state"],"prepared");self.assertEqual(self.gateway.calls,[])
        start=self.post("/"+record["id"]+"/start",{});self.assertEqual(start.status_code,200,start.text)
        self.manager._threads[record["id"]].join(10)
        detail=self.client.get("/api/reference-evaluations/"+record["id"]).json();self.assertEqual(detail["state"],"succeeded",detail)
        self.assertEqual(len(self.gateway.calls),4);self.assertIsNone(detail["condition_mapping"])
        human=self.post("/"+record["id"]+"/human-review",{"expected_hash":detail["hash"],"ranking":["sample-A","sample-C","sample-B"],"ratings":{"sample-A":8,"sample-B":5,"sample-C":7},"note":"合成作者偏好"})
        self.assertEqual(human.status_code,200,human.text);self.assertTrue(human.json()["condition_mapping"])
    def test_csrf_strict_input_stale_preview_and_path_rejection(self):
        self.assertEqual(self.client.post("/api/reference-evaluations/preview",json=self.request).status_code,403)
        self.assertEqual(self.post("/preview",{**self.request,"api_key":"not-accepted"}).status_code,422)
        self.assertEqual(self.post("/preview",{**self.request,"method_indices":[True]}).status_code,422)
        self.assertEqual(self.post("",{**self.request,"expected_preview":"0"*64,"idempotency_key":"eval-http-stale-001"}).status_code,409)
        self.assertEqual(self.client.get("/api/reference-evaluations/not-an-id").status_code,400)
        self.assertEqual(self.gateway.calls,[])


if __name__=="__main__":unittest.main()
