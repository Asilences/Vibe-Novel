"""Offline continuation contracts: inherited paid work and unknown fees survive.

POST /reference-studies/{id}/continuations/preview then /continuations freezes
the same request plus expected_preview/idempotency_key. Saving never sends.
Unknown-tail permission defaults false (preview only); old unknown account entries stay owned
by the parent, while new sends use the branch ID and an explicit extra cap.
"""
import copy
import unittest

from tests import test_reference_studies as fixtures
from vibenovel.core.files import digest, json_bytes
from vibenovel.core.locking import ConflictError
from vibenovel.core.research_gateway import ResearchGateway
from vibenovel.errors import VibeNovelError
from vibenovel.studio.reference_studies import ReferenceStudiesManager


class LedgerGateway(fixtures.Gateway):
    def __init__(self):
        super().__init__()
        self.unknown = {}
        self.fail_seventh = True

    def generate(self, preview, prompt, cancel, key):
        result = super().generate(preview, prompt, cancel, key)
        if len(self.calls) == 7 and self.fail_seventh:
            attempt_id = digest(key.encode())[:32]
            self.unknown[attempt_id] = {"id": attempt_id, "state": "unknown", "attempt_hash": "a" * 64,
                "cached_response_hash": None, "reserved_cost_usd": preview["estimated_cost_usd"]}
            raise VibeNovelError("人工模拟：已发送但响应与费用未知，不重放")
        return result

    def status(self):
        return {"pending_reconciliations": list(copy.deepcopy(self.unknown).values())}


class ContinuationTests(unittest.TestCase):
    request = fixtures.StudyTests.request
    create = fixtures.StudyTests.create
    run_study = fixtures.StudyTests.run_study

    def setUp(self):
        fixtures.StudyTests.setUp(self)
        self.manager.close()
        raw = "\n".join("第%d章 河岸%d\n小明继续越过第%d条河流，河岸留下足迹。" % (n, n, n) for n in range(1, 15))
        self.book = self.books.import_book("人工十四章.txt", raw.encode(), title="人工十四章")
        self.books.update_policy(self.book["book_id"], expected_hash=self.book["hash"], outbound="excerpts")
        self.gateway = LedgerGateway()
        self.manager = ReferenceStudiesManager(self.workspace, self.gateway)
        self.addCleanup(self.manager.close)

    def interrupted_parent(self):
        study = self.run_study(self.create())
        self.assertEqual((study["state"], study["completed_nodes"]), ("failed", 6), study.get("error"))
        parent = self.manager.store.load(study["id"])
        parent.update(state="interrupted")
        parent["nodes"][6]["state"] = "interrupted"
        parent["plan"]["template_hash"] = "old-template-v1"
        parent["plan_hash"] = digest(json_bytes(parent["plan"]))
        self.manager.store._write(self.manager.store.path(parent["id"] + "/manifest.json"), parent, overwrite=True)
        return parent

    def prepare_branch(self, parent, allow=True, key="continuation-test-key-001"):
        preview = self.manager.preview_continuation(parent["id"], 2, allow, .04, "显式保留旧费后续读")
        result = self.manager.create_continuation(parent["id"], 2, preview["hash"], key, allow, .04, "显式保留旧费后续读")
        return preview, result

    def test_six_paid_results_exactly_inherited_unknown_fee_retained_and_only_tail_sent(self):
        parent = self.interrupted_parent()
        root = self.manager.store.path(parent["id"])
        old_files = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        old_account = copy.deepcopy(self.gateway.unknown)
        old_export = self.manager.store.export_verified(parent["id"])
        preview, branch = self.prepare_branch(parent)
        self.assertEqual((branch["state"], branch["completed_nodes"], branch["total_nodes"]), ("prepared", 6, 14))
        self.assertLessEqual(preview["uncertain_reread_estimated_cost_usd"], .04)
        inherited = self.manager.store.load(branch["id"])
        owner = ResearchGateway(self.workspace)._reconciliation_owner(next(iter(self.gateway.unknown)))
        self.assertEqual(owner["id"], parent["id"])
        self.assertEqual(self.manager.store.export_verified(branch["id"])["results"], old_export["results"])
        for index in range(6):
            node_id = parent["nodes"][index]["id"]
            self.assertEqual(self.manager.store.path(branch["id"] + "/results/" + node_id + ".json").read_bytes(),
                             self.manager.store.path(parent["id"] + "/results/" + node_id + ".json").read_bytes())
            self.assertNotIn("attempt_key", inherited["nodes"][index])
        self.assertEqual(len(self.gateway.calls), 7)
        final = self.run_study(branch)
        self.assertEqual((final["state"], final["completed_nodes"]), ("succeeded", 14), final.get("error"))
        self.assertEqual(len(self.gateway.calls), 15)  # original 7 + remaining 8
        self.assertNotEqual(self.gateway.calls[6][1], self.gateway.calls[7][1])
        self.assertIn('"e6"', self.gateway.calls[7][0])
        self.assertEqual(self.gateway.unknown, old_account)
        self.assertEqual({path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}, old_files)
        exported = self.manager.store.export_verified(branch["id"])
        self.assertEqual(exported["results"][:6], old_export["results"])
        self.assertEqual(exported["results"][6]["temporal_mode"], "continuation_read")
        self.assertEqual(self.manager.store.node_result(self.manager.store.load(branch["id"]), 6)["prior_checkpoint_hash"], parent["nodes"][5]["result_hash"])

    def test_false_permission_only_previews_and_single_parent_has_one_branch(self):
        parent = self.interrupted_parent()
        preview = self.manager.preview_continuation(parent["id"], 2, False, .04, "显式保留旧费后续读")
        self.assertTrue(preview["requires_uncertain_reread_permission"])
        with self.assertRaisesRegex(ConflictError, "尚未明确允许"):
            self.manager.create_continuation(parent["id"], 2, preview["hash"], "continuation-test-key-001", False, .04, "显式保留旧费后续读")
        self.assertFalse(any(item["id"] != parent["id"] for item in self.manager.store.list_studies()))
        _, branch = self.prepare_branch(parent, True)
        _, same = self.prepare_branch(parent, True)
        self.assertEqual(same["id"], branch["id"])
        with self.assertRaisesRegex(ConflictError, "已有续读分支"):
            self.prepare_branch(parent, True, "another-continuation-key1")
        self.assertEqual(len(self.gateway.calls), 7)

    def _late_change_blocks_publication_and_repair(self, change):
        parent = self.interrupted_parent()
        _, branch = self.prepare_branch(parent)
        def mutate_during_new_request():
            if len(self.gateway.calls) != 8:
                return
            if change == "parent":
                current = self.manager.store.load(parent["id"])
                current["error"] = "在新请求返回前核对了父工程"
                self.manager.store.save(current)
            else:
                next(iter(self.gateway.unknown.values()))["attempt_hash"] = "d" * 64
        self.gateway.callback = mutate_during_new_request
        final = self.run_study(branch)
        self.assertEqual((final["state"], final["completed_nodes"]), ("failed", 6), final.get("error"))
        self.assertEqual(len(self.manager.store.export_verified(branch["id"])["results"]), 6)
        response = self.manager.response(branch["id"], "read-00007")
        self.assertTrue(response["text"])
        with self.assertRaises(ConflictError):
            self.manager.repair_response(branch["id"], "read-00007", response["hash"], response["text"], "不能借离线修复绕过失效父来源")
        self.assertEqual(self.manager.detail(branch["id"])["completed_nodes"], 6)
        self.assertFalse(self.manager.store.path(branch["id"] + "/results/read-00007.json").exists())
        self.assertEqual(len(self.gateway.calls), 8)

    def test_late_parent_change_preserves_response_without_publishing_or_repair_bypass(self):
        self._late_change_blocks_publication_and_repair("parent")

    def test_late_unknown_account_change_preserves_response_without_publishing_or_repair_bypass(self):
        self._late_change_blocks_publication_and_repair("account")

    def test_original_cache_or_reserved_state_forbids_new_paid_branch(self):
        parent = self.interrupted_parent()
        attempt = next(iter(self.gateway.unknown.values()))
        attempt["state"] = "reserved"
        with self.assertRaisesRegex(ConflictError, "已核对为unknown"):
            self.manager.preview_continuation(parent["id"], 2, True)
        attempt["state"] = "unknown"
        attempt["cached_response_hash"] = "f" * 64
        with self.assertRaises(ConflictError):
            self.manager.preview_continuation(parent["id"], 2, True)
        attempt["cached_response_hash"] = None
        self.manager.store._write(self.manager.store.path(parent["id"] + "/responses/read-00007.json"), {"text": "已有原响应"})
        with self.assertRaisesRegex(ConflictError, "已有原响应"):
            self.manager.preview_continuation(parent["id"], 2, True)
        self.assertEqual(len(self.gateway.calls), 7)

    def test_budget_permission_or_account_drift_invalidates_preview_without_sends(self):
        parent = self.interrupted_parent()
        preview = self.manager.preview_continuation(parent["id"], 2, False)
        with self.assertRaises(ConflictError):
            self.manager.create_continuation(parent["id"], 2, preview["hash"], "changed-permission-key1", True)
        with self.assertRaisesRegex(VibeNovelError, "超过明确上限"):
            self.manager.preview_continuation(parent["id"], 2, True, .00000001)
        preview = self.manager.preview_continuation(parent["id"], 2, True)
        next(iter(self.gateway.unknown.values()))["attempt_hash"] = "b" * 64
        with self.assertRaises(ConflictError):
            self.manager.create_continuation(parent["id"], 2, preview["hash"], "changed-account-key-01", True)
        self.assertEqual(len(self.gateway.calls), 7)

    def test_parent_or_inherited_result_tampering_blocks_start_and_new_sends(self):
        parent = self.interrupted_parent()
        _, branch = self.prepare_branch(parent)
        inherited = self.manager.store.path(branch["id"] + "/results/read-00001.json")
        inherited.write_bytes(inherited.read_bytes().replace("本段观察".encode(), "伪造观察".encode()))
        with self.assertRaisesRegex(ConflictError, "被修改"):
            self.manager.control(branch["id"], "start")
        self.assertEqual(len(self.gateway.calls), 7)

    def test_parent_state_or_account_change_after_preparation_blocks_start(self):
        parent = self.interrupted_parent()
        _, branch = self.prepare_branch(parent)
        pending = next(iter(self.gateway.unknown.values()))
        pending["cached_response_hash"] = "c" * 64
        with self.assertRaises(ConflictError):
            self.manager.control(branch["id"], "start")
        pending["cached_response_hash"] = None
        changed = self.manager.store.load(parent["id"])
        changed["error"] = "原工程经人工核对后变化"
        self.manager.store.save(changed)
        with self.assertRaisesRegex(ConflictError, "父工程已变化"):
            self.manager.control(branch["id"], "start")
        self.assertEqual(len(self.gateway.calls), 7)

    def test_restart_keeps_inheritance_and_same_key_commit_is_idempotent(self):
        parent = self.interrupted_parent()
        preview, branch = self.prepare_branch(parent)
        restarted = ReferenceStudiesManager(self.workspace, self.gateway)
        self.addCleanup(restarted.close)
        same = restarted.create_continuation(parent["id"], 2, preview["hash"], "continuation-test-key-001", True, .04, "显式保留旧费后续读")
        self.assertEqual(same["id"], branch["id"])
        with self.assertRaises(ConflictError):
            restarted.create_continuation(parent["id"], 3, preview["hash"], "continuation-test-key-001", True, .04, "显式保留旧费后续读")
        self.assertEqual(len(self.gateway.calls), 7)

    def test_http_preview_commit_csrf_and_strict_boolean_do_not_send(self):
        from fastapi.testclient import TestClient
        from vibenovel.studio.server import create_app
        parent = self.interrupted_parent()
        app = create_app(self.workspace)
        app.state.reference_studies.gateway = self.gateway
        path = "/api/reference-studies/" + parent["id"] + "/continuations"
        payload = {"budget_max_usd": 2., "allow_uncertain_reread": True,
                   "uncertain_reread_max_usd": .04, "reason": "显式保留旧费后续读"}
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            headers = {"X-VibeNovel-Token": client.get("/api/bootstrap").json()["csrf_token"]}
            self.assertEqual(client.post(path + "/preview", json=payload).status_code, 403)
            denied = client.post(path + "/preview", json={**payload, "allow_uncertain_reread": "true"}, headers=headers)
            self.assertEqual(denied.status_code, 422)
            quoted = client.post(path + "/preview", json=payload, headers=headers)
            self.assertEqual(quoted.status_code, 200, quoted.text)
            commit = {**payload, "expected_preview": quoted.json()["hash"], "idempotency_key": "http-continuation-key1"}
            saved = client.post(path, json=commit, headers=headers)
            self.assertEqual(saved.status_code, 201, saved.text)
            self.assertEqual((saved.json()["state"], saved.json()["completed_nodes"]), ("prepared", 6))
            again = client.post(path, json=commit, headers=headers)
            self.assertEqual(again.status_code, 201, again.text)
            self.assertEqual(again.json()["id"], saved.json()["id"])
            self.assertEqual(saved.json()["continuation"]["unknown_attempts"][0]["state"], "unknown")
        self.assertEqual(len(self.gateway.calls), 7)


if __name__ == "__main__":
    unittest.main()
