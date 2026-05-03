# Agent Session Rotation Design

## Core principle

- Agent = long-lived identity
- Session = rotatable work container

Do not bind an agent permanently to one session.

## First-version scope

The first version is a safe operator-oriented skeleton:
- scan candidate agents
- reconcile primary session pointers
- compute rotation eligibility
- build a deterministic handoff summary
- define an atomic switch sequence
- emit logs and a machine-readable result

It does not need to fully integrate with OpenClaw internals on day one. Adapters can be filled in later.

## Suggested registry shape

```json
{
  "agent_id": "matrix-code",
  "current_primary_session_id": "sess_xxx",
  "current_primary_session_key": "agent:matrix-code:...",
  "rotated_at": "2026-04-20T01:30:00+08:00",
  "rotation_reason": "too_many_events",
  "cooldown_until": "2026-04-20T03:30:00+08:00",
  "rotated_from_session_id": "sess_old",
  "updated_at": "2026-04-20T01:30:01+08:00",
  "version": 3
}
```

## Candidate rule

A primary session becomes a candidate when either is true:
- `session_age_hours >= maxSessionAgeHours`
- `event_count >= maxEventCount`

And all of these are true:
- `idle_minutes >= minIdleMinutes`
- `has_running_work == false`
- `now >= cooldown_until`

## Reconcile flow

1. Load registry entry.
2. Query live sessions for the agent.
3. If the registry pointer is valid, keep it.
4. Otherwise elect a new primary from live sessions.
5. If none exists, create a new primary stub.
6. Persist repaired registry entry.

## Atomic switch flow

1. Build summary from old primary session.
2. Create replacement session.
3. Inject summary/start context.
4. Validate replacement session.
5. Persist new primary pointer.
6. Record rotation log.
7. Mark old session archived logically.

If any step fails before step 5, abort without modifying the current primary pointer.

## Failure classes

- registry missing or corrupt
- pointed session missing
- no eligible replacement can be created
- summary generation failure
- concurrent rotation attempts
- primary session became active during rotation window

## Logging

Every run should log:
- run id
- scanned agents
- primary session metrics
- candidate decision
- skip reason or rotation reason
- old/new session ids
- reconcile actions
- errors

## Future adapters

The Python script should isolate all environment-specific operations behind adapter methods so later migration is easy:
- list agents
- list sessions for agent
- read session metrics
- create session
- inject handoff summary
- persist registry
- persist rotation log
- archive session

## Real local findings

Current local OpenClaw data revealed three important integration facts:

1. Not every agent uses a canonical `agent:{agent}:main` key
   - `matrix-code`, `matrix-core`, `matrix-quality` do
   - `debug-master` and some others do not
   - those agents instead keep long-lived active sessions under delivery-scoped keys like:
     - `agent:debug-master:feishu:direct:...`
     - `agent:debug-master:feishu:group:...`

2. `sessionId` is not always the transcript filename
   - example observed: `agent:matrix-code:main` has `sessionId=facbc508-...`
   - but `sessionFile=/root/.openclaw/agents/matrix-code/sessions/363fc90e-....jsonl`
   - so any real rotation implementation must treat `sessionFile` as authoritative and not derive it from `sessionId`

3. Real-write rotation needs a primary alias policy
   - if a canonical `:main` key exists, it is the safest first-class primary pointer
   - if no canonical `:main` key exists, rotating blindly would mean rewriting routing-bound Feishu keys, which is riskier

## Recommended v1 write boundary

Use this staged rollout:

- support real scanning and reconcile for all agents ✅ implemented
- support real write rotation only for agents with canonical `agent:{agent}:main` ✅ implemented (v1)
- support `--create-main-alias` for non-`:main` agents ✅ implemented (v2)
- support `--summary` for human-readable output ✅ implemented (v2)

## v2 implementation details (2026-04-20)

### Non-main alias creation (`create_main_alias`)

For agents without `agent:{agent}:main`, the adapter can create an alias:
1. Locate the target session's metadata in `sessions.json`
2. Add `agent:{agent}:main` entry pointing to that session
3. Preserve `channel`, `skillsSnapshot`, `updatedAt` from original entry
4. Backup `sessions.json` before mutation
5. Returns `{status: "created"}` or `{status: "already-exists"}` / `{status: "session-not-found"}`

### Alias + rotation flow (`_rotate_with_alias`)

When `--create-main-alias` is used:
1. `create_main_alias()` → creates the `:main` pointer
2. `_rotate_canonical()` → archive old → create new → switch pointer

This means after the first rotation, the agent will always have a canonical `:main` key and future runs rotate normally.

### Human-readable summary (`format_summary`)

When `--summary` is used, a formatted text report is printed after JSON output:
- Total agents processed, broken down by status
- Rotated sessions (old → new IDs, reason)
- Main aliases created (if applicable)
- Dry-run candidates (would rotate)
- Skipped agents (with reason)
- Detect-only agents (no :main key)

### Canonical main rotation (v1, unchanged)

1. **Archive old session file** (before switching)
2. **Create new session file** (with handoff summary)
3. **Switch sessions.json main entry** (with backup)
4. **Update skill registry**

### v2 verification results

Test run with `--create-main-alias` confirmed:
- ✅ `debug-master` alias created + rotated (old: `9f60ed72`, new: `37d3e99e`)
- ✅ `assistant-default` rotated successfully (old: `84e84ef8`, new: `3487d876`)
- ✅ Old session files archived with `.rotated.{timestamp}.jsonl` suffix
- ✅ `sessions.json` backups created before mutation
- ✅ Handoff summary injected as system message in new session file
- ✅ Human-readable summary output works correctly
