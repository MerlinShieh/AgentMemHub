# -*- coding: utf-8 -*-
"""从 `agentmemhub.yaml` **原文**提取「键 → 值 + 注释」，供面板配置页只读展示。

为什么不用 `yaml.safe_load`
==========================
它把注释全丢了 —— 而本项目的 yaml 恰恰用注释承载了"这一项为什么这么配、
改了会怎样"，那才是配置页最该给人看的东西。只剩键和值的话，面板不过是把
文件内容换个地方重排，反而更难查。

为什么不用 ruamel.yaml
=====================
只为读注释引入一个新依赖不划算（它还会改变 round-trip 写盘行为，涉及数据
安全的依赖能不加就不加）。本项目 yaml 的结构足够简单，**行扫描**够用。

有意接受的局限（`tests/test_config_doc.py` 里写明）
==================================================
- 不解析锚点/别名（`&` / `*`）、多文档（`---`）、块标量（`|` / `>`）
- 注释归属：连续的 `#` 行归给其下方第一个键；行尾 `#` 归本行；
  两者都有时按"上方说明 + 行尾补充"拼接
- 空行会**清空**待归属注释 —— 否则上一节的说明会串到下一节第一个键上

实测本项目 `agentmemhub.yaml`（280 行）不含上述任何未支持结构，
`test_与_yaml_safe_load_叶子一致` 用真实文件做了交叉校验。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 环境变量覆盖表：这些键在运行期的生效值可能**不是** yaml 里写的那个。
# 配置页必须标出来，否则用户改完 yaml 发现"没生效"会以为是 bug。
# 对应 `config.Config` 里各 property 的 `self._env.get(...)` 分支。
ENV_OVERRIDES: tuple[dict[str, str], ...] = (
    {"path": "web.port", "env": "AGENTMEMHUB_PORT"},
    {"path": "data_dir", "env": "AGENTMEM_HUB_DATA_DIR"},
    {"path": "db_path", "env": "AGENTMEMHUB_DB"},
)

_KEY_RE = re.compile(r"^(\s*)([^:#]+?)\s*:\s*(.*)$")


def _split_inline_comment(rest: str) -> tuple[str, str]:
    """把 `value   # 说明` 拆成 (value, 说明)。

    只认**空白 + #** 的行尾注释（`#` 紧跟字符时视为值的一部分，如 URL 锚点）。
    """
    idx = rest.find(" #")
    if idx < 0:
        return rest.rstrip(), ""
    return rest[:idx].rstrip(), rest[idx + 1:].strip().lstrip("#").strip()


def parse_annotated(text: str) -> list[dict[str, Any]]:
    """把 yaml 原文解析成带注释的键清单（保序、含 section 行）。

    每条：`{path, key, depth, value_raw, comment, line, is_section}`。
    `path` 是点分路径（与 `Config._get("rag", {}).get("write")` 的读法一致），
    `value_raw` 保留 yaml 里**原样**写的文本 —— 面板直接显示它，
    比用 PyYAML 反序列化再 repr 更贴近用户实际看到的文件。
    """
    entries: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []      # [(indent, key)]
    pending: list[str] = []                # 待归属的上方注释

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            pending = []                   # 空行分节：不让注释跨节串味
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            txt = stripped.lstrip("#").strip()
            if txt:
                pending.append(txt)
            continue

        m = _KEY_RE.match(line)
        if not m:
            continue                       # 非键行（本项目 yaml 里没有，兜底跳过）
        indent = len(m.group(1))
        key = m.group(2).strip().strip("\"'")
        rest, inline = _split_inline_comment(m.group(3))

        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])

        comment = " ".join(pending).strip()
        if inline:
            comment = (comment + " " + inline).strip() if comment else inline
        pending = []

        is_section = (rest == "")
        entries.append({
            "path": path,
            "key": key,
            "depth": len(stack),
            "value_raw": "" if is_section else rest,
            "comment": comment,
            "line": lineno,
            "is_section": is_section,
        })
        if is_section:
            stack.append((indent, key))
    return entries


def to_leaves(entries: list[dict[str, Any]]) -> dict[str, str]:
    """`{path: value_raw}`，只取叶子 —— 供测试与"改了哪些项"比对用。"""
    return {e["path"]: e["value_raw"] for e in entries if not e["is_section"]}


def load_annotated(path: Path | str | None = None) -> dict[str, Any]:
    """读配置文件并返回面板要的全部信息；文件不存在时 `exists=False`（不抛）。"""
    from agentmemhub.config import PROJECT_ROOT

    p = Path(path) if path else (PROJECT_ROOT / "agentmemhub.yaml")
    if not p.exists():
        return {"file": str(p), "exists": False, "lines": 0,
                "entries": [], "env_overrides": list(ENV_OVERRIDES)}
    text = p.read_text(encoding="utf-8")
    entries = parse_annotated(text)
    return {
        "file": str(p),
        "exists": True,
        "lines": len(text.splitlines()),
        "entries": entries,
        "env_overrides": list(ENV_OVERRIDES),
    }


EXAMPLE_NAME = "agentmemhub.yaml.example"


def _norm_value(v: str) -> str:
    """比较用的值归一：去引号、`~`/`null` 视作空。**只用于判等，不用于展示**。

    否则 example 写 `port: "8080"`、本地写 `port: 8080` 会被误报成"值不同"，
    噪音会盖住真问题（面板展示始终用 `value_raw` 原文）。
    """
    s = (v or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    if s.lower() in ("~", "null"):
        s = ""
    return s


def diff_example(
    local_path: Path | str | None = None,
    example_path: Path | str | None = None,
) -> dict[str, Any]:
    """对比 `agentmemhub.yaml.example` 与本地配置，找出「升级后没跟上的键」。

    解决的是**人做配置更新**时的真实痛点（不是程序写回）：项目加了新键 →
    example 跟着更新 → 而用户的本地 yaml 是手写维护的。整篇覆盖会把注释和
    自定义值一起丢掉，逐段手工对照又容易漏，漏了也不报错（那个键安静地取默认值）。
    这里**只报告，一个字都不改用户文件**。

    三类结果，语义各不相同：

    - `missing`：example 有、本地没有 —— 升级新增的键多半落在这里。
      **但不等于错**：example 里有些键本就是可选项，本地不配也正常，故只作提示。
    - `obsolete`：本地有、example 已没有 —— 可能是被改名/删除，
      也可能本地加过只对自己有用的键；报出来让人判断，不自动清理。
    - `changed`：两边都有但值不同 —— **多数是本机定制**（端口、路径、密钥、
      模型名），属正常差异，仅供参照，不要理解成"要同步成 example 的值"。

    `missing` / `obsolete` 的每项保留 `path` / `key` / `depth` / `value_raw` /
    `comment` / `line`，其中 `comment` 是 **example 里的原注释** ——
    决定"要不要跟、跟成什么值"时要的正是这段上下文。
    """
    from agentmemhub.config import PROJECT_ROOT

    local_p = Path(local_path) if local_path else (PROJECT_ROOT / "agentmemhub.yaml")
    ex_p = Path(example_path) if example_path else (PROJECT_ROOT / EXAMPLE_NAME)

    if not ex_p.exists():
        return {"example_file": str(ex_p), "example_exists": False,
                "missing": [], "obsolete": [], "changed": [],
                "counts": {"missing": 0, "obsolete": 0, "changed": 0}}

    ex_entries = parse_annotated(ex_p.read_text(encoding="utf-8"))
    local_entries = (parse_annotated(local_p.read_text(encoding="utf-8"))
                     if local_p.exists() else [])

    ex_leaves = {e["path"]: e for e in ex_entries if not e["is_section"]}
    lo_leaves = {e["path"]: e for e in local_entries if not e["is_section"]}

    missing = [ex_leaves[p] for p in ex_leaves if p not in lo_leaves]
    obsolete = [lo_leaves[p] for p in lo_leaves if p not in ex_leaves]
    changed = []
    for path, ex in ex_leaves.items():
        lo = lo_leaves.get(path)
        if lo and _norm_value(lo["value_raw"]) != _norm_value(ex["value_raw"]):
            changed.append({"path": path, "local_raw": lo["value_raw"],
                            "example_raw": ex["value_raw"],
                            "comment": ex["comment"], "line": ex["line"]})

    return {
        "example_file": str(ex_p),
        "example_exists": True,
        "local_exists": local_p.exists(),
        "missing": missing,
        "obsolete": obsolete,
        "changed": changed,
        "counts": {"missing": len(missing), "obsolete": len(obsolete),
                   "changed": len(changed)},
    }
