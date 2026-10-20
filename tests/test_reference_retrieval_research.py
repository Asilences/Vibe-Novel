"""Derived layers use real research validators with synthetic offline replies."""
import copy
import unittest

from tests import test_reference_studies as studies
from tests import test_reference_jobs as batches
from tests.test_reference_retrieval import FixtureEmbedding
from vibenovel.core.reference_retrieval import ReferenceRetrievalStore
from vibenovel.errors import VibeNovelError


class RetrievalStudyTests(unittest.TestCase):
    setUp = studies.StudyTests.setUp
    request = studies.StudyTests.request
    create = studies.StudyTests.create
    run_study = studies.StudyTests.run_study

    def index(self, book_ids):
        retrieval = ReferenceRetrievalStore(self.workspace, engine=FixtureEmbedding())
        preview = retrieval.preview(book_ids, ["original", "structure", "expression", "method"])
        index = retrieval.build(book_ids, preview["layers"], preview["hash"], background=False)
        self.assertEqual(index["state"], "ready", index.get("error"))
        return retrieval, index

    def test_continuous_reading_graph_relations_and_later_edit_invalidates(self):
        study = self.create()
        self.assertEqual(self.run_study(study)["state"], "succeeded")
        retrieval, index = self.index([self.book["book_id"]])
        self.assertGreater(index["progress"]["layers"]["structure"], 0)
        self.assertGreater(index["progress"]["layers"]["expression"], 0)
        self.assertEqual(index["progress"]["layers"]["method"], 0)
        result = retrieval.search(index["id"], "选择代价", layers=["structure", "expression"], limit=30)
        self.assertGreater(result["relationship_candidates"], 0)
        self.assertTrue(any("relationship" in item["channels"] for item in result["results"]))
        evidence = result["results"][0]["refs"][0]
        raw = self.books.chapter(evidence["book_id"], evidence["chapter_id"], evidence["edition_id"])["text"]
        self.assertEqual(raw[evidence["start_char"]:evidence["end_char"]], evidence["quote"])
        selection = retrieval.selection(index["id"], [result["results"][0]["id"]])
        retrieval.validate_task_selection(selection)
        export = self.manager.store.export_verified(study["id"])
        graphs = copy.deepcopy(export["graphs"])
        graphs["causal"]["nodes"][0]["label"] = "作者修订后的候选"
        self.manager.store.edit_graphs(study["id"], export["hash"], graphs, "核对关系")
        self.assertEqual(retrieval.get(index["id"])["state"], "stale")
        with self.assertRaises(VibeNovelError):
            retrieval.validate_task_selection(selection)

    def test_cross_book_comparison_results_indexed_without_invented_graphs(self):
        second = self.books.import_book("另一书.txt", "第一章 雪地\n小红在雪中寻找走失的羊，远处传来铃声。".encode(), title="人工书B")
        self.books.update_policy(second["book_id"], expected_hash=second["hash"], outbound="excerpts")
        request = self.request()
        request["kind"] = "comparison"
        request["sources"][0]["chapter_ids"] = [self.book["chapters"][0]["chapter_id"]]
        request["sources"].append({"book_id": second["book_id"], "edition_id": second["edition_id"], "chapter_ids": []})
        study = self.create(request)
        self.assertEqual(self.run_study(study)["state"], "succeeded")
        retrieval, index = self.index([self.book["book_id"], second["book_id"]])
        result = retrieval.search(index["id"], "不同安排", layers=["structure"], limit=30)
        self.assertTrue(result["results"])
        self.assertTrue(all(item["locator"]["kind"] == "study_result" for item in result["results"]))
        self.assertEqual(result["relationship_candidates"], 0)
        comparison = next(item for item in result["results"] if item["locator"]["section"] == "comparisons")
        self.assertEqual(len({ref["book_id"] for ref in comparison["refs"]}), 2)

    def test_failed_research_keeps_only_verified_prefix_with_explicit_partial_coverage(self):
        study = self.create()
        self.assertEqual(self.run_study(study)["state"], "succeeded")
        value = self.manager.store.load(study["id"])
        value["state"] = "failed"
        value["nodes"][1]["state"] = "failed"
        self.manager.store.save(value)
        retrieval, index = self.index([self.book["book_id"]])
        coverage = index["coverage"]["studies"][0]
        self.assertEqual((coverage["state"], coverage["completed_segments"], coverage["total_segments"]), ("failed", 1, 2))
        result = retrieval.search(index["id"], "选择", layers=["structure"], limit=30)
        self.assertTrue(result["results"])
        self.assertTrue(all(item["locator"].get("node_id", "").startswith("read-00001") for item in result["results"]))


class RetrievalBatchTests(unittest.TestCase):
    setUp = batches.ReferenceBatchTests.setUp
    manager = batches.ReferenceBatchTests.manager
    request = batches.ReferenceBatchTests.request
    create = batches.ReferenceBatchTests.create
    wait = batches.ReferenceBatchTests.wait
    response = staticmethod(batches.ReferenceBatchTests.response)

    def test_existing_a10_successful_structure_and_expression_remain_retrievable(self):
        manager = self.manager()
        batch = self.create(manager)
        manager.control(self.book["book_id"], batch["id"], "start")
        self.assertEqual(self.wait(manager, batch["id"])["state"], "succeeded")
        retrieval = ReferenceRetrievalStore(self.workspace, engine=FixtureEmbedding())
        preview = retrieval.preview([self.book["book_id"]], ["structure", "expression", "method"])
        index = retrieval.build(preview["book_ids"], preview["layers"], preview["hash"], background=False)
        self.assertEqual(index["state"], "ready", index["error"])
        result = retrieval.search(index["id"], "采取行动", semantic=False)
        self.assertEqual({item["layer"] for item in result["results"]}, {"structure", "expression"})
        self.assertTrue(all(item["locator"]["kind"] == "batch" for item in result["results"]))
