"""Offline integration and revocation boundaries; embeddings here are fixtures.

Real installed BGE is exercised separately on a private corpus, never in CI.
"""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from vibenovel.core.files import atomic_write, file_digest, json_bytes
from vibenovel.core.library import LibraryStore
from vibenovel.core.project import ProjectStore
from vibenovel.core.reference_books import ReferenceBookStore
from vibenovel.core.reference_retrieval import ReferenceRetrievalStore
from vibenovel.errors import VibeNovelError


class FixtureEmbedding:
    def status(self):
        return {"available": True, "model": "explicit-test-fixture"}

    def embed(self, texts, *, query=False):
        # Deliberately explicit fixture; production always calls engine_for BGE.
        values = []
        for text in texts:
            index = 0 if any(word in text for word in ("母亲", "家庭", "父亲")) else 1
            vector = [0.] * 512
            vector[index] = 1.
            values.append(vector)
        return values


class ReferenceRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.books = ReferenceBookStore(self.workspace)
        self.book = self.books.import_book("测试.txt", "第一章 离别\n母亲离开以后，父亲独自照料孩子。\n第二章 山路\n暴雨冲毁大桥，村民沿山路绕行。".encode(), title="人工参考")
        self.retrieval = ReferenceRetrievalStore(self.workspace, engine=FixtureEmbedding())

    def build(self, layers=None):
        layers = layers or ["original", "structure", "expression", "method"]
        preview = self.retrieval.preview([self.book["book_id"]], layers)
        result = self.retrieval.build([self.book["book_id"]], layers, preview["hash"], background=False)
        self.assertEqual(result["state"], "ready", result.get("error"))
        return result

    def allow(self, **changes):
        current = self.books.detail(self.book["book_id"], limit=1)
        return self.books.update_policy(current["book_id"], expected_hash=current["hash"],
            outbound=changes.get("outbound", current["outbound"]), withdrawn=changes.get("withdrawn", current["withdrawn"]))

    def test_local_semantic_lexical_layers_and_explicit_outbound_selection(self):
        index = self.build()
        self.assertGreater(index["progress"]["layers"]["original"], 0)
        self.assertEqual(index["progress"]["layers"]["structure"], 0)
        self.assertEqual(index["progress"]["layers"]["method"], 0)
        semantic = self.retrieval.search(index["id"], "家庭", limit=1)
        self.assertIn("母亲", semantic["results"][0]["text"])
        self.assertIn("semantic", semantic["results"][0]["channels"])
        self.assertFalse(semantic["outbound"])
        self.assertEqual(semantic["relationship_candidates"], 0)
        lexical = self.retrieval.search(index["id"], "大桥", semantic=False)
        self.assertIn("大桥", lexical["results"][0]["text"])
        with self.assertRaises(VibeNovelError):
            self.retrieval.selection(index["id"], [semantic["results"][0]["id"]])
        self.assertFalse((self.workspace / "library/.state/manifest.json").exists())
        self.allow(outbound="excerpts")
        index = self.build()
        results = self.retrieval.search(index["id"], "家庭", limit=1)["results"]
        selected = self.retrieval.selection(index["id"], [results[0]["id"]])
        project = ProjectStore(self.workspace).init("author", genre="general")
        before = project.fingerprint()
        library = LibraryStore(self.workspace, project)
        task = library.select_for_task({"purpose": "writing", "retrieval_selection": {
            "index_id": index["id"], "ids": selected["ids"], "expected_preview": selected["hash"]}})
        self.assertIn("母亲", task["text"])
        library.validate_task_snapshot(task)
        self.assertEqual(project.fingerprint(), before)
        self.assertEqual(task["source_fragments"], [])
        self.allow(withdrawn=True)
        with self.assertRaises(VibeNovelError):
            library.validate_task_snapshot(task)

    def test_aba_authorization_and_new_edition_reject_old_vectors(self):
        index = self.build()
        self.allow(withdrawn=True)
        self.allow(withdrawn=False)
        with self.assertRaises(VibeNovelError):
            self.retrieval.search(index["id"], "山路")
        newer = self.build()
        self.books.import_book("测试.txt", "第一章 修订\n村民重新修桥。".encode(), title="人工参考")
        with self.assertRaises(VibeNovelError):
            self.retrieval.search(newer["id"], "山路")

    def test_source_withdrawal_during_embedding_prevents_cache_publication(self):
        retrieval = self.retrieval
        real = retrieval.engine.embed
        def withdraw(texts, **kw):
            self.allow(withdrawn=True)
            return real(texts, **kw)
        retrieval.engine.embed = withdraw
        preview = retrieval.preview([self.book["book_id"]], ["original"])
        result = retrieval.build([self.book["book_id"]], ["original"], preview["hash"], background=False)
        self.assertEqual(result["state"], "failed")
        self.assertFalse(retrieval.path(result["id"] + "/cache.sqlite3").exists())

    def test_original_tamper_cache_tamper_and_forged_locator_never_return(self):
        index = self.build()
        path = self.retrieval.path(index["id"] + "/cache.sqlite3")
        with path.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(VibeNovelError):
            self.retrieval.search(index["id"], "家庭")
        index = self.build()
        path = self.retrieval.path(index["id"] + "/cache.sqlite3")
        with sqlite3.connect(path) as connection:
            identity, raw = connection.execute("SELECT id,record FROM docs LIMIT 1").fetchone()
            record = json.loads(raw)
            record["refs"][0]["quote"] = "伪造原文"
            connection.execute("UPDATE docs SET record=? WHERE id=?", (json.dumps(record), identity))
        connection.close()
        # Even if a disposable cache digest is recomputed, authoritative
        # reconstruction still rejects altered citation data.
        manifest = self.retrieval._manifest(index["id"])
        manifest["cache_hash"] = file_digest(path)
        self.retrieval._save(manifest)
        with self.assertRaises(VibeNovelError):
            self.retrieval.selection(index["id"], [identity], outbound=False)
        index = self.build()
        chapter = self.books.detail(self.book["book_id"])["chapters"][0]
        shard = self.books.path(self.book["book_id"] + "/editions/" + self.book["edition_id"] + "/" + chapter["blocks"][0]["path"])
        shard.write_bytes(shard.read_bytes().replace("母亲".encode(), "阿姨".encode()))
        with self.assertRaises(VibeNovelError):
            self.retrieval.search(index["id"], "家庭")

    def test_adopted_method_is_searchable_and_edit_revokes_old_index(self):
        library = LibraryStore(self.workspace)
        method = library.save_entry({"type": "method", "title": "制造选择代价", "content": "家庭冲突应通过决定与行动呈现。", "outbound": "excerpts",
            "method": {"purpose": "场景规划", "steps": ["明确选择", "检验后果"], "limitations": ["未验证效果"]}})
        candidate = self.build(["method"])
        self.assertEqual(candidate["progress"]["indexed_chunks"], 0)
        adopted = library.adopt_method(method["id"], method["hash"])
        index = self.build(["method"])
        result = self.retrieval.search(index["id"], "家庭")["results"][0]
        self.assertEqual(result["layer"], "method")
        selected = self.retrieval.selection(index["id"], [result["id"]])
        # Personal task validation occurs while its library lock is held and
        # must not recursively acquire that same write lock.
        task = library.select_for_task({"retrieval_selection": {"index_id": index["id"], "ids": selected["ids"], "expected_preview": selected["hash"]}})
        library.validate_task_snapshot(task)
        edited = {key: copy.deepcopy(adopted[key]) for key in ("id", "type", "title", "content", "method", "outbound")}
        edited["content"] = "作者编辑后重新核对。"
        library.save_entry(edited, adopted["hash"])
        with self.assertRaises(VibeNovelError):
            self.retrieval.search(index["id"], "家庭")
        with self.assertRaises(VibeNovelError):
            library.validate_task_snapshot(task)

    def test_invalid_preview_selection_and_vector_fail_without_fallback(self):
        preview = self.retrieval.preview([self.book["book_id"]], ["original"])
        with self.assertRaises(VibeNovelError):
            self.retrieval.build([self.book["book_id"]], ["original"], "0" * 64)
        self.retrieval.engine.embed = lambda texts, **kw: [[1.] for _ in texts]
        result = self.retrieval.build([self.book["book_id"]], ["original"], preview["hash"], background=False)
        self.assertEqual(result["state"], "failed")
        self.assertIn("512", result["error"])
        with self.assertRaises(VibeNovelError):
            self.retrieval.search(result["id"], "家庭")

    def test_cancel_during_inference_stays_cancelled_across_progress_save(self):
        real = self.retrieval.engine.embed
        calls = []
        def cancel(texts, **kw):
            calls.append(len(texts))
            current = self.retrieval.list_indexes()[0]
            self.retrieval.cancel(current["id"])
            return real(texts, **kw)
        self.retrieval.engine.embed = cancel
        preview = self.retrieval.preview([self.book["book_id"]], ["original"])
        result = self.retrieval.build([self.book["book_id"]], ["original"], preview["hash"], background=False)
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["cancel_requested"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.retrieval.path(result["id"] + "/cache.sqlite3").exists())

    def test_control_character_section_is_reported_omitted_not_silently_cleaned(self):
        abnormal = self.books.import_book("异常.txt", "第一章 异常\n原件保留孤立\x00字符。\n第二章 正常\n父亲照料孩子。".encode(), title="有异常参考")
        preview = self.retrieval.preview([abnormal["book_id"]], ["original"])
        result = self.retrieval.build([abnormal["book_id"]], ["original"], preview["hash"], background=False)
        self.assertEqual(result["state"], "ready", result.get("error"))
        self.assertEqual(len(result["omitted"]), 1)
        results = self.retrieval.search(result["id"], "家庭")["results"]
        self.assertTrue(results)
        self.assertTrue(all("\x00" not in item["text"] for item in results))
        original = self.books.chapter(abnormal["book_id"], abnormal["chapters"][0]["chapter_id"])["text"]
        self.assertIn("\x00", original)


if __name__ == "__main__":
    unittest.main()
