"""Synthetic three-condition experiments; no paid requests or real books."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from vibenovel.core.files import digest,json_bytes
from vibenovel.core.reference_evaluations import ReferenceEvaluationManager,validate_judgment,_Lock
from vibenovel.core.locking import project_lock
from vibenovel.studio.providers import GenerationResult
from vibenovel.errors import VibeNovelError


class Gateway:
    def __init__(self): self.calls=[];self.bad=False;self.callback=None
    def preview(self,prompt,maximum):
        return {"hash":digest(prompt.encode()),"model_identity":"offline-v1","model_id":"offline","config_hash":"offline-config","estimated_cost_usd":len(prompt.encode())/10000000+maximum/10000000,"max_output_tokens":maximum}
    def generate(self,preview,prompt,cancel,key):
        self.calls.append((prompt,key))
        if self.callback:self.callback()
        if "<anonymous_samples>" in prompt:
            samples=json.loads(prompt.split("<anonymous_samples>\n")[1].split("\n</anonymous_samples>")[0])
            result={"summary":"单次样本各有取舍","ratings":[{"sample_id":s["sample_id"],"scores":{k:6 for k in ("causality","character","pacing","expression","coherence")},"strengths":["行动具体"],"weaknesses":["单次样本有限"],"evidence":[s["text"][:15]]} for s in samples],"ranking":[s["sample_id"] for s in samples],"limitations":["同模型评阅存在偏差"]}
        else: result={"text":"小岑把最后一块木板放在水边，雨水灌进他的鞋。对岸的人没有催促，只举起一盏灯。他先把绳子缠在树上，再试探着踏上木板。木板沉了一半，他退回来，解下背包，把里面的石头倒在岸上。他知道桥未必承受得住两个人，但他可以先把灯接过来。"}
        return GenerationResult("invalid" if self.bad else json.dumps(result,ensure_ascii=False),{"prompt_tokens":10,"completion_tokens":20})


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.gateway=Gateway();self.manager=ReferenceEvaluationManager(self.root,self.gateway);self.addCleanup(self.manager.close)
        self.valid=True
        self.method={"index":0,"title":"行动代价","steps":["先行动再承担损失"],"limitations":["不适合所有场景"],"source_refs":[{"quote":"原书证据只用于结构组"}]}
        self.manager.studies.preview_methods=lambda sid,indices:{"methods":[copy.deepcopy(self.method)],"origin":{"study_id":sid,"indices":indices,"version":"frozen-v1"}}
        def check(origin):
            if not self.valid:raise VibeNovelError("source expired")
            return True
        self.manager.studies.validate_origin=check
    def request(self):return {"study_id":"a"*32,"method_indices":[0],"brief":"原创练习：一个摆渡人必须在救人与守约之间选择。","budget_max_usd":1.0,"max_output_tokens":2048}
    def create(self):
        request=self.request();preview=self.manager.preview(request)
        return self.manager.create(request,preview["hash"],"evaluation-offline-001")
    def execute(self,record):
        self.manager.control(record["id"],"start");self.manager._threads[record["id"]].join(10)
        self.assertFalse(self.manager._threads[record["id"]].is_alive())
        return self.manager.detail(record["id"])
    def test_three_conditions_blind_judge_human_separate_and_no_replay(self):
        record=self.create();self.assertEqual(self.gateway.calls,[])
        result=self.execute(record);self.assertEqual(result["state"],"succeeded",result["error"])
        self.assertEqual(len(self.gateway.calls),4)
        self.assertNotIn("行动代价",self.gateway.calls[0][0]);self.assertIn("行动代价",self.gateway.calls[1][0]);self.assertNotIn("原书证据只用于结构组",self.gateway.calls[1][0]);self.assertIn("原书证据只用于结构组",self.gateway.calls[2][0])
        self.assertNotIn("structured_method",self.gateway.calls[3][0]);self.assertNotIn("summary_only",self.gateway.calls[3][0])
        self.assertIsNone(result["condition_mapping"])
        human=self.manager.human_review(record["id"],expected_hash=result["hash"],ranking=["sample-C","sample-B","sample-A"],ratings={"sample-A":3,"sample-B":5,"sample-C":8},note="作者偏好更紧张的处理")
        self.assertEqual(human["human_reviews"][0]["provenance"],"AUTHOR_BLIND_REVIEW");self.assertTrue(human["condition_mapping"])
        self.assertTrue(human["ai_judgment"])
        with self.assertRaises(VibeNovelError):self.manager.control(record["id"],"start")
        self.assertEqual(len(self.gateway.calls),4)
    def test_bad_response_offline_repair_without_resend(self):
        self.gateway.bad=True;record=self.create();result=self.execute(record)
        self.assertEqual(result["state"],"failed");self.assertEqual(len(self.gateway.calls),1)
        raw=self.manager.response(record["id"],"generate-1")
        valid=json.dumps({"text":"这是人工核对的原创新练习。"*12},ensure_ascii=False)
        repaired=self.manager.repair(record["id"],"generate-1",expected_hash=raw["hash"],raw=valid,reason="修正响应格式，保留原响应")
        self.assertEqual(repaired["state"],"paused");self.assertEqual(len(self.gateway.calls),1)
        self.gateway.bad=False;final=self.execute(repaired)
        self.assertEqual(final["state"],"succeeded",final["error"]);self.assertEqual(len(self.gateway.calls),4)
    def test_source_withdrawal_during_send_blocks_late_result(self):
        self.gateway.callback=lambda:setattr(self,"valid",False)
        result=self.execute(self.create());self.assertEqual(result["state"],"failed")
        self.assertEqual(len(self.gateway.calls),1);self.assertEqual(result["samples"],[])
        self.assertTrue(self.manager.response(result["id"],"generate-1")["text"])
    def test_budget_frozen_prompt_and_plan_tampering_block(self):
        request=self.request();request["budget_max_usd"]=.000001
        with self.assertRaises(VibeNovelError):self.manager.preview(request)
        record=self.create();path=self.manager.path(record["id"]+"/manifest.json");value=json.loads(path.read_text(encoding="utf-8"));value["plan"]["request"]["budget_max_usd"]=100
        path.write_bytes(json_bytes(value))
        with self.assertRaises(VibeNovelError):self.manager.detail(record["id"])
        self.assertEqual(self.gateway.calls,[])
    def test_judge_must_quote_own_sample_and_cover_all_conditions(self):
        result=self.execute(self.create());judgment={k:v for k,v in result["ai_judgment"].items() if k!="provenance"}
        judgment["ratings"][0]["evidence"]=["未出现的原句"]
        with self.assertRaises(VibeNovelError):validate_judgment(json.dumps(judgment),result["samples"])
    def test_constructor_does_not_interrupt_a_live_runner(self):
        record=self.create();value=self.manager._load(record["id"]);value["state"]="running";value["nodes"][0]["state"]="running";self.manager._save(value)
        entered=threading.Event();release=threading.Event()
        def hold():
            with project_lock(_Lock(self.manager,record["id"]+"/runner"),timeout=1):
                entered.set();release.wait(5)
        worker=threading.Thread(target=hold);worker.start();self.assertTrue(entered.wait(2))
        try:
            new=ReferenceEvaluationManager(self.root,self.gateway);self.addCleanup(new.close)
            self.assertEqual(new.detail(record["id"])["state"],"running")
        finally:release.set();worker.join(3)

    def test_restart_keeps_unknown_request_blocked(self):
        record=self.create();value=self.manager._load(record["id"]);value["state"]="running";value["nodes"][0]["state"]="running";self.manager._save(value)
        new=ReferenceEvaluationManager(self.root,self.gateway);self.addCleanup(new.close)
        self.assertEqual(new.detail(record["id"])["state"],"interrupted")
        with self.assertRaises(VibeNovelError):new.control(record["id"],"start")
        self.assertEqual(self.gateway.calls,[])

if __name__=="__main__":unittest.main()


class EvaluationOriginIntegrationTests(unittest.TestCase):
    def test_actual_study_method_origin_is_checked_without_adoption(self):
        from tests import test_reference_studies as fixtures
        fixture=fixtures.StudyTests(methodName="test_prepared_continuous_checkpoints_four_maps_and_method_origin")
        fixture.setUp()
        try:
            study=fixture.create();final=fixture.run_study(study)
            self.assertEqual(final["state"],"succeeded")
            gateway=Gateway();manager=ReferenceEvaluationManager(fixture.workspace,gateway)
            try:
                request={"study_id":study["id"],"method_indices":[0],"brief":"原创练习：摆渡人在暴雨中选择守约或救人，承担明确代价。","budget_max_usd":1.0,"max_output_tokens":2048}
                preview=manager.preview(request)
                record=manager.create(request,preview["hash"],"actual-origin-evaluation-01")
                current=fixture.books.detail(fixture.book["book_id"])
                fixture.books.update_policy(current["book_id"],expected_hash=current["hash"],outbound="local")
                with self.assertRaises(VibeNovelError):manager.control(record["id"],"start")
                self.assertEqual(gateway.calls,[])
                self.assertFalse((fixture.workspace/"library"/"records"/"entry").exists())
            finally:manager.close()
        finally:fixture.doCleanups()
