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
