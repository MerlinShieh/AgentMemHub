# -*- coding: utf-8 -*-
"""迷你 markdown 渲染器（`agentmemhub/mdmini.py`，面板「知识库」正文用）。

守两件事：
① **结构**：标题 / 段落 / 列表 / 表格 / 代码块 / 引用 / 行内标记能认出来；
② **安全**：内容里的 HTML 一律转义、链接只放行 http(s) ——
   这正是"先转义、再套结构"这条路线存在的理由（外部投喂内容会进这个渲染器）。
"""
from agentmemhub import mdmini


def test_标题分级():
    html = mdmini.render_body("# 一级\n\n## 二级\n\n#### 四级")
    assert "<h1>一级</h1>" in html
    assert "<h2>二级</h2>" in html
    assert "<h4>四级</h4>" in html


def test_段落与行内标记():
    html = mdmini.render_body("这是**粗体**与`代码`还有*斜体*。")
    assert "<strong>粗体</strong>" in html
    assert "<code>代码</code>" in html
    assert "<em>斜体</em>" in html
    assert html.startswith("<p>")


def test_无序与有序列表():
    ul = mdmini.render_body("- 甲\n- 乙")
    assert "<ul>" in ul and ul.count("<li>") == 2
    ol = mdmini.render_body("1. 一\n2. 二")
    assert "<ol>" in ol and ol.count("<li>") == 2


def test_管道表格():
    md = "| 名称 | 值 |\n| --- | --- |\n| a | 1 |\n| b | 2 |"
    html = mdmini.render_body(md)
    assert "<table" in html
    assert "<th>名称</th>" in html and html.count("<td>") == 4


def test_表格列数不齐时补齐():
    md = "| a | b |\n| --- | --- |\n| 只有一列 |"
    html = mdmini.render_body(md)
    assert html.count("<td>") == 2, "缺列要补空，别让表格塌陷"


def test_代码块内容不参与解析():
    html = mdmini.render_body("```py\n# 这不是标题\n**不是粗体**\n```")
    assert "<pre" in html
    assert "# 这不是标题" in html          # 原样保留（代码块内不解析）
    assert "<strong>" not in html
    assert "<h1>" not in html


def test_引用与水平线():
    html = mdmini.render_body("> 引用一句\n\n---")
    assert "<blockquote>引用一句</blockquote>" in html
    assert "<hr>" in html


def test_裸HTML被转义_防注入():
    """**本模块存在的理由**：内容里的标签只能当文本显示。"""
    html = mdmini.render_body('<script>alert(1)</script> 与 <img src=x onerror=alert(1)>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html


def test_链接只放行http与https():
    ok = mdmini.render_body("[文档](https://example.com/a)")
    assert '<a href="https://example.com/a"' in ok
    bad = mdmini.render_body("[点我](javascript:alert(1))")
    assert "<a " not in bad, "非 http(s) 链接不许生成 a 标签"
    # 原文**保留为文本**（不是被吞掉）—— 读者看得到这里原本是个链接，只是点不了
    assert "[点我](javascript:alert(1))" in bad


def test_引用标记变成徽章():
    """L2 页面里的 `[m3236]` 是溯源锚：渲染成小徽章，并把记忆 id 放进 `data-mid`。

    `data-mid` 是面板把徽章做成**可点**（点开看原记忆）的唯一依据；
    同时徽章文本仍保留 `m3236` 原文 —— 没有 JS 时它依旧是可读的溯源标记，
    不会退化成一个点不动也读不懂的空壳。
    """
    html = mdmini.render_body("见 [m3236] 的结论")
    assert '<span class="md-cite" data-mid="3236">m3236</span>' in html


def test_多个引用逐一带id():
    """一段里多个引用各带各的 id（正则不能把 `[m1] [m22]` 合并成一个）。"""
    html = mdmini.render_body("先 [m1] 后 [m22]")
    assert 'data-mid="1"' in html
    assert 'data-mid="22"' in html
    assert html.count('class="md-cite"') == 2


def test_空内容不炸():
    assert mdmini.render_body("") == ""
    assert mdmini.render_body(None) == ""


def test_render带外壳():
    out = mdmini.render("# 标题", title="某页")
    assert out.startswith("<article") and "<h1>标题</h1>" in out
    assert "某页" in out          # aria-label（无障碍 + 便于测试定位）
