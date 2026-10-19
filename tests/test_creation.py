"""The opening brief is author intent; its candidates never become canon."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibenovel.core.creation import (CATALOG_VERSION, CREATION_SELECTION_PATH,
                                     CREATIVE_BRIEF_PATH, MAX_BRIEF_BYTES,
                                     CreativeBriefStore, brief_warnings, catalog,
                                     defaults, effective_brief, render_brief,
                                     validate_brief, validate_for_generation)
from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import json_bytes
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


class CreationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.project = ProjectStore(self.workspace).init("opening", genre="campus")
        self.documents = DocumentStore(self.project)
        self.creation = CreativeBriefStore(self.project)
        self.before = self.project.fingerprint()

    def save(self, value=None):
        current = self.creation.get()
        return self.creation.save(value if value is not None else current["brief"],
                                  current["hash"], current["version_id"])

    def outline(self, content="# 总纲\n作者尚未确认的故事建议。"):
        doc = self.documents.save("outlines/proposals/first.md", content, None)
        doc["version_id"] = self.documents.history(doc["path"])[0]["id"]
        return doc

    def choose(self, doc=None, brief=None):
        doc = doc or self.outline()
        selected = self.creation.get_selection()
        return self.creation.select(doc["path"], doc["hash"], doc["version_id"],
                                    brief_hash=brief["hash"] if brief else None,
                                    brief_version_id=brief["version_id"] if brief else None,
                                    expected_hash=selected["hash"], expected_version_id=selected["version_id"])

    def tag(self, key, *, decision="selected", source="catalog"):
        value = copy.deepcopy(next(item for item in catalog()["tags"] if item["id"] == key))
        value.update(decision=decision, source=source)
        return value

    def test_empty_default_can_be_saved_without_model_or_canon(self):
        current = self.creation.get()
        self.assertEqual(current["path"], CREATIVE_BRIEF_PATH)
        self.assertEqual(current["brief"], defaults("opening"))
        self.assertIsNone(current["hash"])
        self.assertIsNone(current["version_id"])
        self.assertFalse(self.project.path(CREATIVE_BRIEF_PATH).exists())
        saved = self.save()
        self.assertEqual(self.creation.get(), saved)
        self.assertEqual(len(self.documents.history(CREATIVE_BRIEF_PATH)), 1)
        self.assertEqual(self.project.fingerprint(), self.before)
        self.assertEqual(list(self.project.path("chapters").glob("*")), [])
        self.assertEqual(list(self.project.path("memory").rglob("*.json")), [])

    def test_only_story_seed_required_to_generate(self):
        value = defaults("opening")
        for seed in ("", " \n\t"):
            value["story_seed"] = seed
            with self.assertRaises(VibeNovelError):
                validate_for_generation(value, "opening")
        value["story_seed"] = "一位失败的教练回到自己的母校。"
        self.assertEqual(validate_for_generation(value, "opening"), value)
        self.assertEqual(value["protagonists"], [])
        self.assertEqual(value["advantage"]["status"], "undecided")

    def test_strict_schema_identity_unknown_fields_and_invalid_unicode(self):
        cases = []
        for field, value in (("schema_version", True), ("schema_version", 2),
                             ("novel_id", "another"), ("catalog_version", "future"),
                             ("reading_category", "../../private"), ("story_seed", "x" * 4001),
                             ("story_seed", "\ud800"), ("story_seed", "bad\x00seed")):
            brief = defaults("opening")
            brief[field] = value
            cases.append(brief)
        missing = defaults("opening")
        missing.pop("opening")
        cases.append(missing)
        extra = defaults("opening")
        extra["api_key"] = "not-allowed"
        cases.append(extra)
        for brief in cases:
            with self.subTest(brief=str(brief)[:100]), self.assertRaises(VibeNovelError):
                self.save(brief)
        self.assertFalse(self.project.path(CREATIVE_BRIEF_PATH).exists())

    def test_custom_categories_and_tags_do_not_choose_prompt_paths(self):
        value = defaults("opening")
        value["reading_category"] = "custom:太空经营"
        value["tags"] = [{"id": "custom:space_management", "label": "太空经营", "source": "author",
                          "decision": "selected", "dimensions": ["plot", "world"]}]
        self.assertEqual(validate_brief(value, "opening"), value)
        self.assertEqual(self.project.metadata["genre"], "campus")
        value["tags"][0]["source"] = "catalog"
        with self.assertRaises(VibeNovelError):
            validate_brief(value, "opening")

    def test_catalog_shared_concepts_use_one_id_and_cannot_be_relabelled(self):
        data = catalog()
        self.assertEqual(data["catalog_version"], CATALOG_VERSION)
        self.assertEqual(len({tag["id"] for tag in data["tags"]}), len(data["tags"]))
        friendship = self.tag("theme:friendship")
        self.assertEqual(set(friendship["dimensions"]), {"theme", "emotion"})
        value = defaults("opening")
        value["tags"] = [friendship, copy.deepcopy(friendship)]
        with self.assertRaises(VibeNovelError):
            validate_brief(value, "opening")
        value["tags"] = [self.tag("plot:management")]
        value["tags"][0]["label"] = "系统流"
        with self.assertRaises(VibeNovelError):
            validate_brief(value, "opening")
        value["tags"] = [self.tag("plot:management")]
        value["tags"][0]["dimensions"] = ["persona"]
        with self.assertRaises(VibeNovelError):
            validate_brief(value, "opening")

    def test_pending_suggestions_are_absent_and_exclusions_stay_separate(self):
        value = defaults("opening")
        value["tags"] = [self.tag("plot:management"),
                         self.tag("plot:rebirth", source="ai", decision="pending"),
                         self.tag("plot:system", decision="excluded"),
                         self.tag("world:future", source="external", decision="undecided")]
        effective = effective_brief(value)
        self.assertEqual([tag["id"] for tag in effective["selected_tags"]], ["plot:management"])
        self.assertEqual([tag["id"] for tag in effective["excluded_tags"]], ["plot:system"])
        rendered = render_brief(value)
        self.assertNotIn("plot:rebirth", rendered)
        self.assertNotIn("world:future", rendered)
        value["tags"][1]["decision"] = "selected"
        self.assertEqual(effective_brief(value)["selected_tags"][1]["source"], "ai")
        self.assertNotIn("正式正文事实", json.dumps(effective))

    def test_no_and_undecided_preserve_notes_but_do_not_send_hidden_advantage(self):
        value = defaults("opening")
        value["advantage"]["ability"] = "以前考虑过读心术，尚未决定"
        for status in ("undecided", "no"):
            value["advantage"]["status"] = status
            self.assertEqual(effective_brief(value)["advantage"], {"status": status})
            self.assertEqual(self.save(value)["brief"]["advantage"]["ability"], "以前考虑过读心术，尚未决定")
        value["advantage"]["status"] = "yes"
        self.assertIn("ability", effective_brief(value)["advantage"])

    def test_conflicts_warn_without_rewriting_author_conditions(self):
        value = defaults("opening")
        value["advantage"]["status"] = "no"
        value["tags"] = [self.tag("plot:system"), self.tag("world:realistic"), self.tag("world:supernatural")]
        value["scale"]["target_words_min"] = 20000
        value["scale"]["target_words_max"] = 10000
        self.assertEqual(len(brief_warnings(value)), 3)
        self.assertEqual(validate_brief(value, "opening"), value)

    def test_nested_types_and_bounds_reject_boolean_numbers_and_duplicate_ids(self):
        cases = []
        for path, value in (("volume_count", True), ("chapter_words", 0), ("target_words_max", 1.5)):
            brief = defaults("opening")
            brief["scale"][path] = value
            cases.append(brief)
        brief = defaults("opening")
        brief["protagonists"] = [{"id": "p1", "name": "", "role": "", "goal": "", "flaw": ""}] * 2
        cases.append(brief)
        brief = defaults("opening")
        brief["constraints"]["hard"] = ["x"] * 41
        cases.append(brief)
        brief = defaults("opening")
        brief["narration"]["person"] = "omniscient"
        cases.append(brief)
        for brief in cases:
            with self.subTest(brief=brief), self.assertRaises(VibeNovelError):
                validate_brief(brief, "opening")

    def test_per_dimension_and_serialized_size_limits_are_enforced(self):
        value = defaults("opening")
        value["tags"] = [{"id": "custom:tag%d" % index, "label": "标签%d" % index,
                          "source": "author", "decision": "selected", "dimensions": ["plot"]}
                         for index in range(21)]
        with self.assertRaises(VibeNovelError):
            validate_brief(value, "opening")
        value = defaults("opening")
        for key in value["constraints"]:
            value["constraints"][key] = ["中" * 1000] * 40
        self.assertGreater(len(json_bytes(value)), MAX_BRIEF_BYTES)
        with self.assertRaises(VibeNovelError):
            validate_brief(value, "opening")

    def test_save_requires_hash_and_history_even_after_author_round_trip(self):
        original = self.save()
        second = copy.deepcopy(original["brief"])
        second["story_seed"] = "第二版"
        changed = self.creation.save(second, original["hash"], original["version_id"])
        restored = self.creation.save(original["brief"], changed["hash"], changed["version_id"])
        self.assertEqual(restored["hash"], original["hash"])
        self.assertNotEqual(restored["version_id"], original["version_id"])
        with self.assertRaises(ConflictError):
            self.creation.save(second, original["hash"], original["version_id"])

    def test_observed_external_changes_are_snapshotted_and_old_identity_rejected(self):
        original = self.save()
        modified = copy.deepcopy(original["brief"])
        modified["story_seed"] = "外部编辑"
        self.project.path(CREATIVE_BRIEF_PATH).write_bytes(json_bytes(modified))
        self.assertEqual(self.creation.get()["brief"], modified)
        self.project.path(CREATIVE_BRIEF_PATH).write_bytes(json_bytes(original["brief"]))
        observed = self.creation.get()
        self.assertEqual(observed["hash"], original["hash"])
        with self.assertRaises(ConflictError):
            self.creation.save(modified, original["hash"], original["version_id"])

    def test_duplicate_json_fields_and_cross_novel_disk_content_are_rejected(self):
        self.project.path(CREATIVE_BRIEF_PATH).write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
        with self.assertRaises(VibeNovelError):
            self.creation.get()
        self.project.path(CREATIVE_BRIEF_PATH).write_bytes(json_bytes(defaults("another")))
        with self.assertRaises(VibeNovelError):
            self.creation.get()

    def test_explicit_outline_selection_is_versioned_and_not_canon_approval(self):
        self.assertIsNone(self.creation.get_selection()["selection"])
        chosen = self.choose()
        self.assertEqual(chosen["path"], CREATION_SELECTION_PATH)
        self.assertFalse(chosen["stale"])
        self.assertIsNone(chosen["selection"]["brief_hash"])
        self.assertEqual(CreativeBriefStore(self.project).get_selection(), chosen)
        self.assertEqual(self.project.fingerprint(), self.before)
        self.assertEqual(len(self.documents.history(CREATION_SELECTION_PATH)), 1)

    def test_selection_rejects_non_outline_sources_stale_version_and_empty_text(self):
        doc = self.outline()
        for path in ("chapters/ch001.md", "memory/events/x.md", "reviews/other.md", CREATIVE_BRIEF_PATH,
                     "outlines/proposals/../escape.md", "config/api.local.json"):
            with self.subTest(path=path), self.assertRaises(VibeNovelError):
                self.creation.select(path, doc["hash"], doc["version_id"], expected_hash=None, expected_version_id=None)
        with self.assertRaises(ConflictError):
            self.creation.select(doc["path"], doc["hash"], "0" * 32, expected_hash=None, expected_version_id=None)
        empty = self.documents.save("outlines/source/empty.md", " \n", None)
        empty["version_id"] = self.documents.history(empty["path"])[0]["id"]
        with self.assertRaises(VibeNovelError):
            self.choose(empty)

    def test_selection_cannot_revive_after_document_history_round_trip(self):
        doc = self.outline()
        chosen = self.choose(doc)
        changed = self.documents.save(doc["path"], "新版本", doc["hash"])
        self.documents.save(doc["path"], doc["content"], changed["hash"])
        stale = self.creation.get_selection()
        self.assertTrue(stale["stale"])
        self.assertNotEqual(stale["version_id"], chosen["version_id"])
        self.assertIsNotNone(stale["selection"]["invalidated_at"])

    def test_selection_observes_deletion_and_remains_stale_after_restore(self):
        doc = self.outline()
        self.choose(doc)
        self.project.path(doc["path"]).unlink()
        self.assertTrue(self.creation.get_selection()["stale"])
        self.project.path(doc["path"]).write_bytes(doc["content"].encode("utf-8"))
        self.assertTrue(CreativeBriefStore(self.project).get_selection()["stale"])

    def test_generated_selection_requires_current_brief_and_preserves_association(self):
        brief = self.save()
        doc = self.outline()
        chosen = self.creation.select(doc["path"], doc["hash"], doc["version_id"],
                                      brief_hash=brief["hash"], brief_version_id=brief["version_id"],
                                      job_id="a" * 32, expected_hash=None, expected_version_id=None)
        self.assertFalse(chosen["stale"])
        newer = copy.deepcopy(brief["brief"])
        newer["story_seed"] = "更新了作者意图"
        self.creation.save(newer, brief["hash"], brief["version_id"])
        stale = self.creation.get_selection()
        self.assertTrue(stale["stale"])
        with self.assertRaises(ConflictError):
            self.creation.select(doc["path"], doc["hash"], doc["version_id"],
                                 brief_hash=brief["hash"], brief_version_id=brief["version_id"],
                                 expected_hash=stale["hash"], expected_version_id=stale["version_id"])
        with self.assertRaises(VibeNovelError):
            self.creation.select(doc["path"], doc["hash"], doc["version_id"], job_id="a" * 32,
                                 expected_hash=stale["hash"], expected_version_id=stale["version_id"])

    def test_selection_concurrent_change_and_cross_project_record_rejected(self):
        doc = self.outline()
        chosen = self.choose(doc)
        replacement = self.choose(doc)
        with self.assertRaises(ConflictError):
            self.creation.select(doc["path"], doc["hash"], doc["version_id"],
                                 expected_hash=chosen["hash"], expected_version_id=chosen["version_id"])
        record = copy.deepcopy(replacement["selection"])
        record["novel_id"] = "another"
        self.documents.save(CREATION_SELECTION_PATH, json_bytes(record).decode("utf-8"), replacement["hash"])
        with self.assertRaises(ConflictError):
            self.creation.get_selection()

    def test_brief_save_journal_recovers_after_content_published(self):
        self.creation.get()  # Initialize storage before simulating the crash.
        original = self.creation.storage._write_state
        failed = False
        def fail_state(state):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated crash")
            return original(state)
        value = defaults("opening")
        value["story_seed"] = "一个等作者审阅的想法"
        with patch.object(self.creation.storage, "_write_state", side_effect=fail_state), self.assertRaises(OSError):
            self.creation.save(value, None, None)
        recovered = CreativeBriefStore(self.project).get()
        self.assertEqual(recovered["brief"], value)
        self.assertEqual(len(self.documents.history(CREATIVE_BRIEF_PATH)), 1)
        self.assertFalse(self.project.path("approvals/.studio/journal.json").exists())


if __name__ == "__main__":
    unittest.main()
