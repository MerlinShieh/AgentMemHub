"""统一配置体系测试：默认值 / 环境变量覆盖 / YAML 文件 / 派生路径。"""
from pathlib import Path

from agentmemhub import config as C


def _cfg(env=None, path=None):
    return C.Config(env=env or {}, path=path)


def test_defaults():
    c = _cfg()
    assert c.data_dir == Path.home() / ".agentmemhub"
    assert c.db_path == Path.home() / ".agentmemhub" / "agentmemhub.db"
    assert c.web_port == 8086
    # MemOS repo 默认 = 项目根/memOS
    assert c.memos_repo_dir == C.PROJECT_ROOT / "memOS"
    assert c.memos_base_url == "http://127.0.0.1:18800"
    # 默认推导路径正确（memOS/ 是否已平移由环境决定，存在性不在此断言）
    assert c.memos_plugin_dir == C.PROJECT_ROOT / "memOS" / "apps" / "memos-local-plugin"
    assert c.memos_password == ""
    assert c.memos_lightweight is None


def test_env_overrides():
    c = _cfg(env={
        "AGENTMEMHUB_DB": "D:/x/y.db",
        "AGENTMEMHUB_PORT": "9000",
        "MEMOS_REPO_DIR": "D:/memos",
        "MEMOS_BASE_URL": "http://127.0.0.1:9999",
    })
    assert c.db_path == Path("D:/x/y.db")
    assert c.web_port == 9000
    assert c.memos_repo_dir == Path("D:/memos")
    assert c.memos_base_url == "http://127.0.0.1:9999"


def test_yaml_file_and_derivation(tmp_path):
    f = tmp_path / "agentmemhub.yaml"
    f.write_text("""
data_dir: "./custom-data"
db_path: "./custom-data/app.db"
web:
  port: 9001
agents:
  hermes: "~/hermes-custom/state.db"
memos:
  repo_dir: "~/memos-place"
  password: "sekrit"
  lightweight: false
""", encoding="utf-8")
    c = _cfg(path=f)
    assert c.data_dir == C.PROJECT_ROOT / "custom-data"
    assert c.db_path == C.PROJECT_ROOT / "custom-data" / "app.db"
    assert c.web_port == 9001
    assert c.agent_path("hermes") == Path.home() / "hermes-custom" / "state.db"
    assert c.agent_path("zcode") is None
    assert c.memos_repo_dir == Path.home() / "memos-place"
    assert c.memos_plugin_dir == Path.home() / "memos-place" / "apps" / "memos-local-plugin"
    assert c.memos_password == "sekrit"
    assert c.memos_lightweight is False