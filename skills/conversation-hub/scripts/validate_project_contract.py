#!/usr/bin/env python3
"""Validate the minimal repository contract used for cross-agent relay."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REQUIRED_ROOT_FILES = ("AGENTS.md", "PROJECT.md", "DECISIONS.md", "TASKS.md")
REQUIRED_FRONTMATTER = ("task_id", "status", "owner_role", "updated")
REQUIRED_SECTIONS = (
    "Goal",
    "Scope",
    "Do not touch",
    "Inputs",
    "Acceptance criteria",
    "Git delivery",
    "Attempts and evidence",
    "Files changed",
    "Current status",
    "Next step",
)
REQUIRED_GIT_FIELDS = (
    "repo_root",
    "base_branch",
    "start_commit",
    "work_branch",
    "commit_policy",
    "push_policy",
    "push_target",
    "integration_owner",
    "end_commit",
    "dirty_state",
)
TASK_STATUSES = {"planned", "active", "review", "blocked", "done", "cancelled"}
COMMIT_POLICIES = {"required", "optional", "forbidden"}
PUSH_POLICIES = {"allowed", "approval_required", "forbidden"}


def parse_frontmatter(text: str) -> dict[str, str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def markdown_sections(text: str) -> set[str]:
    text = text.replace("\r\n", "\n")
    return {
        match.group(1).strip()
        for match in re.finditer(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE)
    }


def section_body(text: str, section: str) -> str:
    text = text.replace("\r\n", "\n")
    match = re.search(
        rf"^##\s+{re.escape(section)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        text,
        flags=re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def task_rows(text: str) -> tuple[dict[str, str], list[str]]:
    rows: dict[str, str] = {}
    errors: list[str] = []
    for line in text.replace("\r\n", "\n").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not re.fullmatch(r"T-\d+", cells[0]):
            continue
        task_id, status = cells[0], cells[2]
        if task_id in rows:
            errors.append(f"TASKS.md: duplicate task {task_id}")
        rows[task_id] = status
        if status not in TASK_STATUSES:
            errors.append(f"TASKS.md: {task_id} has invalid status {status}")
    if not rows:
        errors.append("TASKS.md: no task rows found")
    return rows, errors


def git_fields(text: str) -> dict[str, str]:
    text = text.replace("\r\n", "\n")
    match = re.search(
        r"^##\s+Git delivery\s*$([\s\S]*?)(?=^##\s+|\Z)",
        text,
        flags=re.MULTILINE,
    )
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        item = re.match(r"^\s*-\s+([a-z_]+):\s*(.*?)\s*$", line)
        if item:
            values[item.group(1)] = item.group(2)
    return values


def validate(root: Path) -> dict[str, object]:
    errors: list[str] = []
    for name in REQUIRED_ROOT_FILES:
        if not (root / name).is_file():
            errors.append(f"missing root contract file: {name}")

    handoff_dir = root / "handoffs"
    handoffs = sorted(handoff_dir.glob("T-*.md")) if handoff_dir.is_dir() else []
    if not handoffs:
        errors.append("missing handoff files: handoffs/T-*.md")

    task_path = root / "TASKS.md"
    task_text = task_path.read_text(encoding="utf-8") if task_path.is_file() else ""
    tasks, task_errors = task_rows(task_text)
    errors.extend(task_errors)
    checked: list[str] = []
    active_work_branches: dict[str, list[str]] = {}
    for path in handoffs:
        text = path.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(text)
        for key in REQUIRED_FRONTMATTER:
            if not frontmatter.get(key):
                errors.append(f"{path.name}: missing frontmatter field {key}")
        task_id = frontmatter.get("task_id", path.stem)
        if task_id not in tasks:
            errors.append(f"{path.name}: {task_id} is not listed in TASKS.md")
        status = frontmatter.get("status")
        if status and status not in TASK_STATUSES:
            errors.append(f"{path.name}: invalid status {status}")
        if task_id in tasks and status and tasks[task_id] != status:
            errors.append(
                f"{path.name}: status {status} does not match TASKS.md status {tasks[task_id]}"
            )

        sections = markdown_sections(text)
        for section in REQUIRED_SECTIONS:
            if section not in sections:
                errors.append(f"{path.name}: missing section {section}")
            elif not section_body(text, section):
                errors.append(f"{path.name}: empty section {section}")

        criteria = section_body(text, "Acceptance criteria")
        boxes = re.findall(r"^\s*-\s+\[([ xX])\]\s+", criteria, flags=re.MULTILINE)
        if criteria and not boxes:
            errors.append(f"{path.name}: acceptance criteria need Markdown checkboxes")
        if status == "done" and any(mark == " " for mark in boxes):
            errors.append(f"{path.name}: done task has unchecked acceptance criteria")

        delivery = git_fields(text)
        for key in REQUIRED_GIT_FIELDS:
            if not delivery.get(key):
                errors.append(f"{path.name}: missing Git delivery field {key}")
        if delivery.get("commit_policy") not in COMMIT_POLICIES:
            errors.append(f"{path.name}: invalid commit_policy {delivery.get('commit_policy', '')}")
        if delivery.get("push_policy") not in PUSH_POLICIES:
            errors.append(f"{path.name}: invalid push_policy {delivery.get('push_policy', '')}")
        if status == "done" and delivery.get("end_commit") == "pending":
            errors.append(f"{path.name}: done task has pending end_commit")
        work_branch = delivery.get("work_branch")
        if status == "active" and work_branch and work_branch not in {"none", "pending"}:
            active_work_branches.setdefault(work_branch, []).append(task_id)
        checked.append(path.relative_to(root).as_posix())

    for work_branch, task_ids in active_work_branches.items():
        if len(task_ids) > 1:
            errors.append(
                f"multiple active tasks share work_branch {work_branch}: "
                f"{', '.join(task_ids)}"
            )

    for task_id, status in tasks.items():
        if status != "planned" and not (handoff_dir / f"{task_id}.md").is_file():
            errors.append(f"TASKS.md: {task_id} status {status} requires handoffs/{task_id}.md")

    return {
        "schema": "conversation-hub/project-contract-v1",
        "root": str(root.resolve()),
        "valid": not errors,
        "handoffs_checked": checked,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="repository root")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()
    result = validate(Path(args.root))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["valid"]:
        print(f"project contract valid: {result['root']}")
        for item in result["handoffs_checked"]:
            print(f"  checked {item}")
    else:
        print(f"project contract invalid: {result['root']}", file=sys.stderr)
        for error in result["errors"]:
            print(f"  - {error}", file=sys.stderr)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
