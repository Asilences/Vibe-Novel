"""Independent offline provenance boundaries for reference-research methods."""
import copy
import json
import unittest
import uuid
from unittest.mock import patch

from tests import test_reference_jobs as fixtures
from vibenovel.core.files import atomic_write
from vibenovel.core.library import LibraryStore
from vibenovel.errors import VibeNovelError


class ReferenceMethodBoundaryTests(unittest.TestCase):
    setUp = fixtures.ReferenceBatchTests.setUp
    manager = fixtures.ReferenceBatchTests.manager
    request = fixtures.ReferenceBatchTests.request
    create = fixtures.ReferenceBatchTests.create
    wait = fixtures.ReferenceBatchTests.wait

    @staticmethod
    def response(prompt):
        result = fixtures.ReferenceBatchTests.response(prompt)
        result["methods"] = [{"title": "让选择带来代价", "steps": ["先明确人物可选行动，再检查行动代价。"],
                              "limitations": ["只是待作者试用的方法；未证明能提高文学质量。"],
                              "source_refs": copy.deepcopy(result["structure"][0]["source_refs"])}]
        return result

    def finished_research(self):
        manager = self.manager()
        batch = self.create(manager)
        manager.control(self.book["book_id"], batch["id"], "start")
        result = self.wait(manager, batch["id"])
        self.assertEqual(result["state"], "succeeded", result["error"])
        node_id = result["nodes"][-1]["id"]
        return LibraryStore(self.workspace), result, node_id

    def publish(self, personal, batch, node_id, key=None):
        preview = personal.preview_reference_methods(self.book["book_id"], batch["id"], node_id, [0])
        return personal.publish_reference_methods(self.book["book_id"], batch["id"], node_id, [0],
                                                  preview["hash"], key or uuid.uuid4().hex)

    def edit(self, store, entry, **changes):
        fields = {"id", "type", "title", "content", "tags", "summary", "outbound", "status", "verification",
                  "source_refs", "method", "analysis", "withdrawn", "withdrawal_reason"}
        payload = {key: copy.deepcopy(value) for key, value in entry.items() if key in fields}
        payload.update(changes)
        return store.save_entry(payload, entry["hash"])

    def permission(self, **changes):
        current = self.store.detail(self.book["book_id"])
        return self.store.update_policy(current["book_id"], expected_hash=current["hash"],
                                        outbound=changes.get("outbound", current["outbound"]),
                                        withdrawn=changes.get("withdrawn", current["withdrawn"]))

    def ready(self, personal, entry):
        edited = self.edit(personal, entry, outbound="summary", summary="检查选择和后果之间的联系。", status="ready")
        return personal.adopt_method(edited["id"], edited["hash"])

    def test_aba_permission_change_cannot_revive_candidate_or_prepared_preview(self):
        personal, batch, node_id = self.finished_research()
        preview = personal.preview_reference_methods(self.book["book_id"], batch["id"], node_id, [0])
        self.permission(withdrawn=True)
        self.permission(withdrawn=False)
        with self.assertRaises(VibeNovelError):
            personal.publish_reference_methods(self.book["book_id"], batch["id"], node_id, [0],
                                               preview["hash"], uuid.uuid4().hex)
        self.assertEqual(personal.overview()["entries"], [])

    def test_edit_without_origin_preserves_it_and_client_cannot_attach_origin_to_copy(self):
        personal, batch, node_id = self.finished_research()
        entry = self.publish(personal, batch, node_id)["methods"][0]
        original_origin = copy.deepcopy(entry["research_origin"])
        edited = self.edit(personal, entry, content="作者补充说明，不声称重新检查了整书。")
        self.assertEqual(edited["research_origin"], original_origin)
        malicious = {"type": "method", "title": "复制卡", "content": "copy", "method": {"steps": ["步骤"]},
                     "research_origin": original_origin}
        with self.assertRaises(VibeNovelError):
            personal.save_entry(malicious)
        with self.assertRaises(VibeNovelError):
            personal.save_entry({"id": edited["id"], "type": "style", "title": "改成无来源的表达卡", "content": "保留文字但试图移除方法来源"},
                                edited["hash"])
        self.permission(outbound="local")
        with self.assertRaises(VibeNovelError):
            personal.adopt_method(edited["id"], edited["hash"])

    def test_binding_historical_entry_and_frozen_task_recheck_reference_permission(self):
        personal, batch, node_id = self.finished_research()
        entry = self.ready(personal, self.publish(personal, batch, node_id)["methods"][0])
        book_library = LibraryStore(self.workspace, self.project)
        with self.assertRaises(VibeNovelError):
            book_library.preview_reference_methods(self.book["book_id"], batch["id"], node_id, [0])
        bound = book_library.bind(entry["id"], version_id=entry["version_id"], purpose="writing")
        snapshot = book_library.select_for_task({"purpose": "writing", "binding_ids": [bound["id"]]})
        self.assertTrue(snapshot["text"])
        self.permission(withdrawn=True)
        self.permission(withdrawn=False)
        for operation in (
            lambda: book_library.bind(entry["id"], version_id=entry["version_id"], purpose="writing"),
            lambda: book_library.select_for_task({"purpose": "writing", "binding_ids": [bound["id"]]}),
            lambda: book_library.validate_task_snapshot(snapshot),
        ):
            with self.assertRaises(VibeNovelError):
                operation()

    def test_binding_and_task_recheck_transitive_raw_shards(self):
        personal, batch, node_id = self.finished_research()
        entry = self.ready(personal, self.publish(personal, batch, node_id)["methods"][0])
        book_library = LibraryStore(self.workspace, self.project)
        bound = book_library.bind(entry["id"], version_id=entry["version_id"], purpose="writing")
        snapshot = book_library.select_for_task({"purpose": "writing", "binding_ids": [bound["id"]]})
        detail = self.store.detail(self.book["book_id"])
        relative = "%s/editions/%s/%s" % (detail["book_id"], detail["edition_id"], detail["chapters"][1]["blocks"][0]["path"])
        path = self.store.path(relative)
        path.write_bytes(path.read_bytes() + b"modified")
        for operation in (
            lambda: book_library.bind(entry["id"], version_id=entry["version_id"]),
            lambda: book_library.select_for_task({"purpose": "writing", "binding_ids": [bound["id"]]}),
            lambda: book_library.validate_task_snapshot(snapshot),
        ):
            with self.assertRaises(VibeNovelError):
                operation()

    def test_recovery_rechecks_origin_before_exposing_candidate_bundle(self):
        personal, batch, node_id = self.finished_research()
        preview = personal.preview_reference_methods(self.book["book_id"], batch["id"], node_id, [0])
        def fail_manifest(path, raw, **kwargs):
            if path.name == "manifest.json" and path.parent.parent == personal.root:
                raise OSError("模拟候选发布中断")
            return atomic_write(path, raw, **kwargs)
        with patch("vibenovel.core.library.atomic_write", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                personal.publish_reference_methods(self.book["book_id"], batch["id"], node_id, [0],
                                                   preview["hash"], uuid.uuid4().hex)
        self.assertTrue(personal.path(".state/journal.json").exists())
        self.permission(withdrawn=True)
        with self.assertRaises(VibeNovelError):
            personal.overview()
        self.assertEqual(personal.overview()["entries"], [])
        self.assertFalse(personal.path(".state/journal.json").exists())

    def test_research_quotes_are_dependencies_not_actual_sent_source_fragments(self):
        personal, batch, node_id = self.finished_research()
        entry = self.ready(personal, self.publish(personal, batch, node_id)["methods"][0])
        snapshot = personal.select_for_task({"purpose": "writing", "entry_ids": [entry["id"]]})
        self.assertEqual(snapshot["source_fragments"], [])
        self.assertTrue(any(dep["kind"] == "reference_research" for dep in snapshot["typed_deps"]))
        verbatim = self.edit(personal, entry, outbound="excerpts", content=entry["research_origin"]["evidence"][0]["quote"])
        verbatim = personal.adopt_method(verbatim["id"], verbatim["hash"])
        self.assertEqual(personal.select_for_task({"purpose": "writing", "entry_ids": [verbatim["id"]]})["source_fragments"], [])
        book_library = LibraryStore(self.workspace, self.project)
        bound = book_library.bind(entry["id"], version_id=entry["version_id"], purpose="writing")
        self.assertEqual(book_library.select_for_task({"purpose": "writing", "binding_ids": [bound["id"]]})["source_fragments"], [])

    def test_legacy_method_extraction_cannot_launder_research_card_dependencies(self):
        personal, batch, node_id = self.finished_research()
        entry = self.ready(personal, self.publish(personal, batch, node_id)["methods"][0])
        snapshot = personal.select_for_task({"purpose": "method_extraction", "entry_ids": [entry["id"]]})
        bundle = {"methods": [{"type": "method", "title": "再提炼候选", "source_refs": [],
                               "method": {"steps": ["该旧接口尚不支持继承研究卡的来源关系。"]}}]}
        before = len(personal.overview()["entries"])
        with self.assertRaises(VibeNovelError):
            personal.publish_method_bundle(bundle, snapshot)
        self.assertEqual(len(personal.overview()["entries"]), before)
        manager = self.manager()
        for field in ("entry_ids", "binding_ids", "library_selection"):
            request = {"kind": "analyze_reference", "provider": "api", "purpose": "scene_beats",
                       "library_scope": "personal", "source_ranges": [], "idempotency_key": uuid.uuid4().hex,
                       field: {"entry_ids": [entry["id"]]} if field == "library_selection" else [entry["id"]]}
            with self.assertRaises(VibeNovelError):
                manager.jobs.preview(self.project, request)


if __name__ == "__main__":
    unittest.main()
