"""FastAPI application for Fufu Archive P0."""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fufu_archive.config import Settings
from fufu_archive.db import KnowledgeBase, initialize_database
from fufu_archive.embedding import Embedder, OllamaEmbedder
from fufu_archive.models import (
    ErrorResponse,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from fufu_archive.query_rewrite import rewrite_query

logger = logging.getLogger(__name__)

_UPLOAD_CONTENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
}
_UPLOAD_READ_SIZE = 64 * 1024
_UPLOAD_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class ServiceError(Exception):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    detail = "internal service error"


class AuthenticationError(ServiceError):
    status_code = status.HTTP_401_UNAUTHORIZED
    detail = "invalid or missing API token"


class ConfigurationError(ServiceError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    detail = "FUFU_ARCHIVE_API_TOKEN is not configured"


def configure_logging() -> None:
    """Set a modest default only when an embedding application has not configured logs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def require_kb_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    _verify_kb_token(request.headers.get("Authorization"), settings)


def _verify_kb_token(authorization: str | None, settings: Settings) -> None:
    if settings.api_token is None:
        raise ConfigurationError()
    received = _extract_bearer_token(authorization)
    if received is None or not hmac.compare_digest(
        received.encode("utf-8"), settings.api_token.encode("utf-8")
    ):
        raise AuthenticationError()


def _service_error_response(error: ServiceError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail},
        headers=headers,
    )


class UploadBodyLimitMiddleware:
    """Authenticate and cap upload request bytes before multipart parsing."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == "/kb/upload"
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        try:
            _verify_kb_token(request.headers.get("Authorization"), self.settings)
        except ServiceError as error:
            await _service_error_response(error)(scope, receive, send)
            return

        max_body_bytes = (
            self.settings.upload_max_bytes + _UPLOAD_MULTIPART_OVERHEAD_BYTES
        )
        raw_content_length = request.headers.get("Content-Length")
        try:
            content_length = (
                int(raw_content_length) if raw_content_length is not None else None
            )
        except ValueError:
            content_length = None
        if content_length is not None and content_length > max_body_bytes:
            await self._too_large(max_body_bytes)(scope, receive, send)
            return

        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > max_body_bytes:
                await self._too_large(max_body_bytes)(scope, receive, send)
                return
            body.extend(block)

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    def _too_large(max_body_bytes: int) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={
                "detail": (
                    "multipart request exceeds the "
                    f"{max_body_bytes}-byte aggregate limit"
                )
            },
        )


def _strict_json_loads(value: str, field_name: str) -> object:
    def reject_non_finite(constant: str) -> None:
        raise ValueError(f"non-finite number {constant} is not valid JSON")

    try:
        parsed = json.loads(value, parse_constant=reject_non_finite)
        json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return parsed
    except (
        json.JSONDecodeError,
        UnicodeEncodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must contain valid JSON",
        ) from error


def _json_form_value(value: str, field_name: str, expected_type: type) -> object:
    parsed = _strict_json_loads(value, field_name)
    if not isinstance(parsed, expected_type):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be a JSON {expected_type.__name__}",
        )
    return parsed


async def _read_upload_limited(upload: UploadFile, max_bytes: int) -> bytes:
    content = bytearray()
    while len(content) <= max_bytes:
        remaining = max_bytes + 1 - len(content)
        block = await upload.read(min(_UPLOAD_READ_SIZE, remaining))
        if not block:
            break
        content.extend(block)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"uploaded file exceeds the {max_bytes}-byte limit",
        )
    return bytes(content)


def _decode_upload(raw_content: bytes, extension: str) -> str:
    if not raw_content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="uploaded file is empty",
        )
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="uploaded file must be valid UTF-8",
        ) from error

    if extension == ".json":
        parsed = _strict_json_loads(content, "uploaded JSON file")
        try:
            return json.dumps(
                parsed,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, RecursionError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="uploaded JSON file cannot be serialized safely",
            ) from error
    return content


def create_app(
    settings: Settings | None = None,
    embedder: Embedder | None = None,
) -> FastAPI:
    """Create an independently testable application instance.

    ``embedder`` may be injected for tests; when omitted the app builds an
    OllamaEmbedder from settings (which itself degrades gracefully).
    """
    runtime_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize_database(
            runtime_settings.db_path, vector_dim=runtime_settings.embed_dim
        )
        runtime_embedder = embedder
        if runtime_embedder is None:
            runtime_embedder = OllamaEmbedder(
                base_url=runtime_settings.embed_url,
                model=runtime_settings.embed_model,
                dim=runtime_settings.embed_dim,
            )
        app.state.kb = KnowledgeBase(runtime_settings.db_path, embedder=runtime_embedder)
        logger.info("Fufu Archive started with database %s", runtime_settings.db_path)
        yield

    app = FastAPI(
        title="Fufu Archive",
        description="面向 AI Agent 的外置长期记忆与知识库系统",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.add_middleware(UploadBodyLimitMiddleware, settings=runtime_settings)

    @app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, error: ServiceError) -> JSONResponse:
        return _service_error_response(error)

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service="fufu-archive", database="ready")

    @app.post(
        "/kb/ingest",
        response_model=IngestResponse,
        responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
        dependencies=[Depends(require_kb_token)],
        tags=["knowledge-base"],
    )
    def ingest(payload: IngestRequest, request: Request) -> IngestResponse:
        kb: KnowledgeBase = request.app.state.kb
        document_id, chunk_count = kb.ingest(**payload.model_dump())
        return IngestResponse(
            document_id=document_id,
            chunk_count=chunk_count,
            parent_chunk_count=1 if chunk_count > 1 else 0,
            status="created",
        )

    @app.post(
        "/kb/upload",
        response_model=IngestResponse,
        responses={
            401: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            415: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        dependencies=[Depends(require_kb_token)],
        tags=["knowledge-base"],
    )
    async def upload(
        request: Request,
        file: UploadFile = File(...),
        collection: str = Form("default"),
        domain: str | None = Form(None),
        topic_key: str | None = Form(None),
        status_value: str = Form("active", alias="status"),
        source: str | None = Form(None),
        title: str | None = Form(None),
        content_type: str | None = Form(None),
        language: str | None = Form(None),
        tags: str = Form("[]"),
        metadata: str = Form("{}"),
        chunking: str | None = Form(None),
        scan_injection: bool = Form(True),
    ) -> IngestResponse:
        extension = Path(file.filename or "").suffix.lower()
        if extension not in _UPLOAD_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="supported upload extensions are .txt, .md, and .json",
            )

        raw_content = await _read_upload_limited(
            file, request.app.state.settings.upload_max_bytes
        )
        content = _decode_upload(raw_content, extension)
        parsed_tags = _json_form_value(tags, "tags", list)
        parsed_metadata = _json_form_value(metadata, "metadata", dict)
        parsed_chunking = (
            None
            if chunking is None
            else _json_form_value(chunking, "chunking", dict)
        )
        try:
            payload = IngestRequest(
                content=content,
                collection=collection,
                domain=domain,
                topic_key=topic_key,
                status=status_value,
                source=source,
                title=title,
                content_type=content_type or _UPLOAD_CONTENT_TYPES[extension],
                language=language,
                tags=parsed_tags,
                metadata=parsed_metadata,
                chunking=parsed_chunking,
                scan_injection=scan_injection,
            )
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from error

        kb: KnowledgeBase = request.app.state.kb
        document_id, chunk_count = kb.ingest(**payload.model_dump())
        return IngestResponse(
            document_id=document_id,
            chunk_count=chunk_count,
            parent_chunk_count=1 if chunk_count > 1 else 0,
            status="created",
        )

    @app.post(
        "/kb/search",
        response_model=SearchResponse,
        response_model_exclude_none=True,
        responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
        dependencies=[Depends(require_kb_token)],
        tags=["knowledge-base"],
    )
    def search(payload: SearchRequest, request: Request) -> SearchResponse:
        kb: KnowledgeBase = request.app.state.kb
        queries = rewrite_query(payload.query) if payload.rewrite else [payload.query]
        rows = []
        seen: set[tuple[str, int]] = set()
        for query in queries:
            query_rows = kb.search(
                query=query,
                collection=payload.collection,
                domain=payload.domain,
                topic_key=payload.topic_key,
                limit=payload.limit,
                mode=payload.mode,
            )
            for row in query_rows:
                identity = (row["document_id"], row["chunk_id"])
                if identity not in seen:
                    seen.add(identity)
                    rows.append(row)
        return SearchResponse(
            query=payload.query,
            results=[SearchResult(**row) for row in rows[: payload.limit]],
            rewritten_queries=queries if payload.rewrite else None,
        )

    return app


app = create_app()


def run() -> None:
    configure_logging()
    settings = Settings.from_env()
    uvicorn.run("fufu_archive.main:app", host=settings.host, port=settings.port, reload=False)
