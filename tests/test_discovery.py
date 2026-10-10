"""USB discovery performs only ping/read traffic and always releases ownership."""
import json
import sys
from types import SimpleNamespace

import pytest

import find_ports
from orchid_demo import discovery as d
from orchid_demo import motion as m
from orchid_demo.engine import Engine


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(ids=list(range(1, 7)), raw=124, result=0, error=0,
                            calls=[], closed=False, busy=False)

    class Serial:
        def __init__(self, **kwargs):
            state.calls.append(("open", kwargs))
            assert kwargs["exclusive"] is True
            assert kwargs["timeout"] == 0 and kwargs["write_timeout"] > 0
            if state.busy:
                raise OSError("Resource temporarily unavailable")
        def reset_input_buffer(self):
            state.calls.append(("reset",))
        def close(self):
            state.closed = True

    class PortHandler:
        def __init__(self, path):
            self.port_name, self.ser = path, None
        def setBaudRate(self, baud):
            self.baudrate = baud
            return self.setupPort(baud)
        def closePort(self):
            self.ser.close()

    class Packet:
        def ping(self, handler, motor_id):
            assert not state.closed
            assert handler.tx_time_per_byte == 0.01
            state.calls.append(("ping", motor_id))
            return 777, 0 if motor_id in state.ids else -1, 0
        def read1ByteTxRx(self, handler, motor_id, register):
            state.calls.append(("read", motor_id, register))
            return state.raw, state.result, state.error
        def read2ByteTxRx(self, handler, motor_id, register):  # position limits: identity only, never written
            state.calls.append(("read2", motor_id, register))
            return 1000 + motor_id * 10 + (register == 11) * 2000, 0, 0

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=Serial))
    monkeypatch.setitem(sys.modules, "scservo_sdk", SimpleNamespace(PortHandler=PortHandler, PacketHandler=lambda _: Packet(), COMM_SUCCESS=0))
    return state


@pytest.mark.parametrize("raw,role", [(52, "leader"), (124, "follower"), (80, "follower")])
def test_probe_roles_and_read_only_traffic(sdk, raw, role):
    sdk.raw = raw
    arm = d.probe("/dev/ttyACM0")
    assert arm["role"] == role
    assert arm["voltage"] == raw / 10
    assert arm["motor_ids"] == list(range(1, 7))
    assert arm["voltage_motor_id"] == 1
    assert sdk.calls[2:22] == [("ping", i) for i in range(1, 21)]
    assert sdk.calls[22] == ("read", 1, 62)
    assert sdk.calls[23:] == [("read2", i, r) for i in range(1, 7) for r in (9, 11)]  # reads only
    assert arm["limits"]["3"] == [1030, 3030] and sdk.closed


def test_an_arm_is_recognised_by_the_limits_its_calibration_wrote():
    calibration = {f"m{i}": {"id": i, "range_min": 1000 + i * 10, "range_max": 3000 + i * 10} for i in range(1, 7)}
    limits = {str(i): [1000 + i * 10, 3000 + i * 10] for i in range(1, 7)}
    assert d.matches(calibration, limits)
    assert not d.matches(calibration, {**limits, "4": [1040, 3041]})
    assert not d.matches(None, limits) and not d.matches(calibration, None)


def test_non_robot_port_is_omitted(sdk):
    sdk.ids = []
    assert d.probe("/dev/ttyUSB0") is None
    assert not any(c[0] == "read" for c in sdk.calls)
    assert sdk.closed


@pytest.mark.parametrize("attribute,value", [("result", -1), ("error", 1), ("raw", 0)])
def test_voltage_failures_never_imply_follower(sdk, attribute, value):
    setattr(sdk, attribute, value)
    arm = d.probe("/dev/ttyACM0")
    assert arm["role"] is arm["voltage"] is None
    assert "Voltage unavailable" in d.follower_problem(arm)
    assert sdk.closed


def test_partial_motor_set_visible_but_not_connectable(sdk):
    sdk.ids = [3, 4, 5]
    arm = d.probe("/dev/ttyACM0")
    assert arm["motor_ids"] == [3, 4, 5]
    assert sdk.calls[-1] == ("read", 3, 62)
    assert "IDs 1–6" in d.follower_problem(arm)


def test_busy_port_never_sends_packets(sdk, monkeypatch):
    sdk.busy = True
    monkeypatch.setattr(d, "serial_candidates", lambda: ["/dev/ttyACM0"])
    result = d.discover_arms()
    assert result["arms"] == []
    assert "busy" in result["warnings"][0]
    assert [c[0] for c in sdk.calls] == ["open"]


def test_connected_port_and_its_alias_are_not_opened(sdk, monkeypatch, tmp_path):
    owned = tmp_path / "follower"
    alias = tmp_path / "same-follower"
    alias.symlink_to(owned)
    monkeypatch.setattr(d, "serial_candidates", lambda: [str(owned), str(alias), "/dev/leader"])
    d.discover_arms(exclude_ports=[str(owned)])
    assert [c[1]["port"] for c in sdk.calls if c[0] == "open"] == ["/dev/leader"]


def test_cancellation_closes_open_port(sdk):
    def guard():
        if len(sdk.calls) > 3:
            raise m.SafetyError("Operator connection lost")
    with pytest.raises(m.SafetyError, match="connection lost"):
        d.probe("/dev/ttyACM0", guard=guard)
    assert sdk.closed
    assert len(sdk.calls) < 10


def test_usb_candidates_are_deduplicated_and_exclude_ttys(monkeypatch):
    patterns = []
    def glob(pattern):
        patterns.append(pattern)
        return ["/dev/ttyACM1", "/dev/ttyACM0"]
    monkeypatch.setattr(d.glob, "glob", glob)
    assert d.serial_candidates() == ["/dev/ttyACM0", "/dev/ttyACM1"]
    assert all("ttyS" not in p for p in patterns)


def test_macos_adapter_names_are_candidates(monkeypatch):
    mac = {"/dev/tty.usbmodem5A7A0185321": 1, "/dev/tty.wchusbserial14310": 1, "/dev/cu.usbmodem5A7A0185321": 0,
           "/dev/tty.Bluetooth-Incoming-Port": 0}
    import fnmatch
    monkeypatch.setattr(d.glob, "glob", lambda pattern: [p for p in mac if fnmatch.fnmatch(p, pattern)])
    assert d.serial_candidates() == sorted(p for p, wanted in mac.items() if wanted)


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_missing_voltage_and_warnings(sdk, monkeypatch, capsys, as_json):
    arm = d.probe("/dev/ttyACM0")
    arm.update(voltage=None, role=None)
    monkeypatch.setattr(find_ports, "discover_arms", lambda: {"arms": [arm], "warnings": ["busy adapter"]})
    monkeypatch.setattr(sys, "argv", ["find_ports.py"] + (["--json"] if as_json else []))
    with pytest.raises(SystemExit) as exc:
        find_ports.main()
    assert exc.value.code == 0
    output = capsys.readouterr()
    assert "busy adapter" in output.err
    if as_json:
        assert json.loads(output.out)[0]["voltage"] is None
    else:
        assert "voltage unavailable" in output.out


def test_refresh_replaces_results_and_failure_clears_previous(sdk, tmp_path):
    arm = d.probe("/dev/ttyACM0")
    def scan(**kwargs):
        kwargs["guard"]()
        return {"arms": [arm], "warnings": []}
    e = Engine(tmp_path, "hardware", port_scanner=scan)
    try:
        e.heartbeat("owner")
        e.dispatch("refresh_ports", {})
        assert e.discovery["ports"][0]["connectable"] is True
        assert e.discovery["scanned_at"]
        def failed(**kwargs):
            raise OSError("USB unplugged")
        e.port_scanner = failed
        with pytest.raises(m.SafetyError, match="USB unplugged"):
            e.dispatch("refresh_ports", {})
        assert e.discovery["ports"] == []
        assert not e.discovery["scanning"]
        assert e.discovery["scanned_at"] is None
    finally:
        e.close()


@pytest.mark.parametrize("phase", ["connected", "ready", "calibration_range", "note_touch", "holding", "testing", "fault"])
def test_refresh_rejected_outside_disconnected(tmp_path, phase):
    e = Engine(tmp_path, "hardware", port_scanner=lambda **_: pytest.fail("Probed while busy"))
    try:
        e.phase = phase
        with pytest.raises(m.SafetyError, match="not available"):
            e.dispatch("refresh_ports", {})
    finally:
        e.close()


def test_simulation_never_discovers_hardware(tmp_path):
    e = Engine(tmp_path, port_scanner=lambda **_: pytest.fail("Probed in simulation"))
    try:
        e.dispatch("refresh_ports", {})
        assert e.discovery["ports"][0]["path"] == "simulator"
    finally:
        e.close()


@pytest.mark.parametrize("raw,ids", [(52, list(range(1, 7))), (124, [1, 2, 3]), (0, list(range(1, 7)))])
def test_connect_rejects_ineligible_arms(sdk, tmp_path, raw, ids):
    sdk.raw, sdk.ids = raw, ids
    arm = d.probe("/dev/ttyACM0")
    e = Engine(tmp_path, "hardware", port_scanner=lambda **_: {"arms": [arm], "warnings": []},
               hardware_factory=lambda *_: pytest.fail("Opened an ineligible arm"))
    try:
        e.heartbeat("owner")
        e.dispatch("refresh_ports", {})
        assert not e.discovery["ports"][0]["connectable"]
        with pytest.raises(m.SafetyError, match="detected follower"):
            e.dispatch("connect", {"prepared": True, "fixture": "Test", "port": arm["port"]})
    finally:
        e.close()
