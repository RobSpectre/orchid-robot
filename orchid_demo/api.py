"""Loopback-only HTTP interface and operator lease."""
from contextlib import ExitStack, asynccontextmanager
import fcntl
import json
from pathlib import Path
import secrets
import time
from typing import Any
from uuid import UUID, uuid4

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
    arm: str = Field(default="a", pattern="^[ab]$")


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
    press_hardness: float | None = Field(default=None, ge=0.1, le=1.0)


class ConnectAll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prepared: bool
    fixture: str = Field(default="", max_length=120)
    tool: str | None = Field(default=None, max_length=40)
    teaching_mode: str = Field(default="leader", pattern="^(leader|manual)$")
    scan: bool = False  # find the arms first (hardware), on a follower that is not connected


class LeaderMove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(pattern="^[ab]$")


class IncidentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=1000)


def create_app(directory: Path, mode="simulation", *, engine=None, engines=None, studio_port=None):
    """studio_port: check each play's notes against Orchid Studio's key monitor on that local port.
    A second follower (rig.py) is always available; it shows up once detected, and its data lives in directory/arm-b."""
    from .rig import LeaderStore, Rig
    if engines is None and engine is not None:
        engines = {"a": engine}
    if engines is None:
        from .keycheck import KeyChecker, StudioKeys
        checker = lambda: KeyChecker(StudioKeys(studio_port)) if studio_port else None  # noqa: E731
        engines = {"a": Engine(directory, mode, key_checker=checker())}
        engines["b"] = Engine(directory / "arm-b", mode, key_checker=checker(), leader_store=LeaderStore(engines["a"].repo))
    rig = None
    if len(engines) > 1:
        rig = Rig()
        for arm, member in engines.items():
            rig.add(arm, member)
    engine = engines["a"]
    token = secrets.token_urlsafe(32)

    def pick(arm):
        if arm not in engines:
            raise HTTPException(404, f"No follower {arm!r} in this setup.")
        return engines[arm]

    @asynccontextmanager
    async def lifespan(app):
        with ExitStack() as stack:
            for member in engines.values():
                lock = stack.enter_context((member.repo.directory / "operator.lock").open("a"))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("An operator console already owns this data directory.") from exc
            for member in engines.values():
                member.start()
            try:
                yield
            finally:
                for member in engines.values():
                    member.close()

    app = FastAPI(title="Orchid operator console", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine, app.state.engines, app.state.rig = engine, engines, rig
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
    def session(arm: str = "a", teaching_mode: str = "leader"):
        return {"token": token, "state": pick(arm).snapshot(), "arms": rig.summary() if rig else None,
                "connect_plan": rig.plan(teaching_mode) if rig else None}

    def perform(member, owner, action, args, seconds=15):
        """Submit to one follower's worker and wait for its verdict; a refusal becomes a 409 naming the arm."""
        identity = str(uuid4())
        try:
            member.submit(owner, identity, action, member.revision, args)
        except SafetyError as exc:
            raise HTTPException(409, f"Arm {member.arm_id.upper()}: {exc}") from exc
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and member.receipts.get(identity, {}).get("status") == "queued":
            time.sleep(0.03)
        done = member.receipts.get(identity, {})
        if done.get("status") != "complete":
            raise HTTPException(409, f"Arm {member.arm_id.upper()}: {done.get('message') or 'did not finish'}")

    @app.post("/api/leader/move")
    def move_leader(body: LeaderMove, request: Request):
        """Give the one leader to another follower: the follower that has it stops following and holds where it is
        (torque stays on), lets the leader go, and the other follower connects it. Calibration is shared."""
        target = pick(body.to)
        holder = next((member for member in engines.values() if member.leader is not None), None)
        if holder is None:
            raise HTTPException(409, "No follower has the leader. Use Find and connect all arms to connect it.")
        if holder is target:
            return {"status": "ok", "message": "This arm already has the leader."}
        owner = operator(request)
        if holder.phase in ("teach_follow", "teach_record"):
            perform(holder, owner, "teach_hold", {})
        perform(holder, owner, "leader_detach", {})
        perform(target, owner, "connect_leader", {"prepared": True, "leader_port": rig.released_leader})
        return {"status": "ok", "message": f"The leader now teaches arm {body.to.upper()}."}

    @app.post("/api/connect-all")
    def connect_all(body: ConnectAll, request: Request):
        """Find the arms (scan=True) and connect every detected follower to its arm, plus the leader, each through its
        own worker. Connecting only reads the motors; nothing moves."""
        scanner = next((member for member in engines.values() if member.arm is None), None)
        if body.scan and mode == "hardware" and scanner is not None:
            identity = str(uuid4())
            try:
                scanner.submit(operator(request), identity, "refresh_ports", scanner.revision, {})
            except SafetyError as exc:
                raise HTTPException(409, str(exc)) from exc
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and scanner.receipts.get(identity, {}).get("status") == "queued":
                time.sleep(0.05)
            done = scanner.receipts.get(identity, {})
            if done.get("status") != "complete":
                raise HTTPException(409, done.get("message") or "Finding the arms did not finish. Try again.")
        plan = rig.plan(body.teaching_mode) if rig else {}
        if not plan:
            raise HTTPException(409, "No follower found to connect. Check that each arm is powered and its USB cable is "
                                     "plugged in, then try again.")
        receipts = {}
        for arm, step in plan.items():
            member = engines[arm]
            label = body.fixture.strip() or member.fixture.get("label") or "Orchid plate"
            args = {"prepared": body.prepared, "fixture": label, "tool": body.tool or member.fixture.get("tool", "rubber_gloved_tips"),
                    "fixture_unchanged": True, "port": step["port"], "teaching_mode": step["teaching_mode"],
                    **({"leader_port": step["leader_port"]} if step["leader_port"] else {})}
            try:
                receipts[arm] = member.submit(operator(request), str(uuid4()), "connect", member.revision, args)
            except SafetyError as exc:
                raise HTTPException(409, f"Arm {arm.upper()}: {exc}") from exc
        return {"plan": plan, "receipts": receipts}

    @app.get("/api/state")
    def state(arm: str = "a"):
        return pick(arm).snapshot()

    @app.post("/api/heartbeat")
    async def heartbeat(request: Request):
        try:
            body = await request.json()
            for member in engines.values():  # one console operates every follower
                member.heartbeat(operator(request), leader_visible=isinstance(body, dict) and body.get("leader_visible") is True)
        except ValueError as exc:
            raise HTTPException(400, "Heartbeat must contain JSON") from exc
        except SafetyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/ports")
    def ports(arm: str = "a"):
        # GET never opens a serial device. Authenticated refresh_ports commands
        # perform discovery on the same worker that owns all other bus traffic.
        return pick(arm).snapshot()["discovery"]

    @app.post("/api/commands", status_code=202)
    def commands(command: Command, request: Request):
        target = pick(command.arm)
        try:
            if command.action == "stop":  # Stop motion stops every follower
                receipts = [member.submit(operator(request), str(command.id), "stop", member.revision, {})
                            for member in engines.values()]
                return receipts[list(engines).index(command.arm)]
            return target.submit(operator(request), str(command.id), command.action, command.revision, command.args)
        except SafetyError as exc:
            target.report_error("rejected", exc, {"action": command.action, "command_id": str(command.id), "source": "http"})
            raise HTTPException(409, str(exc)) from exc

    # --- Control API: list, configure and play taught controls --------------------------------------
    # Every motion goes through the operator console's live session (Stop/Esc and the lost-page stop
    # still apply); the API refuses while no console is in control. POSTs need X-Orchid-Token from
    # GET /api/session, like the console.

    def owner(control):
        """The follower that plays this control."""
        if rig is None:
            return engine
        if control not in Rig.CONTROLS:
            raise HTTPException(404, f"Unknown control {control!r}.")
        return engines[Rig.control_arm(control)]

    def run(action, args, wait, engine=engine):
        plays_before = (engine.snapshot()["key_check"] or {}).get("play", 0)
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
        checked = wait and action in ("teach_play", "teach_sequence") and engine.key_checker and done.get("status") == "complete"
        until = time.monotonic() + 3  # the note check reads Orchid Studio just after the play ends
        while checked and time.monotonic() < until and (snap["key_check"] or {}).get("play", 0) <= plays_before:
            time.sleep(0.05)
            snap = engine.snapshot()
        return {"status": done.get("status"), "phase": snap["phase"], "message": snap["message"], "error": snap["error"],
                **({"key_check": snap["key_check"]} if checked else {})}

    @app.get("/api/controls")
    def controls():
        listing = []
        for arm, member in engines.items():
            snap = member.snapshot()
            statuses = {**snap["keys"], **snap["controls"]}
            listing += [{"id": c["id"], "name": c["name"], "kind": c["kind"], "group": c["group"], "arm": arm,
                         **{k: statuses[c["id"]].get(k) for k in ("status", "press_s", "turn_degrees") if k in statuses[c["id"]]}}
                        for c in snap["catalog"].values() if c["id"] in snap["owns"]]
        snap = engine.snapshot()
        ready = {arm: s["phase"] in ("teach_hold", "teach_follow") and s["lease_live"]
                 for arm, s in ((arm, member.snapshot()) for arm, member in engines.items())}
        return {"phase": snap["phase"], "ready_to_play": ready["a"] if rig is None else ready,
                "settings": snap["teach_settings"], "controls": listing}

    @app.post("/api/controls/{control}/play")
    def play_control(control: str, body: PlayRequest):
        return run("teach_play", {"control": control, "speed": body.speed, "press_s": body.press_s,
                                  "turn_degrees": body.turn_degrees}, body.wait, owner(control))

    @app.post("/api/controls/{control}")
    def configure_control(control: str, body: ControlSettings):
        return run("teach_configure", {"control": control, "press_s": body.press_s, "turn_degrees": body.turn_degrees},
                   False, owner(control))

    @app.post("/api/sequence")
    def play_sequence(body: SequenceRequest):
        steps = [{k: v for k, v in step.model_dump().items() if v is not None} for step in body.steps]
        players = {owner(step["control"]) for step in steps}
        if len(players) > 1:
            raise HTTPException(409, "This sequence uses both arms. Sequences across both arms (held chords) are not available yet; "
                                     "play each arm's steps separately.")
        return run("teach_sequence", {"steps": steps, "speed": body.speed}, body.wait, players.pop())

    @app.post("/api/settings")
    def playback_settings(body: PlaybackSettings):
        results = [run("teach_settings", {"speed": body.speed, "press_s": body.press_s, "press_hardness": body.press_hardness},
                       False, member) for member in engines.values()]  # one Arm speed and hardness for both arms
        return results[0]

    @app.post("/api/home")
    def go_home(arm: str = "a"):
        return run("teach_go_home", {}, True, pick(arm))

    @app.post("/api/stop")
    def stop():
        return [run("stop", {}, False, member) for member in engines.values()][0]

    @app.get("/api/export")
    def export(arm: str = "a"):
        engine = pick(arm)
        return Response(json.dumps(engine.export_snapshot(), indent=2, allow_nan=False),
                        media_type="application/json", headers={"Content-Disposition": f'attachment; filename="orchid-{mode}-{arm}-session.json"'})

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
