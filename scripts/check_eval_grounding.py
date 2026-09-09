"""评测集落地校验：每题至少一个期望关键词必须真实存在于索引语料（text/title LIKE）。

防止出「语料里根本没有答案」的废题。用法：
  uv run python scripts/check_eval_grounding.py [--file eval/queries.yaml]
退出码非 0 表示存在无落地依据的用例。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from asrag.eval import load_cases  # noqa: E402


def like_hit(conn: sqlite3.Connection, kw: str) -> int:
    like = "%" + kw.replace("%", "\\%").replace("_", "\\_") + "%"
    return conn.execute(
        "SELECT COUNT(*) FROM units"
        " WHERE text LIKE ? ESCAPE '\\' OR IFNULL(title,'') LIKE ? ESCAPE '\\'",
        (like, like),
    ).fetchone()[0]


def main(argv: list[str] | None = None) -> int:
    file = PROJECT_ROOT / "eval" / "queries.yaml"
    argv = argv or sys.argv[1:]
    if "--file" in argv:
        file = Path(argv[argv.index("--file") + 1])
    cases = load_cases(file)
    conn = sqlite3.connect(
        f"file:{(PROJECT_ROOT / 'database' / 'session_rag.db').as_posix()}?mode=ro",
        uri=True)
    bad = []
    for c in cases:
        hits = {k: like_hit(conn, k) for k in c.expect_any}
        if not any(hits.values()):
            bad.append((c.id, c.expect_any))
        print(f"{c.id:<28} " + " ".join(f"{k}={v}" for k, v in hits.items()))
    conn.close()
    print(f"\n{len(cases)} cases, {len(bad)} ungrounded")
    for cid, kw in bad:
        print(f"  未落地: {cid} expect_any={kw}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
