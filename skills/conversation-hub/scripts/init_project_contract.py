#!/usr/bin/env python3
"""Create a minimal cross-agent repository contract without overwriting files."""
from __future__ import annotations

import argparse
import subprocess
from datetime import date
from pathlib import Path


def git_value(root: Path, *args: str, fallback: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return fallback
    return result.stdout.strip() or fallback


def render_files(args: argparse.Namespace, root: Path) -> dict[Path, str]:
    today = date.today().isoformat()
    branch = args.work_branch or git_value(
        root, "branch", "--show-current", fallback="pending"
    )
    start_commit = args.start_commit or git_value(
        root, "rev-parse", "--short", "HEAD", fallback="pending"
    )
    owner = args.owner_role
    scope = "\n".join(f"- {item}" for item in args.scope)
    exclusions = "\n".join(f"- {item}" for item in args.do_not_touch)
    checks = "\n".join(f"- [ ] {item}" for item in args.acceptance)

    agents = """# Repository relay rules

For substantial multi-session or multi-agent work, treat this repository contract as current authorization and conversation history only as evidence.

Before editing, read `PROJECT.md`, `DECISIONS.md`, `TASKS.md`, and the one active `handoffs/T-xxx.md`. Preserve unrelated changes. Maintain one active writing task per shared worktree. Follow the handoff's Git delivery policy exactly; credentials do not authorize push, merge, deployment, or release.

Before handoff or completion, run relevant project tests and the bundled project-contract validator, then record evidence and final Git state in the active handoff.
"""
    project = f"""# {args.project_title}

## Objective

{args.project_goal}

## Scope

{scope}

## Non-goals

{exclusions}

## Definition of Done

{checks}
"""
    decisions = """# Decisions

## D-001: Repository contract is current authorization

- Decision: Current scope, acceptance criteria, and delivery permissions come from the repository contract.
- Reason: Historical conversations can be stale or incomplete.
- Reconsider when: The project adopts another explicit, reviewable authorization system.
"""
    tasks = f"""# Tasks

Statuses: `planned`, `active`, `review`, `blocked`, `done`, `cancelled`.

| ID | Task | Status | Owner | Dependencies | Handoff |
|---|---|---|---|---|---|
| T-001 | {args.task_title} | active | {owner} | none | [handoffs/T-001.md](handoffs/T-001.md) |
"""
    handoff = f"""---
task_id: T-001
status: active
owner_role: {owner}
updated: {today}
---

# T-001: {args.task_title}

## Goal

{args.task_goal}

## Scope

{scope}

## Do not touch

{exclusions}

## Inputs

- Current user request and canonical repository state.

## Acceptance criteria

{checks}

## Git delivery

- repo_root: .
- base_branch: {args.base_branch}
- start_commit: {start_commit}
- work_branch: {branch}
- commit_policy: {args.commit_policy}
- push_policy: {args.push_policy}
- push_target: {args.push_target}
- integration_owner: {args.integration_owner}
- end_commit: pending
- dirty_state: inspect and record before implementation

## Attempts and evidence

- Contract initialized on {today}; implementation has not started.

## Files changed

- Contract files only.

## Current status

Contract initialized; validate it before implementation.

## Next step

Run the contract validator, then execute only T-001.
"""
    return {
        root / "AGENTS.md": agents,
        root / "PROJECT.md": project,
        root / "DECISIONS.md": decisions,
        root / "TASKS.md": tasks,
        root / "handoffs" / "T-001.md": handoff,
    }


def create_contract(args: argparse.Namespace) -> list[Path]:
    root = Path(args.root).resolve()
    files = render_files(args, root)
    existing = [path for path in files if path.exists()]
    if existing:
        joined = ", ".join(str(path.relative_to(root)) for path in existing)
        raise FileExistsError(f"refusing to overwrite existing contract files: {joined}")
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return list(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--project-title", required=True)
    parser.add_argument("--project-goal", required=True)
    parser.add_argument("--task-title", required=True)
    parser.add_argument("--task-goal", required=True)
    parser.add_argument("--scope", action="append", required=True)
    parser.add_argument(
        "--do-not-touch",
        action="append",
        default=["Unrelated files, credentials, and user data."],
    )
    parser.add_argument("--acceptance", action="append", required=True)
    parser.add_argument("--owner-role", default="primary coordinating agent")
    parser.add_argument("--base-branch", default="origin/main")
    parser.add_argument("--work-branch")
    parser.add_argument("--start-commit")
    parser.add_argument(
        "--commit-policy", choices=("required", "optional", "forbidden"), default="required"
    )
    parser.add_argument(
        "--push-policy",
        choices=("allowed", "approval_required", "forbidden"),
        default="approval_required",
    )
    parser.add_argument("--push-target", default="none")
    parser.add_argument("--integration-owner", default="user")
    args = parser.parse_args()
    try:
        created = create_contract(args)
    except FileExistsError as exc:
        print(exc)
        return 2
    for path in created:
        print(f"created {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
