"""scripts/migrate.py: export the hardware state on one computer, import it on another."""
import fcntl
import importlib.util
from pathlib import Path

import pytest

from orchid_demo.storage import Repository

spec = importlib.util.spec_from_file_location("orchid_migrate", Path(__file__).resolve().parent.parent / "scripts" / "migrate.py")
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)


def machine(tmp_path, name, monkeypatch):
    root = tmp_path / name
    (root / "calibration").mkdir(parents=True)
    monkeypatch.setenv("HF_LEROBOT_CALIBRATION", str(root / "calibration"))
    return root / "data"


def test_export_then_import_on_a_new_machine(tmp_path, monkeypatch):
    old = machine(tmp_path, "old", monkeypatch)
    repo = Repository(old, "hardware")  # left open: export must work while the console runs
    repo.save_note("C", {"format": "leader_waypoints_v1", "points": {"home": {}}})
    repo.put("teach_settings", {"speed": 2.0, "press_s": 0.4})
    follower = old.parent / "calibration" / "robots/so_follower/so101_follower.json"
    follower.parent.mkdir(parents=True)
    follower.write_text('{"shoulder_pan": {}}')
    bundle = tmp_path / "state.tar.gz"
    migrate.main(["--data-dir", str(old), "export", "-o", str(bundle)])

    new = machine(tmp_path, "new", monkeypatch)
    migrate.main(["--data-dir", str(new), "inspect", str(bundle)])
    migrate.main(["--data-dir", str(new), "import", str(bundle)])
    moved = Repository(new, "hardware")
    assert "C" in moved.notes() and moved.get("teach_settings")["speed"] == 2.0
    assert (new.parent / "calibration" / "robots/so_follower/so101_follower.json").read_text() == '{"shoulder_pan": {}}'
    moved.db.close()

    other = Repository(new, "hardware")  # the new machine has since changed something
    other.put("teach_settings", {"speed": 1.0, "press_s": 0.3})
    other.db.close()
    with pytest.raises(SystemExit, match="already exist"):
        migrate.main(["--data-dir", str(new), "import", str(bundle)])
    migrate.main(["--data-dir", str(new), "import", str(bundle), "--force"])
    assert Repository(new, "hardware").get("teach_settings")["speed"] == 2.0
    assert list((new / "backups").glob("before-import-*"))


def test_import_refuses_while_a_console_owns_the_data(tmp_path, monkeypatch):
    old = machine(tmp_path, "old", monkeypatch)
    Repository(old, "hardware").save_note("C", {"points": {}})
    bundle = tmp_path / "state.tar.gz"
    migrate.main(["--data-dir", str(old), "export", "-o", str(bundle)])
    new = machine(tmp_path, "new", monkeypatch)
    (new / "hardware").mkdir(parents=True)
    with (new / "hardware" / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="console is running"):
            migrate.main(["--data-dir", str(new), "import", str(bundle)])


def test_a_tampered_bundle_is_refused(tmp_path, monkeypatch):
    import io
    import json
    import tarfile
    bundle = tmp_path / "evil.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        manifest = json.dumps({"format": migrate.FORMAT, "files": {"../escape": {"sha256": "x", "bytes": 1}}}).encode()
        for name, data in (("manifest.json", manifest), ("../escape", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(SystemExit, match="Refusing"):
        migrate.read_bundle(bundle)
