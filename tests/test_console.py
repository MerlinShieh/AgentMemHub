"""控制台菜单单元测试：看板停止的 PID 解析。"""
from __future__ import annotations

from unittest import mock

from agentmemhub.console import _dashboard_pid


def test_dashboard_pid_parses_netstat():
    fake_out = (
        "  TCP    127.0.0.1:8086         0.0.0.0:0              LISTENING       30560\r\n"
        "  TCP    127.0.0.1:18800        0.0.0.0:0              LISTENING      12345\r\n"
    )
    with mock.patch("agentmemhub.console.subprocess.run") as mr, \
         mock.patch("agentmemhub.console.os.name", "nt"):
        mr.return_value = mock.Mock(stdout=fake_out)
        assert _dashboard_pid(8086) == 30560
        assert _dashboard_pid(18800) == 12345
        assert _dashboard_pid(9999) is None


def test_dashboard_pid_no_listening():
    with mock.patch("agentmemhub.console.subprocess.run") as mr, \
         mock.patch("agentmemhub.console.os.name", "nt"):
        mr.return_value = mock.Mock(stdout="  TCP  127.0.0.1:9000  ...\n")
        assert _dashboard_pid(8086) is None


def test_memos_probe_uses_public_auth_status_endpoint():
    """在线探测必须用公开端点（/api/v1/auth/status）——设密码后 overview 会 401 误判离线。"""
    from agentmemhub.console import memos_probe
    with mock.patch("urllib.request.urlopen") as mu:
        mu.return_value = mock.MagicMock()
        mu.return_value.__enter__.return_value.read.return_value = b'{"enabled": true}'
        r = memos_probe("http://127.0.0.1:19800")
    assert r == {"enabled": True}
    url = mu.call_args[0][0]
    assert url == "http://127.0.0.1:19800/api/v1/auth/status"


def test_memos_probe_returns_none_when_offline():
    from agentmemhub.console import memos_probe
    with mock.patch("urllib.request.urlopen", side_effect=OSError("connect refused")):
        assert memos_probe("http://127.0.0.1:19800") is None