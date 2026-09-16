"""fetch_model 纯逻辑测试（不触网）：续传计划、注册表条目、脚本可导入。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fetch_model.py"


@pytest.fixture(scope="module")
def fm():
    spec = importlib.util.spec_from_file_location("fetch_model", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_curl_cmd(fm):
    cmd = fm.build_curl_cmd("https://x/y", fm.Path("out.bin"))
    assert cmd[0] == "curl"
    assert "-C" in cmd and "-" in cmd, "必须断点续传"
    assert "-L" in cmd and "--fail" in cmd, "必须跟随 307 且非 2xx 报错"
    assert cmd[-1] == "https://x/y"
    assert "--proxy" not in cmd
    cmd2 = fm.build_curl_cmd("https://x/y", fm.Path("out.bin"),
                             proxy="http://127.0.0.1:8080")
    i = cmd2.index("--proxy")
    assert cmd2[i + 1] == "http://127.0.0.1:8080"


def test_registry_entry_shape(fm):
    e = fm.registry_entry("m1", 768, "Xenova/m1")
    assert e["path"] == "models/m1"
    assert e["dim"] == 768 and e["pooling"] == "cls" and e["quantized"]
    assert e["queryPrefix"] == ""


def test_core_files_cover_minimal_transformers_set(fm):
    names = set(fm.CORE_FILES)
    assert {"config.json", "tokenizer.json"} <= names
    assert any(p.startswith("onnx/") for p in names)
