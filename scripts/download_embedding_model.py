#!/usr/bin/env python3
"""Restore the MemOS local embedding model files.

The engine's local embeddings are served by transformers.js from
`memOS/apps/memos-local-plugin/node_modules/@huggingface/transformers/models/`.
That folder is NOT part of the npm dependencies: any `npm install` (or a fresh
clone + install) wipes it, so the embedding model must be re-downloaded after
dependency reinstall — otherwise the engine falls back to a poor/default
embedder (or fails to embed at all).

Usage:
    uv run python scripts/download_embedding_model.py
    uv run python scripts/download_embedding_model.py --model Xenova/paraphrase-multilingual-MiniLM-L12-v2
    uv run python scripts/download_embedding_model.py --base https://hf-mirror.com --retries 10

Defaults restore the project's configured model (Xenova/bge-small-zh-v1.5).
The download is resumable (Range requests), verifies final size against
Content-Length, and honors HTTPS_PROXY/HTTP_PROXY env vars when set.
"""
import argparse
import os
import sys
import time
import urllib.request
import ssl
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FILES = ["config.json", "tokenizer.json", "tokenizer_config.json", "onnx/model_quantized.onnx"]

DEFAULT_BASE = "https://huggingface.co"
DEFAULT_MODEL = "Xenova/bge-small-zh-v1.5"
REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = (
    REPO_ROOT
    / "memOS/apps/memos-local-plugin/node_modules/@huggingface/transformers/models"
)


def fetch(url: str, out: Path, retries: int) -> bool:
    if out.exists() and out.stat().st_size > 0:
        print(f"skip existing {out.name}", flush=True)
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    ctx = ssl.create_default_context()
    for attempt in range(1, retries + 1):
        try:
            size = out.stat().st_size if out.exists() else 0
            headers = {"User-Agent": "Mozilla/5.0"}
            if size > 0:
                headers["Range"] = f"bytes={size}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                if r.status == 200:
                    total = int(r.headers.get("Content-Length", 0))
                    size = 0  # server ignored Range; restart from zero
                elif r.status == 206:
                    total = int(r.headers.get("Content-Range", "/").split("/")[-1])
                else:
                    raise RuntimeError(f"HTTP {r.status}")
                with open(out, "ab" if size > 0 else "wb") as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
            actual = out.stat().st_size
            print(f"ok {out.name} {actual}/{total} bytes", flush=True)
            if total and actual >= total:
                return True
            print(f"incomplete {actual}/{total}, resuming next attempt", flush=True)
            time.sleep(2)
        except Exception as e:
            print(f"  attempt {attempt}: {e!r}", flush=True)
            time.sleep(3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL, help="HF model id (default: bge-small-zh-v1.5)")
    ap.add_argument("--base", default=DEFAULT_BASE, help="HF base URL, e.g. https://hf-mirror.com")
    ap.add_argument("--retries", type=int, default=6)
    args = ap.parse_args()

    dest = MODELS_DIR.joinpath(*args.model.split("/"))
    ok_all = True
    for f in FILES:
        url = f"{args.base.rstrip('/')}/{args.model}/resolve/main/{f}"
        if not fetch(url, dest / f, args.retries):
            ok_all = False
    print("ALL_OK" if ok_all else "SOME_FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())