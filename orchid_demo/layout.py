"""Where the two followers and Orchid sit on the registration frame (FRAME V2, FRAMEV2whole.stl).

Plate frame: millimetres, the STL's own axes; z = 0 is below the frame (its underside, on the table, is z 56.4).
The followers stand on two raised pads at the -y side, behind Orchid; the player's side of Orchid faces +y.
Each follower screws into four tapped M5 sockets. The SO101 base's four 5 mm holes (base_link frame:
(-13.86, ±31.75) and (55.91, ±27.78) mm, from the bundled CAD) fit each socket pattern within 0.06 mm, giving
each base_link origin and the direction the arm faces (base_link +x): 10 deg in from straight at Orchid. The base's
ribs sit on the socket slot floor (z 141.77); base_link is 2.4 mm above the rib bottoms.

Keys Arm (keys) is on the right mount, as seen from the player (plate -x): its taught key presses, run through the
SO101 model, fall in a row across Orchid, low to high toward the player's right, with the black keys behind the
white ones; on the left mount they would fall outside Orchid. Chord Arm (chord buttons) is on the left mount (+x).
Orchid sits in a cradle tilted 10.8 deg down toward the player (its back edge rests at z 131.2, its front edge at
z 97.7), between lips at y 126 and y 307; it is about 43 mm tall (measured). The arms' bases are 265 mm apart
(400 mm on the old plate) and both reach toward Orchid's middle, where the keys meet the chord buttons.
Joint zeros come from hand-centred calibration midpoints, so modelled positions can be off by about 2 cm. The display
uses it as is. The chord clearance check (kinematics.py) uses it only with a margin that covers that error.
"""
MOUNTS = {
    "a": {"xyz": (-132.33, 22.21, 144.17), "yaw_deg": 80.0, "side": "right"},
    "b": {"xyz": (132.33, 22.21, 144.17), "yaw_deg": 100.0, "side": "left"},
}
SOCKETS_MM = {  # tapped M5 sockets, plate frame
    "a": ((-165.96, 14.05), (-103.51, 3.04), (-95.30, 72.45), (-149.91, 82.13)),
    "b": ((165.96, 14.05), (103.51, 3.04), (95.30, 72.45), (149.91, 82.13)),
}
# center: the middle of Orchid's underside, plate x/y, and floor_z its height there; size: Orchid's own width (along
# the keys) and depth. facing_deg: the direction, on the plate, from Orchid's back edge to its front (player's) edge.
# tilt_deg: how far the front edge dips below the back edge.
ORCHID = {"center": (0.0, 216.9), "size": (299.0, 184.0), "floor_z": 114.6, "height": 43.0, "height_estimated": False,
          "facing_deg": 90.0, "tilt_deg": 10.8}
PLATE_TOP_Z = 144.01  # the mount pads' top face
# Measured corrections to the model's shoulder_pan zero, degrees, by follower calibration id (calibration_id: the first
# 12 hex digits of the calibration's fingerprint). A hand-centred pan midpoint can be several degrees off, which swings
# the whole modelled arm sideways about its base; no other joint moves the fingertip sideways. Fitted by
# scripts/fit_model_pan.py from where the taught presses land along Orchid's front edge (measured with a ruler). A
# recalibrated follower has no correction until it is measured again.
# 2026-10-12, FRAME V2: Sus to C 63 mm, Dim to B 246 mm (centre to centre along Orchid); the model had 12 and 188.
# With no reading from Orchid's ends, Dim and B are taken to sit equally far in from them (28 mm): every chord this
# allows is allowed by every split of the error that keeps B on Orchid, so a different split only refuses more.
PAN_CORRECTION_DEG = {
    "8325e2704b76": -3.87,  # Keys Arm
    "78caa059e7ac": 11.68,  # Chord Arm
}


def public():
    return {"mounts": {arm: {**mount, "xyz": list(mount["xyz"])} for arm, mount in MOUNTS.items()},
            "sockets": {arm: [list(p) for p in points] for arm, points in SOCKETS_MM.items()},
            "orchid": {**ORCHID, "center": list(ORCHID["center"]), "size": list(ORCHID["size"])},
            "plate_top_z": PLATE_TOP_Z, "units": "mm"}
