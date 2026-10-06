# Contributing

Use Python 3.12 and run the app from the checkout. Install dependencies with `python -m pip install -r requirements-dev.txt -c constraints-web.txt`. There is no editable project install and no frontend build.

Run `python -m pytest -q` and `python -m ruff check .` before submitting changes. Tests must use fake/simulated motors; connecting to physical hardware is never part of CI. For frontend changes, run `python app.py` and check desktop and narrow layouts, keyboard focus, confirmation gating, countdown cancellation, faults, and the complete calibration-to-note flow.

Keep all serial writes in the single engine worker. Do not add startup/reconnect torque enable, automatic fault torque release, automatic retraction, arbitrary joint goals from HTTP, or a public network listener. A changed control limit needs evidence and a meaningful behavioral test. The UI must distinguish simulated trials from real operator verification.

Preserve recorded sessions. `data/`, legacy key maps, diagnostic backups, and recordings belong in local storage, not Git. Do not auto-import old paths or rename a changed fixture as “unchanged” to bypass invalidation. Schema changes need explicit migration and backup behavior.

Keep changes on a branch and include the behavior change, test results, and any physical commissioning still required in the pull request. Do not claim software tests establish safe physical operation.
