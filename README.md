# AI Ads Agent

Open-source AI agent for automating paid-acquisition campaigns across
Meta, Google Ads, Amazon Ads, and LinkedIn.

Plans creative + keyword changes, proposes them through a human-in-the-loop
Discord gateway, then executes approved changes against the platforms.

## What it does

- **Action planner** — reads daily ad performance data (Meta, GA4, PostHog,
  Amazon, Google Ads) and proposes write-actions (pause underperformers,
  budget shifts, creative rotations).
- **HITL gateway** — proposed actions land in a Discord channel as
  approvable cards. Approvals fire executor jobs against the relevant
  platform API.
- **Executor** — runs approved actions, captures the response, writes back
  to the audit trail.
- **Daily snapshots** — Meta destination URLs synced for Meta→Amazon
  attribution; GA4 + PostHog + Amazon Attribution joined on a date axis.

## Platforms

- Meta (Facebook + Instagram) — via `facebook-business`
- Google Ads — via `google-ads` (native MCC client)
- Amazon Ads — via internal MCP server (separate repo)
- LinkedIn Ads — via internal MCP server
- GA4 — via `google-analytics-data`
- PostHog — via `posthog` SDK

## Layout

```
src/ads_agent/
  agent/        # planner + executor LangGraph state machines
  actions/      # platform-specific write actions
  meta/         # Meta API client + insights
  google_ads/   # Google Ads client
  amazon/       # Amazon Ads client (via MCP)
  linkedin/     # LinkedIn Ads client (via MCP)
  ga4/          # GA4 reporting client
  posthog/      # PostHog event ingest
  discord/      # HITL gateway
  scheduler/    # periodic snapshot + sync jobs
  server.py     # FastAPI receiver for webhooks
```

## Install

```
uv pip install -e .          # or: pip install -e .
cp .env.example .env         # then fill in API keys for the platforms you use
```

## License

MIT — see `LICENSE`.
