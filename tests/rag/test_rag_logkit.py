"""日志纪律：统一目录、按程序分文件、UTF-8、重复初始化幂等、测试不污染真实 logs/。"""
from __future__ import annotations

import logging

from agentmemhub.rag.logkit import get_logger


def test_logger_writes_per_component_file(tmp_log_dir):
    log = get_logger("ingest", tmp_log_dir, console=False)
    log.info("中文溯源记录 test=1")
    for h in log.handlers:
        h.flush()
    f = tmp_log_dir / "asrag-ingest.log"
    assert f.exists()
    assert "中文溯源记录" in f.read_text(encoding="utf-8")


def test_components_do_not_share_files(tmp_log_dir):
    get_logger("ingest", tmp_log_dir, console=False).info("a")
    get_logger("search", tmp_log_dir, console=False).info("b")
    for comp in ("ingest", "search"):
        for h in logging.getLogger(f"asrag.{comp}").handlers:
            h.flush()
    assert "a" in (tmp_log_dir / "asrag-ingest.log").read_text(encoding="utf-8")
    assert "b" in (tmp_log_dir / "asrag-search.log").read_text(encoding="utf-8")
    assert "b" not in (tmp_log_dir / "asrag-ingest.log").read_text(encoding="utf-8")


def test_get_logger_idempotent(tmp_log_dir):
    a = get_logger("eval", tmp_log_dir, console=False)
    n = len(a.handlers)
    b = get_logger("eval", tmp_log_dir, console=False)
    assert a is b and len(b.handlers) == n, "重复调用不得叠加 handler"
