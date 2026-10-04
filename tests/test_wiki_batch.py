# -*- coding: utf-8 -*-
"""离线批处理编排层（`agentmemhub/wiki_batch.py`）的单元测试。

全程用**假 client**（不发真请求）与**假通知器**（不碰通知库）。
"""
from __future__ import annotations

import json

from agentmemhub.llm_batch import (
    CONSUME,
    FALLBACK,
    PARTIAL,
    STATE_NAME,
    WAIT,
    BatchJob,
    load_state,
    save_state,
)
from agentmemhub.wiki_batch import import_results, poll


class _Client:
    """假批量客户端：按预设返回状态与文件内容，可指定某些调用失败。"""

    def __init__(self, *, batches=None, files=None,
                 fail_retrieve=(), fail_download=()):
        self.batches = batches or {}
        self.files = files or {}
        self.fail_retrieve = set(fail_retrieve)
        self.fail_download = set(fail_download)
        self.calls: list[tuple] = []

    def retrieve(self, batch_id):
        self.calls.append(("retrieve", batch_id))
        if batch_id in self.fail_retrieve:
            raise RuntimeError("网络断了")
        return self.batches.get(batch_id, {})

    def download(self, file_id):
        self.calls.append(("download", file_id))
        if file_id in self.fail_download:
            raise RuntimeError("下载失败")
        return self.files.get(file_id, "")


def _job(**kw) -> BatchJob:
    base = dict(batch_id="batch-1", stage="l2plan", submitted_at=1000.0,
                items={"l2plan|域A-0|0": {"domain": "域A", "batch_no": 0}},
                members={"域A": [{"title": "P1"}]})
    base.update(kw)
    return BatchJob(**base)


def _state(tmp_path, *jobs):
    save_state(tmp_path / STATE_NAME, list(jobs))


def _notifier():
    """假通知器：记下每次通知，返回 True。"""
    seen: list[tuple] = []

    def f(level, category, title, body="", meta=None):
        seen.append((level, category, title, dict(meta or {})))
        return True

    return seen, f


def _ok_line(cid: str, content: str = "{}") -> str:
    return json.dumps({"custom_id": cid,
                       "response": {"status_code": 200,
                                    "body": {"choices": [{"message": {"content": content}}]}},
                       "error": None})


# ---------------------------------------------------------------------------
# 空与等待
# ---------------------------------------------------------------------------

def test_没有在飞任务时什么都不做(tmp_path):
    c = _Client()

    out = import_results(c, tmp_path)

    assert out == {"jobs": [], "decisions": {}, "results": {},
                   "consumed": [], "voided": [], "waited": []}
    assert c.calls == [], "没有在飞任务就不该发任何请求"


def test_还在跑就继续等且不打扰人(tmp_path):
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "in_progress",
                                     "request_counts": {"total": 1, "completed": 0}}})
    seen, note = _notifier()

    out = import_results(c, tmp_path, now=1000.0 + 60, deadline_s=3600,
                         notify_fn=note)

    assert out["decisions"]["batch-1"] == WAIT
    assert out["waited"] == ["batch-1"]
    assert ("download", "file-out") not in c.calls, "还没好就不该下载"
    assert seen == [], "还在等属正常，不该发通知"


def test_查状态失败不抛_保持原状留给下轮(tmp_path):
    _state(tmp_path, _job())
    c = _Client(fail_retrieve={"batch-1"})

    out = import_results(c, tmp_path, now=2000.0)

    assert out["jobs"][0].provider_status == "", "查不到就保持原状"
    assert out["decisions"]["batch-1"] == WAIT, "状态未知时保守地继续等"


# ---------------------------------------------------------------------------
# 消费结果
# ---------------------------------------------------------------------------

def test_完成后消费结果并落回现场(tmp_path):
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "completed",
                                     "output_file_id": "file-out"}},
                files={"file-out": _ok_line(
                    "l2plan|域A-0|0",
                    '{"pages": [{"title": "页1", "members": [1]}]}') + "\n"})

    out = import_results(c, tmp_path, now=2000.0)

    assert out["decisions"]["batch-1"] == CONSUME
    assert out["consumed"] == ["batch-1"]
    got = out["results"]["batch-1"]["l2plan|域A-0|0"]
    assert got["ok"] is True and got["data"]["pages"][0]["title"] == "页1"
    assert load_state(tmp_path / STATE_NAME)[0].results["l2plan|域A-0|0"]["ok"] is True


def test_错误文件也要读_才知道哪条失败(tmp_path):
    """只读成功文件的话，"哪些请求没算出来"就无从知道，只能整体重跑。"""
    _state(tmp_path, _job())
    err = json.dumps({"id": "r1", "custom_id": "l2plan|域A-0|0", "response": None,
                      "error": {"code": "inference_failed", "message": "400"}})
    c = _Client(batches={"batch-1": {"status": "completed",
                                     "error_file_id": "file-err"}},
                files={"file-err": err + "\n"})

    out = import_results(c, tmp_path, now=2000.0)

    got = out["results"]["batch-1"]["l2plan|域A-0|0"]
    assert got["ok"] is False and "inference_failed" in got["error"]


def test_下载失败不抛_结果留待下轮(tmp_path):
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "completed",
                                     "output_file_id": "file-out"}},
                fail_download={"file-out"})

    out = import_results(c, tmp_path, now=2000.0)

    assert out["consumed"] == [], "没取到就不算消费"
    assert out["results"]["batch-1"] == {}


def test_部分到位且超预算_结论是先用已到的(tmp_path):
    """决策要在**取回结果之后重新算** —— 取之前 results 是空的，会被误判成
    "一条都没到"（FALLBACK）。"""
    _state(tmp_path, _job(items={"a": {"domain": "域A", "batch_no": 0},
                                 "b": {"domain": "域A", "batch_no": 1}}))
    c = _Client(batches={"batch-1": {"status": "in_progress",
                                     "output_file_id": "file-out"}},
                files={"file-out": _ok_line("a") + "\n"})
    seen, note = _notifier()

    out = import_results(c, tmp_path, now=1000.0 + 7200, deadline_s=3600,
                         notify_fn=note)

    assert out["decisions"]["batch-1"] == PARTIAL, "取到 1/2 条 → 先用已到的"
    assert out["consumed"] == ["batch-1"]
    assert seen and "只到了一部分" in seen[0][2]
    assert seen[0][3]["pending"] == 1


def test_超时且一条都没到_结论是整批回退(tmp_path):
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "in_progress"}})

    out = import_results(c, tmp_path, now=1000.0 + 7200, deadline_s=3600)

    assert out["decisions"]["batch-1"] == FALLBACK


# ---------------------------------------------------------------------------
# 终局：作废 + 回池 + 通知
# ---------------------------------------------------------------------------

def test_终局即作废回池并通知人(tmp_path):
    """D2 的决策：终局（过期/失败/取消）整批作废，**不捞已完成部分**。"""
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "expired",
                                     "output_file_id": "file-out",
                                     "expires_at": 123.0}})
    seen, note = _notifier()

    out = import_results(c, tmp_path, now=2000.0, notify_fn=note)

    assert out["voided"][0]["status"] == "expired"
    assert out["voided"][0]["domains"] == ["域A"], "作废要报出需要回池的域"
    assert ("download", "file-out") not in c.calls, "终局不捞已完成部分（D2）"
    kept = load_state(tmp_path / STATE_NAME)
    assert len(kept) == 1 and kept[0].voided is True, \
        "作废后要**保留现场**（members/items 是重提与回读落盘的唯一依据），" \
        "只是不再算在飞（2026-09-30 实测：删掉后 12 条请求的成员快照一起丢了）"
    assert seen and seen[0][0] == "warning" and "expired" in seen[0][2]


def test_poll刷新厂商侧的各个字段(tmp_path):
    job = _job()
    c = _Client(batches={"batch-1": {"status": "Completed", "output_file_id": "f1",
                                     "error_file_id": "f2", "expires_at": 55.0,
                                     "request_counts": {"total": 1, "completed": 1}}})

    info = poll(c, [job])

    assert job.provider_status == "completed", "厂商状态一律 lower()"
    assert job.output_file_id == "f1" and job.error_file_id == "f2"
    assert job.expire_at == 55.0
    assert info["batch-1"]["counts"]["completed"] == 1


# ---------------------------------------------------------------------------
# 重提：平台侧 validation_failed 时，用原输入文件重建（不重跑 L1/归域）
# ---------------------------------------------------------------------------

def test_重提_只重建已终局的任务并复用输入文件(tmp_path):
    """2026-09-30 实测：平台"排队执行过程中遇到异常"会让任务 failed（连官方示例
    也如此），官方建议"重新创建任务"。这里复用原输入文件，避免重跑 L1 编译与归域。"""
    from agentmemhub.wiki_batch import resubmit_terminal

    failed = _job(batch_id="batch-old", stage="l2plan",
                  provider_status="failed", input_file_id="file-1")
    flying = _job(batch_id="batch-fly", stage="l2plan",
                  provider_status="in_progress", input_file_id="file-2")
    save_state(tmp_path / STATE_NAME, [failed, flying])

    class _C:
        def __init__(self):
            self.created: list = []

        def create(self, fid, *, name="", n_requests=0):
            self.created.append((fid, name, n_requests))
            return {"id": "batch-new", "status": "Created", "expires_at": 99.0}

    c = _C()
    done = resubmit_terminal(c, tmp_path)

    assert len(done) == 1 and done[0]["old"] == "batch-old"
    assert done[0]["new"] == "batch-new" and done[0]["n_requests"] == 1
    assert c.created == [("file-1", "agentmemhub-l2plan", 1)], \
        "只重建终局的任务，且复用原输入文件"
    jobs = {j.batch_id: j for j in load_state(tmp_path / STATE_NAME)}
    assert "batch-old" not in jobs, "旧 id 应被新 id 取代"
    assert jobs["batch-new"].provider_status == "created"
    assert jobs["batch-new"].results == {}, "新任务的结果要从头收"
    assert jobs["batch-fly"].provider_status == "in_progress", "在飞的任务不许动"


def test_重提_没有终局任务时什么都不做(tmp_path):
    from agentmemhub.wiki_batch import resubmit_terminal

    save_state(tmp_path / STATE_NAME, [_job(provider_status="in_progress")])

    class _C:
        def create(self, *a, **k):
            raise AssertionError("不该重建在飞任务")

    assert resubmit_terminal(_C(), tmp_path) == []


def test_重提_没有输入文件凭据的任务会被跳过(tmp_path):
    from agentmemhub.wiki_batch import resubmit_terminal

    save_state(tmp_path / STATE_NAME,
               [_job(batch_id="b1", provider_status="failed", input_file_id="")])

    class _C:
        def create(self, *a, **k):
            raise AssertionError("没有输入文件就没法重建")

    assert resubmit_terminal(_C(), tmp_path) == []


# ---------------------------------------------------------------------------
# 即将过期预警
# ---------------------------------------------------------------------------

def _warn_job(**kw):
    """在飞（未终局）且带过期时刻的任务。"""
    base = dict(batch_id="b1", stage="l2compile", submitted_at=1000.0,
                items={"a": {"domain": "域A", "batch_no": 0}},
                provider_status="in_progress", expire_at=100000.0)
    base.update(kw)
    return BatchJob(**base)


def test_接近过期时预警一次():
    """到期未完成会**整批作废**、数据回池 —— 提前几小时告知，且**只告知一次**。

    去重靠 `job.warned_expire` 持久化标记而不是通知的去重窗口：收割是高频事件
    （每 10 分钟一次、每次写入记忆也会走到），300 秒窗口挡不住反复推。
    """
    from agentmemhub.wiki_batch import _warn_expiring

    job = _warn_job()
    seen: list = []

    def _note(level, category, title, body="", meta=None):
        seen.append((level, category, title, meta))
        return True

    # 距过期还有 2 小时（< 默认 6 小时阈值）⇒ 预警
    hit = _warn_expiring([job], now=100000.0 - 2 * 3600, note=_note)

    assert hit == ["b1"] and job.warned_expire is True
    assert seen and seen[0][0] == "warning" and seen[0][1] == "batch"
    assert "即将过期" in seen[0][2]
    assert seen[0][3]["hours_left"] == 2.0

    # 再收割一次 ⇒ **不重复推**（这是高频路径，重复推就是刷屏）
    seen.clear()
    assert _warn_expiring([job], now=100000.0 - 3600, note=_note) == []
    assert seen == []


def test_离过期还早或已终局都不预警():
    from agentmemhub.wiki_batch import _warn_expiring

    seen: list = []

    def _note(*a, **k):
        seen.append(a)
        return True

    far = _warn_job(batch_id="far")                       # 还有 20 小时
    assert _warn_expiring([far], now=100000.0 - 20 * 3600, note=_note) == []

    done = _warn_job(batch_id="done", provider_status="completed")
    assert _warn_expiring([done], now=99999.0, note=_note) == []

    finite = _warn_job(batch_id="no-expire", expire_at=0.0)
    assert _warn_expiring([finite], now=99999.0, note=_note) == []

    assert seen == []


def test_预警阈值可配且0关闭(monkeypatch):
    """`wiki.batch.expire_warn_s`：0 = 关闭预警。"""
    from agentmemhub import wiki_batch

    monkeypatch.setattr(wiki_batch, "expire_warn_s", lambda cfg=None: 0.0)
    called: list = []

    assert wiki_batch._warn_expiring(
        [_warn_job()], now=100000.0 - 60, note=lambda *a, **k: called.append(a)) == []
    assert called == [], "阈值 0 时不该预警"


# ---------------------------------------------------------------------------
# 坏 JSON 的 repair_model 兜底
# ---------------------------------------------------------------------------

class _RepairClient:
    """只有 `_repair_json` 的假 client（模拟实时 LLM client 的修复能力）。"""

    def __init__(self, out=None, raises=None):
        self.out = out if out is not None else {"pages": [{"title": "修好了"}]}
        self.raises = raises
        self.calls: list = []

    def _repair_json(self, system, user, raw, max_tokens=None):
        self.calls.append(raw)
        if self.raises:
            raise self.raises
        return self.out


def test_坏JSON走修复模型():
    """**实时路一直有这一步，离线路此前缺** —— 同一条输出在两条路上质量不同。

    `load_batch_results()` 早就为它留了路（保留原文 `content`，注释写着"坏 JSON 有
    两条出路：重试 / 交给修复模型"），这里把缺的那一步接上。
    """
    from agentmemhub.wiki_batch import repair_broken_results

    results = {
        "ok1": {"ok": True, "data": {"a": 1}},                 # 成功的不该被碰
        "bad1": {"ok": False, "content": "```json\n{坏的", "error": "不是 JSON"},
        "nobody": {"ok": False, "error": "没有正文"},            # 没正文没法修
    }
    c = _RepairClient()

    stats = repair_broken_results(results, repair_client=c)

    assert stats == {"tried": 1, "repaired": 1}
    assert c.calls == ["```json\n{坏的"], "只把坏 JSON 的**原文**交给修复器"
    assert results["bad1"]["ok"] is True
    assert results["bad1"]["data"] == {"pages": [{"title": "修好了"}]}
    assert results["bad1"]["repaired"] is True
    assert "error" not in results["bad1"], "修好了就该清掉 error"
    assert results["ok1"] == {"ok": True, "data": {"a": 1}}, "成功的不动"
    assert results["nobody"]["ok"] is False, "没正文的没法修"


def test_修复失败保持原样():
    """修复是**尽力而为**：失败就保持"坏 JSON"的原状（原文还在，下轮还能再修/重试）。"""
    from agentmemhub.wiki_batch import repair_broken_results

    results = {"bad": {"ok": False, "content": "坏的", "error": "原错误"}}
    c = _RepairClient(raises=RuntimeError("模型不可用"))

    stats = repair_broken_results(results, repair_client=c)

    assert stats == {"tried": 1, "repaired": 0}
    assert results["bad"]["ok"] is False and results["bad"]["error"] == "原错误"


def test_修复有单轮上限():
    """修复走**实时**调用（要花钱）—— 坏 500 条不能悄无声息打 500 次。"""
    from agentmemhub.wiki_batch import repair_broken_results

    results = {"b%d" % i: {"ok": False, "content": "x"} for i in range(10)}
    c = _RepairClient()

    stats = repair_broken_results(results, repair_client=c, max_repair=3)

    assert stats == {"tried": 3, "repaired": 3}
    assert len(c.calls) == 3
    left = [k for k, v in results.items() if not v.get("ok")]
    assert len(left) == 7, "超出的留到下轮（它们的 content 还在，不会丢）"


def test_没配修复模型时行为不变(monkeypatch):
    """与 `LLMClient` 的既有语义一致：**没配 `repair_model` 就什么都不做**。"""
    from agentmemhub import wiki_batch

    class _NoRepair:
        pass                                     # 没有 _repair_json

    monkeypatch.setattr(wiki_batch, "_repair_client", lambda: _NoRepair())
    results = {"bad": {"ok": False, "content": "坏的"}}

    assert wiki_batch.repair_broken_results(results) == {"tried": 0, "repaired": 0}
    assert results["bad"]["ok"] is False, "原样返回"


def test_上限0时不修(monkeypatch):
    from agentmemhub import wiki_batch

    called: list = []
    monkeypatch.setattr(wiki_batch, "_repair_client",
                        lambda: called.append(1) or _RepairClient())
    results = {"bad": {"ok": False, "content": "坏的"}}

    assert wiki_batch.repair_broken_results(results, max_repair=0) == {
        "tried": 0, "repaired": 0}
    assert called == [], "上限 0 时连 client 都不必构造"


def test_重提后取新窗口并重置预警(tmp_path):
    """重提出的新任务要带**新窗口**、并且能**重新预警**。

    2026-10-04 体检发现漏了两处：
      · 不更新 `expire_at` → 新任务带着**旧窗口**（可能早已过期）→ 预警要么立刻误报、
        要么永不触发；
      · 不重置 `warned_expire` → 新任务**再也不会预警**（那个标记是为旧任务打的）。
    """
    from agentmemhub.wiki_batch import resubmit_terminal

    job = _job(batch_id="old", provider_status="failed", input_file_id="f1")
    job.expire_at = 1000.0
    job.warned_expire = True
    save_state(tmp_path / STATE_NAME, [job])

    class _C:
        def create(self, fid, *, name="", n_requests=0):
            assert fid == "f1", "要用**原输入文件**重建（不重跑 L1/归域）"
            return {"id": "new", "status": "created", "expires_at": 9999.0}

    resubmit_terminal(_C(), tmp_path)

    after = load_state(tmp_path / STATE_NAME)[0]
    assert after.batch_id == "new"
    assert after.expire_at == 9999.0, "新任务必须用**新窗口**"
    assert after.warned_expire is False, "新任务必须能**重新预警**"
    assert after.results == {} and after.provider_status == "created"
