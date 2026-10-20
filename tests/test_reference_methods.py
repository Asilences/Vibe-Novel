"""Method-bridge lifecycle uses synthetic books and offline model responses."""
import copy
import json
import unittest
import uuid
from unittest.mock import patch

from tests import test_reference_method_boundaries as fixtures
from vibenovel.core.files import atomic_write
from vibenovel.core.library import LibraryStore, default_policy
from vibenovel.errors import VibeNovelError


class ReferenceMethodTests(unittest.TestCase):
    setUp = fixtures.ReferenceMethodBoundaryTests.setUp
    manager = fixtures.ReferenceMethodBoundaryTests.manager
    request = fixtures.ReferenceMethodBoundaryTests.request
    create = fixtures.ReferenceMethodBoundaryTests.create
    wait = fixtures.ReferenceMethodBoundaryTests.wait
    response = staticmethod(fixtures.ReferenceMethodBoundaryTests.response)
    finished_research = fixtures.ReferenceMethodBoundaryTests.finished_research
    edit = fixtures.ReferenceMethodBoundaryTests.edit
    permission = fixtures.ReferenceMethodBoundaryTests.permission
    ready = fixtures.ReferenceMethodBoundaryTests.ready
    publish = fixtures.ReferenceMethodBoundaryTests.publish

    def test_candidate_edit_adopt_bind_and_frozen_task_lifecycle(self):
        personal, batch, node_id = self.finished_research()
        result = self.publish(personal, batch, node_id)
        candidate = result["methods"][0]
        self.assertEqual((candidate["outbound"], candidate["status"], candidate["verification"], candidate["provenance"]),
                         ("local", "inbox", "unverified", "AI_SUGGESTED"))
        self.assertFalse(candidate["adopted"])
        self.assertEqual(candidate["source_refs"], [])
        with self.assertRaises(VibeNovelError):
            personal.select_for_task({"entry_ids": [candidate["id"]]})
        enabled = self.ready(personal, candidate)
        own = personal.select_for_task({"entry_ids": [enabled["id"]], "purpose": "writing"})
        self.assertEqual(own["source_fragments"], [])
        self.assertEqual(len([dep for dep in own["typed_deps"] if dep["kind"] == "reference_research"]), 1)
        personal.validate_task_snapshot(own)
        book = LibraryStore(self.workspace, self.project)
        bound = book.bind(enabled["id"], purpose="writing")
        task = book.select_for_task({"purpose": "writing", "binding_ids": [bound["id"]]})
        book.validate_task_snapshot(task)
        self.assertNotIn("reference_resource", task["text"])
        edited = self.edit(personal, enabled, title="作者修订方法")
        self.assertFalse(edited["adopted"])
        self.assertEqual(edited["research_origin"], candidate["research_origin"])
        with self.assertRaises(VibeNovelError):
            personal.validate_task_snapshot(own)

    def test_idempotent_replay_preserves_original_candidate_and_author_edits(self):
        personal, batch, node_id = self.finished_research()
        args = (self.book["book_id"], batch["id"], node_id, [0])
        preview = personal.preview_reference_methods(*args)
        key = uuid.uuid4().hex
        original = personal.publish_reference_methods(*args, preview["hash"], key)
        entry = original["methods"][0]
        edited = self.edit(personal, entry, title="作者保存的新标题")
        replay = personal.publish_reference_methods(*args, preview["hash"], key)
        self.assertEqual(replay, original)
        self.assertEqual(personal.entry(entry["id"])["hash"], edited["hash"])
        other_node = batch["nodes"][0]["id"]
        other_args = (self.book["book_id"], batch["id"], other_node, [0])
        other = personal.preview_reference_methods(*other_args)
        with self.assertRaises(VibeNovelError):
            personal.publish_reference_methods(*other_args, other["hash"], key)
        self.assertEqual(len(personal.overview()["entries"]), 1)

    def test_preview_missing_selection_and_unsuccessful_node_are_controlled_errors(self):
        personal = LibraryStore(self.workspace)
        with self.assertRaises(VibeNovelError):
            personal.preview_reference_methods(self.book["book_id"], "1" * 32, "chapter-00000", [0])
        manager = self.manager()
        batch = self.create(manager)
        with self.assertRaises(VibeNovelError):
            personal.preview_reference_methods(self.book["book_id"], batch["id"], "chapter-00000", [0])
        for indices in ([], [True], [0, 0], [-1], [6], ["0"]):
            with self.assertRaises(VibeNovelError):
                personal.preview_reference_methods(self.book["book_id"], batch["id"], "chapter-00000", indices)

    def test_changed_transitive_result_or_raw_original_prevents_reuse(self):
        personal, batch, node_id = self.finished_research()
        candidate = self.publish(personal, batch, node_id)["methods"][0]
        original_node = batch["nodes"][0]
        path = self.store.path(original_node["result_path"])
        saved = path.read_bytes()
        path.write_bytes(saved + b" ")
        with self.assertRaises(VibeNovelError):
            personal.adopt_method(candidate["id"], candidate["hash"])
        path.write_bytes(saved)
        detail = self.store.detail(self.book["book_id"])
        edition = self.store.path("%s/editions/%s/metadata.json" % (detail["book_id"], detail["edition_id"]))
        metadata = json.loads(edition.read_text(encoding="utf-8"))
        original = self.store.path("%s/editions/%s/%s" % (detail["book_id"], detail["edition_id"], metadata["original_path"]))
        original.write_bytes(original.read_bytes() + b"changed")
        with self.assertRaises(VibeNovelError):
            personal.adopt_method(candidate["id"], candidate["hash"])

    def test_journal_recovery_publishes_once_without_model_or_overwriting_cards(self):
        personal, batch, node_id = self.finished_research()
        args = (self.book["book_id"], batch["id"], node_id, [0])
        preview = personal.preview_reference_methods(*args)
        key, call_count = uuid.uuid4().hex, len(self.calls)
        def interrupted(path, raw, **kwargs):
            if path.name == "manifest.json" and path.parent.parent == personal.root:
                raise OSError("artificial publication interruption")
            return atomic_write(path, raw, **kwargs)
        with patch("vibenovel.core.library.atomic_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                personal.publish_reference_methods(*args, preview["hash"], key)
        self.assertTrue(personal.path(".state/journal.json").exists())
        restored = LibraryStore(self.workspace)
        self.assertEqual(len(restored.overview()["entries"]), 1)
        result = restored.publish_reference_methods(*args, preview["hash"], key)
        self.assertEqual(len(result["methods"]), 1)
        self.assertEqual(len(self.calls), call_count)
        self.assertFalse(personal.path(".state/journal.json").exists())

    def test_automatic_optional_invalid_origin_is_omitted_but_required_rejected(self):
        personal, batch, node_id = self.finished_research()
        candidate = self.ready(personal, self.publish(personal, batch, node_id)["methods"][0])
        book = LibraryStore(self.workspace, self.project)
        bound = book.bind(candidate["id"], purpose="writing")
        policy = default_policy()
        policy.update(mode="auto")
        saved_policy = book.save_policy(policy)
        self.permission(withdrawn=True)
        snapshot = book.select_for_task({"automatic": True, "purpose": "writing", "query": candidate["title"]})
        self.assertEqual(snapshot["text"], "")
        self.assertTrue(any(item.get("id") == bound["id"] and not item["included"] for item in snapshot["manifest"]))
        policy["required_ids"] = [bound["id"]]
        book.save_policy(policy, saved_policy["hash"])
        with self.assertRaises(VibeNovelError):
            book.select_for_task({"automatic": True, "purpose": "writing", "query": candidate["title"]})

    def test_research_dependency_capacity_rejects_without_truncation(self):
        personal, batch, node_id = self.finished_research()
        candidate = self.ready(personal, self.publish(personal, batch, node_id)["methods"][0])
        with patch("vibenovel.core.library.MAX_RESEARCH_TASK_DEPENDENCIES_BYTES", 50):
            with self.assertRaises(VibeNovelError):
                personal.select_for_task({"entry_ids": [candidate["id"]]})
        snapshot = personal.select_for_task({"entry_ids": [candidate["id"]]})
        with patch("vibenovel.core.library.MAX_RESEARCH_TASK_DEPENDENCIES_BYTES", 50):
            with self.assertRaises(VibeNovelError):
                personal.validate_task_snapshot(snapshot)
        with patch("vibenovel.core.reference_methods.MAX_RESEARCH_ORIGIN_BYTES", 50):
            with self.assertRaises(VibeNovelError):
                personal.preview_reference_methods(self.book["book_id"], batch["id"], node_id, [0])

    def test_generic_method_reextraction_cannot_drop_research_origin(self):
        personal, batch, node_id = self.finished_research()
        candidate = self.ready(personal, self.publish(personal, batch, node_id)["methods"][0])
        snapshot = personal.select_for_task({"entry_ids": [candidate["id"]], "purpose": "method_extraction"})
        with self.assertRaises(VibeNovelError):
            personal.publish_method_bundle({"methods": [{"title": "没有封存来源的新方法", "method": {"steps": ["copy"]}}]}, snapshot)
        self.assertEqual(len(personal.overview()["entries"]), 1)

    def test_invalid_origin_allows_terminal_author_restriction_only(self):
        personal, batch, node_id = self.finished_research()
        candidate = self.ready(personal, self.publish(personal, batch, node_id)["methods"][0])
        self.permission(withdrawn=True)
        for changes in ({"content": "趁来源失效替换内容", "status": "archived"},
                        {"method": {"steps": ["无来源步骤"]}, "withdrawn": True},
                        {"outbound": "excerpts", "status": "archived"}):
            with self.assertRaises(VibeNovelError):
                self.edit(personal, candidate, **changes)
        archived = self.edit(personal, candidate, status="archived", outbound="local", withdrawn=True,
                             withdrawal_reason="参考研究已失效，仅保留历史")
        self.assertEqual(archived["research_origin"], candidate["research_origin"])
        self.assertEqual(archived["method"], candidate["method"])
        self.assertFalse(archived["adopted"])
        with self.assertRaises(VibeNovelError):
            self.edit(personal, archived, status="ready", outbound="summary", withdrawn=False)
        with self.assertRaises(VibeNovelError):
            personal.adopt_method(archived["id"], archived["hash"])


if __name__ == "__main__":
    unittest.main()
