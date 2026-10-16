# -*- coding: utf-8 -*-
"""投喂通知：分级选得对、批量不刷屏、以及"通知只是旁路"。

`agentmemhub/feed/notify.py` 立了三条纪律，本文件每条都守一遍：

① **复用既有五开关、不新增键** ⇒ 等级必须选对：成功 `info`（默认不推微信）、
   低质量/失败 `warning`、代码 bug `error`。选错就是"要么漏报、要么刷屏"；
② **通知绝不拖累投喂** ⇒ 推送通道炸了，投喂仍必须成功；
③ **批量只推一条汇总** ⇒ 逐条推会刷爆手机（标题各不相同 ⇒ 去重窗口失效）。

**隔离**：全部注入 tmp 配置；`AGENTMEM_HUB_DATA_DIR` 由 conftest 置为 tmp，
所以通知库落在沙箱里；推送出口另有 `notify.pushplus_send()` 的进程级熔断。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentmemhub import notify as real_notify
from agentmemhub.feed import notify as fn
from agentmemhub.feed import service as svc


class _FakeCfg:
    """三段路径全在 tmp（与 `test_feed_text.py` 同款）。"""

    def __init__(self, root: Path):
        self.root = root
        self.paths = {"db": root / "materials.db", "store": root / "store",
                      "kb": root / "kb"}

    def feed_paths(self):
        return dict(self.paths)

    def feed(self):
        return {}

    @property
    def wiki(self):
        return {"out_l2": ""}


@pytest.fixture()
def feed_cfg(tmp_path, monkeypatch):
    cfg = _FakeCfg(tmp_path / "feed")
    monkeypatch.setattr(svc, "_cfg", lambda: cfg)
    return cfg


def _ok_res(**kw) -> dict:
    r = {"ref": "/tmp/笔记.md", "ok": True, "skipped": "", "doc_id": "abc123",
         "kind": "text", "title": "笔记", "quality": 1.0, "warnings": [],
         "kb_path": "abc123.md", "error": "", "error_kind": "", "dry_run": False}
    r.update(kw)
    return r


# ---------------------------------------------------------------------------
# ① 等级选得对（这直接决定"推不推微信"）
# ---------------------------------------------------------------------------

def test_成功且干净是info级():
    """默认 `push_when.info=false` ⇒ 普通成功**不打扰**。"""
    level, title, body = fn.describe(_ok_res())
    assert level == "info"
    assert "投喂成功" in title and "笔记" in title
    assert "质量：1.00" in body


def test_低质量或有告警是warning级():
    """`warning` 默认推微信 —— "需要人工复核"确实值得人看一眼。"""
    assert fn.describe(_ok_res(quality=0.3))[0] == "warning"
    assert fn.describe(_ok_res(quality=0.7, warnings=["表格有损"]))[0] == "warning"


def test_失败是warning_代码bug才是error():
    """**素材/环境的问题**与**代码 bug** 必须分级：前者用户换个文件就行，
    后者要人去看代码 —— 混成一级会让"该修代码"淹没在"文件不支持"里。"""
    assert fn.describe(_ok_res(ok=False, error="文件不存在",
                               error_kind="not_found"))[0] == "warning"
    assert fn.describe(_ok_res(ok=False, error="格式不支持",
                               error_kind="unsupported"))[0] == "warning"
    assert fn.describe(_ok_res(ok=False, error="转译崩了",
                               error_kind="crash"))[0] == "error"
    assert fn.describe(_ok_res(ok=False, error="注册表异常",
                               error_kind="registry"))[0] == "error"


def test_失败通知带上来源与原因分类():
    level, title, body = fn.describe(_ok_res(ok=False, error_kind="empty",
                                             error="转译结果正文为空",
                                             ref="/tmp/空.pdf"))
    assert "投喂失败" in title
    assert "原因分类：empty" in body and "正文为空" in body


def test_dry_run与重复投喂不发通知(monkeypatch):
    """dry-run 承诺"一个字都不写"（其中也包括不打扰人）；
    重复投喂若也通知，用户重投一次就被推一次 —— 那是最烦的那种通知。"""
    sent: list = []
    monkeypatch.setattr(fn, "_send", lambda *a, **k: sent.append(a))
    fn.notify_result(_ok_res(dry_run=True))
    fn.notify_result(_ok_res(skipped="duplicate"))
    assert sent == []


# ---------------------------------------------------------------------------
# ② 通知是旁路，绝不拖累投喂
# ---------------------------------------------------------------------------

def test_通知层抛异常时投喂照常成功(feed_cfg, tmp_path, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("通知炸了")
    monkeypatch.setattr(fn, "_send", _boom)
    p = tmp_path / "旁路笔记.md"
    p.write_text("# 旁路测试\n\n正文内容够长，不会被判空。\n", encoding="utf-8")
    res = svc.ingest(str(p))
    assert res["ok"] is True, res
    assert res["kb_path"]


def test_describe_本身不抛(monkeypatch):
    """坏输入（缺字段、类型不对）也只能让这条通知难看，不能炸穿调用方。"""
    fn.notify_result({})                 # 全空字典
    fn.notify_result({"ok": True, "quality": "不是数字", "warnings": None})


# ---------------------------------------------------------------------------
# ③ 批量只推一条汇总
# ---------------------------------------------------------------------------

def test_批量汇总通知带计数(monkeypatch):
    calls: list = []
    monkeypatch.setattr(fn, "_send", lambda lv, title, body, meta:
                        calls.append((lv, title, body)))
    fn.notify_batch({"total": 3, "ok": 2, "failed": 1, "skipped": 0,
                     "errors": [{"path": "a.pdf", "error": "坏了"}]})
    assert len(calls) == 1
    level, title, body = calls[0]
    assert level == "warning" and "失败 1" in title and "a.pdf" in body


def test_批量全成功是info级(monkeypatch):
    calls: list = []
    monkeypatch.setattr(fn, "_send", lambda lv, *a: calls.append(lv))
    fn.notify_batch({"total": 2, "ok": 2, "failed": 0, "skipped": 0})
    assert calls == ["info"]


def test_批量_dry_run不发通知(monkeypatch):
    calls: list = []
    monkeypatch.setattr(fn, "_send", lambda *a, **k: calls.append(a))
    fn.notify_batch({"total": 5, "ok": 5, "failed": 0, "skipped": 0,
                     "dryRun": True})
    assert calls == []


# ---------------------------------------------------------------------------
# 端到端：真的落到通知库（category=feed）
# ---------------------------------------------------------------------------

def test_端到端_投喂成功落一条feed类通知(feed_cfg, tmp_path):
    p = tmp_path / "端到端通知.md"
    p.write_text("# 端到端通知\n\n正文内容够长，不会被判空。\n", encoding="utf-8")
    res = svc.ingest(str(p))
    assert res["ok"] is True, res
    rows = real_notify.list_notifications(category="feed", limit=30)
    assert any("投喂成功" in (r.get("title") or "") for r in rows), rows
    assert all(r["category"] == "feed" for r in rows)


def test_端到端_失败投喂落warning级通知(feed_cfg, tmp_path):
    res = svc.ingest(str(tmp_path / "根本不存在.md"))
    assert res["ok"] is False
    rows = real_notify.list_notifications(category="feed", level="warning",
                                          limit=30)
    assert any("投喂失败" in (r.get("title") or "") for r in rows), rows


def test_端到端_批量只留一条汇总(feed_cfg, tmp_path, monkeypatch):
    """本文件最重要的一条：**3 份文件只发 1 条通知**。

    若哪天有人把通知挪进逐条循环里，这条会立刻变红 —— 而那正是"手机被刷爆"
    这个事故的唯一防线（去重窗口挡不住标题各异的逐条通知）。
    """
    sent: list = []
    monkeypatch.setattr(fn, "_send", lambda lv, title, body, meta:
                        sent.append(title))
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(3):
        (root / ("批量笔记%d.md" % i)).write_text(
            "# 批量笔记%d\n\n正文内容够长，不会被判空。\n" % i, encoding="utf-8")
    out = svc.ingest_batch(roots=[str(root)])
    assert out["total"] == 3 and out["ok"] == 3, out
    assert len(sent) == 1, "批量必须只发一条汇总，实际发了 %d 条：%s" % (len(sent), sent)
    assert "批量投喂" in sent[0]
