"""构造 E2E 测试用的临时数据库（3 个会话，勿用于生产）。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from agentmemhub.models import Event, renumber
from agentmemhub.store import Store

db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_e2e_tmp/e2e.db")
s = Store(db)
sessions = []
for i in range(3):
    events = renumber([
        Event(role="user", content=f"E2E 测试会话 {i} 的用户提问", time=1750000000 + i * 100),
        Event(role="reasoning", content=f"E2E 思考 {i}", time=1750000001 + i * 100),
        Event(role="tool", tool_name="Bash", tool_input={"command": "echo hi"},
              tool_output="hi", tool_status="completed", time=1750000002 + i * 100),
        Event(role="assistant", content=f"E2E 回复 {i}", model="e2e-model", time=1750000003 + i * 100),
    ])
    sessions.append({
        "source": "e2e", "id": f"sess_{i}", "title": f"E2E 会话 {i}",
        "cwd": "D:/e2e/proj", "created_at": 1750000000 + i * 100,
        "updated_at": 1750000003 + i * 100, "model": "", "meta": {}, "events": events,
    })
n = s.replace_source("e2e", sessions) if False else s.replace_source("e2e", sessions)
print(f"E2E 临时库已建: {s.db_path}  会话=3 事件={n}")
s.close()