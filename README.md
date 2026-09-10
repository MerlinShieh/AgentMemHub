# AgentSessionRag

独立实验项目：不依赖 MemOS，用通用 RAG 向量化技术实现两个核心能力——

1. **会话向量化**：把 Agent 会话（user/assistant 消息）嵌入入库；
2. **记忆召回**：向量（sqlite-vec）+ 中文 trigram 全文（FTS5）混合检索，RRF 融合、轮次展开。

本项目是 AgentMemHub 记忆链路的「存储/召回层」预研，验证成熟后再考虑接入；
写策略层（提取、评分、巩固、遗忘）仍归 AgentMemHub，不在本项目范围。
工程规约见 [AGENTS.md](AGENTS.md)。

## 快速开始

```bash
uv sync --extra dev                 # 依赖装进项目内 .venv（禁止全局 python）
uv run pytest                       # 48 项单测全绿为准入门槛
uv run python -m asrag ingest       # 增量摄取 database/agentmemhub.db → session_rag.db
uv run python -m asrag search "查询文本" -k 10
uv run python -m asrag eval         # vector/fts/hybrid 三档 recall@k 对比
uv run python -m asrag stats
```

## 模型管理（切换契约）

```
models/                 # 平铺一层，目录名 = 模型 id（不带 HF namespace）
  bge-small-zh-v1.5/    # 默认激活：512 维中文，量化 ONNX ~24MB
models.json             # 注册表：active 指针 + 每模型元数据（dim/pooling/quantized/queryPrefix）
```

切换/新增模型：

```bash
uv run python scripts/fetch_model.py Xenova/<repo>   # 镜像下载+冒烟+自动注册
uv run python -m asrag reembed --model <新id>          # units 不动，按新模型建独立向量表
uv run python -m asrag eval --model <新id>             # A/B 召回对比
# 满意后改 models.json 的 active 一行即完成切换（新老向量表共存，随时可回退）
```

## 当前验收状态（2026-09-10 定稿）

- 真实库全量摄取：**17,720 单元**（user 2,232 / assistant 15,488），
  向量覆盖 1.0，增量重跑 `embedded=0 vec_gc=0`（幂等，1.8s），
  全量耗时：bge-small ~9.5 分钟 / bge-base ~47 分钟（CPU q8）。
- **双模型向量表共存**（vec_bge_small_zh_v1_5 @512 + vec_bge_base_zh_v1_5 @768），
  `--model` 零成本切换，验证了模型切换契约端到端可用。
- 评测集 **51 题**（改写式查询防偷题；`scripts/check_eval_grounding.py`
  校验 51/51 全部有语料落地依据），k=10。P0 四件套（标题通道/标识符通道/
  滑窗噪音过滤/会话限席+MMR）落地后：

| mode | small-vector | small-fts | small-hybrid | base-vector | base-hybrid |
|---|---|---|---|---|---|
| recall@10 | 0.902 | 0.824 | 0.941 | 0.882 | **0.980** |
| MRR | 0.769 | 0.581 | **0.793** | 0.714 | 0.746 |

- P0 实测收益：base-hybrid **0.922→0.980**（51 题仅 1 失）、fts 单路 0.725→0.824；
  hybrid 延迟 **p50=113ms / p95=151ms**（目标 <500ms 大幅达标）。
- 选型观察：base 召回占优、small 的 MRR 占优——active 终选留待 P1（候选级 RRF）
  落地后按质量/延迟帕累托定夺。
- 模型加载实测：Python(onnxruntime) 与 Node(transformers.js) 双实现向量语义一致。
- 测试：57 项单测全绿（P0 各通道回归 + vec0 GC + 位级确定性 + 幂等 + 水印回放）。

## 已知改进点

- 唯一残留顽题 `starship-prompt`：查询与语料无公共三元组窗口，
  "Starship=终端提示美化"需世界知识桥接——属查询改写/LLM 终审（P1/P2）课题，
  非索引缺陷（side-chat 等标题盲区已被 P0-1 标题通道解决并转绿）。
- reasoning 角色未入库（英文思考流、信噪比差），`ingest --include-reasoning` 可开。
- 评测判定为「top-k 含期望关键词」的宽松口径，下一步可引入人工标注会话级金集。
