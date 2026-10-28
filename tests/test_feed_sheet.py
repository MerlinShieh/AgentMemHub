# -*- coding: utf-8 -*-
"""XLSX 与 CSV/TSV：**"常用文档的最后一个缺口"**（办公三件套的最后一件 + 导出格式）。

为什么这两个格式值得单开一份测试
=============================
DOCX / PPTX 已通之后，办公三件套只剩 XLSX；而 CSV 是**导出格式的默认选择**
（几乎每个系统都"能导出 CSV"），它的量往往比 xlsx 还大、脏法还多。
两者形态完全不同（一个 OOXML、一个纯字节），坑也完全不同，所以分开守：

**XLSX 的坑是"值看着对、其实错"**
  · 日期在文件里是**序列号**（`<v>45000</v>` + `styles.xml` 的数字格式 id）——
    自己解 XML 会把它当整数放出去，**不报错、不告警**，表里静静多一列数字。
    这是"必须用 openpyxl"的全部理由，所以第一条测试就是它；
  · 布尔要出 `TRUE`（Excel 的显示样式），不是 Python 的 `True`；
  · core 时间读回来**不带 tzinfo**（docx/pptx 带），naive 的 `.timestamp()`
    按本地时区解释 ⇒ doc_time 偏 8 小时（实测踩到，见 `test_..._按UTC解释`）。

**CSV 的坑是"整份看起来像数据"**
  · 中文 CSV 大量是 **GBK**（Excel 在中文 Windows 上的默认），按 UTF-8 硬解
    出来是乱码，而乱码**看起来仍然像数据**；
  · 分隔符不写在文件里（逗号 / 分号 / 制表符都常见），且判定必须跳过引号内；
  · 类型全丢 ⇒ **坚决不做类型推断**：`00123` 变成 `123` 是改数据。

**隔离**：本文件全部直接喂 `BytesIO`，不碰真实库、产物目录与网络。
夹具 `sample.xlsx` 由 `tests/fixtures/feed/make_fixtures_m4.py` 生成（可复现）。
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook

from agentmemhub.feed import get_converter
from agentmemhub.feed.base import FeedError
from agentmemhub.feed.converters.csvfile import CsvConverter, _decode
from agentmemhub.feed.converters.xlsx import XlsxConverter
from agentmemhub.feed.formats import is_allowed, supported_exts
from agentmemhub.feed.mdtable import MAX_COLS, MAX_ROWS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "feed"
#: 夹具的字节指纹。**改了夹具内容就要同步改这里** ——
#: 它守的是"逐字节可复现"（`openpyxl` 会把 core.xml 的 modified 写成当前时间，
#: 不钉死的话每次生成都不同，git 里会持续冒出无意义的二进制变更）。
XLSX_FIXTURE_SHA256 = \
    "c95f8dc96f056800d7f9750edbcc3839a57d9dcb8730c3cf1aabf09913b8136c"


def _meta(**extra) -> dict:
    m = {"doc_id": "x", "mtime": 0, "now": 0}
    m.update(extra)
    return m


def _xlsx_bytes(sheets, *, props_title=None) -> bytes:
    """造一份最小工作簿。`sheets` = `[(表名, 行列表)]`；表名给 `None` 用默认名。"""
    wb = Workbook()
    first = True
    for name, rows in sheets:
        ws = wb.active if first else wb.create_sheet()
        first = False
        if name:
            ws.title = name
        for row in rows:
            ws.append(list(row))
    if props_title is not None:
        wb.properties.title = props_title
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _conv_xlsx(data: bytes, ref: str = "台账.xlsx", **kw):
    return XlsxConverter().convert(ref=ref, data=data, meta=_meta(**kw))


def _conv_csv(data: bytes, ref: str = "名单.csv", **kw):
    return CsvConverter().convert(ref=ref, data=data, meta=_meta(**kw))


def _table_lines(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.startswith("|")]


# ---------------------------------------------------------------------------
# XLSX：值看着对、其实错的那一类
# ---------------------------------------------------------------------------

def test_xlsx_日期还原成日期而不是序列号():
    """**这条是"用库、不自己解 XML"的全部理由。**

    xlsx 里的日期是"序列号 + 数字格式 id"两步表达：解错了不会报错，
    只是表里静静多出一列 `45000` —— 等你拿它算账才发现。
    """
    data = _xlsx_bytes([("Sheet1", [["日期"], [datetime(2026, 10, 15)]])])
    md = _conv_xlsx(data).markdown
    assert "2026-10-15" in md
    assert "45000" not in md and "46000" not in md


def test_xlsx_零点只给日期_带时间的给完整时间():
    """零点若也打 `00:00:00`，一整列日期都拖着无意义的尾巴。"""
    data = _xlsx_bytes([(None, [
        ["纯日期", "日期时间"],
        [datetime(2026, 10, 15, 0, 0, 0), datetime(2026, 10, 15, 14, 30, 5)],
    ])])
    md = _conv_xlsx(data).markdown
    assert "| 2026-10-15 |" in md
    assert "2026-10-15 14:30:05" in md
    assert "00:00:00" not in md


def test_xlsx_布尔出TRUE而不是Python字面量():
    data = _xlsx_bytes([(None, [["开关"], [True], [False]])])
    md = _conv_xlsx(data).markdown
    assert "TRUE" in md and "FALSE" in md
    assert "True" not in md and "False" not in md


def test_xlsx_竖线与换行不会破坏表结构():
    """`|` 会撑坏管道表、换行会撑坏行 —— 两者都必须在单元格层被处理掉。"""
    data = _xlsx_bytes([(None, [["a", "b"], ["含|竖线", "含\n换行"]])])
    md = _conv_xlsx(data).markdown
    assert "含\\|竖线" in md
    assert "含 换行" in md
    assert len(_table_lines(md)) == 3        # 表头 + 分隔 + 1 数据行


def test_xlsx_全空行与尾部空列被跳过():
    """只读模式下行的长度由 `dimension` 决定，尾部常是一长串 `None`。"""
    data = _xlsx_bytes([(None, [["a", "b"], [None, None], ["1", "2", None, None]])])
    md = _conv_xlsx(data).markdown
    lines = _table_lines(md)
    assert len(lines) == 3
    assert lines[2] == "| 1 | 2 |"           # 尾部空洞被裁掉


def test_xlsx_多表分节_单表且默认名时不写表名():
    """表名是**定位信息**：两张表时必须写；单表且叫 `Sheet` 时写出来只是噪声
    （与 XMIND 的"单画布省略 `## 画布`"同款取舍）。"""
    two = _xlsx_bytes([("销售明细", [["a"], [1]]), ("Sheet2", [["b"], [2]])])
    md = _conv_xlsx(two).markdown
    assert "## 销售明细" in md and "## Sheet2" in md

    one_default = _xlsx_bytes([(None, [["a"], [1]])])
    assert "## " not in _conv_xlsx(one_default).markdown

    one_named = _xlsx_bytes([("7月台账", [["a"], [1]])])
    assert "## 7月台账" in _conv_xlsx(one_named).markdown


def test_xlsx_公式无缓存值时是空格子_已知边界():
    """**记录在案的行为，不是 bug**：`data_only=True` 读的是 Excel 算出来的
    **缓存值**；文件生成后从没被 Excel 打开过就没有缓存 ⇒ 空格子。
    钉住它是为了让后来看到空格子的人知道这是有意的，而不是"某次改动弄丢了数据"。
    也确认**不把公式文本当内容**输出（`=A2+1` 不是数据）。
    """
    data = _xlsx_bytes([(None, [["a", "b"], [1, "=A2+1"]])])
    md = _conv_xlsx(data).markdown
    assert "=A2+1" not in md
    assert "| 1 |  |" in md


def test_xlsx_标题回落_core到文件名到表名():
    with_title = _xlsx_bytes([(None, [["a"], [1]])], props_title="年度经营数据")
    assert _conv_xlsx(with_title).title == "年度经营数据"
    no_title = _xlsx_bytes([(None, [["a"], [1]])])
    assert _conv_xlsx(no_title, ref="2026台账.xlsx").title == "2026台账"
    # 文件名是 Office 默认名（`工作簿1`）时不用它，而表名也是默认名 ⇒ 兜底
    assert _conv_xlsx(no_title, ref="工作簿1.xlsx").title == "表格工作簿"


def test_xlsx_老格式或加密在解析前就被分开报错():
    """`.xls`（Excel 97-2003）与**加密工作簿**都是 OLE 复合二进制，不是 ZIP。
    必须在解析**之前**判出来：前者的处置是"另存为 .xlsx"，后者是"文件坏了" ——
    混成一句报错等于没说。"""
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    with pytest.raises(FeedError) as ei:
        _conv_xlsx(ole)
    assert ei.value.kind == "unsupported" and ei.value.retryable is False
    assert "另存为" in str(ei.value)


def test_xlsx_损坏文件报convert():
    with pytest.raises(FeedError) as ei:
        _conv_xlsx(b"PK\x03\x04 not a real zip")
    assert ei.value.kind == "convert"


def test_xlsx_全空工作簿报empty():
    """空正文绝不能进知识库（`FeedDocument` 与 `wiki_index.project_pages` 两处都拦）。"""
    with pytest.raises(FeedError) as ei:
        _conv_xlsx(_xlsx_bytes([(None, [])]))
    assert ei.value.kind == "empty"


def test_xlsx_doc_time按UTC解释而不是本地时区():
    """**回归守卫（实测踩过的 8 小时坑）**：

    core.xml 里存的是 `2026-01-01T12:00:00Z`（UTC），但 `openpyxl` 读回来
    **不带 tzinfo**（`python-docx` / `python-pptx` 会保留）。naive 的
    `.timestamp()` 按**本地时区**解释 ⇒ 东八区下 doc_time 整整偏 8 小时。
    这与 PDF 那档"`/CreationDate` 丢掉 `Z`"是同一个坑的第二次出现，
    所以断言的是 **UTC 那一刻**的时间戳。
    """
    doc = _conv_xlsx((FIXTURES / "sample.xlsx").read_bytes(), ref="sample.xlsx")
    assert doc.meta["doc_time_source"] == "core_modified"
    assert doc.doc_time == int(datetime(2026, 1, 1, 12, 0, 0,
                                        tzinfo=timezone.utc).timestamp())


def test_xlsx_超过行上限时截断并降分():
    """截断是**丢数据** ⇒ 必须降分（落进 `feed list --low-quality` 的视野），
    不能只记一条 warning 了事。"""
    rows = [["列"]] + [["行%d" % i] for i in range(MAX_ROWS + 20)]
    doc = _conv_xlsx(_xlsx_bytes([(None, rows)]))
    assert doc.quality == 0.7
    assert doc.meta["truncated"] is True
    assert any("超过" in w for w in doc.warnings)


def test_xlsx_宽表截断到列上限():
    doc = _conv_xlsx(_xlsx_bytes([(None, [["c%d" % i for i in range(MAX_COLS + 5)]])]))
    assert doc.quality == 0.7 and doc.meta["truncated"] is True


def test_xlsx_无后缀靠ZIP内容判型且不抢别的格式():
    """无扩展名时按"ZIP 里有没有 `xl/workbook.xml`"判型 ——
    判据必须能区分 docx / pptx / xmind，否则无后缀文件会被错的转换器认领。"""
    conv = XlsxConverter()
    assert conv.can_handle(ref="无后缀", data=(FIXTURES / "sample.xlsx").read_bytes())
    assert not conv.can_handle(ref="无后缀",
                               data=(FIXTURES / "sample.docx").read_bytes())


def test_xlsx_夹具逐字节可复现():
    """夹具的"可复现"必须被守住，否则 git 里会持续冒出无意义的二进制变更
    （实测踩到：`openpyxl` 保存时把 core.xml 的 modified 改写成当前时间）。"""
    import hashlib
    data = (FIXTURES / "sample.xlsx").read_bytes()
    assert hashlib.sha256(data).hexdigest() == XLSX_FIXTURE_SHA256


# ---------------------------------------------------------------------------
# CSV / TSV：整份"看起来像数据"的那一类
# ---------------------------------------------------------------------------

def test_csv_GBK编码正确解码():
    """中文 Windows 上 Excel"另存为 CSV"默认用系统 ANSI（GBK），不是 UTF-8。
    按 UTF-8 硬解出来是**乱码**，而乱码看起来仍然像数据 —— 比报错难发现。"""
    raw = "姓名,部门\n张三,研发\n".encode("gb18030")
    doc = _conv_csv(raw)
    assert "张三" in doc.markdown and "研发" in doc.markdown
    assert doc.meta["encoding"] == "gb18030"
    assert doc.quality == 1.0


def test_csv_UTF8带BOM时BOM不进正文():
    raw = "\ufeff姓名,部门\n张三,研发\n".encode("utf-8")
    doc = _conv_csv(raw, ref="名单.csv")
    assert doc.meta["encoding"] == "utf-8-sig"
    assert doc.markdown.splitlines()[0] == "# 名单"
    assert "姓名" in doc.markdown


def test_csv_引号内的逗号不参与分隔符判定():
    """`"北京, 上海"` 是**一个**单元格。判分隔符时若把引号内的逗号也算上，
    "内容里逗号多、实际用分号分隔"的文件就会判错（这是真的会发生的组合）。"""
    raw = 'a;b\n"北京, 上海";x\n'.encode("utf-8")
    doc = _conv_csv(raw)
    assert doc.meta["delimiter"] == ";"
    assert "北京, 上海" in doc.markdown


def test_csv_制表符与竖线也能当分隔符():
    assert _conv_csv("a\tb\n1\t2\n".encode(), ref="x.tsv").meta["delimiter"] == "\\t"
    doc = _conv_csv("a|b\n1|2\n".encode())
    assert doc.meta["delimiter"] == "|"
    assert "a | b" in doc.markdown


def test_csv_不做类型推断_前导零必须保留():
    """`00123` 是**字符串**（工号 / 邮编 / 批次号）。"美化"掉前导零就是**改数据** ——
    表格里最不能犯的错是悄悄改了值，比"没识别出这是个日期"严重得多。"""
    md = _conv_csv("编码,数量\n00123,007\n".encode("utf-8")).markdown
    assert "00123" in md and "007" in md


def test_csv_空文件与纯空行报empty():
    for raw in (b"", b"\n\n\n", b",,,\n,,\n"):
        with pytest.raises(FeedError) as ei:
            _conv_csv(raw)
        assert ei.value.kind == "empty"


def test_csv_三种编码都解不出时用替换字符并告警():
    """**不静默失败**：解不出来也要把资料收进来（质量自评会把它标出来），
    但必须有可见的告警 —— 有一两个字是问号，远好过整份资料进不来。"""
    text, enc, warns = _decode(b"\xff\xff\xff")
    assert enc == "utf-8/replace"
    assert warns and "编码" in warns[0]


def test_csv_行超限截断并降分():
    raw = ("h\n" + "".join("r%d\n" % i for i in range(MAX_ROWS + 10))).encode("utf-8")
    doc = _conv_csv(raw)
    assert doc.quality == 0.7 and doc.meta["truncated"] is True


def test_csv_标题取文件名_默认名回落():
    assert _conv_csv(b"a,b\n1,2\n", ref="2026销售.csv").title == "2026销售"
    assert _conv_csv(b"a,b\n1,2\n", ref="工作簿1.csv").title == "表格"


# ---------------------------------------------------------------------------
# 注册表 / 白名单：新格式必须**自动**进去（否则就是"两处清单漂移"那个坑）
# ---------------------------------------------------------------------------

def test_白名单自动包含新增后缀():
    sup = supported_exts()
    for ext in (".xlsx", ".xlsm", ".csv", ".tsv"):
        assert ext in sup, ext
        assert is_allowed("x" + ext) is True


def test_认领_新格式各归各的转换器():
    assert get_converter(ref="a.xlsx").name == "xlsx"
    assert get_converter(ref="a.xlsm").name == "xlsx"
    assert get_converter(ref="a.csv").name == "csv"
    assert get_converter(ref="a.tsv").name == "csv"
    # 新格式**不能**抢走已有的：办公三件套各归各的
    assert get_converter(ref="a.docx").name == "docx"
    assert get_converter(ref="a.pptx").name == "pptx"
    assert get_converter(ref="a.xmind").name == "xmind"
