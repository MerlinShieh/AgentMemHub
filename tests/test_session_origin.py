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
from pathlib import Path

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


# ---------------------------------------------------------------------------
# 项目编码（DSH projectKey 对齐）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cwd,expected", [
    # 与 DSH 实测目录名逐字一致
    (r"D:\data\vibeCoding", "--D-data-vibeCoding--"),
    (r"C:\Users\Mulin", "--C-Users-Mulin--"),
    # Unix：根目录的 / 被 lstrip("-") 吃掉，不需要额外的分隔符
    ("/home/user/proj", "--home-user-proj--"),
    ("/home/data/vibeCoding", "--home-data-vibeCoding--"),
    # 根
    ("/", "--root--"),
    # 中文等非 ASCII → ~XXXX 转义（DSH 的规则，含 hex 码点）
    (r"D:\数据", "--D-~6570~636E--"),
    # 连续分隔符合并成一个 -
    (r"D:\\data\\\vibeCoding", "--D-data-vibeCoding--"),
    # 下划线/点/短横线保留
    (r"D:\a_b.c-d", "--D-a_b.c-d--"),
])
def test_project_key_matches_dsh(cwd, expected):
    """编码必须与 DSH 的 `projectKey()` 逐字一致 —— 这样 `~/.dsh/sessions/`
    的目录名可以直接当项目标识用，零解析、零维护。"""
    assert so.project_key(cwd) == expected


def test_project_key_truncates_at_251():
    """DSH 会截到 251 字符（路径组件上限），保持行为一致。"""
    long = "D:\\" + "a" * 400
    k = so.project_key(long)
    # 前后各 2 个 -，中间被截到 251
    assert len(k) == 251 + 4


@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    (None, ""),
    ("   ", ""),
    # 已是编码 → 原样
    ("--D-data-vibeCoding--", "--D-data-vibeCoding--"),
    # 路径 → 编码
    (r"D:\data\vibeCoding\Agent_Memory\AgentMemHub",
     "--D-data-vibeCoding-Agent_Memory-AgentMemHub--"),
    ("/home/user/proj", "--home-user-proj--"),
    # 逻辑名（无路径分隔符）→ 原样，供人工声明使用
    ("dsh-dev", "dsh-dev"),
])
def test_normalize_project(raw, expected):
    assert so.normalize_project(raw) == expected


def test_normalize_project_does_not_mistake_single_dash_pair():
    """`--` 只是两个短横线，不是合法编码（长度不 > 4），不能被当成编码放过。"""
    assert so.normalize_project("--") == "--"


# ---------------------------------------------------------------------------
# 项目推断
# ---------------------------------------------------------------------------

def test_infer_active_project_returns_workspace_name(tmp_path):
    """项目 = 工作区目录名（DSH 的编码），默认就取它 —— 不猜子目录。"""
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-data-vibeCoding--", "session-main", mtime=now - 5)
    assert so.infer_active_project(root=tmp_path, now=now) == "--D-data-vibeCoding--"


def test_infer_active_project_empty_when_stale(tmp_path):
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-x--", "session-old",
                  mtime=now - so.FRESH_WINDOW_S - 10)
    assert so.infer_active_project(root=tmp_path, now=now) == ""


def test_infer_session_and_project_agree(tmp_path):
    """两个推断函数必须指向**同一个会话**（否则记忆的桶和溯源会打架）。"""
    now = 1_800_000_000.0
    _make_session(tmp_path, "--D-a--", "session-x", mtime=now - 100)
    _make_session(tmp_path, "--D-b--", "session-y", mtime=now - 1)
    assert so.infer_active_session(root=tmp_path, now=now) == "dsh:session-y"
    assert so.infer_active_project(root=tmp_path, now=now) == "--D-b--"


def test_infer_active_project_never_raises(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("x", encoding="utf-8")
    assert so.infer_active_project(root=f) == ""


# ---------------------------------------------------------------------------
# 项目显示名
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    ("--D-data-vibeCoding-Agent_Memory-AgentMemHub--", "AgentMemHub"),
    ("--D-data-vibeCoding-virtual-projects-dsh--", "dsh"),
    ("--home-user-proj--", "proj"),
    ("", ""),
    # 逻辑名本来就是可读的，末段逻辑对它是恒等
    ("dsh-dev", "dsh-dev"),
])
def test_project_display_name_falls_back_to_tail(code, expected, monkeypatch):
    """没有配置映射时退到编码末段 —— 面板至少能看懂，而不是一长串路径。"""
    # 必须隔离真实配置：本机 yaml 里配了 names 映射，否则这里会读到 "DSH开发"
    from agentmemhub import config as hub_config

    class _Cfg:
        distillation = {}

    monkeypatch.setattr(hub_config, "config", lambda: _Cfg())
    assert so.project_display_name(code) == expected


def test_project_display_name_uses_config_mapping(monkeypatch):
    """有配置映射时以配置为准（编码是人造的，别名才是给人看的）。"""
    from agentmemhub import config as hub_config

    class _Cfg:
        distillation = {"project": {"names": {"--D-x--": "自定义名"}}}

    monkeypatch.setattr(hub_config, "config", lambda: _Cfg())
    assert so.project_display_name("--D-x--") == "自定义名"


# ---------------------------------------------------------------------------
# 活动推断（从会话日志的路径频次找真实项目，纯代码）
# ---------------------------------------------------------------------------

def _write_session_log(root: Path, workspace: str, sid: str,
                       events: list[dict], cwd: str) -> Path:
    """造一个 DSH 会话日志（单帧 zstd 的 jsonl，header 带 cwd）。"""
    import json as _json

    import zstandard

    d = root / workspace / sid
    d.mkdir(parents=True)
    lines = [_json.dumps({"cwd": cwd}, ensure_ascii=False)]
    lines += [_json.dumps(e, ensure_ascii=False) for e in events]
    raw = "\n".join(lines).encode("utf-8")
    p = d / so.DSH_SESSION_FILE
    p.write_bytes(zstandard.ZstdCompressor().compress(raw))
    return p


def _events_for(paths: list[str]) -> list[dict]:
    """每条事件里塞一个带路径的工具调用参数（与真实结构近似）。"""
    return [{"type": "tool/call", "data": {"arguments": {"file_path": p}}}
            for p in paths]


def test_infer_project_from_activity_finds_marked_project(tmp_path):
    """有 `pyproject.toml` 的那层被认作项目根（标记文件是硬信号，不是目录名）。"""
    ws = tmp_path / "ws"
    proj = ws / "Agent_Memory" / "AgentMemHub"
    proj.mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="utf-8")

    logs = tmp_path / "sessions"
    _write_session_log(logs, "--D-x--", "session-aaa",
                       _events_for([str(proj / f"f{i}.py") for i in range(5)]),
                       cwd=str(ws))
    code = so.infer_project_from_activity(
        session_id="session-aaa", root=logs, use_cache=False)
    assert code == so.project_key(str(proj))


def test_infer_project_from_activity_rejects_bare_workspace(tmp_path):
    """只引用工作区根的文件 → 不算"更细的项目"，返回空（让调用方回退）。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "readme.md").write_text("", encoding="utf-8")
    logs = tmp_path / "sessions"
    _write_session_log(logs, "--D-x--", "session-bbb",
                       _events_for([str(ws / f"f{i}.md") for i in range(6)]),
                       cwd=str(ws))
    assert so.infer_project_from_activity(
        session_id="session-bbb", root=logs, use_cache=False) == ""


def test_infer_project_from_activity_needs_enough_evidence(tmp_path):
    """命中次数低于阈值就不猜 —— 标错项目比回退工作区更糟。"""
    ws = tmp_path / "ws"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="utf-8")
    logs = tmp_path / "sessions"
    # 只引用 2 次，低于默认阈值 3
    _write_session_log(logs, "--D-x--", "session-ccc",
                       _events_for([str(proj / "a.py"), str(proj / "b.py")]),
                       cwd=str(ws))
    assert so.infer_project_from_activity(
        session_id="session-ccc", root=logs, use_cache=False) == ""


def test_infer_project_from_activity_ignores_noise_dirs(tmp_path):
    """`node_modules` / `.git` 之类的路径不参与统计。"""
    ws = tmp_path / "ws"
    proj = ws / "proj"
    (proj / "node_modules" / "pkg").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="utf-8")
    noisy = [str(proj / "node_modules" / "pkg" / f"x{i}.js") for i in range(10)]
    logs = tmp_path / "sessions"
    _write_session_log(logs, "--D-x--", "session-ddd",
                       _events_for(noisy), cwd=str(ws))
    assert so.infer_project_from_activity(
        session_id="session-ddd", root=logs, use_cache=False) == ""


def test_infer_project_from_activity_missing_log(tmp_path):
    """会话日志不存在 → 空串，不抛。"""
    assert so.infer_project_from_activity(
        session_id="nope", root=tmp_path, use_cache=False) == ""


def test_infer_project_from_activity_empty_session_id(tmp_path):
    assert so.infer_project_from_activity(
        session_id="", root=tmp_path, use_cache=False) == ""


def test_infer_project_from_activity_uses_cache(tmp_path, monkeypatch):
    """缓存命中时不再解压日志（第二次调用不该再读文件）。"""
    ws = tmp_path / "ws"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="utf-8")
    logs = tmp_path / "sessions"
    _write_session_log(logs, "--D-x--", "session-eee",
                       _events_for([str(proj / f"f{i}.py") for i in range(5)]),
                       cwd=str(ws))

    so._ACTIVITY_CACHE.clear()
    first = so.infer_project_from_activity(session_id="session-eee", root=logs)
    assert first == so.project_key(str(proj))

    # 第二次：把日志文件删掉——若仍能返回，说明走的是缓存而非重新解压
    (logs / "--D-x--" / "session-eee" / so.DSH_SESSION_FILE).unlink()
    second = so.infer_project_from_activity(session_id="session-eee", root=logs)
    assert second == first
    so._ACTIVITY_CACHE.clear()
