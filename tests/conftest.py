"""测试全局夹具：数据目录沙箱保险丝。

任何测试都不得读写真实数据目录 ~/.agentmemhub——会话级 autouse 夹具把
AGENTMEM_HUB_DATA_DIR 指到 pytest 临时目录（优先级高于 yaml，见 config.py）。
测试自己显式传 env 构造 Config 的行为不受影响。
"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _sandbox_data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("agentmemhub-data")
    old = os.environ.get("AGENTMEM_HUB_DATA_DIR")
    os.environ["AGENTMEM_HUB_DATA_DIR"] = str(d)
    yield
    if old is None:
        os.environ.pop("AGENTMEM_HUB_DATA_DIR", None)
    else:
        os.environ["AGENTMEM_HUB_DATA_DIR"] = old
