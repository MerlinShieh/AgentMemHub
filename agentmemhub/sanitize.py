"""脱敏：记忆产物入库前的最后一道防线（蒸馏 prompt 之外的兜底层）。

定位：LLM 蒸馏提示词已要求"忽略并不得输出敏感内容"，但提示词可能失效——
本模块用确定性正则在**入库前**再扫一遍，命中即剥离。

两类规则：
- **凭据（secret）**：API key / token / 私钥块 / JWT / 密码赋值 —— 泄漏后果最重；
- **个人信息（pii）**：邮箱 / 手机号 / 身份证 —— 隐私风险。

原则：
- **安全优先**：宁可多剥一点，也不让凭据进长期记忆；
- 但**不做无关脱敏**：技术文本里的普通数字、路径、版本号不受影响
  （误报会毁掉记忆价值，见 tests/test_sanitize.py 的"不误报"用例）；
- 剥离后仍有实质内容 → 保留该记忆；剥空了 → 由调用方丢弃整条。

与 scripts/sensitive_scan.py 的分工：那个扫**源码仓库**（防凭据提交入库），
本模块扫**记忆文本**（防凭据沉淀进记忆）。规则各自演进，不互相依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: 凭据类（高危；泄漏后果最重）
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 私钥块放最前：多行整体匹配，避免被其它规则先切开
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("openai_key", re.compile(r"\b(?:sk|pk)-[A-Za-z0-9]{16,}")),
    ("nvidia_key", re.compile(r"nvapi-[A-Za-z0-9\-_]{16,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=\-]{16,}", re.I)),
    ("password_assign", re.compile(
        r"(?:password|passwd|pwd|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}", re.I)),
)

#: 个人信息类（隐私）
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("cn_id", re.compile(
        r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")),
)

#: 占位符（剥离后留下的标记，便于人工复核"这里原本有敏感内容"）
PLACEHOLDER = "[已脱敏]"


@dataclass(frozen=True)
class Finding:
    """一处命中：类别（secret/pii）、规则名、命中文本（截断供日志）。"""

    kind: str          # "secret" | "pii"
    pattern: str       # 规则名，如 "openai_key"
    snippet: str       # 命中文本（超长截断，避免日志泄漏全文）

    def as_dict(self) -> dict:
        return {"kind": self.kind, "pattern": self.pattern, "snippet": self.snippet}


def _rules(include_pii: bool) -> tuple[tuple[str, str, re.Pattern[str]], ...]:
    rules = [("secret", name, pat) for name, pat in SECRET_PATTERNS]
    if include_pii:
        rules += [("pii", name, pat) for name, pat in PII_PATTERNS]
    return tuple(rules)


def _snip(text: str, limit: int = 16) -> str:
    """命中文本的安全摘要（只留头尾，避免把完整凭据写进日志）。"""
    t = text.replace("\n", " ")
    if len(t) <= limit:
        return t
    return t[:8] + "…" + t[-4:]


def scan(text: str | None, *, include_pii: bool = True) -> list[Finding]:
    """扫描文本，返回全部命中（不修改文本）。空文本返回空列表。"""
    if not text:
        return []
    out: list[Finding] = []
    for kind, name, pat in _rules(include_pii):
        for m in pat.finditer(text):
            out.append(Finding(kind=kind, pattern=name, snippet=_snip(m.group(0))))
    return out


def redact(text: str | None, *, include_pii: bool = True,
           placeholder: str = PLACEHOLDER) -> tuple[str, list[Finding]]:
    """剥离敏感片段：命中替换为占位符。返回 (脱敏后文本, 命中列表)。

    每条规则按序替换（私钥块最先，避免多行内容被后续规则切碎）。
    """
    if not text:
        return (text or ""), []
    findings: list[Finding] = []
    out = text
    for _kind, name, pat in _rules(include_pii):
        def _sub(m: re.Match[str]) -> str:
            findings.append(Finding(
                kind=_kind, pattern=name, snippet=_snip(m.group(0))))
            return placeholder
        out = pat.sub(_sub, out)
    return out, findings


def is_clean(text: str | None, *, include_pii: bool = True) -> bool:
    """文本是否无敏感命中（供断言/快速判断）。"""
    return not scan(text, include_pii=include_pii)


def has_substance(text: str | None, *, min_chars: int = 4) -> bool:
    """剥离后是否仍有实质内容：去掉占位符与空白后达到最小长度。

    用于"剥空了就丢弃整条"的判定，避免入库一条只剩 [已脱敏] 的空壳。
    阈值取 4：中文信息密度高（"采用 RRF 融合"即可独立成条），
    英文短词（"ok"/"yes"）仍会被正确判为无实质。
    """
    if not text:
        return False
    residue = text.replace(PLACEHOLDER, "")
    residue = re.sub(r"[\s\W_]+", "", residue)
    return len(residue) >= min_chars
