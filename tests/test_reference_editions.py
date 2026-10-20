import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from vibenovel.core.reference_books import ReferenceBookStore
from vibenovel.core.reference_editions import ReferenceEditionStore
from vibenovel.core.files import json_bytes, read_json
from vibenovel.errors import VibeNovelError


class ReferenceEditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.books = ReferenceBookStore(self.root)
        self.store = ReferenceEditionStore(self.root)
        self.raw = "第一章 出发\n一\x00二😀。\n第二章 归来\n三四。".encode()
        self.book = self.books.import_book("合成.txt", self.raw, title="合成测试")
        self.bid = self.book["book_id"]

    def candidate(self):
        request = self.store.audit(self.bid)["proposal"]
        preview = self.store.preview(self.bid, request)
        return self.store.create(self.bid, request, expected_preview=preview["hash"], idempotency_key="edition-test-000001")

    def publish(self, candidate):
        return self.store.publish(self.bid, candidate["id"], expected_candidate_hash=candidate["hash"], expected_hash=self.books.detail(self.bid)["hash"], make_current=True)

    def test_candidate_is_non_mutating_and_published_originals_retained(self):
        candidate = self.candidate()
        self.assertEqual(self.books.detail(self.bid)["edition_id"], self.book["edition_id"])
        self.assertEqual(candidate["summary"]["remaining_controls"], 0)
        new = self.publish(candidate)
        self.assertNotEqual(new["edition_id"], self.book["edition_id"])
        self.assertEqual(len(new["editions"]), 2)
        self.assertEqual(new["parser_version"], "reference-editions-v1")
        original = self.books.path(f'{self.bid}/editions/{new["edition_id"]}/{new["original_path"]}')
        self.assertEqual(original.read_bytes(), self.raw)
        self.assertEqual(new["counts"]["control_characters"], 0)
        old = self.books.detail(self.bid, self.book["edition_id"])
        self.assertEqual(old["counts"]["control_characters"], 1)
        self.assertTrue(self.books.verify(self.bid)["ok"])
        again = self.publish(candidate)
        self.assertEqual(again["edition_id"], new["edition_id"])
        mapping = self.store.mapping(self.bid, new["edition_id"])
        self.assertIn("deleted", [m["status"] for m in mapping["ranges"]])
        record = next(m for m in mapping["ranges"] if m["status"] == "unchanged")
        exact = self.store.mapping(self.bid, new["edition_id"], chapter_id=record["old_chapter_id"], start_char=record["old_start_char"], end_char=record["old_end_char"])
        self.assertEqual(exact["status"], "exact")

    def test_no_silent_loss_overlap_or_guessing_patch(self):
        request = self.store.audit(self.bid)["proposal"]
        for change in ("loss", "overlap", "patch"):
            broken = copy.deepcopy(request)
            if change == "loss":
                broken["chapters"].pop()
            elif change == "overlap":
                broken["chapters"].append(copy.deepcopy(broken["chapters"][0]))
            else:
                broken["patches"][0]["expected_text"] = "未知"
            with self.assertRaises(VibeNovelError):
                self.store.preview(self.bid, broken)

    def test_split_merge_reorder_isolated_and_exact_mapping(self):
        request = self.store.audit(self.bid)["proposal"]
        first, second = request["chapters"]
        a, b = copy.deepcopy(first), copy.deepcopy(first)
        split = 5
        a["title"] = "改名"
        a["segments"][0]["end_char"] = split
        b["segments"][0]["start_char"] = split
        b["segments"].extend(second["segments"])
        b["kind"] = "ancillary"
        request["chapters"] = [b, a]
        preview = self.store.preview(self.bid, request)
        candidate = self.store.create(self.bid, request, expected_preview=preview["hash"], idempotency_key="edition-reorder-001")
        new = self.publish(candidate)
        self.assertEqual(new["total_chapters"], 2)
        self.assertEqual(new["chapters"][0]["kind"], "ancillary")
        self.assertEqual(new["chapters"][1]["title"], "改名")
        mapping = self.store.mapping(self.bid, new["edition_id"])
        self.assertTrue(any(m["disposition"] == "isolated" for m in mapping["ranges"]))

    def test_unknown_bytes_stay_unresolved_after_curation(self):
        book = self.books.import_book("缺口.txt", b"\xb5\xda\xd2\xbb\xd5\xc2 \xbf\xaa\xca\xbc\n\x81", title="缺口", encoding="gb18030")
        request = self.store.audit(book["book_id"])["proposal"]
        preview = self.store.preview(book["book_id"], request)
        candidate = self.store.create(book["book_id"], request, expected_preview=preview["hash"], idempotency_key="edition-gap-000001")
        new = self.store.publish(book["book_id"], candidate["id"], expected_candidate_hash=candidate["hash"], expected_hash=book["hash"], make_current=True)
        self.assertEqual(new["counts"]["encoding_gaps"], 1)
        self.assertTrue(new["chapters"][0]["needs_review"])

    def test_authorization_aba_and_stale_preview_rejected(self):
        candidate = self.candidate()
        changed = self.books.update_policy(self.bid, expected_hash=self.book["hash"], outbound="excerpts")
        self.books.update_policy(self.bid, expected_hash=changed["hash"], outbound="local")
        with self.assertRaises(VibeNovelError):
            self.publish(candidate)

    def test_orphan_immutable_publish_can_resume_without_overwrite(self):
        candidate = self.candidate()
        with patch.object(self.store.books, "_publish", side_effect=OSError("simulated before catalog journal")):
            with self.assertRaises(OSError):
                self.publish(candidate)
        self.assertEqual(self.books.detail(self.bid)["edition_id"], self.book["edition_id"])
        new = self.publish(candidate)
        self.assertTrue(self.books.verify(self.bid)["ok"])
        self.assertEqual(len(new["editions"]), 2)

    def test_raw_tamper_after_preview_blocks_candidate_publication(self):
        candidate = self.candidate()
        original = self.books.path(f'{self.bid}/editions/{self.book["edition_id"]}/{self.book["original_path"]}')
        original.write_bytes(b"tampered")
        with self.assertRaises(VibeNovelError):
            self.publish(candidate)

    def test_mapping_marks_changed_and_partial_unknown_not_exact(self):
        new = self.publish(self.candidate())
        mapping = self.store.mapping(self.bid, new["edition_id"])
        deleted = next(m for m in mapping["ranges"] if m["status"] == "deleted")
        changed = self.store.mapping(self.bid, new["edition_id"], chapter_id=deleted["old_chapter_id"], start_char=deleted["old_start_char"], end_char=deleted["old_end_char"])
        self.assertEqual(changed["status"], "changed")
        unchanged = next(m for m in mapping["ranges"] if m["status"] == "unchanged")
        unknown = self.store.mapping(self.bid, new["edition_id"], chapter_id=unchanged["old_chapter_id"], start_char=unchanged["old_start_char"], end_char=1000000)
        self.assertEqual(unknown["status"], "ambiguous")
        reverse = self.store.mapping(self.bid, new["edition_id"], chapter_id=unchanged["new_chapter_id"], start_char=unchanged["new_start_char"], end_char=unchanged["new_end_char"], direction="backward")
        self.assertEqual(reverse["status"], "exact")

    def test_heading_rebuild_is_candidate_and_preserves_all_characters(self):
        raw = ("序章\n" + "甲" * 40000 + "\n上 部 面壁者\n" + "乙" * 40000 + "\n中 部 咒语\n" + "丙" * 40000 + "\n下 部 黑暗森林\n" + "丁" * 40000).encode()
        book = self.books.import_book("结构.txt", raw, title="结构测试")
        audit = self.store.audit(book["book_id"])
        self.assertEqual(len(audit["heading_candidates"]), 4)
        request = audit["alternatives"]["headings"]
        preview = self.store.preview(book["book_id"], request)
        self.assertEqual(preview["summary"]["characters"], book["counts"]["characters"])
        self.assertEqual(preview["summary"]["new_chapters"], 4)
        self.assertEqual(self.books.detail(book["book_id"])["edition_id"], book["edition_id"])

    def test_tampering_and_foreign_paths_rejected(self):
        candidate = self.candidate()
        path = self.store._path(self.bid, candidate["id"])
        value = read_json(path)
        value["reason"] = "篡改"
        path.write_bytes(json_bytes(value))
        with self.assertRaises(VibeNovelError):
            self.store.candidate(self.bid, candidate["id"])
        with self.assertRaises(VibeNovelError):
            self.store.candidate(self.bid, "../../secret")


if __name__ == "__main__":
    unittest.main()
