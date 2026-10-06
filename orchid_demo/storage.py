"""Durable local state; simulation and hardware each get their own database."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .motion import KEYS, stamp


class Repository:
    def __init__(self, directory: Path, mode: str):
        self.directory = directory / mode
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.db = sqlite3.connect(self.directory / "operator.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError(f"Unsupported database version {version}; restore with a compatible application.")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notes (note TEXT PRIMARY KEY, entry TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created TEXT NOT NULL, kind TEXT NOT NULL,
                message TEXT NOT NULL, detail TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)

    def get(self, name, default=None):
        row = self.db.execute("SELECT value FROM documents WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, name, value):
        with self.db:
            self.db.execute("INSERT INTO documents VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                            (name, json.dumps(value, allow_nan=False)))

    def notes(self):
        saved = {name: json.loads(entry) for name, entry in self.db.execute("SELECT note,entry FROM notes")}
        return {note: saved.get(note) for note in KEYS}

    def save_note(self, note, entry):
        if note not in KEYS:
            raise ValueError("Unknown note")
        # Archive every accepted revision in the same transaction as the current note.
        with self.db:
            self.db.execute("INSERT INTO notes VALUES (?,?) ON CONFLICT(note) DO UPDATE SET entry=excluded.entry",
                            (note, json.dumps(entry, allow_nan=False)))
            self.db.execute("INSERT INTO events(created,kind,message,detail) VALUES (?,?,?,?)",
                            (stamp(), "note_saved", f"{note} trial accepted", json.dumps(entry)))

    def event(self, kind, message, detail=None):
        with self.db:
            self.db.execute("INSERT INTO events(created,kind,message,detail) VALUES (?,?,?,?)",
                            (stamp(), kind, message, json.dumps(detail or {}, allow_nan=False)))

    def events(self, limit=40):
        return [{"id": row[0], "created": row[1], "kind": row[2], "message": row[3]}
                for row in self.db.execute("SELECT id,created,kind,message FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def export(self):
        return {"schema_version": 1, "application": "orchid-demo", "mode": self.mode,
                "exported_at": stamp(), "calibration": self.get("calibration"),
                "fixture": self.get("fixture"), "keys": self.notes(), "events": self.events(1000)}

    def close(self):
        self.db.close()
