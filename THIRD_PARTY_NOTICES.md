# Third-party notices

## SO101 joint geometry and visual meshes

`orchid_demo/static/arm-geometry.js` contains joint origins, axes, and limits extracted from TheRobotStudio's SO101 model:

- Source: https://github.com/TheRobotStudio/SO-ARM100/blob/385e8d7c68e24945df6c60d9bd68837a4b7411ae/Simulation/SO101/so101_new_calib.urdf
- Upstream repository: TheRobotStudio/SO-ARM100
- License: Apache License 2.0, reproduced in [licenses/SO101-Apache-2.0.txt](licenses/SO101-Apache-2.0.txt)
- Visual mesh source: https://github.com/TheRobotStudio/SO-ARM100/tree/385e8d7c68e24945df6c60d9bd68837a4b7411ae/Simulation/SO101/assets
- Changes: converted joint transforms to a JavaScript data table. `orchid_demo/static/arm-visuals.js` contains thirteen CAD meshes and seventeen visual placements extracted from the same URDF. Mesh vertices are clustered on a 0.65 mm grid and quantized to 0.01 mm for display. The local renderer changes the material colors and adds motor markers/highlights. Dynamics and collision geometry are omitted; rubber tips and cables are not modeled.
- Rebuild with `python scripts/build_so101_visuals.py --cache-dir /tmp/orchid-so101`. The script fetches missing source files from the pinned revision; the app itself never fetches external assets. Source SHA-256 digests and the revision are recorded in the generated data.

This visualization is a model estimate relative to the operator's captured encoder midpoint. It does not establish physical zero alignment, mounting coordinates, contact-tool geometry, or collision clearance. See the operator guide for its limitations.
