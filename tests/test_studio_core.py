"""Studio authorization, observed drift, process exclusion and crash boundaries."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.documents import (JOURNAL_PATH, MAX_DOCUMENT_BYTES, STATE_PATH, ConflictError,
                                      DocumentStore, canonical_read)
from vibenovel.core.files import atomic_write, digest, json_bytes, read_json
from vibenovel.core.imports import import_outline
from vibenovel.core.locking import project_lock
from vibenovel.core.project import ProjectStore
from vibenovel.errors import MissingDocumentError, VibeNovelError


class StudioCoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("test-story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.approvals = ApprovalStore(self.project)

    def proposal(self, content="# approved plan\n", target="outlines/approved/ch001.md"):
        path = self.project.path(target)
        expected = digest(path.read_bytes()) if path.exists() else None
        return self.approvals.create(target, content, "作者调整场景", ["下一章的行动动机"], expected)

    def approve_apply(self, content="# approved plan\n", target="outlines/approved/ch001.md"):
        proposal = self.proposal(content, target)
        self.approvals.decide(proposal["id"], "approve")
        return self.approvals.apply(proposal["id"])

    def test_parallel_normal_reads_wait_for_short_project_write(self):
        self.documents.save("drafts/ch001.md", "saved", None)
        held = threading.Event()
        release = threading.Event()

        def hold():
            with project_lock(self.project):
                held.set()
                release.wait(1)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(1))
        with ThreadPoolExecutor(max_workers=4) as pool:
            reads = [pool.submit(self.documents.list_documents), pool.submit(self.approvals.list),
                     pool.submit(self.documents.read, "drafts/ch001.md")]
            time.sleep(.03)
            self.assertTrue(all(not result.done() for result in reads))
            release.set()
            self.assertEqual(reads[2].result(timeout=3)["content"], "saved")
            self.assertEqual(len(reads[0].result(timeout=3)), 1)
            self.assertEqual(reads[1].result(timeout=3), [])
        thread.join(2)

    def test_edit_conflict_history_and_restore_are_new_versions(self):
        first = self.documents.save("drafts/ch001.md", "first", None)
        with self.assertRaises(ConflictError):
            self.documents.save("drafts/ch001.md", "racing create", None)
        second = self.documents.save("drafts/ch001.md", "second", first["hash"])
        with self.assertRaises(ConflictError):
            self.documents.save("drafts/ch001.md", "stale", first["hash"])
        history = self.documents.history(first["path"])
        restored = self.documents.restore(first["path"], history[-1]["id"], second["hash"])
        self.assertEqual(restored["content"], "first")
        versions = self.documents.history(first["path"])
        self.assertEqual([item["version"] for item in versions], [3, 2, 1])
        self.assertEqual(versions[0]["restored_from"], history[-1]["id"])
        self.assertNotEqual(versions[0]["id"], versions[-1]["id"])

    def test_external_edit_must_match_hash_and_gets_before_snapshot(self):
        first = self.documents.save("drafts/ch001.md", "first", None)
        self.project.path(first["path"]).write_bytes(b"external")
        with self.assertRaises(ConflictError):
            self.documents.save(first["path"], "second", first["hash"])
        self.documents.save(first["path"], "second", digest(b"external"))
        history = read_json(self.project.path(STATE_PATH), max_bytes=64 * 1024 * 1024)["history"][first["path"]]
        self.assertEqual([item["content"] for item in history], ["first", "external", "second"])

    def test_whitelist_canon_and_metadata_are_not_directly_editable(self):
        for path in ("chapters/ch001.md", "memory/states/current.md", "bible/world.md", "outlines/approved/ch001.md",
                     "project.json", ".runtime/config.json", "approvals/forged.json", "../escape.md",
                     "drafts/../../escape.md", "drafts//same.md", "drafts/./same.md", "drafts\\escape.md"):
            with self.subTest(path=path), self.assertRaises(VibeNovelError):
                self.documents.save(path, "unauthorized", None)
        self.assertFalse(self.project.path("chapters/ch001.md").exists())

    def test_import_is_verified_on_each_read_and_never_saved(self):
        source = self.workspace / "original.md"
        source.write_bytes("# 来源\r\n文字".encode("utf-8"))
        imported = import_outline(self.project, source)
        path = f"imports/original/{imported['import_id']}/extracted.md"
        self.assertTrue(self.documents.read(path)["readonly"])
        with self.assertRaises(VibeNovelError):
            self.documents.save(path, "changed", self.documents.read(path)["hash"])
        self.project.path(path).write_bytes(b"tampered")
        with self.assertRaises(VibeNovelError):
            self.documents.read(path)

    def test_large_valid_import_can_be_listed_without_unbounded_preview(self):
        source = self.workspace / "large.md"
        source.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
        import_outline(self.project, source)
        listed = self.documents.list_documents()
        self.assertEqual(len(listed), 3)
        original = next(item for item in listed if item["path"].endswith("original.md"))
        self.assertEqual(original["bytes"], MAX_DOCUMENT_BYTES + 1)
        self.assertEqual(len(self.documents.read(original["path"])["content"]), MAX_DOCUMENT_BYTES + 1)
        with self.assertRaises(VibeNovelError):
            self.documents.save("drafts/too-large.md", "x" * (MAX_DOCUMENT_BYTES + 1), None)

    def test_missing_document_and_path_scoped_verified_version(self):
        with self.assertRaises(MissingDocumentError):
            self.documents.read("bible/new-world.md")
        first = self.documents.save("drafts/ch001.md", "original", None)
        version_id = self.documents.history(first["path"])[0]["id"]
        version = self.documents.version(first["path"], version_id)
        self.assertEqual(version["content"], "original")
        self.assertEqual(version["hash"], first["hash"])
        with self.assertRaises(MissingDocumentError):
            self.documents.version("drafts/ch002.md", version_id)
        state = read_json(self.project.path(STATE_PATH), max_bytes=64 * 1024 * 1024)
        state["history"][first["path"]][0]["content"] = "tampered"
        self.project.path(STATE_PATH).write_bytes(json_bytes(state))
        with self.assertRaises(ConflictError):
            self.documents.version(first["path"], version_id)

    def test_generation_revision_guard_detects_API_A_B_A(self):
        self.approve_apply("A", "bible/world.md")
        document = self.documents.save("drafts/ch001.md", "candidate", None)
        fingerprint = self.project.fingerprint()
        self.approve_apply("B", "bible/world.md")
        self.approve_apply("A", "bible/world.md")
        with self.assertRaises(ConflictError):
            self.documents.save("drafts/generated.md", "stale", None, expected_fingerprint=fingerprint,
                                expected_revision=document["revision"])

    def test_rejected_pending_and_consumed_proposals_never_apply(self):
        pending = self.proposal()
        with self.assertRaises(ConflictError):
            self.approvals.apply(pending["id"])
        self.approvals.decide(pending["id"], "reject", "重新规划")
        with self.assertRaises(ConflictError):
            self.approvals.apply(pending["id"])
        with self.assertRaises(ConflictError):
            self.approvals.decide(pending["id"], "approve")
        applied = self.approve_apply()
        self.assertEqual(applied["state"], "applied")
        with self.assertRaises(ConflictError):
            self.approvals.apply(applied["id"])
        self.assertEqual(self.documents.read(applied["target"])["content"], applied["after"])

    def test_proposal_exposes_full_diff_and_content_hash(self):
        self.approve_apply("old", "bible/world.md")
        proposal = self.proposal("new", "bible/world.md")
        self.assertEqual(proposal["before"], "old")
        self.assertEqual(proposal["after"], "new")
        self.assertEqual(proposal["novel_id"], "test-story")
        self.assertEqual(proposal["base_revision"], 1)
        self.assertEqual(len(proposal["content_hash"]), 64)
        self.assertTrue(proposal["reason"] and proposal["impact"])

    def test_canonical_api_A_B_A_does_not_revalidate_old_approval(self):
        self.approve_apply("A", "bible/world.md")
        original = self.project.fingerprint()
        old = self.proposal("would overwrite", "bible/world.md")
        self.approvals.decide(old["id"], "approve")
        self.approve_apply("B", "bible/world.md")
        self.approve_apply("A", "bible/world.md")
        self.assertEqual(self.project.fingerprint(), original)
        with self.assertRaises(ConflictError):
            self.approvals.apply(old["id"])

    def test_external_canonical_change_stales_both_approve_and_apply(self):
        pending = self.proposal()
        approved = self.proposal("another")
        self.approvals.decide(approved["id"], "approve")
        self.project.path("bible/world.md").write_bytes(b"external")
        with self.assertRaises(ConflictError):
            self.approvals.decide(pending["id"], "approve")
        with self.assertRaises(ConflictError):
            self.approvals.apply(approved["id"])
        self.assertFalse(self.project.path(pending["target"]).exists())

    def test_external_observed_A_B_A_records_revision(self):
        self.approve_apply("A", "bible/world.md")
        old = self.proposal("stale", "bible/world.md")
        self.project.path("bible/world.md").write_bytes(b"B")
        self.documents.read("bible/world.md")
        self.project.path("bible/world.md").write_bytes(b"A")
        with self.assertRaises(ConflictError):
            self.approvals.decide(old["id"], "approve")
        self.assertEqual(self.documents.read("bible/world.md")["revision"], 3)

    def test_editing_draft_does_not_invalidate_canonical_approval(self):
        proposal = self.proposal()
        self.approvals.decide(proposal["id"], "approve")
        self.documents.save("drafts/ch001.md", "a candidate", None, "AI_SUGGESTED")
        self.assertEqual(self.approvals.apply(proposal["id"])["state"], "applied")

    def test_tampering_or_copying_proposal_identity_fails_closed(self):
        proposal = self.proposal()
        self.approvals.decide(proposal["id"], "approve")
        state = read_json(self.project.path(STATE_PATH), max_bytes=64 * 1024 * 1024)
        baseline = json_bytes(state)
        for field, value in (("after", "changed"), ("novel_id", "other"), ("before", "forged")):
            altered = read_json(self.project.path(STATE_PATH), max_bytes=64 * 1024 * 1024)
            altered["proposals"][proposal["id"]][field] = value
            self.project.path(STATE_PATH).write_bytes(json_bytes(altered))
            with self.subTest(field=field), self.assertRaises(ConflictError):
                self.approvals.apply(proposal["id"])
            self.project.path(STATE_PATH).write_bytes(baseline)
        other = ProjectStore(self.workspace).init("other", genre="adventure")
        with self.assertRaises(VibeNovelError):
            ApprovalStore(other).get(proposal["id"])

    def test_invalid_json_unicode_oversize_and_empty_chapter_are_rejected(self):
        for content in ('{"duplicate":1,"duplicate":2}', '{"value":NaN}', '{"value":1e999}', '{"s":"\\ud800"}'):
            with self.subTest(content=content), self.assertRaises(VibeNovelError):
                self.documents.save("drafts/test.json", content, None)
        for content in ("\ud800", "a" * (MAX_DOCUMENT_BYTES + 1)):
            with self.assertRaises(VibeNovelError):
                self.documents.save("drafts/ch001.md", content, None)
        with self.assertRaises(VibeNovelError):
            self.proposal(" \n", "chapters/ch001.md")

    def test_approved_outline_selection_requires_existing_canonical_path(self):
        self.documents.save("outlines/proposals/ch001.md", "candidate", None)
        with self.assertRaises(VibeNovelError):
            self.approvals.approved_outline("outlines/proposals/ch001.md")
        with self.assertRaises(VibeNovelError):
            self.approvals.approved_outline("outlines/approved/ch001.md")
        self.project.path("outlines/approved/ch001.md").write_bytes(b"manually copied, not approved")
        with self.assertRaises(ConflictError):
            self.approvals.approved_outline("outlines/approved/ch001.md")
        self.approve_apply()
        self.assertEqual(self.approvals.approved_outline("outlines/approved/ch001.md")["kind"], "canonical")

    def test_generation_publication_checks_all_snapshots_under_lock(self):
        source = self.documents.save("outlines/source/ch001.md", "source", None)
        baseline = self.project.fingerprint()
        self.documents.save(source["path"], "changed", source["hash"])
        with self.assertRaises(ConflictError):
            self.documents.save("drafts/generated.md", "stale generation", None,
                                expected_fingerprint=baseline, expected_sources={source["path"]: source["hash"]})
        self.assertFalse(self.project.path("drafts/generated.md").exists())
        self.project.path("bible/external.md").write_bytes(b"new canon")
        with self.assertRaises(ConflictError):
            self.documents.save("drafts/generated.md", "stale generation", None,
                                expected_fingerprint=baseline)

    def test_process_lock_is_exclusive_and_is_released(self):
        script = ("import sys; from pathlib import Path; from vibenovel.core.project import ProjectStore; "
                  "from vibenovel.core.documents import DocumentStore; "
                  "p=ProjectStore(Path(sys.argv[1])).get('test-story'); "
                  "DocumentStore(p).save('drafts/from-process.md','child',None)")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONIOENCODING"] = "utf-8"
        with project_lock(self.project):
            result = subprocess.run([sys.executable, "-c", script, str(self.workspace)], env=env,
                                    capture_output=True, encoding="utf-8", timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ConflictError", result.stderr)
        result = subprocess.run([sys.executable, "-c", script, str(self.workspace)], env=env,
                                capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_apply_recovery_at_each_persistence_boundary_consumes_approval_once(self):
        # Journal -> target -> revision+decision -> cleanup are distinct crash points.
        for boundary in ("journal", "target", "state", "cleanup"):
            with self.subTest(boundary=boundary):
                project = ProjectStore(self.workspace).init("crash-" + boundary, genre="campus")
                approvals = ApprovalStore(project)
                proposal = approvals.create("bible/world.md", "authorized", "理由", ["世界观"], None)
                approvals.decide(proposal["id"], "approve")

                def interrupted_write(path, data, **kwargs):
                    if boundary == "journal" and path.name == "journal.json":
                        atomic_write(path, data, **kwargs)
                        raise OSError("power cut after journal")
                    if boundary == "target" and path.name == "world.md":
                        atomic_write(path, data, **kwargs)
                        raise OSError("power cut after target")
                    if boundary == "state" and path.name == "state.json":
                        atomic_write(path, data, **kwargs)
                        raise OSError("power cut after revision")
                    return atomic_write(path, data, **kwargs)

                if boundary == "cleanup":
                    original_unlink = Path.unlink

                    def interrupted_unlink(path, *args, **kwargs):
                        if path.name == "journal.json":
                            raise OSError("power cut before cleanup")
                        return original_unlink(path, *args, **kwargs)

                    context = patch.object(Path, "unlink", interrupted_unlink)
                else:
                    context = patch("vibenovel.core.documents.atomic_write", interrupted_write)
                with context, self.assertRaises(OSError):
                    approvals.apply(proposal["id"])
                recovered = ApprovalStore(project).get(proposal["id"])
                self.assertEqual(recovered["state"], "applied")
                self.assertEqual(recovered["applied_revision"], 1)
                self.assertEqual(project.path("bible/world.md").read_bytes(), b"authorized")
                self.assertFalse(project.path(JOURNAL_PATH).exists())
                with self.assertRaises(ConflictError):
                    approvals.apply(proposal["id"])

    def test_ambiguous_interrupted_apply_blocks_all_writes_and_keeps_journal(self):
        proposal = self.proposal("authorized", "bible/world.md")
        self.approvals.decide(proposal["id"], "approve")

        def fail_target(path, data, **kwargs):
            if path.name == "world.md":
                raise OSError("before target publish")
            return atomic_write(path, data, **kwargs)

        with patch("vibenovel.core.documents.atomic_write", fail_target), self.assertRaises(OSError):
            self.approvals.apply(proposal["id"])
        self.project.path("bible/world.md").write_bytes(b"unrelated external change")
        with self.assertRaises(ConflictError):
            self.documents.save("drafts/blocked.md", "cannot publish", None)
        self.assertTrue(self.project.path(JOURNAL_PATH).exists())
        self.assertFalse(self.project.path("drafts/blocked.md").exists())
        self.assertEqual(self.project.path("bible/world.md").read_bytes(), b"unrelated external change")

    def test_editable_transaction_recovers_version_as_well_as_content(self):
        first = self.documents.save("drafts/ch001.md", "first", None)

        def fail_state(path, data, **kwargs):
            if path.name == "state.json":
                raise OSError("state disk write failed")
            return atomic_write(path, data, **kwargs)

        with patch("vibenovel.core.documents.atomic_write", fail_state), self.assertRaises(OSError):
            self.documents.save(first["path"], "second", first["hash"])
        self.assertEqual(self.documents.read(first["path"])["content"], "second")
        self.assertEqual(len(self.documents.history(first["path"])), 2)

    def test_canonical_read_recovers_before_exposing_locked_snapshot(self):
        proposal = self.proposal("final", "chapters/ch001.md")
        self.approvals.decide(proposal["id"], "approve")

        def fail_state(path, data, **kwargs):
            if path.name == "state.json":
                raise OSError("revision write interrupted")
            return atomic_write(path, data, **kwargs)

        with patch("vibenovel.core.documents.atomic_write", fail_state), self.assertRaises(OSError):
            self.approvals.apply(proposal["id"])
        with canonical_read(self.project) as snapshot:
            self.assertEqual(snapshot["revision"], 1)
            self.assertEqual(snapshot["fingerprint"], self.project.fingerprint())
            self.assertFalse(self.project.path(JOURNAL_PATH).exists())
            with self.assertRaises(ConflictError):
                self.documents.save("drafts/race.md", "blocked", None)


if __name__ == "__main__":
    unittest.main()
