"""记忆库一致性巡检（命令行入口）。

用法：
    .venv\\Scripts\\python.exe scripts/health_check.py
    .venv\\Scripts\\python.exe scripts/health_check.py --json

判据与面板「记忆报表 → 记忆库健康」卡片**完全一致** —— 两者共用
`agentmemhub/health.py` 的 `collect()` / `diagnose()`，避免同一套 SQL
在两处各写一遍后悄悄漂移。

退出码：0 = 全部正常，1 = 存在问题（便于挂进 CI / 定时任务）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentmemhub.health import collect              # noqa: E402
from agentmemhub.rag.config import load_settings    # noqa: E402
from agentmemhub.rag.ingest import open_index       # noqa: E402


def render(m: dict, index_db) -> str:
    out = ["=== AgentMemHub 记忆库健康检查 ===", f"索引库: {index_db}", ""]

    out.append("[1] units.updated_at 覆盖率")
    out.append(f"    units 总数        {m['units']}")
    out.append(f"    updated_at NULL   {m['updated_at_null']}"
               f"   {'OK' if not m['updated_at_null'] else '问题'}")

    out.append("")
    out.append("[2] 蒸馏投影一致性")
    same = m["projections"] == m["active_with_projection"]
    out.append(f"    蒸馏投影          {m['projections']}")
    out.append(f"    有投影的活跃记忆  {m['active_with_projection']}"
               f"   {'OK' if same else '不一致'}")
    out.append(f"    滞留投影          {m['stale_projections']}"
               f"   {'OK' if not m['stale_projections'] else '问题'}")

    out.append("")
    out.append("[3] 索引一致性")
    out.append(f"    units / units_fts {m['units']} / {m['fts_rows']}"
               f"   {'OK' if m['fts_rows'] == m['units'] else '不同步'}")
    for t, v in m["vec_tables"].items():
        out.append(f"    {t:<24} {v['rows']} 行, 孤儿 {v['orphans']}"
                   f"   {'OK' if not v['orphans'] else '需 GC'}")

    out.append("")
    out.append("[4] 最近 5 条写入")
    for r in m["recent"]:
        out.append(f"    id={r['id']:<7} {r['src_id'] or '-':<24}"
                   f" time={r['time']} updated_at={r['updated_at']}")
    out.append("      （time 是事件时间、updated_at 是写入/改写时刻，本就可能不同）")

    out.append("")
    if m["healthy"]:
        out.append("结论: 全部正常")
    else:
        out.append("结论: 存在问题")
        out.extend(f"  · {t}" for t in m["issues"])
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆库一致性巡检")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供脚本消费）")
    args = ap.parse_args()

    settings = load_settings()
    conn = open_index(settings.index_db)
    try:
        m = collect(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(m, ensure_ascii=False, indent=2))
        return 0
    print(render(m, settings.index_db))
    return 0 if m["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
