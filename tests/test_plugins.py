from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock


TEST_DATA = tempfile.mkdtemp(prefix="hub-plugin-data-")
os.environ.setdefault("CONVERSATION_HUB_DATA_DIR", TEST_DATA)

from plugins import (  # noqa: E402
    discover_plugins,
    local_http_json,
    plugin_public_view,
    require_local_url,
)
from plugins.tencent_agent_memory.plugin import (  # noqa: E402
    TencentAgentMemoryPlugin,
    _normalize_hits,
)


class LocalUrlTests(unittest.TestCase):
    def test_accepts_loopback_http(self) -> None:
        require_local_url("http://127.0.0.1:8420")
        require_local_url("http://localhost:8420/health")
        require_local_url("http://[::1]:8420")

    def test_rejects_remote_and_file(self) -> None:
        for url in (
            "https://example.com/recall",
            "http://8.8.8.8:8420",
            "http://127.0.0.1.attacker.example/health",
            "file:///etc/passwd",
            "http://user:pass@127.0.0.1:8420",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    require_local_url(url)

    def test_gateway_json_checks_url_before_request(self) -> None:
        with self.assertRaises(ValueError):
            local_http_json("GET", "https://example.com/health")


class TencentPluginTests(unittest.TestCase):
    def test_default_off_and_search_is_noop(self) -> None:
        plugin = TencentAgentMemoryPlugin()
        with mock.patch(
            "plugins.tencent_agent_memory.plugin.plugin_settings",
            return_value={},
        ):
            settings = plugin.settings()
            self.assertFalse(settings["enabled"])
            self.assertEqual(settings["base_url"], "http://127.0.0.1:8420")
            self.assertEqual(plugin.search("偏好"), [])

    def test_save_rejects_remote_url(self) -> None:
        plugin = TencentAgentMemoryPlugin()
        with mock.patch(
            "plugins.tencent_agent_memory.plugin.plugin_settings",
            return_value={},
        ), mock.patch("plugins.tencent_agent_memory.plugin.write_plugin_settings") as writer:
            with self.assertRaises(ValueError):
                plugin.save_settings({"enabled": True, "base_url": "https://memory.example"})
            writer.assert_not_called()

    def test_save_keeps_localhost_and_strips_key(self) -> None:
        plugin = TencentAgentMemoryPlugin()
        stored = {
            "enabled": True,
            "base_url": "http://127.0.0.1:8420",
            "api_key": "secret",
            "session_key": "conversation-hub",
        }
        with mock.patch(
            "plugins.tencent_agent_memory.plugin.plugin_settings",
            return_value=stored,
        ), mock.patch(
            "plugins.tencent_agent_memory.plugin.write_plugin_settings",
            return_value=stored,
        ) as writer:
            saved = plugin.save_settings({
                "enabled": True,
                "base_url": "http://localhost:8420/",
                "keep_api_key": True,
            })
        writer.assert_called_once()
        written = writer.call_args[0][1]
        self.assertEqual(written["base_url"], "http://localhost:8420")
        self.assertTrue(written["enabled"])
        self.assertEqual(saved.get("api_key"), "")
        self.assertTrue(saved["has_api_key"])

    def test_public_view_hides_api_key(self) -> None:
        plugin = TencentAgentMemoryPlugin()
        with mock.patch.object(
            plugin,
            "settings",
            return_value={
                "enabled": False,
                "base_url": "http://127.0.0.1:8420",
                "api_key": "secret",
                "session_key": "conversation-hub",
            },
        ):
            view = plugin_public_view(plugin)
        self.assertEqual(view["id"], "tencent-agent-memory")
        self.assertFalse(view["enabled"])
        self.assertNotIn("api_key", view["settings"])
        self.assertTrue(view["has_api_key"])
        self.assertIn("search", view["capabilities"])
        self.assertNotIn("health", view)

    def test_normalize_hits_reads_common_shapes(self) -> None:
        hits = _normalize_hits(
            {"results": [{"title": "偏好", "content": "喜欢简洁回复", "layer": "profile"}]},
            5,
        )
        self.assertEqual(hits[0]["text"], "喜欢简洁回复")
        self.assertEqual(hits[0]["layer"], "profile")

    def test_discover_includes_tencent_plugin(self) -> None:
        ids = [plugin.id for plugin in discover_plugins()]
        self.assertIn("tencent-agent-memory", ids)


if __name__ == "__main__":
    unittest.main()
