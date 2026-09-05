"""Trae 适配器（字节 Trae / Trae CN AI IDE）。

数据位置（全部只读）：
- ``%APPDATA%/Trae CN/ModularData/ai-agent/snapshot/<sessionId>/v2``
  每会话代码快照（git 管理，``before/after-chat-turn-<n>`` 标签对标记每轮对话
  前后的代码状态 → 可提取每轮 diff 作为 patch 事件）
- ``%APPDATA%/Trae CN/ModularData/ai-agent/sandbox/<sessionId>.json``
  会话沙箱配置（name/permission）
- ``~/.trae-cn/work/<taskId>/``
  SOLO 任务工作区（任务产物文件）
- ``~/.trae-cn/memory/projects/<project>/*.md``
  Trae 自带项目记忆（其自动总结的项目知识，Markdown）

⚠️ 已知限制（与 WorkBuddy 相同的"最小可用"策略）：AI 对话正文存于加密库
``ModularData/ai-agent/database.db``（SQLCipher 类私有格式，文件头随机字节），
当前**无法读取用户/助手的输入输出文本**——只能拿到：会话清单（快照/沙箱/SOLO
任务）+ 每轮代码变更 diff + 项目记忆。等 Trae 官方提供开放接口后再补全完整数据。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from agentmemhub.models import Event, renumber
from .base import AgentAdapter

#: 每轮 diff 最多提取的文件数（防超大变更淹没事件流）
MAX_FILES_PER_TURN = 20
#: 项目记忆单文件入库的最大字符数
MAX_MEM_CHARS = 8000
#: SOLO 任务产物清单最多列出的文件数
MAX_SOLO_FILES = 50


def _git(args: list[str], cwd: Path, git_exe: str) -> Optional[str]:
    """运行只读 git 命令，失败返回 None。"""
    try:
        r = subprocess.run([git_exe, *args], cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            return None
        return r.stdout
    except Exception:
        return None


class TraeAdapter(AgentAdapter):
    source = "trae"
    label = "Trae"

    def candidate_paths(self) -> list[Path]:
        paths: list[Path] = []
        env = os.environ.get("TRAE_APPDATA", "").strip()
        if env:
            paths.append(Path(env))
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            paths.append(Path(appdata) / "Trae CN")   # 国内版
            paths.append(Path(appdata) / "Trae")      # 国际版
        return paths

    def load(self, path: Path) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        ai_dir = path / "ModularData" / "ai-agent"
        if ai_dir.is_dir():
            sessions.extend(self._load_snapshots(ai_dir))
        # 次根：~/.trae-cn（SOLO 工作区 + 项目记忆）
        cn_home = os.environ.get("TRAE_CN_HOME", "").strip()
        cn_root = Path(cn_home) if cn_home else Path.home() / ".trae-cn"
        if cn_root.is_dir():
            sessions.extend(self._load_solo(cn_root / "work"))
            sessions.extend(self._load_memory(cn_root / "memory" / "projects"))
        for s in sessions:
            if s["events"]:
                s["events"] = renumber(s["events"])
        return sessions

    # ------------------------------------------------------------------ 快照
    def _load_snapshots(self, ai_dir: Path) -> list[dict[str, Any]]:
        """每会话：代码快照 diff（patch 事件）+ 沙箱配置（meta 事件）。"""
        snap_root = ai_dir / "snapshot"
        if not snap_root.is_dir():
            return []
        # 沙箱配置索引：sessionId → json 内容
        sandbox: dict[str, dict] = {}
        sb_dir = ai_dir / "sandbox"
        if sb_dir.is_dir():
            for f in sb_dir.glob("*.json"):
                if f.name.endswith("-hooks.json"):
                    continue
                try:
                    sid = f.stem
                    sandbox[sid] = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    continue
        git_exe = shutil.which("git")
        sessions: list[dict[str, Any]] = []
        for sid_dir in sorted(snap_root.iterdir()):
            if not sid_dir.is_dir():
                continue
            sid = sid_dir.name
            v2 = sid_dir / "v2"
            events: list[Event] = []
            if (v2 / ".git").is_dir() and git_exe:
                events.extend(self._snapshot_turn_events(v2, sid, git_exe))
            elif (v2 / ".git").is_dir():
                # git 不可用：降级为纯会话清单（快照仓库存在但 diff 提不了）
                events.append(Event(role="meta",
                                    content="[snapshot] 代码快照存在（git 不可用，未提取 diff）",
                                    src_id=f"snap:{sid}:nogit"))
            mtime = max((p.stat().st_mtime for p in sid_dir.rglob("*") if p.is_file()),
                        default=0)
            sessions.append({
                "source": self.source, "id": sid,
                "title": f"Trae AI 会话 {sid[:8]}",
                "cwd": "", "created_at": 0, "updated_at": mtime,
                "model": "",
                "meta": {"kind": "snapshot", "sandbox": sandbox.get(sid)},
                "events": events,
            })
        return sessions

    def _snapshot_turn_events(self, v2: Path, sid: str, git_exe: str) -> list[Event]:
        """从 before/after-chat-turn-<n> 标签对提取每轮代码变更。"""
        out = _git(["tag", "--list", "before-chat-turn-*"], v2, git_exe)
        if not out:
            return []
        events: list[Event] = []
        for before in sorted(t for t in out.splitlines() if t.strip()):
            turn = before.split("before-chat-turn-", 1)[-1]
            after = f"after-chat-turn-{turn}"
            ts_raw = _git(["log", "-1", "--format=%at", after], v2, git_exe)
            try:
                ts = float(ts_raw.strip()) if ts_raw and ts_raw.strip() else None
            except ValueError:
                ts = None
            names = _git(["diff", "--name-status", before, after], v2, git_exe) or ""
            turn_key = f"snap:{sid}:turn-{turn}"
            for line in names.splitlines()[:MAX_FILES_PER_TURN]:
                parts = line.strip().split("\t", 1)
                if len(parts) != 2:
                    continue
                status, rel = parts
                patch = _git(["diff", "--unified=2", before, after, "--", rel],
                             v2, git_exe) or ""
                events.append(Event(
                    role="patch", time=ts, patch_file=rel,
                    patch_diff=patch[:20000] or None,
                    src_id=f"snap:{sid}:{after}:{rel}",
                    turn_key=turn_key,
                ))
        return events

    # ------------------------------------------------------------------ SOLO
    def _load_solo(self, work_dir: Path) -> list[dict[str, Any]]:
        """SOLO 任务工作区：只列产物清单（meta 事件），正文不可得。"""
        if not work_dir.is_dir():
            return []
        sessions: list[dict[str, Any]] = []
        for task in sorted(work_dir.iterdir()):
            if not task.is_dir():
                continue
            files = sorted(str(p.relative_to(task)) for p in task.rglob("*") if p.is_file())
            events: list[Event] = []
            if files:
                listing = "\n".join(files[:MAX_SOLO_FILES])
                if len(files) > MAX_SOLO_FILES:
                    listing += f"\n…（共 {len(files)} 个文件）"
                events.append(Event(role="meta", content=f"[SOLO 产物清单]\n{listing}",
                                    src_id=f"solo:{task.name}:index",
                                    time=task.stat().st_mtime))
            sessions.append({
                "source": self.source, "id": task.name,
                "title": f"Trae SOLO 任务 {task.name[:8]}",
                "cwd": "", "created_at": 0, "updated_at": task.stat().st_mtime,
                "model": "", "meta": {"kind": "solo_task"}, "events": events,
            })
        return sessions

    # -------------------------------------------------------------- 项目记忆
    def _load_memory(self, mem_dir: Path) -> list[dict[str, Any]]:
        """Trae 项目记忆：每条 Markdown 一条 user 事件（可入 MemOS 被检索）。"""
        if not mem_dir.is_dir():
            return []
        sessions: list[dict[str, Any]] = []
        for proj in sorted(mem_dir.iterdir()):
            if not proj.is_dir():
                continue
            events: list[Event] = []
            for f in sorted(proj.rglob("*.md"))[:20]:
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if not text.strip():
                    continue
                events.append(Event(
                    role="user",
                    content=f"[Trae 项目记忆] {text[:MAX_MEM_CHARS]}",
                    time=f.stat().st_mtime,
                    src_id=f"mem:{proj.name}:{f.relative_to(proj)}",
                    is_system=False,
                ))
            if not events:
                continue
            sessions.append({
                "source": self.source, "id": proj.name,
                "title": f"Trae 项目记忆 {proj.name[:40]}",
                "cwd": "", "created_at": 0,
                "updated_at": max(e.time or 0 for e in events),
                "model": "", "meta": {"kind": "project_memory"}, "events": events,
            })
        return sessions
