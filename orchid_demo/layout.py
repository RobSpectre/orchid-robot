"""Where the two followers and Orchid sit on the registration plate (NEBIUSSR101_ORCHID PLATE V2).

Plate frame: millimetres, the STL's own axes; z = 0 near the plate's underside, the plate top is z 8.26.
Each follower screws into four tapped M5 sockets. The SO101 base's four 5 mm holes (base_link frame:
(-13.86, ±31.75) and (55.91, ±27.78) mm, from the bundled CAD) fit each socket pattern within 0.10 mm, giving
each base_link origin and the direction the arm faces (base_link +x). The base's ribs sit on the socket slot
floor (z 2.45); base_link is 2.4 mm above the rib bottoms.

Arm A (keys) is on the right mount: its taught key presses, run through the SO101 model, fall in a row across
Orchid with the black keys behind the white ones; on the left mount they would not. Arm B (chord buttons and
dial) is on the left mount. Orchid sits in the rimmed pocket and is about 43 mm tall (measured).
Display only: joint zeros come from hand-centred calibration midpoints, so modelled positions can be off by
about 2 cm. Never use this for collision checks.
"""
MOUNTS = {
    "a": {"xyz": (200.19, 776.69, 4.85), "yaw_deg": -120.0, "side": "right"},
    "b": {"xyz": (-200.19, 776.69, 4.85), "yaw_deg": -60.0, "side": "left"},
}
SOCKETS_MM = {  # tapped M5 sockets, plate frame
    "a": ((179.71, 804.62), (234.63, 772.92), (148.17, 742.06), (196.21, 714.33)),
    "b": ((-179.71, 804.62), (-234.63, 772.92), (-148.17, 742.06), (-196.21, 714.33)),
}
ORCHID = {"center": (0.0, 591.1), "size": (299.0, 184.0), "floor_z": 0.61, "height": 43.0, "height_estimated": False}
PLATE_TOP_Z = 8.26


def public():
    return {"mounts": {arm: {**mount, "xyz": list(mount["xyz"])} for arm, mount in MOUNTS.items()},
            "sockets": {arm: [list(p) for p in points] for arm, points in SOCKETS_MM.items()},
            "orchid": {**ORCHID, "center": list(ORCHID["center"]), "size": list(ORCHID["size"])},
            "plate_top_z": PLATE_TOP_Z, "units": "mm"}
