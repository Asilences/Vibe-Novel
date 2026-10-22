"""Saved chapter manager metadata must use text, never encoded byte sizes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vibenovel.core.documents import MAX_DOCUMENT_BYTES, DocumentStore
from vibenovel.core.files import digest
from vibenovel.core.project import ProjectStore


class ChapterSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = ProjectStore(Path(temporary.name).resolve()).init("chapter-metadata", genre="general")
        self.store = DocumentStore(self.project)

    def test_saved_unicode_count_and_title_without_exposing_body(self):
        content = "# 第三章 蓝色球衣\r\n\r\n门将笑了。 ⚽\U0001f600\t\n"
        saved = self.store.save("drafts/ch003.md", content, None)
        item = self.store.list_documents()[0]
        self.assertEqual(item["title"], "第三章 蓝色球衣")
        self.assertEqual(item["character_count"], sum(not char.isspace() for char in content))
        self.assertEqual(item["summary_status"], "available")
        self.assertEqual(item["hash"], saved["hash"])
        self.assertEqual(item["bytes"], len(content.encode("utf-8")))
        self.assertNotEqual(item["character_count"], item["bytes"])
        self.assertNotIn("content", item)

    def test_external_formal_chapters_stay_readonly_and_unchanged(self):
        raw = "\ufeff第一章 开场\n\n正式正文".encode("utf-8")
        path = self.project.path("chapters/ch001.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        item = self.store.list_documents()[0]
        self.assertTrue(item["readonly"])
        self.assertEqual(item["kind"], "canonical")
        self.assertEqual(item["title"], "第一章 开场")
        self.assertEqual(item["hash"], digest(raw))
        self.assertEqual(path.read_bytes(), raw)

    def test_blank_and_no_title_files_do_not_use_first_body_paragraph_as_title(self):
        self.store.save("drafts/blank.md", " \r\n\t", None)
        self.store.save("drafts/no-heading.md", "这是第一段正文。\n# 后来的小节", None)
        items = {item["path"]: item for item in self.store.list_documents()}
        self.assertEqual(items["drafts/blank.md"]["character_count"], 0)
        self.assertIsNone(items["drafts/blank.md"]["title"])
        self.assertIsNone(items["drafts/no-heading.md"]["title"])

    def test_invalid_or_oversized_external_drafts_report_unknown_and_other_documents_omit_statistics(self):
        self.project.path("drafts/bad.md").write_bytes(b"\xff")
        self.project.path("drafts/large.md").write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
        self.store.save("reviews/report.md", "# 不是章节", None)
        items = {item["path"]: item for item in self.store.list_documents()}
        for path, status in [("drafts/bad.md", "invalid_utf8"), ("drafts/large.md", "too_large")]:
            self.assertEqual(items[path]["summary_status"], status)
            self.assertIsNone(items[path]["character_count"])
            self.assertIsNone(items[path]["title"])
        self.assertNotIn("character_count", items["reviews/report.md"])


if __name__ == "__main__":
    unittest.main()
