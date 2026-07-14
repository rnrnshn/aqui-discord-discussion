# aqui-discord-discussion

A Hermes plugin that adds **controlled, visible bot-to-bot discussion sessions**
on top of Hermes' shipped Discord platform adapter (`discord-platform`).

An approved human posts a trigger in a private channel:

```text
discuss: What is the safest driver onboarding plan for next week?
```

and the configured Hermes bots contribute automatically, in a deterministic
order, one turn each, stopping on the turn limit, a timeout, or `stop discussion`.
The final-turn author closes its local session immediately, so a new
`discuss:` session can start without waiting for the previous timeout.

## How it works (and what it is not)

- It does **not** fork or replace the shipped Discord adapter. It registers a
  single `pre_gateway_dispatch` hook that acts as a session/turn gate: for each
  inbound Discord message it decides `skip` (drop), `rewrite` (inject bounded
  discussion context and let this bot contribute), or pass-through (normal
  behavior, untouched).
- Each bot keeps its own Discord token, its own gateway connection via the
  adapter, and its own full Hermes brain (memory/tools/persona). There is no
  central service and no shared database — turn coordination is deterministic
  local in-memory state (see "Determinism" below).
- Every real contribution starts with a compact `[[AQD:<session>:<turn>]]`
  marker. Only a correctly marked contribution for the active session and
  expected turn advances the protocol. Hermes lifecycle notices, busy messages,
  compression warnings, and stale responses are dropped before agent dispatch.
- It does **not** touch `aqui-discord-mcp` (`dmcp`), send DMs, or expose admin
  operations.

## Requirements

- A Hermes install with the bundled `discord-platform` adapter (one Hermes
  profile per Discord bot identity).
- The plugin API and Discord event metadata were validated against Hermes
  revision `44ddc552f5e054759a6970af8997ea588a9d81c9`.
- `DISCORD_ALLOW_BOTS=all` on participating profiles, so the adapter forwards
  other bots' messages into the pipeline where this hook can gate them. The hook
  — not the global flag — restricts bot participation to active sessions in
  approved channels.
- Every discussion channel must also be listed in
  `DISCORD_FREE_RESPONSE_CHANNELS`. Hermes applies its mention gate before
  `pre_gateway_dispatch`, so unmentioned triggers and subsequent bot turns do
  not reach the plugin without this adapter exemption.

## Configuration (per profile)

Real values live in each profile's local secret configuration and must never be
committed. Discussion mode stays **disabled** (and normal behavior is unaffected)
until all required values are present and valid.

Relies on existing adapter config: `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USERS`,
`DISCORD_ALLOWED_CHANNELS`, `DISCORD_ALLOW_BOTS=all`, and
`DISCORD_FREE_RESPONSE_CHANNELS` containing every discussion channel.
Every discussion starter must also be present in `DISCORD_ALLOWED_USERS` (or
authorized by an equivalent Discord adapter policy), because adapter intake
authorization runs before the plugin hook.

Discussion-specific variables:

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DISCORD_DISCUSSION_SELF_BOT_ID` | yes | — | This profile's own Discord bot user ID (used to detect its turn). |
| `DISCORD_DISCUSSION_PARTICIPANT_BOT_IDS` | yes | — | Ordered, distinct, comma-separated participant bot user IDs. |
| `DISCORD_DISCUSSION_CHANNELS` | yes | — | Comma-separated channel IDs allowed to host sessions. |
| `DISCORD_DISCUSSION_ALLOWED_STARTERS` | yes | — | Comma-separated human user IDs allowed to start/stop. |
| `DISCORD_DISCUSSION_TRIGGER_PREFIX` | no | `discuss:` | Text prefix that starts a session. |
| `DISCORD_DISCUSSION_STOP_PHRASE` | no | `stop discussion` | Text that stops a session. |
| `DISCORD_DISCUSSION_MAX_TURNS` | no | `4` | Total bot messages per session (capped at 20). |
| `DISCORD_DISCUSSION_SESSION_TIMEOUT_SECONDS` | no | `300` | Session duration (clamped to 30..1800). |

Starters must also satisfy the adapter's existing authorization
(`DISCORD_ALLOWED_USERS`), and discussion channels must also be permitted by
`DISCORD_ALLOWED_CHANNELS`.

To find IDs: enable Discord Developer Mode (User Settings → Advanced), then
right-click a user/bot/channel → "Copy ID".

## Install (per participating profile)

```bash
# 1. Install the plugin into this profile's Hermes home
hermes plugins install https://github.com/rnrnshn/aqui-discord-discussion

# 2. Enable it (user plugins are opt-in) in this profile's config.yaml:
#    plugins:
#      enabled:
#        - aqui-discord-discussion

# 3. Set DISCORD_ALLOW_BOTS=all, DISCORD_FREE_RESPONSE_CHANNELS, and the
#    DISCORD_DISCUSSION_* config for this profile.

# 4. Restart this Hermes profile.
```

Adding another bot later is configuration only: run its Hermes profile with its
own token, install + enable the plugin, set the IDs, restart that profile. No
code change and no `dmcp` restart.

Pin the plugin version in the shared repo and roll installs forward with
`hermes plugins update` to avoid version drift across profiles.

## Determinism (why no Redis)

Every profile runs its own engine. The turn index is the size of the deduped set
of observed participant messages — including this profile's own contributions,
counted immediately when it decides to reply. A bot posts slot `k` only if
`participant_bot_ids[k % n]` is its own ID, guarded so each slot is posted once.
Set size is order-independent, duplicate message IDs don't grow the set (bounded
per-engine dedup, on top of the adapter's own dedup), and participant IDs are
distinct — so reordering, duplication, or delay can only *delay* a turn (safe
stall → timeout), never make two bots claim the same slot. Validated by a
400-seed adversarial fuzz (`tests/`).

## Tests

```bash
python3 tests/test_discussion.py     # or: pytest tests/
```

Covers config validation, every authorization/loop-protection rule, and the
multi-engine determinism fuzz. It also reproduces Hermes lifecycle-message noise
and verifies that unmarked, stale-session, and wrong-turn bot messages cannot
advance or trigger a discussion.

## Operations

- **Token rotation:** rotate the bot token in the profile's secret config, then
  restart only that Hermes profile. Tokens are never read or logged by this
  plugin.
- **Rollback / disable:** remove `aqui-discord-discussion` from `plugins.enabled`
  (or set `DISCORD_ALLOW_BOTS=none`) and restart the profile. Behavior reverts to
  normal owner-bound chat. `hermes plugins uninstall aqui-discord-discussion`
  removes it entirely.
- **Sessions are in-memory:** restarting a profile ends its active sessions by
  design (no unsafe recovery).

## Troubleshooting

- *Nothing happens on `discuss:`* — discussion mode is likely disabled: check
  that all required `DISCORD_DISCUSSION_*` values are set, participant IDs are
  distinct, and the plugin is listed in `plugins.enabled`. Startup logs one line
  saying enabled or disabled. Also confirm the starter is admitted by
  `DISCORD_ALLOWED_USERS`; the plugin cannot override adapter authorization.
- *Bots ignore each other* — set `DISCORD_ALLOW_BOTS=all` and confirm the channel
  is in `DISCORD_ALLOWED_CHANNELS`, `DISCORD_FREE_RESPONSE_CHANNELS`, and
  `DISCORD_DISCUSSION_CHANNELS`.
- *Only one bot ever replies per turn* — that is correct; exactly one participant
  owns each turn.
- *A bot replies out of turn or twice* — verify `DISCORD_DISCUSSION_SELF_BOT_ID`
  is unique per profile and the participant list is identical (and identically
  ordered) across all profiles.

## Security & privacy

- Never logs Discord bot tokens, authorization headers, or message bodies.
- The visible Discord transcript is the discussion audit trail.
- No DMs, no admin operations, no reading of human-to-human private messages.
- Public channels should not be configured for the initial deployment.

## Status / scope

- MVP: discussion sessions only. Bot-owned DM handling is a separate, later
  phase and is deliberately out of scope here.
- Version `0.1.2` is validated on a live two-profile Hermes deployment with a
  two-turn canary (one contribution per bot). Longer sessions remain disabled
  pending additional live testing.
