"""Orchid's voicing dial is the operator's stop: a hand on it holds every arm where it is.

The robots never turn the voicing dial, so any turn Orchid reports through Orchid Studio's key monitor (not Studio setting
the voicing itself, which Studio marks "software") is a person asking the arms to stop. It does what the console's Stop
and Esc do: every arm holds where it is and a performance in progress ends; the operator resumes in the console.

It is a software stop: Orchid, its USB MIDI, Orchid Studio (with its key monitor) and orchid-robot must all be running,
and it acts within about a tenth of a second. Keep the arms' power cut-off as the emergency stop.
"""
from __future__ import annotations

import threading
import time

POLL_S = 0.05  # how often Studio's key events are read
RETRY_S = 1.0  # after Studio could not be read


class DialStop:
    """Watches Studio's key events and calls stop(turn) on a hand on the voicing dial. studio(command, **fields) ->
    reply; state says whether it is watching ("armed"), why not ("error"), and the stops it made."""

    def __init__(self, studio, stop, sleep=time.sleep):
        self.studio, self.stop, self.sleep = studio, stop, sleep
        self.cursor = None
        self.state = {"armed": False, "error": None, "stops": 0, "last": None}
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self.run, name="orchid-dial-stop", daemon=True)

    def poll(self):
        """One read of Studio's key events: stops the arms on a hand on the dial. Returns the turns it acted on."""
        reply = self.studio("key-events", after=self.cursor or 0)
        last = reply.get("last_key_event_id", 0)
        if self.cursor is None or last < self.cursor:  # starting, or Studio restarted: only turns from now on count
            self.cursor = last
            self.state.update(armed=True, error=None)
            return []
        self.cursor = last
        turns = [e for e in reply.get("events") or [] if e.get("type") == "voicing" and e.get("source", "dial") == "dial"]
        if turns:
            turn = turns[-1]
            self.state.update(stops=self.state["stops"] + 1, last={"t": turn.get("t"), "value": turn.get("value")})
            self.stop(turn)
        self.state.update(armed=True, error=None)
        return turns

    def run(self):
        while not self.finished.is_set():
            try:
                self.poll()
            except Exception as exc:  # Studio down, restarting or without its key monitor: say so, and keep trying
                self.state.update(armed=False, error=str(exc))
                self.cursor = None
                self.sleep(RETRY_S)
                continue
            self.sleep(POLL_S)

    def start(self):
        self.thread.start()

    def close(self):
        self.finished.set()
        if self.thread.is_alive():
            self.thread.join(2.0)
