"""Synthetic/offline narrative research: no novels, credentials or API charges."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibenovel.core.files import digest, json_bytes, read_json
from vibenovel.core.locking import ConflictError
from vibenovel.core.reference_books import ReferenceBookStore
from vibenovel.core.reference_studies import ReferenceStudiesStore, resolve_response_refs, validate_comparison
from vibenovel.errors import VibeNovelError
from vibenovel.studio.providers import GenerationResult
from vibenovel.studio.reference_studies import ReferenceStudiesManager


class Gateway:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.bad = False

    def preview(self, prompt, maximum):
        return {"hash": digest(prompt.encode()), "model_identity": "synthetic-model-v1", "model_id": "fake",
                "estimated_cost_usd": len(prompt)/1000000 + maximum/1000000,
                "max_output_tokens": maximum}

    def generate(self, preview, prompt, cancel, key):
        self.calls.append((prompt, key))
        if self.callback:
            self.callback()
        fragments = json.loads(prompt.split("<untrusted_fragments>\n", 1)[1].split("\n</untrusted_fragments>", 1)[0])
        aliases = [{"source_id": fragment["source_id"], "quote": fragment["text"][:25]} for fragment in fragments]
        if "研究类型：comparison" in prompt:
            result = {"summary": "两书采用不同路径", "per_book": [{"book_id": fragment["book_id"], "solution": "以行动说明", "premises": ["仅本章"],
                "limitations": ["无全书结论"], "source_refs": [ref]} for fragment, ref in zip(fragments, aliases)],
                "comparisons": [{"title": "开场", "similarities": "都有目标", "differences": "人物不同", "counterexamples": "不能代表全文", "source_refs": aliases}],
                "methods": [{"title": "目标与阻碍", "steps": ["以具体行动给出人物目标"], "limitations": ["并非所有开场适用"], "source_refs": aliases}], "gaps": ["后文未读"]}
        else:
            number = len(self.calls)
            key = "e" + str(number)
            result = {"summary": "本段观察", "carry_forward": "仅记录当前时点；未知后文。", "events": [{"key": key, "title": "选择", "action": "采取行动", "outcome": "付出代价",
                "caused_by": ["e1"] if number == 2 else [], "claim_type": "inferred", "source_refs": [aliases[0]]}],
                "characters": [{"key": "c1", "name": "小明", "goal": "过河", "state": "河边", "source_refs": [aliases[0]]}],
                "relationships": [], "knowledge": [{"subject": "reader", "fact": "读到第%d段" % number, "status": "known", "source_refs": [aliases[0]]}],
                "threads": [{"key": "t1", "title": "去向", "state": "introduced" if number == 1 else "advanced", "explanation": "尚待后文", "setup_refs": [aliases[0]], "payoff_refs": []}],
                "rhythm": [{"label": "选择", "tension": 3, "emotion": "犹豫", "valence": -1, "source_refs": [aliases[0]]}],
                "methods": [{"title": "行动与代价", "steps": ["给出阻力"], "limitations": ["不是所有章节适用"], "source_refs": [aliases[0]]}], "gaps": ["后文未知"]}
        if self.bad:
            result["events"][0]["source_refs"] = [{**aliases[0], "quote": "从未发送的句子"}]
        return GenerationResult(json.dumps(result, ensure_ascii=False), usage={"total_tokens": 100})


class StudyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.books = ReferenceBookStore(self.workspace)
        self.book = self.books.import_book("人工.txt", "第一章 河岸\n小明在河岸寻找木桥。河水渐涨，桥已断裂。\n第二章 绕路\n小明沿河向北，在夕阳下发现渡口。\n".encode(), title="人工书A")
        self.books.update_policy(self.book["book_id"], expected_hash=self.book["hash"], outbound="excerpts")
        self.gateway = Gateway()
        self.manager = ReferenceStudiesManager(self.workspace, self.gateway)
        self.addCleanup(self.manager.close)

    def request(self):
        return {"kind": "reading", "sources": [{"book_id": self.book["book_id"], "edition_id": self.book["edition_id"], "chapter_ids": []}],
                "question": "分析因果、人物和读者当时知识及节奏", "budget_max_usd": 1.0, "max_output_tokens": 2048, "segment_characters": 12000}

    def create(self, request=None, key="synthetic-study-key-001"):
        request = request or self.request()
        preview = self.manager.preview(request)
        return self.manager.create(request, preview["hash"], key)

    def run_study(self, study):
        self.manager.control(study["id"], "start")
        self.manager._threads[study["id"]].join(timeout=15)
        self.assertFalse(self.manager._threads[study["id"]].is_alive())
        return self.manager.detail(study["id"])

    def test_prepared_continuous_checkpoints_four_maps_and_method_origin(self):
        study = self.create()
        self.assertEqual(study["state"], "prepared")
        self.assertEqual(self.gateway.calls, [])
        same = self.manager.create(self.request(), study["plan_hash"], "synthetic-study-key-001")
        self.assertEqual(same["id"], study["id"])
        final = self.run_study(study)
        self.assertEqual(final["state"], "succeeded", final.get("error"))
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertNotIn("渡口", self.gateway.calls[0][0])
        self.assertIn("carry_forward", self.gateway.calls[1][0])
        export = self.manager.store.export_verified(study["id"])
        self.assertEqual(len(export["results"]), 2)
        for group in ("causal", "characters", "information", "rhythm"):
            self.assertTrue(export["graphs"][group]["nodes"])
        first = copy.deepcopy(export["results"][0])
        self.assertEqual(first["result"]["knowledge"][0]["fact"], "读到第1段")
        self.assertEqual(self.manager.store.export_verified(study["id"])["results"][0], first)
        preview = self.manager.store.preview_methods(study["id"], [0, 1])
        self.assertTrue(self.manager.store.validate_origin(preview["origin"]))
        original = self.book["chapters"][0]["chapter_id"]
        ref = preview["methods"][0]["source_refs"][0]
        self.assertEqual(ref["chapter_id"], original)
        self.assertEqual(self.books.chapter(self.book["book_id"], original)["text"][ref["start_char"]:ref["end_char"]], ref["quote"])

    def test_template_change_blocks_send_without_replaying_or_altering_plan(self):
        study = self.create()
        frozen = self.manager.store.load(study["id"])["plan_hash"]
        with patch("vibenovel.studio.reference_studies.template_hash", return_value="changed-template"):
            with self.assertRaises(ConflictError):
                self.manager.control(study["id"], "start")
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.manager.store.load(study["id"])["plan_hash"], frozen)

    def test_late_withdrawal_blocks_publication_and_never_replays(self):
        def withdraw():
            current = self.books.detail(self.book["book_id"])
            self.books.update_policy(current["book_id"], expected_hash=current["hash"], outbound="local", withdrawn=True)
        self.gateway.callback = withdraw
        study = self.create(); final = self.run_study(study)
        self.assertEqual(final["state"], "failed")
        self.assertEqual(len(self.gateway.calls), 1)
        with self.assertRaises(ConflictError):
            self.manager.control(study["id"], "start")
        self.assertTrue(self.manager.response(study["id"], "read-00001")["text"])

    def test_bad_evidence_can_be_repaired_without_new_charge(self):
        self.gateway.bad = True
        study = self.create(); final = self.run_study(study)
        self.assertEqual(final["state"], "failed")
        raw = self.manager.response(study["id"], "read-00001")
        value = json.loads(raw["text"])
        source = self.manager.store.node_input(self.manager.store.load(study["id"]), 0)[0]["refs"][0]
        value["events"][0]["source_refs"][0]["quote"] = source["quote"][:25]
        repaired = self.manager.repair_response(study["id"], "read-00001", raw["hash"], json.dumps(value, ensure_ascii=False), "修正引文")
        self.assertEqual(repaired["state"], "paused")
        self.assertEqual(len(self.gateway.calls), 1)
        self.gateway.bad = False
        final = self.run_study(repaired)
        self.assertEqual(final["state"], "succeeded", final.get("error"))
        self.assertEqual(len(self.gateway.calls), 2)

    def test_cross_book_all_books_required_and_evidence_is_not_invented(self):
        second = self.books.import_book("另一书.txt", "第一章 雪地\n小红在雪中寻找走失的羊，远处传来铃声。".encode(), title="人工书B")
        self.books.update_policy(second["book_id"], expected_hash=second["hash"], outbound="excerpts")
        request = self.request(); request["kind"] = "comparison"
        request["sources"][0]["chapter_ids"] = [self.book["chapters"][0]["chapter_id"]]
        request["sources"].append({"book_id": second["book_id"], "edition_id": second["edition_id"], "chapter_ids": []})
        study = self.create(request); final = self.run_study(study)
        self.assertEqual(final["state"], "succeeded", final.get("error"))
        result = self.manager.store.export_verified(study["id"])["results"][0]["result"]
        self.assertEqual(len(result["per_book"]), 2)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_versioned_map_edit_and_unsafe_evidence_rejected(self):
        study = self.create(); self.run_study(study)
        export = self.manager.store.export_verified(study["id"])
        graphs = copy.deepcopy(export["graphs"])
        graphs["causal"]["nodes"][0]["label"] = "人工核对后的候选"
        newer = self.manager.store.edit_graphs(study["id"], export["hash"], graphs, "修正标题")
        self.assertEqual(newer["graphs"]["causal"]["nodes"][0]["label"], "人工核对后的候选")
        self.assertTrue(newer["map_version"])
        with self.assertRaises(ConflictError):
            self.manager.store.edit_graphs(study["id"], export["hash"], graphs, "过期版本")
        graphs["causal"]["nodes"][0]["source_refs"][0]["quote"] = "伪造"
        with self.assertRaises(ConflictError):
            self.manager.store.edit_graphs(study["id"], newer["hash"], graphs, "伪造依据")

    def test_pending_source_tamper_stops_before_paid_send(self):
        study = self.create()
        root = self.books.path(self.book["book_id"] + "/editions/" + self.book["edition_id"])
        shard = root / "text/part-00001.txt"
        shard.write_text("篡改", encoding="utf-8")
        final = self.run_study(study)
        self.assertEqual(final["state"], "failed")
        self.assertEqual(self.gateway.calls, [])

    def test_restart_and_unknown_response_are_never_auto_replayed(self):
        study = self.create()
        value = self.manager.store.load(study["id"])
        value["state"] = "running"; value["nodes"][0]["state"] = "running"
        self.manager.store.save(value)
        restarted = ReferenceStudiesManager(self.workspace, self.gateway)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.detail(study["id"])["state"], "interrupted")
        with self.assertRaises(ConflictError):
            restarted.control(study["id"], "start")
        self.assertEqual(self.gateway.calls, [])

    def test_quote_alias_refuses_ambiguous_and_future_evidence(self):
        allowed = [{"book_id": "a"*32, "edition_id": "b"*32, "chapter_id": "c"*32,
                    "start_char": 0, "end_char": 4, "quote": "同句同句"}]
        from vibenovel.core.reference_studies import source_id
        with self.assertRaises(VibeNovelError):
            resolve_response_refs({"source_id": source_id(allowed[0]), "quote": "同句"}, allowed)
        with self.assertRaises(VibeNovelError):
            resolve_response_refs({"source_id": "r未来", "quote": "同句"}, allowed)

    def test_second_manager_does_not_interrupt_a_live_runner(self):
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set(); release.wait(timeout=10)
        self.gateway.callback = block
        study = self.create()
        self.manager.control(study["id"], "start")
        self.assertTrue(entered.wait(timeout=5))
        other = ReferenceStudiesManager(self.workspace, self.gateway)
        self.addCleanup(other.close)
        self.assertEqual(other.detail(study["id"])["state"], "running")
        with self.assertRaises(ConflictError):
            other.control(study["id"], "start")
        self.assertEqual(len(self.gateway.calls), 1)
        release.set()
        self.manager._threads[study["id"]].join(timeout=10)
        self.assertEqual(self.manager.detail(study["id"])["state"], "succeeded")
        self.assertEqual(len(self.gateway.calls), 2)

    def test_paused_manual_map_stays_archived_after_continued_reading(self):
        entered, release = threading.Event(), threading.Event()
        def block_once():
            if len(self.gateway.calls) == 1:
                entered.set(); release.wait(timeout=10)
        self.gateway.callback = block_once
        study = self.create()
        self.manager.control(study["id"], "start")
        self.assertTrue(entered.wait(timeout=5))
        self.manager.control(study["id"], "pause")
        release.set(); self.manager._threads[study["id"]].join(timeout=10)
        paused = self.manager.detail(study["id"])
        self.assertEqual(paused["state"], "paused")
        old = self.manager.store.export_verified(study["id"])
        edited = self.manager.store.edit_graphs(study["id"], old["hash"], old["graphs"], "确认目前已读候选")
        self.assertFalse(edited["map_version_stale"])
        final = self.run_study(paused)
        self.assertEqual(final["state"], "succeeded", final.get("error"))
        latest = self.manager.store.export_verified(study["id"])
        self.assertTrue(latest["map_version_stale"])
        self.assertEqual(latest["map_version"], edited["map_version"])
        self.assertEqual(len(latest["results"]), 2)


if __name__ == "__main__":
    unittest.main()
