"""Loopback-only HTTP interface and operator lease."""
from contextlib import asynccontextmanager
import fcntl
import json
from pathlib import Path
import secrets
import time
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


class PlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: float | None = Field(default=None, ge=0.1, le=3.0)
    press_s: float | None = Field(default=None, ge=0, le=5)
    turn_degrees: float | None = Field(default=None, ge=-90, le=90)
    wait: bool = True


class SequenceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    control: str = Field(min_length=1, max_length=20)
    press_s: float | None = Field(default=None, ge=0, le=5)
    turn_degrees: float | None = Field(default=None, ge=-90, le=90)


class SequenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[SequenceStep] = Field(min_length=1, max_length=64)
    speed: float | None = Field(default=None, ge=0.1, le=3.0)
    wait: bool = True


class ControlSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    press_s: float | None = Field(default=None, ge=0, le=5)
    turn_degrees: float | None = Field(default=None, ge=-90, le=90)


class PlaybackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: float | None = Field(default=None, ge=0.1, le=3.0)
    press_s: float | None = Field(default=None, ge=0, le=5)


class IncidentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=1000)


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
    async def heartbeat(request: Request):
        try:
            body = await request.json()
            engine.heartbeat(operator(request), leader_visible=isinstance(body, dict) and body.get("leader_visible") is True)
        except ValueError as exc:
            raise HTTPException(400, "Heartbeat must contain JSON") from exc
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
            engine.report_error("rejected", exc, {"action": command.action, "command_id": str(command.id), "source": "http"})
            raise HTTPException(409, str(exc)) from exc

    # --- Control API: list, configure and play taught controls --------------------------------------
    # Every motion goes through the operator console's live session (Stop/Esc and the lost-page stop
    # still apply); the API refuses while no console is in control. POSTs need X-Orchid-Token from
    # GET /api/session, like the console.

    def run(action, args, wait):
        try:
            receipt = engine.api_submit(action, {k: v for k, v in args.items() if v is not None})
        except SafetyError as exc:
            raise HTTPException(409, str(exc)) from exc
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:  # the worker processes the command between control ticks
            done = engine.receipts.get(receipt["id"], {})
            if done.get("status") != "queued":
                break
            time.sleep(0.05)
        done = dict(engine.receipts.get(receipt["id"], {}))
        if done.get("status") == "rejected":
            raise HTTPException(409, done.get("message") or "Rejected")
        moving = action in ("teach_play", "teach_sequence", "teach_go_home", "teach_go_rest")
        while wait and moving and time.monotonic() < deadline and engine.snapshot()["phase"] == "teach_play":
            time.sleep(0.1)
        snap = engine.snapshot()
        return {"status": done.get("status"), "phase": snap["phase"], "message": snap["message"], "error": snap["error"]}

    @app.get("/api/controls")
    def controls():
        snap = engine.snapshot()
        statuses = {**snap["keys"], **snap["controls"]}
        return {"phase": snap["phase"], "ready_to_play": snap["phase"] in ("teach_hold", "teach_follow") and snap["lease_live"],
                "settings": snap["teach_settings"],
                "controls": [{"id": c["id"], "name": c["name"], "kind": c["kind"], "group": c["group"],
                              **{k: statuses[c["id"]].get(k) for k in ("status", "press_s", "turn_degrees") if k in statuses[c["id"]]}}
                             for c in snap["catalog"].values()]}

    @app.post("/api/controls/{control}/play")
    def play_control(control: str, body: PlayRequest):
        return run("teach_play", {"control": control, "speed": body.speed, "press_s": body.press_s,
                                  "turn_degrees": body.turn_degrees}, body.wait)

    @app.post("/api/controls/{control}")
    def configure_control(control: str, body: ControlSettings):
        return run("teach_configure", {"control": control, "press_s": body.press_s, "turn_degrees": body.turn_degrees}, False)

    @app.post("/api/sequence")
    def play_sequence(body: SequenceRequest):
        steps = [{k: v for k, v in step.model_dump().items() if v is not None} for step in body.steps]
        return run("teach_sequence", {"steps": steps, "speed": body.speed}, body.wait)

    @app.post("/api/settings")
    def playback_settings(body: PlaybackSettings):
        return run("teach_settings", {"speed": body.speed, "press_s": body.press_s}, False)

    @app.post("/api/home")
    def go_home():
        return run("teach_go_home", {}, True)

    @app.post("/api/stop")
    def stop():
        return run("stop", {}, False)

    @app.get("/api/export")
    def export():
        return Response(json.dumps(engine.export_snapshot(), indent=2, allow_nan=False),
                        media_type="application/json", headers={"Content-Disposition": f'attachment; filename="orchid-{mode}-session.json"'})

    @app.get("/api/incidents")
    def incidents():
        return engine.incidents.listing()

    @app.get("/api/incidents/{identity}")
    def incident(identity: str):
        try:
            engine.incidents.metadata(identity)
            path = engine.incidents.path(identity)
            if not path.is_file():
                raise FileNotFoundError(identity)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Incident not found or still saving") from exc
        return FileResponse(path, media_type="application/json", filename=f"orchid-incident-{identity}.json")

    @app.post("/api/incidents/{identity}/report")
    def report_incident(identity: str, report: IncidentReport):
        # CSRF protection applies; motor ownership/health is deliberately unrelated.
        try:
            return engine.incidents.request_report(identity, report.note)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Incident not found or still saving") from exc
        except OSError as exc:
            raise HTTPException(503, "Could not save report request; retry when local storage is available") from exc

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
