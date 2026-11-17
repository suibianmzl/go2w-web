# Repository Guidelines

## Project Structure & Module Organization

- `server/`: Python aiohttp/ROS 2 backend for navigation, maps, camera, recording, and process control.
- `static/`: Dependency-free HTML, CSS, and ES-module JavaScript for the web console.
- `dev/`: Fake-ROS environment, local runner, and regression tests.
- `tools/`: Python and C++ rollback recorders.
- `prototype/`: Standalone HTML interface prototypes.
- `docs/`: Ignored local notes; do not force-add it.
- `README.md`: Operational, safety, deployment, and test guidance.

## Build, Test, and Development Commands

This project has no package manager or frontend build step.

- `dev/run.sh`: Creates the Python environment when needed and serves `http://127.0.0.1:8080`.
- `GO2W_MAPS_SRC=/path/to/data/maps dev/run.sh`: Supplies maps for standalone checkouts.
- `python3 dev/test_procs.py`: Runs offline process-management regression tests.
- `python3 dev/test_camera.py`: Runs offline camera RTP parsing tests.
- `node dev/test_protocol.mjs`: Runs protocol tests after `dev/run.sh` is active.
- `node dev/test_routes.mjs`: Runs route behavior tests against the same fake backend.
- `bash tools/rollback_recorder_cpp/build.sh`: Builds the C++17 recorder in ROS 2 Humble.

No aggregate test command exists; run focused tests.

## Coding Style & Naming Conventions

- Use four-space indentation in Python and two spaces in JavaScript and C++.
- Use `snake_case` for Python functions and C++ helpers, `PascalCase` for classes, and `camelCase` for JavaScript functions and variables.
- Keep constants in `UPPER_SNAKE_CASE`; preserve established protocol names.
- No formatter or linter is configured; match nearby code. Avoid reformatting `static/js/vendor/`.
- The backend and frontend share JSON commands and binary frame formats. Update both sides and tests together when changing them.

## Testing Guidelines

Name scripts `test_<feature>.py` or `test_<feature>.mjs` under `dev/`. They are executable scripts, not pytest or npm suites. Add focused regressions for changed behavior; no coverage threshold is enforced.

Integration tests use fake ROS. Start `dev/run.sh` separately and verify URLs are local. Never point navigation, emergency-stop, or teleoperation tests at a physical robot.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects, optionally prefixed by area, for example `server: reject invalid map names` or `test_routes: cover cancellation`. Recent history uses both English and Chinese; keep each commit coherent.

Pull requests should explain behavior and safety impact, list verification commands, link issues, and include UI screenshots. Call out missing physical-robot validation.

## Configuration & Safety

Runtime data normally lives under `~/.cache/go2w_web_data`. Relevant variables include `PORT`, `GO2W_DATA`, and `GO2W_MAPS_SRC`. The console has no authentication, so use it only on trusted networks. Preserve heartbeat, emergency-stop, locking, and parameter-validation safeguards.
