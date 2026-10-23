# -*- coding: utf-8 -*-
"""`scripts/audit_frontend_api.py` 的解析回归测试。

静态扫描的漏网是**已经踩过两次**的坑：
1. 模板串 `` `/api/x/${encodeURIComponent(y)}` `` 在 `(` 处截断；
2. 字符串拼接 `` '/api/x/' + encodeURIComponent(y) `` 停在闭合引号上。

两次都是"明明在用、却被报成未用"。这里用临时文件把两种写法固定住，
不依赖仓库当下那份 `index.html` 的内容。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "audit_frontend_api", ROOT / "scripts" / "audit_frontend_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setup(tmp_path, monkeypatch, routes_src: str, html_src: str):
    mod = _load()
    app = tmp_path / "app.py"
    app.write_text(routes_src, encoding="utf-8")
    html = tmp_path / "index.html"
    html.write_text(html_src, encoding="utf-8")
    monkeypatch.setattr(mod, "APP", app)
    monkeypatch.setattr(mod, "HTML", html)
    return mod


def test_detects_template_literal_path(tmp_path, monkeypatch):
    mod = _setup(
        tmp_path, monkeypatch,
        '@app.get("/api/feed/show/{doc_id}")\ndef f(): ...\n',
        "fetch(`/api/feed/show/${encodeURIComponent(id)}`)\n",
    )
    rep = mod.audit()
    assert rep["used"] == 1 and rep["unused"] == 0


def test_detects_plus_concatenated_path(tmp_path, monkeypatch):
    """引号 + 拼接写法曾被报成未用（2026-10-23 实测：feed 详情那条）。"""
    mod = _setup(
        tmp_path, monkeypatch,
        '@app.get("/api/feed/show/{doc_id}")\ndef f(): ...\n',
        "const r = await fetch('/api/feed/show/' + encodeURIComponent(id));\n",
    )
    rep = mod.audit()
    assert rep["used"] == 1 and rep["unused"] == 0


def test_missing_segment_still_reports_unused(tmp_path, monkeypatch):
    """反向守卫：归一不能顺手把真正没接的接口放过。"""
    mod = _setup(
        tmp_path, monkeypatch,
        '@app.get("/api/feed/lint")\ndef f(): ...\n',
        "const r = await fetch('/api/feed/show/' + encodeURIComponent(id));\n",
    )
    rep = mod.audit()
    assert rep["total"] == 1 and rep["unused"] == 1


def test_extra_segment_is_not_a_match(tmp_path, monkeypatch):
    """段数必须相等：`/api/x` 不能因为前端调了 `/api/x/y` 就算接上。"""
    mod = _setup(
        tmp_path, monkeypatch,
        '@app.get("/api/wiki/pending")\ndef f(): ...\n',
        "fetch('/api/wiki/pending/drop', {method:'POST'})\n",
    )
    rep = mod.audit()
    assert rep["unused"] == 1
