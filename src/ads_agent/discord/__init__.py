"""Discord transport for the ads agent.

Discord side runs a separate bot (see /home/support/glitch-discord-bot)
that listens on guild channels and drops each message into
    /home/support/.glitch-discord/inbox/<channel>/<msg_id>.json

This package is the ads-agent half of that contract:

  - `poster.post_message(channel_id, text)` — send a message back
  - `dispatcher.parse_and_run(content, default_slug)` — route a `/cmd`
    string through the existing LangGraph and return reply_text
  - `inbox_consumer.main()` — watch-and-dispatch loop, run as a systemd service

The Telegram bot stays the primary transport; Discord is additive, not
replacing it.
"""
