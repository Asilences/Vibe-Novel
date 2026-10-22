"""Local, bounded reference-book operations; never a novel's canonical store."""
from __future__ import annotations

import base64
import binascii
from typing import Literal, Optional

from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr, StrictInt

from vibenovel.core.library import LibraryStore
from vibenovel.core.reference_books import ReferenceBookStore
from vibenovel.errors import VibeNovelError
from .reference_jobs import ReferenceBatchManager

MAX_BOOK_BYTES = 128 * 1024 * 1024
MAX_BOOK_REQUEST_BYTES = ((MAX_BOOK_BYTES + 2) // 3) * 4 + 65536


class ReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BookImport(ReferenceInput):
    filename: StrictStr = Field(min_length=1, max_length=240)
    content_base64: StrictStr = Field(min_length=1, max_length=((MAX_BOOK_BYTES + 2) // 3) * 4)
    title: StrictStr = Field(default="", max_length=500)
    author: StrictStr = Field(default="", max_length=500)
    encoding: Optional[Literal["utf-8", "utf-8-sig", "utf-16", "gb18030"]] = None


class BookPolicy(ReferenceInput):
    expected_hash: StrictStr = Field(min_length=64, max_length=64)
    outbound: Literal["local", "excerpts"]
    withdrawn: bool = False


class BatchPreview(ReferenceInput):
    novel_id: StrictStr = Field(min_length=1, max_length=64)
    chapter_ids: list[StrictStr] = Field(default_factory=list, max_length=20000)
    purpose: StrictStr = Field(default="内容结构与表达分析", min_length=1, max_length=2000)
    budget_max_usd: float = Field(default=1.0, gt=0, le=10000)
    max_output_tokens: int = Field(default=2048, ge=256, le=32000)


class BatchCreate(BatchPreview):
    expected_preview: StrictStr = Field(min_length=64, max_length=64)
    idempotency_key: StrictStr = Field(min_length=16, max_length=80)


class MethodPreview(ReferenceInput):
    method_indices: list[StrictInt] = Field(min_length=1, max_length=6)


class BatchResponseRepair(ReferenceInput):
    expected_hash: StrictStr = Field(min_length=64, max_length=64)
    text: StrictStr = Field(min_length=1, max_length=1024 * 1024)
    reason: StrictStr = Field(min_length=1, max_length=2000)


class MethodPublish(MethodPreview):
    expected_preview: StrictStr = Field(min_length=64, max_length=64)
    idempotency_key: StrictStr = Field(min_length=16, max_length=80)


def install_reference_routes(app: FastAPI, workspace, projects, jobs):
    books = ReferenceBookStore(workspace)
    batches = ReferenceBatchManager(workspace, projects, jobs)

    @app.get("/api/reference-books")
    def overview():
        return {"books": books.list_books()}

    @app.post("/api/reference-books/import", status_code=201)
    def import_book(body: BookImport):
        try:
            raw = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise VibeNovelError("参考书原件的Base64编码无效") from None
        if not raw or len(raw) > MAX_BOOK_BYTES:
            raise VibeNovelError("参考书原件需要1字节至128MiB")
        return books.import_book(body.filename, raw, title=body.title,
                                 author=body.author, encoding=body.encoding)

    @app.get("/api/reference-books/{book_id}")
    def detail(book_id: str, edition_id: Optional[str] = None,
               offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200)):
        return books.detail(book_id, edition_id, offset=offset, limit=limit)

    @app.get("/api/reference-books/{book_id}/chapters/{chapter_id}")
    def chapter(book_id: str, chapter_id: str, edition_id: Optional[str] = None,
                offset: int = Query(default=0, ge=0), limit: int = Query(default=24000, ge=1, le=60000)):
        return books.chapter(book_id, chapter_id, edition_id, offset=offset, limit=limit)

    @app.put("/api/reference-books/{book_id}/policy")
    def policy(book_id: str, body: BookPolicy):
        return books.update_policy(book_id, expected_hash=body.expected_hash,
                                   outbound=body.outbound, withdrawn=body.withdrawn)

    @app.get("/api/reference-books/{book_id}/search")
    def search(book_id: str, q: str = Query(min_length=1, max_length=200),
               edition_id: Optional[str] = None, limit: int = Query(default=20, ge=1, le=100)):
        return books.search(book_id, q, edition_id=edition_id, limit=limit)

    @app.post("/api/reference-books/{book_id}/verify")
    def verify(book_id: str):
        return books.verify(book_id)

    @app.get("/api/reference-books/{book_id}/batches")
    def list_batches(book_id: str):
        books.detail(book_id, limit=1)
        return {"batches": batches.list(book_id)}

    @app.post("/api/reference-books/{book_id}/batches/preview")
    def preview_batch(book_id: str, body: BatchPreview):
        return batches.preview(book_id, body.model_dump())

    @app.post("/api/reference-books/{book_id}/batches", status_code=201)
    def create_batch(book_id: str, body: BatchCreate):
        values = body.model_dump(exclude={"expected_preview", "idempotency_key"})
        return batches.create(book_id, values, body.expected_preview, body.idempotency_key)

    @app.get("/api/reference-books/{book_id}/batches/{batch_id}")
    def read_batch(book_id: str, batch_id: str,
                   offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
        return batches.get(book_id, batch_id, offset=offset, limit=limit)

    @app.get("/api/reference-books/{book_id}/batches/{batch_id}/nodes/{node_id}")
    def read_node(book_id: str, batch_id: str, node_id: str):
        return batches.get_node(book_id, batch_id, node_id)

    @app.get("/api/reference-books/{book_id}/batches/{batch_id}/nodes/{node_id}/response")
    def read_response(book_id: str, batch_id: str, node_id: str):
        return batches.response(book_id, batch_id, node_id)

    @app.put("/api/reference-books/{book_id}/batches/{batch_id}/nodes/{node_id}/response")
    def repair_response(book_id: str, batch_id: str, node_id: str, body: BatchResponseRepair):
        return batches.repair_response(book_id, batch_id, node_id, body.expected_hash, body.text, body.reason)

    @app.post("/api/reference-books/{book_id}/batches/{batch_id}/nodes/{node_id}/methods/preview")
    def preview_methods(book_id: str, batch_id: str, node_id: str, body: MethodPreview):
        return LibraryStore(workspace).preview_reference_methods(
            book_id, batch_id, node_id, body.method_indices)

    @app.post("/api/reference-books/{book_id}/batches/{batch_id}/nodes/{node_id}/methods", status_code=201)
    def publish_methods(book_id: str, batch_id: str, node_id: str, body: MethodPublish):
        return LibraryStore(workspace).publish_reference_methods(
            book_id, batch_id, node_id, body.method_indices,
            body.expected_preview, body.idempotency_key)

    @app.post("/api/reference-books/{book_id}/batches/{batch_id}/{action}")
    def control_batch(book_id: str, batch_id: str, action: Literal["start", "pause", "cancel", "retry"]):
        return batches.control(book_id, batch_id, action)

    return batches
