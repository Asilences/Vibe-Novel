"""Author configuration and diagnostic clues never acquire canon authority."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.documents import DocumentStore
from vibenovel.core.files import digest, json_bytes, read_json
from vibenovel.core.locking import ConflictError
from vibenovel.core.project import ProjectStore
from vibenovel.core.toolkit import TOOLKIT_PATH, WritingToolkitStore, _chapter
from vibenovel.errors import VibeNovelError


class WritingToolkitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.projects = ProjectStore(self.workspace)
        self.project = self.projects.init("toolkit", genre="football")
        self.documents = DocumentStore(self.project)
        self.toolkit = WritingToolkitStore(self.project)

    def configuration(self):
        return {"lexicon": [{"id": "goalkeeper", "term": "门将", "aliases": ["守门员"],
                              "forbidden": ["门神"], "required": True, "note": "作者指定用词"}],
                "characters": [{"id": "coach", "name": "陈教练", "aliases": ["老陈"], "max_absence": 2}],
                "threads": [{"id": "letter", "label": "红色信封", "terms": ["红色信封"],
                             "start_chapter": 1, "remind_after": 2, "end_chapter": None}],
                "style": {"instructions": "使用清晰的动作和对话。", "avoid": ["命运的齿轮"], "anchors": []}}

    def chapter(self, name, text):
        path = f"chapters/{name}.md"
        self.project.path(path).write_bytes(text.encode("utf-8"))
        return {"path": path, "hash": digest(text.encode("utf-8"))}

    def test_author_configuration_is_local_optimistic_and_does_not_change_canon(self):
        before = self.project.fingerprint()
        empty = self.toolkit.settings()
        self.assertFalse(empty["configured"])
        self.assertIsNone(empty["hash"])
        selected = self.toolkit.save(self.configuration(), None)
        self.assertTrue(selected["configured"])
        self.assertFalse(selected["stale"])
        self.assertEqual(self.project.fingerprint(), before)
        self.assertEqual(self.toolkit.settings()["hash"], selected["hash"])
        self.assertEqual(list(self.project.path("memory").rglob("*.md")), [])
        with self.assertRaises(ConflictError):
            self.toolkit.save(self.configuration(), None)
        modified = self.configuration()
        modified["lexicon"][0]["note"] = "更新的作者用词约束"
        changed = self.toolkit.save(modified, selected["hash"])
        self.assertNotEqual(changed["hash"], selected["hash"])
        with self.assertRaises(ConflictError):
            self.toolkit.save(self.configuration(), selected["hash"])

    def test_lexicon_rules_keep_exact_lines_and_required_is_advisory(self):
        self.toolkit.save(self.configuration(), None)
        issues = self.toolkit.audit("# 第二章\r\n\r\n守门员冲了出来，旁人称他为门神。\r\n命运的齿轮转动。")
        self.assertEqual({issue["rule"] for issue in issues}, {"lexicon_alias", "lexicon_forbidden", "style_avoid"})
        alias = next(issue for issue in issues if issue["rule"] == "lexicon_alias")
        self.assertEqual(alias["start_line"], 3)
        self.assertEqual(alias["quote"], "守门员冲了出来，旁人称他为门神。")
        self.assertEqual(alias["severity"], "info")
        self.assertTrue(all(issue["severity"] == "warning" for issue in issues if issue["rule"] != "lexicon_alias"))
        required = self.toolkit.audit("陈教练暂时没说话。")
        self.assertEqual([issue["rule"] for issue in required], ["lexicon_required"])
        self.assertEqual(required[0]["severity"], "info")
        self.assertIn("不应", required[0]["suggestion"])
        self.assertEqual(self.toolkit.audit("门将冲了出来。"), [])

    def test_long_line_quotes_contain_the_actual_registered_word(self):
        self.toolkit.save(self.configuration(), None)
        text = "字" * 2500 + "门神" + "字" * 2100 + "守门员命运的齿轮"
        issues = self.toolkit.audit(text)
        for rule, word in (("lexicon_forbidden", "门神"), ("lexicon_alias", "守门员"), ("style_avoid", "命运的齿轮")):
            issue = next(issue for issue in issues if issue["rule"] == rule)
            self.assertIn(word, issue["quote"])
            self.assertLessEqual(len(issue["quote"]), 2000)
            self.assertEqual(issue["start_line"], 1)

    def test_anchor_snapshots_bind_hash_and_line_range_and_drift_is_not_silently_accepted(self):
        source = self.chapter("ch001", "# 第一章\r\n\r\n陈教练把信封放在桌上。\r\n门将没有回头。")
        config = self.configuration()
        config["style"]["anchors"] = [{**source, "start_line": 3, "end_line": 4}]
        before = self.project.fingerprint()
        selected = self.toolkit.save(config, None)
        self.assertEqual(self.project.fingerprint(), before)
        anchor = selected["style"]["anchors"][0]
        self.assertEqual(anchor["quote"], "陈教练把信封放在桌上。\n门将没有回头。")
        self.assertEqual(anchor["hash"], source["hash"])
        self.assertFalse(anchor["stale"])
        saved = self.project.path(TOOLKIT_PATH).read_bytes()
        self.project.path(source["path"]).write_bytes("章节开头插入了一行。\n新内容。".encode("utf-8"))
        stale = self.toolkit.settings()
        self.assertTrue(stale["stale"])
        self.assertTrue(stale["style"]["anchors"][0]["stale"])
        self.assertEqual(stale["style"]["anchors"][0]["hash"], source["hash"])
        self.assertEqual(stale["style"]["anchors"][0]["quote"], anchor["quote"])
        self.assertEqual(self.project.path(TOOLKIT_PATH).read_bytes(), saved)
        with self.assertRaises(ConflictError):
            self.toolkit.save(config, selected["hash"])
        self.project.path(source["path"]).unlink()
        self.assertIsNone(self.toolkit.settings()["style"]["anchors"][0]["current_hash"])

    def test_anchor_forged_quote_is_stale_even_when_source_hash_matches(self):
        source = self.chapter("ch001", "陈教练检查球场。\n门将走了过来。")
        config = self.configuration()
        config["style"]["anchors"] = [{**source, "start_line": 1, "end_line": 1}]
        self.toolkit.save(config, None)
        stored = read_json(self.project.path(TOOLKIT_PATH))
        stored["data"]["style"]["anchors"][0]["quote"] = "伪造但保留正确来源hash的原句。"
        self.project.path(TOOLKIT_PATH).write_bytes(json_bytes(stored))
        self.assertTrue(self.toolkit.settings()["style"]["anchors"][0]["stale"])

    def test_invalid_registration_and_foreign_anchor_paths_are_rejected(self):
        original = self.configuration()
        mutations = []
        bad = copy.deepcopy(original)
        bad["unexpected"] = True
        mutations.append(bad)
        bad = copy.deepcopy(original)
        bad["lexicon"][0]["required"] = 1
        mutations.append(bad)
        bad = copy.deepcopy(original)
        bad["characters"][0]["max_absence"] = True
        mutations.append(bad)
        bad = copy.deepcopy(original)
        bad["lexicon"][0]["aliases"] = ["门将"]
        mutations.append(bad)
        bad = copy.deepcopy(original)
        bad["lexicon"][0]["aliases"] = ["守门员", "守门员"]
        mutations.append(bad)
        bad = copy.deepcopy(original)
        bad["threads"][0]["end_chapter"] = 0
        mutations.append(bad)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(VibeNovelError):
                self.toolkit.save(value, None)
        for path in ("../another/chapters/ch001.md", "bible/rules.md", "memory/events/ch001.md"):
            config = self.configuration()
            config["style"]["anchors"] = [{"path": path, "hash": "0" * 64, "start_line": 1, "end_line": 1}]
            with self.subTest(path=path), self.assertRaises(VibeNovelError):
                self.toolkit.save(config, None)
        self.assertFalse(self.toolkit.settings()["configured"])

    def test_structure_counts_only_this_novels_formal_evidence(self):
        first = self.chapter("ch001", "# 第一章\r\n\r\n老陈把红色信封放在桌上。")
        self.chapter("ch004", "门将等待开球。")
        self.documents.save("drafts/ch005.md", "陈教练拆开红色信封。", None)
        self.project.path("memory/other.md").write_text("陈教练打开红色信封。", encoding="utf-8")
        other = self.projects.init("other", genre="football")
        other.path("chapters/ch009.md").write_text("陈教练带来了红色信封。", encoding="utf-8")
        self.toolkit.save(self.configuration(), None)
        before = self.project.fingerprint()
        report = self.toolkit.structure()
        self.assertEqual(report["scanned"], 2)
        character = report["characters"][0]
        self.assertEqual(character["mentioned_chapters"], [1])
        self.assertEqual(character["last_mentioned"], 1)
        self.assertEqual(character["absence"], 3)
        self.assertEqual(character["status"], "attention")
        self.assertEqual(character["evidence"], [{**first, "start_line": 3, "end_line": 3,
                                                  "quote": "老陈把红色信封放在桌上。"}])
        thread = report["threads"][0]
        self.assertEqual(thread["span"], 3)
        self.assertEqual(thread["last_mentioned"], 1)
        self.assertTrue(any("提及不等于" in item for item in report["limitations"]))
        self.assertEqual(self.project.fingerprint(), before)

    def test_unknown_chapter_number_is_explicitly_omitted_from_span_calculation(self):
        self.chapter("special_20261019", "陈教练打开了红色信封。")
        self.chapter("ch004_v1", "门将正在训练。")
        self.toolkit.save(self.configuration(), None)
        report = self.toolkit.structure()
        self.assertIn({"path": "chapters/special_20261019.md", "reason": "chapter_number_unknown_for_span"}, report["omitted"])
        self.assertEqual(report["characters"][0]["status"], "not_found")
        self.assertIsNone(report["characters"][0]["absence"])
        self.assertEqual(report["threads"][0]["span"], 3)
        for name, expected in (("ch004_v1.md", 4), ("ch004_20261019.md", 4),
                               ("20261019.md", None), ("interlude_4.md", None), ("ch000.md", None)):
            self.assertEqual(_chapter(name), expected)

    def test_history_limits_are_visible_and_future_registration_stays_planned(self):
        self.chapter("ch001", "陈教练站在场边。")
        self.chapter("ch002", "门将检查球鞋。")
        settings = self.configuration()
        settings["threads"][0]["start_chapter"] = 5
        self.toolkit.save(settings, None)
        with patch("vibenovel.core.analysis.MAX_HISTORY_FILES", 1):
            report = self.toolkit.structure()
        self.assertEqual(report["scanned"], 1)
        self.assertIn({"path": "chapters/ch002.md", "reason": "file_limit"}, report["omitted"])
        self.assertEqual(report["threads"][0]["status"], "planned")
        self.assertEqual(report["threads"][0]["evidence"], [])


if __name__ == "__main__":
    unittest.main()
