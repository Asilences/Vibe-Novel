"""Offline reference retrieval API, under Studio's existing CSRF boundary."""
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vibenovel.core.reference_retrieval import ReferenceRetrievalStore


class RetrievalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Layer = Literal["original", "structure", "expression", "method"]


class IndexPreview(RetrievalInput):
    book_ids: list[StrictStr] = Field(min_length=1, max_length=16)
    layers: list[Layer] = Field(min_length=1, max_length=4)


class IndexBuild(IndexPreview):
    expected_preview: StrictStr = Field(min_length=64, max_length=64)


class RetrievalSearch(RetrievalInput):
    query: StrictStr = Field(min_length=1, max_length=200)
    layers: list[Layer] | None = None
    limit: int = Field(default=12, ge=1, le=30)
    semantic: bool = True
    relationships: bool = True


class RetrievalSelection(RetrievalInput):
    ids: list[StrictStr] = Field(min_length=1, max_length=30)


def install_reference_retrieval_routes(app: FastAPI, workspace, projects=None):
    retrieval = ReferenceRetrievalStore(workspace)

    @app.get("/api/reference-retrieval")
    def overview():
        return {"indexes": [retrieval.get(item["id"]) for item in retrieval.list_indexes()], "engine": retrieval.engine.status()}

    @app.post("/api/reference-retrieval/preview")
    def preview(body: IndexPreview):
        return retrieval.preview(body.book_ids, body.layers)

    @app.post("/api/reference-retrieval/indexes", status_code=201)
    def build(body: IndexBuild):
        return retrieval.build(body.book_ids, body.layers, body.expected_preview)

    @app.get("/api/reference-retrieval/indexes/{identity}")
    def detail(identity: str):
        return retrieval.get(identity)

    @app.post("/api/reference-retrieval/indexes/{identity}/cancel")
    def cancel(identity: str):
        return retrieval.cancel(identity)

    @app.post("/api/reference-retrieval/indexes/{identity}/search")
    def search(identity: str, body: RetrievalSearch):
        return retrieval.search(identity, body.query, layers=body.layers, limit=body.limit,
                                semantic=body.semantic, relationships=body.relationships)

    @app.post("/api/reference-retrieval/indexes/{identity}/selection/preview")
    def selection(identity: str, body: RetrievalSelection):
        result = retrieval.selection(identity, body.ids, outbound=True)
        return {**result, "library_selection": {"retrieval_selection": {"index_id": identity, "ids": body.ids,
                "expected_preview": result["hash"]}}, "warnings": ["仅供作者明确加入后续任务；预览不调用模型。"]}

    return retrieval
