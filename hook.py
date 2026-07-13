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

import logging
import time
from typing import Any, Callable, Optional

from .session import DiscussionEngine

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


def make_pre_gateway_dispatch_hook(engine: DiscussionEngine) -> Callable[..., Optional[dict]]:
    """Return a synchronous ``pre_gateway_dispatch`` callback bound to *engine*."""

    def hook(event: Any = None, gateway: Any = None,
             session_store: Any = None, **_kwargs: Any) -> Optional[dict]:
        if event is None or not _is_discord(event):
            return None
        source = event.source

        decision = engine.decide(
            channel_id=source.chat_id,
            author_id=source.user_id or "",
            author_name=source.user_name or "",
            is_bot=bool(getattr(source, "is_bot", False)),
            message_id=(source.message_id or getattr(event, "message_id", None) or ""),
            text=getattr(event, "text", "") or "",
            now=_event_epoch(event),
        )

        if decision.action == "skip":
            logger.debug("aqui-discord-discussion skip: %s (chat=%s)",
                         decision.reason, source.chat_id)
            return {"action": "skip", "reason": decision.reason}
        if decision.action == "rewrite":
            logger.debug("aqui-discord-discussion turn taken (chat=%s)", source.chat_id)
            return {"action": "rewrite", "text": decision.text}
        return None

    return hook
