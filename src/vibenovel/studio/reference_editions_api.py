"""Explicit author-reviewed derived reference editions; no model calls."""
from __future__ import annotations

from typing import Literal, Optional
from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from vibenovel.core.reference_editions import ReferenceEditionStore


class EditionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Segment(EditionInput):
    chapter_id: StrictStr = Field(min_length=32, max_length=32)
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(ge=0)


class EditionChapter(EditionInput):
    title: StrictStr = Field(min_length=1, max_length=500)
    kind: Literal["body", "ancillary"]
    volume_title: StrictStr = Field(default="", max_length=500)
    segments: list[Segment] = Field(min_length=1, max_length=1000)


class ExactPatch(EditionInput):
    chapter_id: StrictStr = Field(min_length=32, max_length=32)
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(gt=0)
    expected_text: StrictStr = Field(min_length=1, max_length=10000)
    replacement: StrictStr = Field(max_length=10000)
    reason: StrictStr = Field(min_length=1, max_length=1000)


class EditionPreview(EditionInput):
    base_edition_id: StrictStr = Field(min_length=32, max_length=32)
    expected_hash: StrictStr = Field(min_length=64, max_length=64)
    reason: StrictStr = Field(min_length=1, max_length=2000)
    chapters: list[EditionChapter] = Field(min_length=1, max_length=20000)
    patches: list[ExactPatch] = Field(default_factory=list, max_length=1000)


class EditionCreate(EditionPreview):
    expected_preview: StrictStr = Field(min_length=64, max_length=64)
    idempotency_key: StrictStr = Field(min_length=16, max_length=80)


class EditionPublish(EditionInput):
    expected_candidate_hash: StrictStr = Field(min_length=64, max_length=64)
    expected_hash: StrictStr = Field(min_length=64, max_length=64)
    make_current: bool = False


class EditionSelect(EditionInput):
    expected_hash: StrictStr = Field(min_length=64, max_length=64)


def install_edition_routes(app: FastAPI, workspace):
    store = ReferenceEditionStore(workspace)

    @app.get("/api/reference-books/{book_id}/edition-audit")
    def audit(book_id: str):
        return store.audit(book_id)

    @app.get("/api/reference-books/{book_id}/edition-candidates")
    def candidates(book_id: str):
        return {"candidates": store.candidates(book_id)}

    @app.post("/api/reference-books/{book_id}/edition-candidates/preview")
    def preview(book_id: str, body: EditionPreview):
        return store.preview(book_id, body.model_dump())

    @app.post("/api/reference-books/{book_id}/edition-candidates", status_code=201)
    def create(book_id: str, body: EditionCreate):
        return store.create(book_id, body.model_dump(exclude={"expected_preview", "idempotency_key"}), expected_preview=body.expected_preview, idempotency_key=body.idempotency_key)

    @app.get("/api/reference-books/{book_id}/edition-candidates/{candidate_id}")
    def candidate(book_id: str, candidate_id: str):
        return store.candidate(book_id, candidate_id)

    @app.post("/api/reference-books/{book_id}/edition-candidates/{candidate_id}/publish")
    def publish(book_id: str, candidate_id: str, body: EditionPublish):
        return store.publish(book_id, candidate_id, **body.model_dump())

    @app.post("/api/reference-books/{book_id}/editions/{edition_id}/select")
    def select(book_id: str, edition_id: str, body: EditionSelect):
        return store.select(book_id, edition_id, expected_hash=body.expected_hash)

    @app.get("/api/reference-books/{book_id}/editions/{edition_id}/map")
    def mapping(book_id: str, edition_id: str, chapter_id: Optional[str] = None,
                start_char: Optional[int] = Query(default=None, ge=0), end_char: Optional[int] = Query(default=None, gt=0),
                direction: Literal["forward", "backward"] = "forward", offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        return store.mapping(book_id, edition_id, chapter_id=chapter_id, start_char=start_char, end_char=end_char, direction=direction, offset=offset, limit=limit)

    return store
