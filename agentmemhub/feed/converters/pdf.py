# -*- coding: utf-8 -*-
"""T3 · PDF：**先探再派**（probe → route）。

为什么 PDF 需要一个"分派器"而不是"一个解析器"
==============================================
同一份 `.pdf` 后缀底下可能是四种完全不同的东西，复杂度差一个数量级：

    T3a 文字版·单栏    文本层直接抽             近乎无损（pypdf）
    T3b 文字版·含表格  表格要从线条重建空间结构  表格可救、**公式拿不回**（结构性上限）
    T3c 扫描件         没有文本层，只有像素      必须先渲染成图 → 需要 OCR 通路（T4）
    T3d 加密/DRM       连字节都读不出来          要密码，**不硬闯**

判据全部**客观可算**（加密标志 / 每页字符数 / 表格线条命中），不看文件名也不猜来源。
"猜来源"在真实素材上必然出错，而错判的后果是**静默失真** ——
把扫描件抽成空正文、把表格抽成散乱文字，都属于"看起来成功了"的失败
（`docs/feed-adapter-plan.md` §7.3 / §7.5）。

⚠️ 一处硬约束（被加密夹具实测逼出来的）
====================================
加密档**不能先碰 `len(reader.pages)`**：未解密就访问会抛 `FileNotDecryptedError`。
所以 `probe()` 的流程必须"先判加密 → 再谈页数"，否则加密档表现为**崩溃**
（`error_kind=crash`）而不是可处置的 T3d。夹具 `pdf_encrypted.pdf` 守的就是这条顺序。

能力边界（明说，不假装）
====================
· T3b 的**公式**拿不回来：PDF 里的公式是排版符号而非语义，任何解析器都一样（C 维度上限）；
· 页内**图片位置**不保留：文本层里没有图片的位置信息，抽出的内嵌图只落 `mirror/`；
· **T3c 未接 OCR**：本档只做到"探测 + 渲染落 mirror + 明确指路"，OCR 属 T4。
"""
from __future__ import annotations

import io
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..base import BaseConverter, FeedDocument, FeedError
from ..mdtable import table_md

#: 认领的扩展名
EXTS = (".pdf",)

#: 路由阈值：平均每页可抽出字符数低于它 ⇒ 判为扫描件（计划 §7.3 定的是 50）
MIN_CHARS_PER_PAGE = 50

#: 单份 PDF 的处理页数上限。为什么要有：几百页的 PDF 会让抽取/渲染变成分钟级，
#: 而投喂是**交互式**操作（人等在那里）。超限**不失败**，只转译前 N 页并记 warning。
MAX_PAGES = 300
#: 扫描件渲染页数上限（渲染比抽取贵得多：每页都要走一遍 PDFium 光栅化）
MAX_RENDER_PAGES = 20
#: 内嵌图片抽取上限
MAX_IMAGES = 40

ROUTE_TEXT = "T3a"
ROUTE_TABLE = "T3b"
ROUTE_SCANNED = "T3c"
ROUTE_ENCRYPTED = "T3d"

#: 元数据里的标题常是这些"占位值"（Word 导出尤其常见）—— 它们不构成标题
_TITLE_JUNK = {"", "untitled", "microsoft word", "document1", "doc1", "无标题"}
#: Word 导出的经典脏标题前缀：`Microsoft Word - 季度报告.doc`
_JUNK_TITLE_PREFIX = re.compile(r"^(?:microsoft\s+word|word)\s*[-–—]\s*", re.I)
#: PDF 日期：`D:YYYYMMDDHHmmSSOHH'mm'`（各段可截断，`O` 是 `+` / `-` / `Z`）
_PDF_DATE_RE = re.compile(
    r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:(Z)|([+-])(\d{2})'?(\d{2})?)?")


# ---------------------------------------------------------------------------
# 探针：只测量、不决策（把"判据"从"处置"里拆出来，判据才能被单独测）
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """只做**机械清理**（行尾空白、多余空行），**不重排不改写**。

    转译层的职责是"换形态"，不是"润色"：PDF 抽出的文本已经丢了版式，
    再动它的字词就是往知识库里掺二手内容。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_pdf_date(raw) -> int:
    """PDF 日期串 → epoch 秒。`raw` 可能已被 pypdf 解析成 `datetime`。

    ⚠️ **必须认时区**：`D:20260101120000Z` 是 UTC，早先的实现在正则里丢掉了那个 `Z`，
    然后拿 `time.mktime` 按**本地时间**解释 ⇒ `doc_time` 系统性偏 8 小时。
    时间在这里不是装饰：召回侧的时间衰减按它算（`docs/data-architecture.md` §4），
    偏 8 小时足以让"当天的知识"排到别的日子去。
    """
    if raw is None:
        return 0
    if hasattr(raw, "timestamp"):            # pypdf 已把 /CreationDate 转成 datetime
        try:
            return int(raw.timestamp())
        except Exception:                    # noqa: BLE001 —— 坏时间不该毁掉转译
            return 0
    m = _PDF_DATE_RE.search(str(raw))
    if not m:
        return 0
    y, mo, d, h, mi, s, zulu, sign, oh, om = m.groups()
    tz = None
    if zulu:
        tz = timezone.utc
    elif sign:
        off = timedelta(hours=int(oh or 0), minutes=int(om or 0))
        tz = timezone(off if sign == "+" else -off)
    try:
        dt = datetime(int(y), int(mo or 1), int(d or 1), int(h or 0),
                      int(mi or 0), int(s or 0), tzinfo=tz)
        # 无时区字段时按**本地时间**解释：PDF 规范要求带时区，但现实里大量不带
        return int(dt.timestamp())
    except Exception:                        # noqa: BLE001
        return 0


def _meta_time(md) -> int:
    """**文档自身时间**优先 CreationDate，其次 ModDate（与 T1/T2 同一口径：
    时间是"知识产生时间"，不是投喂时刻 —— 召回侧的时间衰减按它算）。"""
    if not md:
        return 0
    # 先问 pypdf 解析好的属性（它自带时区），再回落到字符串正则
    for attr in ("creation_date", "modification_date"):
        v = getattr(md, attr, None)
        t = _parse_pdf_date(v) if v is not None else 0
        if t:
            return t
    for key in ("/CreationDate", "/ModDate"):
        try:
            raw = md.get(key)
        except Exception:                    # noqa: BLE001 —— 元数据坏了就走下一个
            raw = None
        t = _parse_pdf_date(raw)
        if t:
            return t
    return 0


def _open_reader(data: bytes):
    """打开 PDF（`PdfReader` 构造失败 = 结构坏/根本不是 PDF，要人换文件 ⇒ 不可重试）。"""
    import pypdf

    if not data:
        raise FeedError("PDF 字节为空", kind="convert", retryable=False)
    try:
        return pypdf.PdfReader(io.BytesIO(data))
    except Exception as e:                   # noqa: BLE001
        raise FeedError("PDF 结构无法解析（文件可能损坏，或扩展名是 .pdf 但内容不是）：%s"
                        % str(e)[:120], kind="convert", retryable=False)


def _probe_tables(data: bytes) -> tuple[int, str]:
    """探表格命中数。返回 `(命中数, 引擎)`。

    **探测失败不算失败**：表格是"增强"，探不到就按 T3a 走 —— 但要把"为什么没探"
    记进结果（`engine` 为 `unavailable`/`error`），让降级**可见**而不是静默。
    """
    try:
        import pdfplumber
    except ImportError:
        return 0, "unavailable"
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            n = 0
            for page in pdf.pages[:MAX_PAGES]:
                try:
                    n += len(page.find_tables())
                except Exception:            # noqa: BLE001 —— 单页探测失败不毁全局
                    continue
            return n, "pdfplumber"
    except Exception:                        # noqa: BLE001
        return 0, "error"


def probe(data: bytes) -> dict:
    """测量这份 PDF 的客观形态。**不做任何处置决策**（决策在 `route()`）。

    返回键：`encrypted / decrypted / pages / texts / chars / avg_chars /
    empty_pages / title / doc_time / first_line / tables / table_engine / warnings`。

    `texts` 顺带保留每页文本：抽文本是 PDF 处理里最贵的一步，
    `probe` 已经为了算字符数抽过一遍，转换阶段再抽一次纯属浪费。
    """
    info: dict = {
        "encrypted": False, "decrypted": False, "pages": 0, "texts": [],
        "chars": [], "avg_chars": 0.0, "empty_pages": 0,
        "title": "", "doc_time": 0, "first_line": "",
        "tables": 0, "table_engine": "", "warnings": [],
    }
    reader = _open_reader(data)

    # ⚠️ 顺序硬约束：**先加密后页数**。未解密访问 reader.pages 会抛
    #    FileNotDecryptedError —— 那样加密档就成了 crash，而不是 T3d。
    if reader.is_encrypted:
        info["encrypted"] = True
        try:
            opened = int(reader.decrypt("") or 0)
        except Exception as e:               # noqa: BLE001
            info["warnings"].append("空密码解密尝试失败：%s" % str(e)[:60])
            return info
        if opened == 0:
            return info                      # 空密码打不开 ⇒ 交给 route() 判 T3d
        info["decrypted"] = True
        info["warnings"].append("PDF 带加密标记但空密码可打开（按正常路径转译）")

    try:
        total = len(reader.pages)
    except Exception as e:                   # noqa: BLE001
        raise FeedError("PDF 页表无法读取：%s" % str(e)[:120],
                        kind="convert", retryable=False)

    if total > MAX_PAGES:
        info["warnings"].append("PDF 共 %d 页，超过单份处理上限 %d 页，只转译前 %d 页"
                                % (total, MAX_PAGES, MAX_PAGES))
    texts: list[str] = []
    for page in reader.pages[:MAX_PAGES]:
        try:
            texts.append(page.extract_text() or "")
        except Exception as e:               # noqa: BLE001 —— 单页坏不毁整份
            texts.append("")
            info["warnings"].append("第 %d 页文本抽取失败：%s"
                                    % (len(texts), str(e)[:60]))
    chars = [len(t) for t in texts]
    info["texts"] = texts
    info["pages"] = len(chars)
    info["chars"] = chars
    info["avg_chars"] = (sum(chars) / len(chars)) if chars else 0.0
    info["empty_pages"] = sum(1 for c in chars if c < MIN_CHARS_PER_PAGE)

    md = getattr(reader, "metadata", None) or {}
    try:
        info["title"] = str(md.get("/Title") or "").strip()
    except Exception:                        # noqa: BLE001
        info["title"] = ""
    info["doc_time"] = _meta_time(md)
    if texts:
        head = _clean_text(texts[0]).splitlines()
        info["first_line"] = head[0].strip() if head else ""

    # 表格只在"文字够多"时才探：扫描件没有文本层，探也是空 —— 先路由更省时间
    if info["avg_chars"] >= MIN_CHARS_PER_PAGE:
        info["tables"], info["table_engine"] = _probe_tables(data)
    return info


def route(info: dict) -> str:
    """**路由判据**（顺序即优先级）。纯函数 —— 喂 `probe()` 的结果就能断言。"""
    if info.get("encrypted") and not info.get("decrypted"):
        return ROUTE_ENCRYPTED
    if info.get("pages", 0) <= 0:
        return ROUTE_SCANNED                 # 一页都没有 = 拿不到文本，同等处置
    if info.get("avg_chars", 0.0) < MIN_CHARS_PER_PAGE:
        return ROUTE_SCANNED
    if info.get("tables", 0) > 0:
        return ROUTE_TABLE
    return ROUTE_TEXT


# ---------------------------------------------------------------------------
# 结构提取（T3a / T3b）
# ---------------------------------------------------------------------------

def _bigrams(text: str) -> set:
    """去空白后的 2-gram 集合。**中英通吃**：中文没有空格分词，
    按"字"切会把整句变成一个 token，bigram 才分得开。"""
    s = re.sub(r"\s+", "", (text or "").lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similar(needle: str, haystack: str) -> float:
    """`needle` 的 2-gram 有多大比例出现在 `haystack` 里。

    **以 needle 为分母是刻意的**：要问的是"首行这些内容能否在元数据标题里找到"，
    而不是反过来。用错分母实测会漏判（截断的元数据标题因为多了
    `C2A … (Commercial Use)` 反而更长，按"谁长谁说了算"就会被它蒙过去）。
    """
    a, b = _bigrams(needle), _bigrams(haystack)
    if not a:
        return 0.0
    return len(a & b) / len(a)


def _clean_meta_title(raw: str) -> str:
    t = (raw or "").strip()
    t = _JUNK_TITLE_PREFIX.sub("", t).strip()
    if t.lower() in _TITLE_JUNK:
        return ""
    return t


def title_of(info: dict, ref: str) -> tuple[str, str]:
    """取标题，返回 `(标题, 来源)`。

    顺序：元数据 `/Title` → 首页首行 → 文件名。
    为什么不无条件用首行：PDF 的"第一行"经常是页眉、日期或页码。
    所以只接受"像标题"的首行（不是纯日期/数字、长度在合理区间）。

    为什么也不无条件信元数据：**真实素材实测**（`Intel_Proprietary_License.pdf`）
    的 `/Title` 被写坏了 —— 停在 `…License Agreement [v2022.12`，而正文首行是
    完整的 `…[v2022.12.20]`。所以加两条同时成立的条件：
      ① 首行不短于元数据标题的 60%（**不能用"谁更长"**：截断的元数据标题因为多了
         `C2A … (Commercial Use)` 反而更长，按长度就会被它蒙过去）；
      ② 首行的 2-gram 有 ≥60% 能在元数据标题里找到（排除"首行是页眉"）。
    两者同时成立，才认为首行是它的完整版。
    """
    meta_title = _clean_meta_title(info.get("title") or "")
    first = (info.get("first_line") or "").strip()
    first_ok = (4 <= len(first) <= 120
                and not re.fullmatch(r"[\d\s\-/.:年月日]+", first))
    if meta_title:
        if (first_ok and len(first) >= 0.6 * len(meta_title)
                and _similar(first, meta_title) >= 0.6):
            return first, "first_line"
        return meta_title, "meta"
    if first_ok:
        return first, "first_line"
    return Path(str(ref)).stem or "PDF 文档", "stem"


def _table_md(rows, *, index: int, page: int, total: int) -> str:
    """渲染表格并打标签 —— "表 N（第 M 页）"是**回原件的定位锚**：
    表格一旦被抽成 markdown 就脱离了页面，不留页码就没法核对。"""
    label = "**表 %d**（第 %d 页）" % (index, page) if total > 1 else "**表 %d**" % index
    return table_md(rows, label=label)


def _build_markdown(info: dict, *, tables: dict, head_title: str) -> str:
    """把每页文本（+ 该页表格）拼成最终 md。

    多页时保留 `## 第 N 页`：页码是**引用锚**（人能拿它回原件定位、LLM 能标注出处），
    成本只是一行小标题。单页不加 —— 加了只是噪声。
    """
    pages = info.get("texts") or []
    total = len(pages)
    blocks: list[str] = []
    if head_title:
        blocks.append("# %s" % head_title)
    for i, raw in enumerate(pages):
        body = _clean_text(raw)
        page_tables = (tables or {}).get(i) or []
        if total > 1:
            blocks.append("## 第 %d 页" % (i + 1))
        if body:
            blocks.append(body)
        for ti, rows in enumerate(page_tables):
            tmd = _table_md(rows, index=ti + 1, page=i + 1, total=total)
            if tmd:
                blocks.append(tmd)
    return "\n\n".join(b for b in blocks if b).strip() + "\n"


def _extract_pdf_tables(data: bytes, info: dict) -> tuple[dict, list[str]]:
    """用 pdfplumber 抽表格，**并把表格区域从正文里剔除**。

    为什么必须剔除（不能"正文 + 表格"简单相加）：pdfplumber 的 `extract_text()`
    里**包含**表格中那些文字 —— 不剔除的话同一批字会以"流式正文"和"表格"
    两种形态各出现一次，读起来像文档被复制了一遍。
    """
    tables: dict[int, list] = {}
    warns: list[str] = []
    try:
        import pdfplumber
    except ImportError:
        warns.append("未安装 pdfplumber，表格未能结构化抽取（表格文字仍留在流式正文里，"
                     "但行列关系已丢）—— `uv sync --extra feed-pdf` 可开启")
        return tables, warns
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages[:MAX_PAGES]):
                try:
                    found = page.find_tables()
                except Exception as e:       # noqa: BLE001
                    warns.append("第 %d 页表格探测失败：%s" % (i + 1, str(e)[:60]))
                    continue
                if not found:
                    continue
                rows_list = []
                for t in found:
                    try:
                        rows = t.extract()
                    except Exception as e:   # noqa: BLE001
                        warns.append("第 %d 页有表格但内容抽取失败：%s"
                                     % (i + 1, str(e)[:60]))
                        continue
                    if rows:
                        rows_list.append(rows)
                if not rows_list:
                    continue
                tables[i] = rows_list
                texts = info.get("texts") or []
                if i < len(texts):
                    boxes = [t.bbox for t in found]

                    def _outside(obj, _boxes=boxes):
                        for (x0, top, x1, bottom) in _boxes:
                            if not (obj["x1"] <= x0 or obj["x0"] >= x1
                                    or obj["bottom"] <= top or obj["top"] >= bottom):
                                return False
                        return True

                    try:
                        texts[i] = page.filter(_outside).extract_text() or ""
                    except Exception as e:   # noqa: BLE001 —— 剔除失败就留着（宁可重复也别丢字）
                        warns.append("第 %d 页表格区域剔除失败（该页表格文字会重复出现在正文）：%s"
                                     % (i + 1, str(e)[:60]))
    except Exception as e:                   # noqa: BLE001
        warns.append("表格抽取整体失败：%s" % str(e)[:80])
    return tables, warns


def _extract_embedded_images(data: bytes, *, put_image, info: dict) -> tuple[int, list[str]]:
    """抽 PDF **内嵌图片**落 `mirror/`。

    为什么不插进 markdown：文本层里**没有图片的位置信息**（图片是独立的对象，
    与文字流无对应关系），硬插只能瞎猜位置。所以只保原件、如实记数 ——
    "镜像是可重生成的中间物"这一层本来就是为了这种场景存在的（§8.0）。
    """
    if not callable(put_image):
        return 0, []
    warns: list[str] = []
    n = 0
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as e:                   # noqa: BLE001
        return 0, ["内嵌图片抽取时无法重新打开 PDF：%s" % str(e)[:60]]
    for pi, page in enumerate(reader.pages[:MAX_PAGES]):
        try:
            images = list(page.images)
        except Exception as e:               # noqa: BLE001 —— 单页图片坏了不影响正文
            warns.append("第 %d 页内嵌图片读取失败：%s" % (pi + 1, str(e)[:60]))
            continue
        for im in images:
            if n >= MAX_IMAGES:
                warns.append("内嵌图片超过 %d 张，其余未抽取" % MAX_IMAGES)
                return n, warns
            name = str(getattr(im, "name", "") or "image")
            blob = getattr(im, "data", b"") or b""
            if not blob:
                continue
            try:
                put_image("embedded/p%d/%s" % (pi + 1, name), blob)
                n += 1
            except Exception as e:           # noqa: BLE001
                warns.append("第 %d 页图片 %s 保存失败：%s" % (pi + 1, name, str(e)[:50]))
    return n, warns


# ---------------------------------------------------------------------------
# T3c：渲染成图（为 OCR 通路备料）
# ---------------------------------------------------------------------------

def render_pages(data: bytes, *, put_mirror, scale: float = 2.0,
                 limit: int = MAX_RENDER_PAGES) -> tuple[int, list[str]]:
    """把扫描件逐页渲染成 PNG 落 `mirror/<doc_id>/pages/`。

    `scale=2.0`（约 144 dpi）：OCR 对分辨率敏感，1x 渲染出来的小字识别率明显更差。

    **本档不产正文**：渲染只是"把像素保存下来"这一步 —— 把像素变成文字需要 OCR
    （T4 通路，未接）。所以 T3c 的终点是"明确报告 + 指路"，不是"假装成功"。
    """
    if not callable(put_mirror):
        return 0, ["没有 mirror 写入口，未能渲染扫描页"]
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return 0, ["未安装 pypdfium2，无法渲染扫描页（`uv sync --extra feed-pdf`）"]
    warns: list[str] = []
    try:
        doc = pdfium.PdfDocument(data)
    except Exception as e:                   # noqa: BLE001
        return 0, ["渲染器无法打开该 PDF：%s" % str(e)[:80]]
    n = 0
    try:
        total = len(doc)
        for i in range(min(total, limit)):
            try:
                bitmap = doc[i].render(scale=scale)
                buf = io.BytesIO()
                bitmap.to_pil().save(buf, format="PNG")
                put_mirror("pages/page-%03d.png" % (i + 1), buf.getvalue())
                n += 1
            except Exception as e:           # noqa: BLE001
                warns.append("第 %d 页渲染失败：%s" % (i + 1, str(e)[:60]))
        if total > limit:
            warns.append("共 %d 页，只渲染了前 %d 页（渲染上限 %d）"
                         % (total, limit, limit))
    finally:
        try:
            doc.close()
        except Exception:                    # noqa: BLE001
            pass
    return n, warns


# ---------------------------------------------------------------------------
# 各路由的文档构造
# ---------------------------------------------------------------------------

def _make_doc(info: dict, *, ref: str, meta: dict, stamp: str, route_name: str,
              tables: dict, images: int, warnings: list[str],
              quality: float) -> FeedDocument:
    title, src = title_of(info, ref)
    # 标题来自**首行**时不再补 H1：那行字本来就在正文里，补了就是同一标题出现两次
    head = "" if src == "first_line" else title
    md = _build_markdown(info, tables=tables, head_title=head)
    doc_time = int(info.get("doc_time") or 0) or int(meta.get("mtime") or 0) \
        or int(time.time())
    n_tables = sum(len(v) for v in (tables or {}).values())
    return FeedDocument(
        doc_id=meta["doc_id"], title=title[:200], markdown=md, kind="pdf",
        source_ref=str(ref), doc_time=doc_time, converter=stamp,
        quality=round(float(quality), 2), warnings=warnings,
        meta={"route": route_name, "pages": info.get("pages", 0),
              "chars_per_page": info.get("chars", []),
              "avg_chars": round(float(info.get("avg_chars") or 0.0), 1),
              "empty_pages": info.get("empty_pages", 0),
              "tables": n_tables, "table_engine": info.get("table_engine", ""),
              "images_extracted": images, "title_source": src,
              "doc_time_source": "pdf_metadata" if info.get("doc_time") else "mtime"},
    )


def _quality_of(info: dict, route_name: str) -> tuple[float, list[str]]:
    """质量自评（计划 §7.5：T3 的信号 = 每页字符数 / 表格命中 / 扫描页占比）。"""
    quality = 1.0
    warns = list(info.get("warnings") or [])
    pages = int(info.get("pages") or 0)
    empty = int(info.get("empty_pages") or 0)
    if route_name == ROUTE_TABLE:
        # 表格是"从线条重建空间结构"，合并单元格与跨页表格必然有损耗 ——
        # 不像 T3a 那样近乎无损，所以不打满分（结构性上限，不是实现缺陷）
        quality = 0.85
    if pages and 0 < empty < pages:
        warns.append("共 %d 页中有 %d 页几乎没有文本层（疑似扫描页，未纳入正文）"
                     % (pages, empty))
        quality = min(quality, 0.6)
    if pages and empty == 0 and info.get("avg_chars", 0) < 120:
        # 文字够多但很稀疏：可能混了少量扫描页，也可能只是内容本来就短 —— 打标让人复核
        warns.append("平均每页仅 %.0f 个字符，内容偏稀疏，建议复核是否漏页"
                     % info.get("avg_chars", 0))
        quality = min(quality, 0.7)
    return quality, warns


class PdfConverter(BaseConverter):
    """T3 PDF 分派器（认领顺序上必须**排在通用 URL 之前** ——
    否则 `feed add https://…/x.pdf` 会被 URL 转换器先认领，喂 HTML 抽取器去解析 PDF 字节）。
    """

    name = "pdf"
    version = "1"

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        if kind == "pdf":
            return True
        # 本地文件 kind 为空；`service` 取回 URL 后会显式传 kind="url"。
        # 两者都要认（否则"网上下的 PDF"进不来），其余 kind 一律不认。
        if kind and kind not in ("url",):
            return False
        if Path(str(ref)).suffix.lower() in EXTS:
            return True
        # 没有扩展名但字节是 PDF（下载目录里 `download`、`file(1)` 常见）
        return bool(data) and bytes(data[:5]) == b"%PDF-"

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        info = probe(data)
        r = route(info)
        if r == ROUTE_ENCRYPTED:
            raise FeedError(
                "PDF 已加密且空密码打不开 —— 转译层不猜密码、不做暴力尝试。"
                "请提供解锁后的 PDF，或用别的通路投喂正文（`feed add --stdin`）",
                kind="encrypted", retryable=False)
        if r == ROUTE_SCANNED:
            return self._scanned(data, ref=ref, meta=meta, info=info)

        images, img_warns = _extract_embedded_images(
            data, put_image=meta.get("put_image"), info=info)
        if images and "![" not in (info.get("texts") or [""])[0]:
            img_warns.append("已抽出 %d 张内嵌图片存 mirror/，但**正文里没有图片位置**"
                             "（PDF 文本层不含图片坐标）" % images)

        tables: dict = {}
        warns_extra: list[str] = []
        if r == ROUTE_TABLE:
            tables, warns_extra = _extract_pdf_tables(data, info)
        quality, warns = _quality_of(info, r)
        warns = warns + img_warns + warns_extra
        if r == ROUTE_TABLE and info.get("table_engine") == "unavailable":
            quality = min(quality, 0.5)
        return _make_doc(info, ref=ref, meta=meta, stamp=self.stamp,
                         route_name=r, tables=tables, images=images,
                         warnings=warns, quality=quality)

    def _scanned(self, data: bytes, *, ref: str, meta: dict, info: dict) -> FeedDocument:
        """T3c：渲染落 mirror，然后**如实报错**（本档没有 OCR，不假装能转）。

        为什么必须先渲染再抛错：原件（raw/）与可见内容（镜像）是**不可再生**的那部分。
        素材进来一趟就该留下这两个，OCR 通路接上后可以 `feed retry` 直接续跑。
        """
        rendered, rwarns = render_pages(data, put_mirror=meta.get("put_mirror"))
        pages = int(info.get("pages") or 0)
        avg = float(info.get("avg_chars") or 0.0)
        if rendered:
            raise FeedError(
                "这是**扫描件**（%d 页，平均每页只有 %.0f 个可抽出字符 ⇒ 没有文本层）。"
                "已把 %d 页渲染成 PNG 存进 mirror/，但要把像素变成正文还需要 OCR 通路"
                "（T4 图片档，本版本未接入）。可用通路：① 把 OCR 结果 `feed add --stdin` "
                "投喂；② 等 T4 落地后 `feed retry` 重跑（原件与镜像都已保住）"
                % (pages, avg, rendered),
                kind="needs_ocr", retryable=False)
        raise FeedError(
            "这是**扫描件**（无文本层），且页面渲染器不可用 —— %s。"
            "装上渲染器后可先把页面渲进 mirror/ 供 OCR 使用"
            % ("；".join(rwarns) or "原因未知"),
            kind="needs_renderer", retryable=False)
