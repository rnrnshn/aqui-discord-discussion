"""Bridge between Hermes' ``pre_gateway_dispatch`` hook and the pure engine.

The hook fires once per inbound ``MessageEvent`` (before auth/dispatch) and may
return one of::

    {"action": "skip",    "reason": "..."}   -> drop the message
    {"action": "rewrite", "text":  "..."}    -> replace event.text, then dispatch
    None                                       -> normal dispatch (untouched)

We translate the event into a :meth:`DiscussionEngine.decide` call and map the
resulting :class:`Decision` back onto that contract. Only Discord events are
considered; every other platform passes straight through.

Nothing here logs message bodies or tokens (Hard Safety Rule 13): only
metadata-level reasons and non-sensitive ids at debug level.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from .session import DiscussionEngine, SessionStatus

logger = logging.getLogger("aqui_discord_discussion")


def _event_epoch(event: Any) -> float:
    ts = getattr(event, "timestamp", None)
    try:
        return ts.timestamp() if ts is not None else time.time()
    except Exception:
        return time.time()


def _is_discord(event: Any) -> bool:
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    return platform == "discord"


def _render_status(status: SessionStatus) -> str:
    if status.active:
        return (
            f"Discussion active: session `{status.session_id}`; "
            f"turns {status.turn_count}/{status.max_turns}; "
            f"next participant slot {status.next_participant_slot}; "
            f"expires in {status.expires_in_seconds}s."
        )
    if status.session_id:
        return (
            f"No active discussion. Last session `{status.session_id}` ended at "
            f"{status.turn_count}/{status.max_turns} turns."
        )
    return "No active discussion in this channel."


def _log_event(*, action: str, reason: str, channel_id: str,
               author_kind: str, status: SessionStatus) -> None:
    logger.info(
        "discussion_event action=%s reason=%s channel=%s session=%s "
        "active=%s turns=%d/%d next_slot=%s expires_in=%d author_kind=%s",
        action, reason, channel_id, status.session_id or "none", status.active,
        status.turn_count, status.max_turns, status.next_participant_slot or "none",
        status.expires_in_seconds, author_kind,
    )


def _schedule_status_reply(gateway: Any, source: Any, content: str) -> bool:
    adapter = getattr(gateway, "adapters", {}).get(source.platform) if gateway else None
    if adapter is None:
        logger.warning("discussion status reply skipped: Discord adapter unavailable")
        return False
    metadata = {"thread_id": source.thread_id} if getattr(source, "thread_id", None) else None

    async def send() -> None:
        try:
            result = await adapter.send(
                source.chat_id,
                content,
                reply_to=getattr(source, "message_id", None),
                metadata=metadata,
            )
            if not getattr(result, "success", False):
                logger.warning("discussion status reply failed")
        except Exception:
            logger.warning("discussion status reply failed", exc_info=True)

    try:
        asyncio.get_running_loop().create_task(send())
    except RuntimeError:
        logger.warning("discussion status reply skipped: no running event loop")
        return False
    return True


def make_pre_gateway_dispatch_hook(engine: DiscussionEngine) -> Callable[..., Optional[dict]]:
    """Return a synchronous ``pre_gateway_dispatch`` callback bound to *engine*."""

    def hook(event: Any = None, gateway: Any = None,
             session_store: Any = None, **_kwargs: Any) -> Optional[dict]:
        if event is None or not _is_discord(event):
            return None
        source = event.source
        now = _event_epoch(event)
        is_bot = bool(getattr(source, "is_bot", False))
        channel_id = source.chat_id
        author_id = source.user_id or ""
        text = getattr(event, "text", "") or ""
        cfg = engine.config

        if (
            text.strip().lower() == cfg.status_phrase.lower()
            and not is_bot
            and channel_id in cfg.discussion_channels
            and author_id in cfg.allowed_starters
        ):
            status = engine.status(channel_id, now)
            is_primary = bool(
                cfg.participant_bot_ids
                and cfg.self_bot_id == cfg.participant_bot_ids[0]
            )
            if is_primary:
                _schedule_status_reply(gateway, source, _render_status(status))
            _log_event(
                action="status",
                reason="status command handled" if is_primary else "status command peer skipped",
                channel_id=channel_id,
                author_kind="human",
                status=status,
            )
            return {"action": "skip", "reason": "discussion status handled"}

        decision = engine.decide(
            channel_id=channel_id,
            author_id=author_id,
            author_name=source.user_name or "",
            is_bot=is_bot,
            message_id=(source.message_id or getattr(event, "message_id", None) or ""),
            text=text,
            now=now,
        )

        if decision.action == "skip":
            _log_event(
                action="skip", reason=decision.reason, channel_id=channel_id,
                author_kind="bot" if is_bot else "human",
                status=engine.status(channel_id, now),
            )
            return {"action": "skip", "reason": decision.reason}
        if decision.action == "rewrite":
            _log_event(
                action="rewrite", reason=decision.reason, channel_id=channel_id,
                author_kind="bot" if is_bot else "human",
                status=engine.status(channel_id, now),
            )
            return {"action": "rewrite", "text": decision.text}
        return None

    return hook
