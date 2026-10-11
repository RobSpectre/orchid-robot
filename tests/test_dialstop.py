"""Orchid's voicing dial is the operator's stop: a hand on it holds every arm where it is (dialstop.py)."""
import pytest

from orchid_demo import motion as m
from orchid_demo.dialstop import DialStop


class FakeStudio:
    def __init__(self):
        self.events, self.last, self.down = [], 0, False

    def turn(self, value, source="dial"):
        self.last += 1
        self.events.append({"id": self.last, "type": "voicing", "value": value, "delta": 1, "source": source, "t": 1.0})

    def press(self):
        self.last += 1
        self.events.append({"id": self.last, "type": "press", "name": "C", "t": 1.0})

    def __call__(self, command, **fields):
        if self.down:
            raise m.SafetyError("Orchid Studio is not reachable")
        assert command == "key-events"
        return {"events": [e for e in self.events if e["id"] > fields["after"]], "last_key_event_id": self.last}


def test_a_hand_on_the_voicing_dial_stops_the_arms_and_nothing_else_does():
    studio, stops = FakeStudio(), []
    studio.turn(20)  # before the watch began: history, not a stop
    watcher = DialStop(studio, stops.append, sleep=lambda s: None)
    watcher.poll()
    assert stops == [] and watcher.state["armed"]
    studio.press()  # the arms playing keys
    studio.turn(30, source="software")  # Orchid answering Studio's own chord-voicing
    assert watcher.poll() == [] and stops == []
    studio.turn(31)
    studio.turn(32)  # one stop for a quick turn of several clicks
    assert len(watcher.poll()) == 2 and [s["value"] for s in stops] == [32]
    assert watcher.state["stops"] == 1 and watcher.state["last"]["value"] == 32
    studio.last, studio.events = 2, [{"id": 1, "type": "press"}, {"id": 2, "type": "press"}]  # Studio restarted
    assert watcher.poll() == [] and len(stops) == 1  # re-baselined, not a stop
    studio.turn(10)
    watcher.poll()
    assert [s["value"] for s in stops] == [32, 10]  # still watching after the restart


def test_the_watch_says_when_studio_cannot_be_read_and_keeps_trying():
    studio, stops = FakeStudio(), []
    watcher = DialStop(studio, stops.append, sleep=lambda s: watcher.finished.set())
    studio.down = True
    watcher.run()
    assert not watcher.state["armed"] and "not reachable" in watcher.state["error"] and watcher.cursor is None


def test_a_dial_stop_holds_the_arm_and_says_why(tmp_path):
    from orchid_demo.engine import Engine
    engine = Engine(tmp_path)
    engine.request_stop("Stopped by Orchid's voicing dial")
    with pytest.raises(m.SafetyError, match="voicing dial"):
        engine.guard()


def test_the_app_watches_the_dial_and_both_arms_stop(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from orchid_demo.api import create_app
    studio = FakeStudio()
    app = create_app(tmp_path, studio=studio, dial_stop=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        until = time.monotonic() + 5
        while not client.get("/api/controls").json()["dial_stop"]["armed"] and time.monotonic() < until:
            time.sleep(0.02)
        studio.turn(40)
        while not all(e.stop_event.is_set() or getattr(e, "stop_reason", None) for e in app.state.engines.values()) \
                and time.monotonic() < until:
            time.sleep(0.02)
        assert all("voicing dial" in (e.stop_reason or "") for e in app.state.engines.values())
        assert client.get("/api/controls").json()["dial_stop"]["stops"] == 1
    with TestClient(create_app(tmp_path / "plain", studio=studio), base_url="http://127.0.0.1") as client:
        assert client.get("/api/controls").json()["dial_stop"]["armed"] is False  # no Studio port: no watch by default
