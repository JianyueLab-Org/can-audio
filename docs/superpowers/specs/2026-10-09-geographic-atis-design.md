# Geographic ATIS routing

## Approved scope

ATIS transmission is geographically restricted. Ordinary radio transmission is unchanged.
The default radius is 100 nautical miles and is configurable.
Unknown, stale, malformed, and ambiguous positions receive no ATIS.
A voice-only observer needs a positioned FSD session to receive ATIS.
Sector #2 and can-db #65 are outside this change.

## Position source

Use the existing FSD `/v1/data.json` feed.
Poll every 5 seconds through one shared feed reader per URL per process.
Requests have a bounded timeout and retain the existing browser-compatible User-Agent.
Invalidate a malformed or failed response. Never switch to global broadcasting.
Keep only the current minimal routing snapshot in memory. Do not persist or log positions.

`general.update_timestamp` must be no older than 20 seconds.
An individual position must be no older than 90 seconds.
Allow at most 5 seconds of future clock skew. Recheck expiry at transmission time.
Coordinates must be finite and within latitude/longitude bounds. Zero is a valid coordinate.
Missing coordinates are unknown, even when a timestamp is recent.

Pilot coordinates use the new additive `pilots[].position_time` timestamp.
That timestamp records the last authoritative coordinate update and is not renewed by fast velocity packets.
Pilot entries without `position_time` are ineligible.
Controller and ATIS coordinates use their existing `send_time` timestamps.

## Station and receiver identity

Bind each broadcaster to one exact callsign and frequency in `atis[]`.
Use that live entry's coordinates, including station overrides.
Missing, duplicate, stale, or frequency-mismatched station entries disable transmission.

Numeric Mumble names resolve to exactly one entry across `pilots[]` and `controllers[]`.
More than one entry is ambiguous, including an invalid or stale duplicate.
Positioned FSD observers are eligible. `atis[]` does not add numeric-CID ambiguity.
Nearby station-qualified ATIS users are also eligible quiet listeners.
Legacy frequency-only ATIS names do not provide an unambiguous station binding.

Recipients must be within the configured radius and joined to, or listening to, the station frequency channel.
Exclude the broadcaster's own session. Do not target other-frequency users.
Track raw Mumble UserState listener additions/removals as cumulative sets.
Clear listener state on reconnect, user removal, and channel deletion.

## Transmission gate

Send only explicit-session VoiceTarget whispers with nonzero target ID 2.
An empty recipient set clears that target and discards audio.
Never call `remove_whisper()` or rely on `set_whisper([])`.
Never enqueue ordinary channel-target audio.

Wrap every new SoundOutput sender with a routing gate before it can send audio.
The gate serializes enqueue, disable, route changes, and the actual send/encode operation.
Register VoiceTarget changes and send tunneled audio on the Mumble thread.
Clear buffered PCM before changing recipients or disabling a route.
Reconnect resets membership and recipients and wraps the replacement SoundOutput.
Queued tails do not carry into a new route or connection.
Restart an interrupted broadcast cycle after routing changes.

Quiet detection considers only fresh, nearby users on the same frequency.
Drain received sound even when it is ignored for quiet detection.
Distant stations do not serialize one another's broadcast cycles.

## Components

Keep flat per-component imports.
`atis/atisrouting.py` and `server/ATIS/atisrouting.py` are byte-identical copies.
The desktop and daemon broadcasters use the same routing gate and selection logic.
The desktop passes its configured datafeed URL and radius.
The daemon shares a feed reader across its station fleet.
The controller plays received ATIS normally but never cross-couples ATIS into other channels.
The xpc/msfs voice copies remain byte-identical and need no receive-side filtering.

## Configuration and rollout

Desktop settings persist `atis_range_nm`, default 100.
Daemon configuration provides the same radius and feed URL.
Reject nonfinite or nonpositive radii without enabling global transmission.
Document the exact configuration keys and environment variables implemented.

Deploy the authenticator identity fix and the FSD `position_time` field before updated broadcasters.
Update controller clients before relying on geographic isolation with cross-coupling enabled.
Inherited Mumble Whisper permission must cover root and frequency channels.
No production configuration, deployment, credentials, or issue state is changed by this task.

## Verification

Use synthetic feeds and Mumble sessions, never production personal data.
Cover distance boundaries, separate same-frequency airports, overrides, different frequencies, missing/stale/future/invalid data, and duplicate CID/callsign entries.
Cover listener add/remove/add, session reuse, channel deletion, empty targets, reconnect, output replacement, buffered tails, and sender/enqueue races.
Verify nearby peer yielding and distant audio rejection in both broadcasters.
Verify controller ATIS playback remains enabled while ATIS cross-coupling is disabled and ordinary voice cross-coupling remains enabled.
Verify parity of the two helper copies and unchanged pilot voice-copy parity.
Run affected component suites, syntax checks, and whitespace checks.
Live EuroScope/Murmur integration remains an explicit release check when those applications are unavailable locally.
