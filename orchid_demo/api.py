"""Loopback-only HTTP interface and operator lease."""
from contextlib import asynccontextmanager
import fcntl
import json
from pathlib import Path
import secrets
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .engine import Engine
from .motion import SafetyError

STATIC = Path(__file__).with_name("static")


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    action: str = Field(min_length=1, max_length=40)
    revision: StrictInt
    args: dict[str, Any] = Field(default_factory=dict)


def create_app(directory: Path, mode="simulation", *, engine=None):
    engine = engine or Engine(directory, mode)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        with (engine.repo.directory / "operator.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("An operator console already owns this data directory.") from exc
            engine.start()
            try:
                yield
            finally:
                engine.close()

    app = FastAPI(title="Orchid operator console", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"], www_redirect=False)

    @app.middleware("http")
    async def protect(request, call_next):
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 16384:
            return Response("Request too large", status_code=413)
        if request.method not in ("GET", "HEAD"):
            if "content-length" not in request.headers or "transfer-encoding" in request.headers:
                return Response("A bounded Content-Length is required", status_code=411)
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if origin is not None and origin != expected:
                return Response("Cross-origin commands are not allowed", status_code=403)
            if not secrets.compare_digest(request.headers.get("x-orchid-token", ""), token):
                return Response("Invalid operator token", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        return response

    def operator(request):
        try:
            return str(UUID(request.headers.get("x-orchid-operator", "")))
        except ValueError as exc:
            raise HTTPException(400, "Missing operator identity") from exc

    @app.get("/api/session")
    def session():
        return {"token": token, "state": engine.snapshot()}

    @app.get("/api/state")
    def state():
        return engine.snapshot()

    @app.post("/api/heartbeat")
    def heartbeat(request: Request):
        try:
            engine.heartbeat(operator(request))
        except SafetyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/ports")
    def ports():
        # GET never opens a serial device. Authenticated refresh_ports commands
        # perform discovery on the same worker that owns all other bus traffic.
        return engine.snapshot()["discovery"]

    @app.post("/api/commands", status_code=202)
    def commands(command: Command, request: Request):
        try:
            return engine.submit(operator(request), str(command.id), command.action,
                                 command.revision, command.args)
        except SafetyError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/export")
    def export():
        return Response(json.dumps(engine.export_snapshot(), indent=2, allow_nan=False),
                        media_type="application/json", headers={"Content-Disposition": f'attachment; filename="orchid-{mode}-session.json"'})

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
