from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


@unittest.skipUnless(os.name == "nt", "Windows tray only")
class TrayHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global tray
        import tray

    def test_remembered_url_uses_the_dynamic_instance_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "instance.json"
            instance_path.write_text(json.dumps({"port": 8788}), encoding="utf-8")
            with mock.patch.object(tray, "INSTANCE_PATH", instance_path):
                self.assertEqual("http://127.0.0.1:8788", tray.remembered_url())

    def test_discovery_prefers_a_healthy_remembered_port(self) -> None:
        checked: list[str] = []

        def health(url: str) -> bool:
            checked.append(url)
            return url.endswith(":8791")

        with mock.patch.object(tray, "remembered_url", return_value="http://127.0.0.1:8791"):
            with mock.patch.object(tray, "_port_in_use", return_value=True):
                with mock.patch.object(tray, "health_url", side_effect=health):
                    self.assertEqual("http://127.0.0.1:8791", tray.discover_running_url())
        self.assertEqual(["http://127.0.0.1:8791"], checked)

    def test_source_autostart_command_is_relative_to_the_installation(self) -> None:
        command, cwd = tray.source_launch_command()
        self.assertEqual(tray.SOURCE_DIR, cwd)
        self.assertEqual("--no-open", command[-1])
        self.assertEqual(tray.SOURCE_DIR / "desktop_app.py", Path(command[-2]))

    def test_embedded_exit_invokes_the_whole_app_shutdown_callback(self) -> None:
        shutdown_called = threading.Event()
        component = tray.Tray("http://127.0.0.1:8789", shutdown_called.set)
        component._exit_app()
        self.assertTrue(shutdown_called.wait(2))


if __name__ == "__main__":
    unittest.main()
