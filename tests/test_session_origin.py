"""来源会话识别（`agentmemhub.session_origin`）测试。

覆盖三件事：

1. **值规范化**：裸会话 ID 补 `dsh:` 前缀、已带来源前缀的原样保留、
   空值归空 —— 这是"DSH 原生优先、同时兼容其它 harness"的落点；
2. **DSH 会话目录推断**：主会话优先于子代理、过期会话不认、
   多个候选取最近活动；
3. **fail-open 底线**：推断是给写入路径打标签的旁路，判不出来只能返回空串，
   **任何情况下都不许抛** —— 抛了就会连累 `memory_save` 写不进去。
"""
from __future__ import annotations

import os

import pytest

from agentmemhub import session_origin as so


# ---------------------------------------------------------------------------
# 值规范化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    (None, ""),
    ("   ", ""),
    # 裸值：视为 DSH 会话（当前唯一原生支持、能直接取到 ID 的 harness）
    ("session-50a7d24b-784e-4b16-b154-f7623f3a364c",
     "dsh:session-50a7d24b-784e-4b16-b154-f7623f3a364c"),
    ("abc123", "dsh:abc123"),
    # 已带来源前缀：原样保留，不重复加前缀
    ("dsh:session-abc", "dsh:session-abc"),
    ("claude:xyz", "claude:xyz"),
    ("cursor:1f2e", "cursor:1f2e"),
    # 两侧空白要被削掉
    ("  dsh:session-abc  ", "dsh:session-abc"),
])
def test_normalize_source_session(raw, expected):
    assert so.normalize_source_session(raw) == expected


def test_normalize_does_not_invent_prefix_for_empty():
    """空值必须归空，不能变成 `dsh:`（那会写进一条没有会话 ID 的脏标签）。"""
    assert so.normalize_source_session("") == ""
    assert so.normalize_source_session(None) == ""


# ---------------------------------------------------------------------------
# 推断：目录结构
# ---------------------------------------------------------------------------

def _make_session(root, workspace: str, session_id: str, *, mtime: float,
                  with_file: bool = True):
    """在临时根下造一个会话目录，返回其路径。

    真实 DSH 的结构是 `~/.dsh/sessions/<编码 cwd>/<会话 ID>/session.v4.jsonl.zstd`，
    这里只造参与判定的最小部分（文件名与 mtime）。
    """
    sess = root / workspace / session_id
    sess.mkdir(parents=True)
    if with_file:
        log = sess / so.DSH_SESSION_FILE
        log.write_bytes(b"")
        os.utime(log, (mtime, mtime))
    else:
        os.utime(sess, (mtime, mtime))
    return sess


def test_infer_missing_root_returns_empty(tmp_path):
    """根目录不存在（没装 DSH / 路径不同）→ 空串，不抛。"""
    assert so.infer_active_session(root=tmp_path / "nope") == ""


def test_infer_empty_root_returns_empty(tmp_path):
    assert so.infer_active_session(root=tmp_path) == ""


def test_infer_picks_fresh_main_session(tmp_path):
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-data-vibeCoding--", "session-aaa", mtime=now - 5)
    assert so.infer_active_session(root=tmp_path, now=now) == "dsh:session-aaa"


def test_infer_ignores_stale_session(tmp_path):
    """超出窗口的会话不认 —— 宁可留空，也不给记忆挂上昨天那个会话。"""
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-data-vibeCoding--", "session-old",
                  mtime=now - so.FRESH_WINDOW_S - 60)
    assert so.infer_active_session(root=tmp_path, now=now) == ""


def test_infer_prefers_main_over_newer_subagent(tmp_path):
    """主会话优先于子代理，**即使子代理写入更晚**。

    实测依据：主会话在驱动子代理，它的日志一定同时在被写；而子代理自己
    也有独立目录（裸 UUID）。两者取"最近"，会把记忆错标到子代理会话上。
    """
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-data-vibeCoding--", "session-main", mtime=now - 30)
    _make_session(tmp_path, "--D-data-vibeCoding--", "9c0658bd-sub", mtime=now - 1)
    assert so.infer_active_session(root=tmp_path, now=now) == "dsh:session-main"


def test_infer_falls_back_to_subagent_when_no_main(tmp_path):
    """只有子代理在窗口内时，仍然用它（聊胜于无，且确实是同一个会话树）。"""
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-data-vibeCoding--", "9c0658bd-sub", mtime=now - 1)
    assert so.infer_active_session(root=tmp_path, now=now) == "dsh:9c0658bd-sub"


def test_infer_picks_latest_among_multiple_mains(tmp_path):
    """多个主会话同时活跃 → 取最近活动的那个（并发场景，可能猜错，只作兜底）。"""
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-a--", "session-older", mtime=now - 100)
    _make_session(tmp_path, "--D-b--", "session-newer", mtime=now - 3)
    assert so.infer_active_session(root=tmp_path, now=now) == "dsh:session-newer"


def test_infer_uses_dir_mtime_when_log_missing(tmp_path):
    """没有 v4 日志文件的老会话：退回目录 mtime，仍可参与判定。"""
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-x--", "session-noLog", mtime=now - 2,
                  with_file=False)
    assert so.infer_active_session(root=tmp_path, now=now) == "dsh:session-noLog"


def test_infer_skips_non_directories_and_files(tmp_path):
    """工作区根/会话根里的散文件不能干扰判定。"""
    now = 1_800_000_000.0
    (tmp_path / "loose.txt").write_text("x", encoding="utf-8")
    _make_session(tmp_path, "--D-x--", "session-real", mtime=now - 1)
    (tmp_path / "--D-x--" / "stray.txt").write_text("y", encoding="utf-8")
    assert so.infer_active_session(root=tmp_path, now=now) == "dsh:session-real"


def test_infer_window_boundary_is_inclusive(tmp_path):
    """恰好等于窗口边界仍算活跃（`>` 判定，不是 `>=`）。"""
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-x--", "session-edge", mtime=now - so.FRESH_WINDOW_S)
    assert so.infer_active_session(root=tmp_path, now=now) == "dsh:session-edge"


# ---------------------------------------------------------------------------
# fail-open：绝不抛
# ---------------------------------------------------------------------------

def test_infer_never_raises_on_broken_root(tmp_path):
    """root 指向一个文件（不是目录）→ 返回空串，不抛。"""
    f = tmp_path / "not-a-dir"
    f.write_text("x", encoding="utf-8")
    assert so.infer_active_session(root=f) == ""


def test_infer_never_raises_on_unreadable_child(tmp_path, monkeypatch):
    """遍历子目录时抛 OSError 也要被吞掉（权限/瞬时故障）。"""
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-x--", "session-ok", mtime=now - 1)
    real_iterdir = type(tmp_path).iterdir

    def boom(self):
        if str(self).endswith("--D-x--"):
            raise OSError("permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(type(tmp_path), "iterdir", boom)
    assert so.infer_active_session(root=tmp_path, now=now) == ""


def test_dsh_root_points_at_user_home():
    """默认根目录必须是 `~/.dsh/sessions`（换机器/换用户名也要对）。"""
    root = so.dsh_sessions_root()
    assert root.name == "sessions"
    assert root.parent.name == ".dsh"
    assert str(root).startswith(os.path.expanduser("~"))
