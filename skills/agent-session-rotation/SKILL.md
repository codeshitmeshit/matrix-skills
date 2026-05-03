---
name: agent-session-rotation
description: >
  Rotate long-lived OpenClaw agent primary sessions safely. Use when creating or operating
  a scheduled job or manual workflow that scans non-default agents, detects sessions that are
  too old or too large, reconciles stale primary-session pointers after restarts/manual resets,
  generates handoff summaries, creates replacement sessions, and atomically switches the primary
  session reference with logging and rollback safeguards.
---

# Agent Session Rotation

Maintain healthy long-lived agent conversations by treating the agent as a stable identity and the session as a replaceable work container.

## Goal

Detect bloated or stale primary sessions for non-default agents, then rotate them safely without breaking routing, task execution, or history lookup.

## Default workflow

1. Load configuration from `scripts/config.example.json` or runtime overrides.
2. Discover candidate agents, excluding `assistant-default` by default.
3. Reconcile each agent's primary session pointer before any decision.
4. Read metrics for the current primary session:
   - session age
   - event/message count
   - last activity time
   - running task presence
5. Mark the session as a rotation candidate only when a threshold is hit and all protections pass.
6. Generate a compact handoff summary.
7. Create a replacement session.
8. Validate the replacement session.
9. Atomically switch the primary session pointer.
10. Record a rotation log and archive the previous session logically.

## Protections

Always enforce these checks before rotation:

- Skip `assistant-default` unless explicitly included.
- Skip agents in cooldown.
- Skip sessions with activity inside the idle window.
- Skip sessions currently bound to in-progress work.
- Never update the primary pointer before the new session is created and validated.
- If any step fails, keep the old primary session unchanged.

## Reconcile rule

Treat the primary-session registry as an index, not the source of truth.

Before evaluating an agent, reconcile using live session reality:

1. If the registry points to a missing or archived session, repair it.
2. Election order for a repaired primary session:
   - explicit primary-marked live session
   - newest non-archived session
   - most recently active live session
   - otherwise create a fresh primary session stub
3. Write the repaired pointer back to the registry.

## Handoff summary

Build a short summary with these sections:

- agent identity and role
- hard constraints
- current active requirements or workstreams
- unfinished items
- common failure patterns or bad cases
- key repos, paths, and operating conventions

Keep it compact and stable. First version should prefer deterministic summary assembly over fancy generation.

## Files

- `scripts/rotate_sessions.py`: scans real local session data under `~/.openclaw/agents/{agent}/sessions`, reconciles `agent:{agent}:main` style primaries from `sessions.json`, and performs dry-run rotation decisions
- `scripts/config.example.json`: example config and thresholds, including `agentsRoot`
- `references/design.md`: engineering notes, data model, and failure handling

## Current real-data behavior

The current prototype (v2) uses real local OpenClaw artifacts and supports **full rotation for all agents**:

- Agent directories under `~/.openclaw/agents`
- Per-agent `sessions/sessions.json` as the primary live registry
- Per-session `*.jsonl` files for created time, last activity time, and event counts

### Canonical main agents (with `agent:{agent}:main` key)

Real rotation supported for:
- `create_canonical_session()` creates a new `.jsonl` session file with a handoff summary injected as a system message
- `archive_canonical_session()` renames the old `.jsonl` file to `.rotated.{timestamp}.jsonl`
- `switch_canonical_main()` atomically updates `sessions.json` (with backup)

### Non-main agents (without `agent:{agent}:main` key)

Use `--create-main-alias` flag:
1. `create_main_alias()` adds `agent:{agent}:main` entry pointing to the current best session
2. Then performs canonical rotation (archive old → create new → switch pointer)

This enables a two-step migration: first create the alias, future runs rotate normally.

### CLI flags

| Flag | Description |
|------|-------------|
| `--config` | Config file path (default: `config.example.json`) |
| `--dry-run` | Override config and force dry-run mode |
| `--create-main-alias` | For agents without `:main`, create the alias then rotate |
| `--summary` | Print human-readable summary after JSON output |

### Verified agents

| Agent | Has `:main`? | Rotation tested |
|-------|-------------|----------------|
| `matrix-briefing` | ✅ | ✅ canonical rotation |
| `matrix-hub` | ✅ | ✅ canonical rotation |
| `debug-master` | was ❌ → ✅ | ✅ alias + canonical rotation |
| `assistant-default` | ✅ | ✅ canonical rotation |

## Implementation notes

Use the Python script first for rapid validation. If the mechanism proves stable, port the same workflow into a TypeScript internal job later.

When editing behavior, keep the script deterministic and idempotent. A no-op run should be safe.
