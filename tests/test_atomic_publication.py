"""Transient Windows sharing failures may retry disk publication, never work."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from vibenovel.core.files import atomic_write

class PublicationTests(unittest.TestCase):
    def test_windows_sharing_retry_keeps_old_file_until_complete_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"record.json";target.write_bytes(b"old")
            import os
            replace=os.replace;calls=[]
            def transient(source,destination):
                calls.append(1)
                if len(calls)<3:
                    self.assertEqual(target.read_bytes(),b"old")
                    error=PermissionError("sharing");error.winerror=32;raise error
                return replace(source,destination)
            with patch("vibenovel.core.files.os.replace",side_effect=transient),patch("vibenovel.core.files.time.sleep"):
                atomic_write(target,b"complete",overwrite=True)
            self.assertEqual(target.read_bytes(),b"complete");self.assertEqual(len(calls),3)
            self.assertEqual(list(Path(folder).glob(".write-*")),[])

    def test_permanent_denial_preserves_old_file_and_has_bounded_attempts(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"record.json";target.write_bytes(b"old")
            error=PermissionError("denied");error.winerror=5
            with patch("vibenovel.core.files.os.replace",side_effect=error) as replace,patch("vibenovel.core.files.time.sleep"):
                with self.assertRaises(PermissionError):atomic_write(target,b"new",overwrite=True)
                self.assertEqual(replace.call_count,6)
            self.assertEqual(target.read_bytes(),b"old")
            self.assertEqual(list(Path(folder).glob(".write-*")),[])

    def test_other_permission_failure_does_not_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"record.json";target.write_bytes(b"old")
            with patch("vibenovel.core.files.os.replace",side_effect=PermissionError("not a sharing failure")) as replace:
                with self.assertRaises(PermissionError):atomic_write(target,b"new",overwrite=True)
                self.assertEqual(replace.call_count,1)
            self.assertEqual(target.read_bytes(),b"old")
