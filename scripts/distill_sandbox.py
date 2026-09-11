"""蒸馏实验沙箱：把生产数据一致性快照到独立目录，实验全程不碰生产库。

用法：
  uv run python scripts/distill_sandbox.py            # 建沙箱（已存在则拒绝）
  uv run python scripts/distill_sandbox.py --force    # 重建（先删旧沙箱）
  uv run python scripts/distill_sandbox.py --check    # 只校验沙箱完整性

建好后，用它跑任何蒸馏/评测命令（一个环境变量同时切换采集库与索引库）：
  cmd   : set AGENTMEM_HUB_DATA_DIR=<项目根>\\database_distill_test
  pwsh  : $env:AGENTMEM_HUB_DATA_DIR="$PWD\\database_distill_test"
  bash  : export AGENTMEM_HUB_DATA_DIR="<项目根>/database_distill_test"

为什么用 sqlite3 backup API 而不是拷文件：采集库正被采集/面板进程持续写入，
db/-wal/-shm 三件的文件级拷贝在写入期间可能得到不一致快照；backup API 由
SQLite 保证事务一致性（源库一律以只读 URI 打开，不触碰生产数据）。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "database"
DST_DIR = PROJECT_ROOT / "database_distill_test"

#: 需一致性快照的 SQLite 库（走 backup API）
DB_FILES = ("agentmemhub.db", "session_rag.db")
#: 直接拷贝的普通文件（水位 / 评分状态）
PLAIN_FILES = ("watermarks.json", "scored_traces.json")


def _mb(p: Path) -> str:
    return f"{p.stat().st_size / 1048576:.0f} MB"


def _backup(src: Path, dst: Path) -> None:
    """SQLite 在线备份：源只读打开，目标为其一致性快照（含 WAL 已提交内容）。"""
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.commit()
    finally:
        d.close()
        s.close()


def _count(db: Path, table: str) -> str:
    """表行数（表不存在返回 '-'，方便同一套校验覆盖不同库）。"""
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            return str(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return "-"


def verify() -> int:
    """校验沙箱完整性：文件存在、库可打开、关键表可读。"""
    if not DST_DIR.is_dir():
        print(f"✗ 沙箱不存在：{DST_DIR}")
        return 1
    ok = True
    print(f"沙箱：{DST_DIR}")
    for name in DB_FILES:
        p = DST_DIR / name
        if not p.exists():
            print(f"  ✗ 缺文件：{name}")
            ok = False
            continue
        detail = ""
        if name == "agentmemhub.db":
            detail = f"，会话 {_count(p, 'conversations')}，事件 {_count(p, 'events')}"
        else:
            detail = f"，units {_count(p, 'units')}"
        print(f"  ✓ {name}：{_mb(p)}{detail}")
    for name in PLAIN_FILES:
        p = DST_DIR / name
        print(f"  {'✓' if p.exists() else '·'} {name}"
              + (f"：{_mb(p)}" if p.exists() else "（源中不存在，跳过）"))
    if not ok:
        return 1
    print()
    print("沙箱就绪。跑蒸馏/评测前设置环境变量指向它：")
    print(f"  cmd  : set AGENTMEM_HUB_DATA_DIR={DST_DIR}")
    print(f"  pwsh : $env:AGENTMEM_HUB_DATA_DIR=\"{DST_DIR}\"")
    print(f"  bash : export AGENTMEM_HUB_DATA_DIR=\"{DST_DIR.as_posix()}\"")
    return 0


def build(force: bool) -> int:
    if not SRC_DIR.is_dir():
        print(f"✗ 源数据目录不存在：{SRC_DIR}")
        return 1
    if DST_DIR.exists():
        if not force:
            print(f"✗ 沙箱已存在：{DST_DIR}")
            print("  重建请加 --force（只删沙箱，生产库不受影响）；"
                  "仅校验用 --check")
            return 1
        shutil.rmtree(DST_DIR)
        print(f"  已删除旧沙箱（生产库未动）：{DST_DIR}")
    DST_DIR.mkdir(parents=True)

    for name in DB_FILES:
        src = SRC_DIR / name
        if not src.exists():
            print(f"  · 跳过（源中不存在）：{name}")
            continue
        print(f"  · 快照 {name}（{_mb(src)}）…", flush=True)
        _backup(src, DST_DIR / name)
    for name in PLAIN_FILES:
        src = SRC_DIR / name
        if not src.exists():
            print(f"  · 跳过（源中不存在）：{name}")
            continue
        shutil.copy2(src, DST_DIR / name)
        print(f"  · 拷贝 {name}")
    print()
    return verify()


def main() -> int:
    ap = argparse.ArgumentParser(description="蒸馏实验沙箱（生产数据一致性快照）")
    ap.add_argument("--force", action="store_true", help="重建沙箱（删除旧沙箱）")
    ap.add_argument("--check", action="store_true", help="只校验沙箱完整性")
    args = ap.parse_args()
    return verify() if args.check else build(args.force)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
