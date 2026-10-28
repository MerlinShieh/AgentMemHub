# -*- coding: utf-8 -*-
"""网关层防御性校验的回归测试（2026-10-26 审计修复配套）。

这些校验的共同点：**删掉它们，系统平时照样跑**。所以只有测试守住，才不会在
下一次重构里被当成"多余的严格"顺手去掉。每条都对应一个具体可利用面，而不是
泛泛的"更稳妥"：

  · 快照 id 会被拼进备份根 → `../..` 能越出根目录做 rmtree/copytree
  · 投喂 `input=path` 读本地文件 → 读 `~/.ssh/id_rsa` 等于把它发布成知识库页面
  · wiki 产物目录来自请求方 → `?l1=C:\\Windows` 会让编译往系统目录写
  · 写端点全是 query 参数、无鉴权 → 浏览器里任意网页都能跨站触发
"""
from __future__ import annotations

import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agentmemhub import snapshot
from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


# ---------------------------------------------------------------- 快照 id
@pytest.mark.parametrize("bad", ["..", ".", "../x", "a/b", "a\\b", "C:x", "", "   "])
def test_快照id拒绝越界与空值(bad):
    """`..`/分隔符/盘符能改写出备份根之外的目标，必须在拼路径前就拒。"""
    with pytest.raises(ValueError):
        snapshot._safe_sid(bad)


def test_快照id接受普通时间戳():
    assert snapshot._safe_sid("20261023_204727") == "20261023_204727"
    assert snapshot._safe_sid("  abc  ") == "abc"      # 两端空白是手滑，不是攻击


# ------------------------------------------------------- wiki 产物目录
def _cfg_with(wiki: dict) -> object:
    return types.SimpleNamespace(wiki=wiki)


def _guard():
    from agentmemhub.web.app import _wiki_out_guard
    return _wiki_out_guard


def test_wiki产物目录接受配置同根下的路径(tmp_path, monkeypatch):
    root = tmp_path / "wiki_work"
    monkeypatch.setattr(
        "agentmemhub.config.config",
        lambda: _cfg_with({"out_l1": str(root / "out_l1"),
                           "out_l2": str(root / "out_l2")}))
    ok = str(root / "out_l1")
    assert _guard()(ok, "out_l1") == ok


def test_wiki产物目录拒绝任意路径(tmp_path, monkeypatch):
    root = tmp_path / "wiki_work"
    monkeypatch.setattr(
        "agentmemhub.config.config",
        lambda: _cfg_with({"out_l1": str(root / "out_l1")}))
    with pytest.raises(HTTPException) as ei:
        _guard()(str(tmp_path / "elsewhere"), "out_l1")
    assert ei.value.status_code == 400


def test_wiki产物目录拒绝穿越(tmp_path, monkeypatch):
    root = tmp_path / "wiki_work"
    monkeypatch.setattr(
        "agentmemhub.config.config",
        lambda: _cfg_with({"out_l1": str(root / "out_l1")}))
    with pytest.raises(HTTPException):
        _guard()(str(root / "out_l1" / ".." / ".." / "evil"), "out_l1")


def test_wiki产物目录未配置时不设限(tmp_path, monkeypatch):
    """配置里没写产物目录（比如刚 clone 下来）→ 保持旧行为，不要凭空 400。"""
    monkeypatch.setattr("agentmemhub.config.config", lambda: _cfg_with({}))
    assert _guard()(str(tmp_path / "anywhere"), "out_l1")


# ------------------------------------------------------------------ CSRF
def _client() -> TestClient:
    tmp = Path(tempfile.mkdtemp()) / "guard.db"
    s = Store(tmp)
    s.replace_source("zcode", [{
        "source": "zcode", "id": "s1", "title": "t", "cwd": "w",
        "created_at": 1, "updated_at": 1, "model": "m", "meta": {},
        "events": renumber([Event(role="user", content="hi", time=1)]),
    }], signature="t")
    s.close()
    from agentmemhub.web.app import create_app
    return TestClient(create_app(tmp))


def test_跨站写请求被拒():
    """浏览器里 `<form method=POST action="http://127.0.0.1:8765/...">` 不带预检，
    所以只能靠 Origin 兜住 —— 这是本地面板唯一的 CSRF 防线。"""
    c = _client()
    with mock.patch("agentmemhub.cli.run_ingest"), \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        r = c.post("/api/admin/ingest", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403


def test_跨站写请求被拒_sec_fetch_site():
    c = _client()
    with mock.patch("agentmemhub.cli.run_ingest"), \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        r = c.post("/api/admin/ingest",
                   headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403


def test_同源写请求放行():
    c = _client()
    with mock.patch("agentmemhub.cli.run_ingest"), \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        r = c.post("/api/admin/ingest",
                   headers={"Origin": "http://testserver",
                            "Sec-Fetch-Site": "same-origin"})
        assert r.status_code != 403


def test_无来源头的写请求放行():
    """`curl` / CLI / MCP 客户端不带 Origin —— 它们不是浏览器，没有 CSRF 面。"""
    c = _client()
    with mock.patch("agentmemhub.cli.run_ingest"), \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        r = c.post("/api/admin/ingest")
        assert r.status_code != 403


def test_读请求不受来源限制():
    """跨站 GET 只可能泄露数据、不能改状态；这里不误伤（面板自身也是 GET 拉数）。"""
    c = _client()
    r = c.get("/api/stats", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


# ----------------------------------------------------------------- SSRF
@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "10.0.0.1", "192.168.1.1", "172.16.0.1",
    "169.254.169.254",                    # 云元数据（绝大多数厂商）
    "100.100.100.200",                    # 阿里云 IMDS —— is_private 为 False
    "metadata.google.internal",           # GCP 元数据域名
    "metadata.azure.com",
    "2130706433",                         # 十进制 127.0.0.1
    "0x7f.0.0.1",                         # 十六进制点分段
    "0177.0.0.1",                         # 八进制点分段
])
def test_内网与元数据主机一律拒绝(host):
    """投喂一个内网地址 = 让服务器替调用方去访问它，正文还会被发布成页面。"""
    from agentmemhub.feed.fetch import _assert_public_url, _is_private_host
    assert _is_private_host(host) is True
    with pytest.raises(Exception):
        _assert_public_url("http://%s/latest/meta-data/" % host)


@pytest.mark.parametrize("host", ["example.com", "api.deepseek.com", "8.8.8.8"])
def test_公网主机放行(host):
    from agentmemhub.feed.fetch import _is_private_host
    assert _is_private_host(host) is False


# ------------------------------------------------------- 投喂：文件名清洗
@pytest.mark.parametrize("name,expected", [
    ("images/a.png", "images/a.png"),
    ("../../etc/passwd", "etc/passwd"),          # `..` 段被丢弃，不会越界
    ("embedded/p1/../../../../x.png", "embedded/p1/x.png"),
    ("C:/Windows/win.ini", "C_/Windows/win.ini"),
    ("a\\b\\c.png", "a/b/c.png"),
    ("./x.png", "x.png"),
])
def test_镜像文件名被清洗到目录内(name, expected):
    """PDF 内嵌图的资源名**由 PDF 作者控制**，直接拼路径就是任意写。"""
    from agentmemhub.feed.store import _safe_mirror_rel
    assert _safe_mirror_rel(name) == expected


@pytest.mark.parametrize("name", ["", "..", "../..", "/", "./."])
def test_镜像文件名全空时报错(name):
    from agentmemhub.feed.store import _safe_mirror_rel
    with pytest.raises(ValueError):
        _safe_mirror_rel(name)


# ------------------------------- 投喂：本地 HTML 的 <img src> 不许出目录
def test_本地图片不许读目录之外的文件(tmp_path):
    """投喂一个"另存的网页"，正文写 `<img src="C:/Windows/win.ini">`
    就能把任意可读文件当图片读进来 —— 必须限制在网页所在目录之内。"""
    from agentmemhub.feed.fetch import _local_image_path
    base = tmp_path / "page"
    (base / "page_files").mkdir(parents=True)
    (base / "page_files" / "1.png").write_bytes(b"png")
    secret = tmp_path / "secret.txt"
    secret.write_text("机密", encoding="utf-8")

    assert _local_image_path("page_files/1.png", base)        # 正常相对图
    assert _local_image_path("../secret.txt", base) is None   # 出界
    assert _local_image_path(str(secret), base) is None       # 绝对路径出界


# --------------------------------------- 格式白名单：空清单 = 显式禁止一切
def test_空白名单真的是禁止一切(tmp_path, monkeypatch):
    """面板"只允许这些"提交空勾选，得真的变成"什么都不能投"。

    旧实现把 `{"exts": []}` 当成"没写过"，回落到默认全集 —— 界面说成功、
    行为没变（2026-10-26 审计）。
    """
    from agentmemhub.feed import formats
    state = tmp_path / "feed_formats.json"
    monkeypatch.setenv("AGENTMEMHUB_FEED_FORMATS_STATE", str(state))

    assert formats._overrides() is None                       # 没有状态文件
    state.write_text('{"exts": []}', encoding="utf-8")
    assert formats._overrides() == []                         # 显式空清单
    assert formats.allowed_exts(config=None) == []
    assert formats.is_allowed("x.md", config=None) is False   # 有后缀：被拦
    assert formats.is_allowed("MAKEFILE", config=None) is True  # 无后缀仍放行
