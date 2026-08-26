"""更新文档：命令与模块引用指向新的包结构（python -m agentmemhub）。"""
from pathlib import Path

DOCS = [
    "README.md",
    "ARCHITECTURE.md",
    "SECURITY.md",
    "docs/IMPLEMENTATION_REVIEW.md",
]
TMP = ["_fix_imports.py", "_rm_fork.py"]

# (old, new) 顺序替换
REPL = [
    ("python agentmemhub.py ", "python -m agentmemhub "),
    ("python agentmemhub.py", "python -m agentmemhub"),
    ("`agentmemhub.py CLI`", "`python -m agentmemhub`"),
    ("agentmemhub.py CLI", "python -m agentmemhub"),
    ("from store import Store", "from agentmemhub.store import Store"),
    ("sources.example.json（数据源示例）", ".env.example（环境变量占位示例）"),
    ("`sources.example.json`（数据源示例）", "`scripts/sensitive_scan.py`（敏感扫描）"),
    ("`sources.example.json`", "`.env.example`"),
    ("sources.example.json", ".env.example"),
]

for doc in DOCS:
    p = Path(doc)
    if not p.exists():
        continue
    text = p.read_text(encoding="utf-8")
    new = text
    for old, repl in REPL:
        new = new.replace(old, repl)
    if new != text:
        p.write_text(new, encoding="utf-8")
        print(f"updated: {p}")

# 删除临时脚本
for t in TMP:
    p = Path(t)
    if p.exists():
        p.unlink()
        print(f"removed: {t}")