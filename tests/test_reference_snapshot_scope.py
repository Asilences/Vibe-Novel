"""Long-study validation must amortize reads without retaining stale trust."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibenovel.core.files import digest, json_bytes
from vibenovel.core.locking import ConflictError
from vibenovel.core.reference_books import ReferenceBookStore


class SnapshotScopeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ReferenceBookStore(Path(directory.name))
        self.book = self.store.import_book('合成研究.txt',
            '第一章 清晨\n甲去桥边。\n第二章 中午\n乙在河岸。\n第三章 黄昏\n甲乙离开。'.encode('utf-8'))
        self.book = self.store.update_policy(self.book['book_id'], expected_hash=self.book['hash'], outbound='excerpts')
        self.snapshots = [self.store.snapshot(self.book['book_id'], self.book['edition_id'], [row['chapter_id']])
                          for row in self.book['chapters']]

    def test_one_source_context_per_operation_and_fresh_next_operation(self):
        with patch.object(self.store, '_snapshot_context', wraps=self.store._snapshot_context) as context:
            self.assertTrue(self.store.validate_snapshots(self.snapshots))
            self.assertEqual(context.call_count, 1)
            self.assertTrue(self.store.validate_snapshots(self.snapshots))
            self.assertEqual(context.call_count, 2)

    def test_original_changed_between_operations_is_rejected(self):
        self.store.validate_snapshots(self.snapshots)
        relative = '%s/editions/%s/%s' % (self.book['book_id'], self.book['edition_id'], self.book['original_path'])
        original = self.store.path(relative)
        original.write_bytes(original.read_bytes() + b'changed')
        with self.assertRaises(ConflictError):
            self.store.validate_snapshots(self.snapshots)

    def test_withdrawal_and_self_consistent_forgery_are_rejected(self):
        forged = copy.deepcopy(self.snapshots)
        forged[0]['text'] += '伪造'
        forged[0]['hash'] = digest(json_bytes({k: v for k, v in forged[0].items() if k != 'hash'}))
        with self.assertRaises(ConflictError):
            self.store.validate_snapshots(forged)
        self.store.update_policy(self.book['book_id'], expected_hash=self.book['hash'], outbound='excerpts', withdrawn=True)
        with self.assertRaises(ConflictError):
            self.store.validate_snapshots(self.snapshots)


if __name__ == '__main__':
    unittest.main()
