"""Exercise corruption and real failure boundaries, not only happy paths."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.config import DEFAULT_CONFIG, load_config
from vibenovel.core.export import export_markdown
from vibenovel.core.files import cleanup_stage, json_bytes, read_json
from vibenovel.core.imports import import_outline, verify_project
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


class StorageIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.store = ProjectStore(self.workspace)
        self.project = self.store.init("story", genre="campus")
        self.source = self.workspace / "outline.md"
        self.raw = "# 原纲\r\n\r\n主人公走出学校。\r\n".encode("utf-8")
        self.source.write_bytes(self.raw)

    def bundle(self):
        import_outline(self.project, self.source)
        return self.project.path("imports/original/" + hashlib.sha256(self.raw).hexdigest())

    def test_init_publish_failure_leaves_no_partial_project_and_keeps_other_novel(self):
        before = self.project.fingerprint()
        with patch("vibenovel.core.project.os.rename", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                self.store.init("failed", genre="football")
        self.assertFalse((self.workspace / "novels/failed").exists())
        self.assertFalse(list((self.workspace / "novels").glob(".init-*")))
        self.assertEqual(self.project.fingerprint(), before)

    def test_import_publish_failure_cleans_stage_and_does_not_change_canon(self):
        before = self.project.fingerprint()
        with patch("vibenovel.core.imports.os.rename", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                import_outline(self.project, self.source)
        self.assertEqual(list(self.project.path("imports/original").iterdir()), [])
        self.assertEqual(self.project.fingerprint(), before)
        self.assertFalse(import_outline(self.project, self.source)["reused"])

    def test_import_interrupted_write_cleans_stage(self):
        with patch("vibenovel.core.imports.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                import_outline(self.project, self.source)
        self.assertEqual(list(self.project.path("imports/original").iterdir()), [])

    def test_same_bytes_with_different_encoding_cannot_silently_replace_extraction(self):
        bundle = self.bundle()
        before = {p.name: p.read_bytes() for p in bundle.iterdir()}
        with self.assertRaises(VibeNovelError):
            import_outline(self.project, self.source, encoding="gb18030")
        self.assertEqual({p.name: p.read_bytes() for p in bundle.iterdir()}, before)

    def test_tampered_extraction_metadata_and_refs_are_detected_without_repair(self):
        bundle = self.bundle()
        extracted = bundle / "extracted.md"
        original = extracted.read_bytes()
        extracted.write_bytes(b"changed")
        self.assertFalse(verify_project(self.project)["ok"])
        self.assertEqual(extracted.read_bytes(), b"changed")
        extracted.write_bytes(original)
        metadata_file = bundle / "metadata.json"
        metadata = json.loads(metadata_file.read_bytes())
        for field, value in (("novel_id", "another"), ("canonical", True), ("trust", "trusted"),
                             ("source_refs", []), ("original_file", "../../escape.md")):
            altered = dict(metadata, **{field: value})
            metadata_file.write_bytes(json_bytes(altered))
            with self.subTest(field=field):
                self.assertFalse(verify_project(self.project)["ok"])
        metadata_file.write_bytes(json_bytes(metadata))
        self.assertTrue(verify_project(self.project)["ok"])

    def test_corrupt_existing_bundle_is_not_overwritten_on_retry(self):
        bundle = self.bundle()
        original = bundle / "original.md"
        original.write_bytes(b"corrupted")
        with self.assertRaises(VibeNovelError):
            import_outline(self.project, self.source)
        self.assertEqual(original.read_bytes(), b"corrupted")

    def test_leftover_stage_and_unexpected_bundle_files_are_reported(self):
        stage = self.project.path("imports/original/.import-interrupted")
        stage.mkdir()
        (stage / "partial").write_bytes(b"partial")
        result = verify_project(self.project)
        self.assertFalse(result["ok"])
        self.assertTrue(stage.exists())
        (stage / "partial").unlink()
        stage.rmdir()
        (self.bundle() / "unexpected.txt").write_bytes(b"unexpected")
        self.assertFalse(verify_project(self.project)["ok"])

    def test_atomic_export_failure_preserves_previous_output_and_removes_temporary(self):
        chapter = self.project.path("chapters/ch001.md")
        chapter.write_bytes(b"chapter")
        export_markdown(self.project)
        output = self.project.path("exports/story.md")
        before = output.read_bytes()
        with patch("vibenovel.core.files.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                export_markdown(self.project, force=True)
        self.assertEqual(output.read_bytes(), before)
        self.assertFalse(list(output.parent.glob(".write-*")))

    def test_cleanup_refuses_target_outside_expected_temporary_parent(self):
        marker = self.workspace / ".import-do-not-delete"
        marker.mkdir()
        with self.assertRaises(VibeNovelError):
            cleanup_stage(marker, self.project.root, ".import-")
        self.assertTrue(marker.exists())

    def test_json_duplicate_keys_nan_and_oversize_are_rejected(self):
        source = self.workspace / "test.json"
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'[]'):
            source.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(VibeNovelError):
                read_json(source)
        source.write_bytes(b'{"a":1}')
        with self.assertRaises(VibeNovelError):
            read_json(source, max_bytes=3)

    def test_config_rejects_unknown_schema_fields_and_unsafe_automation(self):
        directory = self.workspace / "config"
        directory.mkdir()
        path = directory / "app.json"
        self.assertEqual(load_config(self.workspace), DEFAULT_CONFIG)
        for field, value in (("schema_version", True), ("schema_version", 99), ("unknown", 1)):
            altered = dict(DEFAULT_CONFIG, **{field: value})
            path.write_bytes(json_bytes(altered))
            with self.subTest(field=field, value=value), self.assertRaises(VibeNovelError):
                load_config(self.workspace)
        altered = dict(DEFAULT_CONFIG, execution=dict(DEFAULT_CONFIG["execution"], mode="api"))
        path.write_bytes(json_bytes(altered))
        with self.assertRaises(VibeNovelError):
            load_config(self.workspace)

    def test_source_size_limit_and_empty_final_chapters_fail_without_outputs(self):
        with self.assertRaises(VibeNovelError):
            import_outline(self.project, self.source, max_bytes=1)
        self.assertEqual(list(self.project.path("imports/original").iterdir()), [])
        self.project.path("chapters/ch001.md").write_bytes(b" \n")
        with self.assertRaises(VibeNovelError):
            export_markdown(self.project)
        self.assertEqual(list(self.project.path("exports").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
