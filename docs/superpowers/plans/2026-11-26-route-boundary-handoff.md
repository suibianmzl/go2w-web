# Route boundary handoff implementation plan

**Goal:** Only the actual final task point waits for Nav2 success. Intermediate segment ends advance on physical passage; crossing transitions finish before a cancellable 0.2 s delay and the next goal.

**Spec:** User's 2026-11-26 request: crossing entry sends climb=true and both obstacle parameters=false; crossing exit sends climb=false and both parameters=true. No hardware execution or deployment is authorized by this work.

**Architecture:** Keep the current route scheduler, NavigateThroughPoses/ToPose clients, climbing publisher (2101) and shared ObstaclesCriticAPI. Reuse existing physical proximity/overtaking evidence, preserve point speed constraints, dwell duration, resume checkpoints and loop progression. Pending old goals must be canceled on failure/cancellation before another goal can replace them. Final navigation failures cannot pass via the old distance fallback.

**Tech Stack:** Python asyncio, ROS action clients, aiohttp; local fake ROS and real task coroutines.

## Task 1: Passage and crossing handoff

- [x] Add behavioral regressions with pending intermediate action results: intermediate crossing/speed/dwell/maximum-size segment progression, strict final result, and cancellation/failure cleanup.
- [x] Prove new tests fail against the original scheduler.
- [x] Update `server/go2w_web.py` to advance nonfinal segment ends on physical passage, complete all crossing commands before a cancellable 0.2 s wait, preserve dwell with old-goal cancellation, and stop superseded goals on failed handoff.
- [x] Run focused task, crossing and parameter tests, then local HTTP/WebSocket integration for both navigation modes.
- [x] Complete independent review, fix the cross-action handoff issue, and commit only this work.

## Review focus

- Old goal still running while a parameter write fails or the route is canceled.
- Final task point falsely completed by proximity or Nav2 failure within 0.4 m.
- Loop return/resume incorrectly treated as final.
- Cancellation during 0.2 s or pending parameter writes dispatches another goal.
- Dwell lost when the intermediate point is passed before Nav2 success.

## Decisions

- Existing point dwell durations remain; cancellation stops the previous action before the dwell countdown.
- Intermediate crossing boundaries use physical passage, not Nav2 success; the original crossing markers and parameter command sequence remain.
- This is one coherent scheduler change; implementation stays inline, with one independent review after focused checks.

## Validation results

- New pending-action, strict-final and timing tests failed against the original scheduler before implementation.
- Review added red/green regressions for cross-action fallback and stop during pending entry/exit writes.
- Focused local suite: 72 tests passed (`dev.test_route_force_crossing`, `dev.test_task_controls`, `dev.test_obstacles_critic_api`).
- Real local HTTP/WebSocket + fake ROS integration passed both navigation modes, cancellation, parameter rejection, recovery and startup failure.
- Three-change timing test measures actual monotonic time and requires at least 0.199 s between completion of all commands and next goal (1 ms measurement allowance).
- No real robot commands, deployment or training were performed.
- Independent follow-up review of lifecycle fixes: no confirmed unresolved issues.
