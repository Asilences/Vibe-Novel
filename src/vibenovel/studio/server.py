"""Same-origin, loopback-only HTTP boundary around NovelCore.

Models return candidate text, never paths or executable tools. Files remain the
authority; this module does not implement a separate browser-only approval gate.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vibenovel import __version__
from vibenovel.config import load_config
from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.analysis import AnalysisStore
from vibenovel.core.briefs import ChapterBriefStore
from vibenovel.core.blueprints import PlanningStore
from vibenovel.core.creation import CreativeBriefStore, catalog, CREATIVE_BRIEF_PATH, CREATION_SELECTION_PATH
from vibenovel.core.documents import ConflictError, DocumentStore, canonical_read
from vibenovel.core.export import export_markdown
from vibenovel.core.files import cleanup_stage, safe_path
from vibenovel.core.imports import import_outline, verify_bundle, verify_project
from vibenovel.core.project import ProjectStore
from vibenovel.core.toolkit import WritingToolkitStore
from vibenovel.core.semantic import SemanticStore
from vibenovel.core.diagnostics import WritingDiagnosticsStore
from vibenovel.core.manuscripts import ManuscriptStore
from vibenovel.errors import MissingDocumentError, VibeNovelError
from .jobs import JobManager
from .settings import public_settings, save_settings
from .model_registry import ModelRegistry
from .routing import ProjectRouting
from .workflows import WorkflowManager
from .library_api import install_library_routes
from .reference_api import install_reference_routes, MAX_BOOK_REQUEST_BYTES
from .reference_studies import install_study_routes
from .reference_editions_api import install_edition_routes
from .reference_retrieval_api import install_reference_retrieval_routes
from .research_api import install_research_routes
from .reference_evaluations_api import install_evaluation_routes
from .reference_synthesis_api import install_synthesis_routes
from vibenovel.core.research_gateway import ResearchGateway

MAX_REQUEST_BYTES = 15 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateProject(Input):
    novel_id: Optional[StrictStr] = None
    title: StrictStr = "未命名作品"
    genre: Literal["football", "adventure", "campus", "alternate_history", "general"] = "general"


class SaveCreativeBrief(Input):
    brief: dict
    expected_hash: Optional[StrictStr]
    expected_version_id: Optional[StrictStr]


class SelectCreationOutline(Input):
    document_path: StrictStr
    document_hash: StrictStr
    document_version_id: StrictStr
    brief_hash: Optional[StrictStr] = None
    brief_version_id: Optional[StrictStr] = None
    job_id: Optional[StrictStr] = None
    expected_hash: Optional[StrictStr]
    expected_version_id: Optional[StrictStr]


class SaveDocument(Input):
    path: StrictStr
    content: StrictStr
    expected_hash: Optional[StrictStr]


class RestoreDocument(Input):
    path: StrictStr
    version_id: StrictStr
    expected_hash: StrictStr


class ImportSource(Input):
    filename: StrictStr = Field(min_length=1, max_length=240)
    content_base64: StrictStr
    encoding: Optional[Literal["utf-8", "utf-8-sig", "utf-16", "gb18030"]] = None


class CreateProposal(Input):
    target: StrictStr
    content: StrictStr
    expected_hash: Optional[StrictStr]
    reason: StrictStr = Field(min_length=1, max_length=4000)
    impact: list[StrictStr] = Field(max_length=100)


class Decision(Input):
    decision: Literal["approve", "reject"]
    note: StrictStr = Field(default="", max_length=4000)


class EditorialSelection(Input):
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=1)


class EditorialOptions(Input):
    goals: list[Literal["remove_cliches", "reduce_repetition", "show_actions", "natural_dialogue", "preserve_voice"]] = Field(max_length=5)
    intensity: Literal["conservative", "standard", "deep"]


class JobRequest(Input):
    kind: Literal["generate_story_outline", "expand_outline", "draft", "review", "rewrite", "build_blueprint", "review_evidence", "memory_delta", "analyze_reference", "extract_methods"]
    provider: Literal["api"] = "api"
    document_path: Optional[StrictStr] = None
    document_hash: Optional[StrictStr] = None
    document_version_id: Optional[StrictStr] = None
    idempotency_key: Optional[StrictStr] = Field(default=None, max_length=128)
    expected_configuration: Optional[dict] = None
    instruction: StrictStr = Field(default="", max_length=12000)
    plan_path: Optional[StrictStr] = None
    plan_hash: Optional[StrictStr] = None
    planning_start: Optional[int] = Field(default=None, ge=1, le=999999)
    planning_count: Optional[int] = Field(default=None, ge=1, le=10)
    brief_mode: Optional[Literal["compact", "full"]] = None
    selection: Optional[EditorialSelection] = None
    editorial: Optional[EditorialOptions] = None
    library_scope: Optional[Literal["personal", "book"]] = None
    source_ranges: Optional[list[dict]] = Field(default=None, max_length=40)
    purpose: Optional[Literal["scene_beats", "character_motivation", "suspense", "expression_review", "professional_method"]] = None
    analysis_entry_id: Optional[StrictStr] = None
    analysis_version_id: Optional[StrictStr] = None
    method_run_id: Optional[StrictStr] = None
    library_selection: Optional[dict] = None
    expected_library_hash: Optional[StrictStr] = None


class PlanningProposal(Input):
    path: StrictStr
    expected_hash: StrictStr
    chapter_id: Optional[StrictStr] = None


class RepairResult(Input):
    content: StrictStr


class APISettings(Input):
    base_url: StrictStr = Field(max_length=4096)
    model: StrictStr = Field(max_length=500)
    api_key: StrictStr = Field(default="", max_length=16384)
    clear_api_key: bool = False
    allow_loopback: bool = False
    price_input_usd: Optional[float] = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    price_output_usd: Optional[float] = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)


class SaveSettings(Input):
    expected_hash: Optional[StrictStr]
    api: APISettings


class SaveRegistry(Input):
    expected_hash: Optional[StrictStr]
    profiles: list[dict] = Field(min_length=1, max_length=16)


class ExpectedConfig(Input):
    expected_hash: Optional[StrictStr]


class SaveRouting(Input):
    expected_hash: Optional[StrictStr]
    policy: dict


class CreateWorkflow(Input):
    request: JobRequest
    review: bool = True
    memory: bool = True
    independent_review: bool = False


class ReviseWorkflow(Input):
    issue_ids: list[StrictStr] = Field(min_length=1, max_length=20)
    instruction: StrictStr = Field(default="", max_length=4000)


class BenchmarkPair(Input):
    candidate_a_path: StrictStr
    candidate_a_hash: StrictStr
    candidate_b_path: StrictStr
    candidate_b_hash: StrictStr


class BenchmarkVote(Input):
    decision: Literal["prefer_a", "prefer_b", "tie"]
    note: StrictStr = Field(default="", max_length=4000)
    expected_hash: Optional[StrictStr] = None


class AnalysisSample(Input):
    path: StrictStr
    hash: StrictStr


class SaveBaseline(Input):
    samples: list[AnalysisSample] = Field(max_length=10)
    expected_hash: Optional[StrictStr]


class AnalyzeDocument(Input):
    path: StrictStr
    expected_hash: StrictStr


class ManuscriptSource(Input):
    source_path: StrictStr
    source_hash: StrictStr
    start_lines: Optional[list[int]] = Field(default=None, max_length=80)


class AcceptManuscriptCandidate(Input):
    section_id: StrictStr
    base_hash: StrictStr
    candidate_path: StrictStr
    candidate_hash: StrictStr


class AuditManuscript(Input):
    section_ids: Optional[list[StrictStr]] = Field(default=None, max_length=80)


class MarkIssue(Input):
    report_path: StrictStr
    report_hash: StrictStr
    issue_id: StrictStr
    status: Literal["pending", "resolved", "ignored"]


class BuildBrief(Input):
    plan_path: StrictStr
    expected_hash: StrictStr
    mode: Literal["compact", "full"] = "compact"
    document_path: Optional[StrictStr] = None
    document_hash: Optional[StrictStr] = None


class VerifyAnalysis(Input):
    report_path: StrictStr
    report_hash: StrictStr


class SaveToolkit(Input):
    expected_hash: Optional[StrictStr]
    lexicon: list[dict] = Field(max_length=200)
    characters: list[dict] = Field(max_length=200)
    threads: list[dict] = Field(max_length=200)
    style: dict


class SemanticQuery(Input):
    query: StrictStr = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=20)


class ExportRequest(Input):
    start: Optional[int] = Field(default=None, ge=1)
    end: Optional[int] = Field(default=None, ge=1)
    output: Optional[StrictStr] = None


class LocalBoundary:
    """Reject foreign browser origins and bound even chunked request bodies."""

    def __init__(self, app, *, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope["headers"]}

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

        host = headers.get("host", "")
        try:
            parsed = urlsplit("http://" + host)
            valid_host = (parsed.hostname in LOOPBACK_HOSTS and not parsed.username and not parsed.password
                          and not parsed.path and not parsed.query and not parsed.fragment
                          and parsed.port != 0)
        except ValueError:
            valid_host = False
        if not valid_host:
            return await reject(403, "Studio 仅接受本机 Host")
        origin = headers.get("origin")
        if origin and origin != f'{scope.get("scheme", "http")}://{host}':
            return await reject(403, "拒绝跨来源请求")
        if headers.get("sec-fetch-site") == "cross-site":
            return await reject(403, "拒绝跨站请求")
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            supplied = headers.get("x-vibenovel-token", "")
            if not hmac.compare_digest(supplied.encode("utf-8"), self.token.encode("ascii")):
                return await reject(403, "请求令牌已过期，请刷新 Studio")
            if headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                return await reject(415, "请使用 JSON 请求")
        # Only the dedicated reference-book import accepts a larger body. All
        # other document, model and library endpoints keep their old boundary.
        book_upload = scope["method"] == "POST" and scope.get("path") == "/api/reference-books/import"
        request_limit = MAX_BOOK_REQUEST_BYTES if book_upload else MAX_REQUEST_BYTES
        size_message = "参考书导入请求超过128MiB原件的传输上限" if book_upload else "请求超过 15 MiB 上限"
        try:
            if int(headers.get("content-length", "0")) > request_limit:
                return await reject(413, size_message)
        except ValueError:
            return await reject(400, "Content-Length 无效")
        # Buffer only bounded local requests. This prevents chunked uploads
        # bypassing Content-Length and leaves route handlers simple/synchronous.
        parts = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > request_limit:
                return await reject(413, size_message)
            parts.append(body)
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": b"".join(parts), "more_body": False}

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ]
            await send(message)

        return await self.app(scope, replay, secure_send)


def create_app(workspace: Path, *, frontend_dir: Optional[Path] = None, job_manager=None) -> FastAPI:
    store = ProjectStore(workspace)
    config = load_config(store.workspace)
    jobs = job_manager if job_manager is not None else JobManager(store.workspace)
    workflows = WorkflowManager(jobs)
    token = secrets.token_urlsafe(32)
    frontend = (Path(frontend_dir) if frontend_dir is not None else
                Path(__file__).resolve().parents[3] / "studio" / "dist").resolve()

    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        try:
            yield
        finally:
            app.state.reference_batches.close()
            app.state.reference_studies.close()
            app.state.reference_retrieval.close()
            app.state.reference_evaluations.close()
            app.state.reference_syntheses.close()
            jobs.close()

    app = FastAPI(title="VibeNovel Studio", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs = jobs
    app.state.workspace = store.workspace
    app.add_middleware(LocalBoundary, token=token)
    install_library_routes(app, store.workspace, store)
    app.state.reference_batches = install_reference_routes(app, store.workspace, store, jobs)
    app.state.research_gateway = ResearchGateway(store.workspace)
    app.state.reference_studies = install_study_routes(app, store.workspace, app.state.research_gateway)
    app.state.reference_retrieval = install_reference_retrieval_routes(app, store.workspace, store)
    install_edition_routes(app, store.workspace)
    install_research_routes(app, store.workspace, app.state.research_gateway)
    app.state.reference_evaluations = install_evaluation_routes(app, store.workspace, app.state.research_gateway)
    app.state.reference_syntheses = install_synthesis_routes(app, store.workspace, app.state.research_gateway)

    @app.exception_handler(ConflictError)
    async def conflict_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(VibeNovelError)
    async def user_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, exc):
        return JSONResponse({"detail": "未找到作品内文件"}, status_code=404)

    @app.exception_handler(MissingDocumentError)
    async def missing_document(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        return JSONResponse({"detail": "本地存储操作失败，请检查文件权限、占用或磁盘空间"}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request, exc):
        if request.url.path in ("/api/settings", "/api/model-registry", "/api/model-registry/migrate"):
            return JSONResponse({"detail": "API 设置请求字段无效；请检查地址、模型、密钥和配置版本"}, status_code=422)
        fields = ", ".join(".".join(str(item) for item in error["loc"]) for error in exc.errors())
        return JSONResponse({"detail": "请求字段无效: " + fields}, status_code=422)

    @app.get("/api/bootstrap")
    def bootstrap():
        capability = jobs.capabilities()
        providers = [dict(capability["api"], id="api", label="API · 自动生成")]
        return {"version": __version__, "csrf_token": token, "capabilities": {
            "default_provider": capability.get("default_provider", "api"),
            "providers": providers, "tasks": ["generate_story_outline", "expand_outline", "draft", "review", "rewrite", "build_blueprint", "review_evidence", "memory_delta", "analyze_reference", "extract_methods"],
            "limits": capability.get("limits", {})}}

    @app.get("/api/settings")
    def api_settings():
        return public_settings(store.workspace)

    @app.put("/api/settings")
    def update_api_settings(body: SaveSettings):
        omitted_prices = {"price_input_usd", "price_output_usd"} - body.api.model_fields_set
        return save_settings(store.workspace, body.api.model_dump(exclude=omitted_prices), body.expected_hash)

    @app.get("/api/model-registry")
    def model_registry():
        return ModelRegistry(store.workspace).public()

    @app.put("/api/model-registry")
    def save_model_registry(body: SaveRegistry):
        return ModelRegistry(store.workspace).save({"profiles": body.profiles}, body.expected_hash)

    @app.post("/api/model-registry/migrate")
    def migrate_model_registry(body: ExpectedConfig):
        return ModelRegistry(store.workspace).migrate(body.expected_hash)

    @app.get("/api/projects/{novel_id}/routing")
    def routing_policy(novel_id: str):
        return ProjectRouting(store.get(novel_id), store.workspace).public()

    @app.put("/api/projects/{novel_id}/routing")
    def save_routing_policy(novel_id: str, body: SaveRouting):
        return ProjectRouting(store.get(novel_id), store.workspace).save(body.policy, body.expected_hash)

    @app.post("/api/projects/{novel_id}/jobs/preview")
    @app.post("/api/projects/{novel_id}/routes/preview")
    def route_preview(novel_id: str, body: JobRequest):
        project = store.get(novel_id)
        request = body.model_dump(exclude_none=True, exclude={"expected_configuration", "expected_library_hash"})
        return (jobs.creation_preview(project, request) if body.kind == "generate_story_outline"
                else jobs.preview(project, request))

    @app.get("/api/projects/{novel_id}/workflows")
    def list_workflows(novel_id: str):
        return workflows.list(store.get(novel_id))

    @app.post("/api/projects/{novel_id}/workflows", status_code=201)
    def create_workflow(novel_id: str, body: CreateWorkflow):
        return workflows.create(store.get(novel_id), body.request.model_dump(exclude_none=True),
                                body.review, body.memory, body.independent_review)

    @app.get("/api/projects/{novel_id}/workflows/{identity}")
    def read_workflow(novel_id: str, identity: str):
        return workflows.get(store.get(novel_id), identity)

    @app.post("/api/projects/{novel_id}/workflows/{identity}/advance")
    def advance_workflow(novel_id: str, identity: str):
        return workflows.advance(store.get(novel_id), identity)

    @app.post("/api/projects/{novel_id}/workflows/{identity}/pause")
    def pause_workflow(novel_id: str, identity: str):
        return workflows.stop(store.get(novel_id), identity, "pause")

    @app.post("/api/projects/{novel_id}/workflows/{identity}/cancel")
    def cancel_workflow(novel_id: str, identity: str):
        return workflows.stop(store.get(novel_id), identity, "cancel")

    @app.post("/api/projects/{novel_id}/workflows/{identity}/revise")
    def revise_workflow(novel_id: str, identity: str, body: ReviseWorkflow):
        return workflows.revise(store.get(novel_id), identity, body.issue_ids, body.instruction)

    @app.get("/api/projects/{novel_id}/benchmarks")
    def list_benchmarks(novel_id: str):
        from vibenovel.core.novelbench import NovelBenchStore
        return NovelBenchStore(store.get(novel_id)).list()

    @app.post("/api/projects/{novel_id}/benchmarks", status_code=201)
    def create_benchmark(novel_id: str, body: BenchmarkPair):
        from vibenovel.core.novelbench import NovelBenchStore
        return NovelBenchStore(store.get(novel_id)).create(body.candidate_a_path, body.candidate_a_hash,
                                                          body.candidate_b_path, body.candidate_b_hash)

    @app.get("/api/projects/{novel_id}/benchmarks/{identity}")
    def read_benchmark(novel_id: str, identity: str):
        from vibenovel.core.novelbench import NovelBenchStore
        return NovelBenchStore(store.get(novel_id)).get(identity)

    @app.post("/api/projects/{novel_id}/benchmarks/{identity}/vote")
    def vote_benchmark(novel_id: str, identity: str, body: BenchmarkVote):
        from vibenovel.core.novelbench import NovelBenchStore
        return NovelBenchStore(store.get(novel_id)).vote(identity, body.decision, body.note, body.expected_hash)

    @app.post("/api/projects/{novel_id}/benchmarks/{identity}/reveal")
    def reveal_benchmark(novel_id: str, identity: str):
        from vibenovel.core.novelbench import NovelBenchStore
        return NovelBenchStore(store.get(novel_id)).reveal(identity)

    @app.get("/api/projects/{novel_id}/benchmarks/{identity}/export")
    def export_benchmark(novel_id: str, identity: str):
        from vibenovel.core.novelbench import NovelBenchStore
        return NovelBenchStore(store.get(novel_id)).export(identity)

    def consistent_status(project):
        with canonical_read(project) as snapshot:
            return dict(project.status(), canonical_revision=snapshot["revision"])

    @app.get("/api/projects")
    def projects():
        return {"projects": [dict(project.metadata, counts=consistent_status(project)["counts"])
                             for project in store.list_projects()]}

    @app.post("/api/projects", status_code=201)
    def create_project(body: CreateProject):
        novel_id = body.novel_id if body.novel_id is not None else "story-" + uuid4().hex[:12]
        return store.init(novel_id, title=body.title or "未命名作品", genre=body.genre).metadata

    @app.get("/api/creation/catalog")
    def creation_catalog():
        return catalog()

    @app.get("/api/projects/{novel_id}/creative-brief")
    def creative_brief(novel_id: str):
        return CreativeBriefStore(store.get(novel_id)).get()

    @app.put("/api/projects/{novel_id}/creative-brief")
    def save_creative_brief(novel_id: str, body: SaveCreativeBrief):
        return CreativeBriefStore(store.get(novel_id)).save(body.brief, body.expected_hash, body.expected_version_id)

    @app.get("/api/projects/{novel_id}/creation-selection")
    def creation_selection(novel_id: str):
        return CreativeBriefStore(store.get(novel_id)).get_selection()

    @app.put("/api/projects/{novel_id}/creation-selection")
    def select_creation_outline(novel_id: str, body: SelectCreationOutline):
        project = store.get(novel_id)
        values = body.model_dump()
        if body.document_path.startswith("outlines/proposals/story-outline-"):
            source = jobs.validate_creation_candidate(project, body.document_path, body.document_hash, body.document_version_id)
            for key, expected in (("brief_hash", source["hash"]), ("brief_version_id", source["version_id"]), ("job_id", source["job_id"])):
                if values[key] is not None and values[key] != expected:
                    raise ConflictError("选用总纲的生成来源身份不匹配")
                values[key] = expected
        elif body.job_id is not None or body.brief_hash is not None or body.brief_version_id is not None:
            raise VibeNovelError("手写或导入总纲不能伪造生成任务来源")
        return CreativeBriefStore(project).select(**values)

    @app.get("/api/projects/{novel_id}")
    def project_detail(novel_id: str):
        project = store.get(novel_id)
        status = consistent_status(project)
        status.update(phase="Studio alpha", finalize_available=False,
                      single_file_approval_available=True)
        return {"project": dict(project.metadata, canonical_revision=status["canonical_revision"]), "status": status}

    @app.get("/api/projects/{novel_id}/documents")
    def documents(novel_id: str):
        return {"documents": DocumentStore(store.get(novel_id)).list_documents()}

    @app.get("/api/projects/{novel_id}/document")
    def document(novel_id: str, path: str):
        return DocumentStore(store.get(novel_id)).read(path)

    @app.put("/api/projects/{novel_id}/document")
    def save_document(novel_id: str, body: SaveDocument):
        if body.path in (CREATIVE_BRIEF_PATH, CREATION_SELECTION_PATH):
            raise VibeNovelError("创作简报和总纲选用记录请使用创意与定位页面保存")
        return DocumentStore(store.get(novel_id)).save(body.path, body.content, body.expected_hash)

    @app.get("/api/projects/{novel_id}/history")
    def history(novel_id: str, path: str):
        return {"versions": DocumentStore(store.get(novel_id)).history(path)}

    @app.get("/api/projects/{novel_id}/history/version")
    def history_version(novel_id: str, path: str, version_id: str):
        return DocumentStore(store.get(novel_id)).version(path, version_id)

    @app.post("/api/projects/{novel_id}/restore")
    def restore(novel_id: str, body: RestoreDocument):
        if body.path in (CREATIVE_BRIEF_PATH, CREATION_SELECTION_PATH):
            raise VibeNovelError("创作简报与选用记录不能通过通用文稿恢复；请在创意与定位重新保存或选用")
        return DocumentStore(store.get(novel_id)).restore(body.path, body.version_id, body.expected_hash)

    @app.get("/api/projects/{novel_id}/imports")
    def imports(novel_id: str):
        project = store.get(novel_id)
        result = []
        for entry in sorted(project.path("imports/original").iterdir(), key=lambda item: item.name):
            if entry.name.startswith(".import-"):
                continue
            metadata = verify_bundle(project, entry.name)
            result.append(dict(metadata, original_name=metadata["original_filename"],
                               source_hash=metadata["source_sha256"], extracted_hash=metadata["extracted_sha256"]))
        return {"imports": result}

    @app.post("/api/projects/{novel_id}/imports", status_code=201)
    def upload_source(novel_id: str, body: ImportSource):
        project = store.get(novel_id)
        if "/" in body.filename or "\\" in body.filename or Path(body.filename).suffix.lower() not in {".md", ".txt"}:
            raise VibeNovelError("请选择单个 MD/TXT 文件，不接受目录或其他格式")
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise VibeNovelError("上传内容不是有效 Base64") from exc
        if len(content) > config["imports"]["max_bytes"]:
            raise VibeNovelError("原件超过配置的导入上限")
        parent = project.path(".runtime")
        stage = Path(tempfile.mkdtemp(prefix=".upload-", dir=parent))
        try:
            path = safe_path(stage, body.filename)
            path.write_bytes(content)
            return import_outline(project, path, encoding=body.encoding, max_bytes=config["imports"]["max_bytes"])
        finally:
            cleanup_stage(stage, parent, ".upload-")

    @app.get("/api/projects/{novel_id}/approvals")
    def proposals(novel_id: str):
        return {"approvals": ApprovalStore(store.get(novel_id)).list()}

    @app.post("/api/projects/{novel_id}/approvals", status_code=201)
    def propose(novel_id: str, body: CreateProposal):
        return ApprovalStore(store.get(novel_id)).create(body.target, body.content, body.reason,
                 body.impact, body.expected_hash, provenance="AUTHOR")

    @app.get("/api/projects/{novel_id}/approvals/{proposal_id}")
    def proposal(novel_id: str, proposal_id: str):
        return ApprovalStore(store.get(novel_id)).get(proposal_id)

    @app.post("/api/projects/{novel_id}/approvals/{proposal_id}/decision")
    def decide(novel_id: str, proposal_id: str, body: Decision):
        return ApprovalStore(store.get(novel_id)).decide(proposal_id, body.decision, body.note)

    @app.post("/api/projects/{novel_id}/approvals/{proposal_id}/apply")
    def apply_proposal(novel_id: str, proposal_id: str):
        return ApprovalStore(store.get(novel_id)).apply(proposal_id)

    @app.get("/api/projects/{novel_id}/jobs")
    def job_list(novel_id: str):
        return {"jobs": jobs.list(store.get(novel_id))}

    @app.post("/api/projects/{novel_id}/jobs", status_code=201)
    def submit_job(novel_id: str, body: JobRequest):
        project = store.get(novel_id)
        request = body.model_dump(exclude_none=True, exclude={"expected_configuration", "expected_library_hash"})
        if body.kind == "draft" and body.plan_path and body.plan_hash is None:
            request["plan_hash"] = ApprovalStore(project).approved_outline(body.plan_path)["hash"]
        return jobs.submit(project, request, expected_configuration=body.expected_configuration,
                           expected_library_hash=body.expected_library_hash)

    @app.get("/api/projects/{novel_id}/jobs/{job_id}")
    def job_detail(novel_id: str, job_id: str):
        return jobs.get(store.get(novel_id), job_id)

    @app.post("/api/projects/{novel_id}/jobs/{job_id}/cancel")
    def cancel_job(novel_id: str, job_id: str):
        return jobs.cancel(store.get(novel_id), job_id)

    @app.post("/api/projects/{novel_id}/jobs/{job_id}/pause")
    def pause_job(novel_id: str, job_id: str):
        return jobs.pause(store.get(novel_id), job_id)

    @app.post("/api/projects/{novel_id}/jobs/{job_id}/resume")
    def resume_job(novel_id: str, job_id: str):
        return jobs.resume(store.get(novel_id), job_id)

    @app.post("/api/projects/{novel_id}/jobs/{job_id}/retry", status_code=201)
    def retry_job(novel_id: str, job_id: str):
        return jobs.retry(store.get(novel_id), job_id)

    @app.post("/api/projects/{novel_id}/jobs/{job_id}/repair")
    def repair_result(novel_id: str, job_id: str, body: RepairResult):
        return jobs.repair_result(store.get(novel_id), job_id, body.content)

    @app.post("/api/projects/{novel_id}/jobs/{job_id}/recover")
    def recover_result(novel_id: str, job_id: str):
        return jobs.recover_result(store.get(novel_id), job_id)

    @app.post("/api/projects/{novel_id}/export")
    def export(novel_id: str, body: ExportRequest):
        project = store.get(novel_id)
        output = body.output or f"{novel_id}-{uuid4().hex[:8]}.md"
        with canonical_read(project):
            result = export_markdown(project, start=body.start, end=body.end, output=output)
            content = project.path("exports/" + output).read_text(encoding="utf-8")
        return {"filename": output, "content": content, "sha256": result["sha256"],
                "chapter_count": result["chapter_count"]}

    @app.get("/api/projects/{novel_id}/verify")
    def verify(novel_id: str):
        project = store.get(novel_id)
        with canonical_read(project):
            return verify_project(project)

    @app.get("/api/projects/{novel_id}/world")
    def world(novel_id: str):
        return {"documents": [item for item in DocumentStore(store.get(novel_id)).list_documents()
                               if item["path"].startswith(("bible/", "memory/"))],
                "semantic_extraction": "not_run"}

    @app.get("/api/projects/{novel_id}/planning")
    def planning_packages(novel_id: str):
        return {"packages": PlanningStore(store.get(novel_id)).list()}

    @app.get("/api/projects/{novel_id}/planning/package")
    def planning_package(novel_id: str, path: str):
        return PlanningStore(store.get(novel_id)).read(path)

    @app.post("/api/projects/{novel_id}/planning/proposal", status_code=201)
    def planning_proposal(novel_id: str, body: PlanningProposal):
        return PlanningStore(store.get(novel_id)).propose(body.path, body.expected_hash, body.chapter_id)

    @app.get("/api/projects/{novel_id}/analysis/baseline")
    def style_baseline(novel_id: str):
        return AnalysisStore(store.get(novel_id)).baseline()

    @app.put("/api/projects/{novel_id}/analysis/baseline")
    def save_style_baseline(novel_id: str, body: SaveBaseline):
        return AnalysisStore(store.get(novel_id)).set_baseline(
            [sample.model_dump() for sample in body.samples], body.expected_hash)

    @app.post("/api/projects/{novel_id}/analysis/reports", status_code=201)
    def analyze_document(novel_id: str, body: AnalyzeDocument):
        return AnalysisStore(store.get(novel_id)).analyze(body.path, body.expected_hash)

    @app.get("/api/projects/{novel_id}/analysis/report")
    def analysis_report(novel_id: str, path: str):
        return AnalysisStore(store.get(novel_id)).report(path)

    @app.post("/api/projects/{novel_id}/analysis/mark")
    def mark_analysis_issue(novel_id: str, body: MarkIssue):
        return AnalysisStore(store.get(novel_id)).mark(
            body.report_path, body.report_hash, body.issue_id, body.status)

    @app.post("/api/projects/{novel_id}/analysis/verify")
    def verify_analysis(novel_id: str, body: VerifyAnalysis):
        return AnalysisStore(store.get(novel_id)).verify(body.report_path, body.report_hash)

    @app.get("/api/projects/{novel_id}/analysis/toolkit")
    def writing_toolkit(novel_id: str):
        return WritingToolkitStore(store.get(novel_id)).settings()

    @app.put("/api/projects/{novel_id}/analysis/toolkit")
    def save_writing_toolkit(novel_id: str, body: SaveToolkit):
        return WritingToolkitStore(store.get(novel_id)).save(
            body.model_dump(exclude={"expected_hash"}), body.expected_hash)

    @app.get("/api/projects/{novel_id}/analysis/structure")
    def story_structure(novel_id: str):
        return WritingToolkitStore(store.get(novel_id)).structure()

    @app.get("/api/projects/{novel_id}/analysis/diagnostics")
    def writing_diagnostics(novel_id: str):
        return WritingDiagnosticsStore(store.get(novel_id)).report()

    @app.get("/api/projects/{novel_id}/analysis/semantic/status")
    def semantic_status(novel_id: str):
        return SemanticStore(store.get(novel_id), store.workspace).status()

    @app.post("/api/projects/{novel_id}/analysis/semantic/search")
    def semantic_search(novel_id: str, body: SemanticQuery):
        semantic = SemanticStore(store.get(novel_id), store.workspace)
        if not semantic.status()["available"]:
            raise ConflictError(semantic.status()["reason"])
        return semantic.search(body.query, body.limit)

    @app.get("/api/projects/{novel_id}/analysis/search")
    def search_history(novel_id: str, query: str = Query(min_length=1, max_length=200),
                       limit: int = Query(default=10, ge=1, le=20)):
        return AnalysisStore(store.get(novel_id)).search(query, limit)

    @app.post("/api/projects/{novel_id}/analysis/brief")
    def chapter_brief(novel_id: str, body: BuildBrief):
        return ChapterBriefStore(store.get(novel_id)).build(body.plan_path, body.expected_hash, body.mode,
                                                          document_path=body.document_path,
                                                          document_hash=body.document_hash)

    @app.get("/api/projects/{novel_id}/manuscripts")
    def manuscripts(novel_id: str):
        return ManuscriptStore(store.get(novel_id)).list()

    @app.post("/api/projects/{novel_id}/manuscripts/preview")
    def manuscript_preview(novel_id: str, body: ManuscriptSource):
        return ManuscriptStore(store.get(novel_id)).preview(body.source_path, body.source_hash, body.start_lines)

    @app.post("/api/projects/{novel_id}/manuscripts", status_code=201)
    def prepare_manuscript(novel_id: str, body: ManuscriptSource):
        return ManuscriptStore(store.get(novel_id)).prepare(body.source_path, body.source_hash, body.start_lines)

    @app.get("/api/projects/{novel_id}/manuscripts/{manuscript_id}")
    def manuscript(novel_id: str, manuscript_id: str):
        return ManuscriptStore(store.get(novel_id)).read(manuscript_id)

    @app.post("/api/projects/{novel_id}/manuscripts/{manuscript_id}/accept")
    def accept_manuscript(novel_id: str, manuscript_id: str, body: AcceptManuscriptCandidate):
        return ManuscriptStore(store.get(novel_id)).accept(manuscript_id, body.section_id,
            body.base_hash, body.candidate_path, body.candidate_hash)

    @app.get("/api/projects/{novel_id}/manuscripts/{manuscript_id}/export")
    def export_manuscript(novel_id: str, manuscript_id: str):
        return ManuscriptStore(store.get(novel_id)).export(manuscript_id)

    @app.post("/api/projects/{novel_id}/manuscripts/{manuscript_id}/audit")
    def audit_manuscript(novel_id: str, manuscript_id: str, body: AuditManuscript):
        from vibenovel.core.manuscript_audit import ManuscriptAuditStore
        return ManuscriptAuditStore(store.get(novel_id)).analyze(manuscript_id, body.section_ids)

    if frontend.is_dir():
        assets = safe_path(frontend, "assets")
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        index_path = safe_path(frontend, "index.html")
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                            "<title>VibeNovel Studio</title><body><h1>VibeNovel Studio</h1>"
                            "<p>前端尚未构建。在源码 studio 目录执行 npm ci 和 npm run build，"
                            "然后刷新本页。</p></body></html>", status_code=503)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="VibeNovel Studio — 本地作者工作台")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--frontend", type=Path, help="前端 dist 目录（默认源码 studio/dist）")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口需要 1–65535")
    import uvicorn
    try:
        app = create_app(args.workspace, frontend_dir=args.frontend)
    except (VibeNovelError, OSError) as exc:
        parser.error(str(exc))
    print(f"VibeNovel Studio http://127.0.0.1:{args.port} — Ctrl+C 停止后端", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
