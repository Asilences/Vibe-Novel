"""Local API for explicit, budgeted method-effect experiments."""
from typing import Literal
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from vibenovel.core.reference_evaluations import ReferenceEvaluationManager


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)


class Preview(Input):
    study_id: StrictStr = Field(min_length=32,max_length=32)
    method_indices: list[StrictInt] = Field(min_length=1,max_length=6)
    brief: StrictStr = Field(min_length=1,max_length=6000)
    budget_max_usd: float = Field(gt=0,le=10000)
    max_output_tokens: StrictInt = Field(default=4096,ge=512,le=16000)


class Create(Preview):
    expected_preview: StrictStr = Field(min_length=64,max_length=64)
    idempotency_key: StrictStr = Field(min_length=16,max_length=80)


class Repair(Input):
    expected_hash: StrictStr = Field(min_length=64,max_length=64)
    raw: StrictStr = Field(min_length=1,max_length=100000)
    reason: StrictStr = Field(min_length=1,max_length=2000)


class Human(Input):
    expected_hash: StrictStr = Field(min_length=64,max_length=64)
    ranking: list[StrictStr] = Field(min_length=3,max_length=3)
    ratings: dict[StrictStr,StrictInt]
    note: StrictStr = Field(default="",max_length=4000)


def install_evaluation_routes(app: FastAPI,workspace,gateway):
    manager=ReferenceEvaluationManager(workspace,gateway)

    @app.get("/api/reference-evaluations")
    def listing(): return {"evaluations":manager.list()}

    @app.post("/api/reference-evaluations/preview")
    def preview(body:Preview): return manager.preview(body.model_dump())

    @app.post("/api/reference-evaluations",status_code=201)
    def create(body:Create):
        return manager.create(body.model_dump(exclude={"expected_preview","idempotency_key"}),body.expected_preview,body.idempotency_key)

    @app.get("/api/reference-evaluations/{evaluation_id}")
    def detail(evaluation_id:str): return manager.detail(evaluation_id)

    @app.post("/api/reference-evaluations/{evaluation_id}/human-review")
    def human(evaluation_id:str,body:Human): return manager.human_review(evaluation_id,**body.model_dump())

    @app.get("/api/reference-evaluations/{evaluation_id}/nodes/{node_id}/response")
    def response(evaluation_id:str,node_id:str): return manager.response(evaluation_id,node_id)

    @app.post("/api/reference-evaluations/{evaluation_id}/nodes/{node_id}/repair")
    def repair(evaluation_id:str,node_id:str,body:Repair): return manager.repair(evaluation_id,node_id,**body.model_dump())

    @app.post("/api/reference-evaluations/{evaluation_id}/{action}")
    def control(evaluation_id:str,action:Literal["start","pause","cancel"]): return manager.control(evaluation_id,action)

    return manager
