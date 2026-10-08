#!/usr/bin/env python3
"""Move the Orchid operator state to another computer (Linux or macOS).

    python3 scripts/migrate.py export                    # writes orchid-state-<host>-<time>.tar.gz
    python3 scripts/migrate.py export --logs             # also the large motion/leader logs
    python3 scripts/migrate.py inspect orchid-state-....tar.gz
    python3 scripts/migrate.py import orchid-state-....tar.gz [--force]

The bundle holds what git does not: the hardware console database (taught keys, home/rest poses,
dial steps, speed and press settings, motor calibrations), its backups and incident reports, the
LeRobot calibration files used by teach_key.py, and any teach_key.py recordings (keys/*.json).
Standard library only. Export works while the console runs (SQLite online backup); import refuses
while a console owns the data directory and never overwrites without --force (then the current
files are moved aside first). The motors keep their own calibration; the taught poses only hold
if the arm and Orchid are mounted the same way on the new desk.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
FORMAT = "orchid-state-v1"
CALIBRATION_FILES = ("robots/so_follower/so101_follower.json", "teleoperators/so_leader/so101_leader.json")
SMALL_STATE = ("voltage-diagnosis.json",)
LOG_PATTERNS = ("arm-events.jsonl", "leader-*.jsonl")


def lerobot_calibration_dir() -> Path:
    """LeRobot's HF_LEROBOT_CALIBRATION, computed the way lerobot.utils.constants does."""
    if os.getenv("HF_LEROBOT_CALIBRATION"):
        return Path(os.environ["HF_LEROBOT_CALIBRATION"]).expanduser()
    if os.getenv("HF_LEROBOT_HOME"):
        return Path(os.environ["HF_LEROBOT_HOME"]).expanduser() / "calibration"
    hf_home = os.getenv("HF_HOME") or Path(os.getenv("XDG_CACHE_HOME", "~/.cache")) / "huggingface"
    return Path(hf_home).expanduser() / "lerobot" / "calibration"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sqlite_snapshot(path: Path) -> bytes:
    """A consistent single-file copy of a WAL database, safe while the console writes to it."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.sqlite3"
        source = sqlite3.connect(path)  # WAL: reading alongside the running console is safe
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
            source.close()
        return copy.read_bytes()


def collect(data_dir: Path, logs: bool) -> dict[str, bytes]:
    """Bundle path -> bytes. Bundle paths: data/..., lerobot/..., keys/..."""
    files: dict[str, bytes] = {}
    hardware = data_dir / "hardware"
    database = hardware / "operator.sqlite3"
    if database.exists():
        files["data/hardware/operator.sqlite3"] = sqlite_snapshot(database)
    for name in SMALL_STATE:
        if (hardware / name).is_file():
            files[f"data/hardware/{name}"] = (hardware / name).read_bytes()
    for folder in ("incidents",):
        for path in sorted((hardware / folder).glob("*")) if (hardware / folder).is_dir() else []:
            if path.is_file():
                files[f"data/hardware/{folder}/{path.name}"] = path.read_bytes()
    if logs:
        for pattern in LOG_PATTERNS:
            for path in sorted(hardware.glob(pattern)):
                files[f"data/hardware/{path.name}"] = path.read_bytes()
    backups = data_dir / "backups"
    for backup in sorted(backups.iterdir()) if backups.is_dir() else []:
        if (backup / "operator.sqlite3").exists():
            files[f"data/backups/{backup.name}/operator.sqlite3"] = sqlite_snapshot(backup / "operator.sqlite3")
    calibration = lerobot_calibration_dir()
    for relative in CALIBRATION_FILES:
        if (calibration / relative).is_file():
            files[f"lerobot/{relative}"] = (calibration / relative).read_bytes()
    for path in sorted((ROOT / "keys").glob("*.json")):
        files[f"keys/{path.name}"] = path.read_bytes()
    return files


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def export(args) -> None:
    files = collect(args.data_dir, args.logs)
    if "data/hardware/operator.sqlite3" not in files:
        raise SystemExit(f"No hardware console database in {args.data_dir / 'hardware'}; nothing to move.")
    manifest = {"format": FORMAT, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": platform.node(),
                "platform": platform.platform(), "git_commit": git_commit(),
                "files": {name: {"sha256": sha256(data), "bytes": len(data)} for name, data in files.items()}}
    out = args.output or Path(f"orchid-state-{platform.node().split('.')[0]}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    with tarfile.open(out, "w:gz") as bundle:
        for name, data in {"manifest.json": json.dumps(manifest, indent=2).encode(), **files}.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
            bundle.addfile(info, io.BytesIO(data))
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {len(files)} files).")
    missing = [r for r in CALIBRATION_FILES if f"lerobot/{r}" not in files]
    if missing:
        print("Not found (teach_key.py can recreate them with sync-calibration): " + ", ".join(missing))
    print("Copy it to the new computer and run: python3 scripts/migrate.py import " + out.name)


def read_bundle(path: Path) -> tuple[dict, dict[str, bytes]]:
    with tarfile.open(path, "r:gz") as bundle:
        members = {m.name: m for m in bundle.getmembers()}
        if "manifest.json" not in members:
            raise SystemExit(f"{path} is not an Orchid state bundle (no manifest.json).")
        manifest = json.loads(bundle.extractfile(members["manifest.json"]).read())
        if manifest.get("format") != FORMAT:
            raise SystemExit(f"Unsupported bundle format {manifest.get('format')!r}.")
        files = {}
        for name, expected in manifest["files"].items():
            parts = Path(name).parts
            if (name not in members or not members[name].isfile() or Path(name).is_absolute() or ".." in parts
                    or parts[0] not in ("data", "lerobot", "keys")):
                raise SystemExit(f"Refusing bundle entry {name!r}.")
            data = bundle.extractfile(members[name]).read()
            if sha256(data) != expected["sha256"]:
                raise SystemExit(f"{name} is damaged (checksum mismatch). Export it again.")
            files[name] = data
    return manifest, files


def destination(name: str, data_dir: Path) -> Path:
    parts = Path(name).parts
    if parts[0] == "data":
        return data_dir.joinpath(*parts[1:])
    if parts[0] == "lerobot":
        return lerobot_calibration_dir().joinpath(*parts[1:])
    return ROOT.joinpath(*parts)


def inspect(args) -> None:
    manifest, files = read_bundle(args.bundle)
    print(f"{args.bundle}: from {manifest['host']} ({manifest['platform']}) at {manifest['created']}, "
          f"commit {(manifest.get('git_commit') or 'unknown')[:10]}")
    database = files["data/hardware/operator.sqlite3"]
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "db").write_bytes(database)
        db = sqlite3.connect(Path(tmp) / "db")
        notes = [row[0] for row in db.execute("SELECT note FROM notes ORDER BY note")]
        documents = [row[0] for row in db.execute("SELECT name FROM documents ORDER BY name")]
        db.close()
    print(f"  taught controls ({len(notes)}): {' '.join(notes)}")
    print(f"  saved documents: {', '.join(d for d in documents if d.startswith(('teach_', 'calibration')))}")
    for name, data in files.items():
        print(f"  {name:<58} {len(data) / 1e3:>9.1f} kB -> {destination(name, args.data_dir)}")


def console_running(data_dir: Path) -> bool:
    lock = data_dir / "operator.lock"
    if not lock.exists():
        return False
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def do_import(args) -> None:
    manifest, files = read_bundle(args.bundle)
    if console_running(args.data_dir / "hardware"):
        raise SystemExit("The hardware console is running on this computer. Stop it (Ctrl-C in its terminal), then import.")
    targets = {name: destination(name, args.data_dir) for name in files}
    existing = [path for path in targets.values() if path.exists() and path.read_bytes() != files[_name(targets, path)]]
    if existing and not args.force:
        raise SystemExit("These files already exist here and differ:\n  " + "\n  ".join(map(str, existing))
                         + "\nNothing was changed. Re-run with --force to move them aside and import.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = args.data_dir / "backups" / f"before-import-{stamp}"
    for path in existing:
        kept = aside / path.relative_to(path.anchor)
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(path, kept)
        for suffix in ("-wal", "-shm"):
            if Path(f"{path}{suffix}").exists():
                shutil.move(f"{path}{suffix}", f"{kept}{suffix}")
    for name, path in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):  # a stale journal must not be replayed onto the imported database
            if path.suffix == ".sqlite3" and Path(f"{path}{suffix}").exists():
                os.remove(f"{path}{suffix}")
        tmp = path.with_name(path.name + ".importing")
        tmp.write_bytes(files[name])
        os.replace(tmp, path)
    print(f"Imported {len(files)} files from {manifest['host']} ({manifest['created']}).")
    if existing:
        print(f"Previous files were moved to {aside}.")
    print("Next: start the console with --enable-hardware, connect, and play one key with the speed slider at 1× or lower before playing more.\n"
          "If the arm or Orchid were re-mounted, re-teach hover/touch/press (home first).")


def _name(targets: dict[str, Path], path: Path) -> str:
    return next(name for name, target in targets.items() if target == path)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data", help="the console's --data-dir (default: data/)")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--logs", action="store_true", help="include the large motion and leader logs")
    ex.add_argument("-o", "--output", type=Path)
    sub.add_parser("inspect").add_argument("bundle", type=Path)
    im = sub.add_parser("import")
    im.add_argument("bundle", type=Path)
    im.add_argument("--force", action="store_true", help="move differing files aside instead of refusing")
    args = parser.parse_args(argv)
    {"export": export, "inspect": inspect, "import": do_import}[args.command](args)


if __name__ == "__main__":
    main(sys.argv[1:])
