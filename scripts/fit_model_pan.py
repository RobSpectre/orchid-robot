#!/usr/bin/env python3
"""Fit both followers' shoulder_pan corrections (layout.PAN_CORRECTION_DEG) from ruler measurements on Orchid.

With the arms at rest, lay a ruler along Orchid's front edge and measure, in mm, between the centres of keys and chord
buttons (--apart, e.g. Sus to C), or from the left end of Orchid's body (as you face the keys: the end nearest the chord
buttons) to a centre (--at):

    python scripts/fit_model_pan.py --apart Sus:C=63 --apart Dim:B=246
    python scripts/fit_model_pan.py --at Dim=28 --at C=148 --at Sus=85 --at B=271

Each taught press, run through the model, gives where the finger lands along Orchid; one pan correction per arm moves
those so they agree with the ruler. A pan zero error is the only one that moves a fingertip sideways, so if the leftover
misfit is a few mm, a pan correction alone explains the model's error. Distances between the two arms' controls fix how
far apart the arms are, not where the pair sits along Orchid: without an --at, the fit assumes the outermost measured
controls sit equally far in from Orchid's two ends, and prints where that puts them.
Prints the corrections to put in layout.PAN_CORRECTION_DEG and the chord clearance (kinematics.clearance) of every taught
key with every taught chord button, before and after. Reads copies of the arms' databases (DATA_DIR/hardware and
DATA_DIR/arm-b/hardware); never touches the arms.
"""
import argparse
import math
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchid_demo import kinematics, layout  # noqa: E402
from orchid_demo import motion as m  # noqa: E402
from orchid_demo.controls import CATALOG  # noqa: E402
from orchid_demo.engine import Engine  # noqa: E402
from orchid_demo.rig import NAMES, ROLES  # noqa: E402


def along(tip):
    """mm along Orchid's front edge from the left end of its body (as the player sees it) to above this point."""
    o = layout.ORCHID
    facing = math.radians(o["facing_deg"])
    right = (-math.sin(facing), math.cos(facing))
    return (tip[0] - o["center"][0]) * right[0] + (tip[1] - o["center"][1]) * right[1] + o["size"][0] / 2


class Model:
    """Where the taught presses land along Orchid with given pan corrections."""

    def __init__(self, engines):
        self.engines = engines  # {"a": Keys Arm, "b": Chord Arm}
        self.ids = {arm: m.fingerprint(e.calibration)[:12] for arm, e in engines.items()}

    def along(self, pans, control):
        arm = "a" if control in ROLES["a"] else "b"
        engine, saved = self.engines[arm], dict(layout.PAN_CORRECTION_DEG)
        layout.PAN_CORRECTION_DEG[self.ids[arm]] = pans[arm]
        try:
            return along(kinematics.fingertip(engine.control_points(control)["press"]["goal"], engine.calibration, arm)[0])
        finally:
            layout.PAN_CORRECTION_DEG.clear()
            layout.PAN_CORRECTION_DEG.update(saved)

    def misfits(self, pans, readings):
        """[modelled minus measured, mm] for each reading ((first or None for Orchid's left end, second, mm)); with no
        reading from the left end, also how much further in from its end the leftmost control sits than the rightmost."""
        spot = {c: self.along(pans, c) for r in readings for c in r[:2] if c}
        out = [(abs(spot[second] - (spot[first] if first else 0.0)) - mm) for first, second, mm in readings]
        if all(first for first, _, _ in readings):
            out.append(min(spot.values()) - (layout.ORCHID["size"][0] - max(spot.values())))
        return out


def fit(model, readings):
    """{arm: pan correction, degrees} that best fits the readings (least squares; a grid, then finer around the best)."""
    cost = lambda pans: sum(e * e for e in model.misfits(pans, readings))  # noqa: E731
    best, step, span = (0.0, 0.0), 0.5, 20.0
    for _ in range(3):
        grid = [best[0] + i * step for i in range(-int(span / step), int(span / step) + 1)]
        others = [best[1] + i * step for i in range(-int(span / step), int(span / step) + 1)]
        best = min(((pa, pb) for pa in grid for pb in others), key=lambda p: cost({"a": p[0], "b": p[1]}))
        span, step = step * 2, step / 10
    return {"a": round(best[0], 2), "b": round(best[1], 2)}


def table(keys, chords):
    controls = [c for c in ROLES["b"] if chords.repo.controls().get(c)]
    rows = {k: {c: kinematics.clearance(keys.chord_path(k), keys.calibration, chords.chord_path(c), chords.calibration)
                for c in controls} for k in ROLES["a"] if keys.repo.notes().get(k)}
    return controls, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--at", action="append", default=[], metavar="CONTROL=MM",
                        help="a key (C..B) or chord button (Dim, Sus, ...) and its centre's distance from Orchid's left end")
    parser.add_argument("--apart", action="append", default=[], metavar="CONTROL:CONTROL=MM",
                        help="the distance along Orchid between two controls' centres")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    names = {**{v["label"]: k for k, v in CATALOG.items()}, **{k: k for k in CATALOG}}
    readings = []
    for item, pair in [(i, False) for i in args.at] + [(i, True) for i in args.apart]:
        spec, _, mm = item.partition("=")
        first, _, second = spec.partition(":") if pair else ("", "", spec)
        controls = [names.get(c.strip()) for c in ((first, second) if pair else (second,))]
        if None in controls or not mm:
            parser.error(f"{item}: expected {'CONTROL:CONTROL' if pair else 'CONTROL'}=MM with keys (C..B) or chord "
                         "buttons (Dim, Sus, ...)")
        readings.append((controls[0] if pair else None, controls[-1], float(mm)))
    if not readings:
        parser.error("give at least one --at or --apart")
    with tempfile.TemporaryDirectory() as tmp:
        for part in ("hardware", "arm-b/hardware"):  # a consistent copy, even while orchid-robot is running
            (Path(tmp) / part).mkdir(parents=True)
            source = sqlite3.connect(f"file:{(args.data_dir / part / 'operator.sqlite3').resolve()}?mode=ro", uri=True)
            with sqlite3.connect(Path(tmp) / part / "operator.sqlite3") as copy:
                source.backup(copy)
            source.close()
        keys = Engine(Path(tmp), mode="hardware", owns=ROLES["a"])
        chords = Engine(Path(tmp) / "arm-b", mode="hardware", owns=ROLES["b"])
        chords.arm_id = "b"
        model = Model({"a": keys, "b": chords})
        now = {arm: kinematics.pan_correction(e.calibration) for arm, e in model.engines.items()}
        before = table(keys, chords)
        pans = fit(model, readings)
        for arm in ("a", "b"):
            print(f"{NAMES[arm]} (calibration {model.ids[arm]}): pan correction {pans[arm]:+.2f} deg (now {now[arm]:+.2f})")
        old, new = model.misfits(now, readings), model.misfits(pans, readings)
        for (first, second, mm), o, n in zip(readings, old, new):
            what = f"{CATALOG[first]['label'] if first else 'left end'} to {CATALOG[second]['label']}"
            print(f"  {what:>18}: {mm:5.0f} mm measured, modelled {mm + o:5.0f} -> {mm + n:5.0f}")
        spots = sorted((model.along(pans, c), c) for r in readings for c in set(r[:2]) if c)
        print(f"  which puts {CATALOG[spots[0][1]]['label']} {spots[0][0]:.0f} mm from Orchid's left end and "
              f"{CATALOG[spots[-1][1]]['label']} {layout.ORCHID['size'][0] - spots[-1][0]:.0f} mm from its right end")
        for arm, e in model.engines.items():
            layout.PAN_CORRECTION_DEG[model.ids[arm]] = pans[arm]
        after = table(keys, chords)
    controls, _ = before
    print(f"\nChord clearance, mm (refused below {kinematics.CLEARANCE_MM:.0f}): before -> after")
    print("     " + "".join(f"{CATALOG[c]['label']:>13}" for c in controls))
    for key, row in before[1].items():
        print(f"{key:<4} " + "".join(f"{row[c]:6.0f} ->{after[1][key][c]:4.0f}{'*' if after[1][key][c] < kinematics.CLEARANCE_MM else ' '}"
                                     for c in controls))
    print("\nIn orchid_demo/layout.py:\nPAN_CORRECTION_DEG = {" + ", ".join(f'"{model.ids[a]}": {pans[a]}' for a in ("a", "b")) + "}")


if __name__ == "__main__":
    main()
