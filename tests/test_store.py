"""store.py 单元测试：写入 → FTS 搜索 → 读取回环。"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_model import Event, renumber
from store import Store


def build_session(source, cid, events):
    return {
        "source": source,
        "id": cid,
        "title": f"会话{cid}",
        "cwd": "/tmp",
        "model": "test-model",
        "created_at": 1000,
        "updated_at": 1000 + len(events),
        "meta": {"note": "test"},
        "events": events,
    }


def test_write_search_read():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        store = Store(db)

        # 两个会话，含全部事件类型
        ev = [
            Event(role="user", seq=1, time=1001, content="帮我修复登录问题"),
            Event(role="reasoning", seq=2, time=1002, content="登录按钮没反应，可能事件监听器问题"),
            Event(role="tool", seq=3, time=1003, tool_name="Bash",
                  tool_input={"command": "npm test"}, tool_output="FAIL src/login.ts",
                  tool_status="completed"),
            Event(role="assistant", seq=4, time=1004, content="测试失败，是事件监听器问题"),
            Event(role="patch", seq=5, time=1005, patch_file="src/login.ts",
                  patch_diff="@@ -12,3 +12,5 @@"),
        ]
        sessions = [
            build_session("opencode", "s1", renumber(ev)),
            build_session("zcode", "s2", [
                Event(role="user", seq=1, time=2001, content="你好"),
                Event(role="assistant", seq=2, time=2002, content="你好！"),
            ]),
        ]

        n = store.replace_source("opencode", [sessions[0]], signature="sig1")
        assert n == 5, f"expected 5 events, got {n}"
        n = store.replace_source("zcode", [sessions[1]], signature="sig2")
        assert n == 2

        # 读取回环
        evs = store.get_events("opencode", "s1")
        assert len(evs) == 5
        assert evs[2].tool_name == "Bash"
        assert evs[2].tool_input == {"command": "npm test"}
        assert evs[4].role == "patch"
        assert evs[4].patch_file == "src/login.ts"

        # FTS 搜索：命中正文
        hits = store.search("登录", source="opencode")
        assert len(hits) >= 1, f"expected search hits, got {hits}"
        assert hits[0]["conversation_id"] == "s1"

        hits = store.search("事件监听器")
        assert len(hits) >= 1

        # 按 role 过滤
        hits = store.search("事件监听器", role="reasoning")
        assert all(h["role"] == "reasoning" for h in hits)

        # 统计
        stats = store.stats()
        assert stats["conversations"] == 2
        assert stats["events"] == 7
        assert len(stats["sources"]) == 2

        # 替换（增量）
        n = store.replace_source("opencode", [sessions[0]], signature="sig1b")
        assert store.stats()["events"] == 7  # 替换后总数不变
        store.close()
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    test_write_search_read()