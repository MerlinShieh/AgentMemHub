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


# ---------------------------------------------------------------------------
# action 层收尾日志：曾因裸调未导入的 cli._cli_log 在业务跑完后抛 NameError
# （2026-09-11 实机报障：[3] 蒸馏后 "name '_cli_log' is not defined"）。
# 各 action 一律经 console._action_log 记录，此处逐个执行验证。
# ---------------------------------------------------------------------------

def test_action_distill_completes_and_logs(capsys):
    from agentmemhub import console
    fake = {"conversations": 3, "slices": 5, "skipped_done": 2, "failed": 0,
            "memories_new": 6, "merged": 1, "projected": 6, "duplicate": 0,
            "seconds": 1.5, "model": "deepseek-flash", "error": None}
    logs: list[str] = []
    with mock.patch("agentmemhub.console._confirm", return_value=True), \
         mock.patch("agentmemhub.rag_bridge.settings", return_value=None), \
         mock.patch("agentmemhub.distill.run_distill", return_value=fake), \
         mock.patch("agentmemhub.cli._cli_log",
                    side_effect=lambda m, level="info": logs.append(m)):
        console.action_distill()          # 不得抛异常
    out = capsys.readouterr().out
    assert "✓ 会话 3 · 切片 5" in out
    assert logs and "蒸馏记忆（控制台）" in logs[0]


def test_action_distill_error_result_returns_quietly(capsys):
    """run_distill 返回 error 字典：提示后正常返回，不再走后面的统计行。"""
    from agentmemhub import console
    with mock.patch("agentmemhub.console._confirm", return_value=True), \
         mock.patch("agentmemhub.rag_bridge.settings", return_value=None), \
         mock.patch("agentmemhub.distill.run_distill",
                    return_value={"error": "LLM 未配置完整"}):
        console.action_distill()
    out = capsys.readouterr().out
    assert "蒸馏未执行" in out


def test_action_memos_completes_and_logs(capsys):
    from agentmemhub import console
    logs: list[str] = []
    with mock.patch("agentmemhub.console._confirm", return_value=True), \
         mock.patch("agentmemhub.cli._vectorize_stage",
                    return_value={"embedded": 7, "failed": 0}), \
         mock.patch("agentmemhub.cli._cli_log",
                    side_effect=lambda m, level="info": logs.append(m)):
        console.action_memos()
    out = capsys.readouterr().out
    assert "已写入可检索（7 条新嵌入）" in out
    assert logs and "写入记忆（控制台）→ 7 条" in logs[0]


def test_action_clean_completes_and_logs(capsys):
    from agentmemhub import console
    logs: list[str] = []
    fake_store = mock.Mock()
    fake_store.system_event_counts.return_value = [
        {"source": "zcode", "n": 3, "convs": 1}]
    fake_store.delete_system_events.return_value = (3, 1)
    with mock.patch("agentmemhub.store.Store", return_value=fake_store), \
         mock.patch("agentmemhub.console._confirm", return_value=True), \
         mock.patch("agentmemhub.cli._cli_log",
                    side_effect=lambda m, level="info": logs.append(m)):
        console.action_clean()
    out = capsys.readouterr().out
    assert "已删除 3 条注入事件" in out
    assert logs and "clean（控制台）" in logs[0]
    fake_store.close.assert_called_once()


def test_action_score_completes_and_logs(capsys):
    from agentmemhub import console
    logs: list[str] = []
    fake = {"evaluated": 2, "skipped": 1, "positive": 1, "neutral": 1,
            "negative": 0, "errors": 0, "dryRun": False, "mode": "incremental"}
    with mock.patch("agentmemhub.console._ask", return_value="0"), \
         mock.patch("agentmemhub.console._confirm", return_value=True), \
         mock.patch("agentmemhub.scoring.run_score_incremental",
                    return_value=fake), \
         mock.patch("agentmemhub.cli._cli_log",
                    side_effect=lambda m, level="info": logs.append(m)):
        console.action_score()
    out = capsys.readouterr().out
    assert "evaluated=2" in out
    assert logs and "score（控制台）" in logs[0]


def test_no_undefined_global_names_in_package():
    """静态扫描：包内不得出现「引用但作用域内无定义」的全局名（NameError 温床）。

    覆盖同类历史缺陷：console.action_distill/action_memos 裸调 _cli_log、
    rag.cli bench 分支裸调 get_embedder。
    """
    import builtins
    import symtable
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[1] / "agentmemhub"
    # 模块级隐式名（解释器注入），非缺陷
    implicit = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
                "__loader__", "__builtins__", "__debug__", "__path__", "__all__"}
    known_builtins = set(dir(builtins)) | implicit

    problems: list[str] = []

    def scan(py: Path) -> None:
        # utf-8-sig：部分文件带 BOM，symtable 直接解析会报非法字符
        src = py.read_text(encoding="utf-8-sig")
        top = symtable.symtable(src, str(py), "exec")
        module_bound = {s.get_name() for s in top.get_symbols()
                        if s.is_assigned() or s.is_imported()
                        or s.is_namespace()} | known_builtins

        def walk(table, scope: str) -> None:
            for sym in table.get_symbols():
                name = sym.get_name()
                if (sym.is_referenced() and sym.is_global()
                        and not sym.is_assigned() and name not in module_bound):
                    problems.append(
                        f"{py.relative_to(pkg.parent)}::{scope or '<module>'}::{name}")
            for child in table.get_children():
                walk(child, f"{scope}.{child.get_name()}" if scope else child.get_name())

        walk(top, "")

    for py in sorted(pkg.rglob("*.py")):
        scan(py)
    assert not problems, "未定义的全局引用（运行时 NameError 风险）：\n" + "\n".join(problems)
