# MemOS 平移 + 配置生效 综合状态核查
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

ROOT = Path(r"D:\data\vibeCoding\Agent_Memory\AgentMemHub")
MEMOS = ROOT / "memOS"

print("== 1. MemOS 项目是否整体在项目内 ==")
checks = {
    "repo 根目录存在": MEMOS.is_dir(),
    "apps/memos-local-plugin": (MEMOS / "apps" / "memos-local-plugin").is_dir(),
    "Python SDK(src/)": (MEMOS / "src").is_dir(),
    "package.json": (MEMOS / "apps" / "memos-local-plugin" / "package.json").exists(),
    "node_modules(依赖已装)": (MEMOS / "apps" / "memos-local-plugin" / "node_modules").is_dir(),
    "嵌入模型(本地 ONNX)": (MEMOS / "apps" / "memos-local-plugin" / "node_modules"
                           / "@huggingface" / "transformers" / "models" / "Xenova"
                           / "all-MiniLM-L6-v2" / "onnx" / "model_quantized.onnx").exists(),
    "引擎 home(记忆库/密码/配置)": (MEMOS / "home").is_dir(),
    "记忆库文件": (MEMOS / "home" / "data" / "memos.db").exists(),
    "viewer 密码文件已跟随": (MEMOS / "home" / ".auth.json").exists(),
    "引擎配置已跟随": (MEMOS / "home" / "config.yaml").exists(),
}
for k, v in checks.items():
    print(f"  [{'✓' if v else '✗'}] {k}")

print("\n== 2. 我们的配置是否生效（状态巡检）==")
from agentmemhub import config, memos_daemon
ov = memos_daemon.engine_request("GET", "/api/v1/overview", timeout=8)
print(f"  引擎在线: ✓ (v{ov.get('version')})")
print(f"  记忆数据: {ov.get('episodes')} episodes / {ov.get('traces')} traces")
llm = ov.get("llm") or {}
print(f"  LLM 已配置: {'✓' if llm.get('available') else '✗'} ({llm.get('model')})")
emb = ov.get("embedder") or {}
print(f"  嵌入模型: {'✓' if emb.get('available') else '✗'} ({emb.get('model')})")

c = config.config()
print(f"\n  配置推导 plugin_dir → {c.memos_plugin_dir}")
print(f"  配置推导 engine_home → {c.memos_home}")
st = memos_daemon.daemon_status()
print(f"  实际启用 plugin_dir → {st.get('plugin_dir')}")
print(f"  实际启用 engine_home → {st.get('engine_home')}")
print(f"  轻量模式(完整进化链?) → {'ON' if st.get('lightweight') else 'OFF→完整进化链'}")

print("\n== 3. 旧位置残留（供确认清理）==")
old = Path(r"C:\Users\Mulin\AppData\Local\hermes\memos-plugin")
if old.exists():
    for sub in sorted(old.iterdir()):
        sz = sum(f.stat().st_size for f in sub.rglob("*") if f.is_file()) if sub.is_dir() else sub.stat().st_size
        print(f"  {sub.name}: {'目录' if sub.is_dir() else '文件'} ({sz/1024/1024:.1f} MB)" if sub.is_dir() else f"  {sub.name}: 文件")
else:
    print("  旧位置已不存在")