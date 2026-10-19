"""Blind preference ballots bind two exact draft versions and no model calls."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import json_bytes
from vibenovel.core.locking import ConflictError
from vibenovel.core.novelbench import NovelBenchStore
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


class NovelBenchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.bench = NovelBenchStore(self.project)
        self.first = self.documents.save("drafts/deepseek-candidate.md", "林远推开教室的门。😀\r\n", None)
        self.second = self.documents.save("drafts/glm-candidate.md", "林远在门外停住了脚步。\n", None)
        self.before = self.project.fingerprint()

    def create(self, reverse=0):
        with patch("vibenovel.core.novelbench.secrets.randbelow", return_value=reverse):
            return self.bench.create(self.first["path"], self.first["hash"], self.second["path"], self.second["hash"])

    def test_random_assignment_frozen_and_public_model_identity_hidden(self):
        ballot = self.create(reverse=1)
        self.assertEqual(ballot["candidates"][0]["content"], self.second["content"])
        self.assertEqual(ballot["candidates"][1]["content"], self.first["content"])
        self.assertEqual(self.bench.get(ballot["id"])["candidates"], ballot["candidates"])
        self.assertEqual(NovelBenchStore(self.project).get(ballot["id"])["candidates"], ballot["candidates"])
        exposed = json.dumps(ballot) + json.dumps(self.bench.list())
        self.assertNotIn("deepseek", exposed)
        self.assertNotIn("glm", exposed)
        self.assertNotIn("drafts/", exposed)
        self.assertNotIn("content", self.bench.list()["benchmarks"][0])
        self.assertEqual(self.project.fingerprint(), self.before)
        self.assertEqual(len(list(self.project.path("reviews").glob("bench-*.json"))), 1)

    def test_explicit_vote_is_author_preference_not_approval_or_training(self):
        ballot = self.create()
        decided = self.bench.vote(ballot["id"], "prefer_b", "我喜欢这个开场。", ballot["hash"])
        self.assertEqual(decided["status"], "voted")
        self.assertEqual(decided["vote"]["actor"], "AUTHOR")
        self.assertEqual(decided["vote"]["decision"], "prefer_b")
        self.assertEqual(self.project.fingerprint(), self.before)
        self.assertEqual(len(self.documents.history("reviews/bench-" + ballot["id"] + ".json")), 2)
        self.assertEqual(list(self.project.path("chapters").glob("*.md")), [])
        self.assertEqual(list(self.project.path("memory").rglob("*.json")), [])
        with self.assertRaises(ConflictError):
            self.bench.vote(ballot["id"], "prefer_a")

    def test_tie_and_explicit_reveal_only_after_vote(self):
        ballot = self.create(reverse=1)
        with self.assertRaises(ConflictError):
            self.bench.reveal(ballot["id"])
        self.bench.vote(ballot["id"], "tie")
        reveal = self.bench.reveal(ballot["id"])
        self.assertEqual(reveal["sources"][0]["label"], "A")
        self.assertEqual(reveal["sources"][0]["path"], self.second["path"])
        self.assertNotIn("path", self.bench.get(ballot["id"])["candidates"][0])

    def test_observed_version_round_trip_cannot_revive_ballot(self):
        ballot = self.create()
        modified = self.documents.save(self.first["path"], "另一版", self.first["hash"])
        self.documents.save(self.first["path"], self.first["content"], modified["hash"])
        self.assertTrue(self.bench.get(ballot["id"])["stale"])
        with self.assertRaises(ConflictError):
            self.bench.vote(ballot["id"], "prefer_a")

    def test_observed_external_drift_persists_invalidation_after_bytes_restored(self):
        ballot = self.create()
        self.project.path(self.first["path"]).write_text("external", encoding="utf-8")
        self.assertTrue(self.bench.get(ballot["id"])["stale"])
        self.project.path(self.first["path"]).write_bytes(self.first["content"].encode("utf-8"))
        self.assertTrue(NovelBenchStore(self.project).get(ballot["id"])["stale"])
        with self.assertRaises(ConflictError):
            self.bench.vote(ballot["id"], "tie")

    def test_deleted_source_is_stale_but_snapshots_can_be_read_and_exported(self):
        ballot = self.create()
        self.project.path(self.second["path"]).unlink()
        view = self.bench.get(ballot["id"])
        self.assertTrue(view["stale"])
        self.assertEqual(view["candidates"][1]["content"], self.second["content"])
        exported = self.bench.export(ballot["id"])
        self.assertTrue(exported["stale"])
        self.assertIn(self.first["content"], exported["content"])
        self.assertIn(self.second["content"], exported["content"])
        self.assertNotIn("deepseek", exported["content"])

    def test_export_only_contains_the_two_explicit_snapshots(self):
        self.documents.save("drafts/unrelated.md", "不应出现在比较里的正文。", None)
        ballot = self.create()
        result = self.bench.export(ballot["id"])
        self.assertIn(self.first["content"], result["content"])
        self.assertIn(self.second["content"], result["content"])
        self.assertNotIn("不应出现在比较里的正文", result["content"])
        self.assertEqual(list(self.project.path("exports").glob("*md")), [])

    def test_concurrent_vote_hash_and_bad_input_rejected(self):
        ballot = self.create()
        with self.assertRaises(ConflictError):
            self.bench.vote(ballot["id"], "prefer_a", expected_hash="0" * 64)
        for decision, note in (("approve", ""), (True, ""), ("tie", "x" * 4001), ("tie", None)):
            with self.subTest(decision=decision), self.assertRaises(VibeNovelError):
                self.bench.vote(ballot["id"], decision, note)
        self.assertEqual(self.bench.get(ballot["id"])["status"], "awaiting_vote")

    def test_source_paths_hashes_same_path_and_identity_are_checked(self):
        for path in ("chapters/ch001.md", "memory/states/ch001.md", "drafts/../secret.md", "config/api.local.json"):
            with self.subTest(path=path), self.assertRaises(VibeNovelError):
                self.bench.create(path, self.first["hash"], self.second["path"], self.second["hash"])
        with self.assertRaises(VibeNovelError):
            self.bench.create(self.first["path"], self.first["hash"], self.first["path"], self.first["hash"])
        with self.assertRaises(ConflictError):
            self.bench.create(self.first["path"], "0" * 64, self.second["path"], self.second["hash"])
        for identifier in ("../escape", "0" * 32, True):
            with self.assertRaises(VibeNovelError):
                self.bench.get(identifier)

    def test_existing_external_candidate_gets_frozen_history_in_same_journal(self):
        external = "drafts/external.md"
        self.project.path(external).write_bytes("外部已有候选\r\n".encode("utf-8"))
        source = self.documents.read(external)
        ballot = self.bench.create(external, source["hash"], self.second["path"], self.second["hash"])
        self.assertFalse(ballot["stale"])
        self.assertTrue(self.documents.history(external))
        self.assertFalse(self.bench.get(ballot["id"])["stale"])

    def test_cross_project_record_and_tampered_snapshot_rejected(self):
        ballot = self.create()
        path = "reviews/bench-" + ballot["id"] + ".json"
        doc = self.documents.read(path)
        value = json.loads(doc["content"])
        value["novel_id"] = "other"
        changed = self.documents.save(path, json_bytes(value).decode("utf-8"), doc["hash"])
        with self.assertRaises(ConflictError):
            self.bench.get(ballot["id"])
        value["novel_id"] = "story"
        value["sources"][0]["content"] = "伪造快照"
        self.documents.save(path, json_bytes(value).decode("utf-8"), changed["hash"])
        with self.assertRaises(ConflictError):
            self.bench.get(ballot["id"])

    def test_vote_recovers_after_target_written_without_consuming_twice(self):
        ballot = self.create()
        original = self.bench.storage._write_state
        failed = False

        def fail_state(state):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated crash")
            return original(state)

        with patch.object(self.bench.storage, "_write_state", side_effect=fail_state), self.assertRaises(OSError):
            self.bench.vote(ballot["id"], "prefer_b")
        recovered = NovelBenchStore(self.project).get(ballot["id"])
        self.assertEqual(recovered["vote"]["decision"], "prefer_b")
        with self.assertRaises(ConflictError):
            self.bench.vote(ballot["id"], "prefer_a")
        self.assertEqual(self.project.fingerprint(), self.before)


if __name__ == "__main__":
    unittest.main()
