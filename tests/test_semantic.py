"""Offline retrieval contracts using a deterministic engine, never model assets."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.documents import ConflictError, DocumentStore
from vibenovel.core.files import digest
from vibenovel.core.project import ProjectStore
from vibenovel.core.semantic import CACHE_PATH, MODEL, REVISION, LocalEmbedding, SemanticStore
from vibenovel.errors import VibeNovelError


class FakeEngine:
    """Unit vectors isolate storage behavior from any model's retrieval quality."""

    def __init__(self, *, available=True):
        self.available = available
        self.calls = []
        self.on_query = None

    def status(self):
        return {"available": self.available, "model_present": self.available,
                "dependencies_present": self.available, "model": MODEL, "revision": REVISION,
                "loaded": True, "reason": "测试引擎就绪" if self.available else "测试依赖缺失",
                "setup_command": "test-only-no-download"}

    def embed(self, texts, *, query=False):
        self.calls.append({"texts": list(texts), "query": query})
        if query and self.on_query:
            callback, self.on_query = self.on_query, None
            callback()
        vectors = []
        for text in texts:
            vector = [0.0] * 512
            vector[0 if "手套" in text else 1] = 1.0
            vectors.append(vector)
        return vectors


class SemanticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.projects = ProjectStore(self.workspace)
        self.project = self.projects.init("story", genre="football")
        self.documents = DocumentStore(self.project)
        self.engine = FakeEngine()
        self.store = SemanticStore(self.project, self.workspace, engine=self.engine)

    def canonical(self, path, content, *, project=None):
        project = project or self.project
        target = project.path(path)
        before = digest(target.read_bytes()) if target.is_file() else None
        approvals = ApprovalStore(project)
        proposal = approvals.create(path, content, "作者认可测试材料", [path], before)
        approvals.decide(proposal["id"], "approve")
        approvals.apply(proposal["id"])
        return DocumentStore(project).read(path)

    def document_batches(self):
        return [entry["texts"] for entry in self.engine.calls if not entry["query"]]

    def test_scans_only_this_novels_canonical_chapters_and_preserves_exact_evidence(self):
        original = "# 第一章\n\n  守门员把红色手套留在桌边。  \n\n那封信没有署名。"
        chapter = self.canonical("chapters/ch001.md", original)
        self.documents.save("drafts/ch002.md", "草稿秘密不得进入检索", None)
        self.canonical("memory/events/ch001.md", "记忆秘密不得进入检索")
        other = self.projects.init("other", genre="campus")
        self.canonical("chapters/ch001.md", "另一作品秘密不得进入检索", project=other)

        result = self.store.search("红色手套", limit=20)
        self.assertEqual(result["mode"], "local_embedding")
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["indexed_chunks"], 2)
        self.assertEqual({item["path"] for item in result["results"]}, {chapter["path"]})
        first = result["results"][0]
        self.assertEqual(first["hash"], chapter["hash"])
        self.assertEqual(first["quote"], "  守门员把红色手套留在桌边。  ")
        self.assertEqual((first["start_line"], first["end_line"], first["start_column"]), (3, 3, 1))
        self.assertEqual(first["end_column"], len(first["quote"]))
        indexed = "\n".join(text for batch in self.document_batches() for text in batch)
        self.assertNotIn("秘密不得进入检索", indexed)
        for item in result["results"]:
            line = original.splitlines()[item["start_line"] - 1]
            self.assertEqual(line[item["start_column"] - 1:item["end_column"]], item["quote"])

    def test_repeated_queries_reuse_vectors_but_modified_text_gets_new_index_and_hash(self):
        chapter = self.canonical("chapters/ch001.md", "红色手套仍在。\n那封信没有署名。")
        first = self.store.search("手套")
        self.assertEqual(first["reused_chunks"], 0)
        self.assertEqual(len(self.document_batches()), 1)
        second = self.store.search("署名")
        self.assertEqual(second["reused_chunks"], 2)
        self.assertEqual(len(self.document_batches()), 1, "A new query must not re-embed unchanged prose")

        changed = self.canonical(chapter["path"], "蓝色围巾已经转交。\n那封信没有署名。")
        third = self.store.search("围巾", limit=20)
        self.assertEqual(third["reused_chunks"], 1)
        self.assertEqual(self.document_batches()[-1], ["蓝色围巾已经转交。"])
        self.assertEqual({item["hash"] for item in third["results"]}, {changed["hash"]})
        self.assertNotEqual(chapter["hash"], changed["hash"])
        self.assertNotIn("红色手套仍在。", [item["quote"] for item in third["results"]])

    def test_corrupt_index_is_rebuilt_from_current_canonical_text(self):
        chapter = self.canonical("chapters/ch001.md", "红色手套放回了柜子。")
        self.store.search("手套")
        cache = self.project.path(CACHE_PATH)
        cache.write_text("{invalid index", encoding="utf-8")
        recovered = self.store.search("手套")
        self.assertEqual(recovered["reused_chunks"], 0)
        self.assertEqual(len(self.document_batches()), 2)
        self.assertTrue(any("缓存" in item and "重建" in item for item in recovered["limitations"]))
        self.assertEqual(recovered["results"][0]["hash"], chapter["hash"])
        self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_invalid_cached_vectors_cannot_supply_locations_or_replace_prose(self):
        chapter = self.canonical("chapters/ch001.md", "红色手套放回了柜子。")
        cache = self.project.path(CACHE_PATH)
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps({"schema_version": 1, "model": MODEL, "revision": REVISION,
            "vectors": {digest("红色手套放回了柜子。".encode("utf-8")): [0.0] * 512},
            "results": [{"path": "drafts/forged.md", "quote": "伪造引文"}]}), encoding="utf-8")
        result = self.store.search("手套")
        self.assertEqual(result["reused_chunks"], 0)
        self.assertEqual(result["results"][0]["path"], chapter["path"])
        self.assertEqual(result["results"][0]["quote"], chapter["content"])
        self.assertEqual(len(self.document_batches()), 1)

    def test_canonical_change_during_embedding_rejects_results_and_leaves_cache_unchanged(self):
        chapter = self.canonical("chapters/ch001.md", "红色手套放回了柜子。")
        self.store.search("手套")
        before = self.project.path(CACHE_PATH).read_bytes()
        self.engine.on_query = lambda: self.canonical(chapter["path"], "手套已经交给队长。")
        with self.assertRaises(ConflictError):
            self.store.search("手套")
        self.assertEqual(self.project.path(CACHE_PATH).read_bytes(), before)
        refreshed = self.store.search("手套")
        self.assertEqual(refreshed["results"][0]["quote"], "手套已经交给队长。")

    def test_long_line_chunks_keep_exact_columns_and_chunk_limit_records_omissions(self):
        long_line = "甲" * 200 + "乙" * 200 + "末段证据"
        chapter = self.canonical("chapters/ch001.md", "# 第一章\n\n" + long_line)
        result = self.store.search("线索", limit=20)
        self.assertEqual([(item["start_line"], item["start_column"], item["end_column"])
                          for item in result["results"]], [(3, 1, 200), (3, 201, 400), (3, 401, 404)])
        for item in result["results"]:
            self.assertEqual(item["hash"], chapter["hash"])
            self.assertEqual(item["quote"], long_line[item["start_column"] - 1:item["end_column"]])
        self.canonical("chapters/ch002.md", "另一章的片段。")
        with patch("vibenovel.core.semantic.MAX_CHUNKS", 2):
            capped = self.store.search("线索", limit=20)
        self.assertEqual(capped["indexed_chunks"], 2)
        self.assertEqual({item["path"] for item in capped["omitted"]
                          if item["reason"] == "semantic_chunk_limit_partial_or_all"},
                         {"chapters/ch001.md", "chapters/ch002.md"})
        self.assertEqual("".join(item["quote"] for item in capped["results"]), long_line[:400])

    def test_invalid_queries_are_rejected_before_engine_or_cache_access(self):
        for query in ("", "   ", "x" * 201, "手套\n线索", "手套\x00", "手套\t线索", "手套\ud800"):
            with self.subTest(query=repr(query)), self.assertRaises(VibeNovelError):
                self.store.search(query)
        for limit in (0, 21, True, "10"):
            with self.subTest(limit=limit), self.assertRaises(VibeNovelError):
                self.store.search("手套", limit=limit)
        self.assertEqual(self.engine.calls, [])
        self.assertFalse(self.project.path(CACHE_PATH).exists())

    def test_unavailable_engine_does_not_download_fallback_or_create_index(self):
        self.engine.available = False
        with self.assertRaisesRegex(VibeNovelError, "测试依赖缺失"):
            self.store.search("手套")
        self.assertEqual(self.engine.calls, [])
        self.assertFalse(self.project.path(CACHE_PATH).exists())

    def test_native_worker_failure_and_timeout_are_regular_errors_and_next_call_can_recover(self):
        engine = LocalEmbedding(self.workspace)
        for failure in (subprocess.CompletedProcess([], -1073741819, stdout=b""),
                        subprocess.TimeoutExpired("isolated-worker", 300)):
            with self.subTest(failure=type(failure).__name__):
                engine.loaded = True
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch("vibenovel.core.semantic.subprocess.run", **kwargs) as worker:
                    with self.assertRaisesRegex(VibeNovelError, "工作进程失败或超时"):
                        engine.embed(["手套"], query=True)
                self.assertFalse(engine.loaded)
                self.assertEqual(worker.call_args.kwargs["timeout"], 300)
                self.assertFalse(worker.call_args.kwargs["check"])
                self.assertIn("semantic_worker", worker.call_args.args[0][-1])
        vector = [1.0] + [0.0] * 511
        with patch("vibenovel.core.semantic.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps([vector]).encode("utf-8"))):
            self.assertEqual(engine.embed(["手套"]), [vector])
        self.assertTrue(engine.loaded)


if __name__ == "__main__":
    unittest.main()
