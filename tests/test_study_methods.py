"""Study -> private candidate -> author adoption -> book task, all synthetic."""
import copy
import uuid
import unittest

from tests import test_reference_studies as fixtures
from vibenovel.core.library import LibraryStore
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


class StudyMethodTests(unittest.TestCase):
    setUp = fixtures.StudyTests.setUp
    request = fixtures.StudyTests.request
    create = fixtures.StudyTests.create
    run_study = fixtures.StudyTests.run_study

    def finish(self, request=None):
        study = self.create(request)
        final = self.run_study(study)
        self.assertEqual(final["state"], "succeeded", final.get("error"))
        return final

    @staticmethod
    def edit(personal, entry, **changes):
        fields = {"id", "type", "title", "content", "tags", "summary", "outbound", "status", "verification",
                  "source_refs", "method", "analysis", "withdrawn", "withdrawal_reason"}
        data = {key: copy.deepcopy(value) for key, value in entry.items() if key in fields}
        return personal.save_entry({**data, **changes}, entry["hash"])

    def adopt(self, personal, entry):
        edited = self.edit(personal, entry, status="ready", outbound="summary", summary="按人物选择检验行动的代价。")
        return personal.adopt_method(edited["id"], edited["hash"])

    def publish(self, study, indices=None):
        personal = LibraryStore(self.workspace)
        indices = indices or [0]
        preview = personal.preview_study_methods(study["id"], indices)
        result = personal.publish_study_methods(study["id"], indices, preview["hash"], uuid.uuid4().hex)
        return personal, result, preview

    def test_study_method_full_author_lifecycle_and_withdrawal(self):
        study = self.finish()
        personal, bundle, preview = self.publish(study)
        entry = bundle["methods"][0]
        self.assertEqual(entry["research_origin"]["source_type"], "reference_study")
        self.assertEqual(entry["source_refs"], [])
        self.assertFalse(entry["adopted"])
        with self.assertRaises(VibeNovelError):
            personal.select_for_task({"entry_ids": [entry["id"]]})
        ready = self.adopt(personal, entry)
        task = personal.select_for_task({"entry_ids": [ready["id"]], "purpose": "writing"})
        self.assertEqual(task["source_fragments"], [])
        self.assertEqual(len([dep for dep in task["typed_deps"] if dep["kind"] == "reference_research"]), 1)
        personal.validate_task_snapshot(task)
        project = ProjectStore(self.workspace).init("synthetic-author", genre="campus")
        own = LibraryStore(self.workspace, project)
        bound = own.bind(ready["id"], purpose="writing")
        frozen = own.select_for_task({"binding_ids": [bound["id"]], "purpose": "writing"})
        own.validate_task_snapshot(frozen)
        current = self.books.detail(self.book["book_id"])
        self.books.update_policy(current["book_id"], expected_hash=current["hash"], outbound="local", withdrawn=True)
        for check in (lambda: personal.adopt_method(ready["id"], ready["hash"]),
                      lambda: own.bind(ready["id"], purpose="writing"),
                      lambda: own.select_for_task({"binding_ids": [bound["id"]], "purpose": "writing"}),
                      lambda: own.validate_task_snapshot(frozen)):
            with self.assertRaises(VibeNovelError):
                check()

    def test_cross_book_method_keeps_all_origins_even_if_only_second_book_withdraws(self):
        second = self.books.import_book("人工B.txt", "第一章 起点\n小红看见山顶的灯火，决定改变回家的道路。".encode(), title="人工书B")
        self.books.update_policy(second["book_id"], expected_hash=second["hash"], outbound="excerpts")
        request = self.request(); request["kind"] = "comparison"
        request["sources"][0]["chapter_ids"] = [self.book["chapters"][0]["chapter_id"]]
        request["sources"].append({"book_id": second["book_id"], "edition_id": second["edition_id"], "chapter_ids": []})
        study = self.finish(request)
        personal, bundle, preview = self.publish(study)
        entry = bundle["methods"][0]
        self.assertEqual({book["book_id"] for book in entry["research_origin"]["books"]}, {self.book["book_id"], second["book_id"]})
        self.assertEqual({ref["book_id"] for ref in entry["research_origin"]["evidence"]}, {self.book["book_id"], second["book_id"]})
        ready = self.adopt(personal, entry)
        task = personal.select_for_task({"entry_ids": [ready["id"]], "purpose": "writing"})
        personal.validate_task_snapshot(task)
        current = self.books.detail(second["book_id"])
        self.books.update_policy(current["book_id"], expected_hash=current["hash"], outbound="local")
        with self.assertRaises(VibeNovelError):
            personal.validate_task_snapshot(task)
        with self.assertRaises(VibeNovelError):
            personal.publish_study_methods(study["id"], [0], preview["hash"], uuid.uuid4().hex)

    def test_original_and_later_result_changes_block_historical_tasks(self):
        study = self.finish()
        personal, bundle, _ = self.publish(study)
        ready = self.adopt(personal, bundle["methods"][0])
        task = personal.select_for_task({"entry_ids": [ready["id"]], "purpose": "writing"})
        path = self.manager.store.path(study["id"] + "/results/read-00002.json")
        raw = path.read_bytes(); path.write_bytes(raw + b" ")
        with self.assertRaises(VibeNovelError):
            personal.validate_task_snapshot(task)
        path.write_bytes(raw)
        shard = self.books.path(self.book["book_id"] + "/editions/" + self.book["edition_id"] + "/text/part-00001.txt")
        shard.write_bytes(shard.read_bytes() + b"tamper")
        with self.assertRaises(VibeNovelError):
            personal.validate_task_snapshot(task)

    def test_client_cannot_strip_or_forge_origin_and_method_publish_is_idempotent(self):
        study = self.finish()
        personal = LibraryStore(self.workspace)
        preview = personal.preview_study_methods(study["id"], [0])
        key = uuid.uuid4().hex
        first = personal.publish_study_methods(study["id"], [0], preview["hash"], key)
        entry = first["methods"][0]
        edited = self.edit(personal, entry, title="作者编辑标题")
        self.assertEqual(edited["research_origin"], entry["research_origin"])
        replay = personal.publish_study_methods(study["id"], [0], preview["hash"], key)
        self.assertEqual(replay, first)
        self.assertEqual(personal.entry(entry["id"])["title"], "作者编辑标题")
        with self.assertRaises(VibeNovelError):
            personal.save_entry({"type": "method", "title": "伪造新卡", "content": "text", "method": {"steps": ["x"]}, "research_origin": entry["research_origin"]})


if __name__ == "__main__":
    unittest.main()
