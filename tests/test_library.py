"""Library publication, permissions and evidence use artificial private data."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibenovel.core.files import atomic_write, json_bytes
from vibenovel.core.library import LibraryStore, default_policy
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


class LibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.project = ProjectStore(self.workspace).init("private", genre="general")
        self.other = ProjectStore(self.workspace).init("other", genre="campus")
        self.personal = LibraryStore(self.workspace)
        self.book = LibraryStore(self.workspace, self.project)
        self.before = self.project.fingerprint()

    def source(self, store=None, text="# 方法\r\n先列目标😀，再找障碍。\r\n\r\n第二段：行动应有代价。", outbound="excerpts"):
        return (store or self.personal).import_source("人工材料.md", text.encode("utf-8"), metadata={"title": "自制测试", "outbound": outbound})

    def ref(self, source, quote=None):
        quote = quote or source["text"].splitlines()[1]
        start = source["text"].index(quote)
        return {"source_id": source["id"], "source_version_id": source["version_id"], "start_char": start,
                "end_char": start + len(quote), "quote": quote}

    def entry(self, store=None, **extra):
        return (store or self.personal).save_entry({"type": "material", "title": "桥梁与洪水", "content": "洪水冲垮桥梁，村民只能绕行。", "outbound": "excerpts", **extra})

    def edit(self, store, entry, **extra):
        payload = {key: copy.deepcopy(entry[key]) for key in ("type", "title", "content", "tags", "summary", "outbound", "status", "verification", "source_refs")}
        if "method" in entry:
            payload["method"] = entry["method"]
        payload.update(id=entry["id"], **extra)
        return store.save_entry(payload, entry["hash"])

    def task(self, source, store=None):
        return (store or self.personal).select_for_task({"purpose": "method_extraction", "source_ranges": [self.ref(source)]})

    def bundle(self, source):
        return {"analysis": {"title": "阅读地图", "content": "仅分析所选第二行", "analysis": {"coverage": "选定区间"}, "source_refs": [self.ref(source)]},
                "methods": [{"title": "目标与障碍", "content": "把冲突写成行动。", "source_refs": [self.ref(source)],
                             "method": {"purpose": "场景规划", "steps": ["列目标", "列障碍"], "limitations": ["不保证文学质量"]}}]}

    def test_original_unicode_ranges_and_distinct_bibliography_are_preserved(self):
        source = self.source()
        self.assertEqual(self.personal.path("blobs/" + source["original_sha256"]).read_bytes(), source["text"].encode("utf-8"))
        entry = self.entry(source_refs=[self.ref(source)])
        ref = entry["source_refs"][0]
        self.assertEqual((ref["start_line"], ref["end_line"]), (2, 2))
        self.assertEqual(source["text"][ref["start_char"]:ref["end_char"]], ref["quote"])
        again = self.personal.import_source("另一个书目.md", source["text"].encode("utf-8"), metadata={"author": "另一版本"})
        self.assertNotEqual(again["id"], source["id"])
        self.assertEqual(again["original_sha256"], source["original_sha256"])
        self.assertEqual(self.project.fingerprint(), self.before)

    def test_expanding_legacy_encoding_is_rejected_before_publishing_source(self):
        # GB18030 stores this Chinese character in two bytes; UTF-8 needs three.
        raw = ("桥" * 3600000).encode("gb18030")
        self.assertEqual(len(raw), 7200000)
        with self.assertRaisesRegex(VibeNovelError, "UTF-8"):
            self.personal.import_source("过大抽取.txt", raw, encoding="gb18030")
        self.assertEqual(self.personal.overview()["sources"], [])
        self.assertFalse(self.personal.path(".state/manifest.json").exists())
        self.assertFalse(self.personal.path("blobs").exists())

    def test_search_accounts_for_utf8_rendition_bytes_instead_of_original_encoding(self):
        # A real encoding conversion with a small fixture verifies accounting
        # without allocating several multi-megabyte search inputs.
        source = self.personal.import_source("词项.txt", ("桥" * 100).encode("gb18030"), encoding="gb18030")
        result = self.personal.search("桥")
        record_size = self.personal.path("records/source/%s/%s.json" % (source["id"], source["version_id"])).stat().st_size
        self.assertEqual(result["scanned_bytes"], record_size + len(source["text"].encode("utf-8")))
        self.assertGreater(result["scanned_bytes"], record_size + source["source_bytes"])

    def test_scope_isolation_and_unsafe_paths(self):
        personal = self.entry()
        book = self.entry(self.book, title="本书私有")
        self.assertEqual(LibraryStore(self.workspace, self.other).search("桥梁")["results"], [])
        with self.assertRaises(VibeNovelError):
            self.book.entry(personal["id"])
        with self.assertRaises(VibeNovelError):
            self.book.entry("../" + book["id"])
        with self.assertRaises(VibeNovelError):
            self.personal.import_source("../private.md", b"private")
        with self.assertRaises(VibeNovelError):
            self.personal.import_source("book.pdf", b"fake")

    def test_optimistic_history_and_entry_A_B_A_cannot_revive_task(self):
        first = self.entry(self.book)
        frozen = self.book.select_for_task({"entry_ids": [first["id"]]})
        second = self.edit(self.book, first, content="第二版")
        with self.assertRaises(ConflictError):
            self.edit(self.book, first, content="并发覆盖")
        third = self.edit(self.book, second, content=first["content"])
        self.assertNotEqual(first["version_id"], third["version_id"])
        with self.assertRaises(ConflictError):
            self.book.validate_task_snapshot(frozen)
        self.assertEqual(len(self.book.history("entry", first["id"])), 3)

    def test_source_new_notes_keep_old_fixed_content_but_authorization_ABA_invalidates(self):
        source = self.source()
        frozen = self.task(source)
        notes = self.personal.save_source_metadata(source["id"], {"metadata": {**source["metadata"], "notes": "书目补充"}}, source["hash"])
        self.assertEqual(self.personal.validate_task_snapshot(frozen), frozen)
        denied = self.personal.save_source_metadata(source["id"], {"outbound": "local"}, notes["hash"])
        restored = self.personal.save_source_metadata(source["id"], {"outbound": "excerpts"}, denied["hash"])
        self.assertNotEqual(restored["authorization_revision"], source["authorization_revision"])
        with self.assertRaises(ConflictError):
            self.personal.validate_task_snapshot(frozen)
        new = self.task(source)
        self.personal.validate_task_snapshot(new)

    def test_local_or_summary_source_never_sends_original(self):
        source = self.source(outbound="local")
        with self.assertRaises(ConflictError):
            self.task(source)
        source = self.personal.save_source_metadata(source["id"], {"outbound": "summary"}, source["hash"])
        with self.assertRaises(ConflictError):
            self.task(source)
        card = self.entry(source_refs=[self.ref(source)], outbound="summary", summary="作者自行写的总结")
        frozen = self.personal.select_for_task({"entry_ids": [card["id"]]})
        self.assertIn(card["summary"], frozen["text"])
        self.assertNotIn(source["text"].splitlines()[1], frozen["text"])
        self.assertEqual(frozen["source_fragments"], [])

    def test_binding_stays_fixed_on_new_content_and_revocation_ABA_requires_rebind(self):
        card = self.entry()
        bound = self.book.bind(card["id"])
        frozen = self.book.select_for_task({"binding_ids": [bound["id"]]})
        newer = self.edit(self.personal, card, content="新版信息")
        self.book.validate_task_snapshot(frozen)
        self.assertIn(card["content"], frozen["text"])
        denied = self.edit(self.personal, newer, outbound="local")
        self.edit(self.personal, denied, outbound="excerpts")
        with self.assertRaises(ConflictError):
            self.book.validate_task_snapshot(frozen)

    def test_method_bundle_is_candidate_idempotent_and_author_adoption_is_separate(self):
        source = self.source()
        snapshot = self.task(source)
        bundle = self.bundle(source)
        job = "f" * 32
        result = self.book.publish_method_bundle(bundle, task_snapshot=snapshot, job_id=job)
        method = result["methods"][0]
        self.assertFalse(method["adopted"])
        self.assertEqual(method["outbound"], "local")
        self.assertEqual(method["source_refs"][0]["scope"], "personal")
        self.assertEqual(self.book.method_bundle(bundle_id=job), result)
        self.assertEqual(self.book.publish_method_bundle(bundle, task_snapshot=snapshot, job_id=job), result)
        edited = self.edit(self.book, method, outbound="excerpts")
        with self.assertRaises(ConflictError):
            self.book.select_for_task({"entry_ids": [edited["id"]]})
        adopted = self.book.adopt_method(edited["id"], edited["hash"])
        self.assertTrue(adopted["adopted"])
        self.assertTrue(next(e for e in self.book.overview()["entries"] if e["id"] == adopted["id"])["adopted"])
        frozen = self.book.select_for_task({"entry_ids": [adopted["id"]]})
        self.assertIn("列障碍", frozen["text"])
        withdrawn = self.personal.save_source_metadata(source["id"], {"withdrawn": True}, source["hash"])
        with self.assertRaises(ConflictError):
            self.book.validate_task_snapshot(frozen)
        self.assertTrue(withdrawn["withdrawn"])
        self.assertEqual(self.project.fingerprint(), self.before)

    def test_bundle_rejects_forged_or_unread_evidence_before_any_card_is_visible(self):
        source = self.source()
        bundle = self.bundle(source)
        bundle["methods"][0]["source_refs"][0] = self.ref(source, "第二段：行动应有代价。")
        with self.assertRaises(VibeNovelError):
            self.book.publish_method_bundle(bundle, self.task(source))
        self.assertEqual(self.book.overview()["entries"], [])
        bundle = self.bundle(source)
        bundle["methods"][0]["source_refs"][0]["quote"] = "伪造的原句"
        with self.assertRaises(ConflictError):
            self.book.publish_method_bundle(bundle, self.task(source))

    def test_manifest_last_hides_partial_bundle_and_recovers_complete_intent(self):
        source = self.source(self.book)
        snapshot = self.task(source, self.book)
        original = atomic_write
        def fail_manifest(path, raw, **kwargs):
            if path.name == "manifest.json":
                raise OSError("模拟清单发布中断")
            return original(path, raw, **kwargs)
        with patch("vibenovel.core.library.atomic_write", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.book.publish_method_bundle(self.bundle(source), snapshot, "a" * 32)
        # The authoritative manifest still exposes zero cards before recovery.
        import json
        before = json.loads(self.book.path(".state/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(before["entities"]["entry"], {})
        result = self.book.method_bundle(bundle_id="a" * 32)
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(len(self.book.overview()["entries"]), 2)
        self.assertFalse(self.book.path(".state/journal.json").exists())

    def test_prejournal_failure_leaves_no_partial_published_records(self):
        source = self.source(self.book)
        original = atomic_write
        count = [0]
        def fail_second(path, raw, **kwargs):
            if "records/entry" in path.as_posix():
                count[0] += 1
                if count[0] == 2:
                    raise OSError("模拟第二张卡保存中断")
            return original(path, raw, **kwargs)
        with patch("vibenovel.core.library.atomic_write", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.book.publish_method_bundle(self.bundle(source), self.task(source, self.book))
        self.assertEqual(self.book.overview()["entries"], [])
        self.assertIsNone(self.book.method_bundle(bundle_id="b" * 32))

    def test_collections_questions_chinese_search_and_automatic_scope(self):
        selected = self.entry(self.book, title="桥梁"), self.entry(self.book, title="桥梁的旁支")
        collection = self.book.save_collection({"title": "考据", "entry_ids": [selected[0]["id"]]})
        question = self.book.save_question({"title": "桥梁为何断裂", "question": "需要核实材质", "entry_ids": [selected[0]["id"]]})
        self.assertEqual(question["status"], "open")
        self.assertEqual(len(self.book.search("洪水桥梁")["results"]), 2)
        policy = default_policy()
        policy.update(mode="recommend", collections=[collection["id"]])
        saved = self.book.save_policy(policy)
        with self.assertRaises(VibeNovelError):
            self.book.select_for_task({"automatic": True, "query": "桥梁", "purpose": "writing"})
        policy["mode"] = "auto"
        self.book.save_policy(policy, saved["hash"])
        snapshot = self.book.select_for_task({"automatic": True, "query": "桥梁", "purpose": "writing"})
        self.assertEqual([m["id"] for m in snapshot["manifest"]], [selected[0]["id"]])
        self.assertEqual(self.book.questions()[0]["id"], question["id"])

    def test_required_items_are_not_silently_trimmed_and_policy_ABA_stays_stale(self):
        one, two = self.entry(self.book), self.entry(self.book)
        frozen = self.book.select_for_task({"entry_ids": [one["id"]]})
        policy = default_policy()
        policy.update(mode="auto", required_ids=[one["id"], two["id"]], max_items=1)
        saved = self.book.save_policy(policy)
        with self.assertRaises(VibeNovelError):
            self.book.select_for_task({"automatic": True, "query": "桥梁"})
        self.book.save_policy(default_policy(), saved["hash"])
        with self.assertRaises(ConflictError):
            self.book.validate_task_snapshot(frozen)

    def test_empty_auto_retrieval_and_optional_budget_omissions_do_not_block_writing(self):
        policy = default_policy()
        policy.update(mode="auto", max_characters=20)
        saved = self.book.save_policy(policy)
        empty = self.book.select_for_task({"automatic": True, "query": "不存在的资料", "purpose": "writing"})
        self.assertEqual(empty["text"], "")
        self.assertEqual(empty["typed_deps"], [])
        self.book.validate_task_snapshot(empty)
        card = self.entry(self.book, content="桥梁" * 100)
        omitted = self.book.select_for_task({"automatic": True, "query": "桥梁", "purpose": "writing"})
        self.assertEqual(omitted["text"], "")
        self.assertTrue(any(m.get("id") == card["id"] and m.get("included") is False for m in omitted["manifest"]))
        policy["required_ids"] = [card["id"]]
        self.book.save_policy(policy, saved["hash"])
        with self.assertRaises(VibeNovelError):
            self.book.select_for_task({"automatic": True, "query": "桥梁", "purpose": "writing"})

    def test_explicit_rebinding_old_content_cannot_bypass_current_outbound_permission(self):
        source = self.source()
        card = self.entry(source_refs=[self.ref(source)])
        denied = self.edit(self.personal, card, outbound="local")
        with self.assertRaises(ConflictError):
            self.book.bind(card["id"], version_id=card["version_id"])
        allowed = self.edit(self.personal, denied, outbound="excerpts")
        blocked_source = self.personal.save_source_metadata(source["id"], {"outbound": "local"}, source["hash"])
        with self.assertRaises(ConflictError):
            self.book.bind(allowed["id"])
        self.personal.save_source_metadata(source["id"], {"outbound": "excerpts"}, blocked_source["hash"])
        bound = self.book.bind(allowed["id"])
        self.book.validate_task_snapshot(self.book.select_for_task({"binding_ids": [bound["id"]]}))

    def test_personal_source_revocation_during_book_publication_prevents_restart_publish(self):
        source = self.source()
        snapshot = self.task(source)
        original = atomic_write
        def fail_manifest(path, raw, **kwargs):
            if path.name == "manifest.json":
                raise OSError("模拟清单发布中断")
            return original(path, raw, **kwargs)
        with patch("vibenovel.core.library.atomic_write", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.book.publish_method_bundle(self.bundle(source), snapshot, "c" * 32)
        self.personal.save_source_metadata(source["id"], {"withdrawn": True}, source["hash"])
        with self.assertRaises(ConflictError):
            self.book.overview()
        self.assertEqual(self.book.overview()["entries"], [])
        self.assertEqual(len(list(self.book.path(".state").glob("rejected-*.json"))), 1)
        self.assertIsNone(self.book.method_bundle("c" * 32))

    def test_bound_personal_evidence_in_book_snapshot_keeps_its_true_scope(self):
        source = self.source()
        card = self.entry(content=self.ref(source)["quote"], source_refs=[self.ref(source)])
        bound = self.book.bind(card["id"])
        snapshot = self.book.select_for_task({"binding_ids": [bound["id"]], "purpose": "method_extraction"})
        result = self.book.publish_method_bundle(self.bundle(source), snapshot)
        method = result["methods"][0]
        self.assertEqual(method["source_refs"][0]["scope"], "personal")
        self.assertEqual(method["source_refs"][0]["owner"], "personal")

    def test_citation_dependencies_do_not_claim_unsent_original_quotes_as_input(self):
        source = self.source(self.book)
        card = self.entry(self.book, content="作者只提供抽象的场景提示，不包含所引原句。", source_refs=[self.ref(source)])
        snapshot = self.book.select_for_task({"entry_ids": [card["id"]]})
        self.assertNotIn(self.ref(source)["quote"], snapshot["text"])
        self.assertEqual(snapshot["source_fragments"], [])
        self.assertTrue(any(dep["kind"] == "source" and dep["id"] == source["id"] for dep in snapshot["typed_deps"]))
        self.assertIn("evidence_note", snapshot["manifest"][0])
        personal_source = self.source()
        personal_card = self.entry(content="作者自己的转化笔记。", source_refs=[self.ref(personal_source)])
        bound = self.book.bind(personal_card["id"])
        snapshot = self.book.select_for_task({"binding_ids": [bound["id"]]})
        self.assertEqual(snapshot["source_fragments"], [])
        self.assertIn("evidence_note", snapshot["manifest"][0])
        verbatim = self.entry(self.book, content=self.ref(source)["quote"], source_refs=[self.ref(source)])
        snapshot = self.book.select_for_task({"entry_ids": [verbatim["id"]]})
        self.assertEqual(snapshot["source_fragments"][0]["quote"], self.ref(source)["quote"])

    def test_method_publication_rechecks_analysis_inside_same_library_lock(self):
        source = self.source(self.book)
        analysis = self.book.publish_method_bundle({"analysis": self.bundle(source)["analysis"], "methods": []}, self.task(source, self.book))["analysis"]
        expected = {key: analysis[key] for key in ("id", "version_id", "hash")}
        self.edit(self.book, analysis, status="archived")
        with self.assertRaises(ConflictError):
            self.book.publish_method_bundle(self.bundle(source), self.task(source, self.book), expected_analysis=expected)
        self.assertEqual(len(self.book.overview()["entries"]), 1)

    def test_method_journal_preserves_and_revalidates_analysis_dependency(self):
        source = self.source(self.book)
        analysis = self.book.publish_method_bundle({"analysis": self.bundle(source)["analysis"], "methods": []}, self.task(source, self.book))["analysis"]
        expected = {key: analysis[key] for key in ("id", "version_id", "hash")}
        original = atomic_write
        def fail_manifest(path, raw, **kwargs):
            if path.name == "manifest.json":
                raise OSError("模拟整包清单发布中断")
            return original(path, raw, **kwargs)
        with patch("vibenovel.core.library.atomic_write", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.book.publish_method_bundle(self.bundle(source), self.task(source, self.book), "d" * 32, expected_analysis=expected)
        # Fault injection: recovery must evaluate the saved precondition, not
        # just trust the complete candidate files and repeat publication.
        with patch.object(LibraryStore, "_check_analysis", side_effect=ConflictError("模拟分析选择已失效")) as checked:
            with self.assertRaises(ConflictError):
                self.book.method_bundle("d" * 32)
            self.assertEqual(checked.call_args.args[1], expected)
        self.assertEqual(len(self.book.overview()["entries"]), 1)
        self.assertIsNone(self.book.method_bundle("d" * 32))

    def test_method_bundle_replay_cannot_change_analysis_precondition(self):
        source = self.source(self.book)
        analysis = self.book.publish_method_bundle({"analysis": self.bundle(source)["analysis"], "methods": []}, self.task(source, self.book))["analysis"]
        expected = {key: analysis[key] for key in ("id", "version_id", "hash")}
        bundle, snapshot = self.bundle(source), self.task(source, self.book)
        result = self.book.publish_method_bundle(bundle, snapshot, "e" * 32, expected_analysis=expected)
        self.assertEqual(result["expected_analysis"], expected)
        with self.assertRaises(ConflictError):
            self.book.publish_method_bundle(bundle, snapshot, "e" * 32, expected_analysis=None)

    def test_corrupt_blob_and_cross_scope_snapshot_fail_closed(self):
        source = self.source()
        snapshot = self.task(source)
        with self.assertRaises(ConflictError):
            self.book.validate_task_snapshot(snapshot)
        self.personal.path("blobs/" + source["original_sha256"]).write_bytes(b"modified")
        with self.assertRaises(ConflictError):
            self.personal.source(source["id"])
        with self.assertRaises(ConflictError):
            self.personal.validate_task_snapshot(snapshot)

    def test_library_link_is_rejected_where_symlink_creation_is_available(self):
        outside = self.workspace / "external"
        outside.mkdir()
        try:
            (self.workspace / "library").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("当前Windows账户无符号链接创建权限")
        with self.assertRaises(VibeNovelError):
            LibraryStore(self.workspace).overview()
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
