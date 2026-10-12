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
        p = self.mirror_dir(doc_id) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p.relative_to(self.root)).replace("\\", "/")

    # -- 第 3 段：最终 md（交付物 / 知识库） ------------------------------

    def kb_path_of(self, doc_id: str) -> str:
        """kb 文件路径 = **纯内容锚** `<doc_id>.md`。

        为什么不拼标题：路径是投影的 `src_id`/`seq` 派生依据（必须**路径稳定**），
        而标题是可变的人读字段 —— 标题一改路径就变，旧页会变成孤儿。
        人读的标题放 frontmatter（`title` / 后续补 `aliases`）。
        """
        return "%s.md" % doc_id

    def publish_kb(self, doc_id: str, text: str) -> str:
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        p = self.kb_dir / self.kb_path_of(doc_id)
        p.write_text(text, encoding="utf-8")
        return str(p)

    def kb_file(self, doc_id: str) -> Path:
        return self.kb_dir / self.kb_path_of(doc_id)


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
            " kind=excluded.kind, source_ref=excluded.source_ref,"
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
