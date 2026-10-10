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
from . import score
from . import voicing
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


class ScoreEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bar: StrictInt = Field(ge=1, le=9999)
    beat: float = Field(ge=1, lt=5)  # 1, 2, 3, 4; 1.5 is the "and" of 1
    key: str = Field(min_length=1, max_length=20)
    chord: str | None = Field(default=None, min_length=1, max_length=20)  # a chord button held for this key
    duration: str | None = Field(default=None, min_length=1, max_length=40, description=DURATION)


class StudioSettings(BaseModel):
    """Orchid Studio settings for the voice Orchid's notes sound on (Live Perform, slot 5)."""
    model_config = ConfigDict(extra="forbid")
    sound: StrictInt | None = Field(default=None, ge=1, le=100, description="Pistil preset number (Studio sounds-list)")
    perform: dict[str, Any] | None = Field(default=None, description="Live Perform settings: mode (strum, arp, harp, "
                                           "bloom ...), pattern, step_beats, gate ... (Studio perform-options)")
    fx: dict[str, Any] | None = Field(default=None, description='{"delay": {mix, beats|time, feedback}, "reverb": {mix, room}} '
                                      "(Studio fx)")
    # Orchid's own voicing dial, turned by the Chord Arm until Orchid reports this position (voicing.py)
    voicing: StrictInt | None = Field(default=None, ge=0, le=127, description="Orchid's voicing dial position (its CC115 value)")


class ScoreChange(StudioSettings):
    bar: StrictInt = Field(ge=1, le=9999)
    beat: float = Field(default=1, ge=1, lt=5)


class ScoreRequest(StudioSettings):
    events: list[ScoreEvent] = Field(min_length=1, max_length=512)
    changes: list[ScoreChange] = Field(default_factory=list, max_length=64)  # sound, perform and fx at chosen bars
    # Studio's loop layer (1-4) to hand the composition to: the score starts on the loop's next pass, and from the pass
    # after the arms play it, Studio repeats it exactly as written (voiced as Orchid sounded it).
    loop_slot: StrictInt | None = Field(default=None, ge=1, le=4)
    speed: float | None = Field(default=None, ge=0.1, le=3.0)
    plan_only: bool = False


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


def create_app(directory: Path, mode="simulation", *, engine=None, engines=None, studio_port=None, clock=None, studio=None):
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
    timing_file = Path(directory) / "score-timing.json"  # how long changes really take (score.learn)
    studio = studio or (music.Studio(studio_port) if studio_port else None)  # sound, perform and fx for scores
    voicing_file = Path(directory) / "voicing.json"  # how Orchid's voicing dial answers a turn (voicing.set_voicing)
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

    def rhythm_of(steps, clock, at=None, bar=None, align=music.BEATS_PER_BAR):
        """bar: start the first note on its own beat of a Studio bar (a score), not just the next beat; align: of a
        Studio loop's pass instead (a looped score: its bar 1 is the loop's)."""
        return {"timing": clock, "steps": [{"offset": s["offset"]} for s in steps], **({"at": at} if at is not None else {}),
                **({"align": align, "phase": steps[0]["offset"] % align} if bar == "start" else {}),
                **({"slip": music.BEATS_PER_BAR} if bar else {})}

    def timed(step, clock, at=None, bar=None, align=music.BEATS_PER_BAR):
        """One control's play arguments: its sounding length and when its strike should land."""
        args = {k: v for k, v in step.items() if k not in ("offset", "beats", "chord", "bar", "beat", "hold_s", "moved_bars", "needs_s")}
        return {**args, "sound_s": step["sound_s"], "rhythm": rhythm_of([step], clock, at, bar, align)}

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

    def play_chord(step, clock, speed=None, at=None, bar=None, align=music.BEATS_PER_BAR, stay=False):
        """One chord: orchid-robot runs both arms (rig.Rig.play_chord); this waits for it and for the key check.
        stay: the Keys Arm ends over the key's hover, for the next key, instead of at home."""
        if rig is None:
            raise HTTPException(409, "Chords need the second follower (Chord Arm) for the chord buttons.")
        key, chord = step["control"], step["chord"]
        for control in (key, chord):
            if control not in Rig.CONTROLS:
                raise HTTPException(404, f"Unknown control {control!r}.")
        plays_before = (engine.snapshot()["key_check"] or {}).get("play", 0)
        try:
            played = rig.play_chord(key, chord, speed, {**timed(step, clock, at, bar, align), **({"stay": True} if stay else {})})
        except SafetyError as exc:
            raise HTTPException(409, str(exc)) from exc
        snap = engine.snapshot()
        until = time.monotonic() + 3  # the note check reads Orchid Studio just after the play ends
        while engine.key_checker and time.monotonic() < until and ((snap["key_check"] or {}).get("play", 0) <= plays_before
                                                                   or snap["key_check"]["status"] == "pending"):
            time.sleep(0.05)
            snap = engine.snapshot()
        checked = engine.key_checker and (snap["key_check"] or {}).get("play", 0) > plays_before
        return {"status": "complete", "phase": snap["phase"], "message": f"Played {key} + {CATALOG[chord]['label']}; "
                + ("the Keys Arm waits over its hover for the next key." if stay else "both arms home."),
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
        result = perform_steps(steps, clock, body.speed)
        names = [("rest" if s["control"] in ("rest", "r") else f"{s['control']} + {CATALOG[s['chord']]['label']}" if s.get("chord")
                  else s["control"]) for s in (st.model_dump() for st in body.steps)]
        return {**result, "message": "Played " + " → ".join(names) + "; holding at home." + tempo_note(result["rhythm"], clock)}

    def perform_steps(steps, clock, speed, bar=None, progress=None, align=music.BEATS_PER_BAR, before=None):
        """Chords, or both arms: one step at a time. Between two keys (with or without chord buttons) the Keys Arm waits
        over the last key's hover and goes straight to the next (key_follows); otherwise each arm goes back home before
        the next step (the interlock's rule). Each strike is due its offset after the last one that sounded, so the music
        keeps its rhythm across the steps. bar: the first note waits for its own beat of a Studio bar (a score)."""
        keys = engines["a"]
        stops = keys.stops
        try:
            return play_steps(steps, clock, speed, bar, progress, align, before)
        except BaseException:
            # Stopped partway with the Keys Arm waiting over a key: take it home, unless the operator stopped it (Stop
            # motion holds the arm where it is) or it is faulted.
            if keys.stops == stops and keys.phase == "teach_hold" and keys.over_key():
                try:
                    run("teach_go_home", {}, True, keys)
                except HTTPException:
                    pass
            raise

    def key_follows(steps, index, before=None):
        """Whether the Keys Arm stays over this step's key for the next one: both are keys (a chord is played from its
        key), and nothing else happens between them (turning the voicing dial needs the Keys Arm home)."""
        if index + 1 >= len(steps) or (before and index + 1 in before):
            return False
        return all(CATALOG.get(s["control"], {}).get("kind") == "key" for s in (steps[index], steps[index + 1]))

    def play_steps(steps, clock, speed, bar, progress, align, before):
        results, reports, last = [], [], None  # last: (strike time, offset) of the latest timed strike
        for index, step in enumerate(steps):
            if progress is not None:  # the score's conductor follows which note is next
                progress.update(index=index, stale=getattr(engines["a"], "rhythm", None))
            if before and index in before:  # other work first (turning the voicing dial): the note may then move a bar later
                before[index]()
            at = last[0] + (step["offset"] - last[1]) * clock["beat_s"] if last else None
            # A score: the first note waits for its own beat of a bar; every note slips by whole bars if late.
            mode = ("start" if last is None else "slip") if bar else None
            stay = key_follows(steps, index, before)
            if "chord" in step:
                result = play_chord(step, clock, speed, at, mode, align, stay)
            else:
                member = owner(step["control"])
                result = run("teach_play", {**timed(step, clock, at, mode, align), "speed": speed, "stay": stay}, True, member)
                result["rhythm"] = played_rhythm(member)
            strikes = (result.get("rhythm") or {}).get("steps") or []
            if strikes:
                last = (strikes[-1]["strike_at"], step["offset"])
                reports += [{**s, "step": len(results)} for s in strikes]
                if progress is not None:
                    progress["known"][index] = strikes[-1]["strike_at"]
            results.append(result)
        checks = [r["key_check"] for r in results if r.get("key_check")]
        heard = [((r.get("key_check") or {}).get("steps") or [{}])[0] for r in results]  # one per step, in place
        return {**results[-1], "heard": heard, "rhythm": {"bpm": clock["bpm"], "grid": bool(clock["grid"]), "steps": reports},
                **({"key_check": {"status": "ok" if all(c["status"] == "ok" for c in checks) else "problem",
                                  "summary": "; ".join(c["summary"] for c in checks), "steps": [s for c in checks for s in c["steps"]]}}
                   if checks else {})}

    def score_timing(speed):
        """Each step's motion times at this speed, read from the arms' taught motions (score.plan)."""
        keys, chords = engines["a"], engines.get("b")

        def timing(step, after=None):
            try:
                found = keys.play_timing(step["control"], speed, after)
                if "chord" in step:
                    m_require_rig()
                    found.update(chords.chord_timing(step["chord"], speed))
            except SafetyError as exc:
                raise HTTPException(409, f"{step['control']}{'+' + CATALOG[step['chord']]['label'] if 'chord' in step else ''} "
                                         f"(bar {step['bar']} beat {step['beat']:g}): {exc}") from exc
            return found
        return timing

    def m_require_rig():
        if rig is None:
            raise HTTPException(409, "Chords need the second follower (Chord Arm) for the chord buttons.")

    @app.post("/api/score")
    def play_score(body: ScoreRequest):
        """A score: keys and chords at bars and beats, played as one performance on Orchid Studio's beat. orchid-robot
        plans every move (when to leave home, when to press the chord button, how long each note can be held) and
        reports how far each note landed from its beat. plan_only: the plan, nothing moves."""
        clock = clock_now()
        default = engine.snapshot()["teach_settings"].get("duration", music.DEFAULT_DURATION)
        try:
            placed = score.steps([event.model_dump() for event in body.events], default)
        except SafetyError as exc:
            raise HTTPException(422, str(exc)) from exc
        for step in placed:
            owner(step["control"])
            if "chord" in step:
                m_require_rig()
                if step["chord"] not in Rig.CONTROLS:
                    raise HTTPException(404, f"Unknown control {step['chord']!r}.")
        start = {k: v for k, v in (("sound", body.sound), ("perform", body.perform), ("fx", body.fx)) if v}
        dial = [body.voicing] + [c.voicing for c in body.changes]
        if (start or body.changes or body.loop_slot or body.voicing is not None) and studio is None:
            raise HTTPException(409, "Sound, perform, fx, voicing and looping need Orchid Studio (voicing is checked by its "
                                     "key monitor): start the app with Orchid Studio (--studio-port), or leave them out.")
        if any(v is not None for v in dial):
            if rig is None:
                raise HTTPException(409, "The voicing dial is turned by the Chord Arm: connect it to set the voicing.")
            for control in ("voicing.cw", "voicing.ccw"):
                if engines["b"].snapshot()["controls"][control]["status"] != "registered":
                    raise HTTPException(409, f"{CATALOG[control]['name']} is not taught on the Chord Arm: teach the dial to set "
                                             "the voicing.")
        loop = None
        if body.loop_slot:
            try:
                looper = studio("status").get("looper") or {}
            except SafetyError as exc:
                raise HTTPException(409, str(exc)) from exc
            if not looper.get("running"):
                raise HTTPException(409, "Studio's loops are not playing. Start them (loop-start) so the composition has a loop to "
                                         "land in, then send the score again.")
            loop = {"slot": body.loop_slot, "bars": looper["settings"]["bars"]}
        speed = body.speed or engine.snapshot()["teach_settings"]["speed"]
        timing = score_timing(speed)
        learned = score.load(timing_file)
        later = score.changes([c.model_dump() for c in body.changes], placed)
        turning = {c["event"]: c["voicing"] for c in later if c.get("voicing") is not None and c["event"] > 0}
        before = {}
        if turning:  # a voicing change between notes: the Chord Arm turns the dial, about two turns
            dial = engines["b"].play_timing("voicing.cw", speed)
            before = {i: 2 * (dial["lead"] + dial["rise"] + score.OVERHEAD_S) for i in turning}
        planned = score.plan(placed, clock["beat_s"], timing, learned["extra"], before)
        planned = {**planned, "bpm": clock["bpm"], "on_studio_beat": bool(clock["grid"]), "events": len(placed)}
        align = music.BEATS_PER_BAR
        if loop:
            align = loop["bars"] * music.BEATS_PER_BAR
            try:
                score.fits(placed, align)
            except SafetyError as exc:
                raise HTTPException(409, str(exc)) from exc
            planned["loop"] = dict(loop)
            if planned["length_bars"] > loop["bars"]:
                planned["loop"]["warning"] = (f"the arms' pass needs {planned['length_bars']} bars with its moves; the loop is "
                                              f"{loop['bars']}, so it runs into the next pass")
        planned["changes"] = [{"bar": c["bar"], "beat": c.get("beat", 1),
                               **{k: c[k] for k in ("sound", "perform", "fx", "voicing") if c.get(k) is not None}} for c in later]
        if body.plan_only:
            return {"status": "planned", "plan": planned, "message": plan_note(planned)}
        perform_running = bool(studio and (start or later) and (studio("status").get("performance")))

        def apply(settings):
            done = []
            voices = (score.STUDIO_VOICE, body.loop_slot) if body.loop_slot else (score.STUDIO_VOICE,)
            for command, fields, text in score.setting_steps(settings, perform_running, voices):
                studio(command, **fields)
                done.append(text)
            return done
        voiced = []

        def voicing_to(target, bar):
            """Turn Orchid's voicing dial (the Chord Arm) until Orchid reports target."""
            learned_dial = voicing.load(voicing_file)
            current = (studio("status").get("key_monitor") or {}).get("last_voicing")

            def turn(control, degrees):
                result = run("teach_play", {"control": control, "turn_degrees": round(degrees, 1), "keep_default": True,
                                            "speed": speed}, True, engines["b"])
                step = ((result.get("key_check") or {}).get("steps") or [{}])[0]
                return step.get("clicks") or 0, step.get("value")
            report = voicing.set_voicing(target, current, turn, learned_dial)
            voicing.save(voicing_file, learned_dial)
            voiced.append({"bar": bar, **report})
            return report
        try:  # the starting settings, and any change before the first note, are in place before anything plays
            applied_start = apply(start) if start else []
            for change in [c for c in later if c["event"] == 0]:
                applied_start += apply(change)
            for target, bar in [(body.voicing, placed[0]["bar"])] + [(c["voicing"], c["bar"]) for c in later if c["event"] == 0]:
                if target is not None:
                    voicing_to(target, bar)
        except SafetyError as exc:
            raise HTTPException(409, str(exc)) from exc
        later = [c for c in later if c["event"] > 0]
        for step in placed:
            step["sound_s"] = max(step["hold_s"], 1e-3)
        players = {owner(step["control"]) for step in placed}
        one_motion = len(players) == 1 and not any("chord" in step for step in placed) and not turning
        keys = engines["a"]
        progress = {"index": 0, "stale": None, "known": {}}

        def known():
            """Each note's due time as the Keys Arm reaches it (its live rhythm report), plus the notes already played."""
            live = getattr(keys, "rhythm", None)
            if live and live is not progress["stale"]:
                reports = live.get("steps") or []
                if one_motion:
                    progress["known"].update({i: r["strike_at"] for i, r in enumerate(reports)})
                elif reports:
                    progress["known"][progress["index"]] = reports[-1]["strike_at"]
            return dict(progress["known"])
        conductor = score.Conductor(later, placed, clock["beat_s"], apply, known)
        if later:
            conductor.start()
        try:
            if one_motion:
                result = run("teach_sequence", {"steps": [{k: v for k, v in timed(s, clock).items() if k != "rhythm"} for s in placed],
                                                "rhythm": rhythm_of(placed, clock, bar="start", align=align), "speed": speed}, True, keys)
                result["rhythm"] = played_rhythm(keys)
            else:
                result = perform_steps(placed, clock, speed, bar=True, progress=progress, align=align,
                                       before={i: (lambda t=target, b=placed[i]["bar"]: voicing_to(t, b)) for i, target in turning.items()})
        finally:
            fired = conductor.finish() if later else {"fired": [], "errors": [], "missed": []}
        played = landed(placed, result)
        strikes = (result.get("rhythm") or {}).get("steps") or []
        if len(strikes) == len(placed) and not result.get("error"):  # a whole performance: learn how long changes took
            score.save(timing_file, score.learn(learned, placed, strikes, timing, before))
        if loop:  # what the arms played on this pass, each chord on its written beat, for Studio's loop to repeat
            loop.update(keep_pass(placed, result, loop, body.perform))
        studio_report = {"start": applied_start, **fired, **({"loop": loop} if loop else {}), **({"voicing": voiced} if voiced else {})}
        note = plan_note(planned, played)
        if fired["errors"] or fired["missed"]:
            note += " Studio changes not made: " + "; ".join(
                [f"bar {e['bar']}: {e['error']}" for e in fired["errors"]] + [f"bar {m_['bar']} (after the last note)" for m_ in fired["missed"]]) + "."
        return {**result, "plan": planned, "played": played, "studio": studio_report, "message": note}

    def keep_pass(placed, result, loop, perform=None):
        """Hand Studio's loop this pass: what Orchid sent for each note, placed on its written beat (score.take). It
        takes over at the next loop boundary."""
        check = result.get("key_check") or {}
        heard = result.get("heard") or check.get("steps") or []  # step by step: one per note, in place; one motion: in order
        if not any(h.get("midi") for h in heard):
            reason = ("nothing sounded on this pass" if check and check.get("status") != "unavailable" else
                      "the note check did not hear this pass (is Orchid Studio's key monitor on?)")
            return {"kept": False, "reason": reason + ": the loop layer is unchanged"}
        chords, missed = score.take(placed, heard)
        try:  # the layer plays through the score's own Perform settings (Studio fills in the rest from its defaults)
            reply = studio("loop-compose", slot=loop["slot"], chords=chords, **({"settings": perform} if perform else {}))
        except SafetyError as exc:
            return {"kept": False, "reason": str(exc), "missed": missed}
        return {"kept": True, "chords": len(chords), "missed": missed,
                "applies_at_beat": (reply.get("composed") or {}).get("applies_at_beat")}

    def landed(placed, result):
        """Per note: where it was written, how many beats it slipped, and how far from its beat it sounded."""
        strikes = (result.get("rhythm") or {}).get("steps") or []
        heard = ((result.get("key_check") or {}).get("steps")) or []
        out = []
        for i, step in enumerate(placed):
            note = heard[i] if i < len(heard) else {}
            out.append({"bar": step["bar"], "beat": step["beat"], "key": step["control"], "chord": step.get("chord"),
                        "slipped_beats": strikes[i]["slipped_beats"] if i < len(strikes) else None,
                        "late_ms": note.get("late_ms"), "heard": note.get("text")})
        return out

    def plan_note(planned, played=None):
        changes = planned.get("changes") or []
        text = (f"{len(changes)} Studio change{'s' if len(changes) != 1 else ''} on the bar line. " if changes else "") + \
            f"{planned['events']} notes at {planned['bpm']:g} BPM" + (" on Orchid Studio's beat" if planned["on_studio_beat"] else
                                                                          " in their own time (Studio's transport is stopped)")
        if planned["moves"]:
            text += f"; {len(planned['moves'])} change{'s' if len(planned['moves']) != 1 else ''} cannot be made on the written bar: " + \
                "; ".join(t["text"] for t in planned["moves"])
        if played is not None:
            lates = sorted(abs(p["late_ms"]) for p in played if p["late_ms"] is not None)
            if lates:
                text += f". Landed a median {lates[len(lates) // 2]} ms from the beat (worst {lates[-1]} ms)"
            slipped = sum(p["slipped_beats"] or 0 for p in played) // music.BEATS_PER_BAR
            if slipped:
                text += f"; moved {slipped} bar{'s' if slipped != 1 else ''} later in all"
        return text + "."

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
