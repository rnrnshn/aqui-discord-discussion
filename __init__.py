"""aqui-discord-discussion — controlled bot-to-bot discussion sessions.

A standalone Hermes plugin that adds a controlled discussion protocol on top of
the shipped Discord platform adapter, without forking it. It registers a
``pre_gateway_dispatch`` hook that acts as the session/turn gate:

- an approved human posts ``discuss: <topic>`` in an approved channel;
- configured participant bots contribute automatically in deterministic order;
- the session ends on max turns, timeout, or ``stop discussion``.

Enable per profile by listing ``aqui-discord-discussion`` in ``plugins.enabled``
and setting the ``DISCORD_DISCUSSION_*`` environment variables (see README.md).
The adapter must also be told to forward bot messages
(``DISCORD_ALLOW_BOTS=all``) so the hook can see participant contributions; the
hook — not the global flag — is what scopes bot participation to active
sessions.
"""

from __future__ import annotations

import logging

from .hook import make_pre_gateway_dispatch_hook
from .session import DiscussionConfig, DiscussionEngine

logger = logging.getLogger("aqui_discord_discussion")


def register(ctx) -> None:
    """Entry point called by Hermes' plugin loader."""
    config = DiscussionConfig.from_env()
    engine = DiscussionEngine(config)
    ctx.register_hook("pre_gateway_dispatch", make_pre_gateway_dispatch_hook(engine))

    if config.enabled:
        logger.info(
            "aqui-discord-discussion enabled: %d participant(s), %d channel(s), "
            "max_turns=%d, timeout=%ds.",
            len(config.participant_bot_ids), len(config.discussion_channels),
            config.max_turns, config.timeout_seconds)
    else:
        logger.info("aqui-discord-discussion loaded but discussion mode is "
                    "disabled (incomplete configuration); normal behavior "
                    "is unaffected.")
