# Project relay contract

Use this reference only when a repository needs a new or repaired cross-agent contract.

## Required repository files

```text
AGENTS.md
PROJECT.md
DECISIONS.md
TASKS.md
handoffs/T-001.md
```

- `AGENTS.md`: repository-wide read order, safety boundaries, validation, and Git delivery rules.
- `PROJECT.md`: stable objective, scope, architecture, and Definition of Done.
- `DECISIONS.md`: accepted and rejected choices with reasons and reconsideration triggers.
- `TASKS.md`: small current tasks, dependencies, owners, and status.
- `handoffs/T-xxx.md`: the authorization and evidence record for one task.

## When to initialize

Initialize the contract before implementation when the repository work will span agents, sessions, separately reviewable tasks, or multiple delivery phases such as implementation plus release. The first coordinating agent owns initialization unless the repository names another contract steward.

Do not initialize it for a single small edit or a read-only inspection. If only part of the contract exists, preserve it and repair missing pieces; never overwrite decisions merely to fit a template.

The bundled initializer creates a complete first task without overwriting existing contract files. Supply concrete values derived from the user's current request:

```text
py -3 skills/conversation-hub/scripts/init_project_contract.py <repo> \
  --project-title "Project name" \
  --project-goal "Observable project outcome" \
  --task-title "First bounded task" \
  --task-goal "Observable task outcome" \
  --scope "Allowed files or modules" \
  --acceptance "Objective completion check"
```

Run `validate_project_contract.py` after creation and before implementation.

## Lifecycle ownership

- The coordinating agent is contract steward: it keeps `PROJECT.md`, `DECISIONS.md`, and `TASKS.md` coherent.
- The active task owner maintains its own handoff before editing and after every material state or owner change.
- The integration owner records commit, push, merge, tag, deployment, or release evidence; possessing credentials is not authorization.
- A reviewer changes a task to `review` or returns evidence; only the authorized owner/integrator records final delivery and `done`.
- Keep at most one `active` task in a shared worktree. Independent writers require verified isolated worktrees.

## Handoff template

```markdown
---
task_id: T-001
status: active
owner_role: worker
updated: YYYY-MM-DD
---

# T-001: Short task title

## Goal

One observable outcome.

## Scope

- Files or modules this task may change.

## Do not touch

- Explicit exclusions and protected systems.

## Inputs

- Repository facts and optional Hub evidence references.

## Acceptance criteria

- [ ] Objective check.

## Git delivery

- repo_root: canonical repository path
- base_branch: branch used as the comparison base
- start_commit: full or short commit at task start
- work_branch: branch receiving this task
- commit_policy: required | optional | forbidden
- push_policy: allowed | approval_required | forbidden
- push_target: remote/branch or none
- integration_owner: named agent role or user
- end_commit: commit containing the delivered work, or pending
- dirty_state: clean, expected paths, or acknowledged pre-existing changes

## Attempts and evidence

- Commands, tests, review findings, and blockers.

## Files changed

- Paths changed by this task.

## Current status

What is true now; do not claim completion before acceptance passes.

## Next step

One concrete next action and its owner, or `None — complete`.
```

## Status and policy values

- Task status: `planned`, `active`, `review`, `blocked`, `done`, `cancelled`.
- Commit policy: `required`, `optional`, `forbidden`.
- Push policy: `allowed`, `approval_required`, `forbidden`.

`allowed` authorizes only the named integration owner and push target. It does not authorize force-push, a release, or a merge into another branch.

## Review packet

Give an independent reviewer:

1. This handoff.
2. The relevant diff.
3. Start and end commits.
4. Test and validation evidence.

Do not give the full coordinator transcript unless a specific disputed fact requires source evidence.
