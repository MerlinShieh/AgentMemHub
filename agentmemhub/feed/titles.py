# -*- coding: utf-8 -*-
"""标题判定的**单点** —— PDF / DOCX 两条通路共用（T2/T1 的取值顺序另在各自模块）。

为什么值得单独一层
==================
标题取值是每条通路都要做的事，而它**没有权威来源**：PDF 的元数据可能被写坏、
DOCX 可能完全没用标题样式、纯文本只有一个文件名。各写一份的结果是
"同一份内容从不同格式进来，标题不一样" —— 而标题进 frontmatter，是召回侧给人看的第一眼。

**两条被真实素材反复验证的纪律**：

1. **元数据不可盲信**：真实 PDF（Intel 许可协议）的 `/Title` 被写坏了，
   停在 `…[v2022.12`，而正文首行是完整的 `…[v2022.12.20]`；
2. **标题必须短，不信就标出来**：一份没有标题的求职文档，
   正文首段是"杨工，姚工，您好，我叫…"。取全句（46 字）勉强可用，
   取一整段（200+ 字）就不是标题了 ⇒ 超长要截断，且**截断这件事要能被看见**
   （记 `warnings`），不能悄悄给一个像是原文的标题。
"""
from __future__ import annotations

import re

#: 占位标题（元数据里出现这些值等于没有标题）
#:
#: 这张名单是**三个真实素材**凑出来的，每一条都对应一次实测：
#: · `文字文稿1` —— WPS 默认文件名（求职文档的标题一度成了它）；
#: · `PowerPoint 演示文稿` —— PowerPoint 的默认 core title（4 份评审 PPT 全是它）；
#: · `中心主题` —— XMind 新建导图的**默认根节点名**（2 份业务导图没改过它）。
#: 共同点：**对所有同类文档都一样**，所以作为标题等于零信息。
TITLE_JUNK = {
    "", "untitled", "无标题", "未命名", "microsoft word",
    "document1", "doc1", "word 文档", "新建文档",
    "中心主题", "powerpoint 演示文稿", "powerpoint presentation",
    "presentation1", "幻灯片1", "思维导图", "sheet1",
}
#: 办公软件的**默认文件名**：WPS 的 `文字文稿1`、Word 的 `新建 Microsoft Word 文档`
#: —— 它们作为知识库标题毫无信息量（真实素材踩到：一份求职文档的标题成了"文字文稿1"）
_JUNK_NAME_RES = (
    re.compile(r"^(?:文字文稿|文档|文稿|工作簿|演示文稿|新建\s*microsoft\s*word\s*文档)\s*\d*$",
               re.I),
)
#: Word 导出的经典脏标题前缀：`Microsoft Word - 季度报告.doc`
JUNK_TITLE_PREFIX = re.compile(r"^(?:microsoft\s+word|word)\s*[-–—]\s*", re.I)
#: 句末标点：判断"这句话说完了没有"（用于缝合被版面硬切的首行）
SENTENCE_END_RE = re.compile(r"[。！？；!?;]\s*$")
#: 标题长度上限 —— 超过它就不是标题而是正文了
TITLE_MAX = 60
#: 首行 + 续行拼接后的总长上限
JOIN_MAX = 120


def is_junk_title(text: str) -> bool:
    """这个字符串够不够格当标题（去空白后判）。"""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if t.lower() in TITLE_JUNK:
        return True
    return any(r.match(t) for r in _JUNK_NAME_RES)


def clean_meta_title(raw: str) -> str:
    """清理元数据标题：剥掉 Word 脏前缀，命中占位值则视为"没有标题"。"""
    t = JUNK_TITLE_PREFIX.sub("", (raw or "").strip()).strip()
    return "" if is_junk_title(t) else t


def join_wrapped(head: str, following: str) -> str:
    """把被**版面宽度硬切**的首行缝完整。

    为什么必须做：PDF 文本层里"一行"是**排版产物**而不是语义单位 ——
    真实素材的首行是 `…今年有六年有余的工作经`，`验。` 落在下一行，
    直接拿去当标题就是一句断掉的话。

    为什么缝得要**保守**：下一行也可能已经是**正文的新一句**
    （Intel 许可 PDF 的首行后面就是整段正文）。判据用"续行碎片"的特征 ——
    只有下一行**明显短于**首行时才认为它是被切掉的尾巴。
    """
    head = re.sub(r"\s+", " ", (head or "").strip())
    nxt = re.sub(r"\s+", " ", (following or "").strip())
    if not head or not nxt or SENTENCE_END_RE.search(head):
        return head
    # 首行**已经**比标题上限还长：缝上去的尾巴根本进不了截断后的标题，白缝
    if len(head) >= TITLE_MAX:
        return head
    if len(head) + len(nxt) > JOIN_MAX:
        return head
    if len(nxt) > max(10, int(len(head) * 0.4)):
        return head
    # 英文/数字之间要补一个空格，中文不要 —— 缝合边界是"半个词"时
    # 硬拼会得到 `thenorth region` 这种黏在一起的假词（实测踩到）
    sep = ""
    if head[-1].isascii() and head[-1].isalnum() \
            and nxt[0].isascii() and nxt[0].isalnum():
        sep = " "
    return head + sep + nxt


def clip_title(text: str) -> tuple[str, bool]:
    """限到标题长度。返回 `(标题, 是否被截断)`。

    调用方必须把"被截断"这件事变成**可见的**（记 warning）——
    一个像是原文、其实是半句话的标题，比一个明显是占位的标题更危险。
    """
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if len(t) <= TITLE_MAX:
        return t, False
    return t[:TITLE_MAX].rstrip() + "…", True


def looks_like_title(text: str) -> bool:
    """一段文字像不像标题（用于"首行/首段能不能当标题"的兜底判断）。"""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not (4 <= len(t) <= 200):
        return False
    if re.fullmatch(r"[\d\s\-/.:年月日]+", t):      # 纯日期/页码
        return False
    return not is_junk_title(t)
