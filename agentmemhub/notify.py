# -*- coding: utf-8 -*-
"""全局通知：给「需要人看见的事」一个落点。

## 与日志的分工

`logs/` 是**事实流水**（发生过什么，append-only，写完不再变）；通知是**待办队列**
（有已读/未读、有生命周期、要被查询与处理）。两者都要有：一次批量提交失败，既要
进 `logs/wiki.log` 供事后排查，也要进通知让**人**知道。

## 为什么是 SQLite 而不是队列

单机应用，没有多消费者、没有削峰需求 —— 队列是过度设计。一张表足够：按等级 /
类别 / 未读查询、标记已读、按天清理。**解耦**体现在调用方只
`from agentmemhub.notify import notify`，不关心存储、降级与落盘细节。

## 绝不阻塞主流程

通知是旁路：落库失败**降级**写 `logs/notify_fallback.jsonl`，函数**永不抛**。
一次 wiki 更新不该因为"通知写不进去"而失败。

## 分级

`level` ∈ `debug` / `info` / `warning` / `error`（沿用需求里的写法；写进
`logs/notify.log` 时由 `logs.audit_notify` 映射成日志流的 `warn`）；
`category` 常见值 `batch` / `wiki` / `trigger` / `config`。两者都不做白名单拒绝
—— 只做取值校验与回落（非法 level 回落 `info`）。**通知丢一条比分类不标准更糟**。
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

#: 通知等级（用户语义；日志流用 `warn`，映射在 `logs.audit_notify`）
LEVELS = ("debug", "info", "warning", "error")

#: 常见来源类别（不做白名单限制，供调用方参考与筛选）
CATEGORIES = ("batch", "wiki", "trigger", "config")

#: 清理时默认保留天数（与日志段的 `keep_days` 同思路）
DEFAULT_KEEP_DAYS = 30

DB_NAME = "notify.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL    NOT NULL,
  level    TEXT    NOT NULL,
  category TEXT    NOT NULL,
  title    TEXT    NOT NULL,
  body     TEXT    NOT NULL DEFAULT '',
  meta     TEXT    NOT NULL DEFAULT '{}',
  read_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_notify_ts ON notifications (ts DESC);
"""


def db_path() -> Path:
    """通知库位置：`<data_dir>/notify.db`。

    放 data_dir 是有意的：它随数据目录一起被快照/备份覆盖，且**与源库、索引库
    同处一地**，运维时不必记第二个位置。
    """
    from agentmemhub import config as hub_config
    return Path(hub_config.config().data_dir) / DB_NAME


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)      # 幂等：每次连接都确保表在（首次使用免初始化）
    return conn


@contextmanager
def _db(path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """短连接 + 提交 + **必定关闭**。

    注意：`with sqlite3.connect(...)` 只管事务、**不关连接**，直接那样写会漏 fd。
    """
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fallback(row: dict, err: Exception) -> None:
    """落库失败时的兜底：追加 JSONL；**它再失败也只能咽下去**。"""
    try:
        from agentmemhub.logs import log_dir
        p = Path(log_dir()) / "notify_fallback.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**row, "fallback_error": str(err)[:200]},
                               ensure_ascii=False) + "\n")
    except Exception:                    # noqa: BLE001
        pass


def _audit(row: dict, ok: bool) -> None:
    try:
        from agentmemhub.logs import audit_notify
        audit_notify({"event": "notify_write", "ok": ok, "level": row["level"],
                      "category": row["category"], "title": row["title"]})
    except Exception:                    # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 推送通道（可选的外部投递；落库始终启用）
# ---------------------------------------------------------------------------

#: Pushplus 的发送端点（微信推送服务，用户自己的 token）。
PUSHPLUS_URL = "https://www.pushplus.plus/send"


def _notify_cfg() -> dict[str, Any]:
    """`notify` 段配置（**每次直接读盘**，不走 `config()` 的进程级单例）。

    为什么破例：推送 token 属于"用户改完就想立刻生效"的配置，而 `config()` 是
    **进程级单例**（yaml 只在 `Config.__init__` 读一次）—— 长驻的 MCP 进程改了 yaml
    看不到，为填个 token 重启 DSH 不值当。通知本身是低频操作（每轮几次），
    读一个小 yaml 的成本可以忽略。
    """
    try:
        from agentmemhub.config import Config
        return dict(Config().notify or {})
    except Exception:                       # noqa: BLE001 —— 读不到就当没开
        return {}


def dedup_window_s() -> float:
    """去重窗口（秒）：同一 `(category, title)` 在此窗口内只留一条。0 = 不去重。"""
    try:
        return max(0.0, float(_notify_cfg().get("dedup_window_s") or 0))
    except (TypeError, ValueError):
        return 0.0


def pushplus_send(*, token: str, title: str, content: str, topic: str = "",
                  template: str = "markdown",
                  timeout: float = 5.0) -> dict[str, Any]:
    """推一条到 Pushplus（微信）。**永不抛** —— 推送是旁路。

    只用标准库 `urllib`（不引入新依赖）；返回 `{"ok", "code", "msg"}`，
    失败时带 `error`。Pushplus 的成功判定是响应体里的 `code == 200`。
    """
    payload: dict[str, Any] = {"token": token, "title": title[:100],
                               "content": content, "template": template}
    if topic:
        payload["topic"] = topic
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_URL, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception as e:                  # noqa: BLE001 —— 网络/超时都算旁路失败
        return {"ok": False, "code": None,
                "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    try:
        d = json.loads(txt or "{}")
    except Exception:                       # noqa: BLE001
        return {"ok": False, "code": None, "error": "非 JSON 响应：%s" % txt[:120]}
    try:
        code = int(d.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    return {"ok": code == 200, "code": code, "msg": str(d.get("msg") or "")[:200]}


#: `wiki_trigger` 这个开关覆盖的类别 —— 它们都算"wiki 更新相关的事件"。
WIKI_CATEGORIES = ("wiki", "batch", "trigger")

#: 各等级的 emoji（渲染用；也让"这条有多要紧"在微信里一眼可见）。
_LEVEL_EMOJI = {"debug": "·", "info": "ℹ️", "warning": "⚠️", "error": "❌"}


# ---------------------------------------------------------------------------
# 推送通道：**可插拔**
#
# 一个通道就是一个函数：吃"一条通知 + 它自己那段配置"，返回 `{"ok": bool, ...}`。
# 加渠道 = 写一个这样的函数 + `register_channel()` —— **不动核心逻辑**：
# 落库、推送策略（`push_when`）、去重、审计全都是共用的。
# ---------------------------------------------------------------------------

#: 通道函数签名（一律**关键字调用**）：
#:     fn(*, level, category, title, body, meta, cfg) -> dict
#: `cfg` 是 `notify.<通道名>` 那一段配置（缺省传空 dict）—— 所以**渠道自己决定
#: 要哪些配置键**，核心不必认识它们（加渠道不需要改 `config.py` 的白名单）。
PushChannel = Callable[..., dict[str, Any]]

#: 已注册的通道（`name → 函数`）。**这就是"可插拔"的全部机制。**
_CHANNELS: dict[str, PushChannel] = {}


def register_channel(name: str, fn: PushChannel) -> None:
    """注册 / 替换一个推送通道。**加新渠道的唯一入口**（同名覆盖，便于测试替换）。

    见 `_channel_pushplus()` —— 它就是"怎么写一个渠道"的参照实现。
    """
    _CHANNELS[str(name or "").strip().lower()] = fn


def channels() -> list[str]:
    """已注册的通道名（排查"消息为什么没出去"时先看这个）。"""
    return sorted(_CHANNELS)


def render_markdown(level: str, title: str, body: str = "",
                    meta: Optional[dict[str, Any]] = None) -> str:
    """把一条通知渲染成 markdown 正文（**通道可复用**，也可自行渲染）。"""
    md = "**%s %s**\n\n%s" % (_LEVEL_EMOJI.get(str(level or "info").lower(), "ℹ️"),
                              title, body or "")
    if meta:
        md += "\n\n---\n" + "\n".join("- %s：%s" % (k, v) for k, v in meta.items())
    return md


def render_title(level: str, title: str) -> str:
    """带等级 emoji 的标题（通道可复用）。"""
    return "%s %s" % (_LEVEL_EMOJI.get(str(level or "info").lower(), "ℹ️"), title)


def should_push(level: str, category: str) -> bool:
    """按**推送策略**判断这条通知要不要推到微信。**落库不受它影响**（始终全留）。

    两类开关是 **OR**（任一命中即推）：
      · 按**类别**：`memory_write`（category = `memory`）/ `wiki_trigger`
        （category ∈ `WIKI_CATEGORIES`）
      · 按**等级**：`info` / `warning` / `error`

    为什么用 OR 而不是"类别 AND 等级"：两者的关注点不同 —— "我只想看警告以上"
    与"wiki 的动作我都要知道"应当能并存，用 OR 表达最自然，也不必去枚举
    "等级 × 类别"的组合。
    """
    pol = dict(_notify_cfg().get("push_when") or {})
    lv = str(level or "info").strip().lower()
    cat = str(category or "general").strip().lower()
    if cat == "memory" and pol.get("memory_write"):
        return True
    if cat in WIKI_CATEGORIES and pol.get("wiki_trigger"):
        return True
    return bool(lv in ("info", "warning", "error") and pol.get(lv))


def _channel_pushplus(*, level: str, category: str, title: str, body: str,
                      meta: dict, cfg: dict) -> dict[str, Any]:
    """**Pushplus 通道**（微信推送）。配置段：`notify.pushplus`。

    它是"怎么加一个渠道"的**参照实现**，只做三件事：
      ① 自己判断配置够不够（不够就 `{"skipped": …}` —— 这**不是失败**，只是本轮不发）；
      ② 把通知渲染成渠道要的形状（这里复用 `render_markdown`）；
      ③ 发出去、如实返回结果（`ok` / `code` / `error`）。
    """
    if not cfg.get("enabled"):
        return {"ok": False, "skipped": "disabled"}
    token = str(cfg.get("token") or "").strip()
    if not token:
        return {"ok": False, "skipped": "no_token"}
    try:
        timeout = float(cfg.get("timeout") or 5)
    except (TypeError, ValueError):
        timeout = 5.0
    return pushplus_send(token=token, title=render_title(level, title),
                         content=render_markdown(level, title, body, meta),
                         topic=str(cfg.get("topic") or ""), timeout=timeout)


register_channel("pushplus", _channel_pushplus)


def push(level: str, category: str, title: str, body: str = "", *,
         meta: Optional[dict[str, Any]] = None,
         channel: str = "") -> dict[str, dict[str, Any]]:
    """把一条通知推到**所有已注册的通道**。返回 `{通道名: 结果}`，**永不抛**。

    `channel` 指定则只推那一个（联调单个渠道时用）。每个通道的配置取自
    `notify.<通道名>` 段；通道返回的 `{"ok": False, "skipped": …}`（没开 / 没配）
    **不算失败**，只是"这个渠道本轮没发"。

    **去重不在这里**：统一放在 `notify()` 那层（见 `dedup_window_s()`），
    否则"落库被去重、推送却漏网"会在两处各算一套。
    """
    cfg_all = _notify_cfg()
    names = [str(channel).strip().lower()] if channel else channels()
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        fn = _CHANNELS.get(name)
        if fn is None:
            out[name] = {"ok": False, "skipped": "unknown_channel"}
            continue
        try:
            res = fn(level=level, category=category, title=title, body=body,
                     meta=dict(meta or {}), cfg=dict(cfg_all.get(name) or {})) or {}
        except Exception as e:              # noqa: BLE001 —— 一个通道炸了不能牵连其它
            res = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
        out[name] = dict(res)
        # **一个通道一条审计**：排查"哪个渠道没出去、为什么"必须分得开。
        try:
            from agentmemhub.logs import audit_notify
            audit_notify({"event": "notify_push", "ok": bool(res.get("ok")),
                          "channel": name, "level": level, "category": category,
                          "title": title,
                          "error": (res.get("error") or res.get("skipped")
                                    or res.get("msg") or "")})
        except Exception:                   # noqa: BLE001
            pass
    return out


def _deduped(category: str, title: str, ts: float,
             db: Optional[str]) -> bool:
    """窗口内是否已有同名通知（**只读**；库/表不存在 ⇒ 必然不是重复）。"""
    win = dedup_window_s()
    if win <= 0:
        return False
    try:
        with _db(db) as conn:
            row = conn.execute(
                "SELECT ts FROM notifications WHERE category = ? AND title = ?"
                " ORDER BY ts DESC LIMIT 1", (category, title)).fetchone()
        return bool(row) and (ts - float(row[0])) < win
    except Exception:                       # noqa: BLE001
        return False


def notify(level: str, category: str, title: str, body: str = "", *,
           meta: Optional[dict[str, Any]] = None, db: Optional[str] = None,
           ts: Optional[float] = None, push_too: bool = True) -> bool:
    """写一条通知（并在开启时推到微信）。返回是否**被接受**，**绝不抛**。

    · **去重**：同一 `(category, title)` 在 `dedup_window_s` 内只留一条 ——
      命中时返回 `True`（"这条已被代表"）并记 `deduped` 审计，**不是失败**；
    · **推送**：落库成功后才推（`push_too=False` 可只落库）；
      推送失败**不影响**返回值 —— 它的结果在 `notify_push` 审计与 `NOTIFY_PUSH` 里。
    """
    lv = str(level or "info").strip().lower()
    if lv not in LEVELS:
        lv = "info"
    cat = str(category or "general").strip().lower()
    ttl = str(title or "")[:200]
    _ts = float(ts if ts is not None else time.time())
    row = {"ts": _ts, "level": lv, "category": cat, "title": ttl,
           "body": str(body or "")[:4000],
           "meta": dict(meta or {})}
    if _deduped(cat, ttl, _ts, db):
        try:
            from agentmemhub.logs import audit_notify
            audit_notify({"event": "notify_write", "ok": True, "deduped": True,
                          "level": lv, "category": cat, "title": ttl})
        except Exception:                   # noqa: BLE001
            pass
        return True
    ok = False
    try:
        with _db(db) as conn:
            conn.execute(
                "INSERT INTO notifications (ts, level, category, title, body, meta)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (row["ts"], row["level"], row["category"], row["title"],
                 row["body"], json.dumps(row["meta"], ensure_ascii=False)))
        ok = True
    except Exception as e:               # noqa: BLE001 —— 旁路，永不抛
        _fallback(row, e)
    _audit(row, ok)
    # 推送要过**策略**（哪些事件值得推微信，见 `should_push`）—— 落库不受影响：
    # 策略关了只是"不推微信"，面板里照样查得到。
    if ok and push_too and should_push(lv, cat):
        push(lv, cat, ttl, str(body or ""), meta=meta)
    return ok


# ---------------------------------------------------------------------------
# 语义化快捷函数：**加一条通知 = 一行**（推荐用它，不必记 `notify()` 的参数顺序）
#
# 两种 meta 写法都支持，便于从 `notify(...)` 无痛迁移：
#     warn("batch", "快过期", "还有 2 小时", batch_id="b1")     # 关键字直接展开
#     warn("batch", "快过期", "还有 2 小时", meta={"batch_id": "b1"})
# ---------------------------------------------------------------------------

def _merge_meta(meta: Optional[dict[str, Any]],
                extra: dict[str, Any]) -> Optional[dict[str, Any]]:
    """合并 `meta=` 与散装关键字（后者优先）；都空则返回 None。"""
    out: dict[str, Any] = dict(meta or {})
    out.update(extra or {})
    return out or None


def info(category: str, title: str, body: str = "", *,
         meta: Optional[dict[str, Any]] = None, **extra: Any) -> bool:
    """一般消息（`level=info`）。推不推微信由 `push_when` 决定，落库始终全留。

    用法：`info("batch", "已提交离线批次", "258 条请求", batch_id="b1")`
    """
    return notify("info", category, title, body, meta=_merge_meta(meta, extra))


def warn(category: str, title: str, body: str = "", *,
         meta: Optional[dict[str, Any]] = None, **extra: Any) -> bool:
    """警告（`level=warning`）。默认策略（`push_when.warning`）会推微信。"""
    return notify("warning", category, title, body, meta=_merge_meta(meta, extra))


def error(category: str, title: str, body: str = "", *,
          meta: Optional[dict[str, Any]] = None, **extra: Any) -> bool:
    """严重错误（`level=error`）。默认策略（`push_when.error`）会推微信。"""
    return notify("error", category, title, body, meta=_merge_meta(meta, extra))


def debug(category: str, title: str, body: str = "", *,
          meta: Optional[dict[str, Any]] = None, **extra: Any) -> bool:
    """调试（`level=debug`）。三个等级开关都不含它 —— 除非**按类别**命中。"""
    return notify("debug", category, title, body, meta=_merge_meta(meta, extra))


def list_notifications(*, level: str = "", category: str = "",
                       unread_only: bool = False, limit: int = 50,
                       db: Optional[str] = None) -> list[dict[str, Any]]:
    """查通知（最新在前）。库/表还不存在时返回空表 —— 与 `load_state` 同哲学。"""
    sql = "SELECT * FROM notifications"
    where: list[str] = []
    args: list[Any] = []
    if level:
        where.append("level = ?")
        args.append(str(level).lower())
    if category:
        where.append("category = ?")
        args.append(str(category).lower())
    if unread_only:
        where.append("read_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(int(limit))
    try:
        with _db(db) as conn:
            rows = conn.execute(sql, args).fetchall()
    except Exception:                    # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except Exception:                # noqa: BLE001
            d["meta"] = {}
        d["unread"] = d.get("read_at") is None
        out.append(d)
    return out


def count_unread(*, db: Optional[str] = None) -> int:
    """未读条数（面板/CLI 的角标用）。库不可用时返回 0 而不是抛。"""
    try:
        with _db(db) as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL"
            ).fetchone()[0])
    except Exception:                    # noqa: BLE001
        return 0


def mark_read(ids: Optional[list[int]] = None, *, all: bool = False,
              db: Optional[str] = None) -> int:
    """标记已读，返回受影响行数。`all=True` 时忽略 `ids`。"""
    now = time.time()
    try:
        with _db(db) as conn:
            if all:
                cur = conn.execute(
                    "UPDATE notifications SET read_at = ? WHERE read_at IS NULL",
                    (now,))
            elif ids:
                marks = ",".join("?" for _ in ids)
                cur = conn.execute(
                    "UPDATE notifications SET read_at = ? WHERE read_at IS NULL"
                    " AND id IN (%s)" % marks,
                    (now, *[int(i) for i in ids]))
            else:
                return 0
            return int(cur.rowcount or 0)
    except Exception:                    # noqa: BLE001
        return 0


def prune(keep_days: int = DEFAULT_KEEP_DAYS, *, db: Optional[str] = None) -> int:
    """清掉超过保留期的通知，返回删除条数。`keep_days <= 0` = 不限（不动手）。"""
    days = int(keep_days or 0)
    if days <= 0:
        return 0
    cut = time.time() - days * 86400
    try:
        with _db(db) as conn:
            cur = conn.execute("DELETE FROM notifications WHERE ts < ?", (cut,))
            return int(cur.rowcount or 0)
    except Exception:                    # noqa: BLE001
        return 0
