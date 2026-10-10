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

from . import music
from .controls import CATALOG
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


DURATION = ("How long the note sounds, in musical time at Orchid Studio's tempo: a note value (1/4, 1/8, 1/2, 1, dotted "
            "1/8., triplet 1/8t, or q e h w s), bars of 4/4 (2bars, 1.5bars), or values tied with + (2bars+1/2). Any "
            "number of bars: the key is held softly (no continued push) and the request returns once the note is done.")


class PlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: float | None = Field(default=None, ge=0.1, le=3.0)
    duration: str | None = Field(default=None, min_length=1, max_length=40, description=DURATION)
    turn_degrees: float | None = Field(default=None, ge=-90, le=90)
    wait: bool = True


class ChordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=20)
    chord: str = Field(min_length=1, max_length=20)
    speed: float | None = Field(default=None, ge=0.1, le=3.0)
    duration: str | None = Field(default=None, min_length=1, max_length=40, description=DURATION)


class SequenceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    control: str = Field(min_length=1, max_length=20)  # or "rest": silence for its duration
    chord: str | None = Field(default=None, min_length=1, max_length=20)  # a key played with this chord button held
    duration: str | None = Field(default=None, min_length=1, max_length=40, description=DURATION)
    turn_degrees: float | None = Field(default=None, ge=-90, le=90)


class SequenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[SequenceStep] = Field(min_length=1, max_length=64)
    speed: float | None = Field(default=None, ge=0.1, le=3.0)
    wait: bool = True


class ControlSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_degrees: float | None = Field(default=None, ge=-90, le=90)


class PlaybackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: float | None = Field(default=None, ge=0.1, le=3.0)
    duration: str | None = Field(default=None, min_length=1, max_length=40, description=DURATION)  # the note value when a play gives none
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


def create_app(directory: Path, mode="simulation", *, engine=None, engines=None, studio_port=None, clock=None):
    """studio_port: check each play's notes against Orchid Studio's key monitor on that local port, and time plays by
    its tempo and beat (music.py). Without it (simulation), plays keep 120 BPM time of their own; clock overrides both.
    A second follower (rig.py) is always available; it shows up once detected, and its data lives in directory/arm-b."""
    from .rig import NAMES, LeaderStore, Rig
    if engines is None and engine is not None:
        engines = {"a": engine}
    if engines is None:
        from .keycheck import KeyChecker, StudioKeys
        checker = lambda: KeyChecker(StudioKeys(studio_port)) if studio_port else None  # noqa: E731
        engines = {"a": Engine(directory, mode, key_checker=checker())}
        engines["b"] = Engine(directory / "arm-b", mode, key_checker=checker(), leader_store=LeaderStore(engines["a"].repo))
    read_clock = clock or (music.StudioClock(studio_port) if studio_port else music.fixed_clock())
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
            raise HTTPException(409, f"{NAMES[member.arm_id]}: {exc}") from exc
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and member.receipts.get(identity, {}).get("status") == "queued":
            time.sleep(0.03)
        done = member.receipts.get(identity, {})
        if done.get("status") != "complete":
            raise HTTPException(409, f"{NAMES[member.arm_id]}: {done.get('message') or 'did not finish'}")

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
        return {"status": "ok", "message": f"The leader now teaches {NAMES[body.to]}."}

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
                raise HTTPException(409, f"{NAMES[arm]}: {exc}") from exc
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
        # However long its notes are held (bars of them) and however long it waits for its first beat, plus margin.
        held = args.get("sound_s") or 0.0
        held += sum(step.get("sound_s", 0.0) for step in args.get("steps") or ())
        due = (args.get("rhythm") or {}).get("at")
        deadline = time.monotonic() + 300 + held + max(0.0, (due or 0.0) - time.monotonic())
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
        def pending(snap):  # this play's check is published as "pending" first, then with its result
            check = snap["key_check"] or {}
            return check.get("play", 0) <= plays_before or check.get("status") == "pending"
        while checked and time.monotonic() < until and pending(snap):
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

    # --- Musical time: note values, at Orchid Studio's tempo and on its beat (music.py) -------------------

    def clock_now():
        try:
            return music.timing(read_clock())
        except SafetyError as exc:
            raise HTTPException(409, str(exc)) from exc

    def phrase(steps, clock):
        """Steps with their place in the phrase (beats from its start) and how long each sounds; rests only take time."""
        default = engine.snapshot()["teach_settings"].get("duration", music.DEFAULT_DURATION)
        placed, offset = [], 0.0
        for step in steps:
            try:
                length = music.beats(step.get("duration") or default)
            except SafetyError as exc:
                raise HTTPException(422, str(exc)) from exc
            if step["control"] not in ("rest", "r"):
                owner(step["control"])  # an unknown control is a 404 before anything moves
                placed.append({**{k: v for k, v in step.items() if k != "duration"}, "offset": offset,
                               "beats": length, "sound_s": length * clock["beat_s"]})
            offset += length
        if not placed:
            raise HTTPException(422, "A phrase needs at least one control, not only rests.")
        return placed

    def rhythm_of(steps, clock, at=None):
        return {"timing": clock, "steps": [{"offset": s["offset"]} for s in steps], **({"at": at} if at is not None else {})}

    def timed(step, clock, at=None):
        """One control's play arguments: its sounding length and when its strike should land."""
        args = {k: v for k, v in step.items() if k not in ("offset", "beats", "chord")}
        return {**args, "sound_s": step["sound_s"], "rhythm": rhythm_of([step], clock, at)}

    def played_rhythm(member):
        return (member.snapshot()["teach"] or {}).get("rhythm")

    def tempo_note(rhythm, clock):
        slipped = sum(s["slipped_beats"] for s in (rhythm or {}).get("steps", []))
        where = "on Orchid Studio's beat" if clock["grid"] else "in its own time (Orchid Studio's transport is stopped)"
        return f" At {clock['bpm']:g} BPM, {where}" + (f"; {slipped} beat{'s' if slipped != 1 else ''} late, the arm could "
                                                      "not move faster." if slipped else ".")

    @app.get("/api/clock")
    def clock():
        """Orchid Studio's tempo and whether its transport runs (plays land on its beat while it does)."""
        return clock_now()

    @app.post("/api/controls/{control}/play")
    def play_control(control: str, body: PlayRequest):
        """Play one taught control for `duration` (default: settings.duration), on Orchid Studio's beat while its
        transport runs. Returns when the note has been held and the arm is home, with when it struck (`rhythm`)."""
        clock = clock_now()
        step = phrase([{"control": control, **({"duration": body.duration} if body.duration else {}),
                        **({"turn_degrees": body.turn_degrees} if body.turn_degrees is not None else {})}], clock)[0]
        member = owner(control)
        result = run("teach_play", {**timed(step, clock), "speed": body.speed}, body.wait, member)
        rhythm = played_rhythm(member) if body.wait else None
        return {**result, "message": result["message"] + (tempo_note(rhythm, clock) if body.wait else ""),
                "duration": {"beats": step["beats"], "seconds": round(step["sound_s"], 3)}, "rhythm": rhythm}

    @app.post("/api/controls/{control}")
    def configure_control(control: str, body: ControlSettings):
        return run("teach_configure", {"control": control, "turn_degrees": body.turn_degrees}, False, owner(control))

    def play_chord(step, clock, speed=None, at=None):
        """One chord: orchid-robot runs both arms (rig.Rig.play_chord); this waits for it and for the key check."""
        if rig is None:
            raise HTTPException(409, "Chords need the second follower (Chord Arm) for the chord buttons.")
        key, chord = step["control"], step["chord"]
        for control in (key, chord):
            if control not in Rig.CONTROLS:
                raise HTTPException(404, f"Unknown control {control!r}.")
        plays_before = (engine.snapshot()["key_check"] or {}).get("play", 0)
        try:
            played = rig.play_chord(key, chord, speed, timed(step, clock, at))
        except SafetyError as exc:
            raise HTTPException(409, str(exc)) from exc
        snap = engine.snapshot()
        until = time.monotonic() + 3  # the note check reads Orchid Studio just after the play ends
        while engine.key_checker and time.monotonic() < until and ((snap["key_check"] or {}).get("play", 0) <= plays_before
                                                                   or snap["key_check"]["status"] == "pending"):
            time.sleep(0.05)
            snap = engine.snapshot()
        checked = engine.key_checker and (snap["key_check"] or {}).get("play", 0) > plays_before
        return {"status": "complete", "phase": snap["phase"], "message": f"Played {key} + {CATALOG[chord]['label']}; both arms home.",
                "error": None, **played, "rhythm": played_rhythm(engine), **({"key_check": snap["key_check"]} if checked else {})}

    @app.post("/api/chords/play")
    def play_chord_route(body: ChordRequest):
        """Play a key with a chord button held: the chord button goes down first, the key second, and the Chord Arm
        returns home as soon as the key is down. The key is then held for `duration` (any number of bars) and the chord
        sounds the whole time. Both arms' motion is orchid-robot's; this only names the chord and how long it lasts."""
        clock = clock_now()
        step = phrase([{"control": body.key, "chord": body.chord, **({"duration": body.duration} if body.duration else {})}], clock)[0]
        result = play_chord(step, clock, body.speed)
        return {**result, "message": result["message"] + tempo_note(result["rhythm"], clock),
                "duration": {"beats": step["beats"], "seconds": round(step["sound_s"], 3)}}

    @app.post("/api/sequence")
    def play_sequence(body: SequenceRequest):
        """A phrase: each step sounds for its duration and the next comes after it ({"control": "rest"} for silence).
        Steps may be chords ({"control": "C", "chord": "chord.maj", "duration": "4bars"}). Returns when it is done."""
        clock = clock_now()
        steps = phrase([{k: v for k, v in step.model_dump().items() if v is not None} for step in body.steps], clock)
        players = {owner(step["control"]) for step in steps}
        if len(players) == 1 and not any("chord" in step for step in steps):
            member = players.pop()
            result = run("teach_sequence", {"steps": [{k: v for k, v in timed(s, clock).items() if k != "rhythm"} for s in steps],
                                            "rhythm": rhythm_of(steps, clock), "speed": body.speed}, body.wait, member)
            rhythm = played_rhythm(member) if body.wait else None
            return {**result, "message": result["message"] + (tempo_note(rhythm, clock) if body.wait else ""), "rhythm": rhythm}
        # Chords, or both arms: one step at a time, each arm back home before the next (the interlock's rule). Each
        # strike is due its offset after the last one that sounded, so the phrase keeps its rhythm across the steps.
        results, reports, last = [], [], None  # last: (strike time, offset) of the latest timed strike
        for step in steps:
            at = last[0] + (step["offset"] - last[1]) * clock["beat_s"] if last else None
            if "chord" in step:
                result = play_chord(step, clock, body.speed, at)
            else:
                member = owner(step["control"])
                result = run("teach_play", {**timed(step, clock, at), "speed": body.speed}, True, member)
                result["rhythm"] = played_rhythm(member)
            strikes = (result.get("rhythm") or {}).get("steps") or []
            if strikes:
                last = (strikes[-1]["strike_at"], step["offset"])
                reports += [{**s, "step": len(results)} for s in strikes]
            results.append(result)
        names = [("rest" if s["control"] in ("rest", "r") else f"{s['control']} + {CATALOG[s['chord']]['label']}" if s.get("chord")
                  else s["control"]) for s in (st.model_dump() for st in body.steps)]
        checks = [r["key_check"] for r in results if r.get("key_check")]
        rhythm = {"bpm": clock["bpm"], "grid": bool(clock["grid"]), "steps": reports}
        return {**results[-1], "message": "Played " + " → ".join(names) + "; holding at home." + tempo_note(rhythm, clock),
                "rhythm": rhythm,
                **({"key_check": {"status": "ok" if all(c["status"] == "ok" for c in checks) else "problem",
                                  "summary": "; ".join(c["summary"] for c in checks), "steps": [s for c in checks for s in c["steps"]]}}
                   if checks else {})}

    @app.post("/api/settings")
    def playback_settings(body: PlaybackSettings):
        if body.duration is not None:
            try:
                music.beats(body.duration)
            except SafetyError as exc:
                raise HTTPException(422, str(exc)) from exc
        results = [run("teach_settings", {"speed": body.speed, "duration": body.duration, "press_hardness": body.press_hardness},
                       False, member) for member in engines.values()]  # one Arm speed, note value and hardness for both arms
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
