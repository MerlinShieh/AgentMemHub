from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "conversation-hub"
    / "scripts"
    / "validate_project_contract.py"
)
INIT_SCRIPT = SCRIPT.with_name("init_project_contract.py")
SPEC = importlib.util.spec_from_file_location("validate_project_contract", SCRIPT)
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)
INIT_SPEC = importlib.util.spec_from_file_location("init_project_contract", INIT_SCRIPT)
assert INIT_SPEC and INIT_SPEC.loader
INITIALIZER = importlib.util.module_from_spec(INIT_SPEC)
INIT_SPEC.loader.exec_module(INITIALIZER)


HANDOFF = """---
task_id: T-001
status: active
owner_role: integrator
updated: 2026-08-16
---

# T-001: Fixture

## Goal
Ship one result.
## Scope
- fixture
## Do not touch
- vendor data
## Inputs
- repository
## Acceptance criteria
- [ ] validation passes
## Git delivery
- repo_root: C:/fixture
- base_branch: origin/main
- start_commit: abc1234
- work_branch: feature/test
- commit_policy: required
- push_policy: approval_required
- push_target: origin/feature/test
- integration_owner: user
- end_commit: pending
- dirty_state: clean
## Attempts and evidence
- none
## Files changed
- none
## Current status
Active.
## Next step
Validate.
"""


class ProjectContractTests(unittest.TestCase):
    def make_root(self, handoff: str = HANDOFF) -> Path:
        temp = tempfile.TemporaryDirectory(prefix="hub-contract-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ("AGENTS.md", "PROJECT.md", "DECISIONS.md"):
            (root / name).write_text(f"# {name}\n", encoding="utf-8")
        (root / "TASKS.md").write_text(
            "# Tasks\n\n"
            "| ID | Task | Status | Owner | Dependencies | Handoff |\n"
            "|---|---|---|---|---|---|\n"
            "| T-001 | fixture | active | integrator | none | handoffs/T-001.md |\n",
            encoding="utf-8",
        )
        (root / "handoffs").mkdir()
        (root / "handoffs" / "T-001.md").write_text(handoff, encoding="utf-8")
        return root

    def test_valid_contract(self) -> None:
        result = VALIDATOR.validate(self.make_root())
        self.assertTrue(result["valid"], result["errors"])

    def test_windows_line_endings_are_valid(self) -> None:
        result = VALIDATOR.validate(self.make_root(HANDOFF.replace("\n", "\r\n")))
        self.assertTrue(result["valid"], result["errors"])

    def test_missing_git_policy_is_rejected(self) -> None:
        handoff = HANDOFF.replace("- push_policy: approval_required\n", "")
        result = VALIDATOR.validate(self.make_root(handoff))
        self.assertFalse(result["valid"])
        self.assertIn("T-001.md: missing Git delivery field push_policy", result["errors"])

    def test_handoff_status_must_match_tasks(self) -> None:
        root = self.make_root(HANDOFF.replace("status: active", "status: review", 1))
        result = VALIDATOR.validate(root)
        self.assertFalse(result["valid"])
        self.assertIn(
            "T-001.md: status review does not match TASKS.md status active",
            result["errors"],
        )

    def test_multiple_active_tasks_on_same_branch_are_rejected(self) -> None:
        root = self.make_root()
        with (root / "TASKS.md").open("a", encoding="utf-8") as handle:
            handle.write("| T-002 | second | active | integrator | none | handoffs/T-002.md |\n")
        second = HANDOFF.replace("T-001", "T-002")
        (root / "handoffs" / "T-002.md").write_text(second, encoding="utf-8")
        result = VALIDATOR.validate(root)
        self.assertFalse(result["valid"])
        self.assertIn(
            "multiple active tasks share work_branch feature/test: T-001, T-002",
            result["errors"],
        )

    def test_active_tasks_on_distinct_branches_are_allowed(self) -> None:
        root = self.make_root()
        with (root / "TASKS.md").open("a", encoding="utf-8") as handle:
            handle.write("| T-002 | second | active | integrator | none | handoffs/T-002.md |\n")
        second = HANDOFF.replace("T-001", "T-002").replace(
            "feature/test", "feature/second"
        )
        (root / "handoffs" / "T-002.md").write_text(second, encoding="utf-8")
        result = VALIDATOR.validate(root)
        self.assertTrue(result["valid"], result["errors"])

    def test_initializer_creates_valid_contract_and_refuses_overwrite(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="hub-init-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        args = type(
            "Args",
            (),
            {
                "root": str(root),
                "project_title": "Fixture Project",
                "project_goal": "Ship a traceable fixture.",
                "task_title": "Build fixture",
                "task_goal": "Create one validated contract.",
                "scope": ["Contract files."],
                "do_not_touch": ["User data."],
                "acceptance": ["Validator passes."],
                "owner_role": "integrator",
                "base_branch": "origin/main",
                "work_branch": "feature/fixture",
                "start_commit": "abc1234",
                "commit_policy": "required",
                "push_policy": "approval_required",
                "push_target": "none",
                "integration_owner": "user",
            },
        )()
        created = INITIALIZER.create_contract(args)
        self.assertEqual(len(created), 5)
        result = VALIDATOR.validate(root)
        self.assertTrue(result["valid"], result["errors"])
        with self.assertRaises(FileExistsError):
            INITIALIZER.create_contract(args)


if __name__ == "__main__":
    unittest.main()
