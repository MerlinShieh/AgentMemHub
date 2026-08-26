#!/usr/bin/env python3
"""AgentMemHub 敏感信息扫描器。

在推送公开仓库前运行，扫描所有 git tracked 文件中的常见敏感模式
（真实用户名、API key、token、真实私有路径等），确保仓库已脱敏。

用法：
    python scripts/sensitive_scan.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    # 获取 tracked 文件
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout
    files = [Path(f) for f in out.splitlines() if f]

    patterns = {
        "真实用户名 (mulin)": re.compile(r"\bmulin\b", re.I),
        "GitHub PAT 令牌": re.compile(r"github_pat_[A-Za-z0-9_]+"),
        "OpenAI/厂商 key (sk-)": re.compile(r"\b(?:sk|sk-[A-Za-z0-9]|pk)-[A-Za-z0-9]{12,}"),
        "NVIDIA api key (nvapi-)": re.compile(r"nvapi-[A-Za-z0-9]+"),
        "Bearer token": re.compile(r"Bearer [A-Za-z0-9._~+/=-]{12,}", re.I),
        "Slack token (xox)": re.compile(r"xox[baprs]-[A-Za-z0-9]+"),
        "AWS key (AKIA)": re.compile(r"AKIA[0-9A-Z]{16}"),
        "私有 Windows 路径": re.compile(r"[A-Z]:\\Users|D:\\data|C:\\Users", re.I),
        "私有 POSIX 路径": re.compile(r"/home/|/Users/[A-Za-z]+(?<!Authors)/|/data/vibe", re.I),
        "疑似真实 API key 值": re.compile(r"\bapi[_-]?key\b\s*[:=]\s*['\"]?[A-Za-z0-9]{20,}", re.I),
    }

    skip_ext = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".tgz", ".woff", ".woff2"}
    # 扫描器自身包含规则关键词（mulin/路径模式），排除避免自指误报
    self_file = Path(__file__).resolve()
    found_any = False
    for fp in files:
        if fp.resolve() == self_file:
            continue
        if fp.suffix.lower() in skip_ext:
            continue
        try:
            data = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, pat in patterns.items():
            for m in pat.finditer(data):
                line = data.count("\n", 0, m.start()) + 1
                snippet = m.group(0)
                if len(snippet) > 24:
                    snippet = snippet[:12] + "..." + snippet[-6:]
                print(f"[{label}] {fp}:{line}: {snippet}")
                found_any = True

    if not found_any:
        print("✅ 未发现敏感信息，可以安全推送")
        return 0
    print("\n⚠️  发现潜在敏感信息，请处理后再推送")
    return 1


if __name__ == "__main__":
    sys.exit(main())
