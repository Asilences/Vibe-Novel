"""Explicit retrospective synthesis through the private research account."""
from typing import Literal, Optional
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from vibenovel.core.reference_synthesis import ReferenceSynthesisManager
from vibenovel.core.library import LibraryStore


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Preview(Input):
    study_id: StrictStr = Field(min_length=32, max_length=32)
    stage_size: StrictInt = Field(default=30, ge=30, le=50)
    budget_max_usd: float = Field(gt=0, le=10000)
    max_output_tokens: StrictInt = Field(default=6144, ge=2048, le=16000)


class Create(Preview):
    expected_preview: StrictStr = Field(min_length=64, max_length=64)
    idempotency_key: StrictStr = Field(min_length=16, max_length=80)


class Repair(Input):
    expected_hash: StrictStr = Field(min_length=64, max_length=64)
    raw: StrictStr = Field(min_length=1, max_length=100000)
    reason: StrictStr = Field(min_length=1, max_length=2000)


class Methods(Input):
    method_indices: list[StrictInt] = Field(min_length=1, max_length=6)


class Publish(Methods):
    expected_preview: StrictStr = Field(min_length=64, max_length=64)
    idempotency_key: StrictStr = Field(min_length=16, max_length=80)


def install_synthesis_routes(app: FastAPI, workspace, gateway):
    manager = ReferenceSynthesisManager(workspace, gateway, recover=True)
    library = LibraryStore(workspace)

    @app.get("/api/reference-syntheses")
    def listing(study_id: Optional[str] = None):
        return {"syntheses": manager.list(study_id)}

    @app.post("/api/reference-syntheses/preview")
    def preview(body: Preview):
        return manager.preview(body.model_dump())[0]

    @app.post("/api/reference-syntheses", status_code=201)
    def create(body: Create):
        return manager.create(body.model_dump(exclude={"expected_preview", "idempotency_key"}), body.expected_preview, body.idempotency_key)

    @app.get("/api/reference-syntheses/{synthesis_id}")
    def detail(synthesis_id: str):
        return manager.detail(synthesis_id)

    @app.get("/api/reference-syntheses/{synthesis_id}/export")
    def export(synthesis_id: str):
        return manager.export_verified(synthesis_id)

    @app.get("/api/reference-syntheses/{synthesis_id}/nodes/{node_id}/response")
    def response(synthesis_id: str, node_id: str):
        return manager.response(synthesis_id, node_id)

    @app.post("/api/reference-syntheses/{synthesis_id}/nodes/{node_id}/repair")
    def repair(synthesis_id: str, node_id: str, body: Repair):
        return manager.repair(synthesis_id, node_id, **body.model_dump())

    @app.post("/api/reference-syntheses/{synthesis_id}/methods/preview")
    def methods(synthesis_id: str, body: Methods):
        return library.preview_synthesis_methods(synthesis_id, body.method_indices)

    @app.post("/api/reference-syntheses/{synthesis_id}/methods", status_code=201)
    def publish(synthesis_id: str, body: Publish):
        return library.publish_synthesis_methods(synthesis_id, body.method_indices, body.expected_preview, body.idempotency_key)

    @app.post("/api/reference-syntheses/{synthesis_id}/{action}")
    def control(synthesis_id: str, action: Literal["start", "pause", "cancel"]):
        return manager.control(synthesis_id, action)

    return manager
