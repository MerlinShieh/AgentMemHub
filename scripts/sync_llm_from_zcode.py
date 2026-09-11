"""从 ZCode 客户端配置同步 LLM 接入信息到 agentmemhub.yaml。

背景：ZCode 的 provider 配置在 `<用户目录>/.zcode/v2/config.json`，含 baseURL 与
apiKey。手工复制 key 到 agentmemhub.yaml 易错且会在终端/对话里留下明文——
本脚本直接读配置、推导 OpenAI 兼容端点、原地更新 yaml 的 llm 段（**保留注释
与其他内容**，不用 yaml.safe_dump 重写）。

安全：apiKey 只写入 agentmemhub.yaml（该文件已 gitignore，密钥不入库）；
终端输出一律脱敏（只显示前后各 4 位）。

用法：
  uv run python scripts/sync_llm_from_zcode.py --list
  uv run python scripts/sync_llm_from_zcode.py --provider opencode-go-coding-plan \
      --model deepseek-flash
  uv run python scripts/sync_llm_from_zcode.py --provider <key> --model <m> --dry-run
  uv run python scripts/sync_llm_from_zcode.py --provider <key> --model <m> --show

环境变量：ZCODE_CONFIG 可指定 config.json 路径（默认自动探测）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_YAML = PROJECT_ROOT / "agentmemhub.yaml"


def _mask(secret: str) -> str:
    """脱敏显示：只留首尾各 4 位（过短则全遮）。"""
    if not secret:
        return "(空)"
    if len(secret) <= 12:
        return secret[:2] + "*" * max(len(secret) - 2, 0)
    return f"{secret[:4]}…{secret[-4:]}（{len(secret)} 位）"


def find_zcode_config() -> Path | None:
    """定位 ZCode 客户端 config.json。"""
    env = os.environ.get("ZCODE_CONFIG", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    candidates = [
        Path.home() / ".zcode" / "v2" / "config.json",
        Path.home() / ".zcode" / "config.json",
        Path.home() / "AppData" / "Roaming" / "zcode" / "config.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_providers(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("provider") or {}


def chat_endpoint(base_url: str) -> str:
    """由 baseURL 推导 OpenAI 兼容的 chat/completions 端点。"""
    b = (base_url or "").rstrip("/")
    if not b:
        return ""
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def list_providers(providers: dict) -> int:
    print(f"可用 provider（仅列 openai-compatible，即本引擎可直接使用的）：\n")
    n = 0
    for key, p in providers.items():
        opts = p.get("options") or {}
        if str(p.get("kind") or "") != "openai-compatible":
            continue
        n += 1
        models = list((p.get("models") or {}).keys())
        print(f"  {key}")
        print(f"    {p.get('name')}  enabled={p.get('enabled')}")
        print(f"    endpoint = {chat_endpoint(opts.get('baseURL') or '')}")
        print(f"    apiKey   = {_mask(opts.get('apiKey') or '')}")
        print(f"    models   = {models}")
        print()
    if not n:
        print("  （未找到 openai-compatible provider）")
    return 0


# ── yaml 原地更新（保留注释；不用 safe_dump 重写）──────────────────────

_KEYS = ("endpoint", "api_key", "model")


def default_headers_for(endpoint: str) -> dict[str, str]:
    """provider 特定必需请求头的实测结论（配置驱动，不写死在客户端里）。

    - 通用：带 User-Agent。缺省时 urllib 发 `Python-urllib/3.x`，
      部分网关（Cloudflare）直接 403 error code 1010。
    - OpenCode Go（opencode.ai/zen/go）：实测必须带 `x-opencode-session`
      （用于路由亲和），否则 400 MissingSessionID——
      见 https://opencode.ai/docs/go/#where-can-i-use-it
    """
    ep = endpoint or ""
    h = {"User-Agent": "AgentMemHub/1.0"}
    if "opencode.ai" in ep:
        h["User-Agent"] = "OpenCode/1.0.0"            # 实测可通过 Cloudflare
        h["x-opencode-session"] = "agentmemhub-distill"
    return h


def update_llm_section(text: str, values: dict[str, str],
                       headers: dict[str, str] | None = None) -> tuple[str, bool]:
    """更新 yaml 的 llm 段（顶层）。返回 (新文本, 是否新增了段)。

    标量键只替换**值**（保留行内注释与缩进）；headers 子块整体重建
    （该块由本脚本生成，无用户注释需要保留）；缺失键补行。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^llm:\s*(#.*)?$", line):
            start = i
            break

    if start is None:
        block = ["llm:"] + [f'  {k}: "{values.get(k, "")}"' for k in _KEYS]
        if headers:
            block.append("  headers:")
            block += [f'    {k}: "{v}"' for k, v in headers.items()]
        return "\n".join(lines + [""] + block) + "\n", True

    # 段范围：到下一个顶层键（非缩进、非空）为止
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip() and not lines[j][0].isspace():
            end = j
            break

    body = lines[start + 1:end]

    # 1) 标量键：替换值，保留行内注释
    seen: set[str] = set()
    for i, line in enumerate(body):
        m = re.match(r"^(\s+)(endpoint|api_key|model)\s*:\s*(.*)$", line)
        if not m:
            continue
        indent, key, rest = m.groups()
        cm = re.search(r"\s+#.*$", rest)
        body[i] = f'{indent}{key}: "{values.get(key, "")}"' + (cm.group(0) if cm else "")
        seen.add(key)

    # 2) 移除旧 headers 块（headers: 行 + 其 4 空格缩进的子行）
    cleaned: list[str] = []
    skip_child = False
    for ln in body:
        if re.match(r"^\s+headers:\s*(#.*)?$", ln):
            skip_child = True
            continue
        if skip_child and (not ln.strip() or ln.startswith("    ")):
            continue
        skip_child = False
        cleaned.append(ln)
    body = cleaned

    # 3) 补齐缺失标量 + 追加 headers 块
    for k in _KEYS:
        if k not in seen:
            body.append(f'  {k}: "{values.get(k, "")}"')
    if headers:
        body.append("  headers:")
        body += [f'    {k}: "{v}"' for k, v in headers.items()]

    lines[start + 1:end] = body
    return "\n".join(lines) + "\n", False


def main() -> int:
    ap = argparse.ArgumentParser(description="从 ZCode 配置同步 LLM 设置到 agentmemhub.yaml")
    ap.add_argument("--list", action="store_true", help="列出 ZCode 里可用的 provider")
    ap.add_argument("--provider", default="", help="provider key（见 --list）")
    ap.add_argument("--model", default="", help="模型名")
    ap.add_argument("--dry-run", action="store_true", help="只显示将要写入的内容，不改文件")
    ap.add_argument("--show", action="store_true", help="写入后回显 llm 段（key 脱敏）")
    ap.add_argument("--config", default="", help="ZCode config.json 路径（默认自动探测）")
    args = ap.parse_args()

    cfg_path = Path(args.config).expanduser() if args.config else find_zcode_config()
    if cfg_path is None or not cfg_path.is_file():
        print("✗ 未找到 ZCode 配置（可用 --config 指定，或设 ZCODE_CONFIG）")
        return 1
    providers = load_providers(cfg_path)
    print(f"ZCode 配置：{cfg_path}\n")

    if args.list or not args.provider:
        return list_providers(providers)

    p = providers.get(args.provider)
    if p is None:
        print(f"✗ 未找到 provider：{args.provider}（用 --list 查看）")
        return 1
    opts = p.get("options") or {}
    kind = str(p.get("kind") or "")
    if kind != "openai-compatible":
        print(f"✗ provider kind={kind} 非 openai-compatible，本引擎不支持")
        return 1
    models = list((p.get("models") or {}).keys())
    if not args.model:
        print(f"✗ 需指定 --model，可选：{models}")
        return 1
    if models and args.model not in models:
        print(f"⚠ 模型 {args.model!r} 不在该 provider 的 models 列表里（仍按指定值写入）")

    endpoint = chat_endpoint(opts.get("baseURL") or "")
    api_key = opts.get("apiKey") or ""
    if not endpoint or not api_key:
        print(f"✗ provider 缺 baseURL 或 apiKey（endpoint={endpoint or '空'}，"
              f"key={_mask(api_key)}）")
        return 1

    print(f"provider : {args.provider}（{p.get('name')}）")
    print(f"endpoint : {endpoint}")
    print(f"api_key  : {_mask(api_key)}")
    print(f"model    : {args.model}")
    headers = default_headers_for(endpoint)
    if headers:
        print(f"headers  : {headers}   ← provider 必需（实测结论）")
    print()

    values = {"endpoint": endpoint, "api_key": api_key, "model": args.model}
    current = TARGET_YAML.read_text(encoding="utf-8")
    new_text, added = update_llm_section(current, values, headers)

    if args.dry_run:
        print("（--dry-run：未写入）目标区段将为：")
        _echo_section(new_text)
        return 0

    TARGET_YAML.write_text(new_text, encoding="utf-8")
    print(f"✓ 已写入 {TARGET_YAML}（{'新增 llm 段' if added else '更新 llm 段'}）")
    print("  该文件已被 .gitignore，密钥不会入库。")
    if args.show:
        _echo_section(new_text)
    return 0


def _echo_section(text: str) -> None:
    """回显 llm 段（api_key 脱敏）。"""
    in_block = False
    for line in text.splitlines():
        if re.match(r"^llm:", line):
            in_block = True
        elif in_block and line.strip() and not line[0].isspace():
            break
        if in_block:
            masked = re.sub(r'(api_key:\s*")([^"]*)(")',
                            lambda m: m.group(1) + _mask(m.group(2)) + m.group(3),
                            line)
            print("  " + masked)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
