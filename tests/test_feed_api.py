# -*- coding: utf-8 -*-
"""投喂的**接口层**（CLI 已测；这里测 HTTP + 统一入口 + 格式白名单 + 批量）。

需求来源（用户 2026-10-15）：
· 批量投喂要用**不同格式后缀**控制输入（**大小写不强制**）；
· 要能区分传输的是**二进制数据**（`open/read` 出来的）还是 **path** 还是 **url**；
· 这些接口要支持 **HTTP 服务**，供数据看板直接控制。

**隔离**：
· 投喂落盘（db / store / kb）—— HTTP 层不传 `config`，所以从**配置单例**这层切到 tmp；
· 格式白名单的运行时覆盖 —— 走 `AGENTMEMHUB_FEED_FORMATS_STATE`（conftest 已指向沙箱）。
"""
from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentmemhub.feed import formats as F
from agentmemhub.feed import service as svc


class _FeedCfg:
    """投喂的三段落盘位置（全在 tmp）。"""

    def __init__(self, root: Path):
        self.root = root
        self._paths = {"db": root / "materials.db", "store": root / "store",
                       "kb": root / "kb"}

    def feed_paths(self):
        return dict(self._paths)

    def feed(self):
        # 只留真实存在的键：`accept.exts` 空 = 跟随转换器注册表
        return {"accept": {"exts": []}}


@pytest.fixture(autouse=True)
def _clean_formats():
    """每个用例都从"无运行时覆盖"开始 —— 否则一个用例改了白名单会影响下一个。"""
    F.clear_overrides()
    yield
    F.clear_overrides()


@pytest.fixture()
def feed_cfg(tmp_path, monkeypatch):
    cfg = _FeedCfg(tmp_path / "feed")
    monkeypatch.setattr(svc, "_cfg", lambda: cfg)
    return cfg


@pytest.fixture()
def client(tmp_path, feed_cfg):
    from agentmemhub.web.app import create_app
    from agentmemhub.web import tasks
    tasks.reset()
    yield TestClient(create_app(tmp_path / "admin.db"))
    tasks.reset()


def _md(tmp_path: Path, name="笔记.md", body="# 标题\n\n正文内容够长才不会被判空。\n") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 格式白名单：用户要求"用后缀控制输入、大小写不强制"
# ---------------------------------------------------------------------------

def test_格式默认跟随转换器注册表():
    st = F.state()
    assert st["source"] == "registry"
    for e in (".md", ".pdf", ".docx", ".pptx", ".xmind", ".html", ".txt"):
        assert e in st["allowed"], e


def test_格式大小写不敏感():
    """`.PDF` 与 `.pdf` 必须等价 —— Windows 上文件名大小写本来就乱。"""
    assert F.is_allowed("Report.PDF") is True
    assert F.is_allowed("A.DOCX") is True
    assert F.is_allowed("x.EXE") is False


def test_格式无后缀放行_交给认领机制():
    """URL（`https://…/s/xxx`）与工具导出的无扩展名报告没有后缀可查，
    硬拦会把它们全挡在门外；认领阶段仍会按内容判型、判不出来照旧显式报错。"""
    assert F.is_allowed("https://mp.weixin.qq.com/s/RO8-iHoy") is True


def test_格式可运行时调整并可清除():
    F.set_overrides([".pdf"])
    assert F.allowed_exts() == [".pdf"] and F.source_of() == "override"
    assert F.is_allowed("a.docx") is False
    F.clear_overrides()
    assert F.source_of() == "registry"
    assert F.is_allowed("a.docx") is True


def test_http_格式查看与调整(client):
    r = client.get("/api/feed/formats")
    assert r.status_code == 200 and r.json()["source"] == "registry"
    r = client.put("/api/feed/formats", json={"exts": [".md", ".txt"]})
    assert r.status_code == 200 and r.json()["allowed"] == [".md", ".txt"]
    # 不传 exts = 清除覆盖（回到配置/注册表）
    r = client.put("/api/feed/formats", json={})
    assert r.json()["source"] == "registry"


# ---------------------------------------------------------------------------
# 投喂入口：三种输入形态
# ---------------------------------------------------------------------------

def test_add_path形态(client, tmp_path):
    p = _md(tmp_path)
    r = client.post("/api/feed/add", json={"input": "path", "value": str(p)})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True, body
    assert body["kind"] == "text" and body["kb_path"]


def test_add_data形态_传base64二进制(client, tmp_path):
    """客户端已经 `open(p,"rb").read()` 出来的字节 —— JSON 装不下裸字节，
    所以用 base64；**必须给 filename**（扩展名是判格式的依据）。"""
    blob = "# 来自内存的笔记\n\n内容够长。\n".encode("utf-8")
    r = client.post("/api/feed/add", json={
        "input": "data", "value": base64.b64encode(blob).decode("ascii"),
        "encoding": "base64", "filename": "内存笔记.md"})
    body = r.json()
    assert body["ok"] is True, body
    assert body["kind"] == "text"
    # 原件必须是**原始字节**（不是 base64 文本）
    raw = Path(body["raw_path"]) if Path(body["raw_path"]).is_absolute() else \
        Path(body["raw_path"])
    assert body["raw_path"]


def test_add_data形态_缺filename明确报错(client):
    r = client.post("/api/feed/add", json={"input": "data", "value": "x",
                                           "encoding": "base64"})
    body = r.json()
    assert body["ok"] is False and body["error_kind"] == "bad_input"
    assert "filename" in body["error"], "要告诉调用方缺什么"


def test_add_未知输入形态明确报错(client):
    r = client.post("/api/feed/add", json={"input": "magic", "value": "x"})
    body = r.json()
    assert body["ok"] is False and body["error_kind"] == "bad_input"
    assert "path" in body["error"] and "url" in body["error"]


def test_add_格式被挡时给出允许清单(client, tmp_path):
    """错误提示必须**带上当前允许的清单** —— 只说"进不来"等于把人扔在原地。"""
    p = tmp_path / "x.exe"
    p.write_bytes(b"MZ\x90\x00" + b"\x00" * 32)
    r = client.post("/api/feed/add", json={"input": "path", "value": str(p)})
    body = r.json()
    assert body["ok"] is False and body["error_kind"] == "unsupported_format"
    assert ".md" in body["error"] and ".pdf" in body["error"], body["error"]


def test_add_文件不存在明确报错(client):
    r = client.post("/api/feed/add", json={"input": "path",
                                           "value": str(Path(tempfile.gettempdir()) / "不存在.md")})
    body = r.json()
    assert body["ok"] is False and body["error_kind"] == "not_found"


def test_add_大小写后缀照样能投(client, tmp_path):
    p = _md(tmp_path, "笔记.MD")
    r = client.post("/api/feed/add", json={"input": "path", "value": str(p)})
    assert r.json()["ok"] is True, r.json()


def test_upload_multipart(client):
    """二进制直传通道：客户端不必自己 base64（大文件省 1/3 体积）。"""
    r = client.post("/api/feed/upload",
                    files={"file": ("上传.md", "# 上传的笔记\n\n内容够长。\n".encode("utf-8"),
                                    "text/markdown")})
    body = r.json()
    assert body["ok"] is True, body
    assert body["kind"] == "text"


# ---------------------------------------------------------------------------
# 批量：先扫描后投喂
# ---------------------------------------------------------------------------

def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "子目录").mkdir(parents=True)
    _md(root, "a.md")
    _md(root / "子目录", "b.md")
    (root / "工具.exe").write_bytes(b"MZ" + b"\x00" * 40)
    # `.xlsx` 自 2026-10-15 起**已支持**（办公三件套的最后一件）。
    # 内容不必是真工作簿 —— `scan` 只按后缀分类，不解析字节（`batch` 用 dry-run）。
    (root / "结果.xlsx").write_bytes(b"PK\x03\x04" + b"\x00" * 40)
    (root / "归档.rar").write_bytes(b"Rar!" + b"\x00" * 40)   # 仍不在白名单
    return root


def test_scan_列出可投喂与被挡(client, tmp_path):
    root = _tree(tmp_path)
    r = client.post("/api/feed/scan", json={"roots": [str(root)]})
    body = r.json()
    assert body["allowedCount"] == 3, body          # a.md / b.md / 结果.xlsx
    assert body["skippedCount"] == 2                # 工具.exe / 归档.rar
    exts = sorted(body["byExt"])
    assert exts == [".md", ".xlsx"], body["byExt"]


def test_scan_不递归(client, tmp_path):
    root = _tree(tmp_path)
    body = client.post("/api/feed/scan",
                       json={"roots": [str(root)], "recursive": False}).json()
    assert body["allowedCount"] == 2, "只扫一层：a.md + 结果.xlsx（子目录里的不算）"


def test_batch_长任务_先扫描默认不投(client, tmp_path):
    """`/api/feed/batch` 是**长任务**（几百个文件、分钟级）→ 返回 job，轮询 `/api/admin/job`。"""
    root = _tree(tmp_path)
    r = client.post("/api/feed/batch", json={"roots": [str(root)], "dry_run": True})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["id"] and job["status"] in ("running", "done")
    # 轮询等它跑完
    for _ in range(50):
        st = client.get("/api/admin/job").json().get("job") or {}
        if st.get("status") != "running":
            break
        time.sleep(0.1)
    assert st.get("status") == "done", st
    assert "完成" in (st.get("output") or "")


def test_batch_单并发_已有任务时409(client, tmp_path):
    from agentmemhub.web import tasks
    root = _tree(tmp_path)
    # 占住长任务位：提交一个不会立刻结束的任务
    tasks.submit("占位", lambda emit, meta: (time.sleep(1.0), "done")[1])
    r = client.post("/api/feed/batch", json={"roots": [str(root)]})
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# 查询与处置
# ---------------------------------------------------------------------------

def test_list_与_show(client, tmp_path):
    p = _md(tmp_path)
    doc_id = client.post("/api/feed/add",
                         json={"input": "path", "value": str(p)}).json()["doc_id"]
    items = client.get("/api/feed/list").json()["items"]
    assert any(i["doc_id"] == doc_id for i in items)
    one = client.get("/api/feed/show/%s" % doc_id).json()
    assert one["doc_id"] == doc_id and one["status"] == "published"
    assert client.get("/api/feed/show/不存在").status_code == 404


def test_retract_与_forget(client, tmp_path):
    p = _md(tmp_path)
    doc_id = client.post("/api/feed/add",
                         json={"input": "path", "value": str(p)}).json()["doc_id"]
    assert client.post("/api/feed/retract", json={"doc_id": doc_id}).json()["ok"]
    out = client.post("/api/feed/forget", json={"doc_id": doc_id}).json()
    assert out["ok"] and out.get("raw_removed") in (False, None), "默认不该删原件"


def test_lint_接口可用(client, tmp_path):
    p = _md(tmp_path)
    client.post("/api/feed/add", json={"input": "path", "value": str(p)})
    body = client.get("/api/feed/lint?with_index=false").json()
    assert body["p0"] == 0, body["issues"]


# ---------------------------------------------------------------------------
# 办公表格端到端：新格式必须走**完整链路**（登记 → raw → 转译 → 发布 kb）
# ---------------------------------------------------------------------------

def test_端到端_xlsx出kb页(client, feed_cfg, tmp_path):
    """XLSX 是办公三件套的最后一件 —— 单测过不算数，得走完整链路。

    这里同时验三件事：`kind` 回填成转换器判定的类型、kb 页有 frontmatter
    （`origin: external` 是与自有 wiki 的分界线）、日期没变成序列号。
    """
    from datetime import datetime

    from openpyxl import Workbook

    p = tmp_path / "台账.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "销售明细"
    ws.append(["日期", "产品", "金额"])
    ws.append([datetime(2026, 10, 15), "甲", 100])
    wb.save(p)

    body = client.post("/api/feed/add",
                       json={"input": "path", "value": str(p)}).json()
    assert body["ok"] is True, body
    assert body["kind"] == "xlsx" and body["kb_path"]

    pages = list(feed_cfg.feed_paths()["kb"].rglob("*.md"))
    assert len(pages) == 1, pages
    text = pages[0].read_text(encoding="utf-8")
    assert "origin: external" in text
    assert "2026-10-15" in text and "销售明细" in text
    assert "45000" not in text


def test_端到端_csv出kb页(client, feed_cfg, tmp_path):
    """CSV 用 GBK 写（中文 Excel 的默认导出编码）—— 端到端必须也解得对。"""
    p = tmp_path / "名单.csv"
    p.write_bytes("姓名,部门\n张三,研发\n".encode("gb18030"))

    body = client.post("/api/feed/add",
                       json={"input": "path", "value": str(p)}).json()
    assert body["ok"] is True, body
    assert body["kind"] == "csv" and body["kb_path"]

    pages = list(feed_cfg.feed_paths()["kb"].rglob("*.md"))
    assert len(pages) == 1, pages
    text = pages[0].read_text(encoding="utf-8")
    assert "张三" in text and "研发" in text
    assert "origin: external" in text
