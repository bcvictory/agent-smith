# CLAUDE.md

**Agent Smith** — financial management skill for Claude Code: PocketSmith API
integration with AI analysis, rule management, tax intelligence, scenario planning.
Design spec (source of truth): `docs/design/2025-11-20-agent-smith-design.md`.

## Hard rules

- **Edit `scripts/`, NEVER `agent-smith-plugin/skills/agent-smith/scripts/`** — the
  plugin copy is gitignored and overwritten by `./scripts/dev-sync.sh`. Run dev-sync
  after editing.
- **Always `uv run python -u …`** — bare `python` won't find deps; `-u` is mandatory for
  streaming output.
- **Always `client.get_categories(user_id, flatten=True)`** — without it, 127 child
  categories are invisible.
- **Every PocketSmith mutation goes through a git-tracked script in
  `scripts/operations/`.** Never embed Python in slash commands or prompts; never write
  an ad-hoc one-time script.
- **Back up data before any write to PocketSmith.**
- **After creating/modifying a slash command, skill, hook, or MCP server, tell the user
  to restart Claude Code.**

## Layout

| Directory | Purpose |
|-----------|---------|
| `scripts/core/` | Shared libraries — `api_client.py`, `rule_engine.py`, `labels.py`, `category_utils.py` |
| `scripts/operations/` | Data operations: `fetch_*` (read), `update_*` (single mutation), `create_*`, `reprocess_*` (batch) |
| `scripts/workflows/` | Multi-step processes |
| `scripts/services/` | Business logic (LLM categorization) |
| `scripts/health/` · `scripts/tax/` · `scripts/scenarios/` · `scripts/setup/` | Health checks, tax intelligence, scenario analysis, onboarding |
| `data/rules.yaml` | User-defined rules |

```bash
uv run pytest tests/ -v                        # tests
./scripts/dev-sync.sh                          # mirror scripts/ → plugin copy
uv run python -u scripts/health/check.py       # health check
```

## Technical reference

- PocketSmith auth: `X-Developer-Key` header from `.env`; rate limit via
  `API_RATE_LIMIT_DELAY`; docs in `ai_docs/pocketsmith-api-documentation.md`.
- Env vars: `POCKETSMITH_API_KEY` (required), `TAX_INTELLIGENCE_LEVEL`
  (smart|reference|full), `DEFAULT_INTELLIGENCE_MODE` (smart|conservative|aggressive),
  `TAX_JURISDICTION=AU`, `FINANCIAL_YEAR_END=06-30`.

## Conventions, patterns, UX

Development principles, script/CLI patterns, subagent delegation, command UX, tax
intelligence levels: [docs/guides/ai-conventions.md](docs/guides/ai-conventions.md).
Humans: [DEVELOPMENT.md](DEVELOPMENT.md), [CONTRIBUTING.md](CONTRIBUTING.md).
