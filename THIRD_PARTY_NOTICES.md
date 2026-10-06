# Third-party notices

## SO101 joint geometry

`orchid_demo/static/arm-geometry.js` contains joint origins, axes, and limits extracted from TheRobotStudio's SO101 model:

- Source: https://github.com/TheRobotStudio/SO-ARM100/blob/385e8d7c68e24945df6c60d9bd68837a4b7411ae/Simulation/SO101/so101_new_calib.urdf
- Upstream repository: TheRobotStudio/SO-ARM100
- License: Apache License 2.0, reproduced in [licenses/SO101-Apache-2.0.txt](licenses/SO101-Apache-2.0.txt)
- Changes: converted joint transforms to a JavaScript data table. CAD meshes, dynamics, and collision geometry are omitted. The console draws simplified links and pads, with an illustrative jaw opening.

This visualization is a model estimate relative to the operator's captured encoder midpoint. It does not establish physical zero alignment, mounting coordinates, contact-tool geometry, or collision clearance. See the operator guide for its limitations.
