# Changelog

## [0.1.0] — 2026-05-18

### Added

- Initial open-source release of the AI ads agent engine.
- LangGraph-based action planner and executor.
- Discord HITL gateway for approval workflow.
- Platform clients: Meta, Google Ads, Amazon, LinkedIn.
- Analytics ingest: GA4, PostHog.
- FastAPI webhook receiver.
- Periodic scheduler for daily snapshots + cross-platform sync jobs.

### Removed (extracted to a separate proprietary repo)

- Brand-specific configuration registry.
- Operational guardrails and cost-management rules.
- Fine-tuned planner prompts.
