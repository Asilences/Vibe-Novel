"""Existing manuscript revisions preserve sources and author decisions."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import json_bytes
from vibenovel.core.imports import import_outline
from vibenovel.core.locking import ConflictError
from vibenovel.core.manuscripts import ManuscriptStore
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


class ManuscriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.manuscripts = ManuscriptStore(self.project)

    def source(self, text="前言作者说明\r\n\r\n第一章 走廊\r\n第一章正文。\r\n\r\n# 第二章 门后\r\n第二章正文。\r\n"):
        return self.documents.save("drafts/existing.md", text, None)

    def candidate(self, session, section, content="作者选择的修订正文。", **changes):
        identifier = uuid4().hex
        path = "drafts/rewrite-" + identifier + ".md"
        candidate = self.documents.save(path, content, None, "AI_SUGGESTED")
        with self.documents.storage.locked() as state:
            job = {"schema_version": 1, "id": identifier, "novel_id": "story", "kind": "rewrite", "state": "succeeded",
                "provider": "api", "request": {"document_path": section["path"], "document_hash": section["current_hash"]},
                "context_sources": {section["path"]: section["current_hash"]}, "result_path": path,
                "result_hash": candidate["hash"], "result_version_id": state["history"][path][-1]["id"],
                "canonical_revision": state["revision"], "fingerprint": state["fingerprint"]}
        job.update(changes)
        directory = self.project.path(".runtime/jobs")
        directory.mkdir(exist_ok=True)
        self.project.path(".runtime/jobs/" + identifier + ".json").write_bytes(json_bytes(job))
        return candidate

    def test_preview_prepare_and_export_partition_exact_source_and_preserve_preamble(self):
        source = self.source()
        before = self.project.fingerprint()
        preview = self.manuscripts.preview(source["path"], source["hash"])
        self.assertEqual(len(preview["sections"]), 3)
        self.assertEqual([entry["start_line"] for entry in preview["sections"]], [1, 3, 6])
        self.assertIn("前言", "".join(preview["warnings"]))
        session = self.manuscripts.prepare(source["path"], source["hash"])
        rebuilt = "".join(self.documents.read(entry["path"])["content"] for entry in session["sections"])
        self.assertEqual(rebuilt, source["content"])
        self.assertEqual(self.manuscripts.export(session["id"])["content"], source["content"])
        self.assertEqual(before, self.project.fingerprint())
        self.assertEqual(self.documents.read(source["path"])["content"], source["content"])
        self.assertEqual(len(self.manuscripts.list()["manuscripts"]), 1)
        self.assertEqual(list(self.project.path("chapters").glob("*.md")), [])

    def test_manual_boundaries_are_strict_and_source_hash_is_required(self):
        source = self.source()
        self.assertEqual(len(self.manuscripts.preview(source["path"], source["hash"], [])['sections']), 1)
        manual = self.manuscripts.preview(source["path"], source["hash"], [3, 6])
        self.assertEqual([entry["start_line"] for entry in manual["sections"]], [1, 3, 6])
        for boundaries in ([3, 3], [6, 3], [0], [99], [True], "3"):
            with self.subTest(boundaries=boundaries), self.assertRaises(VibeNovelError):
                self.manuscripts.preview(source["path"], source["hash"], boundaries)
        with self.assertRaises(ConflictError):
            self.manuscripts.preview(source["path"], "0" * 64)
        for path in ("../outside.md", "config/api.local.json", "reviews/review.md", "drafts/existing.txt"):
            with self.subTest(path=path), self.assertRaises(VibeNovelError):
                self.manuscripts.preview(path, source["hash"])

    def test_heading_detection_requires_explicit_title_boundary(self):
        source = self.source("第一章正文不会误切。\n第一章\n正文\n# 第二章：转折\n第二章正文。\nChapter 3 End\n结局\n")
        preview = self.manuscripts.preview(source["path"], source["hash"])
        self.assertEqual([entry["start_line"] for entry in preview["sections"]], [1, 2, 4, 6])

    def test_import_bundle_can_prepare_without_becoming_canon(self):
        path = self.workspace / "完整小说.txt"
        text = "第一章 开场\n正文原稿\n第二章 转折\n后续原稿"
        path.write_bytes(text.encode("utf-8"))
        imported = import_outline(self.project, path)
        source = self.documents.read("imports/original/%s/extracted.md" % imported["import_id"])
        before = self.project.fingerprint()
        session = self.manuscripts.prepare(source["path"], source["hash"])
        self.assertEqual(len(session["sections"]), 2)
        self.assertEqual(self.manuscripts.export(session["id"])["content"], text)
        self.assertEqual(before, self.project.fingerprint())
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))

    def test_failed_preparation_never_publishes_complete_session(self):
        source = self.source()
        original = DocumentStore.save

        def fail_manifest(store, path, *args, **kwargs):
            if path.startswith("reviews/manuscript-"):
                raise OSError("injected manifest failure")
            return original(store, path, *args, **kwargs)

        before = self.project.fingerprint()
        with patch.object(DocumentStore, "save", fail_manifest), self.assertRaises(OSError):
            self.manuscripts.prepare(source["path"], source["hash"])
        self.assertEqual(self.manuscripts.list()["manuscripts"], [])
        self.assertEqual(before, self.project.fingerprint())
        self.assertEqual(self.documents.read(source["path"])["hash"], source["hash"])

    def test_source_or_manifest_change_invalidates_session_without_overwriting_working_copies(self):
        source = self.source()
        session = self.manuscripts.prepare(source["path"], source["hash"])
        original = self.documents.read(session["sections"][0]["path"])
        self.documents.save(source["path"], "作者替换整稿", source["hash"])
        self.assertTrue(self.manuscripts.read(session["id"])["stale"])
        with self.assertRaises(ConflictError):
            self.manuscripts.export(session["id"])
        self.assertEqual(self.documents.read(original["path"])["hash"], original["hash"])
        manifest = self.documents.read(session["path"])
        self.documents.save(manifest["path"], manifest["content"], manifest["hash"])
        with self.assertRaises(ConflictError):
            self.manuscripts.read(session["id"])

    def test_corrupt_manifest_metadata_is_rejected_and_omitted_from_catalog(self):
        source = self.source()
        session = self.manuscripts.prepare(source["path"], source["hash"])
        path = self.project.path(session["path"])
        original = path.read_bytes()
        pristine = json.loads(original)
        mutations = [
            ("created_at", None), ("created_at", 1), ("created_at", " "),
            ("created_at", "x" * 101), ("warnings", None), ("warnings", "text"),
            ("warnings", [None]), ("warnings", ["x" * 2001]),
            ("limitations", {}), ("limitations", [""] * 101),
            ("source", {**pristine["source"], "characters": 8 * 1024 * 1024 + 1}),
            ("source", {**pristine["source"], "characters": True}),
            ("source", {**pristine["source"], "lines": pristine["source"]["characters"] + 1}),
            ("source", {**pristine["source"], "path": []}),
        ]
        try:
            for key, value in mutations:
                with self.subTest(key=key, value=value):
                    damaged = copy.deepcopy(pristine)
                    damaged[key] = value
                    path.write_bytes(json_bytes(damaged))
                    with self.assertRaises(VibeNovelError):
                        self.manuscripts.read(session["id"])
                    catalog = self.manuscripts.list()
                    self.assertEqual(catalog["manuscripts"], [])
                    self.assertEqual(catalog["omitted"][0]["id"], session["id"])
        finally:
            path.write_bytes(original)
        self.assertFalse(self.manuscripts.read(session["id"])["stale"])

    def test_accept_requires_successful_original_candidate_and_preserves_history(self):
        source = self.source()
        session = self.manuscripts.prepare(source["path"], source["hash"])
        section = session["sections"][1]
        before = self.project.fingerprint()
        candidate = self.candidate(session, section, "第一章 走廊\r\n作者决定保留的修改。\r\n")
        result = self.manuscripts.accept(session["id"], section["id"], section["current_hash"], candidate["path"], candidate["hash"])
        self.assertEqual(result["sections"][1]["current_hash"], candidate["hash"])
        self.assertTrue(result["sections"][1]["accepted"])
        history = self.documents.history(section["path"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["hash"], section["original_hash"])
        self.assertEqual(before, self.project.fingerprint())
        self.assertEqual(self.documents.read(source["path"])["hash"], source["hash"])
        with self.assertRaises(ConflictError):
            self.manuscripts.accept(session["id"], section["id"], section["current_hash"], candidate["path"], candidate["hash"])

    def test_edited_or_restored_candidate_cannot_be_accepted_by_hash_alone(self):
        source = self.source()
        session = self.manuscripts.prepare(source["path"], source["hash"])
        section = session["sections"][0]
        candidate = self.candidate(session, section)
        saved = self.documents.save(candidate["path"], "手工改动", candidate["hash"])
        self.documents.save(candidate["path"], candidate["content"], saved["hash"], "AI_SUGGESTED")
        with self.assertRaisesRegex(ConflictError, "编辑、回退"):
            self.manuscripts.accept(session["id"], section["id"], section["current_hash"], candidate["path"], candidate["hash"])
        self.assertEqual(self.documents.read(section["path"])["hash"], section["current_hash"])

    def test_candidate_from_wrong_source_or_failed_job_cannot_be_accepted(self):
        source = self.source()
        session = self.manuscripts.prepare(source["path"], source["hash"])
        section = session["sections"][0]
        candidate = self.candidate(session, section, state="failed")
        with self.assertRaises(ConflictError):
            self.manuscripts.accept(session["id"], section["id"], section["current_hash"], candidate["path"], candidate["hash"])
        with self.assertRaises(VibeNovelError):
            self.manuscripts.accept(session["id"], "other", section["current_hash"], candidate["path"], candidate["hash"])

    def test_candidate_editorial_metadata_and_runtime_dependencies_are_checked(self):
        source = self.source()
        session = self.manuscripts.prepare(source["path"], source["hash"])
        section = session["sections"][0]
        for context in (None, [], "bad", {"runtime_sources": None}, {"runtime_sources": {"config/other.json": None}}):
            with self.subTest(context=context):
                candidate = self.candidate(session, section, editorial_context=context)
                with self.assertRaises(VibeNovelError):
                    self.manuscripts.accept(session["id"], section["id"], section["current_hash"], candidate["path"], candidate["hash"])
                self.assertEqual(self.documents.read(section["path"])["hash"], section["current_hash"])
        candidate = self.candidate(session, section, editorial_context={
            "runtime_sources": {".runtime/style-baseline.json": None, ".runtime/writing-toolkit.json": None}})
        self.project.path(".runtime/writing-toolkit.json").write_bytes(b"{}")
        with self.assertRaises(ConflictError):
            self.manuscripts.accept(session["id"], section["id"], section["current_hash"], candidate["path"], candidate["hash"])
        with self.assertRaises(VibeNovelError):
            self.manuscripts.accept(session["id"], section["id"], section["current_hash"], 123, candidate["hash"])

    def test_export_separates_revised_chapters_without_modifying_working_copy(self):
        source = self.source()
        session = self.manuscripts.prepare(source["path"], source["hash"])
        section = session["sections"][1]
        saved = self.documents.save(section["path"], "第一章 走廊\n修改后的末句。", section["current_hash"])
        exported = self.manuscripts.export(session["id"])
        self.assertIn("修改后的末句。\n# 第二章 门后", exported["content"])
        self.assertEqual(self.documents.read(section["path"])["hash"], saved["hash"])
        self.assertEqual(self.documents.read(source["path"])["hash"], source["hash"])

    def test_limits_reject_partial_or_oversized_plan(self):
        source = self.source("无章标题\n" + "正文\n" * 4)
        with patch("vibenovel.core.manuscripts.MAX_SECTION_CHARACTERS", 5), self.assertRaises(VibeNovelError):
            self.manuscripts.prepare(source["path"], source["hash"])
        self.assertEqual(self.manuscripts.list()["manuscripts"], [])
        with patch("vibenovel.core.manuscripts.MAX_SECTIONS", 2), self.assertRaises(VibeNovelError):
            self.manuscripts.preview(source["path"], source["hash"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
