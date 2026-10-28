"""CLI 退出码（2026-10-26）。

背景：`main()` 里 `fn(args)` 的返回值被直接丢弃、`main() -> None`、
`__main__.py` 直接 `main()` —— 全仓没有 `sys.exit`。于是：

- `cli.py` 注释与 `docs/modules/feed.md` 都承诺「`feed lint` P0>0 ⇒ 退出码 1，
  可直接当提交/巡检门槛」，实际永远 0；
- `scripts/wiki_compile.py` / `scripts/wiki_aggregate.py` 失败与中止同样返回 0，
  挂 cron 上的"全量编译"永远是绿的。

守这一条的代价很低，失效的代价是「门槛形同虚设且没人发现」。
"""
import inspect
import sys

import pytest

from agentmemhub import cli


def test_子命令返回值成为退出码(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agentmemhub", "stats"])
    monkeypatch.setattr(cli, "cmd_stats", lambda args: 7)
    assert cli.main() == 7


def test_子命令返回None算成功(monkeypatch):
    """多数子命令是 `-> None` 的，不能被当成失败。"""
    monkeypatch.setattr(sys, "argv", ["agentmemhub", "stats"])
    monkeypatch.setattr(cli, "cmd_stats", lambda args: None)
    assert cli.main() == 0


def test_未知子命令argparse以2退出(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agentmemhub", "no-such-command"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2


def test_模块入口把返回值交给SystemExit():
    """`python -m agentmemhub` 必须 `raise SystemExit(main())`。

    直接 `main()` 时返回值被丢弃 —— 这是"退出码恒 0"的最后一道缺口。
    """
    from agentmemhub import __main__ as entry
    src = inspect.getsource(entry)
    assert "SystemExit(main())" in src


def test_wiki脚本把失败反映到退出码():
    """两个 wiki 脚本的 main 必须返回 int 而不是丢弃结果。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "scripts"
    compile_src = (root / "wiki_compile.py").read_text(encoding="utf-8")
    agg_src = (root / "wiki_aggregate.py").read_text(encoding="utf-8")
    assert "def main() -> int:" in compile_src
    assert "return 1 if [r for r in results if r.get(\"error\")] else 0" in compile_src
    assert "def main() -> int:" in agg_src
    assert "return 1 if (status or {}).get(\"aborted\") else 0" in agg_src
    assert "SystemExit(main())" in agg_src
