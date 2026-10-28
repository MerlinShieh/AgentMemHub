# -*- coding: utf-8 -*-
"""离线批处理编解码 + 运行器协议的测试（都不需要厂商参与）。

覆盖的是「接通云平台之前就能做完、也必须做对」的那一半：
请求怎么编成 JSONL、结果怎么读回来、单条失败怎么隔离、未返回怎么不吞。
"""
from __future__ import annotations

import json

import pytest

from agentmemhub.llm_batch import (
    CONSUME,
    DEFAULT_URL,
    FALLBACK,
    PARTIAL,
    STATE_NAME,
    WAIT,
    BatchJob,
    BatchRequest,
    PendingError,
    SyncRunner,
    batch_to_jsonl,
    decide,
    load_batch_results,
    load_state,
    save_state,
)


# ---------------------------------------------------------------------------
# 请求编成 JSONL
# ---------------------------------------------------------------------------

def test_请求编成JSONL():
    reqs = [BatchRequest(custom_id="l2plan|域A|0", system="S", user="U",
                         max_tokens=123, temperature=0.2, tag="细分[1/2] "),
            BatchRequest(custom_id="l2plan|域A|1", system="S2", user="U2")]

    text = batch_to_jsonl(reqs, "mimo-v2.6-flash")

    assert text.endswith("\n"), "上传接口对末尾换行的要求不一，带换行更安全"
    lines = text.strip().splitlines()
    assert len(lines) == 2
    line = json.loads(lines[0])
    assert line["custom_id"] == "l2plan|域A|0"
    assert line["method"] == "POST"
    assert line["url"] == DEFAULT_URL
    assert line["body"]["model"] == "mimo-v2.6-flash"
    assert line["body"]["messages"] == [
        {"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert line["body"]["max_tokens"] == 123
    # tag 只用于日志，不该进请求体
    assert "tag" not in line["body"]


def test_空请求列表编成空串():
    assert batch_to_jsonl([], "m") == ""


def test_custom_id可独立定位():
    """上传与回读可能隔几小时、甚至换机器 —— 凭据必须自带定位信息。"""
    reqs = [BatchRequest(custom_id="l2plan|DSH-Hermes|3", system="s", user="u")]
    line = json.loads(batch_to_jsonl(reqs, "m").strip())
    stage, domain, batch_no = line["custom_id"].split("|")
    assert (stage, domain, batch_no) == ("l2plan", "DSH-Hermes", "3")


# ---------------------------------------------------------------------------
# 结果读回来
# ---------------------------------------------------------------------------

def _ok_line(cid: str, content: str) -> str:
    return json.dumps({"custom_id": cid, "response": {
        "status_code": 200,
        "body": {"choices": [{"message": {"content": content}}]}}})


def test_回读结果_成功():
    text = _ok_line("a", '{"pages": [{"title": "T", "members": [1]}]}')

    r = load_batch_results(text)

    assert r["a"]["ok"] is True
    assert r["a"]["data"] == {"pages": [{"title": "T", "members": [1]}]}


def test_回读结果_坏行不拖垮整批():
    """一批几千条里坏一行，不该让整轮回读白跑（与 distill 的批量读取同口径）。"""
    text = "\n".join([
        _ok_line("a", '{"pages": []}'),
        "{这不是 JSON",
        _ok_line("b", '{"pages": [{"title": "T"}]}'),
    ])

    r = load_batch_results(text)

    assert r["a"]["ok"] and r["b"]["ok"]
    assert any(not v["ok"] for k, v in r.items() if k == ""), "坏行应被单独记为失败"


def test_回读结果_错误与异常形状都记为失败():
    text = "\n".join([
        json.dumps({"custom_id": "e1", "error": {"message": "quota"}}),
        json.dumps({"custom_id": "e2", "response": {"status_code": 500,
                                                    "body": {}}}),
        json.dumps({"custom_id": "e3", "response": {
            "status_code": 200, "body": {"choices": []}}}),
        _ok_line("e4", "这不是 JSON"),
    ])

    r = load_batch_results(text)

    assert all(not r[k]["ok"] for k in ("e1", "e2", "e3", "e4"))
    assert "quota" in r["e1"]["error"]


def test_回读结果_无custom_id的行被忽略():
    text = json.dumps({"response": {"status_code": 200, "body": {}}})
    assert load_batch_results(text) == {}


# ---------------------------------------------------------------------------
# 运行器：单条失败隔离 + 未返回不吞
# ---------------------------------------------------------------------------

def test_同步运行器_单条失败不影响其它():
    """**这是丢掉「整域退化为一个页面」的关键**：一条失败只让那一条记空结果。"""
    def call(system, user, max_tokens, tag):
        if user == "bad":
            raise RuntimeError("boom")
        return {"pages": [{"title": "T", "members": [1]}]}

    seen = []
    runner = SyncRunner(call, on_error=lambda req, e: seen.append(req.custom_id))

    out = runner.run([BatchRequest("a", "s", "good"),
                      BatchRequest("b", "s", "bad")])

    assert out[0] == {"pages": [{"title": "T", "members": [1]}]}
    assert out[1] == {}, "失败的一条应记空结果（调用方据此让成员各自成页）"
    assert seen == ["b"], "失败要回调出去，不能静默"


def test_同步运行器_未返回不当作失败():
    """离线语义：`PendingError` 是「还没算」而不是「算不出来」，必须原样抛出。"""
    def call(system, user, max_tokens, tag):
        raise PendingError("batch not ready", pending_ids=["a"])

    runner = SyncRunner(call, on_error=lambda req, e: pytest.fail(
        "PendingError 不该走 on_error 被降级"))

    with pytest.raises(PendingError) as ei:
        runner.run([BatchRequest("a", "s", "u")])

    assert ei.value.pending_ids == ["a"]


def test_同步运行器_记录执行过的请求():
    runner = SyncRunner(lambda s, u, mt, tag: {})
    runner.run([BatchRequest("a", "s", "u"), BatchRequest("b", "s", "u")])
    assert [r.custom_id for r in runner.calls] == ["a", "b"]


# ---------------------------------------------------------------------------
# 离线任务的标记 + 现场（「已经交给厂商了」这件事必须能跨进程记住）
# ---------------------------------------------------------------------------

def _job(**kw) -> BatchJob:
    base = dict(batch_id="batch_123", provider="demo", submitted_at=1000.0,
                items={"l2plan|域A|0": {"domain": "域A", "batch_no": 0},
                       "l2plan|域A|1": {"domain": "域A", "batch_no": 1},
                       "l2plan|域B|0": {"domain": "域B", "batch_no": 0}},
                members={"域A": [{"title": "P1", "summary": "s"}],
                         "域B": [{"title": "Q1", "summary": "s"}]})
    base.update(kw)
    return BatchJob(**base)


def test_标记_待回读与状态():
    job = _job()
    assert job.pending_ids() == ["l2plan|域A|0", "l2plan|域A|1", "l2plan|域B|0"]
    assert job.results_state == "pending"
    assert job.domains() == ["域A", "域B"]

    job.results["l2plan|域A|0"] = {"ok": True, "data": {}}
    assert job.results_state == "partial"
    assert job.pending_ids() == ["l2plan|域A|1", "l2plan|域B|0"]

    job.results["l2plan|域A|1"] = {"ok": True, "data": {}}
    job.results["l2plan|域B|0"] = {"ok": True, "data": {}}
    assert job.results_state == "complete"
    assert job.pending_ids() == []


def test_标记_落盘再读回_现场不丢():
    """**关键**：提交在这次运行、取结果在下次运行，成员快照必须原样带回来。"""
    job = _job()

    text = json.dumps([job.to_dict()], ensure_ascii=False)
    back = BatchJob.from_dict(json.loads(text)[0])

    assert back.batch_id == "batch_123"
    assert back.submitted_at == 1000.0
    assert back.pending_ids() == job.pending_ids()
    assert back.members["域A"] == [{"title": "P1", "summary": "s"}], \
        "成员快照丢了就还原不回全局编号"


def test_标记_通过文件往返(tmp_path):
    p = tmp_path / STATE_NAME
    save_state(p, [_job()])
    assert p.exists()
    assert not (tmp_path / (STATE_NAME + ".tmp")).exists(), "临时文件不该留下"

    jobs = load_state(p)

    assert len(jobs) == 1 and jobs[0].batch_id == "batch_123"


def test_标记_文件缺失或损坏都不抛(tmp_path):
    """没有标记只意味着"没提交过离线任务"，不该让整轮跑不动。"""
    assert load_state(tmp_path / "不存在.json") == []

    bad = tmp_path / "bad.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    assert load_state(bad) == []

    notlist = tmp_path / "notlist.json"
    notlist.write_text('{"batch_id": "x"}', encoding="utf-8")
    assert load_state(notlist) == []

    nocid = tmp_path / "nocid.json"
    nocid.write_text('[{"provider": "demo"}]', encoding="utf-8")
    assert load_state(nocid) == [], "没有 batch_id 的条目应被跳过"


# ---------------------------------------------------------------------------
# 处置决策：等不及了就改走实时
# ---------------------------------------------------------------------------

def test_决策_结果齐了就消费():
    job = _job(results={k: {"ok": True, "data": {}} for k in _job().items})
    assert decide(job, now=99999.0) == CONSUME


def test_决策_未超预算就继续等():
    job = _job()
    assert decide(job, now=1000.0 + 60, deadline_s=3600) == WAIT


def test_决策_不限时时一直等():
    """离线批处理本来就慢，默认不该催它。"""
    job = _job()
    assert decide(job, now=1000.0 + 86400 * 7, deadline_s=0) == WAIT


def test_决策_超预算且有部分结果_先用已到其余走实时():
    job = _job()
    job.results["l2plan|域A|0"] = {"ok": True, "data": {}}
    assert decide(job, now=1000.0 + 7200, deadline_s=3600) == PARTIAL


def test_决策_超预算且一条都没到_整批走实时():
    job = _job()
    assert decide(job, now=1000.0 + 7200, deadline_s=3600) == FALLBACK


# ---------------------------------------------------------------------------
# M1：厂商侧字段、终局判定、作废回池
#
# 这些字段是**回读轮**与**模式切换**的依据：不知道厂商把任务跑到哪一步，就没法
# 判断"该继续等"还是"该改走实时"；不作废标记，切换模式时那批域既不进基线、
# 也不回池，就会永久卡住。
# ---------------------------------------------------------------------------

def test_标记_厂商字段能落盘再读回():
    job = _job()
    job.input_file_id = "file-in"
    job.output_file_id = "file-out"
    job.error_file_id = "file-err"
    job.provider_status = "Completed"
    job.expire_at = 123456.0

    back = BatchJob.from_dict(job.to_dict())

    assert back.input_file_id == "file-in"
    assert back.output_file_id == "file-out" and back.error_file_id == "file-err"
    assert back.provider_status == "completed", "厂商状态一律 lower() 存取"
    assert back.expire_at == 123456.0
    assert back.voided is False


def test_标记_旧版状态文件没有新字段也能读回():
    """向后兼容：产物目录里可能躺着 M1 之前写的 `batch_state.json`。"""
    old = {"batch_id": "batch_old", "stage": "l2plan", "submitted_at": 1.0,
           "items": {"c1": {"domain": "域A"}}, "members": {}, "results": {}}

    back = BatchJob.from_dict(old)

    assert back.batch_id == "batch_old" and back.voided is False
    assert back.provider_status == "" and back.expire_at == 0.0


@pytest.mark.parametrize("state", ["failed", "expired", "cancelled"])
def test_决策_厂商已终局时不必等deadline(state):
    """终局任务永远不会再变 —— 继续等只是白等（实测 `expired` 会一直躺着）。"""
    job = _job(provider_status=state)
    assert job.is_terminal() is True
    assert decide(job, now=1000.0 + 1, deadline_s=0) == FALLBACK


def test_决策_终局但有部分结果_消费已到的其余走实时():
    job = _job(provider_status="expired")
    job.results["l2plan|域A|0"] = {"ok": True, "data": {}}
    assert decide(job, now=1000.0, deadline_s=0) == PARTIAL


def test_决策_进行中的状态不算终局():
    for st in ("created", "validating", "in_progress", "finalizing", "cancelling"):
        assert _job(provider_status=st).is_terminal() is False
    assert decide(_job(provider_status="in_progress"), now=1000.0, deadline_s=0) == WAIT


@pytest.mark.parametrize("deadline", [0.0, 3600.0])
def test_决策_厂商完成后立刻消费不受deadline影响(deadline):
    """`completed` 的意义就是"结果已经在那儿了" —— 与等多久无关。

    漏掉这一条时（M1 初版就没有），`completed` 且 `deadline_s <= 0`（**默认不限时**）
    会被判成 `WAIT`，等于永远不去取结果。
    """
    job = _job(provider_status="completed")
    assert decide(job, now=1000.0, deadline_s=deadline) == CONSUME


def test_作废_返回覆盖的域供回池():
    job = _job()

    assert job.void("模式切回实时") == ["域A", "域B"], "作废要报出需要回池的域"
    assert job.voided is True and job.voided_reason == "模式切回实时"
    assert BatchJob.from_dict(job.to_dict()).voided is True, "作废标记必须能落盘"


def test_结果状态与厂商状态不是一回事():
    """`results_state` 说"结果齐没齐"，`provider_status` 说"厂商跑到哪个阶段"。"""
    job = _job(provider_status="in_progress")
    for k in list(job.items):
        job.results[k] = {"ok": True, "data": {}}

    assert job.results_state == "complete"
    assert job.provider_status == "in_progress", "两者互不影响、互不覆盖"


# ---------------------------------------------------------------------------
# M2a：离线运行器 —— 把请求交出去、当场抛 Pending 保留现场
# ---------------------------------------------------------------------------

class _FakeUploader:
    """假上传器：记下每次调用，全程不发真请求。"""

    def __init__(self, *, status="Created", expires_at=999.0):
        self.uploads: list[tuple[str, str]] = []
        self.creates: list[tuple[str, str, int]] = []
        self.status = status
        self.expires_at = expires_at

    def upload(self, jsonl_text, *, filename="input.jsonl"):
        self.uploads.append((jsonl_text, filename))
        return {"id": "file-1", "bytes": len(jsonl_text.encode("utf-8"))}

    def create(self, input_file_id, *, name="", n_requests=0):
        self.creates.append((input_file_id, name, n_requests))
        return {"id": "batch-1", "status": self.status,
                "expires_at": self.expires_at}


def _members(n: int) -> list[dict]:
    return [{"title": "P%d" % i, "summary": "摘要", "type": "主题",
             "body": "正文", "mids": [], "file": "f%03d.md" % i}
            for i in range(1, n + 1)]


def test_离线运行器_提交后抛Pending并留下现场():
    from agentmemhub.llm_batch import OfflineRunner

    up = _FakeUploader()
    seen = []
    runner = OfflineRunner(uploader=up, model="mimo-v2.6-flash", stage="l2plan",
                           job_name="agentmemhub-wiki-l2plan",
                           on_submitted=seen.append)
    reqs = [BatchRequest(custom_id="l2plan|域A|0", system="S", user="U"),
            BatchRequest(custom_id="l2plan|域A|1", system="S2", user="U2")]

    with pytest.raises(PendingError) as e:
        runner.run(reqs)

    assert e.value.pending_ids == ["l2plan|域A|0", "l2plan|域A|1"], \
        "现场要带全部 custom_id（回读时靠它对号入座）"
    assert seen and seen[0]["batch_id"] == "batch-1"
    assert seen[0]["input_file_id"] == "file-1"
    assert seen[0]["provider_status"] == "created", "厂商状态一律 lower()"
    assert seen[0]["expire_at"] == 999.0 and seen[0]["n_requests"] == 2

    text, fname = up.uploads[0]
    assert fname == "l2plan.jsonl"
    assert '"custom_id": "l2plan|域A|0"' in text
    assert '"model": "mimo-v2.6-flash"' in text
    assert up.creates == [("file-1", "agentmemhub-wiki-l2plan", 2)]


def test_离线运行器_空请求不提交():
    from agentmemhub.llm_batch import OfflineRunner

    up = _FakeUploader()
    assert OfflineRunner(uploader=up).run([]) == []
    assert up.uploads == [] and up.creates == [], "没有请求就不该上传/建任务"


def test_离线运行器_建任务失败时不落现场也不吞异常():
    """建任务失败可能是"结果未知" —— 不能当成"已提交"，也别自己重试。"""
    from agentmemhub.llm_batch import OfflineRunner

    seen = []

    class _Boom:
        def upload(self, t, **kw):
            return {"id": "f"}

        def create(self, i, **kw):
            raise RuntimeError("网络断了")

    with pytest.raises(RuntimeError):
        OfflineRunner(uploader=_Boom(), on_submitted=seen.append).run(
            [BatchRequest(custom_id="c", system="s", user="u")])

    assert seen == [], "没拿到 batch_id 就没有现场可落"


def test_离线运行器_能直接接进stage_plan接缝():
    """端到端接缝：`stage_plan(runner=离线运行器)` 把请求交出去并冒泡 Pending。"""
    from agentmemhub.llm_batch import OfflineRunner
    from wiki_aggregate import stage_plan

    up = _FakeUploader()

    with pytest.raises(PendingError):
        stage_plan(None, {"name": "域A"}, _members(5), batch=2,
                   runner=OfflineRunner(uploader=up, model="m", stage="l2plan"))

    assert len(up.creates) == 1, "一个域 = 一次提交（请求全部装进同一个 JSONL）"
    assert up.creates[0][2] == 3, "5 个成员按 batch=2 切成 3 条请求"
    lines = [ln for ln in up.uploads[0][0].splitlines() if ln.strip()]
    assert len(lines) == 3, "3 条请求 → JSONL 三行"
    assert all("l2plan|" in t for t, _ in up.uploads), "custom_id 要带定位信息"


# ---------------------------------------------------------------------------
# M2c：两路一致性补丁（正文清洗 + 可选项透传）
# ---------------------------------------------------------------------------

def test_编码时剥离零宽与控制字符():
    """与实时路一致：那些码点最易触发审核与解析异常。"""
    reqs = [BatchRequest(custom_id="c1", system="正常\u200b文本", user="u\ufeff")]

    body = json.loads(batch_to_jsonl(reqs, "m").strip())["body"]

    assert "\u200b" not in body["messages"][0]["content"]
    assert "\ufeff" not in body["messages"][1]["content"]
    assert "正常" in body["messages"][0]["content"]


def test_可选项透传进请求体():
    """`thinking` / `reasoning_effort` 这些开关由配置传入，保证两条路发出去一致。"""
    reqs = [BatchRequest(custom_id="c1", system="s", user="u")]

    body = json.loads(batch_to_jsonl(
        reqs, "m",
        extra={"thinking": {"type": "disabled"}, "reasoning_effort": "low"}
    ).strip())["body"]

    assert body["thinking"] == {"type": "disabled"}
    assert body["reasoning_effort"] == "low"
    assert body["model"] == "m" and body["max_tokens"] == 16000, "原有字段不受影响"
