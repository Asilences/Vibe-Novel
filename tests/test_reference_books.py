"""Offline reference corpus fixtures: no real novels, credentials or model calls."""
from __future__ import annotations

import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from vibenovel.core.files import digest, json_bytes, read_json
from vibenovel.core.locking import ConflictError
from vibenovel.core.reference_books import ReferenceBookStore, _xml
from vibenovel.errors import VibeNovelError


def epub(documents, *, toc=True):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        items = "".join('<item id="c%d" href="c%d.xhtml" media-type="application/xhtml+xml"/>' % (i, i) for i in range(len(documents)))
        if toc:
            items += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            links = "".join('<li><a href="c%d.xhtml#section">目录 %d</a></li>' % (i, i + 1) for i in range(len(documents)))
            archive.writestr("OEBPS/nav.xhtml", '<html><body><nav><ol>' + links + '</ol></nav></body></html>')
        spine = "".join('<itemref idref="c%d"/>' % i for i in range(len(documents)))
        archive.writestr("OEBPS/book.opf", '<package><metadata><title>人工参考书</title><creator>测试作者</creator></metadata><manifest>' + items + '</manifest><spine>' + spine + '</spine></package>')
        for i, text in enumerate(documents):
            archive.writestr("OEBPS/c%d.xhtml" % i, '<html><head><title>不可当正文</title><style>hidden</style></head><body><section id="section">' + text + '</section></body></html>')
    return data.getvalue()


class ReferenceBookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.store = ReferenceBookStore(self.workspace)

    def import_text(self, text="第一卷 起点\r\n第一章 桥\r\n河水上涨😀。\r\n第二章 绕路\r\n众人寻找新桥。", **kw):
        return self.store.import_book("人工小说.txt", text.encode("utf-8"), title="人工小说", **kw)

    def test_original_bytes_chapter_sources_and_idempotence(self):
        raw = "第一章 桥\r\n河水上涨😀。\r\n第二章 绕路\r\n众人寻找新桥。".encode("utf-8-sig")
        book = self.store.import_book("人工小说.txt", raw, title="人工小说")
        original = self.store.path("%s/editions/%s/%s" % (book["book_id"], book["edition_id"], book["original_path"]))
        self.assertEqual(original.read_bytes(), raw)
        values = []
        for item in book["chapters"]:
            chapter = self.store.chapter(book["book_id"], item["chapter_id"])
            values.append(chapter["text"])
            for span in chapter["source"]["byte_spans"]:
                self.assertEqual(raw[span["start_byte"]:span["end_byte"]].decode(span["encoding"]), chapter["text"][span["start_char"]:span["end_char"]])
        self.assertEqual("".join(values), raw.decode("utf-8-sig"))
        again = self.store.import_book("人工小说.txt", raw, title="人工小说")
        self.assertEqual(book["hash"], again["hash"])
        self.assertEqual(len(self.store.list_books()), 1)
        self.assertTrue(self.store.verify(book["book_id"])["ok"])

    def test_epub_spine_navigation_nodes_and_absent_toc(self):
        raw = epub(['<h1>第一章 晨</h1><p>小明出门。</p>', '<h1>第二章 暮</h1><p>小明归来。</p>'])
        book = self.store.import_book("测试.epub", raw)
        self.assertEqual(book["title"], "人工参考书")
        self.assertEqual(len(book["toc"]), 2)
        self.assertEqual([chapter["title"] for chapter in book["chapters"]], ["第一章 晨", "第二章 暮"])
        chapter = self.store.chapter(book["book_id"], book["chapters"][0]["chapter_id"])
        self.assertIn("小明出门。", chapter["text"])
        self.assertNotIn("不可当正文", chapter["text"])
        self.assertEqual(chapter["source"]["resource"], "OEBPS/c0.xhtml")
        self.assertTrue(chapter["source"]["nodes"])
        no_toc = self.store.import_book("无目录.epub", epub(['<p>没有目录也保留全部文本。</p>'], toc=False), title="无目录")
        self.assertIn("missing_toc", [item["code"] for item in no_toc["issues"]])
        self.assertEqual(no_toc["completeness"], "not_verified")

    def test_repeated_titles_are_not_deleted_and_actual_duplicates_are_candidates(self):
        book = self.import_text("第一章 开端\n相同文本。\n第二章 转折\n不同文本。\n第一章 开端\n相同文本。\n")
        self.assertEqual(book["total_chapters"], 3)
        self.assertEqual(book["counts"]["duplicate_candidates"], 1)
        self.assertEqual(book["chapters"][2]["duplicate_of"], book["chapters"][0]["chapter_id"])
        self.assertEqual(book["chapters"][2]["order"], 3)

    def test_bad_encoding_preserves_original_and_explicit_gap_mapping(self):
        raw = "第一章 夜\n字节前。".encode("utf-8") + b"\xff" + "字节后。\n第二章 晨\n继续。".encode("utf-8")
        book = self.store.import_book("坏编码.txt", raw, encoding="utf-8")
        self.assertEqual(book["counts"]["encoding_gaps"], 1)
        gap = next(item for item in book["issues"] if item["code"] == "encoding_gap")
        self.assertEqual(raw[gap["start_byte"]:gap["end_byte"]], b"\xff")
        chapter = self.store.chapter(book["book_id"], book["chapters"][0]["chapter_id"])
        self.assertTrue(chapter["incomplete"])
        self.assertNotIn("\ufffd", chapter["text"])
        self.assertEqual(len(chapter["source"]["byte_spans"]), 2)
        self.assertTrue(self.store.verify(book["book_id"])["ok"])
        self.assertEqual(book["encoding_status"], "provisional_with_gaps")

    def test_technical_volumes_and_oversized_chapter_blocks_remain_contiguous(self):
        text = "第一卷 原卷\n第一章 长章\n" + "甲乙丙\n" * 80 + "第二章 后续\n" + "后果\n" * 50
        with patch("vibenovel.core.reference_books.MAX_SHARD_BYTES", 128), patch("vibenovel.core.reference_books.TARGET_VOLUME_BYTES", 90):
            book = self.import_text(text)
        self.assertGreater(book["counts"]["technical_volumes"], 2)
        self.assertTrue(all(volume["bytes"] <= 128 for volume in book["technical_volumes"]))
        reconstructed = "".join(self.store.chapter(book["book_id"], item["chapter_id"])["text"] for item in book["chapters"])
        self.assertEqual(reconstructed, text)
        self.assertTrue(all(item["volume_title"] == "第一卷 原卷" for item in book["chapters"]))

    def test_policy_revocation_A_B_A_does_not_revive_snapshot(self):
        book = self.import_text()
        ids = [book["chapters"][1]["chapter_id"]]
        with self.assertRaises(ConflictError):
            self.store.snapshot(book["book_id"], book["edition_id"], ids)
        book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        snapshot = self.store.snapshot(book["book_id"], book["edition_id"], ids)
        self.assertTrue(self.store.validate_snapshot(snapshot))
        book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="local", withdrawn=True)
        book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        with self.assertRaises(ConflictError):
            self.store.validate_snapshot(snapshot)
        self.assertGreater(book["authorization_revision"], snapshot["authorization_revision"])

    def test_snapshot_no_silent_truncation_and_modified_text_rejected(self):
        book = self.import_text()
        book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        ids = [book["chapters"][1]["chapter_id"]]
        with self.assertRaisesRegex(VibeNovelError, "未静默截断"):
            self.store.snapshot(book["book_id"], book["edition_id"], ids, max_characters=1)
        snapshot = self.store.snapshot(book["book_id"], book["edition_id"], ids)
        changed = copy.deepcopy(snapshot)
        changed["text"] += "伪造"
        with self.assertRaises(ConflictError):
            self.store.validate_snapshot(changed)
        first = book["chapters"][1]["blocks"][0]
        path = self.store.path("%s/editions/%s/%s" % (book["book_id"], book["edition_id"], first["path"]))
        path.write_bytes(path.read_bytes() + b"modified")
        self.assertFalse(self.store.verify(book["book_id"])["ok"])
        with self.assertRaises(ConflictError):
            self.store.validate_snapshot(snapshot)

    def test_publish_interruption_recovers_catalogue_and_orphan_edition(self):
        from vibenovel.core import reference_books
        original = reference_books.atomic_write
        def fail_catalog(path, data, **kwargs):
            if path.name == "catalog.json":
                raise OSError("fixture interruption")
            return original(path, data, **kwargs)
        with patch("vibenovel.core.reference_books.atomic_write", side_effect=fail_catalog):
            with self.assertRaises(OSError):
                self.import_text()
        recovered = ReferenceBookStore(self.workspace).list_books()
        self.assertEqual(len(recovered), 1)
        self.assertTrue(self.store.verify(recovered[0]["book_id"])["ok"])
        # Second fixture: stop after edition rename but before journal preparation.
        other_workspace = self.workspace / "orphan"
        other_workspace.mkdir()
        other = ReferenceBookStore(other_workspace)
        with patch.object(other, "_publish", side_effect=OSError("stop before intent")):
            with self.assertRaises(OSError):
                other.import_book("orphan.txt", "第一章 测试\n保留。".encode())
        self.assertEqual(other.list_books(), [])
        book = other.import_book("orphan.txt", "第一章 测试\n保留。".encode())
        self.assertTrue(other.verify(book["book_id"])["ok"])

    def test_search_reports_full_local_coverage_and_character_ranges(self):
        book = self.import_text()
        search = self.store.search(book["book_id"], "新桥")
        self.assertEqual(search["coverage"]["scanned_chapters"], book["total_chapters"])
        self.assertFalse(search["coverage"]["analysis"])
        self.assertEqual(len(search["results"]), 1)
        result = search["results"][0]
        chapter = self.store.chapter(book["book_id"], result["chapter_id"])
        self.assertEqual(result["text"], chapter["text"][result["start_char"]:result["end_char"]])

    def test_path_boundaries_and_zip_traversal(self):
        for name in ("../book.txt", "D:\\book.txt", "con.txt"):
            with self.assertRaises(VibeNovelError):
                self.store.import_book(name, b"test")
        with self.assertRaises(VibeNovelError):
            self.store.detail("../unsafe")
        stream = io.BytesIO(epub(["<p>test</p>"]))
        with zipfile.ZipFile(stream, "a") as archive:
            archive.writestr("../escape.txt", "bad")
        with self.assertRaisesRegex(VibeNovelError, "越界"):
            self.store.import_book("unsafe.epub", stream.getvalue())

    def test_external_doctype_is_offline_but_internal_entities_are_rejected(self):
        self.assertEqual(_xml(b'<!DOCTYPE ncx SYSTEM "https://unreachable.invalid/ncx.dtd"><ncx/>').tag, "ncx")
        with self.assertRaises(VibeNovelError):
            _xml(b'<!DOCTYPE ncx [<!ENTITY x "expanded">]><ncx>&x;</ncx>')

    def test_long_chapter_exact_ranges_and_partial_gap_gate(self):
        raw = ("第一章 长章\n" + "未解码前的内容。" * 100).encode() + b"\xff" + ("后面的内容。" * 100).encode()
        book = self.store.import_book("区间.txt", raw, encoding="utf-8")
        book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        chapter_id = book["chapters"][0]["chapter_id"]
        with self.assertRaisesRegex(ConflictError, "缺口"):
            self.store.snapshot(book["book_id"], book["edition_id"], [chapter_id])
        selected = [{"chapter_id": chapter_id, "start_char": 2, "end_char": 100}]
        snapshot = self.store.snapshot_ranges(book["book_id"], book["edition_id"], selected)
        self.assertEqual(snapshot["refs"][0]["start_char"], 2)
        self.assertTrue(self.store.validate_snapshot(snapshot))
        partial = self.store.snapshot(book["book_id"], book["edition_id"], [chapter_id], allow_partial=True)
        self.assertTrue(partial["refs"][0]["incomplete"])
        self.assertTrue(partial["refs"][0]["gaps"])
        self.assertTrue(self.store.validate_snapshot(partial))
        with self.assertRaisesRegex(VibeNovelError, "重叠"):
            self.store.snapshot_ranges(book["book_id"], book["edition_id"], selected + selected)

    def test_quota_failure_never_publishes_partial_book(self):
        with patch("vibenovel.core.reference_books.MAX_CORPUS_BYTES", 10):
            with self.assertRaisesRegex(VibeNovelError, "配额"):
                self.import_text()
        self.assertEqual(self.store.list_books(), [])

    def test_isolated_nul_is_preserved_with_exact_byte_location_and_external_gate(self):
        raw = "第一章 测试\n前面\x00后面。".encode("gb18030")
        book = self.store.import_book("孤立控制符.txt", raw, encoding="gb18030")
        self.assertEqual(book["counts"]["control_characters"], 1)
        issue = next(item for item in book["issues"] if item["code"] == "control_character")
        self.assertEqual(raw[issue["start_byte"]:issue["end_byte"]], b"\x00")
        chapter_id = book["chapters"][0]["chapter_id"]
        chapter = self.store.chapter(book["book_id"], chapter_id)
        self.assertIn("\x00", chapter["text"])
        self.assertFalse(chapter["not_fully_decoded"])
        self.assertTrue(chapter["needs_review"])
        book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        with self.assertRaisesRegex(ConflictError, "控制字符"):
            self.store.snapshot(book["book_id"], book["edition_id"], [chapter_id])
        snapshot = self.store.snapshot(book["book_id"], book["edition_id"], [chapter_id], allow_partial=True)
        self.assertIn("\x00", snapshot["text"])
        self.assertEqual(len(snapshot["refs"][0]["control_characters"]), 1)
        self.assertTrue(self.store.validate_snapshot(snapshot))

    def test_changed_original_invalidates_snapshot_even_if_derived_text_is_intact(self):
        book = self.import_text()
        book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        snapshot = self.store.snapshot(book["book_id"], book["edition_id"], [book["chapters"][0]["chapter_id"]])
        original = self.store.path("%s/editions/%s/%s" % (book["book_id"], book["edition_id"], book["original_path"]))
        original.write_bytes(original.read_bytes() + b"changed original only")
        with self.assertRaisesRegex(ConflictError, "被修改"):
            self.store.validate_snapshot(snapshot)

    def test_bulk_snapshot_scope_reuses_hashing_but_never_survives_later_change(self):
        book = self.import_text("第一章 起\n第一段。\n第二章 续\n第二段。")
        book = self.store.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        groups = [[{"chapter_id": chapter["chapter_id"], "start_char": 0, "end_char": chapter["characters"]}]
                  for chapter in book["chapters"]]
        from vibenovel.core import reference_books
        original_digest = reference_books.file_digest
        hashed = []
        def counted(path):
            hashed.append(str(path))
            return original_digest(path)
        with patch("vibenovel.core.reference_books.file_digest", side_effect=counted):
            snapshots = self.store.snapshots_ranges(book["book_id"], book["edition_id"], groups)
        original_path = str(self.store.path("%s/editions/%s/%s" % (book["book_id"], book["edition_id"], book["original_path"])))
        self.assertEqual(hashed.count(original_path), 1)
        shard_path = str(self.store.path("%s/editions/%s/%s" % (book["book_id"], book["edition_id"], book["chapters"][0]["blocks"][0]["path"])))
        self.assertEqual(hashed.count(shard_path), 1)
        for snapshot, group in zip(snapshots, groups):
            self.assertEqual(snapshot, self.store.snapshot_ranges(book["book_id"], book["edition_id"], group))
        with self.assertRaisesRegex(VibeNovelError, "重叠"):
            self.store.snapshots_ranges(book["book_id"], book["edition_id"], [groups[0], groups[0]])
        Path(shard_path).write_bytes(Path(shard_path).read_bytes() + b"modified later")
        with self.assertRaises(ConflictError):
            self.store.snapshots_ranges(book["book_id"], book["edition_id"], groups)

    def test_bounded_body_prefix_chapters_preserve_original_titles_and_text(self):
        text = "书目前言。\r\n正文 第1章 高寒\r\n人物出场。\r\n正文\t第2章 第二天\r\n人物继续行动。\r\n正文 第3章 选择\r\n结尾。"
        book = self.import_text(text)
        self.assertEqual([chapter["title"] for chapter in book["chapters"]],
                         ["人工小说", "正文 第1章 高寒", "正文\t第2章 第二天", "正文 第3章 选择"])
        restored = "".join(self.store.chapter(book["book_id"], chapter["chapter_id"])["text"] for chapter in book["chapters"])
        self.assertEqual(restored, text)
        # Narrative mentions and unsupported arbitrary prefixes are not chapter titles.
        other = self.store.import_book("不猜标题.txt", "他说正文 第1章是故事。\n网页更新 第2章 不是已支持的标题。".encode())
        self.assertEqual(other["total_chapters"], 1)
        self.assertEqual(book["parser_version"], "reference-books-v3")


if __name__ == "__main__":
    unittest.main()
