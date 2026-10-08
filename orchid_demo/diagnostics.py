"""Structured operator events on stdout and a bounded, durable diagnostic log."""
import json
import logging
from logging.handlers import RotatingFileHandler
import traceback

from .motion import stamp


class StdoutHandler(logging.Handler):
    def emit(self, record):
        try:
            print(self.format(record), flush=True)
        except Exception:
            self.handleError(record)


class EventLogger:
    def __init__(self, directory):
        self.path = directory / "arm-events.jsonl"
        self.logger = logging.Logger(f"orchid.arm.{id(self)}", level=logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(StdoutHandler())
        self.logger.addHandler(RotatingFileHandler(self.path, maxBytes=5_000_000, backupCount=3))

    def emit(self, kind, message, context=None, exc=None):
        record = {"created": stamp(), "kind": kind, "message": message, **(context or {})}
        if exc is not None:
            record["exception"] = type(exc).__name__
            record["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        level = logging.ERROR if kind in ("fault", "rejected", "connection_failed", "hold_failed", "worker_failed", "cleanup_failed") else logging.INFO
        self.logger.log(level, json.dumps(record, allow_nan=False))

    def close(self):
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)
