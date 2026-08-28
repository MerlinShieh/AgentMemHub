# 验证 MemOS 导入结果：overview 统计 / traces 列表 / 语义检索命中
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:18800"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


print("== 1. /api/v1/overview（服务与库状态）==")
try:
    o = get("/api/v1/overview")
    print(json.dumps(o, ensure_ascii=False, indent=1)[:1200])
except Exception as e:
    print("  overview 失败:", e)
    sys.exit(1)

print("\n== 2. /api/v1/traces?limit=5（最近导入的记忆）==")
try:
    t = get("/api/v1/traces?limit=5")
    print(f"  total={t.get('total')}")
    for tr in t.get("traces", [])[:5]:
        print(f"  - [{tr.get('ts')}] userText: {(tr.get('userText') or '')[:60]}")
except Exception as e:
    print("  traces 失败:", e)

print("\n== 3. /api/v1/memory/search（语义检索命中测试）==")
for q in ("部署", "记忆配置"):
    try:
        r = post("/api/v1/memory/search", {"agent": "hermes", "query": q})
        hits = r.get("hits", [])
        print(f"  query='{q}' → 命中 {len(hits)} 条")
        for h in hits[:3]:
            print(f"    · [{h.get('tier')}] {h.get('refKind')}: {(h.get('snippet') or '')[:60]}")
    except Exception as e:
        print(f"  query='{q}' 检索失败:", e)