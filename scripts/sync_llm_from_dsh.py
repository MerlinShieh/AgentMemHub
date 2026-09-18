"""从 DSH 配置同步 LLM 接入信息到 agentmemhub.yaml。

为什么需要这个脚本：DSH 的 provider 配置在
`~/.dsh/settings.yaml`（`llm-pi-ai.providers.<name>`：baseURL / apiKeyEnv /
models），密钥在 `~/.dsh/.credentials.yaml`（`refs.<apiKeyEnv>`）。手工复制到
agentmemhub.yaml 易错，而且会把明文密钥留在终端与对话记录里。

本脚本直接读配置、推导 OpenAI 兼容端点、**原地更新** yaml 的 `llm` 段
（保留注释与其它内容，不用 yaml.safe_dump 整篇重写）。

安全：apiKey 只写入 agentmemhub.yaml（该文件已 gitignore，密钥不入库、
不回显）。可用 `--dry-run` 预览将写入哪些字段（密钥只显示长度）。

用法：
    python scripts/sync_llm_from_dsh.py --list
    python scripts/sync_llm_from_dsh.py --provider command
    python scripts/sync_llm_from_dsh.py --provider command \\
        --model deepseek/deepseek-v4.1-flash --thinking disabled
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_YAML = PROJECT_ROOT / "agentmemhub.yaml"
DSH_HOME = Path.home() / ".dsh"
SETTINGS = DSH_HOME / "settings.yaml"
CREDS = DSH_HOME / ".credentials.yaml"

#: 这些 provider 需要自定义 UA 才能过 Cloudflare（实测 403 error code 1010）
_UA_PROVIDERS = {"command": "AgentMemHub/1.0"}

#: DSH 里部分 provider 只声明了 apiKeyEnv、没写 baseURL（用客户端内置默认值）。
#: 这里补上对应的 OpenAI 兼容端点，供本脚本推导 chat/completions。
_DEFAULT_BASE = {
    "deepseek": "https://api.deepseek.com",
    "command": "https://api.commandcode.ai/provider/v1",
}


def _load_yaml(p: Path) -> dict:
    import yaml
    if not p.exists():
        raise SystemExit(f"配置文件不存在：{p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def list_providers() -> list[tuple[str, str, str, int]]:
    """[(key, displayName, baseURL, 模型数)]。"""
    st = _load_yaml(SETTINGS)
    provs = ((st.get("llm-pi-ai") or {}).get("providers") or {})
    out = []
    for key, p in provs.items():
        if not isinstance(p, dict):
            continue
        out.append((key, str(p.get("displayName") or ""),
                    str(p.get("baseURL") or ""),
                    len(p.get("models") or [])))
    return out


def resolve(provider: str, model: str = "") -> dict:
    """解析出 endpoint / api_key / model / headers（密钥不回显）。"""
    st = _load_yaml(SETTINGS)
    provs = ((st.get("llm-pi-ai") or {}).get("providers") or {})
    p = provs.get(provider)
    if not isinstance(p, dict):
        raise SystemExit(f"DSH 里没有 provider {provider!r}；用 --list 查看可用项")

    base = str(p.get("baseURL") or "").rstrip("/")
    if not base:
        base = _DEFAULT_BASE.get(provider, "")
    if not base:
        raise SystemExit(f"provider {provider} 没有 baseURL（且无内置默认值）")
    endpoint = base + "/chat/completions"

    key_env = str(p.get("apiKeyEnv") or "")
    key = ""
    if key_env:
        refs = (_load_yaml(CREDS).get("refs") or {})
        key = str(refs.get(key_env) or "").strip()
    if not key:
        raise SystemExit(f"未能从 .credentials.yaml 取到密钥（apiKeyEnv={key_env!r}）")

    if not model:
        models = p.get("models") or []
        if not models:
            raise SystemExit(f"provider {provider} 没有配置模型，请用 --model 指定")
        model = str(models[0].get("id") or "")
    elif model not in {str(m.get("id")) for m in (p.get("models") or [])}:
        print(f"⚠️  模型 {model!r} 不在 DSH 该 provider 的模型清单里，仍将写入")

    return {"endpoint": endpoint, "api_key": key, "model": model,
            "provider": provider,
            "user_agent": _UA_PROVIDERS.get(provider, "")}


def _fields_block(info: dict, thinking: str, indent: str) -> str:
    out = indent + 'endpoint: "%s"\n' % info["endpoint"]
    out += indent + 'api_key: "%s"\n' % info["api_key"]
    out += indent + 'model: "%s"\n' % info["model"]
    if thinking:
        out += indent + 'thinking: "%s"\n' % thinking
    return out


def patch_yaml(info: dict, *, thinking: str = "", dry_run: bool = False,
               section: str = "llm") -> None:
    """原地更新 agentmemhub.yaml（保留注释与其他内容）。

    section="llm"  → 顶层 `llm:` 段（蒸馏 / 评分共用）
    section="wiki" → `wiki.llm:` 子段（Wiki 编译独立覆盖）
    """
    if not TARGET_YAML.exists():
        raise SystemExit(f"目标不存在：{TARGET_YAML}（先从 .example 复制一份）")
    text = TARGET_YAML.read_text(encoding="utf-8")

    if section == "wiki":
        mw = re.search(r"(?m)^wiki:[ \t]*\n", text)
        llm_block = "  llm:\n" + _fields_block(info, thinking, "    ")
        if mw:
            wstart = mw.end()
            nxt = re.search(r"(?m)^[A-Za-z_][\w-]*:", text[wstart:])
            wend = wstart + (nxt.start() if nxt else len(text) - wstart)
            wblock = text[wstart:wend]
            lpat = re.compile(r"(?ms)^  llm:\n(?:    [^\n]*\n)*")
            if lpat.search(wblock):
                wblock = lpat.sub(lambda _m: llm_block, wblock, count=1)
            else:
                wblock = llm_block + wblock
            text = text[:wstart] + wblock + text[wend:]
        else:
            text = text.rstrip() + "\n\n# Wiki 编译独立配置（与蒸馏分开）\nwiki:\n" + llm_block
    else:
        m = re.search(r"(?m)^llm:[ \t]*\n", text)
        if not m:
            raise SystemExit("在 agentmemhub.yaml 中找不到顶层 `llm:` 段")
        start = m.end()
        nxt = re.search(r"(?m)^[A-Za-z_][\w-]*:", text[start:])
        end = start + (nxt.start() if nxt else len(text) - start)
        block = text[start:end]

        def set_field(b: str, key: str, value: str) -> str:
            line = '  %s: "%s"\n' % (key, value)
            pat = re.compile(r"(?m)^[ \t]*%s:[^\n]*\n" % re.escape(key))
            return pat.sub(lambda _m: line, b, count=1) if pat.search(b) else line + b

        new = block
        new = set_field(new, "endpoint", info["endpoint"])
        new = set_field(new, "api_key", info["api_key"])
        new = set_field(new, "model", info["model"])
        if thinking:
            new = set_field(new, "thinking", thinking)
        ua = info.get("user_agent")
        if ua:
            hpat = re.compile(r"(?ms)^[ \t]*headers:\n(?:[ \t]+[^\n]*\n)*")
            hblock = '  headers:\n    User-Agent: "%s"      # 过 Cloudflare 1010\n' % ua
            if hpat.search(new):
                new = hpat.sub(lambda _m: hblock, new, count=1)
            else:
                new = new.rstrip("\n") + "\n" + hblock
        text = text[:start] + new + text[end:]

    print("将写入 agentmemhub.yaml 的 %s 段：" % section)
    print("  provider    : %s" % info["provider"])
    print("  endpoint    : %s" % info["endpoint"])
    print("  model       : %s" % info["model"])
    print("  api_key     : ***（%d 字符，不回显）" % len(info["api_key"]))
    if thinking:
        print("  thinking    : %s" % thinking)
    if dry_run:
        print("\n(--dry-run，未写入)")
        return
    TARGET_YAML.write_text(text, encoding="utf-8")
    print("\n✅ 已更新 %s 的 %s 段" % (TARGET_YAML, section))


def main() -> int:
    ap = argparse.ArgumentParser(description="从 DSH 配置同步 LLM 接入信息")
    ap.add_argument("--list", action="store_true", help="列出 DSH 里可用的 provider")
    ap.add_argument("--provider", default="", help="provider key（如 command / deepseek）")
    ap.add_argument("--model", default="", help="模型 id（默认取该 provider 的第一个）")
    ap.add_argument("--thinking", default="", choices=["", "enabled", "disabled"],
                    help="同时写入思考模式开关")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    ap.add_argument("--section", default="llm", choices=["llm", "wiki"],
                    help="写入哪一段：llm=蒸馏/评分共用（默认）；wiki=Wiki 编译独立覆盖")
    args = ap.parse_args()

    if args.list or not args.provider:
        rows = list_providers()
        print("%-22s %-18s %6s  %s" % ("provider", "显示名", "模型数", "baseURL"))
        for k, name, base, n in rows:
            print("%-22s %-18s %6d  %s" % (k, name[:18], n, base))
        if not args.provider:
            print("\n用 --provider <key> 选择要写入 agentmemhub.yaml 的 provider。")
        return 0

    info = resolve(args.provider, args.model)
    patch_yaml(info, thinking=args.thinking, dry_run=args.dry_run,
               section=args.section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
