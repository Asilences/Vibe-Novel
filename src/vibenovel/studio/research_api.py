"""Private research account and evidence-bound method publication routes."""
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vibenovel.core.library import LibraryStore


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Account(Input):
    model_id: StrictStr = Field(min_length=1, max_length=80)
    max_cost_usd: float = Field(gt=0, le=10000)
    price_input_usd: float = Field(gt=0)
    price_output_usd: float = Field(gt=0)
    price_source: StrictStr = Field(default="", max_length=2000)
    max_calls: StrictInt = Field(default=256, ge=1, le=4096)
    expected_hash: StrictStr = Field(min_length=64, max_length=64)


class MethodSelection(Input):
    method_indices: list[StrictInt] = Field(min_length=1, max_length=6)


class MethodPublish(MethodSelection):
    expected_preview: StrictStr = Field(min_length=64, max_length=64)
    idempotency_key: StrictStr = Field(min_length=16, max_length=80)


def install_research_routes(app: FastAPI, workspace, gateway):
    @app.get("/api/research-account")
    def account():
        return gateway.status()

    @app.put("/api/research-account")
    def configure(body: Account):
        return gateway.configure(**body.model_dump())

    @app.post("/api/reference-studies/{study_id}/methods/preview")
    def preview_methods(study_id: str, body: MethodSelection):
        return LibraryStore(workspace).preview_study_methods(study_id, body.method_indices)

    @app.post("/api/reference-studies/{study_id}/methods", status_code=201)
    def publish_methods(study_id: str, body: MethodPublish):
        return LibraryStore(workspace).publish_study_methods(study_id, body.method_indices,
            body.expected_preview, body.idempotency_key)
