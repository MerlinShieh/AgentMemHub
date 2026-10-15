# -*- coding: utf-8 -*-
"""M2 收尾：撤下 / 清除 / 确定性体检（`feed retract` / `forget` / `lint`）。

三条要守的东西：
  · **撤下可恢复** —— 交付物删掉、投影收敛，但**原件保留**（三段保留的回报：
    派生物不需要回收站，回收站反而会引出"扫描器漏排 ⇒ 真断链被掩盖"的坑）；
  · **清除要显式确认原件** —— `raw/` 是不可再生的那一份，`--purge` 才删；
  · **体检是确定性的** —— 只读、可注入索引库、P0 时非零退出码。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmemhub.feed import service
from agentmemhub.feed.lint import format_report, run_lint
from agentmemhub.feed.store import MaterialsDB


class _FakeCfg:
    """测试用配置：三段路径全在 tmp 下。"""

    def __init__(self, root: Path, *, kb=None):
        self.root = root
        self.paths = {"db": root / "materials.db",
                      "store": root / "store",
                      "kb": kb or (root / "kb")}

    def feed_paths(self):
        return dict(self.paths)

    def feed(self):
        # 空 = 全走内置默认（假开关 `convert.*` 已删，别再复刻）
        return {}

    @property
    def wiki(self):
        return {"out_l2": ""}          # 不带 L2 根，避免体检去碰真实目录


@pytest.fixture()
def cfg(tmp_path):
    return _FakeCfg(tmp_path)


def _add(tmp_path, cfg, name="笔记.md", body="# 标题\n\n正文内容。\n"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    r = service.ingest(str(p), config=cfg, log=lambda *_: None)
    assert r["ok"], r
    return p, r


# ---------------------------------------------------------------------------
# 撤下（可恢复）
# ---------------------------------------------------------------------------

def test_撤下_删交付物保原件且状态为retracted(tmp_path, cfg):
    src, r = _add(tmp_path, cfg)
    kb = Path(r["kb_path"])
    raw = cfg.paths["store"] / r["raw_path"]
    assert kb.is_file() and raw.is_file()

    out = service.retract(r["doc_id"], config=cfg, log=lambda *_: None)

    assert out["ok"] and out["kb_removed"] is True
    assert not kb.exists(), "交付物该删（它是派生物，随时能重建）"
    assert raw.is_file(), "**原件必须保留** —— 它不可再生"
    row = service.get_material(r["doc_id"], config=cfg)
    assert row["status"] == "retracted"
    # 沙箱下投影收敛被熔断跳过（真实索引库不能被测试碰）
    assert out["projection"].get("skipped") == "test_sandbox"


def test_撤下_可以重转恢复(tmp_path, cfg):
    """撤下不是单向门：`reconvert` 能从原件把它重新发布出来。"""
    src, r = _add(tmp_path, cfg)
    service.retract(r["doc_id"], config=cfg, log=lambda *_: None)
    assert not Path(r["kb_path"]).exists()

    back = service.reconvert(r["doc_id"], config=cfg, log=lambda *_: None)
    assert back["ok"] and Path(back["kb_path"]).is_file()
    assert service.get_material(r["doc_id"], config=cfg)["status"] == "published"


def test_撤下_不存在的id如实报错(cfg):
    r = service.retract("没有这个id", config=cfg, log=lambda *_: None)
    assert not r["ok"] and r["error_kind"] == "not_found"


# ---------------------------------------------------------------------------
# 清除（原件要显式确认）
# ---------------------------------------------------------------------------

def test_清除_默认保留原件(tmp_path, cfg):
    src, r = _add(tmp_path, cfg)
    raw = cfg.paths["store"] / r["raw_path"]

    out = service.forget(r["doc_id"], config=cfg, log=lambda *_: None)

    assert out["ok"] and out["raw_removed"] is False
    assert raw.is_file(), "**默认不删原件** —— 它不可再生，删了不可恢复"
    assert not Path(r["kb_path"]).exists()
    assert service.get_material(r["doc_id"], config=cfg) is None, "登记该没了"


def test_清除_purge才连原件一起删(tmp_path, cfg):
    src, r = _add(tmp_path, cfg)
    raw = cfg.paths["store"] / r["raw_path"]

    out = service.forget(r["doc_id"], purge=True, config=cfg, log=lambda *_: None)

    assert out["ok"] and out["raw_removed"] is True
    assert not raw.exists()


def test_清除_不存在的id如实报错(cfg):
    r = service.forget("没有这个id", config=cfg, log=lambda *_: None)
    assert not r["ok"] and r["error_kind"] == "not_found"


# ---------------------------------------------------------------------------
# 体检：登记 ↔ 原件 ↔ 交付物
# ---------------------------------------------------------------------------

def _kinds(res):
    return {i["kind"] for i in res["issues"]}


def test_体检_健康产物无P0(tmp_path, cfg):
    _add(tmp_path, cfg)
    res = run_lint(config=cfg, with_index=False)
    assert res["p0"] == 0, res["issues"]
    assert res["ok"] is True


def test_体检_交付物丢失报P0(tmp_path, cfg):
    src, r = _add(tmp_path, cfg)
    Path(r["kb_path"]).unlink()
    res = run_lint(config=cfg, with_index=False)
    assert "kb_missing" in _kinds(res) and res["p0"] >= 1


def test_体检_未登记的交付物报P0(tmp_path, cfg):
    _add(tmp_path, cfg)
    (cfg.paths["kb"] / "野文件.md").write_text("---\ntitle: x\n---\n\n正文\n",
                                              encoding="utf-8")
    res = run_lint(config=cfg, with_index=False)
    assert "kb_unregistered" in _kinds(res)


def test_体检_原件丢失报P0(tmp_path, cfg):
    """原件是不可再生的那一份 —— 丢了必须报，且是 P0。"""
    src, r = _add(tmp_path, cfg)
    (cfg.paths["store"] / r["raw_path"]).unlink()
    res = run_lint(config=cfg, with_index=False)
    assert "raw_missing" in _kinds(res) and res["p0"] >= 1


def test_体检_扫描件待OCR时报P1不报P0(tmp_path, cfg):
    """`needs_ocr` **不是数据不一致**（原件与页面镜像都已保住），所以不该报 P0；
    但也不该静默躺在库里没人管 —— 体检要把它顶到台面上。"""
    from pathlib import Path

    fx = Path(__file__).resolve().parent / "fixtures" / "feed" / "pdf_scanned.pdf"
    p = tmp_path / "scan.pdf"
    p.write_bytes(fx.read_bytes())
    r = service.ingest(str(p), config=cfg, log=lambda *_: None)
    assert r["error_kind"] == "needs_ocr", r.get("error")
    res = run_lint(config=cfg, with_index=False)
    assert "awaiting_ocr" in _kinds(res)
    assert res["p0"] == 0, "能力缺口不是数据不一致，不该拖垮提交门槛"


def test_体检_溯源字段错了报P0(tmp_path, cfg):
    src, r = _add(tmp_path, cfg)
    kb = Path(r["kb_path"])
    text = kb.read_text(encoding="utf-8")
    # 三种写坏方式：doc 对不上、origin 不对、必填字段缺失
    kb.write_text(text.replace('doc: "%s"' % r["doc_id"], 'doc: "别人"',
                              ), encoding="utf-8")
    assert "doc_mismatch" in _kinds(run_lint(config=cfg, with_index=False))

    kb.write_text(text.replace("origin: external", "origin: native"),
                  encoding="utf-8")
    assert "origin_wrong" in _kinds(run_lint(config=cfg, with_index=False))

    kb.write_text("---\ntitle: \"只有标题\"\n---\n\n正文\n", encoding="utf-8")
    assert "fm_missing" in _kinds(run_lint(config=cfg, with_index=False))


def test_体检_低质量进P1而不是P0(tmp_path, cfg):
    """质量偏低**不该拦发布**（§7.5：打标 + 可见，判断权留给人）。"""
    from agentmemhub.feed.base import FeedDocument
    from agentmemhub.feed.service import render_kb_page
    src, r = _add(tmp_path, cfg)
    # 手工把登记改成低质量 + 有告警（模拟 T3b 那种"表格可能失真"）
    db = MaterialsDB(cfg.paths["db"])
    try:
        row = db.get(r["doc_id"])
        db.upsert(doc_id=row["doc_id"], sha256=row["sha256"], kind=row["kind"],
                  source_ref=row["source_ref"], title=row["title"],
                  doc_time=row["doc_time"], raw_path=row["raw_path"],
                  kb_path=row["kb_path"], converter=row["converter"],
                  quality=0.4, warnings=["表格可能失真"], status="published")
    finally:
        db.close()
    res = run_lint(config=cfg, with_index=False)
    assert "low_quality" in _kinds(res)
    assert all(i["level"] == "P1" for i in res["issues"]
               if i["kind"] == "low_quality"), "低质量是 P1，不该是 P0"


# ---------------------------------------------------------------------------
# 体检：投影（可注入索引库）
# ---------------------------------------------------------------------------

@pytest.fixture()
def idx_db(tmp_path):
    from agentmemhub.rag.ingest import ensure_bridge_schema, open_index
    conn = open_index(tmp_path / "idx.db")
    ensure_bridge_schema(conn)
    conn.commit()
    return tmp_path / "idx.db", conn


def _add_unit(conn, *, doc_id, wiki_path, origin="external"):
    conn.execute(
        "INSERT INTO units(source, conversation_id, seq, role, src_id, time,"
        " title, text, chars, wiki_path, origin, doc_id, updated_at)"
        " VALUES('wiki','01-域A',-1,'wiki',?,0,'t','text',4,?,?,?,0)",
        ("wiki_%s" % doc_id, str(wiki_path), origin, doc_id))
    conn.commit()


def test_体检_投影残留报P0(tmp_path, cfg, idx_db):
    """**撤下/删除没清干净** ⇒ 表现为"删了还能搜到"（本项目踩过这一类）。"""
    path, conn = idx_db
    _add_unit(conn, doc_id="幽灵", wiki_path=tmp_path / "不存在.md")
    res = run_lint(config=cfg, with_index=True, index_db=path)
    kinds = _kinds(res)
    assert "projection_orphan" in kinds, "登记里没有它却还在投影里"
    assert "projection_stale" in kinds, "投影指向的文件不存在"


def test_体检_投影origin错了报P0(tmp_path, cfg, idx_db):
    src, r = _add(tmp_path, cfg)
    path, conn = idx_db
    _add_unit(conn, doc_id=r["doc_id"], wiki_path=Path(r["kb_path"]),
              origin="native")
    res = run_lint(config=cfg, with_index=True, index_db=path)
    assert "projection_origin" in _kinds(res)


def test_体检_投影健康则无P0(tmp_path, cfg, idx_db):
    src, r = _add(tmp_path, cfg)
    path, conn = idx_db
    _add_unit(conn, doc_id=r["doc_id"], wiki_path=Path(r["kb_path"]))
    res = run_lint(config=cfg, with_index=True, index_db=path)
    assert res["p0"] == 0, res["issues"]


# ---------------------------------------------------------------------------
# 体检：配置级 + 呈现
# ---------------------------------------------------------------------------

def test_体检_页面根嵌套报P0(tmp_path):
    """两根嵌套 ⇒ 一方的整域替换会毁掉另一方（结构性风险，必须在体检里拦）。"""
    kb = tmp_path / "out_l2_mimo" / "out_feed"       # **嵌在 L2 里面**

    class _C(_FakeCfg):
        @property
        def wiki(self):
            return {"out_l2": str(tmp_path / "out_l2_mimo")}

    res = run_lint(config=_C(tmp_path, kb=kb), with_index=False)
    assert "roots_nested" in _kinds(res), "嵌套的页面根必须报 P0"


def test_体检_报告可读且P0提示退出码(tmp_path, cfg):
    _add(tmp_path, cfg)
    Path([i for i in service.list_materials(config=cfg)][0]["kb_path"]).unlink()
    res = run_lint(config=cfg, with_index=False)
    txt = format_report(res)
    assert "P0" in txt and "退出码 1" in txt
    # JSON 可序列化（CLI --json 用）
    assert json.loads(json.dumps(res, ensure_ascii=False))["p0"] >= 1
