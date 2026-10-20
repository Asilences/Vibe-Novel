"""Additional adversarial boundaries for persisted reference research plans."""
import copy
import json
import unittest

from vibenovel.core.files import atomic_write, digest, json_bytes, read_json
from vibenovel.core.locking import ConflictError
from vibenovel.errors import VibeNovelError
from vibenovel.studio.reference_jobs import _output
from tests import test_reference_jobs as fixtures


class ReferenceBatchBoundaryTests(unittest.TestCase):
    setUp = fixtures.ReferenceBatchTests.setUp
    response = staticmethod(fixtures.ReferenceBatchTests.response)
    manager = fixtures.ReferenceBatchTests.manager
    request = fixtures.ReferenceBatchTests.request
    create = fixtures.ReferenceBatchTests.create
    wait = fixtures.ReferenceBatchTests.wait

    def seed_chapters(self, manager, batch):
        for node in batch["nodes"]:
            if node["kind"] != "chapter":
                continue
            content, fragments = manager._inputs(batch, node)
            prompt = "<reference_material>\n" + content + "\n</reference_material>"
            result = _output(json.dumps(self.response(prompt), ensure_ascii=False), fragments)
            relative = "%s/editions/%s/analyses/%s/%s.json" % (batch["book_id"], batch["edition_id"], batch["id"], node["id"])
            path = self.store.path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = json_bytes(result)
            atomic_write(path, raw)
            node.update(state="succeeded", result_path=relative, result_hash=digest(raw), result_summary=result["summary"])

    def test_synthesis_revalidates_transitive_raw_dependencies(self):
        manager = self.manager()
        public = self.create(manager)
        batch = manager._load(self.book["book_id"], public["id"])
        self.seed_chapters(manager, batch)
        detail = self.store.detail(self.book["book_id"])
        relative = "%s/editions/%s/%s" % (detail["book_id"], detail["edition_id"], detail["chapters"][0]["blocks"][0]["path"])
        path = self.store.path(relative)
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaises(VibeNovelError):
            manager._inputs(batch, batch["nodes"][-1])
        self.assertFalse(self.calls)

    def test_saved_plan_budget_tampering_is_rejected_before_start(self):
        manager = self.manager()
        public = self.create(manager)
        path = manager._path(self.book["book_id"], public["id"])
        value = read_json(path, max_bytes=8 * 1024 * 1024)
        value["budget"]["max_cost_usd"] += 100
        atomic_write(path, json_bytes(value), overwrite=True)
        with self.assertRaises(VibeNovelError):
            manager.control(self.book["book_id"], public["id"], "start")
        self.assertFalse(self.calls)

    def test_saved_node_cannot_read_other_books_candidate_path(self):
        manager = self.manager()
        public = self.create(manager)
        batch = manager._load(self.book["book_id"], public["id"])
        self.seed_chapters(manager, batch)
        other = self.store.import_book("另一书.txt", "第一章 私人\n另一个参考来源。".encode())
        relative = "%s/editions/%s/analyses/private.json" % (other["book_id"], other["edition_id"])
        path = self.store.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json_bytes({"private": "不应跨书读取的候选"})
        atomic_write(path, raw)
        batch["nodes"][0].update(result_path=relative, result_hash=digest(raw))
        manager._save(batch)
        with self.assertRaises(VibeNovelError):
            manager.get_node(self.book["book_id"], public["id"], batch["nodes"][0]["id"])

    def test_late_permission_revocation_does_not_publish_candidate(self):
        def operation(prompt, event):
            current = self.store.detail(self.book["book_id"])
            self.store.update_policy(current["book_id"], expected_hash=current["hash"], outbound="local")
        manager = self.manager(operation)
        public = self.create(manager)
        manager.control(self.book["book_id"], public["id"], "start")
        finished = self.wait(manager, public["id"])
        self.assertIn(finished["state"], {"failed", "interrupted"})
        self.assertNotIn("result_path", finished["nodes"][0])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
