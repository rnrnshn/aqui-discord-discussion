"""Tests for the AQUI Discord discussion protocol.

Two layers:

1. Unit tests exercising every authorization and loop-protection rule directly
   against ``DiscussionEngine.decide`` and ``DiscussionConfig.from_env``.
2. A multi-engine adversarial fuzz (ported from the validated PoC) that runs N
   independent engines against a shared gateway with reorder/duplicate/jitter
   and asserts exactly one bot authors each turn slot — the determinism claim
   that lets us avoid Redis.

Runnable with pytest, or directly: ``python3 tests/test_discussion.py``.
"""

from __future__ import annotations

import heapq
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session import (  # noqa: E402
    DEFAULT_MAX_TURNS, MAX_TURNS_CAP, MAX_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS,
    DiscussionConfig, DiscussionEngine, format_turn_marker,
)

CH = "chan-discuss"
OTHER = "chan-other"
STARTER = "user-starter"
BOTS = ["bot-a", "bot-b", "bot-c"]


def make_config(self_bot_id, participants, *, max_turns=4, timeout=300,
                channels=(CH,), starters=(STARTER,)):
    return DiscussionConfig(
        self_bot_id=self_bot_id,
        participant_bot_ids=list(participants),
        discussion_channels=frozenset(channels),
        allowed_starters=frozenset(starters),
        max_turns=max_turns,
        timeout_seconds=timeout,
        enabled=True,
    )


def feed(engine, *, author, is_bot, text, now, mid="m", name="", channel=CH):
    return engine.decide(channel_id=channel, author_id=author, author_name=name,
                          is_bot=is_bot, message_id=mid, text=text, now=now)


def marked(engine, index, text):
    session = engine._sessions[CH]
    return f"{format_turn_marker(session.session_id, index)}\n{text}"


# ── config loading ───────────────────────────────────────────────────────────

def test_from_env_disabled_when_incomplete():
    assert DiscussionConfig.from_env({}).enabled is False
    # Missing starters → disabled.
    env = {
        "DISCORD_DISCUSSION_SELF_BOT_ID": "b0",
        "DISCORD_DISCUSSION_PARTICIPANT_BOT_IDS": "b0,b1",
        "DISCORD_DISCUSSION_CHANNELS": "c1",
    }
    assert DiscussionConfig.from_env(env).enabled is False


def test_from_env_enabled_and_clamped():
    env = {
        "DISCORD_DISCUSSION_SELF_BOT_ID": "b0",
        "DISCORD_DISCUSSION_PARTICIPANT_BOT_IDS": "b0, b1 ,b2",
        "DISCORD_DISCUSSION_CHANNELS": "c1,c2",
        "DISCORD_DISCUSSION_ALLOWED_STARTERS": "u1",
        "DISCORD_DISCUSSION_MAX_TURNS": "9999",
        "DISCORD_DISCUSSION_SESSION_TIMEOUT_SECONDS": "1",
    }
    cfg = DiscussionConfig.from_env(env)
    assert cfg.enabled is True
    assert cfg.participant_bot_ids == ["b0", "b1", "b2"]
    assert cfg.max_turns == MAX_TURNS_CAP           # clamped down
    assert cfg.timeout_seconds == MIN_TIMEOUT_SECONDS  # clamped up
    assert cfg.discussion_channels == frozenset({"c1", "c2"})


def test_from_env_disabled_on_duplicate_participants():
    env = {
        "DISCORD_DISCUSSION_SELF_BOT_ID": "b0",
        "DISCORD_DISCUSSION_PARTICIPANT_BOT_IDS": "b0,b0",
        "DISCORD_DISCUSSION_CHANNELS": "c1",
        "DISCORD_DISCUSSION_ALLOWED_STARTERS": "u1",
    }
    assert DiscussionConfig.from_env(env).enabled is False


def test_bad_integers_fall_back_to_defaults():
    env = {
        "DISCORD_DISCUSSION_SELF_BOT_ID": "b0",
        "DISCORD_DISCUSSION_PARTICIPANT_BOT_IDS": "b0,b1",
        "DISCORD_DISCUSSION_CHANNELS": "c1",
        "DISCORD_DISCUSSION_ALLOWED_STARTERS": "u1",
        "DISCORD_DISCUSSION_MAX_TURNS": "not-a-number",
    }
    assert DiscussionConfig.from_env(env).max_turns == DEFAULT_MAX_TURNS


# ── disabled mode is transparent ─────────────────────────────────────────────

def test_disabled_engine_passes_everything():
    engine = DiscussionEngine(DiscussionConfig(
        self_bot_id="b0", participant_bot_ids=[], discussion_channels=frozenset(),
        allowed_starters=frozenset(), enabled=False))
    for is_bot in (True, False):
        d = feed(engine, author="whoever", is_bot=is_bot, text="discuss: x", now=0)
        assert d.action == "pass"


# ── normal mode (no active session) ──────────────────────────────────────────

def test_self_message_skipped():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"]))
    d = feed(engine, author="bot-a", is_bot=True, text="hi", now=0)
    assert d.action == "skip"


def test_bot_outside_session_skipped():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"]))
    d = feed(engine, author="bot-b", is_bot=True, text="hello", now=0)
    assert d.action == "skip"


def test_normal_human_passes_through():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"]))
    d = feed(engine, author="some-owner", is_bot=False, text="hey", now=0)
    assert d.action == "pass"


def test_unauthorized_starter_trigger_not_started():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"]))
    d = feed(engine, author="random-human", is_bot=False, text="discuss: sneaky", now=0)
    assert d.action == "pass"  # falls through to normal handling; no session
    assert engine._sessions.get(CH) is None


def test_trigger_in_wrong_channel_not_started():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"]))
    d = feed(engine, author=STARTER, is_bot=False, text="discuss: nope", now=0, channel=OTHER)
    assert d.action == "pass"
    assert engine._sessions.get(OTHER) is None


def test_bot_trigger_rejected():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"]))
    d = feed(engine, author="bot-b", is_bot=True, text="discuss: sneaky", now=0)
    assert d.action == "skip"
    assert engine._sessions.get(CH) is None


# ── starting a session + first turn ownership ────────────────────────────────

def test_valid_trigger_starts_and_first_participant_takes_turn():
    first = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"], max_turns=2))
    second = DiscussionEngine(make_config("bot-b", ["bot-a", "bot-b"], max_turns=2))
    text = "discuss: onboarding plan"
    d1 = feed(first, author=STARTER, is_bot=False, text=text, now=0, mid="start")
    d2 = feed(second, author=STARTER, is_bot=False, text=text, now=0, mid="start")
    assert d1.action == "rewrite"          # bot-a is participant[0] → its turn
    assert "onboarding plan" in d1.text
    assert "Turn: 1 of 2" in d1.text
    assert format_turn_marker(first._sessions[CH].session_id, 0) in d1.text
    assert d2.action == "skip"             # bot-b waits


# ── active session turn gating ───────────────────────────────────────────────

def test_non_participant_bot_ignored_mid_session():
    engine = DiscussionEngine(make_config("bot-b", ["bot-a", "bot-b"]))
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    d = feed(engine, author="outsider-bot", is_bot=True, text="butting in", now=1, mid="x")
    assert d.action == "skip"


def test_human_ignored_mid_session():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"]))
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    d = feed(engine, author="lurker", is_bot=False, text="hi", now=1, mid="h")
    assert d.action == "skip"


def test_expected_turn_only():
    # bot-b should only reply when it's its turn (after bot-a's contribution).
    engine = DiscussionEngine(make_config("bot-b", ["bot-a", "bot-b"], max_turns=2))
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    # bot-a posts turn 1 → now it's bot-b's turn.
    d = feed(engine, author="bot-a", is_bot=True,
             text=marked(engine, 0, "a1"), now=1, mid="a1")
    assert d.action == "rewrite"
    assert "Turn: 2 of 2" in d.text


def test_max_turns_ends_session():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"], max_turns=2))
    # start → bot-a turn1 (index0, self:0). observe bot-b turn2 (index1).
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    feed(engine, author="bot-b", is_bot=True,
         text=marked(engine, 1, "b1"), now=1, mid="b1")
    # index is now 2 == max_turns → any further participant msg ends it.
    d = feed(engine, author="bot-b", is_bot=True,
             text=marked(engine, 2, "b2"), now=2, mid="b2")
    assert d.action == "skip"
    assert engine._sessions[CH].active is False


def test_timeout_ends_session():
    engine = DiscussionEngine(make_config("bot-b", ["bot-a", "bot-b"], timeout=100))
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    d = feed(engine, author="bot-a", is_bot=True, text="a1", now=1000, mid="a1")
    assert d.action == "skip"
    assert engine._sessions[CH].active is False


def test_stop_phrase_ends_session():
    engine = DiscussionEngine(make_config("bot-b", ["bot-a", "bot-b"]))
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    d = feed(engine, author=STARTER, is_bot=False, text="stop discussion", now=1, mid="stp")
    assert d.action == "skip"
    assert engine._sessions[CH].active is False
    # After stop, a participant bot message is treated as "outside session".
    d2 = feed(engine, author="bot-a", is_bot=True, text="a1", now=2, mid="a1")
    assert d2.action == "skip"


def test_unauthorized_stop_ignored():
    engine = DiscussionEngine(make_config("bot-a", ["bot-a", "bot-b"]))
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    # A bot cannot stop the session.
    feed(engine, author="bot-b", is_bot=True, text="stop discussion", now=1, mid="x")
    assert engine._sessions[CH].active is True


def test_unmarked_participant_status_does_not_advance_turn():
    engine = DiscussionEngine(make_config("bot-b", ["bot-a", "bot-b"]))
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    for i, status in enumerate((
            "Interrupting current task.",
            "Operation interrupted: waiting for model response.",
            "Codex context auto-compaction changed.",
    )):
        d = feed(engine, author="bot-a", is_bot=True, text=status,
                 now=i + 1, mid=f"noise-{i}")
        assert d.action == "skip"
        assert d.reason == "unmarked participant message"
    assert not engine._sessions[CH].seen_participant_msg_ids


def test_wrong_session_or_turn_marker_does_not_advance():
    engine = DiscussionEngine(make_config("bot-b", ["bot-a", "bot-b"]))
    feed(engine, author=STARTER, is_bot=False, text="discuss: t", now=0, mid="s")
    wrong_session = feed(
        engine, author="bot-a", is_bot=True,
        text=f"{format_turn_marker('0' * 12, 0)}\nhello", now=1, mid="wrong-session")
    wrong_turn = feed(
        engine, author="bot-a", is_bot=True,
        text=marked(engine, 3, "hello"), now=2, mid="wrong-turn")
    assert wrong_session.reason == "wrong discussion session"
    assert wrong_turn.reason == "wrong discussion turn"
    assert not engine._sessions[CH].seen_participant_msg_ids


# ── multi-engine adversarial determinism fuzz (ported from the PoC) ──────────

class _Msg:
    __slots__ = ("id", "author", "is_bot", "ts", "text")

    def __init__(self, mid, author, is_bot, ts, text=""):
        self.id, self.author, self.is_bot, self.ts, self.text = mid, author, is_bot, ts, text


class _Gateway:
    def __init__(self, rng, dup_prob, max_jitter):
        self.rng, self.dup_prob, self.max_jitter = rng, dup_prob, max_jitter
        self.engines = []          # (self_bot_id, engine)
        self.next_id = 1
        self.seq = 0
        self.pq = []
        self.contributions = []    # bot messages in post order (ground truth)

    def post(self, msg):
        if msg.is_bot:
            self.contributions.append(msg)
        for _, engine in self.engines:
            self._schedule(msg, engine)
            if self.rng.random() < self.dup_prob:
                self._schedule(msg, engine)  # RESUME-style duplicate delivery

    def _schedule(self, msg, engine):
        self.seq += 1
        jitter = self.rng.random() * self.max_jitter
        heapq.heappush(self.pq, (msg.ts + jitter, self.seq, id(engine), engine, msg))

    def inject_human(self, author, text, ts):
        m = _Msg(self.next_id, author, False, ts)
        self.next_id += 1
        # Human messages don't ride the _Msg text; deliver text via closure.
        self._human_text = getattr(self, "_human_text", {})
        self._human_text[m.id] = text
        self.post(m)

    def run(self):
        while self.pq:
            deliver_ts, _, _, engine, msg = heapq.heappop(self.pq)
            self._deliver(engine, msg, deliver_ts)

    def _deliver(self, engine, msg, now):
        self_id = next(sid for sid, e in self.engines if e is engine)
        text = "discuss: fuzz topic" if not msg.is_bot else msg.text
        text = getattr(self, "_human_text", {}).get(msg.id, text)
        d = engine.decide(channel_id=CH, author_id=msg.author, author_name=msg.author,
                          is_bot=msg.is_bot, message_id=str(msg.id), text=text, now=now)
        if d.action == "rewrite":
            # This engine took its turn → it posts a contribution as itself.
            session = engine._sessions[CH]
            index = len(session.seen_participant_msg_ids) - 1
            out = _Msg(
                self.next_id, self_id, True, now,
                f"{format_turn_marker(session.session_id, index)}\ncontribution")
            self.next_id += 1
            self.post(out)


def _run_fuzz_case(seed):
    rng = random.Random(seed)
    n = rng.choice([2, 3])
    max_turns = rng.choice([2, 3, 4, 6])
    participants = BOTS[:n]
    gw = _Gateway(rng, dup_prob=rng.choice([0.0, 0.3, 0.8]),
                  max_jitter=rng.choice([0.5, 2.0, 5.0]))
    for b in participants:
        gw.engines.append((b, DiscussionEngine(
            make_config(b, participants, max_turns=max_turns, timeout=10_000))))
    gw.inject_human(STARTER, "discuss: fuzz topic", ts=0.0)
    gw.run()

    contributions = gw.contributions
    assert len(contributions) <= max_turns, f"seed {seed}: {len(contributions)} > {max_turns}"
    for i, c in enumerate(contributions):
        expected = participants[i % n]
        assert c.author == expected, (
            f"seed {seed}: slot {i} authored by {c.author}, expected {expected}")
    return len(contributions), max_turns


def test_fuzz_determinism_400_seeds():
    for seed in range(400):
        _run_fuzz_case(20_000 + seed)


def test_happy_path_completes_all_turns_in_order():
    # No jitter/dup: a clean 2-bot, 4-turn discussion runs to completion.
    rng = random.Random(1)
    gw = _Gateway(rng, dup_prob=0.0, max_jitter=0.0)
    for b in BOTS[:2]:
        gw.engines.append((b, DiscussionEngine(
            make_config(b, BOTS[:2], max_turns=4, timeout=10_000))))
    gw.inject_human(STARTER, "discuss: driver onboarding", ts=0.0)
    gw.run()
    assert [c.author for c in gw.contributions] == ["bot-a", "bot-b", "bot-a", "bot-b"]


def test_duplicate_delivery_no_double_contribution():
    rng = random.Random(7)
    gw = _Gateway(rng, dup_prob=0.95, max_jitter=0.0)
    for b in BOTS[:2]:
        gw.engines.append((b, DiscussionEngine(
            make_config(b, BOTS[:2], max_turns=4, timeout=10_000))))
    gw.inject_human(STARTER, "discuss: dedup me", ts=0.0)
    gw.run()
    assert [c.author for c in gw.contributions] == ["bot-a", "bot-b", "bot-a", "bot-b"]


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    _run_all()
