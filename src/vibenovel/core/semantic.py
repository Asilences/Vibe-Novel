"""Optional, offline BGE retrieval. No model service, tools, or automatic download.

The cache is disposable and is never a source of story facts. Every result is
rebuilt from the current canonical text and carries its exact source version.
"""
from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
import threading
from pathlib import Path

from vibenovel.errors import VibeNovelError
from .analysis import AnalysisStore
from .documents import ConflictError, DocumentStore
from .files import atomic_write, digest, file_digest, read_json, safe_path

MODEL = "Xenova/bge-small-zh-v1.5"
REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
MODEL_SHA256 = "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"
# Tokenizer is pinned to the same repository revision; installation records and
# validates its digest, and the fixed expected digest is checked at load time.
TOKENIZER_SHA256 = "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"
MODEL_DIRECTORY = ".runtime-build/models/bge-small-zh-v1.5"
CACHE_PATH = ".runtime/semantic-index.json"
MAX_CHUNKS = 4000
MAX_CACHE_BYTES = 48 * 1024 * 1024
_engines = {}
_engine_lock = threading.Lock()


class _NativeEmbedding:
    def __init__(self, workspace: Path):
        self.directory = safe_path(workspace, MODEL_DIRECTORY)
        self.loaded = False
        self.lock = threading.Lock()

    def status(self):
        present = (safe_path(self.directory, "model_quantized.onnx").is_file()
                   and safe_path(self.directory, "tokenizer.json").is_file())
        dependencies = all(importlib.util.find_spec(name) is not None for name in ("onnxruntime", "tokenizers", "numpy"))
        return {"available": present and dependencies, "model_present": present,
                "dependencies_present": dependencies, "model": MODEL, "revision": REVISION,
                "loaded": self.loaded, "reason": ("上次本地推理成功；查询使用隔离工作进程。" if self.loaded else
                "就绪（首次查询加载本地模型）") if present and dependencies else
                "本地语义依赖或模型尚未安装；不会自动联网或改用字面检索。",
                "setup_command": 'python -m pip install -e ".[semantic]"\npython tools/setup_semantic.py'}

    def embed(self, texts: list[str], *, query=False) -> list[list[float]]:
        with self.lock:
            if not self.loaded:
                if not self.status()["available"]:
                    raise VibeNovelError(self.status()["reason"])
                model = safe_path(self.directory, "model_quantized.onnx")
                tokenizer = safe_path(self.directory, "tokenizer.json")
                if file_digest(model) != MODEL_SHA256 or file_digest(tokenizer) != TOKENIZER_SHA256:
                    raise VibeNovelError("本地语义模型/分词器校验失败，请重新运行setup_semantic.py")
                try:
                    import numpy as np
                    import onnxruntime as ort
                    from tokenizers import Tokenizer
                    self.np = np
                    self.tokenizer = Tokenizer.from_file(str(tokenizer))
                    self.tokenizer.enable_truncation(max_length=512)
                    self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
                    options = ort.SessionOptions()
                    options.intra_op_num_threads = 2
                    options.inter_op_num_threads = 1
                    ort.disable_telemetry_events()
                    self.session = ort.InferenceSession(str(model), sess_options=options,
                                                       providers=["CPUExecutionProvider"])
                    self.loaded = True
                except Exception as exc:
                    raise VibeNovelError("本地语义引擎加载失败；请检查可选依赖与模型，不影响字面检索") from exc
            result = []
            for start in range(0, len(texts), 16):
                batch = texts[start:start + 16]
                if query:
                    batch = ["为这个句子生成表示以用于检索相关文章：" + text for text in batch]
                encoded = self.tokenizer.encode_batch(batch)
                feeds = {"input_ids": self.np.array([item.ids for item in encoded], dtype=self.np.int64),
                         "attention_mask": self.np.array([item.attention_mask for item in encoded], dtype=self.np.int64),
                         "token_type_ids": self.np.array([item.type_ids for item in encoded], dtype=self.np.int64)}
                inputs = {item.name for item in self.session.get_inputs()}
                try:
                    values = self.session.run(None, {name: value for name, value in feeds.items() if name in inputs})[0]
                    vectors = values[:, 0, :] if values.ndim == 3 else values
                    norms = self.np.linalg.norm(vectors, axis=1, keepdims=True)
                    if not self.np.isfinite(vectors).all() or (norms <= 0).any() or vectors.shape[1] != 512:
                        raise ValueError("Invalid embedding")
                    result.extend((vectors / norms).tolist())
                except Exception as exc:
                    raise VibeNovelError("本地语义推理失败；没有返回不完整的检索结果") from exc
            return result


class LocalEmbedding(_NativeEmbedding):
    """Keep optional native libraries outside the Studio server process.

    A bad binary/CPU/DLL combination may terminate Python without raising.
    Isolating inference turns that into a regular user-visible failure.
    """
    def embed(self, texts: list[str], *, query=False) -> list[list[float]]:
        payload = json.dumps({"workspace": str(self.directory.parents[2]), "texts": texts,
                              "query": query}, ensure_ascii=True).encode("utf-8")
        with self.lock:
            try:
                result = subprocess.run([sys.executable, "-m", "vibenovel.core.semantic_worker"],
                                        input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        timeout=300, check=False,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode or len(result.stdout) > MAX_CACHE_BYTES:
                    raise ValueError("worker failed")
                vectors = json.loads(result.stdout)
                if (not isinstance(vectors, list) or len(vectors) != len(texts)
                        or any(not isinstance(vector, list) or len(vector) != 512
                               or any(type(value) not in (int, float) or not -1.01 <= value <= 1.01
                                      or not math.isfinite(value) for value in vector) for vector in vectors)):
                    raise ValueError("invalid worker result")
                self.loaded = True
                return vectors
            except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
                self.loaded = False
                raise VibeNovelError("本地语义工作进程失败或超时；请检查可选依赖、CPU与模型。字面检索仍可用。") from exc


def engine_for(workspace: Path) -> LocalEmbedding:
    key = str(workspace.resolve())
    with _engine_lock:
        if key not in _engines:
            _engines[key] = LocalEmbedding(workspace)
        return _engines[key]


def _chunks(documents):
    chunks, omitted = [], []
    for doc in documents:
        exhausted = False
        for number, line in enumerate(doc["content"].splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            for start in range(0, len(line), 200):
                quote = line[start:start + 200]
                if not quote.strip():
                    continue
                if len(chunks) >= MAX_CHUNKS:
                    exhausted = True
                    break
                chunks.append({"path": doc["path"], "hash": doc["hash"], "start_line": number,
                               "end_line": number, "start_column": start + 1,
                               "end_column": start + len(quote), "quote": quote,
                               "key": digest(quote.encode("utf-8"))})
            if exhausted:
                break
        if exhausted:
            omitted.append({"path": doc["path"], "reason": "semantic_chunk_limit_partial_or_all"})
    return chunks, omitted


class SemanticStore:
    def __init__(self, project, workspace: Path, *, engine=None):
        self.project = project
        self.storage = DocumentStore(project).storage
        self.engine = engine if engine is not None else engine_for(workspace)

    def status(self):
        return self.engine.status()

    def search(self, query: str, limit: int = 10) -> dict:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or type(limit) is not int or not 1 <= limit <= 20:
            raise VibeNovelError("语义查询需1–200字符，limit为1–20整数")
        if any(ord(char) < 32 or ord(char) == 127 for char in query):
            raise VibeNovelError("语义查询不允许控制字符")
        try:
            query.encode("utf-8")
        except UnicodeError as exc:
            raise VibeNovelError("语义查询必须为有效Unicode") from exc
        if not self.status()["available"]:
            raise VibeNovelError(self.status()["reason"])
        with self.storage.locked() as state:
            documents, omitted = AnalysisStore(self.project)._history()
            revision, fingerprint = state["revision"], state["fingerprint"]
        chunks, extra = _chunks(documents)
        omitted.extend(extra)
        cache = self.project.path(CACHE_PATH)
        cached, cache_error = {}, None
        if cache.exists():
            try:
                previous = read_json(cache, max_bytes=MAX_CACHE_BYTES)
                if (previous.get("schema_version") == 1 and previous.get("model") == MODEL
                        and previous.get("revision") == REVISION and isinstance(previous.get("vectors"), dict)
                        and len(previous["vectors"]) <= MAX_CHUNKS):
                    for key, vector in previous["vectors"].items():
                        if (isinstance(vector, list) and len(vector) == 512
                                and all(type(value) in (int, float) and -1.01 <= value <= 1.01
                                        and math.isfinite(value) for value in vector)
                                and 0.98 <= sum(value * value for value in vector) <= 1.02):
                            cached[key] = vector
            except (OSError, VibeNovelError):
                cache_error = "缓存损坏/不可读；已从当前正式正文重建。"
        vectors = {chunk["key"]: cached[chunk["key"]] for chunk in chunks if chunk["key"] in cached}
        todo = {chunk["key"]: chunk["quote"] for chunk in chunks if chunk["key"] not in vectors}
        if todo:
            generated = self.engine.embed(list(todo.values()))
            if len(generated) != len(todo):
                raise VibeNovelError("语义索引缺少向量，已停止返回结果")
            vectors.update(zip(todo, generated))
        query_vector = self.engine.embed([query.strip()], query=True)[0]
        matches = []
        for chunk in chunks:
            score = sum(left * right for left, right in zip(query_vector, vectors[chunk["key"]]))
            matches.append({key: value for key, value in chunk.items() if key != "key"} | {"score": round(score, 6)})
        matches.sort(key=lambda item: (-item["score"], item["path"], item["start_line"], item["start_column"]))
        with self.storage.locked() as state:
            if state["revision"] != revision or state["fingerprint"] != fingerprint:
                raise ConflictError("正式内容在语义检索期间变化，请重新检索")
            # Cache never supplies quotes or paths; it can be deleted freely.
            raw = json.dumps({"schema_version": 1, "model": MODEL, "revision": REVISION,
                              "vectors": {key: [round(value, 7) for value in vector] for key, vector in vectors.items()}},
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(raw) <= MAX_CACHE_BYTES:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(cache, raw, overwrite=cache.exists())
                except OSError:
                    cache_error = "索引缓存未保存；下次查询将重新计算。"
        limitations = ["本地BGE量化embedding余弦相似度只表示候选相关性，不证明同一事件、矛盾或伏笔回收。",
                       "仅扫描有界正式章节；每块最多200字符、最多4000块。遗漏范围见清单。",
                       "首次查询需建立索引；查询和正文均不发送到模型API。",
                       "按相似度返回前limit个候选，没有相关性阈值；低分结果可能无关。"]
        if cache_error:
            limitations.append(cache_error)
        return {"novel_id": self.project.novel_id, "mode": "local_embedding", "query": query.strip(),
                "model": MODEL, "revision": REVISION, "canonical_revision": revision,
                "scanned": len(documents), "indexed_chunks": len(chunks),
                "reused_chunks": sum(chunk["key"] in cached for chunk in chunks),
                "results": matches[:limit], "omitted": omitted, "limitations": limitations}
