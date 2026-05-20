# Contributing to AI Ads Agent

Thanks for thinking about contributing. This agent is one of six in the open-source [Mesh Pilot](https://meshpilot.app) marketing stack, and we welcome PRs that fit its scope: paid-acquisition automation with a human-in-the-loop approval gate.

## Ways to contribute

- **Bug reports** — open an issue on either mirror (GitLab is primary; Codeberg syncs nightly). Include the platform involved (Meta / Google / TikTok / etc), the action that failed, and any redacted log output.
- **Platform integrations** — adding a new ad platform writer? Follow the pattern in `src/ads_agent/actions/`. The action must surface a proposal through the HITL gateway before it executes. No exceptions.
- **State-machine improvements** — the planner + executor are LangGraph state machines in `src/ads_agent/agent/`. Keep nodes pure where possible; side effects belong in actions.
- **Documentation** — README clarifications, runnable examples, integration recipes.

## Before you open a PR

1. **Open an issue first** for anything beyond a one-line fix. We'd rather discuss the shape than reject a finished PR.
2. **Preserve the HITL gate**. Any new action must route through `src/ads_agent/discord/` (or an equivalent approval surface). The HITL pattern is the moat — no PRs that bypass it.
3. **Add tests** for new actions and state-machine nodes.
4. **Format + lint** — run `ruff format` and `ruff check` before committing.
5. **One concern per PR** — keep diffs focused and reviewable.

## Development setup

```bash
git clone https://gitlab.com/mesh-pilot/ai-ads-agent.git
cd ai-ads-agent
uv pip install -e ".[dev]"
cp .env.example .env
pytest
```

## Commit style

Conventional commits. Examples:

- `feat(meta): pause-low-roas action`
- `fix(google-ads): handle expired refresh-token`
- `docs(readme): clarify HITL gateway setup`

## License

By contributing you agree your contributions are licensed under [MIT](LICENSE).

## Questions

Open an issue. For private inquiries (security, partnership): `support@meshpilot.app`.
