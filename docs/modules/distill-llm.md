> 覆盖模块：`agentmemhub/distill.py`（1745 行）、`agentmemhub/scoring.py`（574 行）、
> `agentmemhub/llm.py`（627 行）、`agentmemhub/export.py`（71 行）·
> 最后核对：2026-10-23 · 产物表结构见 [data-architecture.md](../data-architecture.md)

# 蒸馏与 LLM（distill-llm）

## 1. 职责边界

把"几百轮原始对话"压成"几十条一句话结论"。这是**唯一大量调用 LLM** 的环节，
也是幂等、去重、软删除三条纪律的起源。

**做什么**

| 环节 | 落点 | 产物 |
|---|---|---|
| 切片：会话 → 轮次 → 可蒸馏单元 | `distill.build_slices` | `Slice`（含 `slice_key` / `content_hash`） |
| 蒸馏：一个切片 → N 条记忆 | `distill.distill_slice` | `distilled_memories` 行 |
| 层内合并：同会话相邻切片重复内容 | `distill.merge_*`（`MERGE_SLICE_KEY`） | `status=merged` + `merged_from_json` |
| 去重：跨会话同 hash | `content_hash` 查重 | `status=duplicate` + `dedup_of` |
| 投影：记忆 → 检索单元 | `distill.project_memories` | `units` / `units_fts` / `vec_*` |
| 评分：记忆价值三轴 | `scoring.evaluate_trace` | `unit_values` / feedback |

**不做什么**

| 不做 | 说明 |
|---|---|
| **不删原文** | 蒸馏产物是**新增**，原始 `events` 一行不动。记忆被"删"也只是 `wiki_ignore_at` 软标记 |
| **不保证一条对话一定产记忆** | 无信息量的切片会返回空；`prompt` 明确要求"没结论就返回空数组" |
| 不做增量判断 | "哪些切片还没蒸馏"由 `distill_hashes` + `slice_done()` 决定，不是靠时间戳猜 |

## 2. 对外入口

| 入口 | 说明 |
|---|---|
| `python -m agentmemhub distill [--source X] [--only id,...] [--dry-run]` | 蒸馏主体 |
| `python -m agentmemhub score [--all / --incremental]` | 批量评分（三轴 → feedback） |
| `sync` | 日常入口：ingest → clean → 向量化（不含 distill，distill 与 wiki 是手动/面板触发） |
| `POST /api/admin/distill` | 面板长任务 |
| Python | `distill.run(...)`、`distill.save_direct_memory(...)`（MCP 直写走这里） |

## 3. 数据流与不变量

### 3.1 一条记忆的一生

```
Slice（轮次片段）
  │  LLM 蒸馏（prompt 要求：一条只讲一个结论，≤120 字）
  ▼
distilled_memories：status='new'
  │  ① 跨会话查重：content_hash 撞了 → status='duplicate'，dedup_of=<旧 id>，不重复投影
  │  ② 层内合并：同会话重复 → status='merged'，merged_from_json=[旧 id...]
  ▼
units 投影（unit_id）+ 向量
  │
  ▼
wiki 编译（L1 页 → L2 页）里被引用为 [m<id>]
```

### 3.2 幂等的准确含义

幂等靠**两根钥匙**：`content_hash`（内容指纹）+ `prompt_ver`（提示词版本）。

- `distill_hashes` 主键 `(source, conversation_id, slice_key, prompt_ver)`
  —— 同一切片、同一提示词版本，永不重跑
- `slice_done()` 要求 **hash 与 prompt_ver 同时匹配**才算完成
- 所以：**换提示词 = 换一个命名空间**，老产物不会挡住新提示词的重新蒸馏；
  但老产物需要 `purge_stale_memories()` 显式清理（`prompt_ver != 当前` 的整批作废并删对应 units 投影）

**为什么不做"时间戳增量"**：时间戳会漏掉"内容没变但时间更新"的重复蒸馏，
也会漏掉"提示词改了但会话没变"该重做的情况。hash + 版本是唯一稳的。

### 3.3 去重的两个层次

| 层次 | 判据 | 结果 |
|---|---|---|
| 跨会话 | `content_hash` 全局撞 | `duplicate` + `dedup_of`，**不生成新 units** |
| 同会话层内 | LLM 判断语义重复 | `merged` + `merged_from_json`（保留来源 id 链，可回溯） |

去重**不删**任何一行，只改 `status` 与指向——所以"这条为什么没进 wiki"永远查得到。

### 3.4 三条硬不变量

1. **原文永不被改写**。蒸馏只读 `events`；被撤销的是**投影**（`units`），不是历史。
2. **软删除优先**。`wiki_ignore_at` 打标记后 wiki 不再引用它，但记忆本身还在库里，
   随时可恢复。
3. **投影与产物成对**。`project_memories()` 与 `drop_projection()` 必须成对调用；
   只删 `distilled_memories` 不删 `units` 会留下搜得到却看不到来源的孤儿。

### 3.5 会话分桶（`mcp` 直写记忆）

`save_direct_memory()` 的 `conversation_id` 默认不再是固定 `direct`，而是
`_direct_bucket(ts)` 算出的 `direct-YYYY-MM-DD`（`DIRECT_BUCKET_DEFAULT = "daily"`，
可用 `distillation.direct_bucket` 改；`none` = 老行为，全部堆一起）。

**为什么必须分桶**：L1 重编的脏单位是**会话**。210 条直写记忆共用一个会话时，
新增 1 条就要重编 84 页；按天分桶后，只有当天的会话变脏。
这是把"重编成本随记忆总量线性增长"打断的那一刀。

## 4. 配置项

| 键 | 作用 |
|---|---|
| `distillation.model` / `api_key` / `endpoint` | 蒸馏与评分用的模型 |
| `distillation.prompt_ver` | 提示词版本；**改提示词必须同步 +1**，否则老切片被当作已完成 |
| `distillation.direct_bucket` | `mcp` 直填记忆的会话分桶（`daily` / `none`） |
| `distillation.llm.max_total_seconds` | **单次调用总时长上限**（不是 socket 空闲超时） |
| `distillation.llm.max_retries` | 瞬态错误重试次数（默认 2） |
| `distillation.llm.backoff_base` | 指数退避基数（默认 1.5，即 `base × 2^(attempt-1)`） |

### 4.1 超时的坑（实测结论）

`timeout` 是**socket 空闲超时**：非流式调用在等首字节期间没有数据往来，
真正"上游卡住"时它不触发。实测出现过 975 秒才返回（`ok=True`, `attempt=1`）的情况。

所以 `LLMClient._post()` 在 socket timeout 之外又加了一层**总时长上限**：
`_post` 把真正的请求交给**工作线程**并发，主线程 `join(上限)` 到点就走人。

**为什么不是 `socket.shutdown()`**：Windows 上对阻塞中的 `recv` 调 `shutdown()`
实测无效（返回成功但读仍在阻塞）。线程方案是唯一可靠的中断方式；
代价是超时后线程可能残留到进程退出，数量受 `max_retries` 约束，可接受。

### 4.2 重试不能叠乘

`call_json(tries=3)` × `complete_json(max_retries=2)` 会乘出最坏 6 次尝试；
内容审核拒绝还会额外 +2。现在 `call_json` 遇 `LLMTransientError` **直接抛出**，
由 `complete_json` 统一重试，只有一层计数。

## 5. 调试与排障

| 现象 | 排查路径 |
|---|---|
| 蒸馏跑完没产出 | `slice_done()` 认为已完成（hash 命中）→ 改 `prompt_ver` 或换会话；或切片本身无结论（正常） |
| 记忆重复很多 | 查 `status`：`duplicate` 有 `dedup_of`、`merged` 有 `merged_from_json`；都**不产生新 units** |
| 改了 prompt 但结果没变 | `prompt_ver` 没 +1 → 老切片被当作已完成跳过 |
| 旧版本产物还占着检索 | 跑 `purge_stale_memories()`（提示词升版后该显式调用） |
| 单次调用卡很久 | 看 `logs/wiki.log` 的 `call` 事件（`ok` / `attempt` / 耗时）；确认 `max_total_seconds` 是否配了 |
| 搜得到但打不开来源 | 孤儿投影：`units` 有行、`distilled_memories` 没行 → 该成对删的没成对 |
| `memory_save` 后 wiki 立刻重编 | 正常：直写会让该会话变脏；按天分桶后影响面只有当天 |

## 6. 测试在哪

| 文件 | 守住什么 |
|---|---|
| `tests/test_distill.py`（1689 行） | 切片边界、蒸馏产物字段、**幂等**（同输入两跑不重复）、去重指向、合并链 |
| `tests/test_scoring.py`（619 行） | 三轴评分、feedback 写入、增量 vs 全量 |
| `tests/test_llm.py` | `extract_json` / `scrub_text` / 审核拒绝 / 瞬态重试 / **总时长上限** |
| `tests/test_export.py` | 导出 JSONL / Markdown 字段完整 |
| `tests/test_rag_bridge.py`（514 行） | 投影与检索桥：`project_memories` / `drop_projection` 成对 |

**改提示词的流程**：改 `prompt` 文本 → `prompt_ver` +1 → 测试里对应用例同步 →
小样本实跑对比前后条数与质量 → 再决定是否 `purge_stale_memories()` 清老产物。
