"""Offline reference-book research, exact evidence and non-replaying recovery."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.project import ProjectStore
from vibenovel.core.reference_books import ReferenceBookStore
from vibenovel.core.locking import ConflictError
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.model_registry import ModelRegistry
from vibenovel.studio.providers import GenerationResult, ProviderError
from vibenovel.studio.reference_jobs import ReferenceBatchManager, _output


class ReferenceBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.projects = ProjectStore(self.workspace)
        self.project = self.projects.init("funding", genre="general")
        env = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                     if not key.upper().startswith("VIBENOVEL_")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        registry = ModelRegistry(self.workspace)
        profile = registry.public()["profiles"][0]
        profile.update(model="offline-model", price_input_usd=0.1, price_output_usd=0.2)
        registry.save({"profiles": [profile]}, None)
        self.store = ReferenceBookStore(self.workspace)
        self.book = self.store.import_book("参考小说.txt", "第一章 出发\n甲离开家乡。\n第二章 选择\n乙决定留下。".encode("utf-8"))
        self.book = self.store.update_policy(self.book["book_id"], expected_hash=self.book["hash"], outbound="excerpts")
        self.calls = []

    @staticmethod
    def response(prompt):
        content = json.loads(prompt.split("<reference_material>\n", 1)[1].rsplit("\n</reference_material>", 1)[0])
        if isinstance(content, dict):
            original = content["source_refs"][0]
            ref = {key: original[key] for key in ("chapter_id", "start_char", "end_char", "quote")}
            ref["quote"] = ref["quote"][:12]
            ref["end_char"] = ref["start_char"] + len(ref["quote"])
        else:
            ref = content[0]["structure"][0]["source_refs"][0]
        observation = {"title": "行动", "observation": "人物采取行动；其余原因待核。", "source_refs": [ref]}
        return {"title": "范围分析", "summary": "只描述本次实际输入。", "structure": [observation],
                "expression": [observation], "threads": [], "methods": [], "gaps": ["仅核对精确引文，效果待作者评阅。"]}

    def manager(self, operation=None):
        owner = self
        class Provider:
            def generate(self, prompt, cancel):
                owner.calls.append(prompt)
                if operation:
                    operation(prompt, cancel)
                return GenerationResult(json.dumps(owner.response(prompt), ensure_ascii=False),
                                        {"prompt_tokens": 10, "completion_tokens": 10}, "offline-model")
        jobs = JobManager(self.workspace, provider_factory=lambda name: Provider())
        self.addCleanup(jobs.close)
        manager = ReferenceBatchManager(self.workspace, self.projects, jobs)
        self.addCleanup(manager.close)
        return manager

    def request(self, **changes):
        return {"novel_id": "funding", "chapter_ids": [], "purpose": "内容安排与表达", "budget_max_usd": 1,
                "max_output_tokens": 2048, **changes}

    def create(self, manager, request=None):
        request = request or self.request()
        preview = manager.preview(self.book["book_id"], request)
        return manager.create(self.book["book_id"], request, preview["hash"], uuid.uuid4().hex)

    def wait(self, manager, identity, target=None, timeout=10, interval=.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = manager.get(self.book["book_id"], identity)
            if value["state"] in (target or {"succeeded", "failed", "interrupted", "cancelled"}):
                return value
            time.sleep(interval)
        self.fail("参考研究未及时完成：" + json.dumps({"state": value["state"], "error": value["error"],
                  "calls": len(self.calls), "coverage": value["coverage"]}, ensure_ascii=False))

    def test_prepared_all_chapters_then_bounded_synthesis_and_exact_results(self):
        manager = self.manager()
        batch = self.create(manager)
        self.assertEqual(batch["state"], "prepared")
        self.assertFalse(self.calls)
        manager.control(self.book["book_id"], batch["id"], "start")
        finished = self.wait(manager, batch["id"])
        self.assertEqual(finished["state"], "succeeded", finished["error"])
        self.assertEqual(finished["coverage"]["completed_chapters"], 2)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(finished["budget_summary"]["calls"], 3)
        self.assertEqual(finished["result"]["status"], "candidate")
        self.assertEqual(list(self.project.path("chapters").glob("*.md")), [])
        node = manager.get_node(self.book["book_id"], batch["id"], "chapter-00000")
        self.assertEqual(node["result"]["verification"], "exact_quotes_only")
        self.assertNotIn("result", finished["nodes"][0])

    def test_idempotency_and_model_changes_bind_whole_preview(self):
        manager = self.manager()
        request = self.request()
        preview = manager.preview(self.book["book_id"], request)
        key = uuid.uuid4().hex
        first = manager.create(self.book["book_id"], request, preview["hash"], key)
        same = manager.create(self.book["book_id"], request, preview["hash"], key)
        self.assertEqual(first["id"], same["id"])
        with self.assertRaises(ConflictError):
            manager.create(self.book["book_id"], self.request(purpose="另一个问题"), preview["hash"], key)
        registry = ModelRegistry(self.workspace)
        public = registry.public()
        public["profiles"][0]["label"] = "已修改"
        registry.save({"profiles": public["profiles"]}, public["hash"])
        with self.assertRaises(ConflictError):
            manager.control(self.book["book_id"], first["id"], "start")
        self.assertFalse(self.calls)

    def test_pause_finishes_current_then_resume_does_not_repeat_it(self):
        entered, release = threading.Event(), threading.Event()
        def operation(prompt, cancel):
            if len(self.calls) == 1:
                entered.set()
                self.assertTrue(release.wait(4))
        manager = self.manager(operation)
        batch = self.create(manager)
        manager.control(self.book["book_id"], batch["id"], "start")
        self.assertTrue(entered.wait(3))
        manager.control(self.book["book_id"], batch["id"], "pause")
        release.set()
        paused = self.wait(manager, batch["id"], {"paused"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(paused["coverage"]["completed_chapters"], 1)
        manager.control(self.book["book_id"], batch["id"], "start")
        finished = self.wait(manager, batch["id"])
        self.assertEqual(finished["state"], "succeeded", finished["error"])
        self.assertEqual(len(self.calls), 3)

    def test_unknown_billing_is_retained_and_cannot_resume_or_restart(self):
        def operation(prompt, cancel):
            raise ProviderError("offline failure")
        manager = self.manager(operation)
        batch = self.create(manager)
        manager.control(self.book["book_id"], batch["id"], "start")
        failed = self.wait(manager, batch["id"])
        self.assertEqual(failed["state"], "interrupted")
        self.assertTrue(failed["budget_summary"]["uncertain"])
        self.assertGreater(failed["budget_summary"]["reserved_cost_usd"], 0)
        with self.assertRaises(ConflictError):
            manager.control(self.book["book_id"], batch["id"], "retry")
        manager.close()
        restarted = self.manager()
        self.assertEqual(restarted.get(self.book["book_id"], batch["id"])["state"], "interrupted")
        self.assertEqual(len(self.calls), 1)

    def test_cancel_discards_late_response_and_keeps_recorded_usage(self):
        entered, release = threading.Event(), threading.Event()
        def operation(prompt, cancel):
            entered.set()
            self.assertTrue(release.wait(4))
        manager = self.manager(operation)
        batch = self.create(manager)
        manager.control(self.book["book_id"], batch["id"], "start")
        self.assertTrue(entered.wait(3))
        manager.control(self.book["book_id"], batch["id"], "cancel")
        release.set()
        deadline = time.monotonic() + 4
        while manager._threads[(self.book["book_id"], batch["id"])].is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        finished = manager.get(self.book["book_id"], batch["id"])
        self.assertEqual(finished["state"], "cancelled")
        self.assertNotIn("result_path", finished["nodes"][0])
        self.assertEqual(finished["budget_summary"]["calls"], 1)
        self.assertGreater(finished["budget_summary"]["reserved_cost_usd"], 0)
        with self.assertRaises(ConflictError):
            manager.control(self.book["book_id"], batch["id"], "start")
        self.assertEqual(len(self.calls), 1)

    def test_permissions_cannot_be_revoked_and_readded_to_revive_batch(self):
        manager = self.manager()
        batch = self.create(manager)
        current = self.store.detail(self.book["book_id"])
        current = self.store.update_policy(self.book["book_id"], expected_hash=current["hash"], outbound="local")
        self.store.update_policy(self.book["book_id"], expected_hash=current["hash"], outbound="excerpts")
        with self.assertRaises(ConflictError):
            manager.control(self.book["book_id"], batch["id"], "start")
        self.assertFalse(self.calls)

    def test_zero_unknown_and_insufficient_budget_are_rejected(self):
        manager = self.manager()
        for amount in (0, -1, float("nan"), 0.000001):
            with self.assertRaises(VibeNovelError):
                manager.preview(self.book["book_id"], self.request(budget_max_usd=amount))
        registry = ModelRegistry(self.workspace)
        public = registry.public()
        public["profiles"][0]["price_input_usd"] = None
        registry.save({"profiles": public["profiles"]}, public["hash"])
        with self.assertRaisesRegex(VibeNovelError, "价格"):
            manager.preview(self.book["book_id"], self.request())
        self.assertFalse(self.calls)

    def test_full_book_dag_over_32_calls_uses_explicit_parent_budget_and_pagination(self):
        book = self.store.import_book("长篇.txt", "\n".join("第%d章 决定\n角色行动%d。" % (index, index) for index in range(1, 52)).encode("utf-8"))
        self.book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        manager = self.manager()
        from vibenovel.core.files import file_digest
        with patch("vibenovel.core.reference_books.file_digest", wraps=file_digest) as checks:
            preview = manager.preview(self.book["book_id"], self.request())
        original_checks = [call for call in checks.call_args_list if "originals" in Path(call.args[0]).parts]
        self.assertEqual(len(original_checks), 1, "整书预览必须单作用域校验原件，不能每章重哈整原件")
        self.assertEqual(preview["coverage"]["selected_chapters"], 51)
        self.assertEqual(preview["budget"]["total_calls"], 69)
        self.assertEqual(len(preview["nodes"]), 50)
        self.assertEqual(preview["total_nodes"], 69)
        batch = manager.create(self.book["book_id"], self.request(), preview["hash"], uuid.uuid4().hex)
        self.assertEqual(len(manager.get(self.book["book_id"], batch["id"], offset=50)["nodes"]), 19)
        manager.control(self.book["book_id"], batch["id"], "start")
        finished = self.wait(manager, batch["id"], timeout=120, interval=.2)
        self.assertEqual(finished["state"], "succeeded", finished["error"])
        self.assertEqual(finished["coverage"]["completed_chapters"], 51)
        self.assertEqual(finished["budget_summary"]["calls"], 69)
        self.assertEqual(len(self.calls), 69)

    def test_long_chapter_is_split_without_silently_losing_range(self):
        book = self.store.import_book("长章.txt", ("第一章 长章\n" + "人物继续行动。\n" * 8000).encode("utf-8"))
        self.book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        manager = self.manager()
        preview = manager.preview(self.book["book_id"], self.request())
        nodes = [node for node in preview["nodes"] if node["kind"] == "chapter"]
        self.assertGreater(len(nodes), 1)
        self.assertEqual(nodes[0]["refs"][0]["start_char"], 0)
        for left, right in zip(nodes, nodes[1:]):
            self.assertEqual(left["refs"][0]["end_char"], right["refs"][0]["start_char"])
        detail = self.store.detail(self.book["book_id"])
        self.assertEqual(nodes[-1]["refs"][0]["end_char"], detail["chapters"][0]["characters"])

    def test_quote_validation_and_two_endpoint_payoff_gate(self):
        fragment = {"chapter_id": "c", "start_char": 0, "end_char": 4, "quote": "精确原句"}
        valid = {"title": "测试", "summary": "仅范围观察", "structure": [], "expression": [],
                 "threads": [], "methods": [], "gaps": ["资料不足"]}
        invalid = copy.deepcopy(valid)
        invalid["threads"] = [{"title": "回收", "status": "recovered", "explanation": "不能凭空认定", "setup_refs": [], "payoff_refs": []}]
        with self.assertRaises(VibeNovelError):
            _output(json.dumps(invalid), [fragment])
        invalid = copy.deepcopy(valid)
        invalid["structure"] = [{"title": "观察", "observation": "没有证据", "source_refs": [{**fragment, "quote": "不同原句"}]}]
        with self.assertRaises(VibeNovelError):
            _output(json.dumps(invalid), [fragment])

    def test_synthesis_keeps_all_threads_and_method_evidence_in_actual_input(self):
        manager = self.manager()
        public = self.create(manager)
        batch = manager._load(self.book["book_id"], public["id"])
        from vibenovel.core.files import atomic_write, digest, json_bytes
        for node in batch["nodes"]:
            if node["kind"] != "chapter":
                continue
            content, fragments = manager._inputs(batch, node)
            value = self.response("<reference_material>\n" + content + "\n</reference_material>")
            ref = value["structure"][0]["source_refs"][0]
            value["threads"] = [{"title": "必须保留的跨章候选", "status": "unresolved", "explanation": "后续待核",
                                  "setup_refs": [ref], "payoff_refs": []}]
            value["methods"] = [{"title": "不能被省掉的方法", "steps": ["比较条件"], "limitations": ["仅候选"], "source_refs": [ref]}]
            result = _output(json.dumps(value, ensure_ascii=False), fragments)
            relative = "%s/editions/%s/analyses/%s/%s.json" % (batch["book_id"], batch["edition_id"], batch["id"], node["id"])
            path = self.store.path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = json_bytes(result)
            atomic_write(path, raw)
            node.update(state="succeeded", result_path=relative, result_hash=digest(raw), result_summary=result["summary"])
        content, fragments = manager._inputs(batch, batch["nodes"][-1])
        self.assertEqual(content.count("必须保留的跨章候选"), 2)
        self.assertEqual(content.count("不能被省掉的方法"), 2)


if __name__ == "__main__":
    unittest.main()
