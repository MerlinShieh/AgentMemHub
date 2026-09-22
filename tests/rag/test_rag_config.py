"""配置体系测试：yaml 统一配置优先、旧 models.json 回退、onnx 文件名探测、契约校验。"""
from __future__ import annotations

import json

import pytest

from agentmemhub.rag.config import PROJECT_ROOT, Settings, load_settings

YAML_M1 = """rag:
  active: m1
  embed:
    batch_size: 8
    bucketing: false
  write:
    order: [m1]
    fast_first: false
  retrieval:
    models: [m1]
    search_max_hits: 7
  models:
    m1:
      path: models/m1
      dim: 384
      family: test
"""


def _mk_model(root, mid, dim=384, onnx_name="model_quantized.onnx"):
    mdir = root / "models" / mid
    (mdir / "onnx").mkdir(parents=True)
    (mdir / "onnx" / onnx_name).write_bytes(b"x")
    (mdir / "tokenizer.json").write_text("{}", encoding="utf-8")
    (mdir / "config.json").write_text(json.dumps({"hidden_size": dim}),
                                      encoding="utf-8")
    return mdir


def test_active_spec_wellformed(project_settings):
    spec = project_settings.active_spec
    assert spec.id == project_settings.active_model
    assert spec.dim > 0 and spec.max_tokens > 0
    assert spec.pooling in ("cls", "mean")
    assert spec.onnx_file.exists()
    assert spec.tokenizer_file.exists()


def test_vec_table_isolated_per_model(project_settings):
    spec = project_settings.active_spec
    table = spec.vec_table
    assert table.startswith("vec_")
    assert "." not in table and "-" not in table, "表名须 SQL 安全"
    s2 = Settings(root=project_settings.root, models=project_settings.models,
                  active_model=project_settings.active_model,
                  source_db=project_settings.source_db,
                  index_db=project_settings.index_db,
                  log_dir=project_settings.log_dir)
    assert s2.active_spec.vec_table == table


def test_unknown_model_raises_clear_message(project_settings):
    with pytest.raises(KeyError, match="未注册"):
        project_settings.model("no-such-model")


def test_missing_registry_file(tmp_path):
    """无 agentmemhub.yaml 也无 models.json → 明确报错（新配置体系）。"""
    with pytest.raises(FileNotFoundError, match="未找到模型配置"):
        load_settings(tmp_path)


def test_active_not_registered_raises(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps({"active": "ghost", "models": {}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="未注册"):
        load_settings(tmp_path)


def test_yaml_config_takes_precedence(tmp_path):
    """agentmemhub.yaml 的 rag 段优先于 models.json（统一配置契约）。"""
    _mk_model(tmp_path, "m1", 384)
    # models.json 指向别的 active，yaml 应覆盖它
    (tmp_path / "models.json").write_text(json.dumps(
        {"active": "old", "models": {"old": {"path": "models/nope", "dim": 1}}}),
        encoding="utf-8")
    (tmp_path / "agentmemhub.yaml").write_text(YAML_M1, encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.active_model == "m1"
    assert s.embed["batch_size"] == 8 and s.embed["bucketing"] is False
    assert s.write["fast_first"] is False
    assert s.retrieval["search_max_hits"] == 7
    assert s.models["m1"].family == "test"
    assert s.write_order == ["m1"] and s.retrieval_models == ["m1"]


def test_models_json_fallback(tmp_path):
    """无 yaml 时回退旧 models.json（向后兼容）。"""
    _mk_model(tmp_path, "legacy", 512)
    (tmp_path / "models.json").write_text(json.dumps({
        "active": "legacy",
        "models": {"legacy": {"path": "models/legacy", "dim": 512}},
    }), encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.active_model == "legacy"
    assert s.embed["batch_size"] == 32          # 内置默认


def test_onnx_filename_autodetect(tmp_path):
    """量化文件名不统一（Xenova=model_quantized，text2vec=model_qint8_*）。"""
    _mk_model(tmp_path, "m2", 768, onnx_name="model_qint8_avx512_vnni.onnx")
    (tmp_path / "agentmemhub.yaml").write_text(
        "rag:\n  active: m2\n  models:\n    m2:\n"
        "      path: models/m2\n      dim: 768\n      quantized: true\n",
        encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.active_spec.onnx_file.name == "model_qint8_avx512_vnni.onnx"


def test_write_order_and_retrieval_models_default_to_active(tmp_path):
    """未配置 order/models 时，两处都退化为仅 active（单模型语义）。"""
    _mk_model(tmp_path, "only", 384)
    (tmp_path / "agentmemhub.yaml").write_text(
        "rag:\n  active: only\n  models:\n    only:\n"
        "      path: models/only\n      dim: 384\n", encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.write_order == ["only"]
    assert s.retrieval_models == ["only"]


def test_write_order_ignores_unregistered(tmp_path):
    """order 里未注册的模型被忽略，不报错（配置容错）。"""
    _mk_model(tmp_path, "a", 384)
    (tmp_path / "agentmemhub.yaml").write_text(
        "rag:\n  active: a\n  write:\n    order: [ghost, a]\n  models:\n    a:\n"
        "      path: models/a\n      dim: 384\n", encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.write_order == ["a"]


def test_model_files_missing_raises(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps({
            "active": "fake-model",
            "models": {"fake-model": {"path": "models/fake-model", "dim": 512}},
        }), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="缺少文件"):
        load_settings(tmp_path)


# ── 沙箱隔离：AGENTMEM_HUB_DATA_DIR 覆盖数据目录 ─────────────────────────

def test_env_override_switches_both_dbs(monkeypatch, tmp_path):
    """无参调用时环境变量生效：一个开关同时切换采集库与索引库（沙箱语义）。"""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENTMEM_HUB_DATA_DIR", str(sandbox))
    s = load_settings()
    assert s.source_db == sandbox / "agentmemhub.db"
    assert s.index_db == sandbox / "session_rag.db"
    # 日志不跟随沙箱——既定语义是统一 <程序根>/logs（logs.py 注释）
    assert s.log_dir == PROJECT_ROOT / "logs"


def test_explicit_root_ignores_env(monkeypatch, tmp_path):
    """显式传 root 时不受环境变量干扰（测试隔离契约：调用方指定根即完整隔离）。"""
    monkeypatch.setenv("AGENTMEM_HUB_DATA_DIR", str(tmp_path / "elsewhere"))
    _mk_model(tmp_path, "m1", 384)
    (tmp_path / "agentmemhub.yaml").write_text(YAML_M1, encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.source_db == tmp_path / "database" / "agentmemhub.db"
    assert s.index_db == tmp_path / "database" / "session_rag.db"


def test_no_env_falls_back_to_project_database(monkeypatch, tmp_path):
    """无环境变量时回退项目内 database/（生产默认）。"""
    monkeypatch.delenv("AGENTMEM_HUB_DATA_DIR", raising=False)
    s = load_settings()
    assert s.source_db == PROJECT_ROOT / "database" / "agentmemhub.db"
    assert s.index_db == PROJECT_ROOT / "database" / "session_rag.db"


# ── 召回严格度档位（rag.retrieval.recall_level，1 最严格 … 5 最宽松）──────

def _settings_at(tmp_path, retrieval_yaml: str = ""):
    """在临时根下构造可加载的配置：`rag:` 段内可插入 retrieval 子段。"""
    _mk_model(tmp_path, "m", 384)
    body = ("rag:\n  active: m\n" + (retrieval_yaml or "")
            + "  models:\n    m:\n      path: models/m\n      dim: 384\n")
    (tmp_path / "agentmemhub.yaml").write_text(body, encoding="utf-8")
    return load_settings(tmp_path)


def test_召回档位_默认均衡(tmp_path):
    """默认 3（均衡）：实测 1~2 档过于极端（三个查询各只返回 1 条）。"""
    from agentmemhub.rag.config import DEFAULT_RECALL_LEVEL, RECALL_LEVELS
    s = _settings_at(tmp_path)
    assert DEFAULT_RECALL_LEVEL == 3
    assert s.recall_level == 3
    assert s.recall_profile["name"] == RECALL_LEVELS[3]["name"] == "均衡"


@pytest.mark.parametrize("bad", ['0', '6', '-1', 'abc', '""', 'null'])
def test_召回档位_非法值回退默认(tmp_path, bad):
    """档位决定"给不给结果"，写错时回退默认档而不是静默放宽。"""
    from agentmemhub.rag.config import DEFAULT_RECALL_LEVEL
    s = _settings_at(tmp_path, f"  retrieval:\n    recall_level: {bad}\n")
    assert s.recall_level == DEFAULT_RECALL_LEVEL


def test_召回档位_展开参数并驱动页面策略(tmp_path):
    s = _settings_at(tmp_path, "  retrieval:\n    recall_level: 5\n")
    prof = s.recall_profile
    assert prof["level"] == 5 and prof["name"] == "最宽松"
    assert prof["candidate_k"] == 60 and prof["curate_floor"] == 0.7
    pol = s.page_policy                      # 档位同时驱动页面层
    assert pol["max_in_results"] == 5 and pol["min_evidence"] == "any"


def test_页面策略_显式配置覆盖档位(tmp_path):
    """想单独调某一项时，yaml 显式项覆盖档位、不必换档。"""
    s = _settings_at(tmp_path, "  retrieval:\n    recall_level: 5\n"
                               "    page:\n      max_in_results: 2\n")
    pol = s.page_policy
    assert pol["max_in_results"] == 2        # 显式覆盖
    assert pol["literal_seats"] == 3         # 未覆盖项仍随档位


def test_召回档位_逐档放宽是单调的():
    """1→5 必须**单调**放宽：候选更多、阈值更低、页面席位更多、证据要求更松。

    这条防止将来改档位表时把刻度改乱（例如把 2 档的席位写得比 3 档还多）——
    档位的唯一价值就是"一个方向上的刻度"，非单调就失去意义。
    """
    from agentmemhub.rag.config import RECALL_LEVELS
    ks, floors, curates, seats, literals, weaken, np_literals = \
        [], [], [], [], [], [], []
    for lv in range(1, 6):
        p = RECALL_LEVELS[lv]
        ks.append(p["candidate_k"])
        floors.append(p["threshold_floor"])
        curates.append(p["curate_floor"])
        seats.append(p["page"]["max_in_results"])
        literals.append(p["page"]["literal_seats"])
        weaken.append(p["page"]["literal_required_below"])
        np_literals.append(p["nonpage_literal_seats"])
        assert "min_evidence" not in p["page"], \
            "档位不得设证据硬门槛——它会连高分页面一起挡掉"
    assert ks == sorted(ks), "候选宽度应逐档变大"
    assert floors == sorted(floors, reverse=True), "相对阈值应逐档变松"
    assert curates == sorted(curates, reverse=True), "终审阈值应逐档变松"
    assert seats == sorted(seats), "页面席位应逐档变多"
    assert literals == sorted(literals), "字面兜底席位应逐档变多"
    assert np_literals == sorted(np_literals), "非页面字面兜底席应逐档变多"
    assert weaken == sorted(weaken, reverse=True), "字面证据门槛线应逐档降低"
    assert RECALL_LEVELS[1]["page"]["max_in_results"] == 1
    assert RECALL_LEVELS[5]["page"]["max_in_results"] == 5
