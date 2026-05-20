# AI Ads Agent

[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)
[![Part of Mesh Pilot](https://img.shields.io/badge/Mesh%20Pilot-stack-black.svg)](https://meshpilot.app)
[![Mirrored on Codeberg](https://img.shields.io/badge/codeberg-mirror-black.svg)](https://codeberg.org/Glitch_Exec_Lab/ai-ads-agent)

> **Part of the [Mesh Pilot](https://meshpilot.app) open-source 6-agent marketing stack.**
> Autonomous paid-acquisition operator for Meta, Google Ads, TikTok, Amazon Ads, and LinkedIn — drafts spend / creative / audience changes, queues them for human approval, then executes against the platforms.

The agent reads daily ad performance, plans write-actions (pause underperformers, shift budgets, swap creative), and surfaces every proposal through a human-in-the-loop gate before anything touches live spend.

## Quick start

```bash
git clone https://gitlab.com/glitch-grow/ai-ads-agent.git
# or: git clone https://codeberg.org/Glitch_Exec_Lab/ai-ads-agent.git
cd ai-ads-agent

uv pip install -e .          # or: pip install -e .
cp .env.example .env         # fill in API keys for the platforms you use

python -m ads_agent.server   # FastAPI server + scheduler
```

## What it does

- **Action planner** — reads daily ad performance (Meta, GA4, PostHog, Amazon, Google Ads) and proposes write-actions: pause underperformers, shift budgets, rotate fatigued creative.
- **HITL gateway** — every proposed action lands in a Discord channel as an approvable card. Nothing fires until a human clicks approve.
- **Executor** — runs approved actions against the platform API, captures the response, writes back to the audit trail.
- **Daily snapshots** — Meta destination URLs synced for Meta→Amazon attribution; GA4 + PostHog + Amazon Attribution joined on a date axis.

## The HITL pattern (shared across the stack)

Every action that touches money, brand voice, or outbound delivery routes through a human-in-the-loop approval gate. This agent never auto-fires destructive or public actions. Proposals land in a queue (Discord by default; the [Mesh Pilot](https://meshpilot.app) cockpit adds a web inbox + Telegram mirrors) and execute only after explicit operator approval. The audit log records who approved what, when, on which channel.

## Platforms

- **Meta** (Facebook + Instagram) — via `facebook-business`
- **Google Ads** — via `google-ads` (native MCC client)
- **TikTok Business** — via internal MCP server
- **Amazon Ads** — via internal MCP server (separate repo)
- **LinkedIn Ads** — via internal MCP server
- **GA4** — via `google-analytics-data`
- **PostHog** — via `posthog` SDK

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

## Companions in the stack

| Agent | Domain | Repo |
|---|---|---|
| **AI Ads Agent** | This repo | — |
| AI Sales Agent | Outbound B2B sales | [glitch-grow/ai-sales-agent](https://gitlab.com/glitch-grow/ai-sales-agent) |
| AI Social Agent | Multi-platform posting + ORM | [glitch-grow/ai-social-agent](https://gitlab.com/glitch-grow/ai-social-agent) |
| AI UGC Agent | Vertical video ad pipeline | [glitch-grow/ai-ugc-agent](https://gitlab.com/glitch-grow/ai-ugc-agent) |
| AI Voice Agent | LiveKit-based phone agent | [glitch-grow/ai-voice-agent](https://gitlab.com/glitch-grow/ai-voice-agent) |
| AI SEO Agent | Shopify SEO autopilot | [glitch-grow/ai-seo-agent](https://gitlab.com/glitch-grow/ai-seo-agent) |

In production they're orchestrated by **[Mesh Pilot](https://meshpilot.app)** — the closed-source cockpit that runs all six in concert with shared brand context, a single web approval inbox, and cross-agent handoffs (the social agent's audience finding feeds the UGC agent's script feeds this agent's ad-set upload, all in one operator turn).

## Mirrors

- GitLab: [`glitch-grow/ai-ads-agent`](https://gitlab.com/glitch-grow/ai-ads-agent)
- Codeberg: [`Glitch_Exec_Lab/ai-ads-agent`](https://codeberg.org/Glitch_Exec_Lab/ai-ads-agent)

Both stay in sync. Issues + PRs welcome on either side.

## License

[MIT](LICENSE) — fork it, ship products with it, no attribution required.

---

Built by [Glitch Executor Labs](https://glitchexecutor.com).
