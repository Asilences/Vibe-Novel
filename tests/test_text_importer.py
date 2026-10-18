"""Source decoding and provenance tests, independent of provider/model APIs."""
from __future__ import annotations

import codecs
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vibenovel.importers.text as text_importer
from vibenovel.errors import VibeNovelError
from vibenovel.importers import EXTRACTOR_VERSION, parse_text_source


class TextImporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def source(self, raw: bytes, name: str = "原稿.md") -> Path:
        path = self.root / name
        path.write_bytes(raw)
        return path

    def test_preserves_original_and_utf8_text_without_writes(self):
        text = "# 原始大纲\r\n\r\n主角抵达。\r\n下一行😀。\r\n"
        raw = text.encode("utf-8")
        path = self.source(raw, "原始大纲.MD")
        with patch.object(Path, "write_bytes", side_effect=AssertionError("must not write")), \
                patch.object(Path, "write_text", side_effect=AssertionError("must not write")):
            parsed = parse_text_source(path)
        self.assertEqual(parsed.original_bytes, raw)
        self.assertEqual(parsed.text, text)
        self.assertEqual(parsed.encoding, "utf-8")
        self.assertEqual(parsed.source_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(parsed.source_name, "原始大纲.MD")
        self.assertEqual(parsed.kind, "md")
        self.assertEqual(EXTRACTOR_VERSION, "text-v1")
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertEqual(path.read_bytes(), raw)

    def test_paragraph_refs_are_unicode_offsets_with_crlf_and_indentation(self):
        text = "\r\n  第一段😀。\r\n第二行。\r\n \t\r\n\t第二段。\n末行"
        parsed = parse_text_source(self.source(text.encode("utf-8")))
        self.assertEqual(len(parsed.source_refs), 2)
        first, second = parsed.source_refs
        self.assertEqual((first["start_line"], first["end_line"]), (2, 3))
        self.assertEqual((second["start_line"], second["end_line"]), (5, 6))
        self.assertEqual(text[first["char_start"]:first["char_end"]], "  第一段😀。\r\n第二行。")
        self.assertEqual(text[second["char_start"]:second["char_end"]], "\t第二段。\n末行")
        self.assertEqual(first["char_start"], 2)
        self.assertEqual(second["char_end"], len(text))

    def test_all_python_line_boundaries_preserve_paragraph_spans(self):
        for boundary in ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e",
                         "\x85", "\u2028", "\u2029"):
            with self.subTest(boundary=ascii(boundary)):
                text = "第一行😀" + boundary + "第二行" + boundary * 2 + "第三行" + boundary
                parsed = parse_text_source(self.source(text.encode("utf-8")))
                self.assertEqual(parsed.text, text)
                self.assertEqual(len(parsed.source_refs), 2)
                first, last = parsed.source_refs
                self.assertEqual((first["start_line"], first["end_line"]), (1, 2))
                self.assertEqual((last["start_line"], last["end_line"]), (4, 4))
                self.assertEqual(text[first["char_start"]:first["char_end"]],
                                 "第一行😀" + boundary + "第二行")
                self.assertEqual(text[last["char_start"]:last["char_end"]], "第三行")

    def test_mixed_crlf_and_unicode_boundaries_count_lines_consistently(self):
        text = "甲\r\n乙\u2028\u2028丙\r丁\x85戊\n\n己"
        parsed = parse_text_source(self.source(text.encode("utf-8")))
        self.assertEqual([(ref["start_line"], ref["end_line"]) for ref in parsed.source_refs],
                         [(1, 2), (4, 6), (8, 8)])
        self.assertEqual([text[ref["char_start"]:ref["char_end"]] for ref in parsed.source_refs],
                         ["甲\r\n乙", "丙\r丁\x85戊", "己"])

    def test_paragraph_limit_accepts_exact_count_and_rejects_overflow_without_truncation(self):
        self.assertEqual(text_importer.MAX_SOURCE_REFS, 20000)
        with patch.object(text_importer, "MAX_SOURCE_REFS", 2):
            parsed = parse_text_source(self.source(b"first\n\nsecond\n\n"))
            self.assertEqual(len(parsed.source_refs), 2)
            with self.assertRaisesRegex(VibeNovelError, "分文件导入"):
                parse_text_source(self.source(b"first\n\nsecond\n\nthird"))

    def test_paragraph_limit_stops_line_iteration_at_first_overflow(self):
        observed_lines = []
        pattern = text_importer._LINE_PATTERN

        class ObservedPattern:
            def finditer(self, text):
                for match in pattern.finditer(text):
                    observed_lines.append(match.group(0))
                    if len(observed_lines) > 5:
                        raise AssertionError("parser read past the overflowing paragraph")
                    yield match

        path = self.source(b"first\n\nsecond\n\nthird\nremaining\nremaining")
        with patch.object(text_importer, "MAX_SOURCE_REFS", 2), \
                patch.object(text_importer, "_LINE_PATTERN", ObservedPattern()):
            with self.assertRaises(VibeNovelError):
                parse_text_source(path)
        self.assertEqual(observed_lines, ["first\n", "\n", "second\n", "\n", "third\n"])

    def test_utf8_and_utf16_boms_are_detected_without_leaking_into_text(self):
        text = "第一段。\n\n第二段。"
        variants = [
            (codecs.BOM_UTF8 + text.encode("utf-8"), "utf-8-sig"),
            (codecs.BOM_UTF16_LE + text.encode("utf-16-le"), "utf-16"),
            (codecs.BOM_UTF16_BE + text.encode("utf-16-be"), "utf-16"),
        ]
        for raw, expected_encoding in variants:
            with self.subTest(encoding=expected_encoding, raw_prefix=raw[:4]):
                parsed = parse_text_source(self.source(raw, "大纲.TXT"))
                self.assertEqual(parsed.text, text)
                self.assertEqual(parsed.encoding, expected_encoding)
                self.assertEqual(parsed.source_refs[0]["char_start"], 0)
                self.assertEqual(parsed.kind, "txt")

    def test_legacy_encoding_requires_explicit_choice(self):
        text = "作者原纲：荒野冒险。"
        path = self.source(text.encode("gb18030"))
        with self.assertRaises(VibeNovelError):
            parse_text_source(path)
        parsed = parse_text_source(path, encoding="gb18030")
        self.assertEqual(parsed.text, text)
        self.assertEqual(parsed.encoding, "gb18030")

    def test_explicit_utf8_encoding_accepts_bom_and_utf8_sig_accepts_no_bom(self):
        for raw, selected, actual in [
            (codecs.BOM_UTF8 + "大纲".encode(), "utf-8", "utf-8-sig"),
            ("大纲".encode(), "utf-8-sig", "utf-8-sig"),
        ]:
            with self.subTest(selected=selected):
                parsed = parse_text_source(self.source(raw), encoding=selected)
                self.assertEqual(parsed.text, "大纲")
                self.assertEqual(parsed.encoding, actual)

    def test_empty_blank_bom_only_and_binary_inputs_are_rejected(self):
        for raw in [b"", b" \r\n\t\n", codecs.BOM_UTF8,
                    codecs.BOM_UTF16_LE, b"text\x00binary", b"\xff", b"\xe4\xb8"]:
            with self.subTest(raw=raw):
                with self.assertRaises(VibeNovelError):
                    parse_text_source(self.source(raw))

    def test_utf32_and_ambiguous_utf16_are_rejected(self):
        for raw, encoding in [
            (codecs.BOM_UTF32_LE + "大纲".encode("utf-32-le"), None),
            (codecs.BOM_UTF32_BE + "大纲".encode("utf-32-be"), None),
            ("大纲".encode("utf-16-le"), "utf-16"),
        ]:
            with self.subTest(encoding=encoding, raw=raw):
                with self.assertRaises(VibeNovelError):
                    parse_text_source(self.source(raw), encoding=encoding)

    def test_limit_accepts_exact_size_and_rejects_extra_byte(self):
        path = self.source(b"abcd")
        self.assertEqual(parse_text_source(path, max_bytes=4).text, "abcd")
        with self.assertRaises(VibeNovelError):
            parse_text_source(path, max_bytes=3)

    def test_read_is_bounded_even_when_file_is_larger(self):
        path = self.source(b"present")

        class ObservedReader(io.BytesIO):
            requested_sizes = []

            def read(self, size=-1):
                self.requested_sizes.append(size)
                if size < 0:
                    raise AssertionError("unbounded read")
                return super().read(size)

        stream = ObservedReader(b"x" * 100)
        with patch.object(Path, "open", return_value=stream):
            with self.assertRaises(VibeNovelError):
                parse_text_source(path, max_bytes=7)
        self.assertEqual(stream.requested_sizes, [8])

    def test_invalid_parameters_and_nonfiles_raise_project_error(self):
        path = self.source(b"outline")
        for value in [0, -1, True, 3.5, "10", None, 2 ** 100]:
            with self.subTest(max_bytes=value):
                with self.assertRaises(VibeNovelError):
                    parse_text_source(path, max_bytes=value)
        for value in ["gbk", "latin-1", "UTF-8", "utf8", "", 1, []]:
            with self.subTest(encoding=value):
                with self.assertRaises(VibeNovelError):
                    parse_text_source(path, encoding=value)
        for nonfile in [self.root / "missing.md", self.root / "folder.md"]:
            if nonfile.name == "folder.md":
                nonfile.mkdir()
            with self.subTest(path=nonfile):
                with self.assertRaises(VibeNovelError):
                    parse_text_source(nonfile)
        with self.assertRaises(VibeNovelError):
            parse_text_source(None)

    def test_unsupported_extension_is_rejected_before_reading(self):
        path = self.source(b"not a document importer", "outline.docx")
        with patch.object(Path, "open", side_effect=AssertionError("must not read")):
            with self.assertRaises(VibeNovelError):
                parse_text_source(path)

    def test_read_error_is_wrapped_without_replacement_decode(self):
        path = self.source(b"present")
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(VibeNovelError):
                parse_text_source(path)
        with self.assertRaises(VibeNovelError):
            parse_text_source(self.source(b"valid prefix\xffinvalid"))


if __name__ == "__main__":
    unittest.main()
