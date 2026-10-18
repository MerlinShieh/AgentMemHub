# -*- coding: utf-8 -*-
"""wiki 长任务接口的**任务函数签名守护**。

背景（2026-10-18 实测事故）
=========================
`tasks.submit(name, fn)` 的契约是 **`fn(emit, meta) -> str`**（见 `web/tasks.py`），
而 `_logged_task()` 内部还会再调 `fn(emit_full, meta)` —— 所以传进去的函数
**必须接受两个参数**。

但 `/api/wiki/update` 与 `/api/wiki/batch/run` 当时写的是**零参 lambda**：

    tasks.submit(name, _logged_task(name, lambda: wiki.update(...)))

后果：真机点「增量更新」立刻报
`lambda() takes 0 positional arguments but 2 were given`。
而这两条路径此前**前端从没接**，所以既没人点过、也没有测试覆盖 —— 直到
「wiki 运维」页把它们接上才暴露。

本文件的做法
===========
把 `tasks.submit` 换成**立即执行**的替身（真按 `(emit, meta)` 调用 fn），
同时把底层动作（`wiki.update` / `batch_ops.run`）打桩 ——
这样"签名不对"会当场抛 TypeError，而不会真跑几分钟的编译。
"""
import pytest
from starlette.testclient import TestClient

from agentmemhub.web import tasks as tasks_mod
from agentmemhub.web.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """立即执行的任务替身：契约不对就当场炸。"""
    calls: list = []

    def _submit(name, fn):
        calls.append(name)
        fn(lambda _line: None, {"id": "job1", "name": name})
        return {"id": "job1", "name": name, "status": "done"}

    monkeypatch.setattr(tasks_mod, "submit", _submit)
    return TestClient(create_app(tmp_path / "admin.db")), calls


def test_增量更新接口_任务函数必须接受emit_meta(client, monkeypatch):
    from agentmemhub import wiki

    seen: dict = {}

    def _fake_update(**kw):
        seen.update(kw)
        return {"updated": 0}

    monkeypatch.setattr(wiki, "update", _fake_update)
    c, calls = client
    r = c.post("/api/wiki/update?l1=A&l2=B")
    assert r.status_code == 200, r.text
    assert any("增量更新" in n for n in calls), calls
    assert seen.get("l1_dir") == "A" and seen.get("l2_dir") == "B", seen


def test_离线批次推进接口_任务函数必须接受emit_meta(client, monkeypatch):
    from agentmemhub import batch_ops

    monkeypatch.setattr(batch_ops, "precheck", lambda: None)
    monkeypatch.setattr(batch_ops, "run", lambda **kw: {"action": "submitted"})
    c, calls = client
    r = c.post("/api/wiki/batch/run")
    assert r.status_code == 200, r.text
    assert any("离线批处理" in n for n in calls), calls


def test_任务函数签名不对时_接口会把它暴露出来(client, monkeypatch):
    """反向守护：如果哪天又写成零参 lambda，这条会红（而不是等到真机点）。"""
    from agentmemhub import wiki

    monkeypatch.setattr(wiki, "update", lambda **kw: {"updated": 0})

    # 把替身换成"会检查签名"的那种：fn 不接受两参就直接抛（TestClient 会变成 500）
    from agentmemhub.web import tasks as t

    def _strict_submit(name, fn):
        fn(lambda _l: None, {"id": "j", "name": name})
        return {"id": "j", "name": name, "status": "done"}

    monkeypatch.setattr(t, "submit", _strict_submit)
    c, _ = client
    r = c.post("/api/wiki/update?l1=A&l2=B")
    # 签名正确 ⇒ 200；写错的话这里会是 500（TestClient 默认不抛，看状态码）
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 任务必须有可见输出（2026-10-18 用户反馈："一直 loading，看不到运行日志"）
# ---------------------------------------------------------------------------

def test_增量更新_任务一开始就emit可见行(tmp_path, monkeypatch):
    """任务不能"提交完就静默" —— 至少要有"开始"与最终结果两行。

    背景：`wiki.update(on_progress=…)` 这个参数**收了却从没调用**，下层
    （`wiki_compile` / `wiki_aggregate`）的进度只写 `logs/wiki.log`、没有回调参数。
    所以任务输出一度为 0 行 —— 用户只能看到一个转圈的"任务运行中"。
    """
    from agentmemhub import wiki
    from agentmemhub.web import tasks as t
    from agentmemhub.web.app import create_app

    got: list = []

    def _submit(name, fn):
        buf: list = []
        fn(buf.append, {"id": "j", "name": name})
        got.extend(str(x) for x in buf)
        return {"id": "j", "name": name, "status": "done"}

    monkeypatch.setattr(t, "submit", _submit)
    monkeypatch.setattr(wiki, "update", lambda **kw: {"updated": True})
    c = TestClient(create_app(tmp_path / "admin.db"))
    r = c.post("/api/wiki/update?l1=A&l2=B")
    assert r.status_code == 200, r.text
    assert any("开始增量更新" in x for x in got), got
    assert any('"updated"' in x for x in got), got


def test_tail_wiki_log_把progress事件转成任务输出(tmp_path, monkeypatch):
    """`_tail_wiki_log`：把 `logs/wiki.log` 的新增进度实时 emit 出去。

    这正是"看不到日志"的解法 —— 下层本来就在写权威流水，读它即可，
    不必为展示去改两个核心脚本模块。
    """
    import json as _json
    import threading
    import time

    from agentmemhub import logs as logs_mod
    from agentmemhub.web.app import _tail_wiki_log

    monkeypatch.setattr(logs_mod, "log_dir", lambda: tmp_path)
    p = tmp_path / "wiki.log"
    p.write_text("", encoding="utf-8")

    got: list = []
    stop = threading.Event()
    th = threading.Thread(target=_tail_wiki_log, args=(got.append, stop), daemon=True)
    th.start()
    time.sleep(0.2)                     # 让 tail 先记下初始 offset
    with open(p, "a", encoding="utf-8") as f:
        f.write(_json.dumps({"event": "progress",
                             "message": "   [分批 2/6] 40 条…"},
                            ensure_ascii=False) + "\n")
        f.write(_json.dumps({"event": "run_start", "script": "wiki_compile"},
                            ensure_ascii=False) + "\n")
    for _ in range(40):
        if len(got) >= 2:
            break
        time.sleep(0.1)
    stop.set()
    th.join(timeout=3)
    joined = "\n".join(str(x) for x in got)
    assert "分批 2/6" in joined, got
    assert "[run_start]" in joined, got

