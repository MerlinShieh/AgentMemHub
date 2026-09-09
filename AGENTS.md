# AgentSessionRag — 项目规约（AGENTS.md）

## 项目定位与边界

独立预研项目：不依赖 MemOS，用通用 RAG 向量化技术实现**会话向量化**与**记忆召回**两个核心能力。

- **只做**存储/召回层（嵌入、索引、混合检索）。
- **不做**写策略层（提取、价值评分、巩固、遗忘）——那部分永远归属 AgentMemHub。
- **不做** MCP server（本阶段只提供 CLI + Python 库接口）。
- **不动** MemOS / AgentMemHub 的任何代码与数据。

## 核心不变量（改代码前先对齐）

1. **源库只读**：`database/agentmemhub.db` 是 AgentMemHub 的采集库，有 daemon 持续写入。
   访问一律 `sqlite3.connect("file:...?mode=ro", uri=True)`；任何写源库的操作视为 bug。
2. **索引独立**：所有派生数据（单元/向量/FTS）只写入 `database/session_rag.db`。
3. **摄取幂等**：以 `(source, conversation_id, seq)` 为单元主键，`src_id` 为稳定锚；
   双跑/重跑结果必须收敛为同一索引（零重复）。
   ⚠️ q8 量化推理的激活按张量动态量化，向量随 batch 组成有 ~cos 0.995 级数值漂移
   （实测，不影响排序语义）。位级可复现的前提：**固定 batch_size + 稳定排序
   （按 source, conversation_id, seq）**，二者不得随意变更；变更视同重建（reembed）。
4. **模型切换一等公民**：
   - 加载路径、维度、pooling、query_prefix 全部来自 `models.json` 注册表；
     代码中**禁止硬编码模型 id/维度/路径**。
   - 每个模型一张独立向量表 `vec_<model_id>`；新老模型向量共存，支持切换前 A/B 召回对比。
   - `models/` 平铺一层（目录名 = 模型 id，不带 HF namespace 前缀）；
     换模型 = 新目录入 `models/` + 注册表加条目 + 改 `active`，其余代码零改动。
5. **消息级入库、轮次级召回**：每条 user/assistant 消息是一个嵌入单元；
   检索命中后按 `turn_key` 展开同轮上下文、按会话聚合展示。
6. **向量化角色白名单**：默认仅 user + assistant；reasoning/tool/patch/meta 不入库
   （reasoning 留配置开关，不作默认）。

## 目录结构

```
AgentSessionRag/
├── AGENTS.md / README.md / models.json / pyproject.toml
├── models/<model-id>/          # ONNX 模型资产（量化版为主，仅 model_quantized.onnx）
├── database/                   # agentmemhub.db(源,只读) + session_rag.db(索引)
├── logs/                       # 统一日志目录（git 忽略）
├── src/asrag/                  # config / logkit / embedder / source / ingest / search / cli
├── scripts/fetch_model.py      # HF 镜像 ONNX 下载（断点续传+校验）
├── eval/queries.yaml           # 召回评测集（仅真实库手动跑）
└── tests/                      # pytest 单元测试 + fixtures
```

## 日志纪律（验证 / 测试 / 溯源三用）

- 统一 `logs/` 目录，**按程序分文件**：`asrag-ingest.log` / `asrag-search.log` / `asrag-eval.log`
  （RotatingFileHandler，UTF-8，由 `asrag.logkit` 单点配置）。
- 溯源最低要求：
  - ingest：每批记录水位、拉取数 / 嵌入数 / 跳过数 / 耗时；
  - search：查询文本、向量 top-k id、FTS top-k id、RRF 融合结果 id、耗时。
  - 出问题时须能**仅凭日志回放**当时的行为。
- **测试必须隔离日志目录**：通过 conftest 注入 tmp_path，绝不写真实 `logs/`。

## 测试纪律

- 栈：pytest（`uv run pytest`）。
- **每次功能改动必须带/补测试；每次 bug 修复必须先有复现该 bug 的回归测试**（红→绿）。
- 里程碑验收以 pytest 全绿为门槛。
- 分层：
  - 纯函数（config / RRF / 截断 / 水位）直接断言；
  - 数据访问用 tmp 目录构造**微型 fixture SQLite 源库**（含中文、超长消息、脏时间戳等边界），
    单测**绝不依赖真实 agentmemhub.db**（真实库只进 eval/bench 手动验证）；
  - Embedder golden 向量回归：固化短句期望向量做容差比对（量化模型上 cosine≈1.0 容差 1e-3）。

## 常用命令

**环境隔离（硬性）**：一切 Python 运行必须走本项目 `.venv`（uv 项目内虚拟环境），
命令一律 `uv run ...` 前缀；禁止用全局 python/pip 安装或运行本项目的任何依赖与脚本。
依赖变更只改 `pyproject.toml` + `uv sync`。

```bash
uv sync                          # 安装依赖（项目内 .venv）
uv run pytest                    # 全量测试（验收门槛）
uv run python -m asrag ingest    # 增量摄取（--rebuild 全量重建索引）
uv run python -m asrag search "查询文本"   # 混合召回
uv run python -m asrag stats     # 索引水位/规模统计
uv run python -m asrag eval      # 评测集 recall@k 三档对比（--model 做 A/B）
uv run python -m asrag reembed --model <id>  # units 不动，按指定模型重建向量表
uv run python scripts/fetch_model.py Xenova/<repo>  # 镜像下载 ONNX + 冒烟 + 自动注册
```

## Windows 环境坑（本机实测，写代码时规避）

- 文件统一 UTF-8；控制台输出避免依赖 GBK。
- `.bat` 若创建：纯 ASCII + CRLF + 块内不得含 `)`。
- 网络受限：HF 下载走 `--base https://hf-mirror.com` 或 Clash 7897 代理兜底。
  **下载必须用 curl 子进程**（`scripts/fetch_model.py` 已实现）：urllib 静默继承注册表
  系统代理，Clash 未运行时直连变拒连/超时；curl 默认不吃注册表且 `-C -` 原生续传。
  大文件（百 MB 级）易被掐断（实测 26MB 处 Connection reset），重跑脚本即可从 .part 续传。
- **CLI 打印中文必须 `sys.stdout.reconfigure(encoding='utf-8')`**（Windows 控制台默认 GBK
  会把会话内容打成乱码，2026-09-09 实测）。
- Git Bash 的 `mv` 对本目录偶发 Permission denied → 用 PowerShell `Move-Item`。
- transformers.js 侧曾要求 `allowRemoteModels=false`；onnxruntime 侧加载本地文件无此问题。
- **FTS5 特殊 `'delete'` 命令在本机 SQLite 3.53.1 不可用**（实测必报 `SQL logic error`，
  与 tokenizer/列名/authorizer 无关）→ 触发器一律用 `DELETE FROM units_fts WHERE rowid=?`
  直接删（普通 FTS5 表自行维护索引）。
- **trigram 检索查询必须切 3 字窗口 OR**：整句自然语言当短语喂 MATCH 几乎必零命中；
  <3 字符查询走 LIKE 兜底（见 `search._query_chunks`）。
