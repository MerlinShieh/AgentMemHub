# -*- coding: utf-8 -*-
"""内嵌数据抢救（`feed/embeddata.py`）—— 数据在 HTML 里、不在正文里的时候。

**真实素材**（2026-10-15，用户文档目录）：60 份"脚本录制工具"导出的测试报告，
HTML 157 KB、静态可见文本只有 136 字（全是「用例名称:」这类**字段标签**），
真正的数据是 `<script>` 里的 `var resultData = {…}`（27 KB）。

这个模块的价值不只是"多抽了点内容"，而是**修掉一个指标失灵**：
质量自评按"抽取覆盖率"算，给空壳打了 1.00 分 ⇒ 知识库里会躺 60 个看起来正常的空页面。
"""
from __future__ import annotations

import json

from agentmemhub.feed import embeddata as ED


# ---------------------------------------------------------------------------
# 空壳判据
# ---------------------------------------------------------------------------

def test_裸标签密度高判为空壳():
    md = "\n".join(["测试报告", "报告汇总", "用例名称:", "步骤总数:", "步骤通过:",
                    "用例跳过:", "开始时间:", "结束时间:", "详细数据"])
    assert ED.looks_like_label_shell(md) is True


def test_正常文档不判为空壳():
    md = ("# 季度评审\n\n本次评审覆盖三个模块，结论如下。\n\n"
          "第一，接口层稳定。\n第二，性能达标。\n第三，遗留两个问题。\n")
    assert ED.looks_like_label_shell(md) is False


def test_短文档不判为空壳():
    """只有两三行的文档无从判断，别乱动。"""
    assert ED.looks_like_label_shell("标题:\n作者:") is False


def test_长句以冒号结尾不算裸标签():
    """阈值是"短"：中文里 40 字已经是一句完整的话，不是字段名。

    这里必须真的写够长 —— 最初这行测试里那条"长句"只有 26 字，
    根本没越过阈值，于是它在测一个不存在的情形。
    """
    long_line = ("这是一句足够长的说明文字，长到不会被误判成只有几个字的字段标签，"
                 "它只是恰好以冒号结尾：")
    assert len(long_line) > 40
    md = "\n".join(["# 报告"] + [long_line] * 3
                   + ["正文一", "正文二", "正文三", "正文四"])
    assert ED.looks_like_label_shell(md) is False


# ---------------------------------------------------------------------------
# 内嵌数据提取
# ---------------------------------------------------------------------------

def _html_with(payload, name="resultData", pad=3000) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    filler = "x" * pad
    return ("<html><body><p>用例名称:</p><p>步骤总数:</p>"
            "<script>var %s = %s;</script>"
            "<script>var noise = '%s';</script></body></html>"
            % (name, body, filler)).encode("utf-8")


#: 测试用的小阈值 —— 生产判据是 `MIN_DATA_BYTES`（2048），但造那么大的样本
#: 会让断言难读；阈值本身可注入（见 `extract_embedded_json`）
_SMALL = 20


def test_能抽出内嵌的JSON数据():
    data = {"testResult": [{"className": "a.yaml", "methodName": "启动", "status": "成功"},
                           {"className": "a.yaml", "methodName": "退出", "status": "失败"}]}
    blocks = ED.extract_embedded_json(_html_with(data), min_bytes=_SMALL)
    assert len(blocks) == 1
    name, obj = blocks[0]
    assert name == "resultData" and obj == data


def test_小数据块不算内容():
    """太小的大半是配置（主题、埋点），不是内容 —— 别往知识库里灌。
    这里用的是**生产默认阈值**，测的就是那条闸本身。"""
    data = {"testResult": [{"a": 1}, {"a": 2}]}
    assert len(json.dumps(data, ensure_ascii=False)) < ED.MIN_DATA_BYTES
    assert ED.extract_embedded_json(_html_with(data)) == []


def test_不是严格JSON就跳过():
    html = (b"<html><script>var x = {a: 'b', c: undefined};</script>"
            + b"y" * 4000 + b"</html>")
    assert ED.extract_embedded_json(html, min_bytes=_SMALL) == []


def test_嵌套对象与字符串里的括号不会截断解析():
    data = {"a": {"b": [{"t": "含 ) 和 } 的值"}] * 3}}
    blocks = ED.extract_embedded_json(_html_with(data), min_bytes=_SMALL)
    assert blocks and blocks[0][1] == data


# ---------------------------------------------------------------------------
# JSON → markdown
# ---------------------------------------------------------------------------

def test_记录列表渲染成表格():
    rows = [{"name": "甲", "score": 90}, {"name": "乙", "score": 80}]
    md = ED.json_to_markdown(rows, label="成绩")
    assert "| name | score |" in md
    assert "| 甲 | 90 |" in md and "| 乙 | 80 |" in md
    assert "（2 条记录）" in md


def test_嵌套的记录列表也能找到():
    """真实形态：`{"testResult": [ {记录}, … ]}` —— 表格在最里面那一层。"""
    md = ED.json_to_markdown({"meta": {"v": 1}, "testResult": [{"a": 1}, {"a": 2}]})
    assert "| a |" in md and "| 1 |" in md


def test_布尔与嵌套值可读化():
    md = ED.json_to_markdown([{"ok": True, "tags": ["x", "y"]}])
    assert "| 是 |" in md and "x / y" in md


def test_竖线与换行会破坏表格_必须转义():
    md = ED.json_to_markdown([{"a": "x|y", "b": "l1\nl2"}])
    assert "x\\|y" in md and "l1 l2" in md


# ---------------------------------------------------------------------------
# rescue：三种出口
# ---------------------------------------------------------------------------

def test_不是空壳就不动它():
    assert ED.rescue(b"<html></html>", "正常的正文内容，没有标签壳的问题。") == ("", [])


def test_是空壳但没有内嵌数据时给出指引():
    html = b"<html><body><p>%s</p></body></html>" % ("字段:\n" * 20).encode()
    md = "\n".join(["报告"] + ["字段%d:" % i for i in range(10)])
    out, warns = ED.rescue(html, md)
    assert out == "" and warns
    assert "重新导出" in warns[0] or "另存" in warns[0]


def test_是空壳且有数据时替换成有内容的正文():
    data = {"rows": [{"步骤": "清除数据", "结果": "成功"},
                     {"步骤": "启动应用", "结果": "成功"}]}
    html = _html_with(data)
    md = "\n".join(["测试报告"] + ["字段%d:" % i for i in range(10)])
    out, warns = ED.rescue(html, md, label="测试报告", min_bytes=_SMALL)
    assert out, "救回来了就该给出新正文"
    assert "| 步骤 | 结果 |" in out
    assert "内嵌数据" in out
    # 裸标签行被精简掉了（数据已取回，骨架只剩噪声）
    assert "字段0:" not in out
    # 但标题这类有用信息保留
    assert "测试报告" in out
    assert warns and "取回" in warns[0]
