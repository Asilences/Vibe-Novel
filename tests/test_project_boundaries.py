"""Workspace isolation and canonical fingerprint contract tests."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


class ProjectBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Windows runners may expose TEMP through an 8.3 alias (RUNNER~1).
        # Compare canonical locations, matching the store's workspace boundary.
        self.workspace = Path(temporary.name).resolve()
        self.store = ProjectStore(self.workspace)

    def test_init_is_isolated_and_never_migrates_v2_canon(self):
        legacy = self.workspace / "chapters" / "ch001.md"
        legacy.parent.mkdir()
        legacy.write_bytes("旧版已定稿正文。".encode("utf-8"))
        original = legacy.read_bytes()
        a = self.store.init("story-a", genre="football", title="甲作品")
        b = self.store.init("story-b", genre="adventure")
        self.assertEqual(a.root, self.workspace / "novels" / "story-a")
        self.assertEqual(b.root, self.workspace / "novels" / "story-b")
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(list(a.path("chapters").iterdir()), [])
        self.assertEqual(list(b.path("chapters").iterdir()), [])
        a.path("chapters/ch001.md").write_text("甲的事实", encoding="utf-8")
        self.assertFalse(b.path("chapters/ch001.md").exists())
        self.assertEqual([p.novel_id for p in self.store.list_projects()], ["story-a", "story-b"])

    def test_existing_project_and_existing_unregistered_directory_are_not_overwritten(self):
        project = self.store.init("story", genre="campus")
        marker = project.path("chapters/ch001.md")
        marker.write_bytes(b"approved content")
        before = project.fingerprint()
        with self.assertRaises(VibeNovelError):
            self.store.init("story", genre="football")
        self.assertEqual(marker.read_bytes(), b"approved content")
        self.assertEqual(self.store.get("story").fingerprint(), before)
        occupied = self.workspace / "novels" / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_bytes(b"existing user file")
        with self.assertRaises(VibeNovelError):
            self.store.init("occupied", genre="football")
        self.assertEqual((occupied / "keep.txt").read_bytes(), b"existing user file")

    def test_invalid_ids_are_rejected_without_creating_projects(self):
        for novel_id in ["", "../escape", "a/b", "a\\b", "A", "小说", "con", "aux",
                         "nul", "com1", "lpt9", "c:drive", "a.", "a ", "a" * 65]:
            with self.subTest(novel_id=novel_id):
                with self.assertRaises(VibeNovelError):
                    self.store.init(novel_id, genre="football")
                with self.assertRaises(VibeNovelError):
                    self.store.get(novel_id)
        self.assertEqual(self.store.list_projects(), [])

    def test_project_paths_reject_escape_absolute_paths_and_windows_aliases(self):
        project = self.store.init("story", genre="football")
        invalid = ["../other", "chapters/../../other", str(self.workspace / "outside"),
                   "C:\\outside.md", "\\\\server\\share\\outside.md", "chapters/aux.md",
                   "chapters/COM1.txt", "chapters/name.", "chapters/name ",
                   "chapters/ch001.md:secret", "chapters/a?.md"]
        for relative in invalid:
            with self.subTest(relative=relative):
                with self.assertRaises(VibeNovelError):
                    project.path(relative)
        self.assertEqual(project.path("chapters/ch001.md"), project.root / "chapters" / "ch001.md")

    def test_fingerprint_changes_for_all_canonical_content_and_deletions(self):
        project = self.store.init("story", genre="football")
        initial = project.fingerprint()
        for relative in ["bible/person.md", "outlines/approved/ch001.md", "chapters/ch001.md",
                         "memory/events/event.json", "memory/states/state.json",
                         "memory/summaries/ch001.md", "memory/threads/thread.json"]:
            with self.subTest(relative=relative):
                path = project.path(relative)
                path.write_bytes(b"canonical state")
                first = project.fingerprint()
                self.assertNotEqual(first, initial)
                path.write_bytes(b"manually edited state")
                self.assertNotEqual(project.fingerprint(), first)
                path.unlink()
                self.assertEqual(project.fingerprint(), initial)
        metadata = project.path("project.json")
        data = json.loads(metadata.read_text(encoding="utf-8"))
        data["title"] = "作者改名"
        metadata.write_text(json.dumps(data), encoding="utf-8")
        self.assertNotEqual(self.store.get("story").fingerprint(), initial)

    def test_fingerprint_includes_relative_names_but_excludes_unapproved_material(self):
        project = self.store.init("story", genre="football")
        original = project.fingerprint()
        for relative in ["drafts/ch001.md", "outlines/source/original.md", "outlines/proposals/new.md",
                         "imports/original/notes.md", "exports/book.md", ".runtime/checkpoint.json"]:
            project.path(relative).write_bytes(b"not canonical")
        self.assertEqual(project.fingerprint(), original)
        first = project.path("bible/a.md")
        first.write_bytes(b"same bytes")
        previous = project.fingerprint()
        first.rename(project.path("bible/b.md"))
        self.assertNotEqual(project.fingerprint(), previous)

    def test_unknown_schema_and_cross_novel_metadata_are_rejected(self):
        project = self.store.init("story", genre="football")
        path = project.path("project.json")
        original = json.loads(path.read_text(encoding="utf-8"))
        for changed in [dict(original, schema_version=99), dict(original, novel_id="another")]:
            with self.subTest(changed=changed):
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(VibeNovelError):
                    self.store.get("story")
        path.write_text('{"schema_version": 1, "schema_version": 99}', encoding="utf-8")
        with self.assertRaises(VibeNovelError):
            self.store.get("story")

    def test_linked_directory_is_rejected_by_path_and_fingerprint(self):
        project = self.store.init("story", genre="adventure")
        outside = self.workspace / "outside"
        outside.mkdir()
        (outside / "fact.md").write_bytes(b"another novel's fact")
        link = project.root / "bible" / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"This environment cannot create directory symlinks: {exc}")
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        with self.assertRaises(VibeNovelError):
            project.path("bible/linked/fact.md")
        with self.assertRaises(VibeNovelError):
            project.fingerprint()
        self.assertEqual((outside / "fact.md").read_bytes(), b"another novel's fact")

    @unittest.skipUnless(os.name == "nt", "Windows junction contract")
    def test_windows_junction_is_rejected_without_reading_outside_content(self):
        project = self.store.init("story", genre="adventure")
        outside = self.workspace / "outside"
        outside.mkdir()
        marker = outside / "fact.md"
        marker.write_bytes(b"outside fact")
        junction = project.root / "bible" / "junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                capture_output=True, check=False)
        if result.returncode:
            self.skipTest("This environment cannot create directory junctions")
        self.addCleanup(lambda: os.rmdir(junction) if os.path.lexists(junction) else None)
        with self.assertRaises(VibeNovelError):
            project.path("bible/junction/fact.md")
        with self.assertRaises(VibeNovelError):
            project.fingerprint()
        self.assertEqual(marker.read_bytes(), b"outside fact")


if __name__ == "__main__":
    unittest.main()
