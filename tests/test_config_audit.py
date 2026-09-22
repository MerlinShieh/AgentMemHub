# -*- coding: utf-8 -*-
"""配置键审计脚本（`scripts/check_config_keys.py`）的测试。

这个脚本本身是**回归防线** —— 防"配置键定义了却没代码读它"。所以测试也分两层：

  · 纯函数层：叶子键摊平、`DEFAULT_*` 定义块剔除、读取点匹配
  · **真实仓库自检**：当前 example 与本地 yaml 上跑一遍必须 0 僵尸键 ——
    将来谁加了配置键却没写读取点（或忘了从 example 删掉），这条会直接变红
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_config_keys as cck  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_leaves_摊平嵌套字典():
    assert cck.leaves({"a": {"b": 1, "c": {"d": 2}}, "e": 3}) == \
        ["a.b", "a.c.d", "e"]


def test_leaves_空字典与非字典():
    assert cck.leaves({}) == []
    assert cck.leaves({"a": None}) == ["a"]          # None 值也算叶子
    assert cck.leaves("不是字典") == []


def test_strip_default_blocks_删定义保留读取():
    """`DEFAULT_XXX = {...}` 是**定义**不是读取；`_get("k")` 才是读取证据。"""
    text = (
        "DEFAULT_WIKI = {\n"
        '    "workers": 4,\n'
        "}\n"
        "\n"
        "def f():\n"
        '    return self._get("data_dir", "")\n'
    )
    out = cck.strip_default_blocks(text)
    assert "DEFAULT_WIKI" not in out
    assert '"workers": 4' not in out, "定义块内的键名必须被剔除"
    assert '_get("data_dir", "")' in out, "定义块外的读取必须保留"


def test_strip_default_blocks_嵌套块整体删除():
    """`DEFAULT_LOGS` 这类是嵌套的 —— 必须按大括号计数删到真正闭合。"""
    text = (
        "DEFAULT_LOGS = {\n"
        '    "rotate": {\n'
        '        "max_mb": 5,\n'
        "    },\n"
        '    "files": {},\n'
        "}\n"
        "AFTER = 1\n"
    )
    out = cck.strip_default_blocks(text)
    assert "max_mb" not in out
    assert "rotate" not in out
    assert "AFTER = 1" in out, "块之后的内容不能被误删"


def test_find_reads_按字面量匹配():
    sources = {"a.py": 'x = cfg.get("workers")', "b.py": "无关内容"}
    assert cck.find_reads("workers", sources) == ["a.py"]
    assert cck.find_reads("完全不存在的键", sources) == []


# ---------------------------------------------------------------------------
# 端到端：构造一个"有僵尸键"的假仓库
# ---------------------------------------------------------------------------

def test_audit_能发现僵尸键(tmp_path):
    """核心能力：example 里写了、代码里没人读的键必须被报出来。

    特别注意 `unused` 同时出现在 `DEFAULT_X` 定义块里 —— 若脚本没剔除定义块，
    它会因为"在 config.py 里出现过"而被误判成有读取点，这条测试就会失败。
    """
    (tmp_path / "agentmemhub").mkdir()
    (tmp_path / "agentmemhub" / "config.py").write_text(
        'DEFAULT_X = {\n    "used": 1,\n    "unused": 2,\n}\n', encoding="utf-8")
    (tmp_path / "agentmemhub" / "core.py").write_text(
        'v = cfg.get("used")\n', encoding="utf-8")
    (tmp_path / "agentmemhub.yaml.example").write_text(
        "used: 1\nunused: 2\n", encoding="utf-8")

    r = cck.audit(tmp_path)["example（支持的键）"]
    assert r["keys"] == ["used", "unused"]
    assert r["dead"] == ["unused"], "没人读的键必须被报为僵尸"
    assert r["reads"]["used"] == ["agentmemhub/core.py"]


def test_audit_读取点在config属性内部也算数(tmp_path):
    """反过来的坑：读取点就在 `Config` 属性里（`self._get("data_dir")`），
    不能因为"命中文件是 config.py"就判成僵尸 —— 这是本脚本最容易写错的地方。"""
    (tmp_path / "agentmemhub").mkdir()
    (tmp_path / "agentmemhub" / "config.py").write_text(
        "DEFAULT_Y = 1\n"
        "\n"
        "class Config:\n"
        "    @property\n"
        "    def data_dir(self):\n"
        '        return self._get("data_dir", "")\n', encoding="utf-8")
    (tmp_path / "agentmemhub.yaml.example").write_text(
        "data_dir: database\n", encoding="utf-8")

    r = cck.audit(tmp_path)["example（支持的键）"]
    assert r["dead"] == [], "读取点在 config.py 内部时不应误报"


# ---------------------------------------------------------------------------
# 真实仓库自检（守护）
# ---------------------------------------------------------------------------

def test_真实仓库_所有配置键都有读取点():
    """**守护测试**：example 与本地 yaml 的每个叶子键都必须有读取点。

    本项目已经踩过三次同一个坑（`wiki.single_shot_max`、`wiki.workers`、
    `wiki.l2.*`，以及没有任何消费点的假开关 `wiki.enabled`），共同特征是
    **配置里的值恰好等于代码里硬编码的默认值** —— 所以永远不报错。
    这条测试就是为了让下一个坑在提交前就变红。
    """
    report = cck.audit(ROOT)
    for label, r in report.items():
        assert r["dead"] == [], "%s 存在疑似僵尸键：%s" % (label, r["dead"])


def test_真实仓库_收集源码时排除了自身与定义块():
    src = cck.collect_sources(ROOT)
    assert "agentmemhub/config.py" in src
    assert "scripts/check_config_keys.py" not in src, "脚本自身不该被算作读取点"
    assert "DEFAULT_WIKI = {" not in src["agentmemhub/config.py"], \
        "config.py 的 DEFAULT_* 定义块必须已被剔除"
