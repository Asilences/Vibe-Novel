"""Saved a10 responses can be repaired without repeating or erasing charges."""
import copy
import json
import unittest
import time
import uuid
import tempfile
from pathlib import Path

from tests import test_reference_jobs as fixtures
from tests import test_reference_api as api_fixtures
from vibenovel.core.files import digest, read_json, tree_files, atomic_write
from vibenovel.core.locking import ConflictError
from vibenovel.errors import VibeNovelError
from vibenovel.studio.providers import GenerationResult
from vibenovel.studio.model_registry import ModelRegistry
from vibenovel.core.project import ProjectStore
from vibenovel.core.reference_books import ReferenceBookStore


class ReferenceBatchRepairTests(unittest.TestCase):
    setUp = fixtures.ReferenceBatchTests.setUp
    response = staticmethod(fixtures.ReferenceBatchTests.response)
    manager = fixtures.ReferenceBatchTests.manager
    request = fixtures.ReferenceBatchTests.request
    create = fixtures.ReferenceBatchTests.create
    wait = fixtures.ReferenceBatchTests.wait

    def fail_first(self, *, finish_reason="stop", usage=True, response_text=None):
        manager = self.manager()
        owner = self

        class Provider:
            def generate(self, prompt, cancel):
                owner.calls.append(prompt)
                value = owner.response(prompt)
                value["structure"][0]["source_refs"][0]["end_char"] += 1
                return GenerationResult(json.dumps(value, ensure_ascii=False) if response_text is None else response_text,
                                        {"prompt_tokens": 10, "completion_tokens": 10} if usage else None,
                                        "offline-model", finish_reason)

        manager.jobs._factory = lambda _: Provider()
        batch = self.create(manager)
        manager.control(self.book["book_id"], batch["id"], "start")
        failed = self.wait(manager, batch["id"])
        node = manager.get_node(self.book["book_id"], batch["id"], "chapter-00000")
        _, fragments = manager._inputs(manager._load(self.book["book_id"], batch["id"]), node)
        content = json.dumps({"source_refs": fragments}, ensure_ascii=False)
        corrected = json.dumps(self.response("<reference_material>\n" + content + "\n</reference_material>"), ensure_ascii=False)
        return manager, failed, node, corrected

    def test_repair_preserves_original_and_budget_then_only_continues_unsent_nodes(self):
        manager, failed, node, corrected = self.fail_first()
        original = node["pending_response"]
        before = manager._load(self.book["book_id"], failed["id"])
        proof = manager.response(self.book["book_id"], failed["id"], node["id"])
        self.assertTrue(proof["repairable"])
        repaired = manager.repair_response(self.book["book_id"], failed["id"], node["id"], proof["hash"], corrected, "仅修正精确引文坐标")
        self.assertEqual(repaired["state"], "paused")
        self.assertEqual(repaired["coverage"]["completed_chapters"], 1)
        self.assertEqual(len(self.calls), 1)
        after = manager._load(self.book["book_id"], failed["id"])
        self.assertEqual(after["preview_hash"], before["preview_hash"])
        self.assertEqual(after["parent_reservations"], before["parent_reservations"])
        self.assertEqual(after["nodes"][0]["budget_summary"], before["nodes"][0]["budget_summary"])
        self.assertEqual(after["nodes"][0]["attempt_id"], before["nodes"][0]["attempt_id"])
        self.assertEqual(after["nodes"][0]["pending_response"], original)
        archived = list(self.store.path(self.book["book_id"] + "/editions/" + self.book["edition_id"] + "/response-repairs").rglob("*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(read_json(archived[0])["original_hash"], digest(original.encode("utf-8")))
        again = manager.repair_response(self.book["book_id"], failed["id"], node["id"], proof["hash"], corrected, "仅修正精确引文坐标")
        self.assertEqual(again["state"], "paused")
        self.assertEqual(len(self.calls), 1)

        owner = self
        class NextProvider:
            def generate(self, prompt, cancel):
                owner.calls.append(prompt)
                return GenerationResult(json.dumps(owner.response(prompt), ensure_ascii=False),
                                        {"prompt_tokens": 10, "completion_tokens": 10}, "offline-model", "stop")
        manager.jobs._factory = lambda _: NextProvider()
        manager.control(self.book["book_id"], failed["id"], "start")
        final = self.wait(manager, failed["id"])
        self.assertEqual(final["state"], "succeeded", final.get("error"))
        self.assertEqual(len(self.calls), 3, "only second chapter and synthesis may be sent")
        self.assertEqual(manager.response(self.book["book_id"], failed["id"], node["id"])["text"], original)
        self.assertEqual(list(self.project.path("chapters").glob("*.md")), [])

    def test_wrong_hash_missing_unsent_or_changed_quote_is_not_repaired(self):
        manager, failed, node, corrected = self.fail_first()
        proof = manager.response(self.book["book_id"], failed["id"], node["id"])
        with self.assertRaises(ConflictError):
            manager.repair_response(self.book["book_id"], failed["id"], node["id"], "0" * 64, corrected, "错误指纹")
        for change in ("quote", "chapter_id", "offset"):
            value = json.loads(corrected)
            ref = value["structure"][0]["source_refs"][0]
            if change == "quote":
                ref["quote"] = "该原句从未发送"
            elif change == "chapter_id":
                ref["chapter_id"] = failed["nodes"][1]["chapter_id"]
            else:
                ref["start_char"] += 1
                ref["end_char"] += 1
            with self.subTest(change=change), self.assertRaises(VibeNovelError):
                manager.repair_response(self.book["book_id"], failed["id"], node["id"], proof["hash"], json.dumps(value, ensure_ascii=False), "不应发布")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(manager.get(self.book["book_id"], failed["id"])["state"], "failed")

    def test_length_or_unknown_billing_cannot_be_completed_offline(self):
        for finish, usage in (("length", True), ("stop", False)):
            manager, failed, node, corrected = self.fail_first(finish_reason=finish, usage=usage)
            proof = manager.response(self.book["book_id"], failed["id"], node["id"])
            self.assertFalse(proof["repairable"])
            before = copy.deepcopy(manager._load(self.book["book_id"], failed["id"])["parent_reservations"])
            with self.assertRaises(ConflictError):
                manager.repair_response(self.book["book_id"], failed["id"], node["id"], proof["hash"], corrected, "不得补造截断或未知响应")
            self.assertEqual(manager._load(self.book["book_id"], failed["id"])["parent_reservations"], before)

    def test_stop_with_invalid_original_json_is_read_only_before_editing(self):
        for original in ('{"title":', '{"title":"first","title":"duplicate"}'):
            with self.subTest(original=original):
                manager, failed, node, corrected = self.fail_first(response_text=original)
                before = copy.deepcopy(manager._load(self.book["book_id"], failed["id"])["parent_reservations"])
                calls = len(self.calls)
                proof = manager.response(self.book["book_id"], failed["id"], node["id"])
                self.assertFalse(proof["repairable"])
                self.assertEqual(proof["text"], original)
                self.assertEqual(proof["hash"], digest(original.encode("utf-8")))
                with self.assertRaises(VibeNovelError):
                    manager.repair_response(self.book["book_id"], failed["id"], node["id"], proof["hash"], corrected, "不能补造不完整原JSON")
                self.assertEqual(len(self.calls), calls)
                self.assertEqual(manager._load(self.book["book_id"], failed["id"])["parent_reservations"], before)
                self.assertEqual(manager.get(self.book["book_id"], failed["id"])["state"], "failed")

    def test_revoked_source_blocks_repair_and_keeps_input(self):
        manager, failed, node, corrected = self.fail_first()
        proof = manager.response(self.book["book_id"], failed["id"], node["id"])
        current = self.store.detail(self.book["book_id"])
        self.store.update_policy(self.book["book_id"], expected_hash=current["hash"], outbound="local")
        with self.assertRaises(ConflictError):
            manager.repair_response(self.book["book_id"], failed["id"], node["id"], proof["hash"], corrected, "来源已撤权")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(manager.response(self.book["book_id"], failed["id"], node["id"])["hash"], proof["hash"])

    def test_repair_archive_succeeds_in_long_windows_workspace_without_nested_batch_name(self):
        # Use an absolute workspace as deep as the real Windows acceptance
        # checkout. The former 295-character archive path fails on ordinary
        # Windows Python builds; the flattened one stays below MAX_PATH.
        with tempfile.TemporaryDirectory(prefix="vbn-deep-repair-") as temporary:
            base = Path(temporary).resolve()
            workspace = base / ("w" * max(1, 104 - len(str(base)) - 1))
            workspace.mkdir()
            for source in self.workspace.rglob("*"):
                if source.is_dir():
                    (workspace / source.relative_to(self.workspace)).mkdir(parents=True, exist_ok=True)
            for source in tree_files(self.workspace):
                target = workspace / source.relative_to(self.workspace)
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, source.read_bytes())
            self.workspace = workspace
            self.projects = ProjectStore(workspace)
            self.project = self.projects.get("funding")
            self.store = ReferenceBookStore(workspace)
            manager, failed, node, corrected = self.fail_first()
            original = manager.response(self.book["book_id"], failed["id"], node["id"])
            repaired = manager.repair_response(self.book["book_id"], failed["id"], node["id"],
                                               original["hash"], corrected, "长工作区路径离线修复")
            self.assertEqual(repaired["state"], "paused")
            folder = self.store.path(self.book["book_id"] + "/editions/" + self.book["edition_id"] + "/response-repairs")
            archives = list(folder.glob("*.json"))
            self.assertEqual(len(archives), 1)
            self.assertLess(len(str(archives[0])), 260)
            record = read_json(archives[0])
            self.assertEqual(record["batch_id"], failed["id"])
            self.assertEqual(record["node_id"], node["id"])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(manager.response(self.book["book_id"], failed["id"], node["id"])["text"], original["text"])
            manager.close()


@unittest.skipUnless(api_fixtures.TestClient, "Optional Studio API tests")
class ReferenceBatchRepairAPITests(unittest.TestCase):
    setUp = api_fixtures.ReferenceAPITests.setUp
    send = api_fixtures.ReferenceAPITests.send
    import_book = api_fixtures.ReferenceAPITests.import_book

    def test_response_repair_requires_csrf_strict_hash_and_never_starts_next_node(self):
        registry = ModelRegistry(self.workspace)
        public = registry.public()
        public["profiles"][0].update(model="offline-repair", price_input_usd=.1, price_output_usd=.2)
        registry.save({"profiles": public["profiles"]}, public["hash"])
        calls, original_good = [], []
        class Provider:
            def generate(self, prompt, cancel):
                calls.append(prompt)
                good = fixtures.ReferenceBatchTests.response(prompt)
                original_good.append(json.dumps(good, ensure_ascii=False))
                good["structure"][0]["source_refs"][0]["end_char"] += 1
                return GenerationResult(json.dumps(good, ensure_ascii=False),
                                        {"prompt_tokens": 10, "completion_tokens": 10}, "offline-repair", "stop")
        self.client.app.state.jobs._factory = lambda _: Provider()
        book = self.import_book()
        prefix = "/" + book["book_id"]
        self.assertEqual(self.send(prefix + "/policy", {"expected_hash": book["hash"], "outbound": "excerpts"}, "PUT").status_code, 200)
        request = {"novel_id": "author", "chapter_ids": [], "purpose": "精确引文核对", "budget_max_usd": 1., "max_output_tokens": 1024}
        preview = self.send(prefix + "/batches/preview", request).json()
        created = self.send(prefix + "/batches", {**request, "expected_preview": preview["hash"], "idempotency_key": uuid.uuid4().hex}).json()
        batch_path = prefix + "/batches/" + created["id"]
        self.assertEqual(self.send(batch_path + "/start").status_code, 200)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            batch = self.client.get("/api/reference-books" + batch_path).json()
            if batch["state"] == "failed":
                break
            time.sleep(.02)
        self.assertEqual(batch["state"], "failed")
        response_path = batch_path + "/nodes/" + batch["nodes"][0]["id"] + "/response"
        original = self.client.get("/api/reference-books" + response_path).json()
        self.assertTrue(original["repairable"])
        command = {"expected_hash": original["hash"], "text": original_good[0], "reason": "修正唯一原句的坐标"}
        self.assertEqual(self.client.put("/api/reference-books" + response_path, json=command).status_code, 403)
        self.assertEqual(self.send(response_path, {**command, "override_budget": True}, "PUT").status_code, 422)
        self.assertEqual(self.send(response_path, {**command, "expected_hash": "0" * 64}, "PUT").status_code, 409)
        repaired = self.send(response_path, command, "PUT")
        self.assertEqual(repaired.status_code, 200, repaired.text)
        self.assertEqual(repaired.json()["state"], "paused")
        self.assertEqual(repaired.json()["budget_summary"], batch["budget_summary"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.client.get("/api/reference-books" + response_path).json()["text"], original["text"])
        self.assertEqual(self.project.fingerprint(), self.fingerprint)


if __name__ == "__main__":
    unittest.main()
