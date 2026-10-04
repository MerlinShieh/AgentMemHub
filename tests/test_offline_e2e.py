# -*- coding: utf-8 -*-
"""离线三段式的**端到端**测试（全程假上传器 / 假 client，**不发任何真请求**）。

为什么需要它（原计划 M6 的明确要求）：三段式是"**跨运行的异步流水线**" ——
提交与落盘发生在**两次不同的运行**里。单测覆盖的是"每一段"，而**段落之间的接口**
只有端到端才验得到：

  · 现场（`batch_state.json` + 成员快照）能否被**下一次运行**读出来消费；
  · 编号能否从全局映射回批内、再由结果映射回页面；
  · 单成员页（本地生成、不占额度）与多成员页（走 LLM）能否在同一域里各就各位；
  · 落盘产物是否完整（页面 + 域目录 + 索引）。

⚠️ 这条测试**绝不能**碰真凭据：假上传器 + 假 client + 进程级沙箱熔断。
"""
from __future__ import annotations

import json

import pytest

from agentmemhub.failures import FailureLog
from agentmemhub.llm_batch import STATE_NAME, OfflineRunner, PendingError, load_state
from agentmemhub import wiki_batch


# ---------------------------------------------------------------------------
# 假通道
# ---------------------------------------------------------------------------

class _FakeUploader:
    """假上传器：`upload` 给个文件 id、`create` 给个任务 id —— 不碰网络。"""

    def __init__(self, batch_id: str = "batch-e2e", file_id: str = "file-e2e"):
        self.batch_id = batch_id
        self.file_id = file_id
        self.uploaded: list[str] = []
        self.created: list[dict] = []

    def upload(self, text: str, *, filename: str = "") -> dict:
        self.uploaded.append(text)
        return {"id": self.file_id, "status": "active"}

    def create(self, input_file_id: str, *, name: str = "",
               n_requests: int = 0) -> dict:
        self.created.append({"input_file_id": input_file_id, "name": name,
                             "n_requests": n_requests})
        return {"id": self.batch_id, "status": "created", "expires_at": 0.0}


class _FakeClient:
    """假批量客户端：`completed` + 一份手写的结果 JSONL。"""

    def __init__(self, *, output: str = "", error: str = "",
                 status: str = "completed"):
        self.output, self.error, self.status = output, error, status

    def retrieve(self, batch_id: str) -> dict:
        return {"status": self.status,
                "output_file_id": "file-out" if self.output else "",
                "error_file_id": "file-err" if self.error else ""}

    def download(self, file_id: str) -> str:
        return self.output if file_id == "file-out" else self.error


def _result_line(custom_id: str, page: dict) -> str:
    """造一条**厂商成功返回**的 JSONL 行（形状与官方一致）。

    ⚠️ 正文里放的是**单个页面对象** —— 一个编译请求对应**一个页组**，所以
    `assemble_pages()` 拿到的 `r["data"]` 就是页面本身（不是 `{"pages": [...]}`）。
    """
    return json.dumps({
        "custom_id": custom_id,
        "response": {"status_code": 200, "body": {"choices": [
            {"message": {"content": json.dumps(page, ensure_ascii=False)}}]}},
    }, ensure_ascii=False)


@pytest.fixture()
def off(tmp_path, monkeypatch):
    """把 `scripts/` 加进 path（聚合层在那边），并返回 (out 目录, 模块)。"""
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sp = str(root / "scripts")
    if sp not in sys.path:
        sys.path.insert(0, sp)
    import wiki_aggregate as wa

    out = tmp_path / "l2"
    out.mkdir()
    return out, wa


def _plan():
    """一个域、两个页组：多成员（走 LLM）+ 单成员（本地生成）。

    字段要与 `compile_prompt()` 的取用一致（`title` / `source` / `cid` / `body`）。
    """
    p1 = {"file": "a__s1.md", "source": "zcode", "cid": "s1", "title": "页A",
          "type": "concept", "summary": "摘要A", "body": "正文A [m1]",
          "mids": ["m1"], "related": []}
    p2 = {"file": "b__s2.md", "source": "zcode", "cid": "s2", "title": "页B",
          "type": "fact", "summary": "摘要B", "body": "正文B [m2]",
          "mids": ["m2"], "related": []}
    return [("域A", [{"title": "合并页", "group": [p1, p2]},      # 两条成员 → 要 LLM
                     {"title": "单独页", "group": [p1]}])]        # 单成员 → 本地


def test_三段式端到端_提交到落盘(off):
    """① 提交（假上传器）→ ② 厂商完成回读 → ③ 落盘。"""
    out, wa = off
    reqs, items, members = wa.compile_requests(_plan(), ["页A", "页B"])

    assert len(reqs) == 1, "只有多成员页进请求（单成员页本地生成、不占额度）"

    # ---- ① 提交：运行器落现场后抛 PendingError，**由 `_submit_stage` 接住**并回报
    #      `offline_submitted`（"结果未回"不是失败，所以不往外抛）
    up = _FakeUploader()
    runner = OfflineRunner(uploader=up, model="fake-model", stage="l2compile")
    status = wa._submit_stage(out, runner, reqs, items, members, stage="l2compile")

    assert status["offline_submitted"]["batch_id"] == "batch-e2e"
    assert status["offline_submitted"]["n_requests"] == 1

    jobs = load_state(out / STATE_NAME)
    assert len(jobs) == 1 and jobs[0].batch_id == "batch-e2e"
    assert jobs[0].members == members, \
        "成员快照必须落盘 —— 落盘轮是**另一次运行**，那时 L1 页面早已不在内存里"
    assert len(up.uploaded) == 1, "只上传一次（整个域集合一个文件、一个任务）"

    # ---- ② 厂商跑完：回读（结果 JSONL 手写，形状与官方一致）
    client = _FakeClient(output=_result_line(
        reqs[0].custom_id,
        {"title": "合并页", "type": "concept", "summary": "合并摘要",
         "body": "合并正文 [m1] [m2]", "mids": ["m1", "m2"], "related": []}) + "\n")

    st = wiki_batch.import_results(client, out)

    assert st["consumed"] == ["batch-e2e"], "结果齐了就该被消费"
    assert st["voided"] == [] and st["waited"] == []
    results = st["results"]["batch-e2e"]
    assert results[reqs[0].custom_id]["ok"] is True

    # ---- ③ 落盘（跨运行：只用现场 + 刚回读的结果）
    status = wa.write_offline_stage(out, members, results,
                                    FailureLog(out / "failures.jsonl"),
                                    partial=True)

    assert "域A" in status["written_domains"]
    assert status["skipped_domains"] == []
    assert status["pages"] >= 2, "多成员页与单成员页都要落地"

    md = sorted(p.name for p in out.rglob("*.md") if p.name != "index.md")
    assert len(md) >= 2, "每个页组一个页面文件"
    body = "\n".join(p.read_text(encoding="utf-8") for p in out.rglob("*.md"))
    assert "合并摘要" in body, "多成员页用的是**回读结果**（不是源页内容）"
    assert "摘要A" in body, "单成员页沿用源页内容（本地生成、没花额度）"
    assert (out / "index.md").exists(), "落盘要一并产出索引"


def test_三段式端到端_坏JSON会走修复兜底(off):
    """回读阶段：JSON 坏的条目**不丢**（保留原文），配了修复模型就救回来。"""
    out, wa = off
    reqs, items, members = wa.compile_requests(_plan(), [])
    up = _FakeUploader(batch_id="b-bad")
    runner = OfflineRunner(uploader=up, model="fake-model", stage="l2compile")
    wa._submit_stage(out, runner, reqs, items, members, stage="l2compile")

    # 厂商返回的正文**不是 JSON**（模型吐了围栏/散文）
    client = _FakeClient(output=json.dumps({
        "custom_id": reqs[0].custom_id,
        "response": {"status_code": 200, "body": {"choices": [
            {"message": {"content": "好的，我分成两页：```json\n{坏的"}}]}},
    }, ensure_ascii=False) + "\n")

    st = wiki_batch.import_results(client, out)

    r = st["results"]["b-bad"][reqs[0].custom_id]
    assert r["ok"] is False, "坏 JSON 就是坏 JSON（没有修复模型时）"
    assert r.get("content"), "**原文必须留着** —— 重试与修复两条路都要它"


def test_三段式端到端_终局失败会作废回池(off):
    """厂商 `failed` ⇒ 作废回池（**保留现场**），下次更新会重新提交。"""
    out, wa = off
    reqs, items, members = wa.compile_requests(_plan(), [])
    up = _FakeUploader(batch_id="b-fail")
    runner = OfflineRunner(uploader=up, model="fake-model", stage="l2compile")
    wa._submit_stage(out, runner, reqs, items, members, stage="l2compile")

    st = wiki_batch.import_results(_FakeClient(status="failed"), out)

    assert [v["batch_id"] for v in st["voided"]] == ["b-fail"]
    assert st["consumed"] == []
    kept = load_state(out / STATE_NAME)
    assert len(kept) == 1 and kept[0].voided is True, \
        "**保留现场**（只标记 voided）—— 否则那批请求的成员快照就永久丢了"
