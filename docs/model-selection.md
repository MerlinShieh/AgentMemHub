# LLM 模型选型与定价（记忆蒸馏 / Wiki 编译用）

> **数据来源**：Command Code 官网 `commandcode.ai/docs/resources/pricing-limits`
> 与 `/models/*`（2026-09 一手调研）+ 本项目的实测结论。价格为
> **USD / 百万 token（输入 / 输出）**，服务商随时可能调整，以官网为准。
>
> 适用范围：`llm` 段配置的模型——**记忆蒸馏**、**批量评分**、**LLM Wiki 两级编译**
> 三条链路共用（蒸馏/wiki 可用 `distillation.llm` / `wiki.llm` 单独覆盖）。
> 注意这与**嵌入模型**（bge-small/base，见 README「模型准备」）是两回事。

## 一、结论：当前推荐

1. **主力：`xiaomi/mimo-v2.6-flash`** ⭐ —— 2026-09-22 上游新上架，**价格与 V2.5
   持平**（$0.14/$0.28），按"同价优先新模型"切换。ID 经**实测探测**确认：
   `mimo-v2.6-flash` 可用，而 `mimo-v2.6`、`mimo-v2.5-flash` 都返回
   "not supported on this endpoint"（探测时注意：必须走项目自己的 client ——
   裸 urllib 会被 Cloudflare 拦成 403 `error code: 1010`，**连在用的旧模型也 403**，
   容易误判成"模型不存在"）。
   **质量指标待重测**：下面 V2.5 的实测结论（12/12 稳定、思维链 24~37%、JSON
   遵从性弱于 deepseek）是**同族上一版**的数据，可作预期参考，但**不能当成本版
   结论**。回退位：`llm.model` 换回 `xiaomi/mimo-v2.5`（同价，已实测）。
2. **批量 / 质量增强：`meituan/LongCat-2.0:free`** ⭐ —— **免费**（100 请求/天/账号，
   UTC 午夜重置）。适合**放后台大批量、多次重跑**：同一批目标跑多轮取更优结果，
   是"零成本提质量"的手段（配合 `wiki --action retry` 定向补跑）。
3. 免费档备选：`inclusionai/ling-3.0-flash-sante:free`（同为 100 请求/天）、
   `poolside/laguna-s-2.1-free`（**唯一无日限**，适合真正的大批量）。
4. 付费档质量优先：`z-ai/glm-5.3-flash`（官网 Intelligence **41.9**，全表最高，
   比最便宜付费档贵约 25%）；高吞吐：`deepseek/deepseek-v4.1-flash`（**247 tok/s**，
   全表最快，但注意峰谷计价）。

> 免费档需账户有 **$1 credits**（Command Code 的准入要求）。

## 二、价格与能力对照（2026-09 官网）

### 付费档

| 模型 | 输入 | 输出 | 能力要点 | 适用 |
|---|---|---|---|---|
| **`xiaomi/mimo-v2.6-flash`** ⭐ | **$0.14** | **$0.28** | 2026-09-22 新上架，**价格与 V2.5 持平**；同族上一版实测 Intelligence 22.3、思维链占比低（24~37%）；**本版质量待重测** | **主力：蒸馏 + wiki 两级编译** |
| `xiaomi/mimo-v2.5` | $0.14 | $0.28 | 同价，**已实测**：12/12 稳定、真实成本≈deepseek 一半；JSON 遵从性略弱 → 配 `repair_model` | **回退位**（新模型出问题时换回） |
| `xiaomi/mimo-v2.5-pro` | $0.435 | $0.87 | 同族增强版 | 质量要求更高的批次 |
| `deepseek/deepseek-v4.1-flash` | $0.15 | $0.60（低谷） | **247 tok/s**（全表最快）；JSON 遵从性好 | 高吞吐；注意峰谷计价 |
| `z-ai/glm-5.3-flash` | $0.15 | $0.50 | **Intelligence 41.9**（全表最高） | 付费档质量优先 |
| `Qwen/Qwen3.8-Flash` | $0.16 | $0.47 | 官网未公布 Intelligence 分数 | 备选 |
| `zai-org/GLM-5.2-Fast` | $3.00 | $10.25 | ⚠️ **"Fast"是高吞吐定位，不是便宜**——比同厂 glm-5.3-flash 贵 20 倍 | **别误选** |

### 免费档（需 $1 credits 准入）

| 模型 | 限额 | 备注 |
|---|---|---|
| **`meituan/LongCat-2.0:free`** ⭐ | 100 请求/天/账号（UTC 午夜重置） | 推荐用于批量多次重跑 |
| `inclusionai/ling-3.0-flash-sante:free` | 100 请求/天/账号 | 备选 |
| `poolside/laguna-s-2.1-free` | **无日限** | ID 是连字符 `-free`（不是 `:free`），写错会 404 |

## 三、三个必须知道的坑

1. **`zai-org/GLM-5.2-Fast` 不是便宜货**：同厂 `glm-5.3-flash` 的 20 倍价格，
   "Fast"指吞吐定位。按名字猜价格极易误选。
2. **DeepSeek 峰谷计价**：换算北京时间后，**高峰 = 工作日 09:00–12:00 与
   14:00–18:00**（正好覆盖国内工作时段），价格翻倍。跑批要排到 12–14 点、
   18 点后或周末。
3. **官网对 `response_format` / structured outputs / JSON mode 无任何承诺**：
   高 JSON 遵从性要求**必须先实测**。本项目的应对是 `llm.repair_model`
   （主模型返回了内容但 JSON 解析失败时，把原始输出交给修复模型转规范 JSON；
   失败仍按失败处理，绝不编造内容）。

## 四、计费模式（决定能不能跑批）

| 模式 | 价格 | 限制 | 结论 |
|---|---|---|---|
| 订阅套餐 | Go $1（**无 API 权限**）/ GOAT $10–70 / Pro $20–80 / Max $100–200 | **5 小时 / 每周滚动窗口** | 交互式使用 |
| Provider 按量档 | $15/mo + $1.01 手续费，按模型原价扣 credits（官方声明 **no markup**） | **无窗口限制** | **批量跑批必须选它** |

官方未公布并发 / QPS / RPM 数字；开源模型标价是多上游均价，单次费用会浮动。

## 五、配置示例

```yaml
# agentmemhub.yaml
llm:
  endpoint: "https://api.commandcode.ai/provider/v1"
  model: "xiaomi/mimo-v2.6-flash"    # 主力推荐（同价优先新模型）
  repair_model: "deepseek/deepseek-v4.1-flash"   # JSON 解析失败时的修复模型
  timeout: 900
  # thinking / reasoning_effort 视 provider 支持情况填
  #   DeepSeek 官方：thinking: "disabled"（彻底关推理，最省）
  #   Command Code：low / medium / high / xhigh / max（传 none 才报错）

# 两级编译刻意不级联（见 docs/llm-wiki.md）
wiki:
  llm: {}                            # 留空 = 继承顶层
  l2:
    llm: {}                          # 同样继承顶层，不继承 wiki.llm
```

**成本参考（本项目实测）**：全量 L1 编译（224 页）约 ¥1.6；L2 全量重跑
（19 域 / 231 页、161 次调用）约 ¥2.9（按 DeepSeek 价）/ ≈¥1.3（按 MiMo 价）；
每次运行的花费都会落 `logs/wiki.log` 的 `run_end` 记录（汇总方式见
[`docs/llm-wiki.md`](llm-wiki.md) 「成本控制」）。
