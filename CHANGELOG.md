# Changelog

## v0.1.3 - 2026-07-14

### Changed

- Discussion coordination markers use Discord spoiler formatting so protocol details are hidden unless a user clicks the spoiler.
- Plain `v0.1.2` markers remain valid for rollback compatibility.
- Partially wrapped and malformed spoiler markers fail closed.

### Validated

- 27 protocol tests, including marker compatibility, lifecycle-noise, and consecutive-session regressions.

### Known Limitations

- Discord displays a small spoiler block at the start of each contribution.
- Production remains capped at two turns pending longer-session validation.

## v0.1.2 - 2026-07-14

First stable canary release.

### Added

- Controlled Discord discussions started by approved humans with `discuss:`.
- Deterministic participant ordering, bounded turns, timeout, stop, and deduplication.
- Session-and-turn markers that reject stale, malformed, and unmarked bot messages.
- Tests for authorization, lifecycle-message noise, duplicate delivery, and consecutive sessions.

### Fixed

- Hermes lifecycle and interruption messages no longer create bot feedback loops.
- The final participant closes its local session immediately, allowing another discussion to start without waiting for timeout.

### Validated

- 25 protocol tests, including a 400-seed adversarial determinism test.
- Live two-profile Hermes deployment using OliBot and Coll.
- Two-turn canary: one contribution from each bot.

### Known Limitations

- Coordination markers are visible in Discord contributions.
- Production remains capped at two turns pending longer-session validation.
- Hermes lifecycle notices may remain visible, but they cannot advance the discussion protocol.
