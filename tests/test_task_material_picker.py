"""Existing task preview accepts a composed, explicit book material selection."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_reference_retrieval import FixtureEmbedding
from vibenovel.core.documents import DocumentStore
from vibenovel.core.library import LibraryStore
from vibenovel.core.project import ProjectStore
from vibenovel.core.reference_books import ReferenceBookStore
from vibenovel.core.reference_retrieval import ReferenceRetrievalStore
from vibenovel.errors import VibeNovelError
from vibenovel.studio.jobs import JobManager


class TaskMaterialPickerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        environment = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                               if not key.upper().startswith("VIBENOVEL_")}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        embedding = patch("vibenovel.core.reference_retrieval.engine_for", return_value=FixtureEmbedding())
        embedding.start()
        self.addCleanup(embedding.stop)
        self.project = ProjectStore(self.workspace).init("story", genre="campus")
        (self.workspace / "prompt.md").write_text("通用测试规则", encoding="utf-8")
        genres = self.workspace / "prompts" / "genres"
        genres.mkdir(parents=True)
        (genres / "campus.md").write_text("测试题材规则", encoding="utf-8")
        self.source = DocumentStore(self.project).save("outlines/source/author.md", "作者大纲：一位教练带领球队训练。", None)
        self.library = LibraryStore(self.workspace, self.project)
        self.card = self.library.save_entry({"type": "material", "title": "本书训练细节", "content": "训练应呈现人物选择与实际代价。", "outbound": "excerpts"})
        personal = LibraryStore(self.workspace)
        entry = personal.save_entry({"type": "style", "title": "通用表达参照", "summary": "作者归纳：用人物动作呈现迟疑。", "outbound": "summary"})
        self.binding = self.library.bind(entry["id"], purpose="planning")
        self.books = ReferenceBookStore(self.workspace)
        book = self.books.import_book("人工参考.txt", "第一章\n父亲在家庭困境中作出决定。".encode(), title="人工参考书")
        self.books.update_policy(book["book_id"], expected_hash=book["hash"], outbound="excerpts")
        retrieval = ReferenceRetrievalStore(self.workspace, engine=FixtureEmbedding())
        preview = retrieval.preview([book["book_id"]], ["original"])
        index = retrieval.build(preview["book_ids"], preview["layers"], preview["hash"], background=False)
        hit = retrieval.search(index["id"], "家庭", limit=1)["results"][0]
        selected = retrieval.selection(index["id"], [hit["id"]])
        self.retrieval = {"index_id": index["id"], "ids": selected["ids"], "expected_preview": selected["hash"]}
        self.provider_calls = []
        def forbidden_provider(name):
            self.provider_calls.append(name)
            raise AssertionError("Preview must not call a provider")
        self.manager = JobManager(self.workspace, provider_factory=forbidden_provider)
        self.addCleanup(self.manager.close)

    def request(self, selection):
        return {"kind": "build_blueprint", "provider": "api", "document_path": self.source["path"],
                "document_hash": self.source["hash"], "planning_start": 1, "planning_count": 1,
                "library_selection": selection}

    def test_cards_bindings_and_exact_retrieval_combine_and_changed_selection_invalidates_old_preview(self):
        simple = self.manager.preview(self.project, self.request({"entry_ids": [self.card["id"]]}))
        request = self.request({"entry_ids": [self.card["id"]], "binding_ids": [self.binding["id"]],
                                "retrieval_selection": self.retrieval})
        combined = self.manager.preview(self.project, request)
        snapshot = combined["library_preview"]["snapshot"]
        self.assertEqual({item["kind"] for item in snapshot["manifest"]}, {"entry", "binding", "reference_retrieval"})
        self.assertIn("人物选择与实际代价", snapshot["text"])
        self.assertIn("人物动作呈现迟疑", snapshot["text"])
        self.assertIn("家庭困境", snapshot["text"])
        old_hash = simple["library_preview"]["snapshot"]["hash"]
        self.assertNotEqual(old_hash, snapshot["hash"])
        with self.assertRaises(VibeNovelError):
            self.manager.submit(self.project, request, expected_library_hash=old_hash)
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(self.manager.list(self.project), [])

    def test_choosing_cards_cannot_adopt_methods_or_expand_authorization(self):
        method = self.library.save_entry({"type": "method", "title": "未采用方法", "content": "候选方法。", "outbound": "excerpts",
                                         "method": {"purpose": "规划", "steps": ["先看选择"], "limitations": ["未试验"]}})
        with self.assertRaises(VibeNovelError):
            self.manager.preview(self.project, self.request({"entry_ids": [method["id"]], "retrieval_selection": self.retrieval}))
        self.assertFalse(self.library.entry(method["id"])["adopted"])
        local = self.library.save_entry({"type": "material", "title": "仅本地", "content": "不允许发送。", "outbound": "local"})
        with self.assertRaises(VibeNovelError):
            self.manager.preview(self.project, self.request({"entry_ids": [local["id"]]}))
        self.assertEqual(self.library.entry(local["id"])["outbound"], "local")
        self.assertEqual(self.provider_calls, [])
