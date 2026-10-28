# -*- coding: utf-8 -*-
"""日志滚动 / 归档 / 清理测试。

规则（用户确定，见 `config.DEFAULT_LOGS` 的说明）：

  · **作用域**：全局 `logs.rotate` 对**所有**日志生效；`logs.files.<日志名>`
    覆盖它——单文件那段为空（或没写）时**回落全局**。
  · **滚动规则二选一**（不会同时命中）：`max_mb` > 0 → 按**大小**滚动，
    **这是最高优先级**；没有大小规则但 `daily` 为真 → 按**自然日首次写入**滚动；
    两者都没有 → 不滚动。
  · **归档**：`logs/archive/<日志名>/<日志名>.<时间戳>`（先按日志名分文件夹）。
  · `compress: false`（默认）→ 归档保留**原始文件数据**，直接搬走。
  · `keep_days` 清理超期归档；`logs/tasks/` 的任务日志按天直接清理（不归档）。

日志目录由 `conftest` 的 autouse 夹具指向临时目录，本文件不碰真实 `logs/`。
"""
from __future__ import annotations

import os
import time

from agentmemhub import config as hub_config
from agentmemhub import logs


def _pin(policy: dict, monkeypatch):
    """钉死生效策略（替代读 yaml）。"""
    monkeypatch.setattr(logs, "rotate_policy", lambda name: dict(policy))


def _pol(**kw) -> dict:
    base = {"max_mb": 5, "daily": False, "keep_days": 30,
            "compress": False, "archive_dir": "archive"}
    base.update(kw)
    return base


def _fill(n: int = 30, size: int = 60) -> None:
    for i in range(n):
        logs.audit_memory({"event": "write", "memory_id": i,
                           "content": "占位" * size})


# ---------------------------------------------------------------------------
# 策略解析：全局 vs 单文件
# ---------------------------------------------------------------------------

def test_单文件配置覆盖全局(monkeypatch):
    class _Cfg:
        logs = {"rotate": {"max_mb": 5, "daily": False, "keep_days": 30},
                "files": {"wiki": {"max_mb": 1}}}
    monkeypatch.setattr(hub_config, "config", lambda: _Cfg())
    assert logs.rotate_policy("wiki")["max_mb"] == 1        # 单文件覆盖
    assert logs.rotate_policy("mcp")["max_mb"] == 5         # 未配置 → 回落全局


def test_单文件配置为空时回落全局(monkeypatch):
    class _Cfg:
        logs = {"rotate": {"max_mb": 7, "daily": True, "keep_days": 30},
                "files": {"wiki": {}}}                       # 空段
    monkeypatch.setattr(hub_config, "config", lambda: _Cfg())
    assert logs.rotate_policy("wiki")["max_mb"] == 7
    assert logs.rotate_policy("wiki")["daily"] is True


def test_默认配置_大小规则生效且保留三十天(monkeypatch):
    """不写任何配置时的默认（用户定的：大小优先、keep_days 30、暂不压缩）。

    这里用真实 `Config` 合并行为模拟（`Config.logs` 会把 DEFAULT_LOGS 并进来）。
    """
    from agentmemhub.config import DEFAULT_LOGS
    class _Cfg:
        logs = DEFAULT_LOGS                       # 模拟 Config.logs 的合并结果
    monkeypatch.setattr(hub_config, "config", lambda: _Cfg())
    pol = logs.rotate_policy("memory")
    assert pol["max_mb"] == 5 and pol["keep_days"] == 30
    assert pol["compress"] is False
    assert pol["daily"] is False                  # 默认走大小规则


# ---------------------------------------------------------------------------
# 滚动：大小规则 / 按天规则 / 二选一与优先级
# ---------------------------------------------------------------------------

def test_按大小滚动并归档到日志名目录(monkeypatch):
    _pin(_pol(max_mb=0.001), monkeypatch)        # 约 1 KB 即滚动
    _fill()
    src = logs.memory_audit_file()
    arch = logs.archive_dir("memory")
    assert src.exists(), "滚动后当前日志应继续可用"
    assert arch.exists() and list(arch.iterdir()), "应归档到 archive/memory/"
    names = [f.name for f in arch.iterdir()]
    assert all(n.startswith("memory.") for n in names)


def test_归档保留原始文件而非压缩(monkeypatch):
    """compress=false（当前默认）→ 归档文件是可读的原始日志，不是 .gz。"""
    _pin(_pol(max_mb=0.001, compress=False), monkeypatch)
    _fill()
    f = next(iter(logs.archive_dir("memory").iterdir()))
    assert f.suffix != ".gz"
    assert '"event": "write"' in f.read_text(encoding="utf-8")


def test_压缩开关打开时产出gz(monkeypatch):
    """compress=true 是预留配置位，打开即用 gzip。"""
    _pin(_pol(max_mb=0.001, compress=True), monkeypatch)
    _fill()
    files = list(logs.archive_dir("memory").iterdir())
    assert files and all(f.suffix == ".gz" for f in files)


def test_大小规则优先于按天规则(monkeypatch):
    """两条都配时按大小（**最高优先级**）——文件没到阈值就不滚动，哪怕跨了日。"""
    _pin(_pol(max_mb=999, daily=True), monkeypatch)      # 阈值极大 + 开了按天
    logs.audit_memory({"event": "write", "memory_id": 1})
    p = logs.memory_audit_file()
    old = time.time() - 86400 * 3
    os.utime(p, (old, old))                              # 造成"跨日"状态
    logs.audit_memory({"event": "write", "memory_id": 2})
    assert not logs.archive_dir("memory").exists(), \
        "有大小规则时不得走按天规则"


def test_按天规则_跨日首次写入时滚动(monkeypatch):
    _pin(_pol(max_mb=0, daily=True), monkeypatch)
    logs.audit_memory({"event": "write", "memory_id": 1})
    p = logs.memory_audit_file()
    old = time.time() - 86400 * 2
    os.utime(p, (old, old))                              # 昨天（更早）写的
    logs.audit_memory({"event": "write", "memory_id": 2})
    files = list(logs.archive_dir("memory").iterdir())
    assert files, "跨日后第一次写入应滚动归档"
    # 同日再写不滚动
    before = len(files)
    logs.audit_memory({"event": "write", "memory_id": 3})
    assert len(list(logs.archive_dir("memory").iterdir())) == before


def test_两条规则都不配则不滚动(monkeypatch):
    _pin(_pol(max_mb=0, daily=False), monkeypatch)
    _fill()
    assert logs.memory_audit_file().exists()
    assert not logs.archive_dir("memory").exists()


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

def test_keep_days_清理超期归档(monkeypatch):
    _pin(_pol(max_mb=0.001, keep_days=7), monkeypatch)
    _fill()
    arch = logs.archive_dir("memory")
    files = list(arch.iterdir())
    assert files
    old = time.time() - 86400 * 30                       # 30 天前
    for f in files:
        os.utime(f, (old, old))
    _fill()                                              # 再滚一次 → 触发清理
    left = list(arch.iterdir())
    assert left, "滚动应继续产出归档"
    assert all((time.time() - f.stat().st_mtime) < 3600 for f in left), \
        "清理后不应残留 30 天前的归档"


def test_keep_days为零表示不清理(monkeypatch):
    _pin(_pol(max_mb=0.001, keep_days=0), monkeypatch)
    _fill()
    arch = logs.archive_dir("memory")
    old = time.time() - 86400 * 999
    for f in arch.iterdir():
        os.utime(f, (old, old))
    _fill()
    assert list(arch.iterdir()), "keep_days=0 应保留全部归档"


def test_任务日志按keep_days直接清理(monkeypatch, tmp_path):
    """任务日志是"一次性输出"：按天直接删，不归档。"""
    d = tmp_path / "tasks"
    monkeypatch.setattr(logs, "task_log_dir", lambda: d)
    _pin(_pol(keep_days=7), monkeypatch)
    d.mkdir(parents=True)
    fresh, stale = d / "fresh.log", d / "stale.log"
    fresh.write_text("新", encoding="utf-8")
    stale.write_text("旧", encoding="utf-8")
    old = time.time() - 86400 * 30
    os.utime(stale, (old, old))
    removed = logs.prune_task_logs()
    assert removed == ["stale.log"]
    assert fresh.exists() and not stale.exists()
