from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_agent_setup():
    # Delay this import until tests run. Other test modules establish the
    # process-wide isolated CONVERSATION_HUB_DATA_DIR during discovery.
    import agent_setup

    return agent_setup


class AgentSetupTests(unittest.TestCase):
    def make_resources(self, root: Path) -> Path:
        agent_setup = load_agent_setup()
        resources = root / "resources"
        for name in agent_setup.SKILL_NAMES:
            skill = resources / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            (skill / "scripts").mkdir()
            (skill / "scripts" / "fixture.py").write_text("VALUE = 1\n", encoding="utf-8")
        return resources

    def test_setup_is_idempotent_and_preserves_other_mcp_blocks(self) -> None:
        agent_setup = load_agent_setup()
        with tempfile.TemporaryDirectory(prefix="hub-agent-setup-") as directory:
            root = Path(directory)
            home = root / "home"
            data = root / "data"
            resources = self.make_resources(root)
            codex = home / ".codex" / "config.toml"
            codex.parent.mkdir(parents=True)
            codex.write_text(
                "[mcp_servers.other]\ncommand = \"other\"\n\n"
                "[mcp_servers.conversation-hub]\ncommand = \"old\"\nargs = []\n",
                encoding="utf-8",
            )
            statuses = {
                "codex": {"valid": True, "path": str(home / ".codex"), "conversations": 2},
                "grok": {"valid": False, "path": "", "conversations": 0},
            }
            with mock.patch.object(agent_setup, "repair", return_value={"config_version": 4}), mock.patch.object(
                agent_setup, "source_status", return_value=statuses
            ):
                first = agent_setup.run_setup(
                    home=home,
                    resource_dir=resources,
                    data_dir=data,
                    command=Path("C:/Program Files/AI Hub/AIConversationHubAgent.exe"),
                    prefix_args=[],
                )
                second = agent_setup.run_setup(
                    home=home,
                    resource_dir=resources,
                    data_dir=data,
                    command=Path("C:/Program Files/AI Hub/AIConversationHubAgent.exe"),
                    prefix_args=[],
                )

            self.assertEqual(first["usage_path"], second["usage_path"])
            text = codex.read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.other]", text)
            self.assertEqual(1, text.count("[mcp_servers.conversation-hub]"))
            self.assertIn("AIConversationHubAgent.exe", text)
            grok = (home / ".grok" / "config.toml").read_text(encoding="utf-8")
            self.assertIn("enabled = true", grok)
            for skill_name in agent_setup.SKILL_NAMES:
                for agent_root in (".agents", ".grok", ".claude"):
                    self.assertTrue(
                        (home / agent_root / "skills" / skill_name / "SKILL.md").is_file()
                    )
            usage = (data / "AGENT_USAGE.md").read_text(encoding="utf-8")
            self.assertIn(str(home / ".codex"), usage)
            self.assertIn("AIConversationHubAgent.exe", usage)
            self.assertIn("localhost", usage)

    def test_mcp_upsert_handles_following_table(self) -> None:
        agent_setup = load_agent_setup()
        with tempfile.TemporaryDirectory(prefix="hub-mcp-") as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[mcp_servers.conversation-hub]\ncommand = \"old\"\nargs = []\n\n"
                "[projects.demo]\ntrusted = true\n",
                encoding="utf-8",
            )
            agent_setup.upsert_mcp_config(
                path, Path("/opt/hub/agent"), ["mcp"], enabled=False
            )
            text = path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("[mcp_servers.conversation-hub]"))
            self.assertIn("[projects.demo]", text)
            self.assertIn('command = "/opt/hub/agent"', text)

    def test_domestic_agents_get_verified_skills_and_safe_mcp_routes(self) -> None:
        agent_setup = load_agent_setup()
        with tempfile.TemporaryDirectory(prefix="hub-domestic-setup-") as directory:
            root = Path(directory)
            home = root / "home"
            data = root / "data"
            app_support = root / "app-support"
            resources = self.make_resources(root)
            for name in (".workbuddy", ".qwenworkcn", ".qoder", ".qoder-cn"):
                (home / name).mkdir(parents=True)
            (app_support / "QoderWork CN").mkdir(parents=True)
            workbuddy_mcp = home / ".workbuddy" / "mcp.json"
            workbuddy_mcp.write_text(
                '{"mcpServers":{"other":{"command":"other","args":[]}}}\n',
                encoding="utf-8",
            )
            statuses = {"codex": {"valid": False, "path": "", "conversations": 0}}
            with mock.patch.object(agent_setup, "repair", return_value={}), mock.patch.object(
                agent_setup, "source_status", return_value=statuses
            ):
                first = agent_setup.run_setup(
                    home=home,
                    resource_dir=resources,
                    data_dir=data,
                    command=Path("/opt/AIConversationHubAgent"),
                    prefix_args=[],
                    application_support=app_support,
                )
                second = agent_setup.run_setup(
                    home=home,
                    resource_dir=resources,
                    data_dir=data,
                    command=Path("/opt/AIConversationHubAgent"),
                    prefix_args=[],
                    application_support=app_support,
                )

            self.assertEqual(16, len(first["skills"]))
            agents = {item["id"]: item for item in second["domestic_agents"]}
            self.assertEqual(
                {"workbuddy", "qwenworkcn", "qoder", "qodercn", "qoderwork"},
                set(agents),
            )
            self.assertTrue(all(item["detected"] for item in agents.values()))
            self.assertTrue(all(item["skill_installed"] for item in agents.values()))
            self.assertEqual("qwen_builtin_action", agents["qwenworkcn"]["mcp_mode"])
            self.assertEqual("cli_fallback", agents["qoderwork"]["mcp_mode"])
            for agent_id in ("workbuddy", "qoder", "qodercn"):
                config_path = Path(agents[agent_id]["mcp_path"])
                payload = __import__("json").loads(config_path.read_text(encoding="utf-8"))
                self.assertIn("conversation-hub", payload["mcpServers"])
                self.assertEqual(1, list(payload["mcpServers"]).count("conversation-hub"))
            workbuddy_payload = __import__("json").loads(
                workbuddy_mcp.read_text(encoding="utf-8")
            )
            self.assertIn("other", workbuddy_payload["mcpServers"])
            self.assertEqual(
                "qwenwork.settings.connector.custom",
                second["qwenwork_mcp_action"]["key"],
            )
            usage = (data / "AGENT_USAGE.md").read_text(encoding="utf-8")
            for label in agent_setup.DOMESTIC_LABELS.values():
                self.assertIn(label, usage)


if __name__ == "__main__":
    unittest.main()
