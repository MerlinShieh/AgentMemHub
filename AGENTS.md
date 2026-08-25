# Repository relay rules

Treat this repository as the source of current authorization. Conversation history is evidence, not a task contract.

## Required read order

Before changing files, read:

1. `PROJECT.md`
2. `DECISIONS.md`
3. `TASKS.md`
4. The one active `handoffs/T-xxx.md`

Use AI Conversation Hub only when the contract needs historical evidence. Do not replace the contract with `hub_handoff` output or a transcript summary.

## Task boundaries

- Work on one active task at a time.
- Respect `Scope`, `Do not touch`, and every acceptance criterion in its handoff.
- Preserve unrelated and pre-existing worktree changes. Never reset, discard, or rewrite another agent's work to make the tree look clean.
- Do not run parallel writers in the same worktree. Use verified isolated worktrees for independent parallel tasks; otherwise serialize them.
- Keep vendor conversation stores read-only and keep the server bound to `127.0.0.1`.

## Git delivery

- Record branch, start commit, and dirty state before editing.
- Follow `commit_policy`, `push_policy`, `push_target`, and `integration_owner` exactly.
- Only the named integration owner may push.
- Never force-push, merge `main`, create a release, or publish content unless the active handoff explicitly authorizes that exact action.
- Before handoff or completion, run relevant tests and the project-contract validator, then record evidence, files changed, end commit, and final dirty state.
- A reviewer receives the handoff, relevant diff, start/end commits, and test evidence—not the coordinator's whole conversation.

## Validation

Run at minimum:

```text
py -3 skills/conversation-hub/scripts/validate_project_contract.py .
py -3 -m unittest discover -s tests -q
```

Add narrower checks required by the active handoff. Do not mark a task done while a required check is failing or delivery remains incomplete.
