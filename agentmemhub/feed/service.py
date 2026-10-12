# -*- coding: utf-8 -*-
"""投喂门面：一次投喂的完整编排（原件落盘 → 登记 → 转译 → 发布 → 审计）。

流水线（与 `docs/feed-adapter-plan.md` §4 一致）
==============================================
```
① 查重（sha256 内容锚）→ ② 原件落 raw/（received）
   → ③ 认领转换器并转译（converted）→ ④ 发布 kb/（published）
```
任一步失败都是 `failed` 且**原件保留** —— 换更好的转换器版本后可以重转，
这是"三段保留"的意义（§7.0 的 D 维度）。

为什么把编排收在一个函数里
==========================
投喂是**长流程且步骤间有依赖**（没有原件就不能转译、没转译就不能发布），
散在 CLI / MCP / 面板三处必然漂移（本项目在 wiki 上踩过这个坑：面板与 CLI
各写一份流程，后来对不齐）。所以三处入口都调 `ingest()`，各自只做参数解析与呈现。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from .base import FeedDocument, FeedError
from .store import MaterialStore, MaterialsDB, doc_id_of, sha256_of


def _cfg():
    """延迟导入配置单例（避免 import 期就建对象，便于测试注入）。"""
    from agentmemhub.config import config
    return config()


def _audit(**kw: Any) -> None:
    """写投喂事实流。**静默失败** —— 审计绝不能反过来把投喂弄失败。

    ⚠️ **`depth=4`，不是默认的 3**：`audit_feed()` 的默认假设是"业务代码**直接**调它"，
    而这里隔了一层包装（业务代码 → `_audit` → `audit_feed`）。不 +1 的话 `line` 记的是
    **包装函数内部那一行**，该流所有事件的行号恒等 ⇒ 字段等于作废。
    2026-10-04 wiki 流踩过同一坑（commit `78a6e02` 修的就是它），
    **2026-10-12 我在 feed 流又犯一次** —— 所以补了守护测试
    （`test_日志_事件行号不是恒等值`）。
    """
    try:
        from agentmemhub.logs import audit_feed
        audit_feed(kw, depth=4)
    except Exception:                       # noqa: BLE001
        pass


def _fm(key: str, val: Any) -> str:
    """frontmatter 一行。值用 JSON 编码 —— JSON 字符串是合法 YAML，
    比手工加引号安全（标题里出现 `:` `#` 引号时不会把 frontmatter 写坏）。"""
    return "%s: %s" % (key, json.dumps(str(val), ensure_ascii=False))


def _claim(*, ref: str, kind: str = "", data: bytes | None = None):
    """问一遍注册表"谁能处理"。抽出来是为了让 dry-run 与正式路径**共用同一套认领逻辑**
    —— 两处各写一遍必然漂移（dry-run 说会认领、真跑却认领不到）。"""
    from . import get_converter
    return get_converter(ref=ref, kind=kind, data=data)


def render_kb_page(doc: FeedDocument) -> str:
    """最终 md = frontmatter（溯源 + 召回两个维度） + 正文。

    frontmatter 三项都是**下游要读的**，不是装饰：
      · `origin: external` —— 召回侧据此区分自有沉淀与外部投喂；
      · `doc: <doc_id>`    —— 回指原件（`wiki_index` 会持久化进 `units.doc_id`）；
      · `doc_time`         —— 知识产生时间（不是投喂时刻），影响时间衰减。
    """
    lines = ["---", _fm("title", doc.title), "origin: external",
             _fm("doc", doc.doc_id), _fm("kind", doc.kind),
             _fm("source_ref", doc.source_ref), "doc_time: %d" % int(doc.doc_time),
             _fm("converter", doc.converter),
             "quality: %.2f" % float(doc.quality)]
    if doc.warnings:
        lines.append(_fm("warnings", "；".join(doc.warnings)))
    lines += ["---", "", doc.markdown.rstrip(), ""]
    return "\n".join(lines)


def ingest(ref: str, *, kind: str = "", force: bool = False,
           dry_run: bool = False, config=None, log=print) -> dict[str, Any]:
    """投喂一个本地文件（URL 归 M3）。返回结构化结果，**失败也返回**（不抛）。

    返回字段：`ok / skipped / doc_id / converter / kb_path / quality /
    warnings / error / error_kind / dry_run`。
    """
    cfg = config or _cfg()
    paths = cfg.feed_paths()
    res: dict[str, Any] = {
        "ref": str(ref), "ok": False, "skipped": "", "doc_id": "", "kind": kind,
        "converter": "", "kb_path": "", "raw_path": "", "title": "",
        "quality": 0.0, "warnings": [], "error": "", "error_kind": "",
        "low_quality": False, "dry_run": bool(dry_run),
    }

    def _fail(msg: str, err_kind: str, *, doc_id: str = "",
              retryable: bool = True) -> dict[str, Any]:
        res.update(ok=False, error=msg, error_kind=err_kind, doc_id=doc_id)
        _audit(event="feed_failed", kind=kind, doc_id=doc_id, error=msg,
               error_kind=err_kind, retryable=retryable, ref=str(ref))
        log("[x] 投喂失败：%s" % msg)
        return res

    # -- URL 归 M3（现在**显式报错**，不静默跳过）----------------------------
    if "://" in str(ref):
        return _fail("URL 投喂尚未实现（M3）：请先下载为本地文件再投喂",
                     "unsupported", retryable=False)
    src = Path(str(ref))
    if not src.is_file():
        return _fail("找不到文件：%s" % src, "not_found", retryable=False)

    data = src.read_bytes()
    sha = sha256_of(data)
    doc_id = doc_id_of(sha)
    res["doc_id"] = doc_id

    store = MaterialStore(root=paths["store"], kb_dir=paths["kb"])
    dry = bool(dry_run)
    # dry-run **不建库也不建目录** —— 它承诺"一个字都不写"，建库本身就是写
    db = None
    prev = None
    if not dry or Path(paths["db"]).exists():
        db = MaterialsDB(paths["db"])
        prev = db.find_by_sha(sha)
    try:
        if prev and not force:
            # 幂等：同字节已投喂过。**不是失败** —— 如实回报并附既有状态
            res.update(ok=True, skipped="duplicate", doc_id=prev["doc_id"],
                       title=prev.get("title") or "", converter=prev.get("converter") or "",
                       kb_path=prev.get("kb_path") or "", raw_path=prev.get("raw_path") or "",
                       quality=float(prev.get("quality") or 0.0),
                       error="", error_kind="")
            _audit(event="feed_skipped", kind=kind, doc_id=prev["doc_id"],
                   reason="duplicate", title=prev.get("title") or "")
            log("[=] 已投喂过（内容未变）：%s" % (prev.get("title") or prev["doc_id"]))
            return res

        if dry:
            conv = _claim(ref=str(ref), kind=kind, data=data)
            res["converter"] = conv.stamp if conv else ""
            if conv:
                res.update(ok=True, skipped="dry_run")
                log("[i] dry-run：会认领 %s（未写盘）" % conv.stamp)
            else:
                res.update(ok=False, skipped="dry_run", error_kind="unsupported",
                           error="没有转换器能处理这个输入（扩展名 %s / kind=%s）"
                                 % (src.suffix or "-", kind or "-"))
                log("[i] dry-run：没有转换器能认领 .%s（未写盘）" % src.suffix)
            return res

        # ② 原件落盘（内容寻址，天然幂等）→ 立即登记 received
        # **认领失败也要落** —— 原件是不可再生的那一份，将来补上转换器才能 retry
        sha2, raw_rel = store.put_raw(data, ext=src.suffix)
        res["raw_path"] = raw_rel
        _audit(event="feed_received", kind=kind, doc_id=doc_id, ref=str(ref),
               raw_path=raw_rel, bytes=len(data), sha256=sha2)
        log("[+] 原件入 raw：%s（%d 字节）" % (raw_rel, len(data)))

        # 认领转换器（认领在后：先保住原件再谈能不能转）
        conv = None
        try:
            conv = _claim(ref=str(ref), kind=kind, data=data)
        except Exception as e:              # noqa: BLE001 —— 注册表故障也要如实报
            db.upsert(doc_id=doc_id, sha256=sha2, kind=kind or "unknown",
                      source_ref=str(ref), title=src.stem, doc_time=0,
                      raw_path=raw_rel, status="failed",
                      error=str(e), error_kind="registry")
            return _fail("转换器注册表异常：%s" % e, "registry", doc_id=doc_id)
        if conv is None:
            msg = ("没有转换器能处理这个输入（扩展名 %s / kind=%s）—— "
                   "未知类型必须显式报错，不静默跳过" % (src.suffix or "-", kind or "-"))
            db.upsert(doc_id=doc_id, sha256=sha2, kind=kind or "unknown",
                      source_ref=str(ref), title=src.stem, doc_time=0,
                      raw_path=raw_rel, status="failed",
                      error=msg, error_kind="unsupported")
            return _fail(msg, "unsupported", doc_id=doc_id, retryable=False)
        res["converter"] = conv.stamp
        db.upsert(doc_id=doc_id, sha256=sha2, kind=kind or "text",
                  source_ref=str(ref), title=src.stem, doc_time=0,
                  raw_path=raw_rel, converter=conv.stamp, status="received")

        # ③ 转译
        meta = {"doc_id": doc_id, "mtime": int(src.stat().st_mtime)}
        try:
            doc = conv.convert(ref=str(ref), data=data, meta=meta)
        except FeedError as e:
            db.set_status(doc_id, "failed", error=str(e), error_kind=e.kind)
            return _fail(str(e), e.kind, doc_id=doc_id, retryable=e.retryable)
        except Exception as e:              # noqa: BLE001 —— 转换器崩了也算失败
            db.set_status(doc_id, "failed", error=str(e), error_kind="crash")
            return _fail("%s: %s" % (type(e).__name__, e), "crash", doc_id=doc_id)

        res.update(title=doc.title, quality=doc.quality,
                   warnings=list(doc.warnings), low_quality=doc.low_quality,
                   doc_time=int(doc.doc_time))
        _audit(event="feed_converted", kind=doc.kind, doc_id=doc_id,
               converter=doc.converter, title=doc.title, chars=len(doc.markdown),
               quality=doc.quality, warnings=list(doc.warnings))
        log("[+] 转译完成：%s（%d 字，质量 %.2f）"
            % (doc.title, len(doc.markdown), doc.quality))

        # ④ 发布最终 md（交付物 / 知识库本体）
        kb_path = store.publish_kb(doc_id, render_kb_page(doc))
        db.upsert(doc_id=doc_id, sha256=sha2, kind=doc.kind,
                  source_ref=doc.source_ref, title=doc.title,
                  doc_time=doc.doc_time, raw_path=raw_rel,
                  mirror_path="", kb_path=kb_path, converter=doc.converter,
                  quality=doc.quality, warnings=doc.warnings, status="published")
        _audit(event="feed_published", kind=doc.kind, doc_id=doc_id,
               title=doc.title, kb_path=kb_path, quality=doc.quality,
               low_quality=doc.low_quality)
        res.update(ok=True, kb_path=kb_path)
        log("[+] 已发布：%s" % kb_path)
        if doc.low_quality:
            log("    ⚠️ 质量偏低（%.2f）%s —— 可用 feed list --low-quality 复核"
                % (doc.quality, ("；" + "；".join(doc.warnings)) if doc.warnings else ""))
        return res
    finally:
        if db is not None:                  # dry-run 下没有库可关
            db.close()


# ---------------------------------------------------------------------------
# 查询与重试（CLI / MCP / 面板共用）
# ---------------------------------------------------------------------------

def list_materials(*, status: str = "", low_quality: bool = False,
                   limit: int = 0, config=None) -> list[dict[str, Any]]:
    cfg = config or _cfg()
    db = MaterialsDB(cfg.feed_paths()["db"])
    try:
        return db.list(status=status, low_quality=low_quality, limit=limit)
    finally:
        db.close()


def get_material(doc_id: str, *, config=None) -> Optional[dict[str, Any]]:
    cfg = config or _cfg()
    db = MaterialsDB(cfg.feed_paths()["db"])
    try:
        return db.get(doc_id)
    finally:
        db.close()


def retry(doc_id: str, *, force: bool = False, config=None,
          log=print) -> dict[str, Any]:
    """按登记表里的 `source_ref` 重跑一次转译（原件始终在，所以必然可重跑）。

    用途：换了转换器版本、或上次因网络/LLM 失败之后补跑。
    """
    cfg = config or _cfg()
    row = get_material(doc_id, config=cfg)
    if not row:
        return {"ok": False, "error": "没有这条素材：%s" % doc_id,
                "error_kind": "not_found"}
    ref = row.get("source_ref") or ""
    if not ref or "://" in ref or not Path(ref).is_file():
        return {"ok": False, "error": "原件来源不可达，无法重跑：%s" % ref,
                "error_kind": "not_found"}
    return ingest(ref, kind=row.get("kind") or "", force=True, config=cfg, log=log)
