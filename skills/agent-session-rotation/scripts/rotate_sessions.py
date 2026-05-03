#!/usr/bin/env python3
"""
Agent Session Rotation

v2: Supports canonical :main rotation, primary alias creation, and human-readable summaries.

This script isolates environment-specific operations behind an adapter so it can run
as a prototype today and be wired into real OpenClaw session APIs later.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionInfo:
    session_id: str
    session_key: str
    agent_id: str
    created_at: str
    last_active_at: str
    event_count: int
    archived: bool = False
    is_primary: bool = False
    has_running_work: bool = False


@dataclass
class RegistryEntry:
    agent_id: str
    current_primary_session_id: str
    current_primary_session_key: str
    rotated_at: Optional[str] = None
    rotation_reason: Optional[str] = None
    cooldown_until: Optional[str] = None
    rotated_from_session_id: Optional[str] = None
    updated_at: Optional[str] = None
    version: int = 1


class LocalJsonAdapter:
    """Adapter backed by local OpenClaw agent session directories."""

    MAIN_KEY = "main"

    def __init__(self, registry_path: Path, rotation_log_path: Path, agents_root: Path):
        self.registry_path = registry_path
        self.rotation_log_path = rotation_log_path
        self.agents_root = agents_root
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.rotation_log_path.parent.mkdir(parents=True, exist_ok=True)

    def list_agents(self) -> List[str]:
        agents = []
        if self.agents_root.exists():
            for child in self.agents_root.iterdir():
                if child.is_dir():
                    agents.append(child.name)
        registry = self.load_registry()
        agents.extend(registry.keys())
        return sorted(set(agents))

    def list_sessions_for_agent(self, agent_id: str) -> List[SessionInfo]:
        session_dir = self.agents_root / agent_id / "sessions"
        if not session_dir.exists():
            return []

        registry = self._load_agent_sessions_json(session_dir)
        live_rows: List[SessionInfo] = []
        seen: set[str] = set()

        for session_key, meta in registry.items():
            session = self._session_from_registry(agent_id, session_key, meta)
            if session:
                live_rows.append(session)
                seen.add(session.session_id)

        for path in sorted(session_dir.glob("*.jsonl")):
            if any(tag in path.name for tag in (".checkpoint.", ".deleted.", ".reset.")):
                continue
            session_id = path.stem
            if session_id in seen:
                continue
            fallback = self._session_from_file(agent_id, path)
            if fallback:
                live_rows.append(fallback)

        return live_rows

    def load_registry(self) -> Dict[str, Dict[str, Any]]:
        if not self.registry_path.exists():
            return {}
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def save_registry(self, data: Dict[str, Dict[str, Any]]) -> None:
        self.registry_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def create_session(self, agent_id: str, handoff_summary: str) -> SessionInfo:
        session_id = str(uuid.uuid4())
        session_key = f"agent:{agent_id}:rotated:{session_id}"
        ts = now_utc().isoformat()
        return SessionInfo(
            session_id=session_id,
            session_key=session_key,
            agent_id=agent_id,
            created_at=ts,
            last_active_at=ts,
            event_count=1,
            archived=False,
            is_primary=True,
            has_running_work=False,
        )

    def validate_session(self, session: SessionInfo) -> bool:
        return bool(session.session_id and session.session_key)

    def archive_session(self, session: SessionInfo) -> None:
        return None

    def append_rotation_log(self, record: Dict[str, Any]) -> None:
        with self.rotation_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load_agent_sessions_json(self, session_dir: Path) -> Dict[str, Any]:
        path = session_dir / "sessions.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _session_from_registry(self, agent_id: str, session_key: str, meta: Dict[str, Any]) -> Optional[SessionInfo]:
        session_id = meta.get("sessionId")
        if not session_id:
            return None
        session_file = meta.get("sessionFile")
        path = Path(session_file) if session_file else self.agents_root / agent_id / "sessions" / f"{session_id}.jsonl"
        created_at, last_active_at, event_count = self._inspect_session_file(path)
        status = (meta.get("status") or "").lower()
        return SessionInfo(
            session_id=session_id,
            session_key=session_key,
            agent_id=agent_id,
            created_at=created_at,
            last_active_at=self._coerce_ts(meta.get("updatedAt"), fallback=last_active_at),
            event_count=event_count,
            archived=False,
            is_primary=session_key == f"agent:{agent_id}:main",
            has_running_work=status in {"running", "thinking"},
        )

    def _session_from_file(self, agent_id: str, path: Path) -> Optional[SessionInfo]:
        created_at, last_active_at, event_count = self._inspect_session_file(path)
        if not created_at:
            return None
        session_id = path.stem
        return SessionInfo(
            session_id=session_id,
            session_key=f"agent:{agent_id}:session:{session_id}",
            agent_id=agent_id,
            created_at=created_at,
            last_active_at=last_active_at,
            event_count=event_count,
            archived=False,
            is_primary=False,
            has_running_work=False,
        )

    def _inspect_session_file(self, path: Path) -> Tuple[str, str, int]:
        if not path.exists():
            ts = now_utc().isoformat()
            return ts, ts, 0

        created_at: Optional[str] = None
        last_active_at: Optional[str] = None
        event_count = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    event_count += 1
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    ts = row.get("timestamp")
                    if ts:
                        if created_at is None:
                            created_at = ts
                        last_active_at = ts
        except Exception:
            ts = now_utc().isoformat()
            return ts, ts, 0

        ts = now_utc().isoformat()
        return created_at or ts, last_active_at or created_at or ts, event_count

    def _coerce_ts(self, value: Any, fallback: str) -> str:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        if isinstance(value, str) and value:
            return value
        return fallback

    # -----------------------------------------------------------------------
    # Primary alias helpers (for agents without canonical :main)
    # -----------------------------------------------------------------------

    def create_main_alias(self, agent_id: str, target_session_id: str) -> Dict[str, Any]:
        """Create agent:{agent}:main entry pointing to the current best session.

        Only adds the :main pointer to sessions.json. Does NOT create a new file.
        """
        session_dir = self.get_session_dir(agent_id)
        store_path = session_dir / "sessions.json"

        if not store_path.exists():
            raise FileNotFoundError(f"sessions.json not found at {store_path}")

        main_key = f"agent:{agent_id}:{self.MAIN_KEY}"
        store = json.loads(store_path.read_text(encoding="utf-8"))

        if main_key in store:
            return {"status": "already-exists", "existing_session_id": store[main_key].get("sessionId")}

        target_meta = None
        for key, meta in store.items():
            if meta.get("sessionId") == target_session_id:
                target_meta = meta
                break

        if not target_meta:
            return {"status": "session-not-found", "session_id": target_session_id}

        store[main_key] = {
            "sessionId": target_session_id,
            "sessionFile": target_meta.get("sessionFile", str(session_dir / f"{target_session_id}.jsonl")),
            "updatedAt": target_meta.get("updatedAt"),
            "channel": target_meta.get("channel", "webchat"),
            "skillsSnapshot": target_meta.get("skillsSnapshot"),
            "label": target_meta.get("label", f"{agent_id}-main-alias"),
            "status": target_meta.get("status", "done"),
        }

        backup_path = f"{store_path}.bak.{now_utc().strftime('%Y-%m-%dT%H-%M-%S')}"
        store_path.rename(backup_path)
        store_path.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        return {"status": "created", "main_session_id": target_session_id, "backup": backup_path}

    # -----------------------------------------------------------------------
    # Canonical main-session real-write helpers (v1 safe boundary)
    # -----------------------------------------------------------------------

    def has_canonical_main(self, agent_id: str) -> bool:
        main_key = f"agent:{agent_id}:{self.MAIN_KEY}"
        store = self._load_agent_sessions_json(self.agents_root / agent_id / "sessions")
        return main_key in store

    def get_session_dir(self, agent_id: str) -> Path:
        return self.agents_root / agent_id / "sessions"

    def create_canonical_session(self, agent_id: str, handoff_summary: str) -> SessionInfo:
        session_dir = self.get_session_dir(agent_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        main_key = f"agent:{agent_id}:{self.MAIN_KEY}"

        session_id = str(uuid.uuid4())
        session_file = session_dir / f"{session_id}.jsonl"
        ts = now_utc().isoformat()

        header = json.dumps({
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": ts,
            "cwd": str(session_dir),
        })
        summary_line = json.dumps({
            "type": "message",
            "id": uuid.uuid4().hex[:8],
            "parentId": None,
            "timestamp": ts,
            "message": {
                "role": "system",
                "content": [{"type": "text", "text": handoff_summary}],
            },
        })
        session_file.write_text(header + "\n" + summary_line + "\n", encoding="utf-8")

        return SessionInfo(
            session_id=session_id,
            session_key=main_key,
            agent_id=agent_id,
            created_at=ts,
            last_active_at=ts,
            event_count=2,
            archived=False,
            is_primary=True,
            has_running_work=False,
        )

    def archive_canonical_session(self, session: SessionInfo) -> None:
        old_file = self._resolve_session_file(session)
        if not old_file or not old_file.exists():
            return
        ts = now_utc().strftime("%Y-%m-%dT%H-%M-%S")
        archive_path = old_file.parent / f"{old_file.stem}.rotated.{ts}.jsonl"
        old_file.rename(archive_path)

    def switch_canonical_main(self, agent_id: str, new_session: SessionInfo) -> Dict[str, Any]:
        session_dir = self.get_session_dir(agent_id)
        store_path = session_dir / "sessions.json"

        if not store_path.exists():
            raise FileNotFoundError(f"sessions.json not found at {store_path}")

        main_key = f"agent:{agent_id}:{self.MAIN_KEY}"
        store = json.loads(store_path.read_text(encoding="utf-8"))

        old_entry = store.get(main_key)
        old_session_id = old_entry.get("sessionId") if old_entry else None

        store[main_key] = {
            "sessionId": new_session.session_id,
            "sessionFile": str(self.agents_root / agent_id / "sessions" / f"{new_session.session_id}.jsonl"),
            "updatedAt": int(now_utc().timestamp() * 1000),
            "channel": old_entry.get("channel", "webchat") if old_entry else "webchat",
            "skillsSnapshot": old_entry.get("skillsSnapshot") if old_entry else None,
            "label": f"{agent_id}-rotated-{now_utc().strftime('%Y%m%d')}",
            "status": "running",
        }

        backup_path = f"{store_path}.bak.{now_utc().strftime('%Y-%m-%dT%H-%M-%S')}"
        store_path.rename(backup_path)
        store_path.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"old_session_id": old_session_id, "backup": backup_path}

    def _resolve_session_file(self, session: SessionInfo) -> Optional[Path]:
        session_dir = self.get_session_dir(session.agent_id)
        store = self._load_agent_sessions_json(session_dir)
        main_key = f"agent:{session.agent_id}:{self.MAIN_KEY}"
        meta = store.get(main_key)
        if meta and meta.get("sessionFile"):
            return Path(meta["sessionFile"])
        candidate = session_dir / f"{session.session_id}.jsonl"
        if candidate.exists():
            return candidate
        return session_dir / f"{session.session_id}.jsonl"


class RotationManager:
    def __init__(self, config: Dict[str, Any], adapter: LocalJsonAdapter, create_alias: bool = False):
        self.config = config
        self.adapter = adapter
        self.create_alias = create_alias
        self.registry = adapter.load_registry()
        self.run_id = uuid.uuid4().hex[:12]
        self.now = now_utc()

    def log(self, level: str, message: str, **extra: Any) -> None:
        payload = {
            "ts": self.now.isoformat(),
            "run_id": self.run_id,
            "level": level,
            "message": message,
            **extra,
        }
        print(json.dumps(payload, ensure_ascii=False))

    def run(self) -> Dict[str, Any]:
        agents = [
            a for a in self.discover_agents() if a not in self.config.get("excludeAgents", [])
        ]
        results = []
        for agent_id in agents:
            results.append(self.process_agent(agent_id))
        self.adapter.save_registry(self.registry)
        return {"run_id": self.run_id, "processed": results}

    def discover_agents(self) -> List[str]:
        agents = set(self.adapter.list_agents())
        configured = self.config.get("includeAgents", [])
        for agent in configured:
            agents.add(agent)
        return sorted(agents)

    def process_agent(self, agent_id: str) -> Dict[str, Any]:
        current = self.reconcile_primary(agent_id)
        if current is None:
            return {"agent_id": agent_id, "status": "no-session"}

        decision = self.evaluate_candidate(current, agent_id)
        if decision["rotate"] is False:
            self.log("info", "skip rotation", agent_id=agent_id, reason=decision["reason"])
            return {"agent_id": agent_id, "status": "skipped", **decision}

        summary = self.build_handoff_summary(agent_id, current)
        if self.config.get("dryRun", True):
            self.log("info", "dry run rotation candidate", agent_id=agent_id, reason=decision["reason"])
            return {
                "agent_id": agent_id,
                "status": "dry-run-candidate",
                "reason": decision["reason"],
                "current_session_id": current.session_id,
                "summary_preview": summary[:300],
            }

        return self.rotate(agent_id, current, summary, decision["reason"])

    def reconcile_primary(self, agent_id: str) -> Optional[SessionInfo]:
        live_sessions = self.adapter.list_sessions_for_agent(agent_id)
        entry = self.registry.get(agent_id)

        if entry:
            target_id = entry.get("current_primary_session_id")
            for session in live_sessions:
                if session.session_id == target_id and not session.archived:
                    return session

        elected = self.elect_primary(live_sessions)
        if elected:
            self.registry[agent_id] = RegistryEntry(
                agent_id=agent_id,
                current_primary_session_id=elected.session_id,
                current_primary_session_key=elected.session_key,
                updated_at=self.now.isoformat(),
                version=(entry or {}).get("version", 0) + 1,
            ).__dict__
            self.log("info", "reconciled primary session", agent_id=agent_id, session_id=elected.session_id)
            return elected

        return None

    def elect_primary(self, sessions: List[SessionInfo]) -> Optional[SessionInfo]:
        primary_marked = [s for s in sessions if s.is_primary and not s.archived]
        if primary_marked:
            return sorted(primary_marked, key=lambda s: s.created_at, reverse=True)[0]

        live = [s for s in sessions if not s.archived]
        if live:
            return sorted(live, key=lambda s: (s.last_active_at, s.created_at), reverse=True)[0]
        return None

    def evaluate_candidate(self, session: SessionInfo, agent_id: str) -> Dict[str, Any]:
        age_hours = (self.now - self.parse_dt(session.created_at)).total_seconds() / 3600
        idle_minutes = (self.now - self.parse_dt(session.last_active_at)).total_seconds() / 60
        cooldown_until = ((self.registry.get(agent_id) or {}).get("cooldown_until"))

        if cooldown_until and self.parse_dt(cooldown_until) > self.now:
            return {"rotate": False, "reason": "cooldown", "age_hours": age_hours, "idle_minutes": idle_minutes}
        if session.has_running_work:
            return {"rotate": False, "reason": "running-work", "age_hours": age_hours, "idle_minutes": idle_minutes}
        if idle_minutes < self.config.get("minIdleMinutes", 30):
            return {"rotate": False, "reason": "not-idle", "age_hours": age_hours, "idle_minutes": idle_minutes}
        if age_hours >= self.config.get("maxSessionAgeHours", 168):
            return {"rotate": True, "reason": "too_old", "age_hours": age_hours, "idle_minutes": idle_minutes}
        if session.event_count >= self.config.get("maxEventCount", 300):
            return {"rotate": True, "reason": "too_many_events", "age_hours": age_hours, "idle_minutes": idle_minutes}
        return {"rotate": False, "reason": "below-threshold", "age_hours": age_hours, "idle_minutes": idle_minutes}

    def build_handoff_summary(self, agent_id: str, session: SessionInfo) -> str:
        return (
            f"# Handoff Summary\n\n"
            f"- Agent: {agent_id}\n"
            f"- Previous session: {session.session_id}\n"
            f"- Previous session key: {session.session_key}\n"
            f"- Created at: {session.created_at}\n"
            f"- Last active at: {session.last_active_at}\n"
            f"- Event count: {session.event_count}\n\n"
            f"## Inherited sections\n"
            f"- Identity and role\n"
            f"- Hard constraints\n"
            f"- Current active work\n"
            f"- Unfinished items\n"
            f"- Common failure patterns\n"
            f"- Key repos, paths, and conventions\n"
        )

    def rotate(self, agent_id: str, current: SessionInfo, summary: str, reason: str) -> Dict[str, Any]:
        has_main = self.adapter.has_canonical_main(agent_id)

        if has_main and current.session_key == f"agent:{agent_id}:{self.adapter.MAIN_KEY}":
            return self._rotate_canonical(agent_id, current, summary, reason)
        elif has_main:
            self.log("warn", "agent has canonical main but elected primary is not the main key -- skipping real rotation", agent_id=agent_id, elected_key=current.session_key)
            return {"agent_id": agent_id, "status": "skipped-non-main", "reason": "elected session is not the canonical main key", "current_session_key": current.session_key}
        elif self.create_alias:
            return self._rotate_with_alias(agent_id, current, summary, reason)
        else:
            self.log("warn", "no canonical main for agent -- detect-only mode", agent_id=agent_id)
            return {"agent_id": agent_id, "status": "detect-only", "reason": "no canonical main key", "current_session_key": current.session_key}

    def _rotate_canonical(self, agent_id: str, current: SessionInfo, summary: str, reason: str) -> Dict[str, Any]:
        # 1. Archive old session file BEFORE switching the pointer
        old_file_path = self.adapter._resolve_session_file(current)
        self.adapter.archive_canonical_session(current)

        # 2. Create new session file
        new_session = self.adapter.create_canonical_session(agent_id, summary)
        if not self.adapter.validate_session(new_session):
            raise RuntimeError(f"new canonical session validation failed for {agent_id}")

        # 3. Switch sessions.json main pointer
        switch_result = self.adapter.switch_canonical_main(agent_id, new_session)

        self.registry[agent_id] = RegistryEntry(
            agent_id=agent_id,
            current_primary_session_id=new_session.session_id,
            current_primary_session_key=new_session.session_key,
            rotated_at=self.now.isoformat(),
            rotation_reason=reason,
            cooldown_until=(self.now + timedelta(minutes=self.config.get("cooldownMinutes", 120))).isoformat(),
            rotated_from_session_id=current.session_id,
            updated_at=self.now.isoformat(),
            version=((self.registry.get(agent_id) or {}).get("version", 0) + 1),
        ).__dict__

        record = {
            "ts": self.now.isoformat(),
            "run_id": self.run_id,
            "agent_id": agent_id,
            "reason": reason,
            "old_session_id": current.session_id,
            "new_session_id": new_session.session_id,
            "switch_backup": switch_result.get("backup"),
        }
        self.adapter.append_rotation_log(record)
        self.log("info", "canonical rotation complete", **record)
        return {"agent_id": agent_id, "status": "rotated", **record}

    def _rotate_with_alias(self, agent_id: str, current: SessionInfo, summary: str, reason: str) -> Dict[str, Any]:
        """For agents without canonical :main, first create the alias, then rotate."""
        alias_result = self.adapter.create_main_alias(agent_id, current.session_id)
        if alias_result["status"] != "created":
            self.log("warn", "failed to create main alias", agent_id=agent_id, result=alias_result)
            return {"agent_id": agent_id, "status": "alias-failed", "alias_result": alias_result}

        self.log("info", "created main alias for non-main agent", agent_id=agent_id, session_id=current.session_id)

        return self._rotate_canonical(agent_id, current, summary, reason)

    @staticmethod
    def parse_dt(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_config(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rotate long-lived agent sessions safely")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.example.json")))
    parser.add_argument("--dry-run", action="store_true", help="Override config and force dry run")
    parser.add_argument("--create-main-alias", action="store_true",
                        help="For agents without :main, create a :main alias pointing to current best session")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary after JSON output")
    return parser.parse_args()


def format_summary(result: Dict[str, Any]) -> str:
    """Human-readable summary of rotation results."""
    run_id = result.get("run_id", "?")
    processed = result.get("processed", [])

    lines = [f"\n{'='*60}", f"Session Rotation Summary (run={run_id})", f"{'='*60}"]

    rotated = [p for p in processed if p.get("status") == "rotated"]
    skipped = [p for p in processed if p.get("status") == "skipped"]
    detect_only = [p for p in processed if p.get("status") == "detect-only"]
    dry_run = [p for p in processed if p.get("status") == "dry-run-candidate"]
    alias_created = [p for p in processed if p.get("status") == "rotated" and p.get("alias_result")]

    lines.append(f"\n📊 Agents processed: {len(processed)}")
    lines.append(f"  ✅ Rotated: {len(rotated)}")
    lines.append(f"  ⏭️  Skipped: {len(skipped)}")
    lines.append(f"  🔍 Detect-only: {len(detect_only)}")
    lines.append(f"  🧪 Dry-run candidates: {len(dry_run)}")

    if rotated:
        lines.append(f"\n✅ Rotated sessions:")
        for p in rotated:
            lines.append(f"  • {p['agent_id']}: {p.get('old_session_id','?')[:8]}... → {p.get('new_session_id','?')[:8]}... ({p.get('reason','?')})")

    if alias_created:
        lines.append(f"\n🔗 Main aliases created:")
        for p in alias_created:
            lines.append(f"  • {p['agent_id']}: alias → {p.get('alias_result',{}).get('main_session_id','?')[:8]}...")

    if dry_run:
        lines.append(f"\n🧪 Would rotate (dry run):")
        for p in dry_run:
            lines.append(f"  • {p['agent_id']}: reason={p.get('reason','?')}, age={p.get('age_hours',0):.1f}h, idle={p.get('idle_minutes',0):.0f}min")

    if skipped:
        lines.append(f"\n⏭️  Skipped:")
        for p in skipped:
            lines.append(f"  • {p['agent_id']}: {p.get('reason','?')}")

    if detect_only:
        lines.append(f"\n🔍 Detect-only (no :main key):")
        for p in detect_only:
            lines.append(f"  • {p['agent_id']}: current session {p.get('current_session_key','?')}")

    lines.append(f"\n{'='*60}\n")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    if args.dry_run:
        config["dryRun"] = True

    registry_path = Path(config["registryPath"])
    if not registry_path.is_absolute():
        registry_path = config_path.parent / registry_path
    rotation_log_path = Path(config["rotationLogPath"])
    if not rotation_log_path.is_absolute():
        rotation_log_path = config_path.parent / rotation_log_path

    agents_root = Path(config.get("agentsRoot", "/root/.openclaw/agents"))
    if not agents_root.is_absolute():
        agents_root = config_path.parent / agents_root

    adapter = LocalJsonAdapter(
        registry_path=registry_path,
        rotation_log_path=rotation_log_path,
        agents_root=agents_root,
    )
    manager = RotationManager(config=config, adapter=adapter, create_alias=args.create_main_alias)
    result = manager.run()

    # Always print JSON first (machine-readable)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # Optionally print human-readable summary
    if args.summary:
        print(format_summary(result))

    return 0


if __name__ == "__main__":
    sys.exit(main())
