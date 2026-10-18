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

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vibenovel import __version__
from vibenovel.config import load_config
from vibenovel.core.approvals import ApprovalStore
from vibenovel.core.documents import ConflictError, DocumentStore, canonical_read
from vibenovel.core.export import export_markdown
from vibenovel.core.files import cleanup_stage, safe_path
from vibenovel.core.imports import import_outline, verify_bundle, verify_project
from vibenovel.core.project import ProjectStore
from vibenovel.errors import MissingDocumentError, VibeNovelError
from .jobs import JobManager

MAX_REQUEST_BYTES = 15 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateProject(Input):
    novel_id: StrictStr
    title: StrictStr
    genre: Literal["football", "adventure", "campus", "alternate_history"]


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


class JobRequest(Input):
    kind: Literal["expand_outline", "draft", "review", "rewrite"]
    provider: Literal["codex", "api"]
    document_path: StrictStr
    document_hash: StrictStr
    instruction: StrictStr = Field(default="", max_length=12000)
    plan_path: Optional[StrictStr] = None
    plan_hash: Optional[StrictStr] = None


class HandoffResult(Input):
    content: StrictStr


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
        try:
            if int(headers.get("content-length", "0")) > MAX_REQUEST_BYTES:
                return await reject(413, "请求超过 15 MiB 上限")
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
            if total > MAX_REQUEST_BYTES:
                return await reject(413, "请求超过 15 MiB 上限")
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
    token = secrets.token_urlsafe(32)
    frontend = (Path(frontend_dir) if frontend_dir is not None else
                Path(__file__).resolve().parents[3] / "studio" / "dist").resolve()

    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        try:
            yield
        finally:
            jobs.close()

    app = FastAPI(title="VibeNovel Studio", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs = jobs
    app.state.workspace = store.workspace
    app.add_middleware(LocalBoundary, token=token)

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
        fields = ", ".join(".".join(str(item) for item in error["loc"]) for error in exc.errors())
        return JSONResponse({"detail": "请求字段无效: " + fields}, status_code=422)

    @app.get("/api/bootstrap")
    def bootstrap():
        capability = jobs.capabilities()
        providers = [dict(capability[name], id=name, label=label) for name, label in (
            ("codex", "Codex · 交互接力"), ("api", "API · 自动生成"))]
        return {"version": __version__, "csrf_token": token, "capabilities": {
            "default_provider": capability.get("default_provider", "api"),
            "providers": providers, "tasks": ["expand_outline", "draft", "review", "rewrite"],
            "limits": capability.get("limits", {})}}

    def consistent_status(project):
        with canonical_read(project):
            return project.status()

    @app.get("/api/projects")
    def projects():
        return {"projects": [dict(project.metadata, counts=consistent_status(project)["counts"])
                             for project in store.list_projects()]}

    @app.post("/api/projects", status_code=201)
    def create_project(body: CreateProject):
        return store.init(body.novel_id, title=body.title, genre=body.genre).metadata

    @app.get("/api/projects/{novel_id}")
    def project_detail(novel_id: str):
        project = store.get(novel_id)
        status = consistent_status(project)
        status.update(phase="Studio alpha", finalize_available=False,
                      single_file_approval_available=True)
        return {"project": project.metadata, "status": status}

    @app.get("/api/projects/{novel_id}/documents")
    def documents(novel_id: str):
        return {"documents": DocumentStore(store.get(novel_id)).list_documents()}

    @app.get("/api/projects/{novel_id}/document")
    def document(novel_id: str, path: str):
        return DocumentStore(store.get(novel_id)).read(path)

    @app.put("/api/projects/{novel_id}/document")
    def save_document(novel_id: str, body: SaveDocument):
        return DocumentStore(store.get(novel_id)).save(body.path, body.content, body.expected_hash)

    @app.get("/api/projects/{novel_id}/history")
    def history(novel_id: str, path: str):
        return {"versions": DocumentStore(store.get(novel_id)).history(path)}

    @app.get("/api/projects/{novel_id}/history/version")
    def history_version(novel_id: str, path: str, version_id: str):
        return DocumentStore(store.get(novel_id)).version(path, version_id)

    @app.post("/api/projects/{novel_id}/restore")
    def restore(novel_id: str, body: RestoreDocument):
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
        request = body.model_dump(exclude_none=True)
        if body.kind == "draft" and body.plan_path and body.plan_hash is None:
            request["plan_hash"] = ApprovalStore(project).approved_outline(body.plan_path)["hash"]
        return jobs.submit(project, request)

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

    @app.post("/api/projects/{novel_id}/jobs/{job_id}/complete")
    def complete_handoff(novel_id: str, job_id: str, body: HandoffResult):
        return jobs.complete_handoff(store.get(novel_id), job_id, body.content)

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
