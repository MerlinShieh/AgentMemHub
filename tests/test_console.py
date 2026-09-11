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


def test_render_snapshot_rag_backend_shows_internal_index():
    """rag 后端状态总览：只说「记忆索引（内置引擎）」，不得出现 18800 / [10] 启动等 MemOS 遗留。"""
    from agentmemhub.console import _render_snapshot
    s = {
        "adapters": [{"source": "zcode", "located": True}],
        "stats": {"conversations": 12, "events": 345},
        "engine": {
            "online": True, "backend": "rag", "base_url": "in-process://rag",
            "summary": {"traces": 99, "embedding_model": "bge-small-zh-v1.5",
                        "coverage": 0.987},
        },
    }
    out = _render_snapshot(s)
    assert "记忆索引: 就绪（内置引擎）" in out
    assert "99 条记忆" in out
    assert "bge-small-zh-v1.5" in out
    assert "18800" not in out
    assert "[10]" not in out
    assert "MemOS" not in out


def test_render_snapshot_rag_backend_offline():
    """rag 索引不可用：给出可执行的排查线索，不提示去启动外部守护。"""
    from agentmemhub.console import _render_snapshot
    s = {
        "adapters": [],
        "stats": None,
        "engine": {"online": False, "backend": "rag", "summary": {}},
    }
    out = _render_snapshot(s)
    assert "记忆索引: 不可用" in out
    assert "session_rag.db" in out
    assert "18800" not in out


def test_render_snapshot_memos_fallback_backend():
    """memos 回退后端：仍显示外部引擎地址（该模式下才有守护可启停）。"""
    from agentmemhub.console import _render_snapshot
    s = {
        "adapters": [],
        "stats": {"conversations": 1, "events": 2},
        "engine": {"online": True, "backend": "memos",
                   "base_url": "http://127.0.0.1:18800",
                   "summary": {"traces": 7}},
    }
    out = _render_snapshot(s)
    assert "记忆引擎: 运行中" in out
    assert "http://127.0.0.1:18800" in out


def test_env_snapshot_uses_daemon_status_single_source():
    """引擎状态只取 daemon_status 一处真相，不再二次 HTTP 探测 18800。"""
    from agentmemhub import console
    fake = {"online": True, "backend": "rag", "summary": {"traces": 1}}
    with mock.patch("agentmemhub.memos_daemon.daemon_status", return_value=fake), \
         mock.patch("agentmemhub.console._store_stats_safe", return_value=None), \
         mock.patch("urllib.request.urlopen") as mu:
        snap = console.env_snapshot()
    assert snap["engine"] is fake
    assert snap["stats"] is None
    mu.assert_not_called()          # 无 18800 探测
