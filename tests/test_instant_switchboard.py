from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TEST_DATA = tempfile.mkdtemp(prefix="hub-unit-data-")
os.environ.setdefault("CONVERSATION_HUB_DATA_DIR", TEST_DATA)

import source_adapters  # noqa: E402
import desktop_app  # noqa: E402
from server import (  # noqa: E402
    ConflictError,
    Conversation,
    ConversationIndex,
    build_continuation_packet,
    build_conversation_review,
    classify_transcript_message,
    continuation_packet_markdown,
    conversation_review_markdown,
    discover_grok_executable,
    discover_grok_launcher,
    grok_launch_env,
    grok_launch_preflight,
    launch_grok_cli,
    launch_new_cli,
    _windows_cmd_k_line,
    is_internal_noise_message,
    launch_server_target,
    launch_targets_for,
    overview_same_line,
    overview_snippet,
    resume_descriptor,
    build_agent_handoff,
    extract_codex_user_text,
    iter_codex_visible_messages,
)


def sample_conversation(source: str, session_id: str = "session-123456") -> Conversation:
    return Conversation(
        source=source,
        id=session_id,
        title="Fixture",
        preview="Prompt",
        cwd="C:/fixture",
        workspace="fixture",
        created_at=1,
        updated_at=2,
        message_count=2,
        tool_call_count=0,
        model="",
        archived=False,
        status="today",
        source_kind=f"{source}-fixture",
    )


class InternalNoiseTests(unittest.TestCase):
    def test_hides_hermes_bookkeeping_and_keeps_real_turns(self) -> None:
        self.assertTrue(is_internal_noise_message("assistant", "内存满了，批量精简+更新一起做："))
        self.assertTrue(is_internal_noise_message("user", "[System: The active model for this chat has changed to glm-5.3]", "model_switch"))
        self.assertTrue(is_internal_noise_message("user", "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below."))
        self.assertFalse(is_internal_noise_message("assistant", "开始批量修改。先处理 models.py 的 4 处修改："))
        self.assertFalse(is_internal_noise_message("user", "我想说怎么最方便打开grok过去的对话呢"))

    def test_classifies_mid_turn_progress_without_dropping_user_facing(self) -> None:
        self.assertEqual(
            classify_transcript_message(
                "assistant",
                "35044 已成功杀掉。现在只剩你的 4 个真实窗口。再用正确方式启动可见的 resume 窗口：",
            ),
            "progress",
        )
        self.assertEqual(
            classify_transcript_message("assistant", "等几秒确认它是否成功恢复："),
            "progress",
        )
        self.assertEqual(
            classify_transcript_message("assistant", "达令菁，搞定！先不折腾内存了，跟你说结果——"),
            "visible",
        )
        self.assertEqual(
            classify_transcript_message("assistant", "开始批量修改。先处理 models.py 的 4 处修改："),
            "visible",
        )
        self.assertEqual(
            classify_transcript_message("assistant", "内存满了，批量精简+更新一起做："),
            "system",
        )


class OverviewSnippetTests(unittest.TestCase):
    def test_takes_first_prose_line_and_skips_tables(self) -> None:
        text = (
            "达令菁，搞定了！两个窗口都已经开好了。\n\n"
            "| # | PID | 内存 |\n|---|---|---|\n| 1 | 44568 | ~87 MB |\n"
        )
        self.assertEqual(overview_snippet(text, 80), "达令菁，搞定了！两个窗口都已经开好了。")
        self.assertTrue(overview_same_line("再帮我开一个窗口", "再帮我开一个窗口冲额度"))
        self.assertFalse(overview_same_line("再帮我开一个窗口", "做了吧"))


class InstantIndexTests(unittest.TestCase):
    def test_running_port_skips_ports_that_can_be_bound(self) -> None:
        class BindableProbe:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def bind(self, _address):
                return None

        with mock.patch.object(desktop_app.socket, "socket", return_value=BindableProbe()):
            with mock.patch.object(desktop_app, "health") as health:
                self.assertIsNone(desktop_app.running_port())
        health.assert_not_called()

    def test_running_port_health_checks_only_an_in_use_port(self) -> None:
        attempts = 0

        class Probe:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def bind(self, _address):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("in use")

        with mock.patch.object(desktop_app.socket, "socket", return_value=Probe()):
            with mock.patch.object(desktop_app, "health", return_value=True) as health:
                self.assertEqual(8765, desktop_app.running_port())
        health.assert_called_once_with(8765)

    def test_index_can_be_constructed_without_blocking_refresh(self) -> None:
        index = ConversationIndex(refresh_on_init=False)
        self.assertEqual("pending", index.initial_state()["status"])
        self.assertEqual([], index._items)

    def test_launch_targets_are_honest_about_exactness(self) -> None:
        codex = launch_targets_for(sample_conversation("codex"))[0]
        self.assertTrue(codex["exact"])
        self.assertEqual("deep_link", codex["kind"])
        self.assertTrue(codex["href"].startswith("codex://threads/"))

        hermes = launch_targets_for(sample_conversation("hermes"))
        self.assertEqual("hermes-app", hermes[0]["target_id"])
        self.assertEqual("client", hermes[0]["capability"])
        self.assertEqual("app_link", hermes[0]["kind"])
        self.assertEqual("hermes://", hermes[0]["href"])
        self.assertFalse(hermes[0]["exact"])

        with tempfile.TemporaryDirectory() as raw:
            transcript = Path(raw) / "session-123456.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            claude_item = sample_conversation("claude")
            claude_item.source_kind = "claude-jsonl"
            claude_item.rollout_path = str(transcript)
            claude_exe = Path("/opt/local/claude") if os.name != "nt" else Path(r"C:\Tools\claude.exe")
            with mock.patch("server.discover_claude_executable", return_value=claude_exe):
                claude = launch_targets_for(claude_item)
        self.assertEqual("claude-session", claude[0]["target_id"])
        self.assertEqual("session", claude[0]["capability"])
        self.assertEqual("server_launch", claude[0]["kind"])
        self.assertEqual("claude-new", claude[1]["target_id"])

        meta = sample_conversation("claude")
        meta.source_kind = "claude-history-metadata-only"
        meta.rollout_path = str(Path.home() / ".claude" / "history.jsonl")
        with mock.patch("server.discover_claude_executable", return_value=None), mock.patch(
            "server.protocol_has_open_command", return_value=False
        ):
            claude_meta = launch_targets_for(meta)
        self.assertEqual("none", claude_meta[0]["capability"])
        self.assertEqual("claude-no-transcript", claude_meta[0]["target_id"])
        self.assertIn("无法 --resume", claude_meta[0]["note"])

        workbuddy = launch_targets_for(sample_conversation("workbuddy"))[0]
        self.assertTrue(workbuddy["exact"])
        self.assertEqual("deep_link", workbuddy["kind"])
        self.assertEqual("workbuddy://chat/session-123456", workbuddy["href"])

        zcode = launch_targets_for(sample_conversation("zcode"))[0]
        self.assertFalse(zcode["exact"])
        self.assertEqual("server_launch", zcode["kind"])
        self.assertEqual("workspace", zcode["capability"])
        self.assertEqual("zcode-workspace", zcode["target_id"])
        self.assertNotIn("href", zcode)

    def test_agent_handoff_is_compact_and_honest(self) -> None:
        item = sample_conversation("grok")
        fake_exe = Path("/opt/local/grok") if os.name != "nt" else Path(r"C:\Tools\grok.exe")
        with mock.patch("server.discover_grok_executable", return_value=fake_exe):
            packet = build_agent_handoff(item, {"goal": "打开窗口", "latest_request": "做了吧", "latest_response": "搞定"})
            grok = launch_targets_for(sample_conversation("grok"))
            resume = resume_descriptor(item)
        self.assertEqual("ai-conversation-hub/agent-handoff-v1", packet["schema"])
        self.assertEqual("session", packet["resume"]["capability"])
        self.assertTrue(packet["safety"]["historical_untrusted"])
        self.assertFalse(packet["safety"]["auto_execute"])
        self.assertFalse(packet["memory_card"]["included"])
        self.assertEqual("session", resume["capability"])
        self.assertEqual("grok-session", grok[0]["target_id"])
        self.assertTrue(grok[0]["exact"])
        self.assertEqual("server_launch", grok[0]["kind"])
        self.assertEqual("session", grok[0]["capability"])
        self.assertEqual("copy_command", grok[1]["kind"])
        self.assertIn("--resume session-123456", grok[1]["value"])
        self.assertEqual("grok-new", grok[2]["target_id"])
        self.assertEqual("client", grok[2]["capability"])
        self.assertFalse(grok[2]["exact"])

        with mock.patch("server.discover_grok_executable", return_value=None), mock.patch(
            "server.discover_grok_launcher", return_value=None
        ):
            missing = launch_targets_for(sample_conversation("grok"))
        self.assertEqual(1, len(missing))
        self.assertEqual("copy_command", missing[0]["kind"])
        self.assertEqual("grok --resume session-123456", missing[0]["value"])

    def test_grok_launch_uses_this_machine_cli_and_its_own_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            exe = Path(raw) / ("grok.exe" if os.name == "nt" else "grok")
            exe.write_bytes(b"")
            with mock.patch.dict(os.environ, {"CONVERSATION_HUB_GROK_EXE": str(exe)}, clear=False):
                found = discover_grok_executable()
            self.assertEqual(found, exe.resolve())

            dead = "http://127.0.0.1:65530"
            their_proxy = "http://127.0.0.1:18888"
            with mock.patch("server._tcp_open", return_value=False):
                stripped = grok_launch_env({"HTTPS_PROXY": dead, "PATH": "/old/grok"})
            self.assertNotIn("HTTPS_PROXY", stripped)

            with mock.patch("server._tcp_open", side_effect=lambda host, port, timeout=0.2: port == 18888):
                kept = grok_launch_env({"HTTPS_PROXY": their_proxy})
                configured = grok_launch_env({}, config={"proxy": their_proxy})
            self.assertEqual(kept["HTTPS_PROXY"], their_proxy)
            self.assertEqual(configured["HTTP_PROXY"], their_proxy)

            item = sample_conversation("grok")
            fake = mock.Mock(pid=4242)
            with mock.patch("server.discover_grok_shortcut", return_value=None), mock.patch(
                "server.discover_grok_launcher", return_value=None
            ), mock.patch("server.discover_grok_executable", return_value=exe.resolve()), mock.patch(
                "server._popen_detached", return_value=fake
            ) as popen, mock.patch("server.grok_launch_env", return_value={"HTTPS_PROXY": their_proxy}), mock.patch(
                "server.grok_launch_preflight", return_value=""
            ):
                result = launch_server_target(item, "grok-session")
            self.assertEqual(4242, result["pid"])
            self.assertTrue(result["exact"])
            command = popen.call_args.args[0]
            self.assertEqual(command[0], str(exe.resolve()))
            self.assertEqual(command[1:], ["--resume", "session-123456"])
            self.assertEqual(popen.call_args.kwargs["env"]["HTTPS_PROXY"], their_proxy)
            self.assertIn(str(exe.parent.resolve()), popen.call_args.kwargs["env"]["PATH"])

            socks = {"HTTP_PROXY": their_proxy, "ALL_PROXY": "socks5://127.0.0.1:18888"}
            with mock.patch("server._tcp_open", side_effect=lambda host, port, timeout=0.2: port == 18888):
                cleaned = grok_launch_env(socks)
            self.assertEqual(cleaned["ALL_PROXY"], "socks5://127.0.0.1:18888")
            self.assertEqual(cleaned["HTTP_PROXY"], their_proxy)

            with mock.patch("server._tcp_open", return_value=False):
                self.assertIn("没在听", grok_launch_preflight({"HTTPS_PROXY": dead}))
            with mock.patch("server._tcp_open", return_value=True):
                self.assertEqual("", grok_launch_preflight({"HTTPS_PROXY": their_proxy}))

            with mock.patch("server.discover_grok_shortcut", return_value=None), mock.patch(
                "server.discover_grok_launcher", return_value=None
            ), mock.patch("server.discover_grok_executable", return_value=exe.resolve()), mock.patch(
                "server._popen_detached", return_value=fake
            ) as new_popen, mock.patch("server.grok_launch_env", return_value={"HTTPS_PROXY": their_proxy}), mock.patch(
                "server.grok_launch_preflight", return_value=""
            ):
                opened = launch_new_cli("grok")
            self.assertFalse(opened["exact"])
            self.assertEqual([str(exe.resolve())], new_popen.call_args.args[0])

            launcher = Path(raw) / "launch-grok-build.cmd"
            launcher.write_text("@echo off\n", encoding="ascii")
            with mock.patch.dict(os.environ, {"CONVERSATION_HUB_GROK_LAUNCHER": str(launcher)}, clear=False):
                self.assertEqual(discover_grok_launcher(), launcher.resolve())
            with mock.patch("server.discover_grok_launcher", return_value=launcher.resolve()), mock.patch(
                "server.grok_launch_preflight", return_value=""
            ), mock.patch("server._windows_explorer_start", return_value=fake) as started:
                launch_grok_cli(extra_args=["--resume", "session-123456"], cwd=raw)
            started.assert_called_once()
            self.assertEqual(started.call_args.args[0], launcher.resolve())
            self.assertEqual(started.call_args.args[1], ["--resume", "session-123456"])

    def test_hermes_and_claude_open_the_registered_client(self) -> None:
        item = sample_conversation("hermes")
        with mock.patch("server.protocol_has_open_command", return_value=True), mock.patch(
            "server._windows_shell_open"
        ) as opener:
            result = launch_server_target(item, "hermes-app")
        self.assertFalse(result["exact"])
        self.assertEqual("hermes-app", result["target_id"])
        opener.assert_called_once_with("hermes://")

        with mock.patch("server.protocol_has_open_command", return_value=False):
            with self.assertRaises(ValueError):
                launch_server_target(sample_conversation("claude"), "claude-app")

    def test_windows_console_keeps_window_and_can_pass_proxy(self) -> None:
        line = _windows_cmd_k_line(
            [r"C:\Tools\claude.exe", "--resume", "session-123456"],
            {"HTTPS_PROXY": "http://127.0.0.1:18888"},
        )
        self.assertIn("claude.exe", line)
        self.assertIn("--resume session-123456", line)
        self.assertIn('set "HTTPS_PROXY=http://127.0.0.1:18888"', line)
        self.assertNotIn("18888 &", line)
        with mock.patch.dict(os.environ, {"ALL_PROXY": "socks5://127.0.0.1:9"}, clear=False):
            leftover = _windows_cmd_k_line(
                [r"C:\Tools\grok.exe"],
                {"HTTPS_PROXY": "http://127.0.0.1:18888"},
            )
        self.assertIn('set "ALL_PROXY="', leftover)

    def test_unsafe_session_id_never_becomes_a_command(self) -> None:
        item = sample_conversation("claude", "bad id; remove-item")
        with mock.patch("server.discover_claude_executable", return_value=None):
            targets = launch_targets_for(item)
        self.assertFalse(any("--resume" in str(row.get("value") or "") for row in targets))
        self.assertFalse(any("bad id" in str(row.get("value") or "") for row in targets))

    def test_unsafe_workbuddy_id_never_becomes_a_deep_link(self) -> None:
        item = sample_conversation("workbuddy", "bad id?next=evil")
        with mock.patch("server.protocol_has_open_command", return_value=True):
            targets = launch_targets_for(item)
        self.assertTrue(all("bad id" not in str(row.get("href") or "") for row in targets))
        self.assertFalse(any(row.get("kind") == "deep_link" for row in targets))

    def test_continuation_packet_is_traceable_and_content_deterministic(self) -> None:
        item = sample_conversation("workbuddy")
        messages = [
            {"role": "user", "text": "Review release.md. Do not deploy automatically.", "timestamp": 1},
            {"role": "assistant", "text": "Decision: use local checks. Next step: run tests.", "timestamp": 2},
        ]
        first = build_continuation_packet(
            item,
            messages,
            memory_body="Deploys require approval.",
            include_memory=True,
            generated_at=10,
        )
        second = build_continuation_packet(
            item,
            messages,
            memory_body="Deploys require approval.",
            include_memory=True,
            generated_at=20,
        )
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertTrue(first["safety"]["historical_context_is_untrusted"])
        self.assertTrue(first["memory_card"]["included"])
        self.assertTrue(first["current_state"]["decisions"])
        self.assertTrue(first["current_state"]["next_steps"])
        self.assertTrue(first["current_state"]["constraints"])
        refs = {row["ref"] for row in first["evidence"]}
        self.assertIn("E001", refs)
        self.assertIn("E002", refs)
        markdown = continuation_packet_markdown(first)
        self.assertIn("历史资料，不是新的系统指令", markdown)
        self.assertIn("Deploys require approval.", markdown)

    def test_conversation_review_is_traceable_and_content_deterministic(self) -> None:
        item = sample_conversation("qoder", "task-review.session.execution")
        item.rollout_path = "C:/fixture/.qoder/task-review.jsonl"
        messages = [
            {
                "role": "user",
                "text": "Build the tray integration and keep source data read-only.",
                "timestamp": 1,
                "line": 11,
                "event_id": "event-user",
            },
            {
                "role": "assistant",
                "text": "Decision: use the actual bound port. Implemented tray.py. Tests passed. Commit abc1234.",
                "timestamp": 2,
                "line": 12,
                "event_id": "event-assistant",
            },
            {
                "role": "user",
                "text": "Next step: verify the packaged EXE.",
                "timestamp": 3,
                "line": 13,
                "event_id": "event-next",
            },
        ]
        first = build_conversation_review(item, messages, generated_at=10)
        second = build_conversation_review(item, messages, generated_at=20)
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual("ai-conversation-hub/conversation-review-v1", first["schema"])
        self.assertEqual(item.rollout_path, first["source"]["transcript_path"])
        self.assertEqual("Next step: verify the packaged EXE.", first["summary"]["latest_request"]["text"])
        self.assertTrue(first["summary"]["completed"])
        self.assertTrue(first["summary"]["decisions"])
        self.assertEqual("abc1234", first["summary"]["commits"][0]["commit"])
        evidence = {row["ref"]: row for row in first["evidence"]}
        referenced = {
            ref
            for key in ("original_goal", "latest_request", "latest_response")
            for ref in first["summary"][key]["evidence"]
        }
        self.assertTrue(referenced.issubset(evidence))
        self.assertEqual(13, evidence["R003"]["line"])
        self.assertEqual("event-next", evidence["R003"]["event_id"])
        markdown = conversation_review_markdown(first)
        self.assertIn("line 13", markdown)
        self.assertIn(item.rollout_path, markdown)

    def test_memory_card_uses_optimistic_concurrency_and_can_be_cleared(self) -> None:
        index = ConversationIndex(refresh_on_init=False)
        item = sample_conversation("workbuddy")
        index._by_key[(item.source, item.id)] = item
        saved = index.save_continuation_memory({
            "source": item.source,
            "conversation_id": item.id,
            "body": "Always ask before deployment.",
            "expected_updated_at": 0,
        })
        self.assertGreater(saved["updated_at"], 0)
        with self.assertRaises(ConflictError):
            index.save_continuation_memory({
                "source": item.source,
                "conversation_id": item.id,
                "body": "Stale write",
                "expected_updated_at": 0,
            })
        cleared = index.save_continuation_memory({
            "source": item.source,
            "conversation_id": item.id,
            "body": "",
            "expected_updated_at": saved["updated_at"],
        })
        self.assertEqual(0, cleared["updated_at"])

    def test_persistent_search_skips_unchanged_conversations(self) -> None:
        index = ConversationIndex(refresh_on_init=False)
        item = sample_conversation("codex", "incremental-123")
        calls = 0

        def messages(_item, start=None, end=None, limit=None):
            nonlocal calls
            calls += 1
            return [{"role": "user", "text": "incremental fixture", "timestamp": 1}]

        index._messages_for_item = messages  # type: ignore[method-assign]
        index._refresh_persistent_search([item], "source-state-a")
        index._refresh_persistent_search([item], "source-state-b")
        self.assertEqual(1, calls)


class AdapterRegistryTests(unittest.TestCase):
    def test_grok_default_home_is_user_dot_grok(self) -> None:
        homes = source_adapters.default_candidates("grok")
        self.assertEqual(Path(os.environ.get("GROK_HOME") or (Path.home() / ".grok")), homes[0])

    def test_launcher_writes_grok_into_the_hub_data_dir(self) -> None:
        import launcher

        launcher.ensure_grok_enabled()
        payload = json.loads(Path(os.environ["CONVERSATION_HUB_DATA_DIR"], "sources.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["extra_sources"]["grok"]["enabled"])
        self.assertTrue(str(payload["extra_sources"]["grok"]["path"]).endswith(".grok"))

    def test_all_bundled_loaders_are_registered(self) -> None:
        expected = {
            "claude", "cursor", "qclaw", "qoderwork", "zcode", "codepilot", "marvis",
            "qoder", "qodercn", "qwenworkcn", "grok",
        }
        self.assertEqual(expected, set(source_adapters.EXTRA_SOURCES))
        self.assertEqual(expected, set(source_adapters.LOADERS))
        for source in expected:
            self.assertIn(source, source_adapters.SOURCE_LABELS)
            self.assertIsInstance(source_adapters.default_candidates(source), list)

    def test_claude_estimate_deduplicates_history_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "projects" / "fixture").mkdir(parents=True)
            (root / "projects" / "fixture" / "same-session.jsonl").write_text(
                json.dumps({
                    "type": "user",
                    "sessionId": "same-session",
                    "message": {"role": "user", "content": "hello"},
                }) + "\n",
                encoding="utf-8",
            )
            (root / "history.jsonl").write_text(
                json.dumps({"sessionId": "same-session", "display": "hello"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(1, source_adapters.estimate_conversations("claude", root))

    def test_readable_turn_unifies_qoderwork_parts_and_system_reminders(self) -> None:
        from readable import readable_turn_text

        parts = [
            {"type": "tool-Thinking", "input": {"text": "internal plan"}},
            {"type": "tool-Bash", "input": {"command": "ls"}},
            {"type": "text", "text": "收到，两件事已经记下。"},
        ]
        text = readable_turn_text("assistant", parts)
        self.assertIn("收到，两件事已经记下。", text)
        self.assertIn("<thinking>", text)
        self.assertIn("internal plan", text)
        self.assertNotIn("tool-Bash", text)
        reminder = readable_turn_text("user", "<system-reminder>\nTimezone: Asia/Shanghai\n</system-reminder>")
        self.assertEqual("", reminder)

    def test_codex_hides_agents_md_and_keeps_assistant_from_event_msg(self) -> None:
        self.assertEqual("", extract_codex_user_text("# AGENTS.md instructions\n<INSTRUCTIONS>\nhi"))
        self.assertEqual("继续吧", extract_codex_user_text("<user_query>继续吧</user_query>"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            rows = [
                {
                    "type": "response_item",
                    "timestamp": "2026-08-05T02:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "# AGENTS.md instructions\nfoo"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-08-05T02:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "<user_query>找一下这个对话id</user_query>"}],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-05T02:00:02Z",
                    "payload": {"type": "agent_message", "message": "找到了，会话还在。"},
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            messages = iter_codex_visible_messages(path)
        self.assertEqual(
            [("user", "找一下这个对话id"), ("assistant", "找到了，会话还在。")],
            [(item["role"], item["text"]) for item in messages],
        )

    def test_codepilot_keeps_assistant_text_and_drops_thinking_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codepilot.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE chat_sessions(id TEXT, title TEXT, updated_at REAL, "
                    "created_at REAL, sdk_cwd TEXT, working_directory TEXT, model TEXT)"
                )
                conn.execute(
                    "CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, "
                    "content TEXT, created_at REAL, is_heartbeat_ack INTEGER)"
                )
                conn.execute(
                    "INSERT INTO chat_sessions VALUES('one','印象笔记',2,1,'C:/fixture','', '')"
                )
                payload = json.dumps(
                    [
                        {"type": "thinking", "thinking": "internal monologue"},
                        {"type": "tool_use", "name": "ls", "input": {}},
                        {"type": "text", "text": "现在正常了。连接稳定。"},
                    ],
                    ensure_ascii=False,
                )
                conn.execute(
                    "INSERT INTO messages VALUES(1,'one','user','检查模型',1,0)"
                )
                conn.execute(
                    "INSERT INTO messages VALUES(2,'one','assistant',?,2,0)",
                    (payload,),
                )
                conn.commit()
            finally:
                conn.close()
            items, messages = source_adapters._load_codepilot(path)
            self.assertEqual(1, len(items))
            turns = messages["one"]
            self.assertEqual("user", turns[0]["role"])
            self.assertIn("现在正常了。连接稳定。", turns[1]["text"])
            self.assertIn("<thinking>", turns[1]["text"])
            self.assertIn("internal monologue", turns[1]["text"])
            self.assertNotIn("tool_use", turns[1]["text"])

    def test_codex_search_refresh_counts_visible_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            rows = [
                {
                    "type": "response_item",
                    "timestamp": "2026-08-05T02:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "<user_query>找一下这个对话id</user_query>"}],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-05T02:00:02Z",
                    "payload": {"type": "agent_message", "message": "找到了，会话还在。"},
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            item = sample_conversation("codex", "codex-count-1")
            item.rollout_path = str(path)
            item.message_count = 0
            index = ConversationIndex(refresh_on_init=False)
            index._refresh_codex_search([item])
            self.assertEqual(2, item.message_count)

            again = sample_conversation("codex", "codex-count-1")
            again.rollout_path = str(path)
            again.message_count = 0
            index._refresh_codex_search([again])
            self.assertEqual(2, again.message_count)

    def test_marvis_keeps_assistant_text_and_drops_thinking_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marvis.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE conversations("
                    "conversation_id TEXT, title TEXT, updated_at REAL, created_at REAL, metadata TEXT)"
                )
                conn.execute(
                    "CREATE TABLE messages("
                    "conversation_id TEXT, role TEXT, content TEXT, created_at REAL, message_seq INTEGER)"
                )
                conn.execute(
                    "INSERT INTO conversations VALUES('one','检查模型',2,1,'{\"cwd\":\"C:/fixture\"}')"
                )
                payload = json.dumps(
                    [
                        {"type": "thinking", "thinking": "internal monologue"},
                        {"type": "text", "text": "连接稳定。"},
                    ],
                    ensure_ascii=False,
                )
                conn.execute("INSERT INTO messages VALUES('one','user','检查模型',1,1)")
                conn.execute("INSERT INTO messages VALUES('one','assistant',?,2,2)", (payload,))
                conn.commit()
            finally:
                conn.close()
            items, messages = source_adapters._load_marvis(path)
            self.assertEqual(1, len(items))
            turns = messages["one"]
            self.assertEqual("user", turns[0]["role"])
            self.assertIn("连接稳定。", turns[1]["text"])
            self.assertIn("<thinking>", turns[1]["text"])
            self.assertNotIn("tool_use", turns[1]["text"])

    def test_codepilot_estimate_is_an_integer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE chat_sessions(id TEXT, title TEXT, updated_at REAL, "
                    "created_at REAL, sdk_cwd TEXT, working_directory TEXT, model TEXT)"
                )
                conn.execute(
                    "CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, "
                    "content TEXT, created_at REAL, is_heartbeat_ack INTEGER)"
                )
                conn.execute(
                    "INSERT INTO chat_sessions VALUES('one','Fixture',2,1,'C:/fixture','', '')"
                )
                conn.commit()
            finally:
                conn.close()
            valid, _ = source_adapters.validate_source("codepilot", path)
            self.assertTrue(valid)
            estimate = source_adapters.estimate_conversations("codepilot", path)
            self.assertIsInstance(estimate, int)
            self.assertEqual(1, estimate)

    def test_qoder_new_index_prefers_the_more_complete_plaintext_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index_db = root / "local.db"
            conn = sqlite3.connect(index_db)
            try:
                conn.execute(
                    "CREATE TABLE chat_session("
                    "session_id TEXT, session_title TEXT, project_uri TEXT, project_name TEXT, "
                    "gmt_create INTEGER, gmt_modified INTEGER, session_type TEXT, mode TEXT)"
                )
                conn.execute(
                    "INSERT INTO chat_session VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "task-abc.session.execution",
                        "Continue project optimization",
                        "C:/fixture/project",
                        "project",
                        1000,
                        2000,
                        "quest",
                        "agent",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            full = root / "projects" / "full" / "transcript" / "task-abc.session.execution.jsonl"
            full.parent.mkdir(parents=True)
            full.write_text(
                json.dumps({
                    "type": "user",
                    "uuid": "full-1",
                    "cwd": "C:/fixture/project",
                    "message": {"role": "user", "content": "old partial transcript"},
                }) + "\n",
                encoding="utf-8",
            )
            compact = (
                root / "cache" / "projects" / "compact" / "conversation-history"
                / "task-abc" / "task-abc.jsonl"
            )
            compact.parent.mkdir(parents=True)
            compact.write_text(
                "\n".join([
                    json.dumps({
                        "role": "user",
                        "uuid": "compact-1",
                        "message": {
                            "content": (
                                "<attached_files>generated diff</attached_files>"
                                "<user_query>Improve the tray integration.</user_query>"
                            )
                        },
                    }),
                    json.dumps({
                        "role": "assistant",
                        "uuid": "compact-2",
                        "message": {"content": "Implemented and verified."},
                    }),
                ]) + "\n",
                encoding="utf-8",
            )

            self.assertTrue(source_adapters.validate_source("qoder", index_db)[0])
            with mock.patch.object(source_adapters, "default_candidates", return_value=[index_db]):
                items, messages = source_adapters._load_qoder_family("qoder", index_db, root)
            self.assertEqual(1, len(items))
            self.assertEqual("Continue project optimization", items[0]["title"])
            self.assertEqual(str(compact.resolve()), items[0]["rollout_path"])
            self.assertEqual(2, len(messages[items[0]["id"]]))
            self.assertEqual("Improve the tray integration.", messages[items[0]["id"]][0]["text"])
            self.assertEqual(1, messages[items[0]["id"]][0]["line"])
            self.assertEqual("compact-1", messages[items[0]["id"]][0]["event_id"])

    def test_grok_reads_updates_and_skips_thoughts_and_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "sessions" / "workspace" / "sess-main"
            child = root / "sessions" / "workspace" / "sess-main" / "subagents" / "child"
            main.mkdir(parents=True)
            child.mkdir(parents=True)
            (main / "summary.json").write_text(
                json.dumps({
                    "info": {"id": "sess-main", "cwd": "C:/work/demo"},
                    "generated_title": "Grok fixture",
                    "created_at": "2026-08-14T00:00:00Z",
                    "updated_at": "2026-08-14T00:01:00Z",
                    "current_model_id": "grok-4.6",
                }),
                encoding="utf-8",
            )
            (main / "updates.jsonl").write_text(
                "\n".join([
                    json.dumps({
                        "timestamp": 100,
                        "params": {
                            "update": {
                                "sessionUpdate": "user_message_chunk",
                                "content": {"type": "text", "text": "Hello "},
                                "_meta": {"eventId": "u1"},
                            }
                        },
                    }),
                    json.dumps({
                        "timestamp": 101,
                        "params": {
                            "update": {
                                "sessionUpdate": "user_message_chunk",
                                "content": {"type": "text", "text": "Grok"},
                            }
                        },
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
                                "sessionUpdate": "tool_call",
                                "title": "read file",
                            }
                        }
                    }),
                    json.dumps({
                        "timestamp": 102,
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "Hi there"},
                                "_meta": {"eventId": "a1"},
                            }
                        },
                    }),
                    json.dumps({
                        "params": {"update": {"sessionUpdate": "turn_completed"}}
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

            self.assertTrue(source_adapters.validate_source("grok", root)[0])
            self.assertEqual(1, source_adapters.estimate_conversations("grok", root))
            items, messages = source_adapters._load_grok(root)
            self.assertEqual(1, len(items))
            self.assertEqual("sess-main", items[0]["id"])
            self.assertEqual("Grok fixture", items[0]["title"])
            self.assertEqual("C:/work/demo", items[0]["cwd"])
            self.assertEqual("grok-4.6", items[0]["model"])
            self.assertEqual(
                [
                    {"role": "user", "text": "Hello Grok"},
                    {"role": "assistant", "text": "Hi there"},
                ],
                [{"role": row["role"], "text": row["text"]} for row in messages["sess-main"]],
            )
            self.assertNotIn("hidden thought", json.dumps(messages))

    def test_qoder_old_config_migrates_to_the_new_title_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_db = root / "state.vscdb"
            conn = sqlite3.connect(old_db)
            try:
                conn.execute("CREATE TABLE ItemTable(key TEXT, value TEXT)")
                conn.execute(
                    "INSERT INTO ItemTable VALUES(?, ?)",
                    (
                        "lingma.chat.localHistory.fixture",
                        json.dumps([{"sessionId": "legacy-id", "title": "Legacy"}]),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            new_db = root / "local.db"
            conn = sqlite3.connect(new_db)
            try:
                conn.execute(
                    "CREATE TABLE chat_session("
                    "session_id TEXT, session_title TEXT, project_uri TEXT, project_name TEXT, "
                    "gmt_create INTEGER, gmt_modified INTEGER, session_type TEXT, mode TEXT)"
                )
                conn.execute(
                    "INSERT INTO chat_session VALUES('new-id','New title','','',1,2,'quest','agent')"
                )
                conn.commit()
            finally:
                conn.close()

            config = {
                "extra_sources": {
                    "qoder": {"enabled": True, "path": str(old_db)},
                }
            }

            def candidates(source: str) -> list[Path]:
                return [new_db, old_db] if source == "qoder" else []

            with mock.patch.object(source_adapters, "default_candidates", side_effect=candidates):
                status = source_adapters.configured_extra_sources(config, with_counts=False)
            self.assertEqual(str(new_db), status["qoder"]["path"])
            self.assertTrue(status["qoder"]["valid"])

    def test_cursor_qclaw_and_marvis_estimators(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            cursor_root = root / "cursor"
            cursor_root.mkdir()
            cursor_db = cursor_root / "conversation-search.db"
            conn = sqlite3.connect(cursor_db)
            try:
                conn.execute(
                    "CREATE TABLE conversations(id TEXT, source TEXT, title TEXT, "
                    "updated_at REAL, is_archived INTEGER, fts_rowid INTEGER)"
                )
                conn.execute(
                    "INSERT INTO conversations VALUES('one','local','Fixture',2,0,1)"
                )
                conn.commit()
            finally:
                conn.close()
            self.assertTrue(source_adapters.validate_source("cursor", cursor_root)[0])
            self.assertEqual(1, source_adapters.estimate_conversations("cursor", cursor_root))

            qclaw_root = root / "qclaw"
            sessions_root = qclaw_root / "agents" / "main" / "sessions"
            sessions_root.mkdir(parents=True)
            (sessions_root / "sessions.json").write_text(
                json.dumps({
                    "main": {"sessionId": "main-session", "sessionFile": "main.jsonl"},
                    "main:heartbeat": {"sessionId": "background"},
                }),
                encoding="utf-8",
            )
            self.assertTrue(source_adapters.validate_source("qclaw", qclaw_root)[0])
            self.assertEqual(1, source_adapters.estimate_conversations("qclaw", qclaw_root))

            marvis_db = root / "marvis.db"
            conn = sqlite3.connect(marvis_db)
            try:
                conn.execute("CREATE TABLE conversations(conversation_id TEXT)")
                conn.execute("CREATE TABLE messages(conversation_id TEXT)")
                conn.execute("INSERT INTO conversations VALUES('one')")
                conn.commit()
            finally:
                conn.close()
            self.assertTrue(source_adapters.validate_source("marvis", marvis_db)[0])
            self.assertEqual(1, source_adapters.estimate_conversations("marvis", marvis_db))


if __name__ == "__main__":
    unittest.main()
