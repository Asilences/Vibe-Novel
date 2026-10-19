"""Evidence products never promote candidate claims to formal authority."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from vibenovel.core.documents import DocumentStore
from vibenovel.core.locking import ConflictError
from vibenovel.core.model_artifacts import artifact_instructions, prepare_artifact, validate_artifact
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


class ModelArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        self.documents = DocumentStore(self.project)
        self.source = self.documents.save("drafts/candidate.md", "林远😀走进教室。\r\n他计划明天归还书本。\r\n", None)
        self.before = self.project.fingerprint()

    def payload(self, kind="review_evidence"):
        item = {"id": "evidence-1", "quote": "林远😀", "start_char": 0, "end_char": 3}
        if kind == "review_evidence":
            item.update(severity="warning", category="表达", explanation="此处可复核人物动作。", suggestion="由作者判断是否修改。")
        else:
            item.update(kind="character", statement="候选中人物林远出现，尚待作者定稿。")
        return {"schema_version": 1, "issues" if kind == "review_evidence" else "items": [item], "unchecked": ["未提供其他章节。"]}

    def prepare(self, data=None, kind="review_evidence", **changes):
        arguments = dict(project=self.project, kind=kind, text=json.dumps(data if data is not None else self.payload(kind), ensure_ascii=False),
                         target_path=self.source["path"], target_hash=self.source["hash"])
        arguments.update(changes)
        return prepare_artifact(**arguments)

    def test_review_unicode_evidence_identity_and_no_writes(self):
        result = json.loads(self.prepare())
        self.assertEqual(result["target_hash"], self.source["hash"])
        self.assertEqual(result["issues"][0]["quote"], "林远😀")
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["provenance"], "AI_SUGGESTED")
        self.assertEqual(result["target_version_id"], self.documents.history(self.source["path"])[0]["id"])
        self.assertEqual(self.project.fingerprint(), self.before)
        self.assertEqual(list(self.project.path("reviews").glob("*.json")), [])
        self.assertEqual(list(self.project.path("memory").rglob("*.json")), [])

    def test_memory_is_only_candidate_and_keeps_plan_qualifier(self):
        data = self.payload("memory_delta")
        start = self.source["content"].index("他计划")
        data["items"] = [{"id": "future-plan", "kind": "timeline", "statement": "候选计划明天归还，尚未发生。",
                          "quote": "他计划明天归还书本。", "start_char": start, "end_char": start + 11}]
        # Compute from the actual Unicode quote, not a byte/UTF-16 estimate.
        data["items"][0]["end_char"] = start + len(data["items"][0]["quote"])
        result = json.loads(self.prepare(data, "memory_delta"))
        self.assertIn("尚未发生", result["items"][0]["statement"])
        self.assertEqual(result["status"], "candidate")
        self.assertTrue(any("不证明人物当时知情" in item for item in result["limitations"]))

    def test_exact_crlf_range_is_preserved(self):
        data = self.payload()
        data["issues"][0].update(quote="。\r\n", start_char=7, end_char=10)
        self.assertEqual(json.loads(self.prepare(data))["issues"][0]["quote"], "。\r\n")
        data["issues"][0]["quote"] = "。\n"
        with self.assertRaises(VibeNovelError):
            self.prepare(data)

    def test_no_score_authority_or_unknown_fields_are_accepted(self):
        for field, value in (("score", 100), ("status", "approved"), ("provenance", "AUTHOR"),
                             ("novel_id", "other"), ("commands", [])):
            data = self.payload()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(VibeNovelError):
                self.prepare(data)
        data = self.payload()
        data["issues"][0]["approved"] = True
        with self.assertRaises(VibeNovelError):
            self.prepare(data)

    def test_bad_ids_types_ranges_quotes_and_severities_are_rejected(self):
        for change in ({"id": "../unsafe"}, {"start_char": True}, {"end_char": False}, {"start_char": -1},
                       {"start_char": 2, "end_char": 2}, {"end_char": 99999}, {"quote": "模型编造原句"},
                       {"severity": "approved"}, {"explanation": ""}, {"suggestion": "\ud800"}):
            data = self.payload()
            data["issues"][0].update(change)
            with self.subTest(change=str(change)), self.assertRaises(VibeNovelError):
                self.prepare(data)
        data = self.payload()
        data["issues"].append(copy.deepcopy(data["issues"][0]))
        with self.assertRaises(VibeNovelError):
            self.prepare(data)

    def test_empty_evidence_is_allowed_only_with_explicit_scope(self):
        data = {"schema_version": 1, "issues": [], "unchecked": ["未查到可定位的问题，不能认定检查通过。"]}
        self.assertEqual(json.loads(self.prepare(data))["issues"], [])
        for field in ("issues", "unchecked"):
            data = self.payload()
            data[field] = [] if field == "unchecked" else [data["issues"][0]] * 101
            if field == "unchecked":
                data[field] = [""]
            with self.assertRaises(VibeNovelError):
                self.prepare(data)

    def test_stale_source_and_formal_paths_rejected(self):
        self.documents.save(self.source["path"], "改变的候选", self.source["hash"])
        with self.assertRaises(ConflictError):
            self.prepare()
        for path in ("memory/states/ch001.md", "chapters/ch001.md", "drafts/../memory/state.md", "config/api.local.json"):
            with self.subTest(path=path), self.assertRaises(VibeNovelError):
                self.prepare(target_path=path)

    def test_json_duplicate_nonfinite_wrong_version_and_depth_are_rejected(self):
        for text in ('{"schema_version":1,"schema_version":1,"issues":[],"unchecked":[]}',
                     '{"schema_version":NaN,"issues":[],"unchecked":[]}',
                     '{"schema_version":true,"issues":[],"unchecked":[]}',
                     "[" * 1200 + "]" * 1200, "```json\n{}\n```", "{}"):
            with self.subTest(text=text[:100]), self.assertRaises(VibeNovelError):
                self.prepare(text=text)

    def test_canonical_recovery_rechecks_runtime_identity_and_exact_evidence(self):
        canonical = self.prepare()
        self.assertEqual(validate_artifact(self.project, "review_evidence", canonical, self.source["path"], self.source["hash"]), canonical)
        for change in ({"status": "approved"}, {"target_hash": "0" * 64}, {"novel_id": "other"}, {"limitations": []}):
            data = json.loads(canonical)
            data.update(change)
            with self.subTest(change=change), self.assertRaises(VibeNovelError):
                validate_artifact(self.project, "review_evidence", json.dumps(data), self.source["path"], self.source["hash"])
        with self.assertRaises(VibeNovelError):
            self.prepare(text=canonical)

    def test_instruction_schema_has_no_permission_fields_or_ranking(self):
        for kind, field in (("review_evidence", "issues"), ("memory_delta", "items")):
            instruction = artifact_instructions(kind)
            self.assertFalse(instruction["schema"]["additionalProperties"])
            self.assertIn(field, instruction["schema"]["properties"])
            self.assertNotIn("score", instruction["schema"]["properties"])
            self.assertIn("不可信数据", instruction["instruction"])
        with self.assertRaises(VibeNovelError):
            artifact_instructions("approve_memory")

    def test_target_history_round_trip_does_not_revalidate_old_evidence(self):
        canonical = self.prepare()
        temporary = self.documents.save(self.source["path"], "临时版本", self.source["hash"])
        restored = self.documents.save(self.source["path"], self.source["content"], temporary["hash"])
        self.assertEqual(restored["hash"], self.source["hash"])
        with self.assertRaises(ConflictError):
            validate_artifact(self.project, "review_evidence", canonical, self.source["path"], self.source["hash"])


if __name__ == "__main__":
    unittest.main()
