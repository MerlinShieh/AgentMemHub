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
import os
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

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


def _fetch_timeout(cfg) -> float:
    """取回超时（配置 `feed.fetch.timeout`，默认 20 秒）。

    短超时是刻意的：一个慢站点不该拖死整批投喂（批量场景下一个卡住的请求
    会让整轮看起来"卡死"）。
    """
    try:
        feed_cfg = getattr(cfg, "feed", None)
        if callable(feed_cfg):              # 测试替身可能把它写成方法
            feed_cfg = feed_cfg()
        return float((feed_cfg or {}).get("fetch", {}).get("timeout") or 20)
    except Exception:                       # noqa: BLE001 —— 配置读不到就用默认
        return 20.0


def _image_writer(store, doc_id: str, kb_dir):
    """返回"存一张图、并给出**相对 kb 页的引用路径**"的闭包。

    为什么给相对路径而不是绝对：交付物是给人读的文件（可能被 Obsidian 打开），
    库内相对路径既可移植、又能正确渲染 —— 绝对路径换台机器/换个目录就断。
    """
    def _put(url: str, blob: bytes) -> str:
        rel = store.put_image(doc_id, url, blob)
        return os.path.relpath(store.root / rel, kb_dir).replace("\\", "/")
    return _put


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
           dry_run: bool = False, config=None, log=print,
           data_override: Optional[bytes] = None) -> dict[str, Any]:
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

    # -- 取字节：本地文件 / URL 抓取 / 调用方直接给字节（重转从 raw/ 读）
    src: Optional[Path] = None
    fetched = None
    is_url = "://" in str(ref)
    if is_url and not kind:
        # 自动判型（URL 不说 kind 也能跑）；`mp.weixin.qq.com` 走特化转换器
        kind = "wechat" if "mp.weixin.qq.com" in str(ref).lower() else "url"
        res["kind"] = kind                  # ⚠️ res 在判型**之前**就建好了，这里要回填
    if data_override is not None:
        data = data_override
        # 重转时来源可能已不在（用户删了下载目录 / URL 也是）—— 扩展名/标题回落到 ref
        src = Path(str(ref))
    elif is_url:
        from . import fetch as _fetch
        ua = (_fetch.WECHAT_UA if "mp.weixin.qq.com" in str(ref).lower()
              else _fetch.DEFAULT_UA)
        try:
            fetched = _fetch.fetch(
                str(ref), ua=ua, timeout=_fetch_timeout(cfg))
        except FeedError as e:
            return _fail(str(e), e.kind, retryable=e.retryable)
        if fetched.status != 200:
            return _fail("取回失败：HTTP %s" % fetched.status, "fetch",
                         retryable=fetched.status >= 500 or fetched.status == 429)
        data = fetched.body
        res["http_status"] = fetched.status
        ref = fetched.url                  # 跟随重定向后的**最终 URL**（溯源用这个）
        res["ref"] = str(ref)
    else:
        src = Path(str(ref))
        if not src.is_file():
            return _fail("找不到文件：%s" % src, "not_found", retryable=False)
        data = src.read_bytes()

    # 扩展名 / 文件名 / mtime 统一取"来源可用就用来源，否则用 ref 的形态"
    url_path = Path(urlparse(str(ref)).path)
    suffix = (src.suffix if src and src.suffix else url_path.suffix)
    stem = (src.stem if src and src.stem else url_path.stem) or "document"
    mtime = int(src.stat().st_mtime) if (src and src.is_file()) else 0

    sha = sha256_of(data)
    doc_id = doc_id_of(sha)
    res["doc_id"] = doc_id

    store = MaterialStore(root=paths["store"], kb_dir=paths["kb"])
    dry = bool(dry_run)
    # dry-run **不建库也不建目录** —— 它承诺"一个字都不写"，建库本身就是写
    db = None
    prev = None
    prev_url = None
    if not dry or Path(paths["db"]).exists():
        db = MaterialsDB(paths["db"])
        # **URL 的身份是"最终 URL"，不是内容 hash**：动态页面字节每次都变
        # （时间戳 / CSRF / 广告位）—— 实测同一篇公众号文章两次抓取 sha 不同，
        # 按内容判会"每投喂一次多一条素材"。所以同 URL 一律视为**原地更新**
        # （复用 doc_id），内容 hash 只用来判断"这次字节有没有变"。
        if is_url:
            prev_url = db.find_by_source(str(ref))
        prev = None if prev_url else db.find_by_sha(sha)
    if prev_url is not None:
        doc_id = prev_url["doc_id"]
        res["doc_id"] = doc_id
        res["updated_existing"] = True
    try:
        if prev_url is not None and prev_url.get("sha256") == sha:
            # 同 URL 且字节没变 ⇒ 真幂等
            res.update(ok=True, skipped="duplicate", doc_id=prev_url["doc_id"],
                       title=prev_url.get("title") or "",
                       converter=prev_url.get("converter") or "",
                       kb_path=prev_url.get("kb_path") or "",
                       raw_path=prev_url.get("raw_path") or "",
                       quality=float(prev_url.get("quality") or 0.0),
                       error="", error_kind="")
            _audit(event="feed_skipped", kind=kind, doc_id=prev_url["doc_id"],
                   reason="duplicate", title=prev_url.get("title") or "")
            log("[=] 已投喂过（内容未变）：%s"
                % (prev_url.get("title") or prev_url["doc_id"]))
            return res
        if prev_url is not None:
            log("[i] 同 URL 已存在 ⇒ 原地更新（doc_id=%s 不变）" % doc_id)
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
                                 % (suffix or "-", kind or "-"))
                log("[i] dry-run：没有转换器能认领 %s（未写盘）" % (suffix or "-"))
            return res

        # ② 原件落盘（内容寻址，天然幂等）→ 立即登记 received
        # **认领失败也要落** —— 原件是不可再生的那一份，将来补上转换器才能 retry
        sha2, raw_rel = store.put_raw(data, ext=suffix)
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
                      source_ref=str(ref), title=stem, doc_time=0,
                      raw_path=raw_rel, status="failed",
                      error=str(e), error_kind="registry")
            return _fail("转换器注册表异常：%s" % e, "registry", doc_id=doc_id)
        if conv is None:
            msg = ("没有转换器能处理这个输入（扩展名 %s / kind=%s）—— "
                   "未知类型必须显式报错，不静默跳过" % (suffix or "-", kind or "-"))
            db.upsert(doc_id=doc_id, sha256=sha2, kind=kind or "unknown",
                      source_ref=str(ref), title=stem, doc_time=0,
                      raw_path=raw_rel, status="failed",
                      error=msg, error_kind="unsupported")
            return _fail(msg, "unsupported", doc_id=doc_id, retryable=False)
        res["converter"] = conv.stamp
        db.upsert(doc_id=doc_id, sha256=sha2, kind=kind or "text",
                  source_ref=str(ref), title=stem, doc_time=0,
                  raw_path=raw_rel, converter=conv.stamp, status="received")

        # ③ 转译
        meta = {"doc_id": doc_id, "mtime": mtime, "now": int(time.time()),
                "url": str(ref) if is_url else "",
                "headers": (fetched.headers if fetched else {}),
                # 第 2 段物料的两个写入口（转换器**不碰存储**，只拿回调 —— 便于测试替身）
                "put_mirror": lambda name, b: store.put_mirror(doc_id, name, b),
                "put_image": _image_writer(store, doc_id, paths["kb"])}
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
                   doc_time=int(doc.doc_time), kind=doc.kind)
        # `kind` 必须回填成**转换器判定的类型**，不是调用方传进来的那个：
        # 本地文件传进来时 kind 是空串（靠扩展名/魔数判型），
        # 不回填的话返回值里 `kind=""`、库里却是 `pdf` —— 同一件事两个答案。
        # 2026-10-14 被 `test_端到端_文字版PDF_出kb页且保住页码` 逼出来。
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


def _reproject(config, log=print) -> dict[str, Any]:
    """让投影收敛（撤下 / 清除之后）。

    **复用 `project_pages` 的全量对齐 + 清扫，而不是另写一份"按 doc_id 删投影"的逻辑**：
    删除要同时处理向量行、价值行、FTS 与"只清本轮扫过的根"那套规则 —— 另写一份必然
    与投影路径漂移（本项目在"两处各写一份"上踩过多次）。代价是重算一次页面向量
    （几百页、秒级），对"撤下"这种低频人工操作完全可接受。

    ⚠️ **测试沙箱下直接跳过**（`AGENTMEMHUB_TEST_SANDBOX`）：它打开的是**真实索引库**。
    本项目 2026-09-26 的事故就是"测试通过一条真实路径改了真实产物"，所以凡碰真实路径的
    出口都必须有进程级熔断（见 AGENTS.md 测试纪律）。测试要验证收敛请自行注入替身。
    """
    try:
        from agentmemhub.logs import SANDBOX_ENV
        if os.environ.get(SANDBOX_ENV):
            log("  （测试沙箱：跳过投影收敛）")
            return {"skipped": "test_sandbox"}
    except Exception:                       # noqa: BLE001 —— 判不出来就当不在沙箱
        pass
    try:
        from agentmemhub import wiki_index
        from agentmemhub.rag.ingest import open_index
        from agentmemhub.rag.config import load_settings

        l2 = (config.wiki.get("out_l2") or "")
        roots = wiki_index.page_roots(l2, config=config)
        conn = open_index(load_settings().index_db)
        try:
            return wiki_index.project_pages(conn, roots, log=lambda *_: None)
        finally:
            conn.close()
    except Exception as e:                  # noqa: BLE001 —— 索引库不可用不该让撤下失败
        log("  ⚠️ 投影收敛失败（交付物与登记已处理）：%s: %s"
            % (type(e).__name__, str(e)[:160]))
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}


def retract(doc_id: str, *, config=None, log=print) -> dict[str, Any]:
    """**撤下**投喂内容（可恢复）：删交付物 + 收敛投影，**原件保留**。

    为什么交付物可以直接删、不需要回收站：kb 是**派生物** ——
    `raw/` 的原件还在，`reconvert` 随时能重建。这正是"三段保留"的回报：
    **派生物不需要回收站**，而回收站恰恰会引出"扫描器漏排回收站 ⇒ 真断链被掩盖"
    这类双向失效的坑（参考实现 `lint.py` 踩过，见计划 §11.4）。
    """
    cfg = config or _cfg()
    row = get_material(doc_id, config=cfg)
    if not row:
        return {"ok": False, "error": "没有这条素材：%s" % doc_id,
                "error_kind": "not_found"}
    paths = cfg.feed_paths()
    store = MaterialStore(root=paths["store"], kb_dir=paths["kb"])
    kb = store.kb_file(doc_id)
    removed_kb = kb.is_file()
    if removed_kb:
        kb.unlink()
    db = MaterialsDB(paths["db"])
    try:
        db.set_status(doc_id, "retracted")
    finally:
        db.close()
    _audit(event="feed_retracted", doc_id=doc_id, kb_removed=removed_kb,
           title=row.get("title") or "", raw_kept=True)
    log("[+] 已撤下：%s（交付物%s；原件保留，可 retry 恢复）"
        % ((row.get("title") or doc_id), "已删" if removed_kb else "本就不存在"))
    proj = _reproject(cfg, log=log)
    return {"ok": True, "doc_id": doc_id, "kb_removed": removed_kb,
            "raw_kept": True, "projection": proj}


def forget(doc_id: str, *, purge: bool = False, config=None,
           log=print) -> dict[str, Any]:
    """**彻底清除**登记（+ 交付物 + 投影）。**原件默认保留**，`purge=True` 才删。

    为什么原件要单独确认：`raw/` 那份是**不可再生**的，而交付物与投影都能从它重来。
    默认留下原件 ⇒ "忘掉它"仍然可逆（重新 `add` 或 `reconvert` 即可）。
    """
    cfg = config or _cfg()
    row = get_material(doc_id, config=cfg)
    if not row:
        return {"ok": False, "error": "没有这条素材：%s" % doc_id,
                "error_kind": "not_found"}
    paths = cfg.feed_paths()
    store = MaterialStore(root=paths["store"], kb_dir=paths["kb"])
    kb = store.kb_file(doc_id)
    removed_kb = kb.is_file()
    if removed_kb:
        kb.unlink()
    removed_raw = False
    raw_rel = row.get("raw_path") or ""
    if purge and raw_rel:
        raw = store.raw_path(raw_rel)
        if raw.is_file():
            raw.unlink()
            removed_raw = True
    db = MaterialsDB(paths["db"])
    try:
        db.delete(doc_id)
    finally:
        db.close()
    _audit(event="feed_forgot", doc_id=doc_id, kb_removed=removed_kb,
           raw_removed=removed_raw, title=row.get("title") or "")
    log("[+] 已清除登记：%s（交付物%s；原件%s）"
        % ((row.get("title") or doc_id), "已删" if removed_kb else "无",
           "已删（purge）" if removed_raw else "保留"))
    proj = _reproject(cfg, log=log)
    return {"ok": True, "doc_id": doc_id, "kb_removed": removed_kb,
            "raw_removed": removed_raw, "projection": proj}


def lint(*, config=None, with_index: bool = True,
         index_db=None) -> dict[str, Any]:
    """确定性产物体检（见 `feed/lint.py`）。"""
    from .lint import run_lint
    return run_lint(config=config, with_index=with_index, index_db=index_db)


def ingest_text(text: str, *, title: str = "", source_ref: str = "",
                config=None, log=print) -> dict[str, Any]:
    """**兜底通路**：直接投喂一段文本（浏览器另存失败 / 反爬 / 只拿到正文时用）。

    为什么必须有它：反爬是军备竞赛，直抓失败时若没有兜底，整条链路就废了
    （计划 §7.6 把它列为一等公民而非补丁）。实测公众号就出现过"不带浏览器 UA
    只拿到 18 KB 桩页"——那种情况下用户手动粘正文是最可靠的路。

    实现上复用 `ingest(data_override=...)`：`ref` 只是一个**伪路径**（用于派生扩展名
    与兜底标题，不参与读取），真正的人读来源放 `source_ref`（若给了就作为溯源值）。
    """
    body = str(text or "")
    if not body.strip():
        return {"ok": False, "error": "stdin 是空的，没有可投喂的内容",
                "error_kind": "empty", "ref": source_ref or "stdin"}
    ref = source_ref if source_ref else "stdin:%s.md" % (title or "pasted")
    payload = body
    if title and "title:" not in body[:200]:
        # 用 frontmatter 传标题（`TextConverter` 的既有取值顺序：frontmatter > H1 > 文件名）
        payload = "---\ntitle: %s\n---\n\n%s" % (title, body)
    return ingest(ref, kind="text", config=config, log=log,
                  data_override=payload.encode("utf-8"))


def reconvert(doc_id: str, *, kind: str = "", config=None,
              log=print) -> dict[str, Any]:
    """**用 `raw/` 里的原件重跑转译** —— 不依赖原始来源是否还在。

    为什么必须有这条路径：三段保留的全部意义就是"原件不可再生、其余都可重建"。
    若重转只能靠 `source_ref`（用户的下载目录），用户一清理文件就再也转不了
    ——**保留下来的原件等于白存**（2026-10-12 核对投喂数据时发现该缺口）。

    溯源不受影响：`source_ref` 仍用登记表里的原值（**不**改成 raw 路径）。
    """
    cfg = config or _cfg()
    paths = cfg.feed_paths()
    row = get_material(doc_id, config=cfg)
    if not row:
        return {"ok": False, "error": "没有这条素材：%s" % doc_id,
                "error_kind": "not_found"}
    raw_rel = row.get("raw_path") or ""
    store = MaterialStore(root=paths["store"], kb_dir=paths["kb"])
    raw = store.raw_path(raw_rel) if raw_rel else None
    if raw is None or not raw.is_file():
        return {"ok": False, "error": "原件不在 raw/ 里，无法重转：%s" % raw_rel,
                "error_kind": "not_found"}
    ref = row.get("source_ref") or str(raw)
    app_kind = kind or row.get("kind") or ""
    if app_kind == "unknown":
        app_kind = ""
    return ingest(ref, kind=app_kind, force=True, config=cfg, log=log,
                  data_override=raw.read_bytes())


def retry(doc_id: str, *, force: bool = False, config=None,
          log=print) -> dict[str, Any]:
    """重跑一次转译：**优先用原始来源，来源不可达时回落到 `raw/` 原件**。

    用途：换了转换器版本、或上次转译失败之后补跑。
    """
    cfg = config or _cfg()
    row = get_material(doc_id, config=cfg)
    if not row:
        return {"ok": False, "error": "没有这条素材：%s" % doc_id,
                "error_kind": "not_found"}
    ref = row.get("source_ref") or ""
    if ref and "://" not in ref and Path(ref).is_file():
        kind = row.get("kind") or ""
        return ingest(ref, kind="" if kind == "unknown" else kind, force=True,
                      config=cfg, log=log)
    log("[i] 原始来源不可达（%s）—— 改用 raw/ 里的原件重转" % (ref or "（空）"))
    return reconvert(doc_id, config=cfg, log=log)
