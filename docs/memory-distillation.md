# 记忆蒸馏方案（Memory Distillation）

> 状态：设计定稿（2026-09-11，分支 `feat/memory-distillation`）
> 实施进度：**D1–D6 已完成** —— 沙箱隔离 / 配置解析 / LLM 客户端
> (`agentmemhub/llm.py`) / 脱敏模块 (`agentmemhub/sanitize.py`) / 两表 DDL /
> S0 切片器 / S1 段级蒸馏 / S2 合并沉淀 / S3+S4 去重投影 / CLI `distill` /
> 面板「蒸馏记忆」/ 71 题评测基线（`eval/baseline-predistill.json`）。
> **D7 验收待 LLM 配置**（在 `agentmemhub.yaml` 的 `llm` 段填
> endpoint/api_key/model 后即可跑）。
> 目标：用 LLM 把原始会话蒸馏为**可沉淀、可总结**的结构化记忆，替代当前
> 「原始 unit 直接向量化」的写入路径，从源头消除召回噪音。

---

## 0. 结论速览

**调研判定：切片器必须建。** 长会话虽是少数，却贡献了 90% 的语料量——
它们恰恰是深度工作、最值得蒸馏的会话；而最大的会话 262 万字符只有 1 轮，
按轮数切片对其失效，必须**轮数 + 字符双预算**兜底。

流水线全景（五阶段，LLM 仅两处）：

```
采集库 agentmemhub.db（只读）
   │  排除过滤（复用 memory_exclusions，父/子语义不变）
   ▼
[S0 切片] ────── 纯本地：轮数粗切 + FTS 话题边界细化；单片 = 整会话或切片
   ▼
[S1 段级蒸馏] ── LLM①：强制结构化输出 memories[]（type/content/confidence）
   ▼
[S2 合并沉淀] ── LLM②：仅多片会话；同会话各段条目去重提炼为终稿
   ▼
[S3 跨会话去重] 纯本地：入库前向量化近邻查询，三档标记 new/similar/duplicate
   ▼
[S4 入库] ────── memories 表（真相源）+ units 投影（source='distilled'）
                 → 三路召回 / MCP / 面板零改动；脱敏正则在入库前兜底
```

幂等基石：**content_hash**（会话/切片内容指纹）。同内容重跑零重复；
内容被编辑/清洗后 hash 变化自动重蒸；提示词升版走 `prompt_ver` 定向重蒸。

---

## 1. 前置调研（2026-09-11，真实库实测）

### 1.1 现状噪音（为什么必须蒸馏）

索引库 `session_rag.db`：14633 条 unit，逐条向量化原始消息。

| 长度 | 条数 | 占比 | 样本 |
|---|---|---|---|
| <20 字符 | 1179 | 8% | `继续` `可以` `ok` `！` `1` `C` |
| 20–50 字符 | 4187 | 29% | 寒暄、确认、过程叙述 |
| ≥50 字符 | 9267 | 63% | 真正承载内容 |

**近 30% 索引容量是对话填充词**。轮级实测（`tk=msg_mte4`）：13 条 unit
里 10 条是过程叙述（「我先快速确认一下…」「下载转入后台执行…」），
真信号只有 2–3 条。

### 1.2 会话体量分布（采集库 239 个含正文会话）

| 指标 | P50 | P75 | P90 | P95 | P99 | MAX |
|---|---|---|---|---|---|---|
| 字符量 | 7,948 | 14,702 | 27,211 | 89,730 | 573,346 | **2,617,427** |
| 轮数 | 1 | 4 | 13 | 44 | 170 | 397 |

超阈值占比（**个数少 ≠ 影响小**）：

| 阈值 | 会话数 | 占比 | 累计语料占比 |
|---|---|---|---|
| > 12k 字符 | 80 | 33.5% | **90.3%** |
| > 24k 字符 | 26 | 10.9% | **80.1%** |
| > 48k 字符 | 23 | 9.6% | 79.0% |
| > 96k 字符 | 12 | 5.0% | **70.4%** |

### 1.3 三个决定性发现

1. **切片器必须建**：10.9% 的会话（>24k）占 80% 语料。若不切片，
   这批会话要么超上下文，要么塞进上下文后总结失焦——恰是用户担心的场景。
2. **轮数切片会失效**：最大会话 2,617,427 字符却只有 1 个 turn_key
   （导入型/单轮巨会话）。切片必须 `max_chars` 与 `max_turns` 双预算，
   任一触顶即切；单轮超预算时轮内按字符硬切（对齐段落边界）。
3. **turn_key 是天然切片锚**：索引库 99.9% 的 unit 带稳定 turn_key，
   采集库同源。切片边界优先落在轮边界，无需新造键。

### 1.4 可复用的既有资产

| 资产 | 位置 | 蒸馏中的用法 |
|---|---|---|
| 会话内容指纹 | 采集库 `conversations.signature` | 算法参考（content_hash 同源思想） |
| LLM 客户端纪律 | `scoring.py`（直连 opener、`ContentFilterRejected`、`_scrub_text`、worker 并发） | 蒸馏 LLM 调用直接复用这套模式 |
| 独立 source 先例 | `rag/memstore.py`（`source='memory'`，MCP 手动记忆） | 蒸馏投影用 `source='distilled'` |
| 排除机制 | `_iter_candidates` 的 LEFT JOIN 过滤 | S0 扫描同款过滤，父/子语义不变 |
| 幂等摄入 | `_existing_keys` / `src_id` 锚 / `ensure_vec_table` | units 投影直接走现有向量化管线 |
| 敏感扫描 | `scripts/sensitive_scan.py` 正则族 | 抽成可编程调用的脱敏模块（S4 兜底） |

### 1.5 配置现状与 D2 修复

`agentmemhub.yaml` 早有 `llm:` 段，但 `config.py` 原先**未解析**它；
`scoring.read_engine_llm()` 读的是回退引擎的配置文件（memos 时代路径）——
rag 主线因此没有统一的 LLM 配置源。

→ **D2 已修复**：`config.py` 新增 `llm` 与 `distillation` 两个属性
（含默认值深合并、`AGENTMEMHUB_LLM_*` 环境变量覆盖）；蒸馏默认继承顶层 `llm` 段，
`distillation.llm.*` 可单独覆盖（留空=继承），需要蒸馏走更便宜/更长上下文的模型时
只改一处。**所有模型名/端点/阈值一律进统一配置，禁止硬编码**（项目既有铁律）。

---

## 2. S0 切片器（纯本地，零 LLM 成本）

### 2.1 分流规则

```
会话正文（user+assistant，is_system=0，排除过滤后）
  ├─ chars ≤ max_chars 且 turns ≤ max_turns → 单片（整会话）
  └─ 超任一预算 → 切片：
       ① 轮数粗切：按 turn 顺序累积，任一预算触顶即在轮边界成片
       ② 话题边界细化（topic_boundary=true）：粗切点 ±boundary_window 轮内，
          计算相邻两侧的关键词重叠度（分词词频 Jaccard，复用 FTS 分词口径），
          重叠度最低处即话题切换点，作为真边界——避免把一个话题从中间切开
       ③ 单轮巨会话兜底（turns=1 但 chars 超预算）：轮内按 max_chars 硬切，
          切点对齐段落/空行边界
```

### 2.2 切片产物

```json
{
  "source": "zcode", "conversation_id": "sess_...",
  "slice_key": "s0",                      // 会话内序号；单片会话恒 "whole"
  "turn_first": "msg_xxx", "turn_last": "msg_yyy",   // 单片会话为 null
  "chars": 21874,
  "content_hash": "sha256(切片规范化文本)[:16]"
}
```

切片文本格式（LLM 输入）：按 turn 分块，user/assistant 角色标注，每条截断
（如 2000 字符/条）——单条超长消息不该独占预算。

**例外**：单轮巨会话走轮内硬切时**不施加单条截断**（该轮独占多片，片预算
已由 `max_chars` 保证）——否则 262 万字符的导入型会话会被截成 2000 字符、
丢掉 99.9% 内容。

### 2.3 D3 实测（沙箱真实数据，239 个会话）

| 指标 | 结果 |
|---|---|
| 总片数 | **599**（话题边界细化使片数高于"纯预算切"的估算） |
| 巨轮硬切会话 | 2 个（1 轮即超 `max_chars`） |
| 最大单轮会话 | qwen `7b39daba…`：**2,617,427 字符 / 1 轮 → 110 片**，最大片 24,021 字符 |
| 全库切片耗时 | **2.3 秒**（纯本地零 LLM 成本） |
| 片长分布 | 最大片 25,940 字符（渲染前缀开销约 8%，在上下文预算内） |

成本含义：约 599 次段级蒸馏调用 + 多片会话的合并调用（S2）。

---

## 3. S1 段级蒸馏（LLM①，强制结构化输出）

### 3.1 输出 schema（用户指定 + 评估后微调）

```json
{
  "memories": [
    {
      "type": "decision | fact | preference | lesson",
      "topic": "AgentMemHub",
      "content": "用户决定记忆引擎采用 RRF 融合向量与 trigram 两路召回",
      "confidence": "high | medium | low"
    }
  ]
}
```

- `type` 四枚举：decision（决策）/ fact（事实）/ preference（偏好）/ lesson（经验教训，含踩坑）
- `topic`（≤12 字主题标签）：供面板按主题分组、FTS 通道命中主题词、跨会话去重的辅助信号
- `content` 必须**自包含**：含关键实体（项目名/文件/命令/版本），脱离原会话可理解
- 无值得沉淀的内容 → `"memories": []`（寒暄会话、纯执行轮的合法出口）

**评估后否决的字段**（曾考虑加入）：`entities` 列表（content 已含实体，FTS 能抽）、
数字型 confidence（枚举对 LLM 的输出稳定性更高）、`skip_reason`（空 memories 即语义）。

**溯源元数据不让 LLM 输出**，由系统自动携带：source / conversation_id /
slice_key / turn_key / prompt_ver / content_hash / model / created_at。

### 3.2 提示词纪律（v1 要点，`prompt_ver` 管理）

- 只沉淀：结论、决策、事实、用户偏好、踩坑与解法
- 忽略：寒暄、确认语、过程叙述（「我先看一下…」「找到模板了…」）、
  一次性的文件清单/中间输出
- **脱敏指令（第一层）**：忽略并**不得输出**任何敏感内容——
  密钥/token/密码、邮箱/手机号/身份证、私钥/证书、内网地址、个人身份信息；
  涉及敏感上下文时只保留非敏感的技术结论
- temperature=0；max_tokens 留足结构化输出空间
- 每片独立调用，输入含：会话标题（话题语境）+ 切片文本

### 3.3 解析与失败策略（fail-open：宁缺勿错，不丢可重试性）

```
LLM 返回 → JSON 解析
  ├─ 成功 → schema 校验（type/confidence 枚举、content 非空）→ 条目落库
  ├─ 解析失败 → 原样重试 1 次（网关偶发截断常见）
  └─ 仍失败 / 网络错 / 审核拒评(ContentFilterRejected) / 超时
       → 跳过该切片：不登记 hash、计数入日志 → 重跑蒸馏自动补上
```

与 Judge 的 fail-closed 相反这里选 fail-open：蒸馏是写入侧，
**丢一条记忆比留一次失败更糟**；原文永远在采集库，可无限重试。

---

## 4. S2 同会话合并沉淀（LLM②，仅多片会话）

- **触发条件**：该会话切片数 > 1（单片会话段级结果即终稿，零成本跳过）
- 输入：该会话全部切片的 memories 拼合（每条一行，标注来源切片）
- LLM 任务：**跨段去重 + 关联信息合并**——同一事项在多片的碎片化描述
  合成一条完整表述，互相矛盾时保留双方并降 confidence
- 输出同 schema

### 4.1 输入保护：层级合并（D7 演练发现的设计漏项）

**问题**：切片有预算保护，合并没有。实测最大会话 262 万字符 → **110 片**，
段级产出上百条 —— 一次性送 LLM 会超上下文（与切片要防的是同一类问题）。

**方案** `merge_hierarchical()`：

```
输入条目按 merge.max_chars（默认 24000）分批
  ├─ 单批 → 直接合并返回（与原语义一致）
  └─ 多批 → 逐批合并 → 把各批结果再分批合并（收敛），最多 merge.max_rounds 轮
       └─ 轮数用尽仍未收敛（LLM 未去重）→ 保底截断至 200 条 + 记录
          「未收敛」告警 —— 绝不无限调用
```

每轮条目数应显著下降（合并即去重），因此通常 1–2 轮即收敛到单批。
成本估算：~80 个多片会话，正常情况约 80 次调用；巨会话按批数放大。

---

## 5. S3 跨会话去重（纯本地，入库前打标）

**依据：蒸馏产物短小、规范、自包含——向量判重才可靠（原始碎片做不到，
这也是先蒸馏再去重的顺序理由）。**

```
每条终稿 memory（status 待定）：
  content 向量化（走 active 模型）
  → 在既有蒸馏记忆池查近邻（cosine，复用 vec 表 + JOIN memories）
  ├─ ≥ cosine_duplicate(默认0.92) → duplicate：丢弃，记录指向已有条目
  ├─ ≥ cosine_similar(默认0.80)   → similar：入库但打标 + 与既有条目互链
  └─ <  cosine_similar            → new：正常入库
```

- 跨项目去重（不同 source 的会话）在同一步骤内自然完成（池子不分会话）
- 阈值全部进配置；`duplicate` 不入 units（不占召回面），
  `similar` 入 units 但保留标记（召回后可按状态过滤/展示）

---

## 6. S4 入库与脱敏兜底

### 6.1 数据模型（索引库新增，幂等 DDL）

```sql
-- 蒸馏幂等层：内容没变 + 提示词没升版 → 重跑跳过
CREATE TABLE IF NOT EXISTS distill_hashes(
    source TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    slice_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prompt_ver INTEGER NOT NULL,
    model TEXT,                      -- 蒸馏所用 LLM 模型（审计）
    created_at INTEGER NOT NULL,
    PRIMARY KEY(source, conversation_id, slice_key, prompt_ver)
);

-- 蒸馏真相源：type/topic/confidence/dedup 链完整保留；units 只是检索投影
-- 表名刻意不用 memories：与 memstore 的手动原子记忆（units.source='memory'）区隔
CREATE TABLE IF NOT EXISTS distilled_memories(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    slice_key TEXT,                  -- 单片会话=whole；多片=s0/s1…
    turn_key TEXT,                   -- 该记忆归属轮（单片取会话首轮，多片取来源切片首轮）
    type TEXT NOT NULL,              -- decision|fact|preference|lesson
    topic TEXT,                      -- ≤12 字主题标签（面板分组 / FTS 命中）
    content TEXT NOT NULL,
    confidence TEXT NOT NULL,        -- high|medium|low
    status TEXT NOT NULL DEFAULT 'new',  -- new|similar|duplicate
    dedup_of INTEGER,                -- similar/duplicate 指向既有 distilled_memories.id
    content_hash TEXT NOT NULL,      -- 条目级防重锚
    prompt_ver INTEGER NOT NULL,
    model TEXT,
    merged_from_json TEXT,           -- S2 合并来源条目 id（审计）
    created_at INTEGER NOT NULL,
    UNIQUE(source, conversation_id, content_hash)
);
```

### 6.2 units 投影（召回零改动）

```
distilled_memories(status IN ('new','similar'))
  → units(source=**原会话 source**, conversation_id=**原会话 id**,
          role='distilled',                    ← 用 role 而非 source 标识蒸馏产物
          seq=**-(memory_id)**,                ← 负值：不与采集事件正 seq 冲突
          turn_key=该记忆归属轮,
          src_id='dst_' + content_hash,        ← 幂等锚（重复投影则原地更新）
          text=('topic：' if topic else '') + content,
          title=会话标题)
  → FTS 触发器自动同步 → 三路召回 / MCP / 面板全部沿用现有管线
```

**关键决策：用 `units.role='distilled'` 而不是独立 `source`。** 现有排除
（`memory_exclusions`）、删除（`delete_units_for_conversation`）与召回侧
`exclude_session` 全部按 `(source, conversation_id)` 定位——若蒸馏投影另起
source，这些机制会**静默失效**（排除会话后蒸馏记忆仍被召回）。沿用原
source/conversation_id 后，三者天然生效，零额外代码。

**待投影判据**：`distilled_memories.status IN ('new','similar')` 且 units 中
不存在对应 `src_id`。用这个判据而非额外的"已投影"标记，好处是 units 被
误删或被排除机制清掉后，重跑会自动补回（自愈）。

### 6.3 S3 去重池的选择

去重池 = **已投影的蒸馏条目**（它们已在 units 里带向量）：

- 不需要额外的向量表（省一套 schema 与同步逻辑）；
- 历史向量不必重算（直接查 vec 表 KNN）；
- 跨项目去重天然完成（池子不按 session/source 分区）。

顺序上先投影再判重会浪费（duplicate 条目填了又删），因此实现为**判定后
才投影**：向量化 → KNN 查既有蒸馏条目 → 三档 → `new`/`similar` 才写 units。

### 6.4 脱敏兜底（第二层，正则）

prompt 层失效的最后防线，入库前对每条 content 扫描：

- 高危格式：API key / Bearer token / 私钥块（`-----BEGIN`）/ 密码赋值模式
- 个人信息：邮箱 / 手机号 / 身份证号
- 命中 → **剥离敏感片段**后入库（剥离后仍有实质内容）；空了 → 整条丢弃 + 日志计数
- 正则族从 `scripts/sensitive_scan.py` 抽取为可编程模块（`agentmemhub/sanitize.py`），
  支持配置扩展自定义模式；`sanitize.enabled` 可关（默认开）

---

## 7. 幂等与重跑矩阵

| 场景 | 行为 | 依据 |
|---|---|---|
| 同内容重跑蒸馏 | 全部切片 hash 命中 → 跳过 | distill_hashes 主键 |
| 提示词升版（prompt_ver+1） | 全量重蒸；`--only <会话>` 可限定 | hash 表含 prompt_ver |
| 会话被编辑/清洗后 | 内容变 → hash 变 → 该会话重蒸 | content_hash 语义 |
| 会话排除 → 恢复写入 | 排除期不蒸（S0 过滤）；恢复后正常蒸 | 排除 LEFT JOIN 复用 |
| 整会话排除 | 其蒸馏条目同步从 units 移除 | 沿用 delete_units_for_conversation |
| 轮次排除 | 该轮所属切片 hash 重算 → 重蒸该会话 | 切片文本变化 |
| LLM 失败的切片 | 未登记 hash → 下次自动补 | fail-open 设计 |
| 蒸馏产物向量化失败 | _vectorize_stage 现有幂等补齐 | units/src_id 锚 |

---

## 8. 配置设计（agentmemhub.yaml 新增段）

```yaml
distillation:
  enabled: true
  prompt_ver: 1                    # 提示词版本：升版触发重蒸
  llm:                             # 留空全部继承顶层 llm 段
    endpoint: ""                   #   如 https://…/chat/completions
    model: ""                      #   如 deepseek-flash（OpenCode Go 所配）
    api_key: ""
  slice:
    max_chars: 24000               # 单片字符预算（双保险之一）
    max_turns: 16                  # 单片轮数预算（之二）；单轮巨会话走字符硬切
    topic_boundary: true           # FTS 关键词重叠度细化话题边界
    boundary_window: 4             # 粗切点 ±N 轮内找最低重叠
    per_message_cap: 2000          # 单条消息入 prompt 时的截断
  merge:
    enabled: true                  # 同会话多片合并沉淀
  dedup:
    cosine_duplicate: 0.92         # ≥ 此值判重复（丢弃+指向）
    cosine_similar: 0.80           # ≥ 此值判相似（入库+打标互链）
  sanitize:
    enabled: true                  # 正则脱敏兜底
  runtime:
    max_concurrent: 4              # LLM 并发（复用 scoring worker 模式）
    timeout: 60                    # 单次调用超时（秒）
    dry_run: false                 # true=只蒸不入库（预览成本与产物）
```

**配套基础设施（D2）**：`config.py` 解析顶层 `llm` 段与 `distillation` 段；
`agentmemhub.yaml.example` 同步补两段（现状 example 缺 llm 段）。

---

## 9. 分期实施

| 期 | 内容 | 验收 |
|---|---|---|
| **D1** | 本文档 + 调研数据（本次完成） | 方案评审通过 |
| **D2** | 基础设施：config llm/distillation 解析、两表 DDL、LLM 客户端（复用 scoring 纪律）、sanitize 模块抽取 | 单测：配置解析默认值/覆盖链、DDL 幂等、脱敏正则命中样本 |
| **D3** | S0 切片器 | 单测：单片分流、双预算触顶、话题边界细化（构造话题切换样本）、**2.6M 单轮巨会话硬切**、排除过滤、切片 hash 稳定性 |
| **D4** | S1 段级蒸馏 + distill_hashes 幂等 | 单测（mock LLM）：schema 校验、枚举拒绝、解析失败重试、fail-open 不登记、重跑跳过 |
| **D5** | S2 合并沉淀 + S3 跨会话去重 | 单测：多片触发/单片跳过、合并去重、三档阈值边界、duplicate 不入 units |
| **D6** | 编排接线：CLI `distill` 命令（含 `--dry-run/--only/--limit`）+ 面板「蒸馏记忆」按钮（后台任务+进度）+ stats 覆盖率/失败切片数 | web_verify 增断言；面板可见进度可中断 |
| **D7** | 验收（先小后大） | ① 选 5 个代表会话（含 1 巨会话）试蒸，**人工抽查产物质量**；② 71 题评测集蒸馏前后 hit@k/MRR 对比；③ 全量蒸馏 + 幂等重跑验证（第二遍零新增） |

---

## 10. 验收标准与风险

**验收标准（全部满足才算成）**
1. `eval/queries.yaml` 71 题：蒸馏后 hit@k / MRR **不低于**基线（理想上升）
2. 召回结果随机抽样：不再出现「继续/可以/ok」类碎片命中
3. 幂等：蒸馏命令连跑两遍，第二遍 0 新增、0 LLM 调用
4. 脱敏：构造含 token/手机号的样本会话 → 蒸馏产物 0 泄漏
5. 中断可续：任意时点 Ctrl+C 后重跑，已完成的切片不重复调用 LLM

**风险与对策**
- **LLM 成本**：估算 ~476 片蒸馏 + ~80 次合并 ≈ 556 次调用。
  对策：D7 强制小批量先行 + `--limit`/`--only` 限流 + `dry_run` 预览。
- **单轮巨会话硬切割裂语义**：段级蒸馏本就不要求完整叙事，
  且 S2 合并会把碎片化描述拼回；可接受，D7 人工抽查重点盯这类会话。
- **评测集基线**：蒸馏前须先跑一次 71 题留存基线分数（当前无存档），
  否则 D7 对比无参照——列入 D6 收尾动作。
- **网关限速**：4 并发对 deepseek-flash 通常安全；遇 429 退避重试
  （沿用 scoring 的错误分类思路，429 单独退避而非 fail-open）。
