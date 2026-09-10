"""从 HF 镜像拉取 ONNX 嵌入模型到 models/<dir>，断点续传 + 大小校验 + 代理兜底。

用法：
  uv run python scripts/fetch_model.py Xenova/bge-base-zh-v1.5
  uv run python scripts/fetch_model.py <repo> --dir bge-base-zh --base https://hf-mirror.com

网络经验（本机）：直连可能超时；代理 Clash 127.0.0.1:7897 兜底；
大文件下载须 Range 续传 + Content-Length 终校，防"假成功截断"。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Xenova ONNX 仓库的最小充分文件集（量化版为主，fp32 可选）
CORE_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "onnx/model_quantized.onnx",
]


def build_curl_cmd(url: str, dest: Path, proxy: str | None = None) -> list[str]:
    """curl 命令构造（纯函数便于测试）。

    为什么用 curl 不用 urllib：本机注册表系统代理指向 Clash 7897（可能未运行），
    urllib 会静默继承导致 10061 拒连 / 走死代理超时（实测两轮）；
    curl 默认不读注册表，-C - 原生断点续传，-L 跟随镜像站 307。
    """
    cmd = ["curl", "-L", "--fail", "--retry", "3", "-C", "-",
           "--connect-timeout", "15", "--max-time", "3600",
           "-o", str(dest), url]
    if proxy:
        cmd[1:1] = ["--proxy", proxy]
    return cmd


def download(url: str, dest: Path, *, proxy: str | None = None,
             timeout: int = 3700) -> int:
    """下载单文件到 .part（curl 续传），成功后原子改名，返回终大小。"""
    import subprocess

    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(build_curl_cmd(url, part, proxy),
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not part.exists() or part.stat().st_size == 0:
        raise IOError(
            f"下载失败 rc={r.returncode}: {url}\n{(r.stderr or '')[-300:]}")
    part.rename(dest)
    return dest.stat().st_size


def fetch(repo: str, model_dir: Path, base: str, proxy: str | None) -> list[tuple[str, int]]:
    results = []
    for rel in CORE_FILES:
        url = f"{base.rstrip('/')}/{repo}/resolve/main/{rel}"
        dest = model_dir / rel
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  skip（已存在） {rel} {dest.stat().st_size}B")
            results.append((rel, dest.stat().st_size))
            continue
        print(f"  下载 {rel} …")
        try:
            size = download(url, dest, proxy=proxy)
        except (OSError, IOError, TimeoutError) as ex:
            if proxy:
                raise
            print(f"  直连失败（{type(ex).__name__}），尝试代理 127.0.0.1:7897")
            size = download(url, dest, proxy="http://127.0.0.1:7897")
        results.append((rel, size))
    return results


def registry_entry(model_id: str, dim: int, repo: str,
                   quantized: bool = True) -> dict:
    return {
        "path": f"models/{model_id}",
        "dim": dim,
        "pooling": "cls",
        "normalize": True,
        "quantized": quantized,
        "maxTokens": 512,
        "language": "zh",
        "queryPrefix": "",
        "source": f"{repo} (HF, quantized ONNX)",
        "verified": "",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下载 ONNX 嵌入模型到 models/")
    ap.add_argument("repo", help="HF repo id，如 Xenova/bge-base-zh-v1.5")
    ap.add_argument("--dir", default=None, help="本地模型 id（默认取 repo 尾段）")
    ap.add_argument("--base", default="https://hf-mirror.com")
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args(argv)

    model_id = args.dir or args.repo.split("/")[-1]
    model_dir = PROJECT_ROOT / "models" / model_id
    print(f"fetch {args.repo} -> {model_dir}")
    sizes = fetch(args.repo, model_dir, args.base, args.proxy)

    # 冒烟：用本项目 Embedder 实测维度（注册表 dim 以此为准）
    sys.path.insert(0, str(PROJECT_ROOT))
    import logging

    from agentmemhub.rag.config import ModelSpec
    from agentmemhub.rag.embedder import OnnxEmbedder

    cfg = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    dim = int(cfg["hidden_size"])
    spec = ModelSpec(id=model_id, path=model_dir, dim=dim, pooling="cls",
                     normalize=True, quantized=True, max_tokens=512,
                     language="zh")
    emb = OnnxEmbedder(spec, log=logging.getLogger("smoke"))
    v = emb.encode_passages(["模型冒烟测试"])
    got = v.shape[1]
    if got != dim:
        print(f"冒烟失败：config 声明 dim={dim} 实测 {got}", file=sys.stderr)
        return 2
    entry = registry_entry(model_id, dim, args.repo)
    entry["verified"] = f"smoke ok dim={dim}"
    reg_file = PROJECT_ROOT / "models.json"
    reg = json.loads(reg_file.read_text(encoding="utf-8"))
    reg["models"][model_id] = entry
    reg_file.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(json.dumps({"registered": model_id, "dim": dim, "files": sizes,
                      "note": "models.json 已登记；reembed --model 建向量后改 active 即切换"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
