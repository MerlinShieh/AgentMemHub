"""`wiki.update` 的脏清单解析：L1 已同步、仅 L2 落后时也要能推进。

2026-11-07 实测场景：触发器跑完 L1（18 会话 147 页），但那一轮 L2 只重编了
16/18 个域；此后人工跑 `update` 因 L1 无脏会话而直接早退，落后的 L2 永远
推不动，只能等下次触发器或跑全量（约 12 小时）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agentmemhub import wiki


def _a1(dirty: dict | None = None) -> dict:
    return {"stages": {"l1": {"dirty_sessions": dirty or {}}},
            "needs_recompile": bool(dirty)}


def _a2(dirty: dict | None = None) -> dict:
    return {"stages": {"l2": {"dirty_sessions": dirty or {}}},
            "needs_recompile": bool(dirty)}


def test_l1有脏会话_正常收集():
    dirty, stale, early = wiki._resolve_dirty(
        _a1({"mcp/direct": {"added": 6}}), _a2(), full=False)
    assert dirty == {("mcp", "direct")}
    assert stale == set()
    assert early == ""


def test_l1无脏但l2落后_反推受影响的L1文件():
    """核心回归：不能因为 L1 干净就早退。"""
    dirty, stale, early = wiki._resolve_dirty(
        _a1(), _a2({"hermes/20260808_1730": {"removed": 1},
                    "mcp/direct": {"removed": 20}}), full=False)
    assert dirty == set()
    assert stale == {"hermes__20260808_1730.md", "mcp__direct.md"}
    assert early == "", "L2 落后时必须继续，不能早退"


def test_两级都干净_早退():
    dirty, stale, early = wiki._resolve_dirty(_a1(), _a2(), full=False)
    assert early == "没有可定位的脏会话"


def test_全量模式不受脏清单约束():
    """`full-run` 不依赖脏会话（它要重来的是域结构）。"""
    dirty, stale, early = wiki._resolve_dirty(_a1(), _a2(), full=True)
    assert early == ""
    assert dirty == set() and stale == set()


def test_脏清单键格式异常时忽略而不崩():
    """align 的键若不带 `/`，跳过而不是抛异常。"""
    dirty, stale, early = wiki._resolve_dirty(
        _a1(), _a2({"no-slash": {"removed": 1}}), full=False)
    assert stale == set()
    assert early == "没有可定位的脏会话"


def test_preset为空时不再回退全量_而是早退():
    """保险：`preset` 为空时聚合层会回退"全量归域 + 全库重编"（极贵）。

    走到那里说明基线落后却定位不到域（典型：L2 页缺 from_files），
    此时必须早退并明说，不能悄悄烧一小时把知识库整库重编。
    """
    src = Path(wiki.__file__).read_text(encoding="utf-8")
    assert "if not preset and not full:" in src
    idx = src.index("if not preset and not full:")
    seg = src[idx:idx + 500]
    assert "full-run" in seg, "早退提示必须指向 full-run"
