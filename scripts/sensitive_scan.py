#!/usr/bin/env python3
"""AgentMemHub 敏感信息扫描器。

在推送公开仓库前运行，扫描 git tracked 与未跟踪（未被 `.gitignore` 忽略）的文件中的
常见敏感模式（真实用户名、API key、token、真实私有路径等），确保仓库已脱敏。

规则分两档，原因是"闸门必须有人看"：
- **致命（fatal）**：命中即退出码 1，必须处理完才能推。
- **提示（soft）**：形态可疑但会命中大量文档示例，只打印不失败 ——
  宁可留给人扫一眼，也不能让噪音淹没真信号、最后整个闸门被无视。

用法：
    python scripts/sensitive_scan.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK，结果里的 ✅/❌ 会抛 UnicodeEncodeError 中断脚本——
# 而它恰好发生在**打印结论**那一步，等于"扫描完却看不到结论"。统一改 UTF-8 容错。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _username_patterns() -> dict[str, re.Pattern]:
    """本机用户名 → 扫描规则（动态取自环境，不把真实用户名写进源码）。"""
    out: dict[str, re.Pattern] = {}
    for var in ("USERNAME", "USER", "USERPROFILE"):
        name = os.environ.get(var, "") or ""
        if var == "USERPROFILE":
            name = Path(name).name
        name = name.strip()
        if not name:
            continue
        if len(name) >= 3 and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            out[f"真实用户名 ({var})"] = re.compile(rf"\b{re.escape(name)}\b", re.I)
        elif re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            # 纯 ASCII 但只有 2 个字符（`Li`、`lj`）：`\b<2字符>\b` 仍会命中
            # `label` / `lib` / `kj` 这类子串，裸字面量更糟 —— 闸门恒红等于没人看。
            # 所以**不生成致命规则**，但必须出声：静默跳过会让使用者以为
            # "这条已经查过了"（这是本脚本最危险的失效方式）。
            print(f"⚠️  环境变量 {var} 的用户名过短（{name!r}），无法生成可靠的"
                  f"致命规则；推送前请人工确认提交内容里没有出现该名", file=sys.stderr)
        elif len(name) >= 2:
            # 中文名 / 带空格 / 带特殊字符的用户名走不进 \b 正则。这里**不能静默跳过**：
            # 跳过等于这条规则根本不存在，而使用者会以为"已经查过了"。
            print(f"⚠️  环境变量 {var} 的用户名含非 ASCII 或特殊字符，"
                  f"已降级为字面量匹配（可能误报，请人工确认）", file=sys.stderr)
            out[f"真实用户名 ({var}, 字面)"] = re.compile(re.escape(name))
    return out


def _local_path_patterns() -> dict[str, re.Pattern]:
    """本机专属路径（项目根、用户目录）的动态规则。

    这两条几乎零误报：它们带真实用户名或私有目录名，不会出现在示例里。
    """
    out: dict[str, re.Pattern] = {}
    root = Path(__file__).resolve().parents[1]
    # 项目根的**上一级**（本机工作区根，如 `D:\data\vibeCoding`）：仓库里出现它
    # 同样是泄漏，而根路径规则匹配不到它（根更长，子串方向不对）。
    parent = root.parent
    for label, p in (("本机项目根路径", root), ("本机工作区根路径", parent),
                     ("本机用户目录", Path.home())):
        s = str(p)
        # 长度门槛：盘根（`D:\`）这种过短前缀会把全仓库路径都命中
        if len(s) >= 8:
            out[label] = re.compile(re.escape(s), re.I)
    return out


# 只认 `C:\Users\<名字>` 这一种"必然带真实用户名"的形态。
# 曾经写成"盘符 + 两级目录"，结果满仓库的示例（`D:/path/to`、`D:\tools\Obsidian`）
# 全被命中 —— 闸门一旦每次报十几条假阳性，就没人再认真看它了。
_WIN_PATH_RE = re.compile(
    r"\b[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}[A-Za-z0-9_.\-]+", re.I)

# 任意盘符路径（含单级）—— 噪音大，只当提示
_WIN_PATH_SOFT_RE = re.compile(r"\b[A-Za-z]:[\\/]{1,2}[A-Za-z0-9_.\-]+", re.I)


def _fatal_patterns() -> dict[str, re.Pattern]:
    return {
        **_username_patterns(),
        **_local_path_patterns(),
        # GitHub：经典 PAT（ghp_）与细粒度 PAT（github_pat_）是两套前缀，
        # 另有 gho_/ghu_/ghs_/ghr_ 几种 OAuth/App 令牌。此前只认 github_pat_，
        # 而"最经典的 ghp_"恰恰是最常见的那个。
        "GitHub 经典令牌": re.compile(r"\b(?:ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{20,}"),
        "GitHub 细粒度令牌": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        "GitLab 令牌": re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
        "Google API key (AIza)": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
        # 排除占位符（sk-test-placeholder / sk-dummy 之类），否则测试夹具天天报
        "OpenAI/厂商 key (sk-)": re.compile(
            r"\bsk-(?!test|demo|example|placeholder|dummy|fake|xxx)"
            r"[A-Za-z0-9_-]{16,}", re.I),
        "NVIDIA api key (nvapi-)": re.compile(r"nvapi-[A-Za-z0-9_-]{20,}"),
        "Bearer token": re.compile(r"Bearer [A-Za-z0-9._~+/=-]{12,}", re.I),
        "Slack token (xox)": re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
        "AWS key (AKIA)": re.compile(r"AKIA[0-9A-Z]{16}"),
        "私有 Windows 路径": _WIN_PATH_RE,
        "私有 POSIX 路径": re.compile(r"/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+/", re.I),
        "疑似真实 API key 值": re.compile(r"\bapi[_-]?key\b\s*[:=]\s*['\"]?[A-Za-z0-9]{20,}", re.I),
    }


def _soft_patterns() -> dict[str, re.Pattern]:
    """形态可疑但噪音大：只打印，不让推送失败。"""
    return {"任意盘符路径（可能误报）": _WIN_PATH_SOFT_RE}


def _snippet(text: str) -> str:
    return text if len(text) <= 24 else text[:12] + "..." + text[-6:]


def _scan(files: list[Path], patterns: dict[str, re.Pattern], skip: set[str],
          self_file: Path, tag: str) -> int:
    hits = 0
    for fp in files:
        if fp.resolve() == self_file or fp.suffix.lower() in skip:
            continue
        try:
            data = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, pat in patterns.items():
            for m in pat.finditer(data):
                line = data.count("\n", 0, m.start()) + 1
                print(f"{tag}[{label}] {fp}:{line}: {_snippet(m.group(0))}")
                hits += 1
    return hits


def main() -> int:
    # 已跟踪 + 未跟踪（但未被 .gitignore 忽略）的文件都要扫。
    # 只看 `git ls-files` 会漏掉"刚写好还没 add"的文件 —— 而那恰恰是最容易
    # 顺手带进真实路径 / API key 的时刻（写完就扫，还没提交）。
    def _git_lines(*args: str) -> list[str]:
        r = subprocess.run(["git", *args], capture_output=True, text=True)
        return [ln for ln in r.stdout.splitlines() if ln]

    seen: set[str] = set()
    files: list[Path] = []
    for f in (_git_lines("ls-files")
              + _git_lines("ls-files", "--others", "--exclude-standard")):
        if f not in seen:
            seen.add(f)
            files.append(Path(f))

    skip_ext = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".tgz", ".woff", ".woff2"}
    # 扫描器自身含规则关键词语义（用户名/路径模式由环境注入），排除避免自指误报
    self_file = Path(__file__).resolve()

    fatal = _scan(files, _fatal_patterns(), skip_ext, self_file, "")
    soft = _scan(files, _soft_patterns(), skip_ext, self_file, "· ")

    if fatal:
        print(f"\n❌ 发现 {fatal} 处敏感信息，请处理后再推送")
        return 1
    if soft:
        print(f"\n⚠️  另有 {soft} 处「任意盘符路径」：形态像，但多数是文档示例；"
              f"请人工扫一眼里面有没有真实私有目录")
    print("✅ 未发现致命敏感信息，可以安全推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())
