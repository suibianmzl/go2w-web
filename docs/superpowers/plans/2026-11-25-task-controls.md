# Web Task Controls Implementation Plan

> **For agentic workers:** Use the navigation-development, TDD and verification workflows. Tasks 1–2 share navigation interfaces; task 3 is independent and may run in parallel.

**Goal:** Add checkpoint termination, per-point speed limits, a 2 rad/s task angular limit, and multiple bag uploads.

**Architecture:** Extend existing route records and cmd_bridge parameter updates; retain route execution and upload bundle protocols. Add controls to existing cards. Work from main `faa71e3` in an isolated worktree.

**Tech Stack:** Python/aiohttp, ROS interfaces with local fake ROS, vanilla JavaScript and CSS.

## Specification and Constraints

- A termination button in the resume card clears that route's unfinished checkpoint and persists the change; the saved route remains. It cannot alter an active or unrelated navigation task.
- Optional point `vmax` uses task `opts.vmax` when absent. Each leg uses `min(task vmax, source vmax, target vmax)`. The initial approach uses the target and task limits. Returning to the first point and resumed routes retain their actual route endpoints.
- Point limits accept finite numbers in the existing 0.1–1.2 m/s range; an empty control means inherit the task limit. Old routes need no migration.
- Task maximum wz accepts up to 2 rad/s in the UI, persistence and start validation. Existing lower-level hardware safety limits remain in effect.
- Multiple bags may be selected and submitted together. Clicking a different bag's upload remains available while another uploads. Two transfers may run concurrently; additional submissions queue. A bag cannot be submitted twice concurrently or deleted while queued/uploading.
- Each upload retains its independent progress, errors, voice wait, data/audio association and success receipt. One failure must not block other bags.
- No robot deployment, robot motion or training. Do not touch the separate shared checkout's branch or work.

## Task 1: Navigation backend

Files: `server/go2w_web.py`, backend navigation tests, fake ROS if needed.

- [x] Add failing tests for point validation/round trip, endpoint speed selection, inheritance, wz=2, and persisted checkpoint termination.
- [x] Extend point parsing and termination dispatch. Apply each leg's limit before dispatch, rejecting failed parameter changes and restoring the task ceiling as needed.
- [x] Run focused backend and fake-ROS route lifecycle tests.

## Task 2: Navigation controls

Files: `static/js/routes.js`, route sections of `static/index.html` and CSS, navigation sections of `actions.js`, `source/ros.js`, `source/mock.js`.

- [x] Add failing behavior tests for point-limit editing, persistence, reordering and checkpoint termination.
- [x] Add labelled point speed controls and the resume termination button, keep defaults dynamic, raise task wz slider maximum to 2.
- [x] Run UI tests with local mock/fake ROS and inspect desktop/mobile layouts.

## Task 3: Multiple uploads

Files: upload sections of `server/go2w_web.py`, `static/js/panels.js`, upload actions/source mocks, bag sections of HTML/CSS and upload tests.

- [x] Add failing tests for two independently running uploads, duplicate suppression and failure isolation.
- [x] Remove cross-bag rejection; queue safely with two active transfer slots. Add selection/all-selection and an upload-selection action.
- [x] Run bundle regression and local browser behavior tests, including repaint-preserved selection and per-bag progress.

## Task 4: Integration and delivery

- [x] Run the focused combined suite and review the complete diff.
- [x] Update version/release notes, commit only this task's changes, and record validation boundaries.
- [x] Report the commit and deployment status. Push only within existing authorized scope.
