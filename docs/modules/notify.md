> 覆盖模块：`agentmemhub/notify.py` · 相关：`agentmemhub/logs.py`（`audit_notify`）·
> 最后核对：2026-10-04 · 上游：[data-architecture.md](../data-architecture.md)、
> [batch-offline.md](batch-offline.md)（离线批次是通知的主要来源）

# 通知（`notify`）

## 1. 职责边界

**做什么**：把"值得人知道的事"记下来（面板可查、带未读/已读状态），并按策略**推送到
外部渠道**（当前支持 Pushplus 微信，**通道可插拔**）。

**不做什么**：

| 不做 | 归属 |
|---|---|
| 不替代日志。日志是**全量事实流**（给排查用），通知是**少量事件**（给人用）——同一个事件可能两处都记，但取舍标准不同 | [logging.md](../logging.md) |
| 不做投递保证 / 重试。推送是**旁路**，失败只留日志与降级文件 | 本模块 |
| 不做聚合与汇总（"今天一共 37 条"这类） | 面板 |

## 2. 三个概念（一次分清，后面都用）

| 概念 | 含义 | 受策略影响吗 |
|---|---|---|
| **落库** | 写 SQLite `database/notify.db`（`list_notifications()` / `mark_read()` / `count_unread()` / `prune()`） | ❌ **始终执行** —— 面板要查得到 |
| **策略**（`push_when`） | 这条**推不推外部渠道** | ✅ 就是它 |
| **通道** | 外部投递的实现（`pushplus` / 将来的钉钉、飞书…） | 每个通道有自己的 `enabled` |

## 3. 加一条通知：**一行**

```python
from agentmemhub import notify as N

N.info("batch", "已提交离线批次", "258 条请求", batch_id="b1")
N.warn("batch", "即将过期", "还有约 2 小时", meta={"hours_left": 2})
N.error("wiki", "落盘失败", "PermissionError: ...", path="D:/...")
N.debug("trigger", "判定细节", "...")          # 默认不推（等级开关不含 debug）
```

- `category` 是**分流标签**，常见值：`batch` / `wiki` / `trigger` / `memory` / `feed` / `general`
- `**meta` 与 `meta={...}` **两种写法都支持**（关键字直接展开更适合手写）
- **一样是旁路**：`notify()` **永不抛**；落库失败降级写 `logs/notify_fallback.jsonl`
- 推不推由 `push_when` 决定（见 §5）——**落库不受影响**

## 4. 加一个**推送渠道**（核心扩展点）

三步，**核心逻辑一行都不用改** —— 落库、策略、去重、审计、多通道分发全部自动复用。

### ① 写一个通道函数

签名固定（一律**关键字调用**）：

```python
def _channel_dingtalk(*, level, category, title, body, meta, cfg) -> dict:
    """钉钉机器人通道。配置段：`notify.dingtalk`。"""
    if not cfg.get("enabled"):
        return {"ok": False, "skipped": "disabled"}
    webhook = str(cfg.get("webhook") or "").strip()
    if not webhook:
        return {"ok": False, "skipped": "no_webhook"}
    # 可以复用公共渲染，也可以自己渲染成渠道要的形状
    text = notify.render_markdown(level, title, body, meta)
    ... POST webhook ...
    return {"ok": True}
```

**约定**：

- 自己判断"配置够不够" → 返回 `{"ok": False, "skipped": "..."}`。
  **这不是失败**，只是"本渠道本轮不发"（与"发了但失败"分开，排查时才分得清）。
- **永不抛**。`push()` 还会兜一层（一个通道炸了不牵连别的），但通道自己兜住更清楚。
- 返回里必须有 `ok`；可选 `error` / `msg` / 其它字段（会原样进返回值与审计）。

### ② 注册

```python
notify.register_channel("dingtalk", _channel_dingtalk)
```

放在模块加载时即可（`pushplus` 就是这么注册的，见 `notify.py` 末尾）。

### ③ 加配置（渠道**自定义**键）

```yaml
notify:
  dingtalk:
    enabled: true
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=..."
```

`cfg` 传进来的就是 `notify.<通道名>` 这一段 —— 所以**渠道自己决定要哪些配置键**，
`config.py` 里不必登记（也就不会变成"僵尸键"）。

**完成。** 之后：策略、去重、审计、`push()` 的遍历全部自动生效。

> 已注册的通道可以用 `notify.channels()` 列出来；联调单个渠道用
> `notify.push(..., channel="dingtalk")`（只推那一个）。

## 5. 配置

| 键 | 默认 | 含义 |
|---|---|---|
| `notify.pushplus.enabled` | `false` | 该通道总闸（默认关：推送是"对外发消息"，不该默认发生） |
| `notify.pushplus.token` | `""` | Pushplus token（只写本地 yaml，不入库/入仓库） |
| `notify.pushplus.topic` | `""` | 群组编码（留空 = 自己收） |
| `notify.pushplus.timeout` | `5` | 单次推送超时（秒）—— 短超时，别拖慢调用方 |
| `notify.dedup_window_s` | `300` | 同一 `(category, title)` 在窗口内只留一条。**防刷屏**（失败重试/重复触发都会发通知，而这是推微信）；`0` = 不去重 |
| `notify.push_when.memory_write` | `false` | ① 记忆写入（发的是**本轮汇总**，不是每条） |
| `notify.push_when.wiki_trigger` | `true` | ② 触发 Wiki 更新（`category ∈ wiki/batch/trigger`） |
| `notify.push_when.info` | `false` | ③ 一般消息 |
| `notify.push_when.warning` | `true` | ④ 紧急或警告 |
| `notify.push_when.error` | `true` | ⑤ 严重错误 |

`push_when` 的**两类开关是 OR**（任一命中即推）：按**类别**（`memory_write` /
`wiki_trigger`）或按**等级**（`info` / `warning` / `error`）。这样"只看警告以上"与
"wiki 的动作我都要知道"能并存，不必枚举"等级 × 类别"的组合。

> **外部投喂（`category="feed"`）刻意只用等级分支**：它既不是 `memory` 也不在
> `wiki/batch/trigger` 里 ⇒ 只受 ③④⑤ 控制。默认配置下的效果正好是想要的：
> **成功不打扰**（`info=false`）、**失败必达**（`warning`/`error=true`）。
> 所以**没有 `push_when.feed` 这个键** —— "别为了一个已经合理的默认去加键"
> 与"加了键必须有消费点"是同一条纪律的两面。
> ⚠️ 批量投喂**只发一条汇总**（`feed/notify.py`）：一次几百份若逐条通知，
> 标题各不相同 ⇒ 去重窗口失效 ⇒ 手机被刷爆。

> ⚠️ **配置**改完**不用重启**：`_notify_cfg()` 每次直接读盘，不走 `config()` 的
> 进程级单例（长驻的 MCP 进程否则看不到新 token）。
> 离线触发器的开关另有一套热改入口（`wiki --action triggers --set-batch on|off`）。

## 6. 日志事件

| 事件 | 流 | 关键字段 | 含义 |
|---|---|---|---|
| `notify_write` | `logs/notify.log` | `ok`、`deduped`、`level`、`category`、`title` | 落库结果；`deduped: true` = 去重命中（**不是失败**）；`ok: false` = 走了降级文件 |
| `notify_push` | `logs/notify.log` | `ok`、`channel`、`level`、`category`、`title`、`error` | **每个通道一条** —— 排查"哪个渠道没出去、为什么" |
| `notify_fallback.jsonl` | `logs/`（不是事件，是文件） | 整行通知 | 落库失败时的降级落点 |

## 7. 测试在哪

`tests/test_notify.py`：

- 基本读写 / 去重窗口 / 策略（OR / 全关 / 只看警告以上 / 落库不受影响）
- 通道：默认关不发请求、没 token 不发、成功与业务失败、网络异常不抛
- **`test_可以注册新通道`** —— 加渠道的**回归保护**（写函数 + 注册即可生效）
- **`test_一个通道炸了不牵连别的`**、`test_指定通道只推那一个`、`test_未知通道如实报`
- 快捷函数（等级正确、两种 meta 写法、不写空 dict）
- **测试绝不出网**（见 §8 第 7 条）：`test_沙箱下pushplus_send一个字节都不出网`、
  `test_逃生门与替换网络出口缺一不可`、`test_配置真开着时warning通知也只落库不出网`

## 8. 不变量

1. **`notify()` 永不抛** —— 通知写不进去不能让业务失败；
2. **落库不受策略影响** —— 策略只管"推不推外部渠道"，面板始终能查到；
3. **落库成功才推送** —— 否则同一条会既进 `notify_fallback.jsonl` 又发微信；
4. **去重统一在 `notify()` 那层** —— 不放进通道，否则"落库去重、推送漏网"两处各算一套；
5. **一个通道一条审计** —— 否则多通道之后无法回答"是哪个渠道没出去"；
6. **加渠道不改核心** —— 只写一个函数 + `register_channel()`；配置键由渠道自定义。
7. **测试沙箱下 `pushplus_send()` 绝不出网**（2026-10-12 加）—— 置位
   `AGENTMEMHUB_TEST_SANDBOX` 即返回 `{"ok": False, "skipped": "test_sandbox"}`。
   **为什么必须有这道闸**：`notify()` 默认 `push_too=True`，而 `_notify_cfg()` 刻意
   绕过 config 单例**直读真实 yaml**（为"填完 token 立刻生效"），两者相加 ⇒
   **任何"只测落库"的 `notify("warning"/"error", …)` 都会用真实 token 真发微信**。
   实测本文件的 7 处这样的调用会让**每跑一次测试就刷一串微信消息**。
   要测 `pushplus_send()` 本体（报文拼装 + 解析）必须**同时**满足两条：置
   `AGENTMEMHUB_ALLOW_REAL_PUSH=1` **且**把 `urlopen` 换成替身（名字故意写刺眼）。
   **同源教训**（本项目三例：付费 LLM / 真删产物 / 发微信）：真实副作用靠
   "测试记得 mock"守不住，必须在**唯一出口**兜死。
