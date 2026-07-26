# Agent Smith — AI agent conventions

Long-form conventions demoted out of `CLAUDE.md` (2026-07-26) so they load on demand
instead of on every session. `CLAUDE.md` keeps the hard rules; this file keeps the
rationale, patterns, and UX guidance.

## Development principles

### 1. Determinism & consistency

Python provides consistency; prompts provide interactivity. Use typed Python interfaces
for all PocketSmith operations. Same inputs must produce the same outputs — no hidden
state, no side effects, all logic testable and auditable.

```python
def update_transaction(
    transaction_id: int,
    category_id: Optional[int] = None,
    labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Update a transaction with explicit parameters."""
    ...
```

### 2. Code reuse over creation

Before writing new code, search for existing code that does the job.

1. Check existing scripts: `scripts/operations/` (data ops), `scripts/workflows/`
   (multi-step), `scripts/services/` (business logic), `scripts/core/` (shared utils).
2. Look for similar patterns — if `fetch_conflicts.py` exists, don't create
   `get_conflicts.py`; if `update_transaction.py` already handles labels, don't
   duplicate label logic.
3. Extend rather than duplicate: add parameters to existing scripts, put shared helpers
   in `scripts/core/`, refactor common patterns into reusable functions.

### 3. Test-driven development

Write the failing test, confirm it fails, implement the minimum to pass, refactor.
Unit tests for new functions, integration tests for API interactions. Tests live in
`tests/unit/` and `tests/integration/`. Run with `uv run pytest tests/ -v`.

### 4. Separation of concerns

| Layer | Purpose | Tools |
|-------|---------|-------|
| Python scripts | Data operations, business logic | Typed functions, argparse CLI |
| Skills / commands | User interaction, orchestration | Markdown prompts |
| Prompts | Conversational UX, guidance | Natural language |

`User → slash command (UX) → Python script (logic) → PocketSmith API (data)`

### 5. Real-time feedback

Stream progress during long operations. `-u` for unbuffered output, explicit counters,
never leave the user waiting silently.

```python
for i, txn in enumerate(transactions):
    print(f"[{i+1}/{len(transactions)}] Processing {txn['payee']}...")
    process(txn)
print(f"✅ Completed {len(transactions)} transactions")
```

### 6. Context window management

Delegate context-heavy operations to subagents: exploring large codebases, processing
more than ~50 records, multi-step operations with verbose output. Keep large data
structures and verbose tool output out of the main conversation.

| Pattern | Use case |
|---------|----------|
| `general-purpose` | Complex multi-step tasks, code review, implementation |
| `Explore` | Finding files, understanding architecture |
| Purpose-built | Repeated specialized tasks (categorization, analysis, reporting) |

## User experience guidelines

Every command/skill interaction should cover: goal, why it matters, steps, real-time
progress, summary, next steps.

```markdown
## Goal
Categorize uncategorized transactions using rules and AI.

## Why This Matters
Uncategorized transactions reduce your financial visibility and health score.

## Steps
1. Fetch uncategorized transactions
2. Apply rule engine matching
3. Use AI for unmatched transactions
4. Update PocketSmith

## Next Steps
- Review flagged conflicts: `/smith:review-conflicts`
- Check your health score: `/smith:health`
```

Visual elements: emoji status indicators (✅ ❌ ⚠️ 🔄 📊 💰), `[23/100]` progress
counters, markdown tables for summaries, ASCII charts for data, `AskUserQuestion` for
choices.

Slash-command frontmatter:

```yaml
---
description: Categorize uncategorized transactions
argument-hint: [period] [--mode smart|conservative|aggressive] [--dry-run]
---
```

Error handling: explain in plain language, suggest a fix, offer next steps.

```markdown
❌ **Error:** Could not find category "Grocereis"

**Did you mean:** "Groceries" (Food & Dining > Groceries)?

**To fix:** Run the command again with the correct category name.
```

## Code patterns

### Script CLI pattern

```python
#!/usr/bin/env python3
"""Brief description of what this script does."""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--output", choices=["json", "summary"], default="summary")
    args = parser.parse_args()

    result = do_work(dry_run=args.dry_run)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"✅ Processed {result['count']} items")

    return 0 if result["success"] else 1

if __name__ == "__main__":
    sys.exit(main())
```

### API client usage

```python
from scripts.core.api_client import PocketSmithClient
from scripts.core.category_utils import find_category_by_name

client = PocketSmithClient()
user = client.get_user()

# Always flatten categories for operations
categories = client.get_categories(user["id"], flatten=True)
category = find_category_by_name(categories, "Groceries")

client.update_transaction(
    transaction_id=12345,
    category_id=category["id"],
    labels=["processed"],
)
```

### Label constants

Use the constants from `scripts/core/labels.py` (`LABEL_CATEGORY_CONFLICT`,
`LABEL_NEEDS_REVIEW`, `LABEL_TAX_DEDUCTIBLE`, …). In templates use `$CONSTANT_NAME`
syntax — `$LABEL_CATEGORY_CONFLICT` → `⚠️ Review: Category Conflict`.

## Tax intelligence levels

| Level | Features |
|-------|----------|
| Reference | Basic reporting, ATO category mapping |
| Smart | Deduction flagging, CGT tracking, thresholds |
| Full | BAS preparation, compliance checks (requires disclaimer) |

## Other documentation

| Document | Audience | Purpose |
|----------|----------|---------|
| [DEVELOPMENT.md](../../DEVELOPMENT.md) | Humans | Development workflow, dual-location architecture |
| [CONTRIBUTING.md](../../CONTRIBUTING.md) | Humans | Setup, testing, PR process |
| [README.md](../../README.md) | Everyone | Project overview, quick start |
| `docs/design/*.md` | Everyone | Architecture and design specs |
| `ai_docs/` | AI agents | API reference, tax guidelines |
