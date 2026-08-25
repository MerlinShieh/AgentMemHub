from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


finder = load_module("find_agent_data", ROOT / "scripts" / "find_agent_data.py")
qoder = load_module("qoder_session_probe", ROOT / "scripts" / "qoder_session_probe.py")
grok = load_module("grok_session_probe", ROOT / "scripts" / "grok_session_probe.py")


class FinderTests(unittest.TestCase):
    def test_json_contract_and_context_only_detection(self):
        result = finder.collect_agent("qoder", probe=False, existing_only=False)
        self.assertEqual(result["id"], "qoder")
        self.assertEqual(result["confidence"], "verified")
        self.assertTrue(all("conversation_evidence" in item for item in result["locations"]))


class QoderProbeTests(unittest.TestCase):
    def test_maps_title_to_highest_coverage_candidate_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "local.db"
            compact_root = root / "compact"
            full_root = root / "full"
            compact_file = compact_root / "project" / "conversation-history" / "task-1" / "task-1.jsonl"
            full_file = full_root / "project" / "transcript" / "task-1234567890.session.execution.jsonl"
            compact_file.parent.mkdir(parents=True)
            full_file.parent.mkdir(parents=True)

            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE chat_session (session_id TEXT, session_title TEXT, project_name TEXT, gmt_modified INTEGER)"
                )
                conn.execute(
                    "INSERT INTO chat_session VALUES (?, ?, ?, ?)",
                    ("task-1234567890.session.execution", "继续codex项目优化", "demo", 10),
                )
                conn.commit()
            finally:
                conn.close()

            compact_events = [
                {"uuid": "u1", "message": {"role": "user", "content": "first goal"}},
                {"uuid": "a1", "message": {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]}},
                {
                    "uuid": "u2",
                    "message": {
                        "role": "user",
                        "content": "<system-reminder>ignore</system-reminder><user_query>old</user_query><user_query>latest request</user_query>",
                    },
                },
                {"uuid": "a2", "message": {"role": "assistant", "content": "latest answer"}},
            ]
            full_events = [
                {"uuid": "fu1", "data": {"role": "user", "content": "first goal"}},
                {"uuid": "fa1", "data": {"role": "assistant", "content": "partial answer"}},
            ]
            compact_file.write_text("\n".join(json.dumps(item) for item in compact_events), encoding="utf-8")
            full_file.write_text("\n".join(json.dumps(item) for item in full_events), encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = qoder.main(
                    [
                        "--query",
                        "继续codex",
                        "--index-db",
                        str(db),
                        "--compact-root",
                        str(compact_root),
                        "--full-root",
                        str(full_root),
                        "--preview",
                        "--json",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            chosen = payload["sessions"][0]["selected_transcript"]

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["schema"], "find-agent-data/qoder-map-v1")
            self.assertEqual(chosen["message_count"], 4)
            self.assertTrue(chosen["path"].endswith("task-1.jsonl"))
            self.assertEqual(chosen["preview"]["latest_user"]["text"], "latest request")
            self.assertEqual(chosen["last_evidence"]["event_id"], "a2")
            self.assertEqual(len(chosen["last_evidence"]["sha256_16"]), 16)

    def test_exact_id_can_recover_transcript_without_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "local.db"
            compact_root = root / "compact"
            full_root = root / "full"
            transcript = full_root / "transcript" / "orphan-session.jsonl"
            transcript.parent.mkdir(parents=True)
            compact_root.mkdir()
            transcript.write_text(
                json.dumps({"id": "event-1", "message": {"role": "user", "content": "recover me"}}),
                encoding="utf-8",
            )
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE chat_session (session_id TEXT, session_title TEXT)")
                conn.commit()
            finally:
                conn.close()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = qoder.main(
                    [
                        "--session-id",
                        "orphan-session",
                        "--index-db",
                        str(db),
                        "--compact-root",
                        str(compact_root),
                        "--full-root",
                        str(full_root),
                        "--json",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["sessions"][0]["source_kind"], "transcript")
            self.assertEqual(payload["sessions"][0]["selected_transcript"]["message_count"], 1)


class SharedLayoutTests(unittest.TestCase):
    def test_qodercn_layout_includes_cli_full_root(self):
        from agent_recovery.qoder import layout

        roots = layout("qodercn").transcript_roots
        self.assertTrue(any("SharedClientCache" in str(path) and "cli" in str(path) for path, kind in roots if kind == "full_transcript"))
        finder_paths = {str(item["path"]) for item in finder.collect_agent("qodercn", probe=False, existing_only=False)["locations"]}
        self.assertTrue(any("cli" in path and "projects" in path for path in finder_paths))

    def test_recover_query_stays_inside_an_explicit_index(self):
        from agent_recovery.qoder import recover_query

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "local.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE chat_session (session_id TEXT, session_title TEXT)")
                conn.execute("INSERT INTO chat_session VALUES (?, ?)", ("only-fixture", "fixture title"))
                conn.commit()
            finally:
                conn.close()
            payload = recover_query(
                "qoder",
                query="fixture",
                configured_index=db,
                compact_root=root / "compact",
                full_root=root / "full",
            )
            self.assertEqual(["only-fixture"], [item["metadata"]["session_id"] for item in payload["sessions"]])
            self.assertEqual("metadata_only", payload["sessions"][0]["source_kind"])


class GrokProbeTests(unittest.TestCase):
    def test_finder_marks_search_db_as_context_only(self):
        result = finder.collect_agent("grok", probe=False, existing_only=False)
        self.assertEqual(result["id"], "grok")
        roles = {item["role"]: item["conversation_evidence"] for item in result["locations"]}
        self.assertTrue(roles["transcript_root"])
        self.assertFalse(roles["session_index"])
        self.assertFalse(roles["runtime_root"])

    def test_maps_title_to_updates_and_skips_subagents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            main = home / "sessions" / "workspace" / "sess-main"
            child = main / "subagents" / "child"
            main.mkdir(parents=True)
            child.mkdir(parents=True)
            (main / "summary.json").write_text(
                json.dumps({
                    "info": {"id": "sess-main", "cwd": "C:/work/demo"},
                    "generated_title": "对话中心 Grok fixture",
                    "created_at": "2026-08-14T00:00:00Z",
                    "updated_at": "2026-08-14T00:01:00Z",
                    "current_model_id": "grok-4.6",
                }),
                encoding="utf-8",
            )
            (main / "updates.jsonl").write_text(
                "\n".join([
                    json.dumps({
                        "params": {
                            "update": {
                                "sessionUpdate": "user_message_chunk",
                                "content": {"type": "text", "text": "Hello "},
                                "_meta": {"eventId": "u1"},
                            }
                        }
                    }),
                    json.dumps({
                        "params": {
                            "update": {
                                "sessionUpdate": "user_message_chunk",
                                "content": {"type": "text", "text": "Grok"},
                            }
                        }
                    }),
                    json.dumps({
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_thought_chunk",
                                "content": {"type": "text", "text": "hidden thought"},
                            }
                        }
                    }),
                    json.dumps({
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "Hi there"},
                                "_meta": {"eventId": "a1"},
                            }
                        }
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            (child / "summary.json").write_text(
                json.dumps({
                    "info": {"id": "child", "cwd": "C:/work/demo"},
                    "generated_title": "hidden child",
                    "parent_session_id": "sess-main",
                }),
                encoding="utf-8",
            )
            (child / "updates.jsonl").write_text(
                json.dumps({
                    "params": {
                        "update": {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": "child work"},
                        }
                    }
                }) + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = grok.main(
                    ["--query", "对话中心", "--home", str(home), "--preview", "--json"]
                )
            payload = json.loads(stdout.getvalue())
            chosen = payload["sessions"][0]["selected_transcript"]
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["schema"], "find-agent-data/grok-map-v1")
            self.assertEqual(1, payload["matched_sessions"])
            self.assertEqual("Hello Grok", chosen["preview"]["latest_user"]["text"])
            self.assertEqual("Hi there", chosen["preview"]["latest_assistant_after_user"]["text"])
            self.assertEqual(2, chosen["message_count"])
            self.assertEqual("u1", chosen["first_evidence"]["event_id"])


if __name__ == "__main__":
    unittest.main()
