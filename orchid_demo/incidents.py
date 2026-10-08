"""Local incident bundles. Capture cached data; never read or command a motor."""
from collections import deque
from copy import deepcopy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import queue
import threading
import time
import traceback
from uuid import UUID, uuid4

from .motion import stamp

ERROR_KINDS = frozenset(("fault", "rejected", "connection_failed", "hold_failed", "worker_failed", "cleanup_failed"))
HISTORY_SECONDS = 30
MAX_SAMPLES = 900


def atomic_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_identity():
    sources = {}
    for path in sorted(Path(__file__).parent.glob("*.py")):
        sources[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        lerobot = version("lerobot")
    except PackageNotFoundError:
        lerobot = None
    return {"python": platform.python_version(), "lerobot": lerobot, "source_sha256": sources}


class IncidentStore:
    def __init__(self, directory, *, error_logger=None, clock=time.monotonic):
        self.directory = Path(directory) / "incidents"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock, self.error_logger = clock, error_logger
        self.runtime = runtime_identity()
        self.history = deque(maxlen=MAX_SAMPLES)
        self.lock = threading.Lock()
        self.work = queue.Queue(maxsize=8)
        self.pending = {}
        self.recent = {}
        self.sequence = 0
        self.closed = False
        self.writer = threading.Thread(target=self._write_loop, name="orchid-incident-writer", daemon=True)
        self.writer.start()

    def record(self, sample):
        now = self.clock()
        with self.lock:
            self.sequence += 1
            self.history.append({"sequence": self.sequence, "monotonic": now, "created": stamp(), **deepcopy(sample)})
            while self.history and now - self.history[0]["monotonic"] > HISTORY_SECONDS:
                self.history.popleft()

    def capture(self, kind, message, context, *, exc=None):
        """Bounded memory copy + nonblocking enqueue; disk I/O stays off motor worker."""
        if kind not in ERROR_KINDS or self.closed:
            return None
        now = self.clock()
        signature = (context.get("instance_id"), message)
        with self.lock:
            self.recent = {k: v for k, v in self.recent.items() if now - v[0] < 2}
            if signature in self.recent:
                return self.recent[signature][1]
            identity = uuid4().hex
            metadata = {"id": identity, "created": stamp(), "kind": kind, "message": message,
                        "mode": context.get("mode"), "state": "saving", "sample_count": 0}
            samples = [deepcopy(s) for s in self.history if now - s["monotonic"] <= HISTORY_SECONDS]
            metadata["sample_count"] = len(samples)
            bundle = {"schema_version": 1, "incident": dict(metadata, state="saved"),
                      "runtime": self.runtime, "context": deepcopy(context), "telemetry": samples,
                      "history_seconds": HISTORY_SECONDS,
                      "telemetry_note": "Cached observations only. Timestamps identify stale or missing readings. No extra motor reads.",
                      "target_note": "target is proposed; command_sent confirms send returned. goal_readback is the motor register before this tick's send.",
                      "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else None}
            self.pending[identity] = metadata
            self.recent[signature] = (now, identity)
            try:
                self.work.put_nowait(bundle)
            except queue.Full:
                metadata.update(state="save_failed", save_error="Diagnostic writer queue full")
                self._log_failure(identity, RuntimeError(metadata["save_error"]))
            # Do not let failed writes create an unbounded in-memory archive.
            while len(self.pending) > 40:
                self.pending.pop(next(iter(self.pending)))
            return identity

    def _log_failure(self, identity, exc):
        if self.error_logger:
            self.error_logger.emit("incident_write_failed", str(exc), {"incident_id": identity}, exc)

    def _write_loop(self):
        while True:
            bundle = self.work.get()
            try:
                if bundle is None:
                    return
                identity = bundle["incident"]["id"]
                atomic_json(self.directory / f"{identity}.json", bundle)
                # Metadata is the commit marker; incomplete bundles are never downloadable.
                atomic_json(self.directory / f"{identity}.meta.json", bundle["incident"])
                with self.lock:
                    self.pending.pop(identity, None)
            except Exception as exc:
                with self.lock:
                    self.pending.setdefault(identity, bundle["incident"]).update(state="save_failed", save_error=str(exc))
                self._log_failure(identity, exc)
            finally:
                self.work.task_done()

    def path(self, identity, suffix=".json"):
        # Canonical IDs only: no paths or alternate UUID spellings.
        if UUID(identity).hex != identity:
            raise ValueError("Invalid incident ID")
        return self.directory / f"{identity}{suffix}"

    def metadata(self, identity):
        data = json.loads(self.path(identity, ".meta.json").read_text())
        data["report"] = {"state": "not_requested"}
        request = self.path(identity, ".request.json")
        status = self.path(identity, ".status.json")
        if request.exists():
            data["report"] = {"state": "queued", "requested_at": json.loads(request.read_text())["requested_at"]}
            if status.exists():
                data["report"].update(json.loads(status.read_text()))
        return data

    def listing(self):
        entries = []
        for path in self.directory.glob("*.meta.json"):
            try:
                entries.append(self.metadata(path.name.removesuffix(".meta.json")))
            except (ValueError, OSError, KeyError):
                continue
        with self.lock:
            entries.extend(deepcopy(list(self.pending.values())))
        # A writer finishing between scan and pending snapshot must not duplicate entries.
        entries = list({e["id"]: e for e in entries}.values())
        delivery = {"enabled": False}
        try:
            delivery = json.loads((self.directory / "delivery.json").read_text())
        except (ValueError, OSError):
            pass
        return {"incidents": sorted(entries, key=lambda e: e["created"], reverse=True)[:20], "delivery": delivery}

    def request_report(self, identity, note=""):
        with self.lock:
            self.metadata(identity)  # Require a completed bundle before queueing.
            path = self.path(identity, ".request.json")
            if not path.exists():
                atomic_json(path, {"schema_version": 1, "id": identity, "requested_at": stamp(), "operator_note": note})
        return self.metadata(identity)

    def close(self):
        if not self.closed:
            self.closed = True
            self.work.put(None)
            self.writer.join(timeout=5)
