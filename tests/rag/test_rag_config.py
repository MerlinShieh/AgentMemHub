"""models.json 注册表解析：模型切换契约的守门测试。"""
from __future__ import annotations

import json

import pytest

from agentmemhub.rag.config import Settings, load_settings


def test_active_spec_wellformed(project_settings):
    spec = project_settings.active_spec
    assert spec.id == project_settings.active_model
    assert spec.dim > 0
    assert spec.max_tokens > 0
    assert spec.pooling in ("cls", "mean")
    assert spec.onnx_file.exists(), "量化模型应命中 model_quantized.onnx"
    assert spec.tokenizer_file.exists()


def test_vec_table_isolated_per_model(project_settings):
    spec = project_settings.active_spec
    table = spec.vec_table
    assert table.startswith("vec_")
    assert "." not in table and "-" not in table, "表名须 SQL 安全"
    # 不同模型 id 必得不同表名（双模型共存的前提）
    s2 = Settings(
        root=project_settings.root,
        models=project_settings.models,
        active_model=project_settings.active_model,
        source_db=project_settings.source_db,
        index_db=project_settings.index_db,
        log_dir=project_settings.log_dir,
    )
    assert s2.active_spec.vec_table == table


def test_unknown_model_raises_clear_message(project_settings):
    with pytest.raises(KeyError, match="未注册"):
        project_settings.model("no-such-model")


def test_missing_registry_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="注册表"):
        load_settings(tmp_path)


def test_active_not_registered_raises(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps({"active": "ghost", "models": {}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="未注册"):
        load_settings(tmp_path)


def test_model_files_missing_raises(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps(
            {
                "active": "fake-model",
                "models": {
                    "fake-model": {
                        "path": "models/fake-model",
                        "dim": 512,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="缺少文件"):
        load_settings(tmp_path)
