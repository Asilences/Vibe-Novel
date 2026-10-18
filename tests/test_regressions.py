"""Regression cases reproduced during the independent M0 storage review."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibenovel.core.files import json_bytes
from vibenovel.core.imports import import_outline, verify_project
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


CLI_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "vibenovel.py"


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.project = ProjectStore(self.workspace).init("review", genre="adventure")

    def cli(self, *arguments: str, io_encoding: str = "utf-8"):
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = io_encoding
        completed = subprocess.run(
            [sys.executable, str(CLI_SCRIPT), "--workspace", str(self.workspace), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=30,
            check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        self.assertNotIn("Traceback", stdout + stderr)
        return completed.returncode, stdout, stderr

    def bundle(self):
        source = self.workspace / "original.md"
        source.write_text("第一段。\n\n第二段。", encoding="utf-8")
        result = import_outline(self.project, source)
        directory = self.project.path("imports/original/" + result["import_id"])
        return directory, result["metadata"]

    def test_cli_successfully_creates_unicode_title_under_gbk_environment(self):
        title = "原稿 😀"
        code, output, errors = self.cli("init", "unicode", "--genre", "campus", "--title", title,
                                        io_encoding="gbk")
        self.assertEqual(code, 0, errors)
        response = json.loads(output)
        self.assertEqual(response["project"]["title"], title)
        self.assertEqual(ProjectStore(self.workspace).get("unicode").metadata["title"], title)
        code, output, errors = self.cli("status", "unicode", io_encoding="gbk")
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["title"], title)

    def test_deep_json_reports_input_error_instead_of_stack_trace(self):
        config = self.workspace / "config"
        config.mkdir()
        # This fits within the byte cap but exceeds Python's JSON recursion limit.
        (config / "app.json").write_text(
            '{"execution":' + "[" * 2000 + "0" + "]" * 2000 + "}", encoding="utf-8")
        code, output, errors = self.cli("config")
        self.assertEqual(code, 2)
        self.assertTrue(errors.strip())
        self.assertFalse(output.strip())

    def test_nonfinite_and_overflow_numbers_in_metadata_are_rejected(self):
        directory, metadata = self.bundle()
        before = self.project.fingerprint()
        for literal in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(literal=literal):
                malformed = dict(metadata, source_refs=["REPLACE_NUMERIC_LITERAL"])
                text = json.dumps(malformed).replace('"REPLACE_NUMERIC_LITERAL"', literal)
                (directory / "metadata.json").write_text(text, encoding="utf-8")
                code, output, errors = self.cli("verify", "review")
                self.assertEqual(code, 1, errors)
                report = json.loads(output)
                self.assertFalse(report["ok"])
                self.assertEqual(report["checked_imports"], 0)
                self.assertTrue(report["findings"])
        self.assertEqual(self.project.fingerprint(), before)

    def test_unpaired_surrogate_in_project_json_is_rejected(self):
        config = self.project.path("project.json")
        metadata = json.loads(config.read_text(encoding="utf-8"))
        metadata["title"] = "\ud800"
        config.write_text(json.dumps(metadata), encoding="utf-8")
        code, output, errors = self.cli("status", "review", io_encoding="gbk")
        self.assertEqual(code, 2)
        self.assertTrue(errors.strip())
        self.assertFalse(output.strip())

    def test_json_serialization_wraps_nonfinite_and_surrogate_errors(self):
        for malformed in (float("nan"), float("inf"), float("-inf"), "\ud800"):
            with self.subTest(value=ascii(malformed)):
                with self.assertRaises(VibeNovelError):
                    json_bytes({"value": malformed})

    def test_bundle_with_extra_empty_directory_fails_layout_verification(self):
        directory, _ = self.bundle()
        (directory / "unexpected").mkdir()
        report = verify_project(self.project)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checked_imports"], 0)
        self.assertTrue(report["findings"])

    def test_invalid_utf8_final_chapter_fails_verification(self):
        self.project.path("chapters/ch001.md").write_bytes(b"invalid UTF-8: \xff")
        code, output, errors = self.cli("verify", "review")
        self.assertEqual(code, 1, errors)
        self.assertFalse(json.loads(output)["ok"])

    def test_empty_final_chapter_fails_verification(self):
        self.project.path("chapters/ch001.md").write_text(" \n\t\r\n", encoding="utf-8")
        report = verify_project(self.project)
        self.assertFalse(report["ok"])
        self.assertTrue(report["findings"])

    def test_gap_in_final_chapters_fails_verification(self):
        self.project.path("chapters/ch001.md").write_text("第一章。", encoding="utf-8")
        self.project.path("chapters/ch003.md").write_text("第三章。", encoding="utf-8")
        report = verify_project(self.project)
        self.assertFalse(report["ok"])
        self.assertTrue(report["findings"])


if __name__ == "__main__":
    unittest.main()
