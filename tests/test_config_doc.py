# -*- coding: utf-8 -*-
"""`config_doc` 的解析守护：面板配置页的每一项都来自这里。

最大风险是**漏键**——面板少显示一项，没有任何报错，用户只会觉得"怎么没有
这一项"。所以核心测试不是逐个例子，而是拿真实 `agentmemhub.yaml` 与 PyYAML
做**叶子键集合**交叉校验（`test_与_yaml_safe_load_叶子一致`）。
"""
from __future__ import annotations

import pytest

from agentmemhub import config_doc


SAMPLE = """\
# ============================
# 顶层说明
# ============================

# 数据目录（db / watermarks）
data_dir: database
db_path: ""                      # 留空 = <data_dir>/agentmemhub.db

# 内置引擎
rag:
  # 激活模型
  active: bge-small-zh-v1.5   # 随仓库分发

  # ---- 写入策略 ----
  write:
    order: [a, b]
    fast_first: true

backend:
  # rag = 内置引擎
  backend: rag
"""


def test_解析出层级与点分路径():
    e = config_doc.parse_annotated(SAMPLE)
    paths = [x["path"] for x in e]
    assert paths == [
        "data_dir", "db_path", "rag", "rag.active",
        "rag.write", "rag.write.order", "rag.write.fast_first",
        "backend", "backend.backend",
    ]
    # depth 反映缩进层级，面板据此缩进渲染
    by = {x["path"]: x for x in e}
    assert by["rag"]["depth"] == 0 and by["rag.write.order"]["depth"] == 2
    assert by["rag.write"]["is_section"] is True
    assert by["rag.active"]["is_section"] is False


def test_注释归属_上方说明与行尾合并():
    by = {x["path"]: x for x in config_doc.parse_annotated(SAMPLE)}
    # 上方注释
    assert "数据目录" in by["data_dir"]["comment"]
    # 行尾注释
    assert "留空" in by["db_path"]["comment"]
    # 两者都有 → 都保留
    assert "激活模型" in by["rag.active"]["comment"]
    assert "随仓库分发" in by["rag.active"]["comment"]


def test_空行清空待归属注释_不串味():
    """分节空行之后的第一项，不该背上一节的说明。"""
    text = "# 这是第一节的说明\n\n\nfirst: 1\nsecond: 2\n"
    by = {x["path"]: x for x in config_doc.parse_annotated(text)}
    assert "第一节的说明" not in by["first"]["comment"]      # 空行清了
    assert by["second"]["comment"] == ""


def test_值原样保留():
    """`value_raw` 是 yaml 里的原文 —— 面板要显示"我写的那个"，不是 repr。"""
    by = {x["path"]: x for x in config_doc.parse_annotated(SAMPLE)}
    assert by["db_path"]["value_raw"] == '""'          # 引号留着
    assert by["rag.write.order"]["value_raw"] == "[a, b]"
    assert by["rag.write.fast_first"]["value_raw"] == "true"


def test_行号可用于定位():
    by = {x["path"]: x for x in config_doc.parse_annotated(SAMPLE)}
    assert by["data_dir"]["line"] == 6
    assert by["rag"]["line"] == 10      # 其上方注释在第 9 行，键在第 10 行


def test_与_yaml_safe_load_叶子一致():
    """行扫描最大的风险是**漏键**：面板少显示一项而无人察觉。

    拿真实 `agentmemhub.yaml` 与 PyYAML 的递归叶子清单比对 ——
    键集合必须完全一致（值不比，因为这里故意保留原文写法）。
    """
    import yaml

    from agentmemhub.config import PROJECT_ROOT
    text = (PROJECT_ROOT / "agentmemhub.yaml").read_text(encoding="utf-8")
    got = set(config_doc.to_leaves(config_doc.parse_annotated(text)))

    def walk(node, prefix=""):
        if isinstance(node, dict):
            out: set[str] = set()
            for k, v in node.items():
                out |= walk(v, f"{prefix}.{k}" if prefix else str(k))
            return out
        return {prefix}

    assert got == walk(yaml.safe_load(text))


def test_真实配置带注释的比例():
    """配置页的价值就在注释：如果真实文件里大部分键都没注释，说明这条路线白做。"""
    from agentmemhub.config import PROJECT_ROOT
    text = (PROJECT_ROOT / "agentmemhub.yaml").read_text(encoding="utf-8")
    leaves = [x for x in config_doc.parse_annotated(text) if not x["is_section"]]
    assert leaves, "真实配置没解析出叶子项"
    documented = [x for x in leaves if x["comment"]]
    assert len(documented) / len(leaves) >= 0.5, (
        f"仅 {len(documented)}/{len(leaves)} 个配置项带注释，配置页会显得空洞")


def test_文件不存在不抛(tmp_path):
    d = config_doc.load_annotated(tmp_path / "nope.yaml")
    assert d["exists"] is False
    assert d["entries"] == []


def test_load_annotated_带环境变量覆盖表():
    d = config_doc.load_annotated()
    assert d["exists"] is True
    envs = {o["env"] for o in d["env_overrides"]}
    assert "AGENTMEMHUB_PORT" in envs
    # 覆盖表里的 path 必须真的存在于配置里，否则"生效值可能不同"的提示指向空处
    paths = {e["path"] for e in d["entries"] if not e["is_section"]}
    for o in d["env_overrides"]:
        assert o["path"] in paths, f"{o['path']} 不在配置文件里"


# --------------------------------------------------------------- 体检（example ↔ 本地）
LOCAL = """\
a: 1
s: x
c: 3
secret_key: abc
"""

EXAMPLE = """\
a: 1
b: 2
s: "x"
secret_key: abc
"""


def test_对比_三类差异各自归位(tmp_path):
    (tmp_path / "l.yaml").write_text(LOCAL, encoding="utf-8")
    (tmp_path / "e.yaml").write_text(EXAMPLE, encoding="utf-8")
    d = config_doc.diff_example(tmp_path / "l.yaml", tmp_path / "e.yaml")
    assert [x["path"] for x in d["missing"]] == ["b"]
    assert [x["path"] for x in d["obsolete"]] == ["c"]
    # `s: x` 与 `s: "x"` 是同一个值 —— 引号差异绝不能报成"值不同"，
    # 否则真实配置里一堆带引号的键会把真问题淹掉。
    assert d["changed"] == []
    assert d["counts"] == {"missing": 1, "obsolete": 1, "changed": 0}


def test_对比_波浪号与_null_等价(tmp_path):
    (tmp_path / "l.yaml").write_text("n: ~\n", encoding="utf-8")
    (tmp_path / "e.yaml").write_text("n: null\n", encoding="utf-8")
    assert config_doc.diff_example(
        tmp_path / "l.yaml", tmp_path / "e.yaml")["counts"]["changed"] == 0


def test_对比_真差异照报并保留两侧原文(tmp_path):
    (tmp_path / "l.yaml").write_text("port: 8080\n", encoding="utf-8")
    (tmp_path / "e.yaml").write_text("port: 8000\n", encoding="utf-8")
    d = config_doc.diff_example(tmp_path / "l.yaml", tmp_path / "e.yaml")
    ch = d["changed"][0]
    assert ch["path"] == "port"
    assert ch["local_raw"] == "8080" and ch["example_raw"] == "8000"


def test_对比_example_缺失不抛(tmp_path):
    d = config_doc.diff_example(tmp_path / "noy.yaml", tmp_path / "noe.yaml")
    assert d["example_exists"] is False
    assert d["counts"] == {"missing": 0, "obsolete": 0, "changed": 0}


def test_对比_真实文件结构自洽():
    """真实文件上的不变式（不写死数量 —— 配置会变，写死等于给自己埋雷）。

    额外守一件事：`missing` 要带 **example 里的注释**。决定"这个新键要不要跟、
    跟成什么值"时，最需要的正是那段上下文；没有它就只剩一堆光秃秃的键名，
    人还得回去翻 example，体验等于没做。
    """
    d = config_doc.diff_example()
    assert d["example_exists"] is True
    assert d["local_exists"] is True
    for k in ("missing", "obsolete", "changed"):
        assert isinstance(d["counts"][k], int) and d["counts"][k] >= 0
    names = ([x["path"] for x in d["missing"]]
             + [x["path"] for x in d["obsolete"]])
    assert len(names) == len(set(names)), "missing 与 obsolete 不该有交集"
    if d["missing"]:
        assert any(x["comment"] for x in d["missing"]), "新增键应当带上 example 注释"

def test_值位只有注释时不会被当成值():
    """`key:   # 说明` 是模板里的"保留位"写法。

    原先只认 `" #"`，而这里 `#` 前没有值、只有空白，于是整段被当成值
    `# 说明` —— 配置页会显示一个不存在的值，`diff_example` 拿它去比对
    就会误报 changed。真实 yaml 目前没有这个形态，但模板一改就会踩。
    """
    entries = config_doc.parse_annotated("a:\n  keep:      # 保留位，暂未启用\n")
    rec = {e["path"]: e for e in entries if not e["is_section"]}
    assert rec["a.keep"]["value_raw"] == ""
    assert rec["a.keep"]["comment"] == "保留位，暂未启用"


def test_值里的井号不会被当成注释():
    yaml_text = "a:\n  url: http://example.com/#anchor\n"
    entries = config_doc.parse_annotated(yaml_text)
    rec = {e["path"]: e for e in entries if not e["is_section"]}
    assert rec["a.url"]["value_raw"] == "http://example.com/#anchor"
    assert rec["a.url"]["comment"] == ""
