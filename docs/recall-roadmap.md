# 高召回效率方案 · 优先级路线图（2026-09-10）

基线（51 题评测，k=10）：base-hybrid **0.922**/MRR 0.729 · small-hybrid 0.902 ·
vector 0.882~0.902 · fts 0.725；延迟实测 87~337ms（q8 CPU）。
目标：召回质量与延迟双优，可接线 Agent 循环。

依据：本项目评测数据 + MemOS `core/retrieval/` 源码分析
（机制本质=多通道投票+闸门级联+LLM终审；评分只是 ≤0.3 有界 tie-breaker）。

## P0 检索侧快赢 ✅ 已完成（2026-09-10 验收：base-hybrid 0.980，fts 0.824，hybrid p95=151ms，57 测试全绿）

| # | 事项 | 证据/预期 | 量级 |
|---|---|---|---|
| P0-1 | **标题通道**：units_fts 加 title 列（或标题独立通道），RRF 可见 | starship、side-chat 51 题下三档全失的根因；预计 +2~3 题 ≈ +0.05 recall | 半天 |
| P0-2 | **精确标识符通道**：抽高熵串（≥12 字符含`_:-`数字、长 [a-z] 段）走 LIKE，命中固定分 1.0 | 开发者语料大量 `timeout_seconds`/`RETRYING` 类精确术语；抄 MemOS exact_identifier 思路（keyword.ts:29,49-63） | 半天 |
| P0-3 | **CJK bigram 噪音过滤**：2 字兜底从裸 LIKE 升级为 bigram 滑窗+噪音正则（剔"你还/得我"对话桥） | fts 0.725 明显偏弱；MemOS keyword.ts:154-181 有现成设计 | 半天 |
| P0-4 | **多样性约束**：top-k 同 conversation_id 限 2 席 + 简化 MMR（λ=0.7，用已存向量算冗余，零额外推理） | 评测口径看不出、但注入场景一个大会话霸屏=不可用；ranker.ts:600 机制平价版 | 半天 |

P0 验收：grounding 51/51 保持；pytest 全绿；base-hybrid recall ≥ 0.922 非劣，
顽题转绿 ≥2；每查询延迟 P95 < 500ms（bench 命令入 CLI）。

## P1 排序结构对齐（中型改造）

| # | 事项 | 说明 |
|---|---|---|
| P1-1 | **候选级 RRF + 相对阈值** | 融合结构升级为 ChannelRank（vec/fts/ident 多通道挂同一候选，Σ1/(60+rank)·0.4）+ 阈值 topRelevance×0.2 + 多通道强信号 bypass；砍噪声尾巴，提 MRR |
| P1-2 | **active 模型终选** | base 召回占优但向量路 0.882<0.902（大向量推理慢 ~3x）；P1-1 落地后重跑三档×双模型，按 质量/延迟 帕累托定 active |
| P1-3 | **exclude_session_id 参数** | 接线前必备：当前会话历史不进召回（防重复注入自己）；MemOS tier2-trace.ts:378 同款 |
| P1-4 | **评测口径升级** | 51→100 题；引入会话级人工金集算 precision@k 与"素材可达率"分离；加每用例 first-hit-rank 分布报表 |

## P2 接线层（属集成，不进引擎本体）

- P2-1 **LLM 终审**（可选开关）：maxKeep=5、temperature 0、fail-closed 安全截断（≥0.7·top 且至少保 1）——照 MemOS llm-filter.ts 的实现纪律，放 AgentMemHub 读侧。
- P2-2 **评分 join 钩子**：按 src_id 接 AgentMemHub 的 value/priority——`priority>0` 硬过滤 + ≤0.3 现场重算 boost（含 30d 半衰期衰减）。大脑留 MemOS 侧，引擎只提供读接口。
- P2-3 **episode/session rollup**（多层记忆）：等写侧沉淀物出现后再做，现在没有可分层的素材，先不做。

## 明确不做

- cross-encoder 独立 rerank 模型（LLM 终审已覆盖精筛需求，除非 P2-1 延迟实测不可接受）
- ANN 索引（17.7k × 512d brute-force 实测 <100ms，百万级前不需要）
- 查询改写/多查询扩展（MemOS 也没做，把改写权让渡给调用方 Agent 更省）

## 执行纪律（全程）

每个 P0/P1 项：测试先行（fixture 库红→绿）→ 真实库小样本验证 → eval 非劣门槛
→ 独立 commit。溯源：检索日志新增通道 id 列表字段（P1-1 起）。
