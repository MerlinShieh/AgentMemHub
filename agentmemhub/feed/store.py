# -*- coding: utf-8 -*-
"""三段物料存储 + 素材登记表（`materials.db`）。

三段物料（用户明确要求的三份保留）
==================================
```
第 1 段 raw/      不可变原件字节，**内容寻址** raw/<sha256 前 2 位>/<sha256>.<ext>
第 2 段 mirror/   转译中间形态，按 doc_id 归档（HTML 源码 / 渲染快照 / OCR 中间态）
第 3 段 kb/       最终 md（**统一交付物 / 知识库本体**），进召回面
```

为什么原件走**内容寻址的文件系统**而不是塞进库 BLOB
==================================================
回溯链的强度由 hash 锚保证，不必把字节放进库里：库一旦放进索引库快照，
每份快照都要复制一遍素材（×`snapshot.keep` 份放大）。而内容寻址还有个好处 ——
**同一份文件重复投喂天然去重**，且文件可以被任何工具直接打开看。

为什么 `materials.db` 是**独立库**且不进快照轮换
==============================================
快照 = 索引库整库 + wiki 产物全目录；素材库若入快照会成倍膨胀，而它本身
只是"元数据 + 可重生成的指针"，丢掉也能从 `raw/` 重建（原件才是不可再生的）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

#: 素材状态机：received（原件已落盘）→ converted（转译完成）→ published（交付物已发布）
#: failed 不删原件 —— 换转换器版本后可以重转（这是三段保留的价值）
STATUSES = ("received", "converted", "published", "failed", "retracted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
  doc_id      TEXT PRIMARY KEY,      -- sha256 前 16 位（内容锚）
  sha256      TEXT NOT NULL,         -- 原件全 hash
  kind        TEXT NOT NULL,         -- text | url | wechat | pdf | image
  source_ref  TEXT NOT NULL,         -- 原始路径或 URL
  title       TEXT,
  doc_time    INTEGER,               -- **文档自身时间**（非投喂时刻）
  raw_path    TEXT NOT NULL,         -- 相对 store_dir
  mirror_path TEXT,
  kb_path     TEXT,
  converter   TEXT,                  -- `名字@版本`（重转判据）
  quality     REAL,
  warnings    TEXT,                  -- JSON 数组文本
  status      TEXT NOT NULL,
  error       TEXT,
  error_kind  TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_materials_sha ON materials(sha256);
CREATE INDEX IF NOT EXISTS idx_materials_status ON materials(status);
"""


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_id_of(sha: str) -> str:
    """内容锚：sha256 前 16 位。**同字节 ⇒ 同 doc_id**（幂等的根）。"""
    return sha[:16]


def _slug(text: str, *, limit: int = 60) -> str:
    """文件名安全化：去路径分隔与 Windows 非法字符，压缩空白。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(text or "")).strip(" .-")
    s = re.sub(r"\s+", "-", s)
    return (s or "untitled")[:limit]


def _safe_mirror_rel(name: str) -> str:
    """把外部给的文件名压成 `mirror/<doc_id>/` 之内的一段相对路径。

    `name` 的来源里至少有 PDF 内嵌图的资源名 —— **PDF 作者可控**。直接拼路径时
    `embedded/p1/../../../../x.png` 会写到 store 之外；而 `put_mirror` 是
    「先 `write_bytes`、再算 `relative_to`」，报错那一刻文件已经落地了
    （2026-10-26 审计实测）。所以清洗必须发生在拼路径**之前**。
    """
    segs: list[str] = []
    for seg in str(name or "").replace("\\", "/").split("/"):
        seg = seg.strip()
        if seg in ("", ".", ".."):
            continue
        seg = seg.replace(":", "_")                    # 去盘符（C:）
        seg = re.sub(r'[<>"|?*\x00-\x1f]', "_", seg)   # Windows 非法字符
        if seg:
            segs.append(seg)
    if not segs:
        raise ValueError("非法的镜像文件名：%r" % (name,))
    rel = "/".join(segs)
    if len(rel) > 240:                                 # 超长名在 Windows 直接失败
        head, _, tail = rel.rpartition("/")
        rel = (head + "/" if head else "") + tail[-200:]
    return rel


#: 文件名末尾的 doc_id（`<自然语言标题>-<doc_id>.md`，也认早期的纯 `<doc_id>.md`）
_DOC_ID_TAIL_RE = re.compile(r"^(?:(?P<slug>.+)-)?(?P<doc>[0-9a-f]{8,64})$")

#: 文件名里不能出现的字符：文件系统保留字符 + Obsidian 的 `[ ] # ^ |`
#: （Obsidian 会把它们当链接/标签/别名语法，文件名里出现就无法 `[[链接]]`）
_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\[\]#^]')


def kb_slug(title: str, limit: int = 60) -> str:
    """把标题压成能当文件名的**自然语言片段**。保留中文，只清掉非法字符。

    为什么限长 60：完整文件名是 `标题-<16位doc_id>.md`，Windows 单段上限 255、
    整路径 260，60 个**字符**（中文按字符算、按字节会到 180）留足了余量，
    同时又不至于把长标题切得看不懂。
    """
    s = _FILENAME_BAD.sub(" ", str(title or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        return ""
    if len(s) > limit:
        s = s[:limit].rstrip(" .")
    return s


def kb_doc_of(filename: str) -> str:
    """从交付物文件名反解 doc_id；认不出（纯哈希之外的怪名字）返回空串。"""
    m = _DOC_ID_TAIL_RE.match(Path(filename).stem)
    return m.group("doc") if m else ""


@dataclass
class MaterialStore:
    """三段物料的读写。路径全部相对 `root`（可整目录搬走 / 归档）。"""

    root: Path
    kb_dir: Path

    # -- 第 1 段：原件（不可变） ------------------------------------------

    def raw_rel(self, sha: str, ext: str) -> str:
        ext = ext if ext.startswith(".") or not ext else "." + ext
        return str(Path("raw") / sha[:2] / (sha + ext))

    def raw_path(self, rel: str) -> Path:
        return self.root / rel

    def put_raw(self, data: bytes, ext: str = "") -> tuple[str, str]:
        """写原件（幂等：同内容已存在则不重复写）。返回 `(sha256, 相对路径)`。"""
        sha = sha256_of(data)
        rel = self.raw_rel(sha, ext)
        p = self.raw_path(rel)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时名再改名：中途失败不会留下"看起来完整"的半截原件
            tmp = p.with_suffix(p.suffix + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        return sha, rel

    def read_raw(self, rel: str) -> bytes:
        return self.raw_path(rel).read_bytes()

    # -- 第 2 段：镜像（可重生成） ----------------------------------------

    def mirror_dir(self, doc_id: str) -> Path:
        d = self.root / "mirror" / doc_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put_mirror(self, doc_id: str, name: str, data: bytes) -> str:
        p = self.mirror_dir(doc_id) / _safe_mirror_rel(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p.relative_to(self.root)).replace("\\", "/")

    def put_image(self, doc_id: str, url: str, data: bytes) -> str:
        """把正文里的一张图存进 `mirror/<doc_id>/images/`。

        文件名由 **URL 的 hash** 派生：稳定（同一张图重复本地化不会产生重复文件）、
        且不会因为 URL 里的查询串/超长路径把文件名搞坏。
        """
        h = hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:12]
        ext = Path(urlparse(str(url)).path).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,5}", ext or ""):
            ext = ".img"                    # 没有/异常扩展名也要能存
        return self.put_mirror(doc_id, "images/%s%s" % (h, ext), data)

    # -- 第 3 段：最终 md（交付物 / 知识库） ------------------------------

    def kb_path_of(self, doc_id: str, title: str = "") -> str:
        """交付物文件名 = `<自然语言标题>-<doc_id>.md`。

        早期是纯 `<doc_id>.md`，理由是「路径是投影的 src_id/seq 派生依据，
        必须路径稳定」。代价是在 Obsidian / 资源管理器里只看到一串哈希
        （2026-10-28 用户提出要改）。现在改为**标题在前、完整 doc_id 在后**：

        - 人读的是标题，机器读的是末尾的 doc_id（`kb_doc_of()` 反解），
          所以「按 doc_id 找文件」的幂等性没丢，只是从"拼路径"变成"扫目录"；
        - 标题为空 / 全是非法字符时**退回纯 `<doc_id>.md`**，保证任何素材
          都有落点，不会因为没有标题而写不出文件。
        """
        slug = kb_slug(title)
        return "%s-%s.md" % (slug, doc_id) if slug else "%s.md" % doc_id

    def kb_candidates(self, doc_id: str) -> list[Path]:
        """所有**可能**是 `doc_id` 交付物的文件（`<标题>-<id>.md` 与旧的 `<id>.md`）。"""
        if not self.kb_dir.is_dir():
            return []
        return ([self.kb_dir / ("%s.md" % doc_id)]
                + sorted(self.kb_dir.glob("*-%s.md" % doc_id)))

    def publish_kb(self, doc_id: str, text: str, title: str = "") -> str:
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        target = self.kb_dir / self.kb_path_of(doc_id, title)
        target.write_text(text, encoding="utf-8")
        # 标题会变（重转 / 改元数据），旧名必须删：否则同一份素材在目录里躺两份，
        # 而 lint 的「文件名 ↔ doc_id」对账会把它当成一条多余登记。
        # 顺序是「先写新、后删旧」—— 中途失败最多多一个文件，不会一份都不剩。
        for old in self.kb_candidates(doc_id):
            if old.resolve() != target.resolve():
                try:
                    old.unlink()
                except OSError:
                    pass
        return str(target)

    def kb_file(self, doc_id: str) -> Path:
        """按 doc_id 找交付物；找不到时返回纯锚路径（调用方用 `.is_file()` 判断）。"""
        for p in self.kb_candidates(doc_id):
            if p.is_file():
                return p
        return self.kb_dir / ("%s.md" % doc_id)


class MaterialsDB:
    """素材登记表（独立库）。状态机与字段见模块 docstring 与 `_SCHEMA`。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:                       # noqa: BLE001 —— 关闭失败不影响调用方
            pass

    # -- 写 ---------------------------------------------------------------

    def upsert(self, *, doc_id: str, sha256: str, kind: str, source_ref: str,
               title: str, doc_time: int, raw_path: str,
               mirror_path: str = "", kb_path: str = "", converter: str = "",
               quality: float = 0.0, warnings: Optional[list] = None,
               status: str = "received", error: str = "",
               error_kind: str = "") -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO materials(doc_id, sha256, kind, source_ref, title,"
            " doc_time, raw_path, mirror_path, kb_path, converter, quality,"
            " warnings, status, error, error_kind, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(doc_id) DO UPDATE SET"
            " sha256=excluded.sha256, kind=excluded.kind,"
            " source_ref=excluded.source_ref,"
            " title=excluded.title, doc_time=excluded.doc_time,"
            " raw_path=excluded.raw_path, mirror_path=excluded.mirror_path,"
            " kb_path=excluded.kb_path, converter=excluded.converter,"
            " quality=excluded.quality, warnings=excluded.warnings,"
            " status=excluded.status, error=excluded.error,"
            " error_kind=excluded.error_kind, updated_at=excluded.updated_at",
            (doc_id, sha256, kind, source_ref, title, int(doc_time or 0),
             raw_path, mirror_path, kb_path, converter, float(quality or 0.0),
             json.dumps(list(warnings or []), ensure_ascii=False), status,
             error, error_kind, now, now))
        self.conn.commit()

    def set_status(self, doc_id: str, status: str, *,
                   error: str = "", error_kind: str = "") -> None:
        if status not in STATUSES:
            raise ValueError("未知状态：%s（合法值 %s）" % (status, STATUSES))
        self.conn.execute(
            "UPDATE materials SET status=?, error=?, error_kind=?, updated_at=?"
            " WHERE doc_id=?", (status, error, error_kind, int(time.time()),
                                doc_id))
        self.conn.commit()

    def set_kb_path(self, doc_id: str, kb_path: str) -> None:
        """只改交付物路径（改名迁移用；整行 `upsert` 需要补齐所有字段，太重）。"""
        self.conn.execute(
            "UPDATE materials SET kb_path=?, updated_at=? WHERE doc_id=?",
            (kb_path, int(time.time()), doc_id))
        self.conn.commit()

    def delete(self, doc_id: str) -> bool:
        """删掉登记行（`forget` 用）。返回是否真的删到了。

        ⚠️ **只删登记**：原件（raw/）与交付物（kb/）由调用方决定去留 ——
        原件是不可再生的那一份，"删登记"不该顺手把它带走。
        """
        cur = self.conn.execute("DELETE FROM materials WHERE doc_id=?", (doc_id,))
        self.conn.commit()
        return bool(cur.rowcount)

    # -- 读 ---------------------------------------------------------------

    def get(self, doc_id: str) -> Optional[dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM materials WHERE doc_id=?",
                              (doc_id,)).fetchone()
        return self._row(r) if r else None

    def find_by_sha(self, sha256: str) -> Optional[dict[str, Any]]:
        """**幂等查重**：同字节（同 sha256）是否已投喂过。"""
        r = self.conn.execute("SELECT * FROM materials WHERE sha256=?",
                              (sha256,)).fetchone()
        return self._row(r) if r else None

    def find_by_source(self, source_ref: str) -> Optional[dict[str, Any]]:
        """按**来源标识**（最终 URL / 文件路径）查最近一条。

        为什么 URL 需要它：动态页面的字节**每次都变**（时间戳、CSRF token、广告位），
        若只按内容 hash 定身份，**同一个链接每投喂一次就多一条素材**
        （2026-10-12 真机验收实测：同一篇公众号文章两次抓取 sha 不同）。
        """
        r = self.conn.execute(
            "SELECT * FROM materials WHERE source_ref=?"
            " ORDER BY updated_at DESC LIMIT 1", (str(source_ref),)).fetchone()
        return self._row(r) if r else None

    def list(self, *, status: str = "", low_quality: bool = False,
             limit: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM materials"
        args: list[Any] = []
        where = []
        if status:
            where.append("status=?")
            args.append(status)
        if low_quality:
            where.append("(quality<? OR (warnings IS NOT NULL AND warnings!='[]'))")
            args.append(0.5)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, doc_id"
        if limit:
            sql += " LIMIT %d" % int(limit)
        return [self._row(r) for r in self.conn.execute(sql, args)]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        try:
            d["warnings"] = json.loads(d.get("warnings") or "[]")
        except Exception:                       # noqa: BLE001 —— 坏值当空
            d["warnings"] = []
        return d
