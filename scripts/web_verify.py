"""Web 前后端集成验证：模拟前端的真实调用序列对运行中的服务做断言。"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8086"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {detail}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, r.read()


print("== 1. 页面与静态资源 ==")
st, html = get_raw("/")
check("GET / index.html", st == 200 and "AgentMemHub".lower() in html.decode("utf-8", "ignore").lower() or b"Agent" in html)
for f in ("vendor/tailwind.browser.js", "vendor/lucide.min.js", "vendor/chart.umd.js"):
    st, _ = get_raw("/" + f)
    check(f"GET /{f}", st == 200)

print("== 2. bootstrap 形状（前端启动数据）==")
st, d = get("/api/bootstrap")
check("meta.totalConversations > 0", d["meta"]["totalConversations"] > 0)
check("conversations 全量 = meta.total",
      len(d["conversations"]) == d["meta"]["totalConversations"])
c0 = d["conversations"][0]
need = {"idx", "source", "id", "title", "cwd", "workspace", "model", "createdAt",
        "updatedAt", "eventCount", "roles"}
check("conversation 字段 camelCase 契约", need.issubset(c0.keys()), str(c0.keys()))
check("sourceColors 是 map", isinstance(d["sourceColors"], dict) and len(d["sourceColors"]) >= 5)
check("roleColors 是 map", "user" in d["roleColors"])
check("eventsByConv 为空(事件按需)", d["eventsByConv"] == {})

print("== 3. 筛选查询链路 ==")
ws = d["stats"]["cwds"]
if ws:
    w = ws[0]
    st, flt = get(f"/api/conversations?workspace={urllib.parse.quote(w)}&all=1")
    check(f"按 workspace 筛选({w}) 有结果", flt["total"] >= 1)
st, flt = get("/api/conversations?days=30&all=1")
check("近30天筛选", st == 200)
src_all = ",".join(s["source"] for s in d["stats"]["sources"])
st, flt = get(f"/api/conversations?sources={urllib.parse.quote(src_all)}&all=1")
check("多 sources 筛选=全部", flt["total"] == d["meta"]["totalConversations"])

print("== 4. 单会话事件流（抽屉按需加载）==")
cid0 = c0["id"]
st, ev = get(f"/api/conversations/{c0['source']}/{urllib.parse.quote(cid0)}/events")
check("events total>0", ev["total"] > 0)
first = next(e for e in ev["events"] if not e.get("gap"))
check("事件短键契约(r/c 或 tn...)",
      any(k in first for k in ("r",)), str(first)[:80])
big = max(d["conversations"], key=lambda x: x["eventCount"])
st, evb = get(f"/api/conversations/{big['source']}/{urllib.parse.quote(big['id'])}/events?limit=100")
if big["eventCount"] > 100:
    check("大会话 capped 截断生效", evb["capped"] is True and len(evb["events"]) <= 101,
          f"capped={evb['capped']}, returned={len(evb['events'])}")

print("== 5. 分页一致性 ==")
st, p1 = get("/api/conversations?page=1&page_size=10")
st, p2 = get("/api/conversations?page=2&page_size=10")
ids1 = {i["id"] for i in p1["items"]}
ids2 = {i["id"] for i in p2["items"]}
check("分页无重叠", not (ids1 & ids2))
check("分页数量正确", len(p1["items"]) == 10)

print()
print(f"结果: {ok} 通过, {fail} 失败")
sys.exit(1 if fail else 0)