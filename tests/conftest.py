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


@pytest.fixture(autouse=True)
def _no_wiki_side_effects(monkeypatch):
    """wiki 侧效应隔离：任何测试都不得触碰真实 wiki 产物目录。

    （实测事故：run_distill 测试触发蒸馏收尾钩子 → 钩子用真实 yaml 的
    wiki.out_l1/out_l2 + 测试 tmp 库跑了增量更新，把真实 manifest 覆盖成
    tmp 库快照、真实 L1 页面被测试编译产物覆盖。）

    做法：把 wiki_triggers._targets 钉为报错——钩子链路在测试里必然早退；
    需要测触发器/update 的用例自己 monkeypatch _targets 指向 tmp（栈顶覆盖）。
    """
    import agentmemhub.wiki_triggers as wt

    def _forbidden(*a, **kw):
        raise RuntimeError("测试环境禁止解析真实 wiki 产出目录")

    monkeypatch.setattr(wt, "_targets", _forbidden)
    # 上面这道 monkeypatch 挡不住**活过测试的线程**（MCP 写入钩子会起 daemon
    # 线程，线程在 patch 撤销后才执行 → 拿到真实配置）。真正的兜底是
    # `_sandbox_data_dir` 里那个**进程级**环境变量熔断，见 `wt.SANDBOX_ENV`。
    # 实测事故：2026-09-26 一次测试越界把真实 out_l1 删剩 25 页。


@pytest.fixture(scope="session", autouse=True)
def _sandbox_data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("agentmemhub-data")
    old = os.environ.get("AGENTMEM_HUB_DATA_DIR")
    oldb = os.environ.get("AGENTMEMHUB_BACKEND")
    olds = os.environ.get("AGENTMEMHUB_TEST_SANDBOX")
    os.environ["AGENTMEM_HUB_DATA_DIR"] = str(d)
    os.environ.setdefault("AGENTMEMHUB_BACKEND", "memos")
    # **进程级** wiki 熔断：与上面按测试生效的 monkeypatch 不同，环境变量在整个
    # 会话内始终置位，且会被子进程继承 —— 这样即使有线程/子进程在测试结束后才
    # 执行到 `wiki_triggers._targets()`，也绝不可能解析到真实产物目录。
    # （2026-09-26 实测：一次测试越界用 tmp 库"全量更新"了真实 out_l1。）
    os.environ["AGENTMEMHUB_TEST_SANDBOX"] = "1"
    # 光有产物熔断还不够：**日志目录**也要跟着沙箱走。上面那道 monkeypatch
    # （`audit_dir`）只对当前测试生效，逃逸的线程/子进程反而在 patch 撤销后
    # 拿到真实的 `PROJECT_ROOT/logs` —— 实测生产 `logs/wiki.log` 里混进了
    # 140 条 `测试环境禁止解析真实 wiki 产出目录`（正是被熔断拦下后回头留痕的）。
    # 环境变量是进程级的，`logs.log_dir()` 认它。
    oldl = os.environ.get("AGENTMEMHUB_TEST_LOG_DIR")
    os.environ["AGENTMEMHUB_TEST_LOG_DIR"] = str(d / "logs")
    yield
    if oldl is None:
        os.environ.pop("AGENTMEMHUB_TEST_LOG_DIR", None)
    else:
        os.environ["AGENTMEMHUB_TEST_LOG_DIR"] = oldl
    if old is None:
        os.environ.pop("AGENTMEM_HUB_DATA_DIR", None)
    else:
        os.environ["AGENTMEM_HUB_DATA_DIR"] = old
    if oldb is None:
        os.environ.pop("AGENTMEMHUB_BACKEND", None)
    else:
        os.environ["AGENTMEMHUB_BACKEND"] = oldb
    if olds is None:
        os.environ.pop("AGENTMEMHUB_TEST_SANDBOX", None)
    else:
        os.environ["AGENTMEMHUB_TEST_SANDBOX"] = olds
