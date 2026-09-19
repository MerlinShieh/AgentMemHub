# -*- coding: utf-8 -*-
"""快照与回滚：索引库在线备份 + wiki 产物目录备份。

为什么必须有
============
wiki 产物与蒸馏表都是 **LLM 产物**：重编要花钱花时间，且 LLM 非确定性意味着
重编结果与原版**不会逐字一致**（域结构、标题都可能漂移）；unit_values 里的
反馈演化值更是完全不可重建。实测事故：测试污染导致 79 个 L1 会话产物丢失，
只能靠重编恢复（¥1.61 + 10 分钟）。有快照就是一次目录拷贝。

快照内容（全部可独立回滚）：
  · 索引库 session_rag.db 整库（sqlite backup API，WAL 安全，含蒸馏表/
    units/unit_values/向量/FTS——一份备份覆盖全部记忆数据）
  · wiki 产物目录（L1 + L2 整目录拷贝，含 manifest/failures/_domains）

存放：`<data_dir>/backups/<时间戳id>/`——data_dir 被测试沙箱覆盖
（AGENTMEM_HUB_DATA_DIR），备份随测试自动隔离，绝不污染真实数据。

回滚语义：restore 前会**先把当前状态做成快照**（防误恢复不可逆），
再整体覆盖。任何时刻都有一级后悔药。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from agentmemhub import config as hub_config

#: 快照保留份数（超出自动删最旧）
KEEP = 5


def backups_dir() -> Path:
    return Path(hub_config.config().data_dir) / "backups"


def _wiki_dirs() -> tuple[Path | None, Path | None]:
    w = hub_config.config().wiki
    l1 = (w.get("out_l1") or "").strip()
    l2 = (w.get("out_l2") or "").strip()
    return (Path(l1) if l1 else None, Path(l2) if l2 else None)


def _index_db() -> Path:
    from agentmemhub.rag.config import load_settings
    return Path(load_settings().index_db)


def create(reason: str = "手动") -> dict[str, Any]:
    """创建一份快照（索引库整库 + wiki 产物目录）。"""
    # id 精确到毫秒——同秒多次 create（如 restore 保护快照）不能互相覆盖
    ts = time.strftime("%Y%m%d_%H%M%S")
    sid = ts
    root = backups_dir() / sid
    n = 0
    while root.exists():                        # 同毫秒冲突时加序号
        n += 1
        sid = f"{ts}_{n}"
        root = backups_dir() / sid
    root.mkdir(parents=True, exist_ok=True)

    # ① 索引库在线备份（backup API 处理 WAL，无需停写）
    src_db = _index_db()
    parts = []
    if src_db.exists():
        dst = root / src_db.name
        src = sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(str(dst))
            try:
                src.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src.close()
        parts.append(src_db.name)

    # ② wiki 产物目录
    for key, d in (("wiki_l1", _wiki_dirs()[0]), ("wiki_l2", _wiki_dirs()[1])):
        if d and d.exists():
            shutil.copytree(d, root / key, dirs_exist_ok=True)
            parts.append(key)

    meta = {
        "id": sid, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason, "parts": parts,
    }
    (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    _prune()
    return {**meta, "path": str(root),
            "size_mb": round(sum(f.stat().st_size for f in root.rglob("*")
                                 if f.is_file()) / 1e6, 1)}


def _prune() -> list[str]:
    """保留最近 KEEP 份，删最旧。返回被删的 id。"""
    root = backups_dir()
    snaps = sorted([d for d in root.iterdir() if d.is_dir()],
                   key=lambda d: d.name)
    removed = []
    for d in snaps[:-KEEP]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


def list_snapshots() -> list[dict[str, Any]]:
    root = backups_dir()
    if not root.exists():
        return []
    out = []
    for d in sorted((x for x in root.iterdir() if x.is_dir()),
                    key=lambda d: d.name, reverse=True):
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        out.append({
            "id": d.name,
            "created_at": meta.get("created_at"),
            "reason": meta.get("reason"),
            "parts": meta.get("parts") or [],
            "path": str(d),
            "size_mb": round(sum(f.stat().st_size for f in d.rglob("*")
                                 if f.is_file()) / 1e6, 1),
        })
    return out


def restore(sid: str, *, wiki_only: bool = False, db_only: bool = False) -> dict[str, Any]:
    """回滚到指定快照。**先给当前状态做一份快照**（防误恢复不可逆），再覆盖。"""
    src = backups_dir() / sid
    if not (src / "meta.json").exists():
        raise ValueError(f"快照不存在：{sid}")
    guard = create(reason=f"restore {sid} 前的当前状态保护快照")

    restored: list[str] = []
    # ① 索引库
    if not wiki_only:
        bak_db = next((src / n for n in ("session_rag.db",) if (src / n).exists()), None)
        live_db = _index_db()
        if bak_db and live_db.exists():
            live_db.with_suffix(".db.wal-bak").unlink(missing_ok=True)
            Path(str(live_db) + "-wal").unlink(missing_ok=True)
            Path(str(live_db) + "-shm").unlink(missing_ok=True)
            shutil.copy2(bak_db, live_db)
            restored.append(live_db.name)
    # ② wiki 产物目录
    if not db_only:
        for key, d in (("wiki_l1", _wiki_dirs()[0]), ("wiki_l2", _wiki_dirs()[1])):
            bak = src / key
            if bak.exists() and d:
                if d.exists():
                    shutil.rmtree(d)
                shutil.copytree(bak, d)
                restored.append(key)
    return {"restored": restored, "snapshot": sid, "guard_snapshot": guard["id"]}
