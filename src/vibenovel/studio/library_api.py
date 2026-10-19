"""Bounded local HTTP operations for private, versioned reference libraries."""
from __future__ import annotations

import base64
import binascii
from typing import Literal, Optional

from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vibenovel.core.library import LibraryStore, MAX_SOURCE_BYTES
from vibenovel.errors import VibeNovelError

Scope = Literal["personal", "book"]


class LibraryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceImport(LibraryInput):
    filename: StrictStr = Field(min_length=1, max_length=240)
    content_base64: StrictStr = Field(max_length=14 * 1024 * 1024)
    encoding: Optional[Literal["utf-8", "utf-8-sig", "utf-16", "gb18030"]] = None
    metadata: dict = Field(default_factory=dict)


class SourceUpdate(LibraryInput):
    metadata: dict
    withdrawn: bool = False
    reason: StrictStr = Field(default="", max_length=2000)
    expected_hash: StrictStr


class EntrySave(LibraryInput):
    entry: dict
    expected_hash: Optional[StrictStr]


class CollectionSave(LibraryInput):
    collection: dict
    expected_hash: Optional[StrictStr]


class QuestionSave(LibraryInput):
    question: dict
    expected_hash: Optional[StrictStr]


class PolicySave(LibraryInput):
    policy: dict
    expected_hash: Optional[StrictStr]


class BindingSave(LibraryInput):
    entry_id: StrictStr
    version_id: Optional[StrictStr] = None
    source_scope: Scope = "personal"
    purpose: StrictStr = "reference"
    binding_id: Optional[StrictStr] = None
    expected_hash: Optional[StrictStr] = None


class AdoptMethod(LibraryInput):
    expected_hash: StrictStr


def install_library_routes(app: FastAPI, workspace, projects):
    """All writes remain inside the parent's same-origin/CSRF boundary."""
    def library(scope, novel_id):
        if scope == "personal":
            if novel_id is not None:
                raise VibeNovelError("个人资料库不接受作品ID，请明确切换资料范围")
            return LibraryStore(workspace)
        if not novel_id:
            raise VibeNovelError("本书资料架需要明确选择作品")
        return LibraryStore(workspace, projects.get(novel_id))

    @app.get("/api/library/{scope}")
    def overview(scope: Scope, novel_id: Optional[str] = None):
        return library(scope, novel_id).overview()

    @app.post("/api/library/{scope}/sources", status_code=201)
    def import_source(scope: Scope, body: SourceImport, novel_id: Optional[str] = None):
        target = library(scope, novel_id)
        try:
            raw = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise VibeNovelError("资料原件的Base64编码无效") from None
        if not raw or len(raw) > MAX_SOURCE_BYTES:
            raise VibeNovelError("资料原件需要1字节至10MiB")
        return target.import_source(body.filename, raw, encoding=body.encoding, metadata=body.metadata)

    @app.get("/api/library/{scope}/sources/{identity}")
    def read_source(scope: Scope, identity: str, novel_id: Optional[str] = None,
                    version_id: Optional[str] = None):
        return library(scope, novel_id).source(identity, version_id)

    @app.put("/api/library/{scope}/sources/{identity}")
    def update_source(scope: Scope, identity: str, body: SourceUpdate, novel_id: Optional[str] = None):
        return library(scope, novel_id).save_source_metadata(identity, {
            "metadata": body.metadata, "withdrawn": body.withdrawn, "withdrawal_reason": body.reason,
        }, body.expected_hash)

    @app.get("/api/library/{scope}/sources/{identity}/history")
    def source_history(scope: Scope, identity: str, novel_id: Optional[str] = None):
        return {"versions": library(scope, novel_id).history("source", identity)}

    @app.get("/api/library/{scope}/entries")
    def entries(scope: Scope, novel_id: Optional[str] = None):
        return {"entries": library(scope, novel_id).overview()["entries"]}

    @app.post("/api/library/{scope}/entries", status_code=201)
    def create_entry(scope: Scope, body: EntrySave, novel_id: Optional[str] = None):
        return library(scope, novel_id).save_entry(body.entry, body.expected_hash)

    @app.get("/api/library/{scope}/entries/{identity}")
    def read_entry(scope: Scope, identity: str, novel_id: Optional[str] = None,
                   version_id: Optional[str] = None):
        return library(scope, novel_id).entry(identity, version_id)

    @app.put("/api/library/{scope}/entries/{identity}")
    def update_entry(scope: Scope, identity: str, body: EntrySave, novel_id: Optional[str] = None):
        if body.entry.get("id", identity) != identity:
            raise VibeNovelError("资料卡ID与请求路径不一致")
        return library(scope, novel_id).save_entry(dict(body.entry, id=identity), body.expected_hash)

    @app.get("/api/library/{scope}/entries/{identity}/history")
    def entry_history(scope: Scope, identity: str, novel_id: Optional[str] = None):
        return {"versions": library(scope, novel_id).history("entry", identity)}

    @app.post("/api/library/{scope}/entries/{identity}/adopt")
    def adopt_method(scope: Scope, identity: str, body: AdoptMethod, novel_id: Optional[str] = None):
        return library(scope, novel_id).adopt_method(identity, body.expected_hash)

    @app.get("/api/library/{scope}/collections")
    def collections(scope: Scope, novel_id: Optional[str] = None):
        return {"collections": library(scope, novel_id).collections()}

    @app.post("/api/library/{scope}/collections", status_code=201)
    def save_collection(scope: Scope, body: CollectionSave, novel_id: Optional[str] = None):
        return library(scope, novel_id).save_collection(body.collection, body.expected_hash)

    @app.get("/api/library/{scope}/research")
    def questions(scope: Scope, novel_id: Optional[str] = None):
        return {"questions": library(scope, novel_id).questions()}

    @app.post("/api/library/{scope}/research", status_code=201)
    def save_question(scope: Scope, body: QuestionSave, novel_id: Optional[str] = None):
        return library(scope, novel_id).save_question(body.question, body.expected_hash)

    @app.post("/api/library/{scope}/bindings", status_code=201)
    def bind(scope: Scope, body: BindingSave, novel_id: Optional[str] = None):
        if scope != "book":
            raise VibeNovelError("请在本书资料架中选用资料")
        return library(scope, novel_id).bind(body.entry_id, body.expected_hash,
            version_id=body.version_id, source_scope=body.source_scope,
            purpose=body.purpose, binding_id=body.binding_id)

    @app.put("/api/library/{scope}/policy")
    def save_policy(scope: Scope, body: PolicySave, novel_id: Optional[str] = None):
        if scope != "book":
            raise VibeNovelError("自动选材策略按作品保存，请选择本书资料架")
        return library(scope, novel_id).save_policy(body.policy, body.expected_hash)

    @app.get("/api/library/{scope}/search")
    def search(scope: Scope, q: str = Query(min_length=1, max_length=200),
               novel_id: Optional[str] = None, limit: int = Query(default=20, ge=1, le=100)):
        return library(scope, novel_id).search(q, limit)

    @app.get("/api/library/{scope}/method-bundles/{identity}")
    def method_bundle(scope: Scope, identity: str, novel_id: Optional[str] = None):
        result = library(scope, novel_id).method_bundle(identity)
        if result is None:
            raise VibeNovelError("未找到已发布的方法候选包")
        return result
