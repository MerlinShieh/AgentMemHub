"""测试全局夹具：数据目录沙箱保险丝 + 后端钉位。

任何测试都不得读写真实数据目录 ~/.agentmemhub——会话级 autouse 夹具把
AGENTMEM_HUB_DATA_DIR 指到 pytest 临时目录（优先级高于 yaml，见 config.py）。
测试自己显式传 env 构造 Config 的行为不受影响。

AGENTMEMHUB_BACKEND=memos：存量测试是 MemOS 回退路径的行为规格（HTTP mock），
整个旧套件显式钉在 memos 后端；rag 后端（生产默认）由 tests/test_rag_bridge.py
逐测试 monkeypatch 为 rag 专项覆盖。
"""
import os

import pytest


@pytest.fixture(autouse=True)
def audit_dir(tmp_path, monkeypatch):
    """日志目录隔离：任何测试都不写真实 `logs/`。

    项目原有的做法是"需要写日志的测试自己 monkeypatch `logs.log_dir`"，但 MCP
    调用审计是新的**高频写入点**（每次工具调用都写），靠逐个自觉迟早漏——
    实测已经漏过一次：`test_mcp_http.py` 的调用把 mock 数据写进了真实
    `logs/mcp.log`。所以改成全局兜底，需要时再在测试内覆盖。

    同时作为审计用例的读取入口：`def test_x(audit_dir)` 拿到临时目录。
    """
    d = tmp_path / "logs"
    monkeypatch.setattr("agentmemhub.logs.log_dir", lambda: d)
    return d


@pytest.fixture(scope="session", autouse=True)
def _sandbox_data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("agentmemhub-data")
    old = os.environ.get("AGENTMEM_HUB_DATA_DIR")
    oldb = os.environ.get("AGENTMEMHUB_BACKEND")
    os.environ["AGENTMEM_HUB_DATA_DIR"] = str(d)
    os.environ.setdefault("AGENTMEMHUB_BACKEND", "memos")
    yield
    if old is None:
        os.environ.pop("AGENTMEM_HUB_DATA_DIR", None)
    else:
        os.environ["AGENTMEM_HUB_DATA_DIR"] = old
    if oldb is None:
        os.environ.pop("AGENTMEMHUB_BACKEND", None)
    else:
        os.environ["AGENTMEMHUB_BACKEND"] = oldb
