# -*- coding: utf-8 -*-
"""Trae 适配器测试：快照 diff / SOLO 清单 / 项目记忆 / 无 git 降级 / src_id 幂等。"""
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentmemhub.adapters.trae import TraeAdapter

GIT = shutil.which("git")
_GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def _git(cwd: Path, *args: str) -> None:
    r = subprocess.run([GIT, *_GIT_ID, *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr


def _make_appdata(tmp_path: Path) -> Path:
    """构造 %APPDATA%/Trae CN 最小结构，返回根目录。"""
    root = tmp_path / "Trae CN"
    (root / "ModularData" / "ai-agent").mkdir(parents=True)
    return root


def _make_snapshot(appdata: Path, sid: str = "aaaabbbbccccdddd") -> Path:
    """一个带两轮 turn 标签的快照仓库：turn1 改 a.txt，turn2 加 b.txt。"""
    v2 = appdata / "ModularData" / "ai-agent" / "snapshot" / sid / "v2"
    v2.mkdir(parents=True)
    _git(v2, "init", "-q")
    (v2 / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(v2, "add", "-A")
    _git(v2, "commit", "-qm", "init")
    _git(v2, "tag", "before-chat-turn-1")
    (v2 / "a.txt").write_text("v2 fixed\n", encoding="utf-8")
    _git(v2, "add", "-A")
    _git(v2, "commit", "-qm", "turn1")
    _git(v2, "tag", "after-chat-turn-1")
    _git(v2, "tag", "before-chat-turn-2")
    (v2 / "b.txt").write_text("new file\n", encoding="utf-8")
    _git(v2, "add", "-A")
    _git(v2, "commit", "-qm", "turn2")
    _git(v2, "tag", "after-chat-turn-2")
    return v2


def _make_sandbox(appdata: Path, sid: str) -> None:
    sb = appdata / "ModularData" / "ai-agent" / "sandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / f"{sid}.json").write_text(json.dumps(
        {"name": "my-sandbox", "permission": {"network": False}}), encoding="utf-8")


@pytest.mark.skipif(GIT is None, reason="git 不可用")
def test_snapshot_diff_events(tmp_path, monkeypatch):
    appdata = _make_appdata(tmp_path)
    sid = "aaaabbbbccccdddd"
    _make_snapshot(appdata, sid)
    _make_sandbox(appdata, sid)
    monkeypatch.setenv("TRAE_CN_HOME", str(tmp_path / "nonexistent-cn"))

    sessions = TraeAdapter().load(appdata)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["source"] == "trae" and s["id"] == sid
    assert s["meta"]["sandbox"]["name"] == "my-sandbox"

    patches = [e for e in s["events"] if e.role == "patch"]
    files = {(e.patch_file, e.turn_key) for e in patches}
    assert ("a.txt", "snap:aaaabbbbccccdddd:turn-1") in files
    assert ("b.txt", "snap:aaaabbbbccccdddd:turn-2") in files
    t1 = [e for e in patches if e.patch_file == "a.txt"][0]
    assert "v2 fixed" in t1.patch_diff
    assert t1.src_id == f"snap:{sid}:after-chat-turn-1:a.txt"
    assert t1.time and t1.time > 0
    # src_id 幂等：两次加载完全一致
    again = TraeAdapter().load(appdata)
    assert [e.src_id for e in again[0]["events"]] == [e.src_id for e in s["events"]]


def test_solo_and_project_memory(tmp_path, monkeypatch):
    appdata = _make_appdata(tmp_path)
    cn = tmp_path / "trae-cn"
    task = cn / "work" / "task1234"
    (task / "sub").mkdir(parents=True)
    (task / "analyze_data.py").write_text("print(1)\n", encoding="utf-8")
    (task / "sub" / "out.csv").write_text("a,b\n", encoding="utf-8")
    proj = cn / "memory" / "projects" / "-d-data-demo"
    proj.mkdir(parents=True)
    (proj / "overview.md").write_text("# 项目结论\n使用 uv 管理。", encoding="utf-8")
    monkeypatch.setenv("TRAE_CN_HOME", str(cn))

    sessions = TraeAdapter().load(appdata)
    by_id = {s["id"]: s for s in sessions}
    assert set(by_id) == {"task1234", "-d-data-demo"}

    solo = by_id["task1234"]
    assert solo["title"].startswith("Trae SOLO 任务")
    assert solo["meta"] == {"kind": "solo_task"}
    assert "analyze_data.py" in solo["events"][0].content
    assert "out.csv" in solo["events"][0].content

    mem = by_id["-d-data-demo"]
    ev = mem["events"][0]
    assert ev.role == "user" and ev.is_system is False
    assert "使用 uv 管理" in ev.content
    assert ev.src_id == "mem:-d-data-demo:overview.md"


def test_git_missing_degrades_to_listing(tmp_path, monkeypatch):
    appdata = _make_appdata(tmp_path)
    sid = "aaaabbbbccccdddd"
    _make_snapshot(appdata, sid)
    monkeypatch.setenv("TRAE_CN_HOME", str(tmp_path / "nonexistent-cn"))
    with mock.patch("agentmemhub.adapters.trae.shutil.which", return_value=None):
        sessions = TraeAdapter().load(appdata)
    assert len(sessions) == 1                      # 会话清单仍在
    assert sessions[0]["events"][0].role == "meta"
    assert "git 不可用" in sessions[0]["events"][0].content


def test_missing_dirs_yield_nothing(tmp_path):
    appdata = _make_appdata(tmp_path)              # 只有空的 ai-agent 目录
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("TRAE_CN_HOME", str(tmp_path / "nonexistent-cn"))
    try:
        assert TraeAdapter().load(appdata) == []
    finally:
        monkeypatch.undo()
