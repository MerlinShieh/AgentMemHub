# -*- coding: utf-8 -*-
"""`index.html` 的**结构**守护：视图容器必须在 `#app` 里。

起因是一个真实 bug（2026-10-24）：新增「设置」页时把 `#viewSettings` 拼到了
`#app` 的闭合 `</div>` **之后**，于是它跑到 flex 布局之外，切过去整页空白 ——
而 `<div` 与 `</div>` 的**数量仍然平衡**，肉眼点不出问题，`node --check`
也只看 JS。同理，容器少包一层不会报错，只会"什么都没有"。

所以这里不数标签个数，而是**算祖先链**：不管中间怎么增删，只要某个视图的
祖先里没有 `app`，就是插错了位置。

实现上顺带处理两个坑：`#jobBar` 是 `<section>` 不是 `<div>`（按 div 扫会漏），
JS 模板串里也有一堆 `<div`（不剔掉会把栈搅乱）。
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "agentmemhub" / "web" / "static"

# 任意标签：闭合位、标签名、属性串、自闭合斜杠
_ANY_TAG = re.compile(r"<(/?)([a-zA-Z][\w:-]*)\b([^>]*?)(/?)>")
_ID = re.compile(r'\bid="([^"]+)"')
_VOID = {"br", "img", "input", "hr", "meta", "link", "source", "area",
         "base", "col", "embed", "param", "track", "wbr"}

VIEW_IDS = ("viewConvs", "viewMemories", "viewWiki", "viewSettings")


def _blank(m: re.Match) -> str:
    """按原长度替换成空格，保持后续位置计算不变。"""
    return " " * (m.end() - m.start())


def _html_stripped() -> str:
    """去掉 `<script>` 与 HTML 注释（JS 模板串里的 `<div` 会干扰栈式扫描）。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"(?s)<script\b.*?</script>", _blank, html)
    return re.sub(r"(?s)<!--.*?-->", _blank, html)


def _ancestor_ids(html: str, target: str) -> list[str]:
    """返回 `#target` 所在元素的祖先 id（由内到外，无 id 的祖先跳过）。"""
    m = re.search(rf'\bid="{re.escape(target)}"', html)
    if not m:
        raise AssertionError(f"index.html 里找不到 #{target}")
    stack: list[tuple[str, str | None]] = []
    for t in _ANY_TAG.finditer(html, 0, m.start()):
        closing, name, attrs, selfclose = (t.group(1), t.group(2).lower(),
                                           t.group(3), t.group(4))
        if selfclose or name in _VOID:
            continue
        if closing:
            while stack:                      # 弹到同名标签，容忍轻微不闭合
                if stack.pop()[0] == name:
                    break
            continue
        i = _ID.search(attrs or "")
        stack.append((name, i.group(1) if i else None))
    return [i for _n, i in stack if i]


def test_所有视图容器都在_app_内():
    """每个视图都必须有 `#app` 祖先，否则切过去是一片空白。"""
    html = _html_stripped()
    for vid in VIEW_IDS:
        anc = _ancestor_ids(html, vid)
        assert "app" in anc, f"#{vid} 不在 #app 内，祖先链={anc}"


def test_任务条在_app_内且不属于任何视图():
    """全局任务条要跨标签页可见，所以必须在 `#app` 里、又在所有视图之外。"""
    html = _html_stripped()
    anc = _ancestor_ids(html, "jobBar")
    assert "app" in anc, f"#jobBar 不在 #app 内，祖先链={anc}"
    assert not (set(VIEW_IDS) & set(anc)), f"#jobBar 被包进了某个视图：{anc}"


def test_设置页与记忆溯源的关键节点齐全():
    """JS 里 `$('#id')` 拿不到元素会静默失败，这里先钉住。"""
    html = _html_stripped()
    for eid in ("cfgDrift", "cfgDriftSum", "cfgDriftToggle", "cfgDriftBody",
                "cfgList", "cfgFile", "cfgStats", "cfgSearch", "cfgFoldBtn",
                "memTimeline", "memTimelineBtn", "memoPeek", "memoPeekBody"):
        assert f'id="{eid}"' in html, f"缺少 #{eid}"


def test_视图顺序与标签顺序一致():
    """标签栏顺序（会话/记忆/知识库/设置）应与 DOM 中视图顺序一致，
    否则键盘与读屏顺序跟视觉顺序对不上。"""
    html = _html_stripped()
    pos = [html.index(f'id="{v}"') for v in VIEW_IDS]
    assert pos == sorted(pos), f"视图顺序与标签不一致：{list(zip(VIEW_IDS, pos))}"


def test_扫描器本身可信():
    """守测试自己：一个永远返回"没问题"的扫描器比没有测试更危险。"""
    assert "app" not in _ancestor_ids('<div id="app"></div><div id="vX"></div>', "vX")
    assert "app" in _ancestor_ids('<div id="app"><div id="vX"></div></div>', "vX")
    # 混用标签时也要对（#jobBar 就是 <section>）
    assert "app" in _ancestor_ids(
        '<div id="app"><section id="bar"><div id="vX"></div></section></div>', "vX")
    # 自闭合标签不该把栈带偏
    assert "app" in _ancestor_ids(
        '<div id="app"><br><img src="a"><div id="vX"></div></div>', "vX")
