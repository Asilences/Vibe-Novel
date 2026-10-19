"""Source integrity and the author-controlled planning approval lifecycle."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibenovel.core.approvals import ApprovalStore, IMMUTABLE_FIELDS
from vibenovel.core.blueprints import APPROVED_BLUEPRINT, PlanningStore, prepare_blueprint
from vibenovel.core.documents import ConflictError, DocumentStore, STATE_PATH
from vibenovel.core.files import digest, json_bytes, read_json
from vibenovel.core.imports import import_outline
from vibenovel.core.project import ProjectStore
from vibenovel.errors import VibeNovelError


def record(identifier, **extra):
    return {"id": identifier, "title": identifier, "summary": "待作者确认的未来策划",
            "basis": "suggested", "evidence": [], **extra}


def blueprint():
    return {"schema_version": 1, "book": record("book"),
            "volumes": [record("volume-one", number=1, chapter_start=1, chapter_end=None)],
            "chapters": [record("chapter-one", number=1, volume_id="volume-one", goal="寻找入口",
                                conflict="入口被封", turn="选择绕行", outcome="付出时间代价",
                                beats=["选择绕行并失去先机"], character_ids=["hero"],
                                depends_on=[], thread_ids=["thread-key"])],
            "characters": [record("hero", role="主角", knowledge="计划在第一章看见入口")],
            "relationships": [],
            "threads": [record("thread-key", setup_chapter=1, payoff_chapter=3)],
            "timeline": [record("first-event", time="第一天傍晚", chapter=1,
                                character_ids=["hero"], status="planned")],
            "settings": [record("gate", category="location")],
            "style": record("style", rules=["以行动推进选择"], avoid=["重复解释设定"]),
            "unknowns": ["绕行路线待作者确认"]}


class BlueprintTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.project = ProjectStore(self.workspace).init("planning-test", genre="adventure")
        self.documents = DocumentStore(self.project)
        self.approvals = ApprovalStore(self.project)
        self.planning = PlanningStore(self.project)
        self.source = self.documents.save("outlines/source/story.md", "# 来源\n主角计划寻找入口。\n", None)
        self.hashes = {self.source["path"]: self.source["hash"]}

    def candidate(self, data=None, name="blueprint-candidate.json"):
        content = prepare_blueprint(self.project, json.dumps(data or blueprint(), ensure_ascii=False), self.hashes)
        return self.documents.save("outlines/proposals/" + name, content, None, "AI_SUGGESTED")

    def apply(self, proposal):
        self.approvals.decide(proposal["id"], "approve")
        return self.approvals.apply(proposal["id"])

    def approved_package(self):
        candidate = self.candidate()
        self.apply(self.planning.propose(candidate["path"], candidate["hash"]))
        return self.planning.read(APPROVED_BLUEPRINT)

    def chapter_proposal(self):
        package = self.approved_package()
        return self.planning.propose(package["path"], package["hash"], "chapter-one")

    def test_normalization_runtime_identity_and_no_formal_memory_pollution(self):
        data = blueprint()
        data["source_hashes"] = {"../../secret.md": "fake"}
        data["book"]["basis"] = "source_asserted"
        data["book"]["evidence"] = [{"path": self.source["path"], "start_line": 1, "end_line": 2,
                                    "quote": "# 来源\n主角计划寻找入口。"}]
        before = self.project.fingerprint()
        prepared = prepare_blueprint(self.project, "```json\n" + json.dumps(data) + "\n```", self.hashes)
        decoded = json.loads(prepared)
        self.assertEqual(decoded["novel_id"], self.project.novel_id)
        self.assertEqual(decoded["source_hashes"], self.hashes)
        self.assertEqual(decoded["timeline"][0]["status"], "planned")
        self.assertEqual(before, self.project.fingerprint())
        self.assertEqual(list(self.project.path("memory").rglob("*.md")), [])
        self.assertEqual(list(self.project.path("chapters").iterdir()), [])
        data["novel_id"] = "other-story"
        with self.assertRaises(VibeNovelError):
            prepare_blueprint(self.project, json.dumps(data), self.hashes)

    def test_duplicate_json_keys_nonfinite_and_extra_or_missing_fields_rejected(self):
        raw = json.dumps(blueprint())
        invalid = [raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
                   raw.replace('"schema_version": 1', '"schema_version": NaN'),
                   raw.replace('"schema_version": 1', '"schema_version": 1e999'),
                   '{"value": "\\ud800"}', '[1, 2]']
        for content in invalid:
            with self.subTest(content=content[:90]), self.assertRaises(VibeNovelError):
                prepare_blueprint(self.project, content, self.hashes)
        for update in ({"approved": True}, {"schema_version": True}, {"schema_version": 2}):
            data = blueprint()
            data.update(update)
            with self.subTest(update=update), self.assertRaises(VibeNovelError):
                prepare_blueprint(self.project, json.dumps(data), self.hashes)
        data = blueprint()
        del data["book"]["summary"]
        with self.assertRaises(VibeNovelError):
            prepare_blueprint(self.project, json.dumps(data), self.hashes)

    def test_forged_quote_line_range_and_unbound_sources_fail(self):
        evidence = {"path": self.source["path"], "start_line": 2, "end_line": 2, "quote": "主角计划寻找入口。"}
        for update in ({"quote": "主角已经到达入口。"}, {"start_line": 1}, {"end_line": 999},
                       {"start_line": True}, {"path": "outlines/source/unselected.md"},
                       {"path": "../escape.md"}, {"author_approved": True}):
            data = blueprint()
            data["book"].update(basis="source_asserted", evidence=[{**evidence, **update}])
            with self.subTest(update=update), self.assertRaises(VibeNovelError):
                prepare_blueprint(self.project, json.dumps(data), self.hashes)
        data = blueprint()
        data["book"]["basis"] = "source_asserted"
        with self.assertRaises(VibeNovelError):
            prepare_blueprint(self.project, json.dumps(data), self.hashes)

    def test_ids_references_ranges_and_planned_status_are_checked(self):
        mutations = [lambda d: d["characters"][0].update(id="book"),
                     lambda d: d["chapters"][0].update(volume_id="absent"),
                     lambda d: d["chapters"][0].update(character_ids=["absent"]),
                     lambda d: d["chapters"][0].update(depends_on=["chapter-one"]),
                     lambda d: d["chapters"][0].update(thread_ids=["absent"]),
                     lambda d: d["volumes"][0].update(chapter_start=2),
                     lambda d: d["chapters"][0].update(number=True),
                     lambda d: d["threads"][0].update(payoff_chapter=0),
                     lambda d: d["threads"][0].update(setup_chapter=4),
                     lambda d: d["timeline"][0].update(status="happened"),
                     lambda d: d["settings"][0].update(category="AUTHOR_APPROVED"),
                     lambda d: d["chapters"][0].update(beats=["x"] * 101),
                     lambda d: d["book"].update(summary="x" * 12001),
                     lambda d: d["volumes"].append(record("overlap", number=2, chapter_start=1, chapter_end=2)),
                     lambda d: d["relationships"].append(record("bad-link", from_id="hero", to_id="absent"))]
        for index, change in enumerate(mutations):
            data = blueprint()
            change(data)
            with self.subTest(index=index), self.assertRaises(VibeNovelError):
                prepare_blueprint(self.project, json.dumps(data), self.hashes)
        data = blueprint()
        data["chapters"].append(record("future", number=2, volume_id="volume-one", goal="目标", conflict="障碍",
                                       turn="变化", outcome="代价", beats=[], character_ids=[], depends_on=[], thread_ids=[]))
        data["chapters"][0]["depends_on"] = ["future"]
        with self.assertRaises(VibeNovelError):
            prepare_blueprint(self.project, json.dumps(data), self.hashes)

    def test_sources_are_path_whitelisted_and_import_bundles_verified(self):
        for hashes in ({"../escape.md": self.source["hash"]}, {"project.json": self.source["hash"]},
                       {self.source["path"]: "bad"}, {self.source["path"]: None}):
            with self.subTest(hashes=hashes), self.assertRaises(VibeNovelError):
                prepare_blueprint(self.project, json.dumps(blueprint()), hashes)
        original = self.workspace / "external.txt"
        original.write_text("原件内容", encoding="utf-8")
        imported = import_outline(self.project, original)
        source_path = f"imports/original/{imported['import_id']}/extracted.md"
        document = self.documents.read(source_path)
        hashes = {source_path: document["hash"]}
        prepare_blueprint(self.project, json.dumps(blueprint()), hashes)
        self.project.path(source_path).write_bytes(b"tampered")
        with self.assertRaises(ConflictError):
            prepare_blueprint(self.project, json.dumps(blueprint()), hashes)

    def test_bad_and_stale_candidates_remain_visible(self):
        candidate = self.candidate()
        malformed = "outlines/proposals/blueprint-malformed.json"
        self.project.path(malformed).write_text('{"broken": ', encoding="utf-8")
        badschema = "outlines/proposals/blueprint-badschema.json"
        self.project.path(badschema).write_text('{"book": "inspect me"}', encoding="utf-8")
        self.documents.save(self.source["path"], "changed source", self.source["hash"])
        result = {item["path"]: item for item in self.planning.list()}
        self.assertEqual(set(result), {candidate["path"], malformed, badschema})
        self.assertTrue(result[candidate["path"]]["stale"])
        self.assertFalse(result[candidate["path"]]["approved"])
        self.assertTrue(all("data" not in item and "content" not in item for item in result.values()))
        self.assertEqual(self.planning.read(candidate["path"])["data"]["book"]["id"], "book")
        self.assertIsNone(self.planning.read(malformed)["data"])
        self.assertEqual(self.planning.read(badschema)["data"], {"book": "inspect me"})
        self.assertTrue(all(item["issues"] for item in result.values()))
        with self.assertRaises(ConflictError):
            self.planning.propose(candidate["path"], candidate["hash"])

    def test_proposal_rechecks_source_history_after_read_before_create(self):
        candidate = self.candidate()
        original_create = ApprovalStore.create

        def change_before_create(store, *args, **kwargs):
            changed = self.documents.save(self.source["path"], "临时变更", self.source["hash"])
            restored = self.documents.save(self.source["path"], self.source["content"], changed["hash"])
            self.assertEqual(restored["hash"], self.source["hash"])
            return original_create(store, *args, **kwargs)

        with patch.object(ApprovalStore, "create", new=change_before_create), self.assertRaises(ConflictError):
            self.planning.propose(candidate["path"], candidate["hash"])
        self.assertEqual(self.approvals.list(), [])

    def test_only_applied_exact_package_can_extract_and_never_auto_applies(self):
        candidate = self.candidate()
        with self.assertRaises(ConflictError):
            self.planning.propose(candidate["path"], candidate["hash"], "chapter-one")
        proposal = self.planning.propose(candidate["path"], candidate["hash"])
        self.assertEqual(proposal["state"], "pending")
        self.assertFalse(self.project.path(APPROVED_BLUEPRINT).exists())
        self.approvals.decide(proposal["id"], "approve")
        self.assertFalse(self.project.path(APPROVED_BLUEPRINT).exists())
        self.approvals.apply(proposal["id"])
        package = self.planning.read(APPROVED_BLUEPRINT)
        self.assertTrue(package["approved"] and package["valid"])
        with self.assertRaises(ConflictError):
            self.planning.propose(APPROVED_BLUEPRINT, "0" * 64, "chapter-one")
        chapter = self.planning.propose(APPROVED_BLUEPRINT, package["hash"], "chapter-one")
        self.assertEqual(chapter["target"], "outlines/approved/ch001.md")
        self.assertEqual(chapter["dependencies"][APPROVED_BLUEPRINT], package["hash"])
        self.assertIn("未来计划", chapter["after"])
        self.assertIn("寻找入口", chapter["after"])
        self.assertFalse(self.project.path(chapter["target"]).exists())
        self.apply(chapter)
        self.assertEqual(self.approvals.approved_outline(chapter["target"])["approval_id"], chapter["id"])
        self.assertEqual(list(self.project.path("memory").rglob("*.md")), [])

    def test_manually_copied_package_and_modified_package_are_not_approved(self):
        candidate = self.candidate()
        self.project.path(APPROVED_BLUEPRINT).write_bytes(candidate["content"].encode("utf-8"))
        self.assertFalse(self.planning.read(APPROVED_BLUEPRINT)["approved"])
        with self.assertRaises(ConflictError):
            self.planning.propose(APPROVED_BLUEPRINT, candidate["hash"], "chapter-one")
        self.apply(self.planning.propose(candidate["path"], candidate["hash"]))
        data = json.loads(candidate["content"])
        data["book"]["summary"] = "external valid change"
        self.project.path(APPROVED_BLUEPRINT).write_bytes(json_bytes(data))
        package = self.planning.read(APPROVED_BLUEPRINT)
        self.assertTrue(package["valid"])
        self.assertFalse(package["approved"])

    def test_source_drift_invalidates_create_approval_application_and_outline(self):
        pending = self.approvals.create("bible/proposal.md", "proposed", "理由", ["影响"], None,
                                        dependencies=self.hashes)
        ready = self.approvals.create("bible/ready.md", "proposed", "理由", ["影响"], None,
                                      dependencies=self.hashes)
        self.approvals.decide(ready["id"], "approve")
        chapter = self.chapter_proposal()
        self.apply(chapter)
        self.documents.save(self.source["path"], "changed", self.source["hash"])
        for action in (lambda: self.approvals.create("bible/new.md", "x", "理由", ["影响"], None, dependencies=self.hashes),
                       lambda: self.approvals.decide(pending["id"], "approve"),
                       lambda: self.approvals.apply(ready["id"]),
                       lambda: self.approvals.approved_outline(chapter["target"])):
            with self.assertRaises(ConflictError):
                action()
        package = self.planning.read(APPROVED_BLUEPRINT)
        self.assertTrue(package["stale"])
        self.assertFalse(package["valid"] or package["approved"])

    def test_applied_parent_change_and_A_B_A_never_revive_old_chapter(self):
        chapter = self.chapter_proposal()
        self.apply(chapter)
        original = self.documents.read(APPROVED_BLUEPRINT)
        updated = json.loads(original["content"])
        updated["book"]["summary"] = "new future direction"
        change = self.approvals.create(APPROVED_BLUEPRINT, json_bytes(updated).decode(), "调整总纲", ["后续章计划"], original["hash"])
        self.apply(change)
        with self.assertRaises(ConflictError):
            self.approvals.approved_outline(chapter["target"])
        restore = self.approvals.create(APPROVED_BLUEPRINT, original["content"], "恢复原方案", ["后续章计划"], change["after_hash"])
        self.apply(restore)
        self.assertEqual(self.documents.read(APPROVED_BLUEPRINT)["hash"], original["hash"])
        with self.assertRaises(ConflictError):
            self.approvals.approved_outline(chapter["target"])

    def test_source_app_A_B_A_does_not_revive_applied_package(self):
        package = self.approved_package()
        changed = self.documents.save(self.source["path"], "B", self.source["hash"])
        self.documents.save(self.source["path"], self.source["content"], changed["hash"])
        current = self.planning.read(APPROVED_BLUEPRINT)
        self.assertEqual(current["hash"], package["hash"])
        self.assertTrue(current["stale"])
        self.assertFalse(current["approved"] or current["valid"])

    def test_reapproved_identical_chapter_uses_latest_revision_not_uuid_order(self):
        # State serialization sorts UUID keys; a fresh approval may sort first.
        with patch("vibenovel.core.approvals.uuid4", side_effect=[
                SimpleNamespace(hex=letter * 32) for letter in ("f", "e", "b", "a")]):
            old = self.chapter_proposal()
            self.apply(old)
            updated = blueprint()
            updated["book"]["summary"] = "调整全书方向，章计划文字仍相同"
            candidate = self.candidate(updated, name="blueprint-updated.json")
            parent = self.apply(self.planning.propose(candidate["path"], candidate["hash"]))
            package = self.planning.read(APPROVED_BLUEPRINT)
            fresh = self.planning.propose(APPROVED_BLUEPRINT, package["hash"], "chapter-one")
            self.apply(fresh)
        self.assertEqual(old["after_hash"], fresh["after_hash"])
        selected = self.approvals.approved_outline(fresh["target"])
        self.assertEqual(selected["approval_id"], fresh["id"])
        state = read_json(self.project.path(STATE_PATH), max_bytes=64 * 1024 * 1024)
        expected = digest(json_bytes({"history": state["history"][APPROVED_BLUEPRINT][-1]["id"],
                                      "approval": parent["id"]}))
        self.assertEqual(fresh["dependency_versions"][APPROVED_BLUEPRINT], expected)

    def test_reapproval_after_source_restore_refreshes_identical_package(self):
        with patch("vibenovel.core.approvals.uuid4", side_effect=[
                SimpleNamespace(hex=letter * 32) for letter in ("f", "a")]):
            original = self.approved_package()
            changed = self.documents.save(self.source["path"], "B", self.source["hash"])
            self.documents.save(self.source["path"], self.source["content"], changed["hash"])
            self.assertTrue(self.planning.read(APPROVED_BLUEPRINT)["stale"])
            candidate = self.documents.read("outlines/proposals/blueprint-candidate.json")
            self.apply(self.planning.propose(candidate["path"], candidate["hash"]))
        current = self.planning.read(APPROVED_BLUEPRINT)
        self.assertEqual(current["hash"], original["hash"])
        self.assertTrue(current["approved"] and current["valid"])
        self.assertFalse(current["stale"])

    def test_external_restore_does_not_fall_back_to_an_obsolete_applied_plan(self):
        old = self.approvals.create("outlines/approved/ch001.md", "original", "理由", ["影响"], None)
        self.apply(old)
        fresh = self.approvals.create(old["target"], "revised", "理由", ["影响"], old["after_hash"])
        self.apply(fresh)
        self.project.path(old["target"]).write_text(old["after"], encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.approvals.approved_outline(old["target"])

    def test_dependency_map_tampering_is_hash_bound(self):
        proposal = self.approvals.create("bible/world.md", "new", "理由", ["影响"], None, dependencies=self.hashes)
        state = read_json(self.project.path(STATE_PATH), max_bytes=64 * 1024 * 1024)
        state["proposals"][proposal["id"]]["dependencies"] = {}
        self.project.path(STATE_PATH).write_bytes(json_bytes(state))
        with self.assertRaises(ConflictError):
            self.approvals.decide(proposal["id"], "approve")

    def test_old_approval_hashes_remain_compatible(self):
        old = self.approvals.create("outlines/approved/manual.md", "manual chapter plan", "理由", ["影响"], None)
        self.assertNotIn("dependencies", old)
        self.assertNotIn("dependency_versions", old)
        expected = digest(json_bytes({key: old[key] for key in IMMUTABLE_FIELDS}))
        self.assertEqual(old["content_hash"], expected)
        self.apply(old)
        self.assertEqual(self.approvals.approved_outline(old["target"])["approval_id"], old["id"])

    def test_self_dependency_is_rejected_before_any_proposal_or_canonical_change(self):
        package = self.approved_package()
        count = len(self.approvals.list())
        with self.assertRaisesRegex(VibeNovelError, "目标文件本身"):
            self.approvals.create(APPROVED_BLUEPRINT, package["content"], "理由", ["影响"], package["hash"],
                                  dependencies={APPROVED_BLUEPRINT: package["hash"]})
        content = prepare_blueprint(self.project, json.dumps(blueprint()), {APPROVED_BLUEPRINT: package["hash"]})
        candidate = self.documents.save("outlines/proposals/blueprint-self.json", content, None)
        with self.assertRaisesRegex(VibeNovelError, "目标不能同时作为来源"):
            self.planning.propose(candidate["path"], candidate["hash"])
        self.assertEqual(len(self.approvals.list()), count)
        self.assertEqual(self.documents.read(APPROVED_BLUEPRINT)["hash"], package["hash"])

    def test_extracting_chapter_cannot_overwrite_its_own_bound_source(self):
        source_plan = self.approvals.create("outlines/approved/ch001.md", "旧章计划", "理由", ["影响"], None)
        self.apply(source_plan)
        content = prepare_blueprint(self.project, json.dumps(blueprint()),
                                    {source_plan["target"]: source_plan["after_hash"]})
        candidate = self.documents.save("outlines/proposals/blueprint-chapter-source.json", content, None)
        self.apply(self.planning.propose(candidate["path"], candidate["hash"]))
        package = self.planning.read(APPROVED_BLUEPRINT)
        with self.assertRaisesRegex(VibeNovelError, "目标不能同时作为来源"):
            self.planning.propose(APPROVED_BLUEPRINT, package["hash"], "chapter-one")
        self.assertEqual(self.documents.read(source_plan["target"])["content"], "旧章计划")

    def test_stale_source_approval_can_be_revoked_with_original_decision_audited(self):
        proposal = self.approvals.create("outlines/approved/ch001.md", "计划候选", "理由", ["影响"], None,
                                         dependencies=self.hashes)
        approved = self.approvals.decide(proposal["id"], "approve", "同意原版")
        self.documents.save(self.source["path"], "新来源版本", self.source["hash"])
        with self.assertRaises(ConflictError):
            self.approvals.apply(proposal["id"])
        revoked = self.approvals.decide(proposal["id"], "reject", "撤销过期批准")
        self.assertEqual(revoked["state"], "rejected")
        self.assertEqual(revoked["decision_history"], [approved["decision"]])
        self.assertEqual(revoked["decision"]["action"], "reject")
        self.assertEqual(revoked["content_hash"], approved["content_hash"])
        state = read_json(self.project.path(STATE_PATH), max_bytes=64 * 1024 * 1024)
        self.assertEqual(state["events"][-1]["kind"], "proposal_approval_revoked")
        self.assertEqual(state["events"][-1]["proposal_id"], proposal["id"])
        self.assertFalse(self.project.path(proposal["target"]).exists())
        with self.assertRaises(ConflictError):
            self.approvals.apply(proposal["id"])
        with self.assertRaises(ConflictError):
            self.approvals.decide(proposal["id"], "approve")

    def test_stale_parent_approval_can_be_revoked(self):
        chapter = self.chapter_proposal()
        self.approvals.decide(chapter["id"], "approve")
        parent = self.documents.read(APPROVED_BLUEPRINT)
        changed = json.loads(parent["content"])
        changed["book"]["summary"] = "新总纲方向"
        self.apply(self.approvals.create(APPROVED_BLUEPRINT, json_bytes(changed).decode(),
                                         "调整总纲", ["本章规划"], parent["hash"]))
        with self.assertRaises(ConflictError):
            self.approvals.apply(chapter["id"])
        revoked = self.approvals.decide(chapter["id"], "reject")
        self.assertEqual(revoked["state"], "rejected")
        self.assertEqual(revoked["decision_history"][0]["action"], "approve")
        with self.assertRaises(ConflictError):
            self.approvals.apply(chapter["id"])

    def test_legacy_approved_proposal_can_be_revoked_but_applied_cannot(self):
        old = self.approvals.create("outlines/approved/manual.md", "manual", "理由", ["影响"], None)
        approved = self.approvals.decide(old["id"], "approve")
        with self.assertRaises(ConflictError):
            self.approvals.decide(old["id"], "approve")
        revoked = self.approvals.decide(old["id"], "reject")
        self.assertNotIn("dependencies", revoked)
        self.assertEqual(revoked["decision_history"], [approved["decision"]])
        with self.assertRaises(ConflictError):
            self.approvals.apply(old["id"])
        fresh = self.approvals.create("outlines/approved/manual.md", "applied manual", "理由", ["影响"], None)
        applied = self.apply(fresh)
        with self.assertRaises(ConflictError):
            self.approvals.decide(applied["id"], "reject")
        self.assertEqual(self.documents.read(applied["target"])["content"], "applied manual")


if __name__ == "__main__":
    unittest.main()
