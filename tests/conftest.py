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
