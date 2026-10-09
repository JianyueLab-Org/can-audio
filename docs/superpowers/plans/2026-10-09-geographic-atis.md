# Geographic ATIS implementation plan

> For agentic workers: use superpowers:subagent-driven-development for implementation and independent review. Do not commit, push, or deploy.

**Goal:** Restrict ATIS reception to fresh, unambiguous nearby FSD positions.

**Architecture:** A shared in-memory FSD feed reader selects explicit Mumble sessions. A sender-thread routing gate programs nonzero whispers and discards obsolete buffered audio. Controller ATIS reception does not cross-couple.

**Tech stack:** Python 3.12, pymumble, standard-library HTTP/threading, existing component unittest suites.

**Spec:** `docs/superpowers/specs/2026-10-09-geographic-atis-design.md`

## Global constraints

- ATIS only; ordinary voice remains unchanged.
- Default radius 100 NM; feed poll 5 s; feed expiry 20 s; position expiry 90 s; future tolerance 5 s.
- Pilot freshness requires additive `position_time`; controllers/stations use `send_time`.
- Missing/stale/invalid/ambiguous data fails closed; output never uses target 0.
- Helpers are flat, byte-identical per-component copies.
- Preserve existing dirty changes, branches, submodule pointers, and existing worktrees.
- All temporary artifacts and reports use `can-audio/.temp/geographic-atis/`; tests use the provided project-local environment and temp directory.
- No location persistence/logging, secrets, production network tests, commits, pushes, or deployment.
- User approved the design in chat. Continue without further nonblocking approval questions.

## Task 1: Geographic transmission and relay boundary

**Files**

- Create: `atis/atisrouting.py`, `server/ATIS/atisrouting.py`.
- Modify: `atis/broadcast.py`, `atis/gui.py`, `atis/settings.py`.
- Modify: `server/ATIS/mumble.py`, daemon configuration as needed.
- Modify: `controller/voice.py`.
- Test: per-component `test_atisrouting.py` plus focused broadcaster/controller regressions in existing suites.
- Document: `CLAUDE.md` and configuration/rollout instructions.

**Interfaces**

- Consumes: FSD fields `general.update_timestamp`, pilot `position_time`, controller/ATIS `send_time`, `cid`, `callsign`, `frequency`, `latitude`, `longitude`.
- Consumes: Mumble users/channels, raw UserState deltas, callbacks, protobuf VoiceTarget, and SoundOutput.
- Produces: shared feed acquire/release, pure nearby-session selection, and `ATISRouter.attach(mumble)`, `enqueue(pcm)`, `disable()`, `close()`; adapt additional private signatures to existing component conventions.
- Produces: desktop `atis_range_nm` and explicit daemon radius/feed configuration.

- [x] **Step 1: Write failing selection tests.** Use complete synthetic feed fixtures and literal expected session IDs. Cover two stations on the same frequency, exact station overrides, range boundaries, different-frequency users, listener membership, malformed/expired/future data, missing pilot `position_time`, multiple FSD roles, duplicate stations, and voice-only receivers.

```python
# Independent fixture expectation: nearby Tokyo session only, not Osaka or an observer without FSD.
self.assertEqual(selected_sessions, [11])
self.assertEqual(selected_after_feed_expiry, [])
self.assertEqual(selected_after_missing_position_time, [])
```

- [x] **Step 2: Write failing sender and relay tests.** Exercise a real routing gate with only network/codec boundaries doubled. Preserve raw protobuf behavior and the library's pop-encode-target order. Include deterministic thread Events for an already-popped-frame race.

```python
self.assertEqual(sent_audio_target_ids, [2])
self.assertEqual(sent_audio_after_empty_route, [])
self.assertEqual(sent_audio_after_output_replacement_without_sync, [])
self.assertEqual(atIS_cross_coupled_audio, [])
self.assertEqual(ordinary_cross_coupled_audio, [ordinary_pcm])
```

- [x] **Step 3: Run the new tests before implementation.** Confirm behavioral failures, not missing dependency or localhost-permission failures. Use the project-local Python environment. Save bounded red evidence in the report.

```sh
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen ../.temp/geographic-env/bin/python -m unittest test_atisrouting -q
```

- [x] **Step 4: Implement the minimal shared feed reader and routing selector.** Parse only routing fields. Use one reference-counted reader per URL, bounded HTTP reads/timeouts, immediate invalidation on failed/malformed fetch, and send-time expiry rechecks. Count all matching numeric FSD roles before eligibility checks. Require pilot coordinate freshness.

```python
# Pilot selection must not fall back to generic activity freshness.
position_timestamp = pilot.get("position_time")
# Controller and ATIS position reports update coordinates and send_time together.
position_timestamp = controller_or_station.get("send_time")
```

- [x] **Step 5: Implement raw Mumble membership tracking and transmission gating.** Observe raw UserState before lossy pymumble delta handling. Reset state on connection initialization and removals. Wrap every replacement SoundOutput before use. Gate sender, enqueue, and disable with one lock. Register targets only from the sender thread, before tunneled audio. Drop buffered data when route generations differ.

```python
target = mumble_pb2.VoiceTarget()
target.id = 2
for session_id in recipient_sessions:
    target.targets.add().session.append(session_id)
mumble.send_message(PYMUMBLE_MSG_TYPES_VOICETARGET, target)
sound_output.target = 2
```

- [x] **Step 6: Wire both broadcasters and configuration.** Replace direct PCM enqueue with the gate. Pass exact station identity, current feed and validated radius. Geographic quiet detection drains remote audio and ignores distant/unknown senders. Stop shared feed readers on final release and failed starts. Restart cycles after route interruptions. Preserve existing reconnect limits and per-station shutdown behavior.

- [x] **Step 7: Disable controller ATIS cross-coupling only.** Recognize anchored legacy and callsign-qualified ATIS usernames. Keep playback and RX indication unchanged. Keep ordinary voice relay unchanged.

```python
if not is_atis_sender(user_name):
    self._forward_cross_couple(khz, soundchunk)
```

- [x] **Step 8: Run green tests and component suites.** Verify helper-copy parity, xpc/msfs voice parity, affected desktop/daemon/controller/server tests, Python syntax, and diff whitespace. Use project-local `tempfile.tempdir`; request command escalation for local fake-server sockets when required. Do not count fixture failures or missing dependencies as production regressions.

```sh
git diff --check
cmp atis/atisrouting.py server/ATIS/atisrouting.py
cmp xpc/voice.py msfs/voice.py
```

- [x] **Step 9: Document exact configuration and deployment ordering.** State observer behavior, the FSD freshness-field prerequisite, controller relay update prerequisite, and inherited Whisper ACL requirements. Write the implementation report to `.temp/geographic-atis/task-1-report.md` with red/green evidence, actual commands/results, changed files, and unresolved integration constraints. No commits.

## Independent review and final verification

- [x] Review the complete geographic diff against the approved spec, including real pymumble sender/membership semantics.
- [x] Resolve important findings through implementer follow-up and scoped re-review.
- [x] Independently rerun targeted regressions and affected suites.
- [x] Verify additive FSD coordinate timestamp tests and compatibility with geographic selection.
- [x] Remove only task-owned scratch/environment/cache artifacts after verification. Preserve pre-existing worktrees and scratch.

## Final verification — 2026-10-09

Independent review approved the routing implementation and the real pymumble startup fix.
Real startup/reconnect regressions: 2 passed independently by the coordinator and reviewer.
Desktop discovery: 322 passed. Daemon discovery: 49 passed. Voice-server discovery: 71 passed.
Controller discovery: 260 passed; 1 skipped because the native RNNoise library was unavailable.
METAR station handler regression passed. Controller URL fixtures: 2 passed.
FSD authoritative coordinate timestamp regressions passed with the Go race detector.
Python syntax checks covered 16 files. Helper parity, pilot voice parity, and whitespace checks passed.
Loopback fixtures required elevated local socket permission.
Native audio hardware, live Murmur ACLs, and EuroScope remain release checks.
No commits, pushes, deployment, or issue changes occurred.
