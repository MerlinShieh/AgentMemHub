# -*- coding: utf-8 -*-
"""标题判据的单点（`feed/titles.py`）—— PDF 与 DOCX 共用。

为什么它值得一个独立测试文件：标题判据既没有权威来源、又直接影响 frontmatter
（召回侧给人看的第一眼），而且它是**两个转换器共用**的 —— 单独测它，
才能把"判据错"和"转换器接错"分开定位。
"""
from __future__ import annotations

from agentmemhub.feed.titles import (TITLE_MAX, clean_meta_title, clip_title,
                                     is_junk_title, join_wrapped,
                                     looks_like_title)


# ---------------------------------------------------------------------------
# 什么不算标题
# ---------------------------------------------------------------------------

def test_办公软件的默认文件名不算标题():
    """真实素材：一份求职文档的标题成了「文字文稿1」（WPS 默认名）——
    它对所有这类文档都一样，等于没有信息。"""
    for name in ("文字文稿1", "文档2", "文稿3", "工作簿1", "演示文稿1",
                 "新建 Microsoft Word 文档", "untitled", "无标题", "未命名",
                 "Microsoft Word", "Document1"):
        assert is_junk_title(name), name


def test_正常标题不被误判():
    for name in ("季度技术评审纪要", "Quarterly Review", "投喂适配器 DOCX 夹具",
                 "PDF text routing fixture"):
        assert not is_junk_title(name), name


def test_Word脏标题前缀被剥掉():
    assert clean_meta_title("Microsoft Word - 季度报告") == "季度报告"
    assert clean_meta_title("Word – Quarterly Report") == "Quarterly Report"
    assert clean_meta_title("Microsoft Word") == ""      # 剥完就空了，等于没标题


def test_纯日期与页码不算标题():
    assert not looks_like_title("2026-01-01")
    assert not looks_like_title("2026 年 1 月 1 日")
    assert not looks_like_title("abc")                   # 太短
    assert looks_like_title("季度技术评审纪要")


# ---------------------------------------------------------------------------
# 缝合被"行宽"硬切的首行
# ---------------------------------------------------------------------------

def test_缝合英文续行时补空格():
    """❌ `for the` + `north region` 硬拼成 `thenorth region` —— 那是**假词**。"""
    assert join_wrapped("review of the memory pipeline for the", "north region") == \
        "review of the memory pipeline for the north region"


def test_缝合中文续行时不加空格():
    """中文没有词间空格，加了反而错。真实素材的形态就是这个。"""
    assert join_wrapped("今年有六年有余的工作经", "验。") == "今年有六年有余的工作经验。"
    assert join_wrapped("负责服务端的 雷鸟智能推荐系统项", "目。") == \
        "负责服务端的 雷鸟智能推荐系统项目。"


def test_句子已经完整时不缝合():
    assert join_wrapped("这是一个完整的标题。", "下一段正文") == "这是一个完整的标题。"
    assert join_wrapped("Quarterly Report.", "Second paragraph") == "Quarterly Report."


def test_下一行是正文而不是续行时不缝合():
    """Intel 许可 PDF 的形态：首行是完整标题，第二行就是整段正文 ——
    拼进去会把正文塞进标题里。判据是"续行碎片**明显短于**首行"。"""
    head = "Limited Distribution License Agreement [v2022.12.20]"
    nxt = ("This LIMITED DISTRIBUTION LICENSE AGREEMENT is a contract between you "
           "and Intel Corporation and its affiliates")
    assert join_wrapped(head, nxt) == head


def test_首行已超长时不缝合():
    """拼接是为了把被切掉的尾巴补回来；首行本身都比标题上限长了，
    尾巴**根本进不了**截断后的标题 —— 缝了也是白缝。"""
    head = "A" * TITLE_MAX
    assert join_wrapped(head, "tail") == head


def test_缝合后超长就不缝合():
    """首行还没到上限，但加上续行就越界 —— 这时也不缝（避免缝出一个必须砍的标题）。"""
    head = "A" * (TITLE_MAX - 1)          # 59 字符，未到上限
    assert join_wrapped(head, "B" * 80) == head


# ---------------------------------------------------------------------------
# 限长
# ---------------------------------------------------------------------------

def test_超长标题被截断且截断可被察觉():
    t, clipped = clip_title("甲" * 100)
    assert clipped is True
    assert len(t) == TITLE_MAX + 1 and t.endswith("…")


def test_未超长不算截断():
    t, clipped = clip_title("季度技术评审纪要")
    assert (t, clipped) == ("季度技术评审纪要", False)


def test_限长时压掉换行与多余空白():
    t, clipped = clip_title("标题\n带换行   和   空格")
    assert "\n" not in t and "  " not in t and clipped is False
