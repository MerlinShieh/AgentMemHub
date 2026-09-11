"""统一配置体系测试：默认值 / 环境变量覆盖 / YAML 文件 / 派生路径。"""
from pathlib import Path

from agentmemhub import config as C


def _cfg(env=None, path=None):
    return C.Config(env=env or {}, path=path)


def test_defaults():
    c = _cfg()
    # 默认数据目录在项目内（database/，随项目走便于备份迁移）
    assert c.data_dir == C.PROJECT_ROOT / "database"
    assert c.db_path == C.PROJECT_ROOT / "database" / "agentmemhub.db"
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


# ── LLM 配置（蒸馏 / 评分共用）─────────────────────────────────────────

def test_llm_defaults_to_empty(tmp_path):
    c = _cfg(env={}, path=tmp_path / "none.yaml")
    assert c.llm == {"endpoint": "", "api_key": "", "model": ""}


def test_llm_from_yaml(tmp_path):
    f = tmp_path / "agentmemhub.yaml"
    f.write_text(
        "llm:\n"
        "  endpoint: https://example.com/v1/chat/completions\n"
        "  api_key: sk-test\n"
        "  model: deepseek-flash\n", encoding="utf-8")
    c = _cfg(path=f)
    assert c.llm == {"endpoint": "https://example.com/v1/chat/completions",
                     "api_key": "sk-test", "model": "deepseek-flash"}


def test_llm_env_overrides_yaml(tmp_path):
    f = tmp_path / "agentmemhub.yaml"
    f.write_text("llm:\n  endpoint: http://old\n  api_key: old\n  model: old\n",
                 encoding="utf-8")
    c = _cfg(env={"AGENTMEMHUB_LLM_MODEL": "deepseek-flash"}, path=f)
    assert c.llm["model"] == "deepseek-flash"     # env 覆盖
    assert c.llm["endpoint"] == "http://old"      # 其余仍来自 yaml


# ── 记忆蒸馏配置 ──────────────────────────────────────────────────────

def test_distillation_defaults(tmp_path):
    c = _cfg(env={}, path=tmp_path / "none.yaml")
    d = c.distillation
    assert d["enabled"] is True
    assert d["prompt_ver"] == 1
    assert d["slice"]["max_chars"] == 24000
    assert d["slice"]["max_turns"] == 16
    assert d["slice"]["topic_boundary"] is True
    assert d["merge"]["enabled"] is True
    assert d["dedup"]["cosine_duplicate"] == 0.92
    assert d["dedup"]["cosine_similar"] == 0.80
    assert d["sanitize"]["enabled"] is True
    assert d["runtime"]["max_concurrent"] == 4
    assert d["runtime"]["dry_run"] is False


def test_distillation_yaml_deep_merge(tmp_path):
    """yaml 只覆盖部分键时其余默认值保留（嵌套子段各自合并，非整段替换）。"""
    f = tmp_path / "agentmemhub.yaml"
    f.write_text(
        "distillation:\n"
        "  prompt_ver: 2\n"
        "  slice:\n"
        "    max_chars: 8000\n", encoding="utf-8")
    c = _cfg(path=f)
    d = c.distillation
    assert d["prompt_ver"] == 2
    assert d["slice"]["max_chars"] == 8000
    assert d["slice"]["max_turns"] == 16          # 未覆盖 → 默认保留
    assert d["dedup"]["cosine_similar"] == 0.80   # 整段未覆盖 → 默认保留
    assert d["enabled"] is True


def test_distillation_llm_inherits_top_level(tmp_path):
    """distillation.llm 留空 → 继承顶层 llm（默认只需配一处）。"""
    f = tmp_path / "agentmemhub.yaml"
    f.write_text(
        "llm:\n  endpoint: https://top/v1\n  api_key: k\n  model: m\n"
        "distillation:\n  enabled: true\n", encoding="utf-8")
    c = _cfg(path=f)
    assert c.distillation["llm"] == {"endpoint": "https://top/v1",
                                     "api_key": "k", "model": "m"}


def test_distillation_llm_partially_overrides_top_level(tmp_path):
    """distillation.llm 指定字段覆盖顶层同名；未指定字段仍继承。"""
    f = tmp_path / "agentmemhub.yaml"
    f.write_text(
        "llm:\n  endpoint: https://top/v1\n  api_key: k\n  model: m\n"
        "distillation:\n  llm:\n    model: cheap-model\n", encoding="utf-8")
    c = _cfg(path=f)
    d = c.distillation["llm"]
    assert d["model"] == "cheap-model"
    assert d["endpoint"] == "https://top/v1"
    assert d["api_key"] == "k"
