"""Revision manuscripts use only an explicit corpus, never implicit canon."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.analysis import AnalysisStore
from vibenovel.core.documents import ConflictError, DocumentStore
from vibenovel.core.files import digest
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


REPEATED = "渔民沿着河岸寻找还能使用的木船，把散落的绳索逐一收紧。远处有人举着灯，喊他们先把伤员送到安全的坡地，随后再回去搬运余下的粮食。"


class ExplicitManuscriptAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = ProjectStore(Path(temporary.name)).init("revision", genre="adventure")
        self.documents = DocumentStore(self.project)
        self.analysis = AnalysisStore(self.project)

    def draft(self, name, content):
        return self.documents.save("drafts/" + name + ".md", content, None)

    def test_explicit_corpus_excludes_canon_and_other_drafts_and_keeps_both_evidence_sides(self):
        first = self.draft("manuscript-a/ch001", REPEATED)
        second = self.draft("manuscript-a/ch002", "# 第二章\n\n" + REPEATED)
        self.draft("unrelated", "他不知道的是，另一部小说里没有桥。")
        chapter = self.project.path("chapters/ch001.md")
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text(REPEATED, encoding="utf-8")
        before = self.project.fingerprint()
        corpus = [{"path": item["path"], "hash": item["hash"]} for item in (first, second)]
        report = self.analysis.analyze(second["path"], second["hash"], explicit_corpus=corpus)
        self.assertEqual(report["explicit_corpus"], corpus)
        self.assertEqual(report["coverage"]["history_sources"], corpus[:1])
        related = next(item["related"] for item in report["issues"] if item["rule"] == "literal_overlap")
        self.assertEqual(related["path"], first["path"])
        self.assertEqual(related["quote"], REPEATED)
        self.assertEqual(self.project.fingerprint(), before)
        verified = self.analysis.verify(report["path"], report["hash"])
        self.assertEqual(verified["report"]["explicit_corpus"], corpus)
        self.assertEqual(verified["results"][0]["status"], "remaining")

    def test_empty_explicit_corpus_never_falls_back_and_stale_or_duplicate_sources_rejected(self):
        first = self.draft("one", REPEATED)
        second = self.draft("two", REPEATED)
        corpus = [{"path": first["path"], "hash": first["hash"]}]
        report = self.analysis.analyze(second["path"], second["hash"], explicit_corpus=[])
        self.assertEqual(report["coverage"]["history_scanned"], 0)
        self.assertFalse(any(item["rule"] == "literal_overlap" for item in report["issues"]))
        with self.assertRaises(VibeNovelError):
            self.analysis.analyze(second["path"], second["hash"], explicit_corpus=corpus * 2)
        self.documents.save(first["path"], "新版本", first["hash"])
        with self.assertRaises(ConflictError):
            self.analysis.analyze(second["path"], second["hash"], explicit_corpus=corpus)
        for path in ("memory/current_state.md", "../outside.md"):
            with self.assertRaises(VibeNovelError):
                self.analysis.analyze(second["path"], second["hash"], explicit_corpus=[{"path": path, "hash": digest(b"x")}])

    def test_missing_corpus_is_explicitly_omitted_and_retest_cannot_claim_resolution(self):
        first = self.draft("first", REPEATED)
        second = self.draft("second", REPEATED)
        corpus = [{"path": first["path"], "hash": first["hash"]}]
        old = self.analysis.analyze(second["path"], second["hash"], explicit_corpus=corpus)
        self.project.path(first["path"]).unlink()
        verified = self.analysis.verify(old["path"], old["hash"])
        self.assertEqual(verified["report"]["coverage"]["omitted"][0]["path"], first["path"])
        self.assertTrue(all(item["status"] == "unchecked" for item in verified["results"]))

    def manuscript(self):
        from vibenovel.core.manuscripts import ManuscriptStore
        document = self.draft("whole", "第一章 渡河\n\n" + REPEATED + "\n\n第二章 重逢\n\n" + REPEATED + "\n")
        return ManuscriptStore(self.project).prepare(document["path"], document["hash"])

    def test_batch_uses_manifest_only_and_preserves_canon_and_original(self):
        from vibenovel.core.manuscript_audit import ManuscriptAuditStore
        session = self.manuscript()
        self.draft("unrelated", REPEATED)
        original = self.documents.read(session["source"]["path"])
        before = self.project.fingerprint()
        result = ManuscriptAuditStore(self.project).analyze(session["id"])
        self.assertEqual(result["coverage"]["analyzed"], 2)
        self.assertEqual(result["coverage"]["requested"], 2)
        self.assertEqual(result["coverage"]["corpus_scanned"], 2)
        self.assertEqual(result["coverage"]["omitted"], [])
        paths = {item["path"] for item in session["sections"]}
        self.assertEqual(set(result["coverage"]["source_hashes"]), paths)
        self.assertTrue(all(set(report["dependencies"]) <= paths for report in result["reports"]))
        self.assertEqual(self.project.fingerprint(), before)
        self.assertEqual(self.documents.read(original["path"])["hash"], original["hash"])

    def test_batch_limits_and_invalid_selection_are_visible(self):
        from vibenovel.core.manuscript_audit import ManuscriptAuditStore
        session = self.manuscript()
        audit = ManuscriptAuditStore(self.project)
        with patch("vibenovel.core.manuscript_audit.MAX_BATCH_TARGETS", 1):
            result = audit.analyze(session["id"])
        self.assertEqual(result["coverage"]["analyzed"], 1)
        self.assertEqual(result["coverage"]["omitted"][0]["reason"], "target_batch_limit")
        with patch("vibenovel.core.manuscript_audit.MAX_BATCH_CHARACTERS", 1):
            result = audit.analyze(session["id"])
        self.assertEqual(result["coverage"]["analyzed"], 0)
        self.assertEqual(len(result["coverage"]["omitted"]), 2)
        for ids in ([], ["unknown"], ["section-001", "section-001"], "section-001", [True]):
            with self.subTest(ids=ids), self.assertRaises(VibeNovelError):
                audit.analyze(session["id"], ids)

    def test_batch_rejects_cross_window_changes_after_first_report(self):
        from vibenovel.core.manuscript_audit import ManuscriptAuditStore
        session = self.manuscript()
        analyze = AnalysisStore.analyze
        changed = False

        def run_then_change(store, *args, **kwargs):
            nonlocal changed
            report = analyze(store, *args, **kwargs)
            if not changed:
                changed = True
                item = session["sections"][1]
                self.documents.save(item["path"], "作者已修改的新版本", item["current_hash"])
            return report

        with patch.object(AnalysisStore, "analyze", run_then_change), self.assertRaises(ConflictError):
            ManuscriptAuditStore(self.project).analyze(session["id"])


if __name__ == "__main__":
    unittest.main()
