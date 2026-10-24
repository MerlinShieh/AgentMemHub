"""AgentMemHub 统一配置体系。

原则：所有路径/端口默认采用官方默认；用户可在配置文件里覆盖（优先最小化配置）。
优先级（高 → 低）：
    1. 环境变量（临时覆盖，命名 AGENTMEMHUB_*）
    2. 配置文件：AGENTMEMHUB_CONFIG 指定 > 项目根 agentmemhub.yaml > 数据目录 config.yaml
    3. 内置默认值

配置文件为标准 YAML（见项目根 agentmemhub.yaml.example）。所有相对路径
相对项目根解析，~ 展开为用户目录。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 记忆蒸馏默认配置（yaml distillation 段逐键覆盖；嵌套子段各自合并）
DEFAULT_DISTILL = {
    "enabled": True,
    # prompt_ver：留空（None）= 跟随代码里的 PROMPT_VER 常量（提示词一改就自动
    # 触发重蒸）；显式给整数则覆盖之（用于强制重蒸，如临时排查）。
    # 不设默认数字：否则改提示词易忘记同步，旧结果永不更新。
    "prompt_ver": None,
    "slice": {
        "max_chars": 24000,        # 单片字符预算（上下文保险）
        "max_turns": 16,           # 单片轮数预算；单轮巨会话走字符硬切
        "topic_boundary": True,    # 用关键词重叠度细化话题边界
        "boundary_window": 4,      # 粗切点 ±N 轮内找最低重叠处
        "per_message_cap": 2000,   # 单条消息入 prompt 时的截断
    },
    "merge": {
        "enabled": True,           # 同会话多片合并沉淀
        # 单次合并调用的输入字符预算。**必须远小于切片预算**：合并的输出是
        # "合并后的完整条目列表"，输入越大输出越大——实测 24000 字符（155 条）
        # 时推理模型把 8192 token 全耗在 reasoning 上，正文为空（确定性失败）。
        # 调小到 8000（约 40-60 条/批）后输出规模可控。
        "max_chars": 8000,
        # 层级收敛最多轮数：批次变小 → 批数变多，收敛需要的轮数也更多
        # （433 条 → 40/批 → 11 批 → 逐轮减半，约 5 轮收敛）
        "max_rounds": 6,
    },
    "dedup": {
        "cosine_duplicate": 0.92,  # ≥ 判重复（丢弃 + 指向已有）
        "cosine_similar": 0.80,    # ≥ 判相似（入库 + 打标互链）
    },
    "sanitize": {"enabled": True},  # 正则脱敏兜底
    "runtime": {
        "max_concurrent": 4,
        "timeout": 60,
        "dry_run": False,
    },
}

#: LLM Wiki 编译（把零散记忆归纳成可读页面）。
#:
#: **为什么必须与 distillation 分开配 llm**：两条链路的推理需求是相反的——
#: 蒸馏 / 评分要"想清楚"（思考提升结论质量），而 wiki 编译只是把**已经想清楚的
#: 结论**归纳成页面，相对不需要思考。共用一份 llm 配置会让 wiki 白白付推理开销
#: （实测思维链占其输出的 66%，单次编译 105K → 326K 输出 token）。
#:
#: llm 子段留空 = 全部继承顶层 llm；只写需要覆盖的字段：
#:     llm: {thinking: "disabled"}       # DeepSeek 官方：彻底关闭推理（最省）
#:     llm: {reasoning_effort: "low"}    # 只能调强度的 provider（如 Command Code）
DEFAULT_WIKI = {
    "enabled": True,
    # 第一级是**长调用**：一个会话几十条记忆，分组与逐组编译的输入可达几万字符。
    # 默认 timeout 只有 60s —— 实测 101 条记忆的会话在**分组调用**上连续 3 次读
    # 超时（60s × 3 + 退避 ≈ 187 秒），`LLMTransientError` 抛出后整会话编译失败。
    # 第二级早已在 yaml 里放宽到 900s，第一级此前漏配；**默认值必须是"能跑完"
    # 的**，不能要求使用者先知道这个内部坑才会配。
    "llm": {"timeout": 900},
    "single_shot_max": 12,   # 记忆数 ≤ 此值时单次编译（省一次分组调用）
    # 分组调用的**单批记忆数上限**：分组是"输出规模随输入线性增长"的调用 ——
    # 101 条一次性分组实测会卡死（客户端 60s × 3 次超时；放宽到 900s 后 22 分钟
    # 仍无任何响应）。超出即分批分组，批内编号映射回全局编号
    # （见 `wiki_compile.plan_groups_batched`）。
    "plan_batch_max": 40,
    # 组级（页级）并发编译的并发度：各组之间无依赖，串行纯属浪费（实测 32 组
    # 串行约 48 分钟，并发 3 后约 12 分钟）。真实并发 = 会话级 workers × 本级，
    # 上游有 RPM/TPM 限制，撞 429 会走全局限流闸门退避（`agentmemhub/llm.py`）。
    "page_workers": 3,
    "workers": 4,            # 全量编译的并发数
    # 产出目录的正式配置位 —— 触发器/对齐审计/增量更新的默认目标。
    # 不配则这些功能需要显式传目录。
    "out_l1": "",
    "out_l2": "",
    # Obsidian CLI 可执行文件（产物链接验收用，见 `agentmemhub/obsidian.py`）。
    # **为什么不只靠 PATH**：进程环境是启动时捕获的 —— 实测把 Obsidian 目录加进
    # 用户 PATH 后重启 DSH，35 条用户 PATH 里 34 条都继承了，**只有最后加的那条没**
    # （DSH 从旧环境继承而来）。验收因此静默退化成"未验收"。配置位与进程启动时机
    # 无关，是唯一稳的做法。留空则按 OBSIDIAN_CLI 环境变量 → PATH 解析。
    "obsidian_cli": "",
    # 增量更新触发器：检测便宜（align 毫秒级只读）、编译昂贵（LLM），
    # 按规则攒批触发；任一规则命中且有脏数据才真的跑 update。
    "update": {
        "enabled": True,
        "schedule": ["09:00", "12:00", "18:00"],  # 每天这些时点检测一次
        "dirty_memories": 10,   # 脏记忆（新增+变更）达到此数触发一次更新
        "first_write_daily": True,  # 每自然日第一次蒸馏写入后检测并更新
    },
    # 第二级：跨会话聚合（第一级的会话内页面 → 主题实体页）。
    # 它的 llm 子段**继承顶层 llm 而非 wiki.llm** —— 两级是独立的覆盖关系，
    # 级联继承会让第一级的 provider 选择牵连第二级（见 Config.wiki_l2）。
    "l2": {
        "llm": {},
        "workers": 6,        # 聚合阶段调用密集，并发可高于第一级
        "min_pages": 2,      # 一个分组至少这么多页才值得合并成新页
        "domain_max": 60,    # 主题域数量上限（控制归域调用的输出规模）
    },
    # 离线批处理（Batch API）：**收割节流** + **离线触发器**（与上面的 `update`
    # 并列；判定顺序是"**离线优先 → 实时兜底**"，见 `wiki_triggers._run_locked`）。
    # 为什么并列一整套规则而不是只做一个模式开关：两者的"划算形状"不同 ——
    # 实时要**及时**（时点多、阈值低、当日首写就触发），离线要**攒批**
    # （时点少、阈值高、不追求及时）。
    "batch": {
        "check_interval_s": 300,   # 收割节流：距上次检查不足这么多秒就不再查厂商
        # ---- 离线触发器（两个都开 = 离线优先、实时兜底；只开一个 = 纯某通道）----
        "enabled": False,          # 默认**关**：离线要花钱且异步，不默认自动发起
        "schedule": ["02:00"],     # 离线时点档：少而集中（夜里跑，白天不打扰）
        "dirty_memories": 30,      # 阈值调高：离线本来就该攒批，少跑多编译更划算
        "first_write_daily": False,  # 离线不追求及时，不必"当日首次写入"就触发
        # 距 `completion_window` 到期不足这么多秒就发一次**即将过期预警**
        # （默认 6 小时；0=关闭）。为什么要它：到期未完成的任务会**整批作废**、
        # 数据回待更新池 —— 前面的等待全白费，提前知道才好决定"调大窗口重提"。
        "expire_warn_s": 21600,
        # 单轮回读**最多修几条坏 JSON**（默认 50；0 = 不修）。修复走**实时**调用
        # （要花钱），而坏 JSON 可能成片出现 —— 不能悄无声息打几百次；超出的留给
        # 下轮（它们的原文还在，不会丢）。见 `wiki_batch.repair_broken_results()`。
        "max_repair": 50,
    },
    # 全量重跑（`wiki --action full-run`）：**重新归域 + 全部域重编 + 覆盖写盘**。
    # 什么时候需要：增量归类"撑不住"了（align / hint 会提示"『待整理』域已有 N 页"）——
    # 该重新归纳的是**域结构本身**，而不是再往结构里塞新域。
    "full": {
        # 走哪条通道：`realtime`（当场出结果、单价高）或 `offline`（官方批量 5 折，
        # 但要等厂商 18 分钟 ~ 24 小时）。**默认实时** —— 全量是"显式要求重来一遍"
        # 的重操作，默认当场做完更符合直觉；想省钱、不怕等就改成 offline。
        # ⚠️ 哪个更划算**取决于凭据**：官方批量是实时的 5 折，但某些订阅套餐给出的
        # 实时额度折算下来更便宜 —— 所以这里只做开关，不替用户判断。
        "mode": "realtime",
    },
}


#: 离线批处理（Batch API）配置（`llm_batch` 段）。
#:
#: **刻意独立成段、不与顶层 `llm` 合并**：批量推理是**另一套端点与凭据** —— 实测
#: 小米 MiMo 的批量主机 `batch-api-cn.xiaomimimo.com` 与实时路由
#: `api.commandcode.ai` 不是一套 key，模型名写法也不同（批量要**小写、不带厂商
#: 前缀**：`mimo-v2.6-flash`，而实时段里是 `xiaomi/mimo-v2.6-flash`）。
#: 让它们互相继承只会把两套凭据搅在一起，出问题时无从判断用的是哪一套。
#:
#: 实测结论（2026-09-29 真凭据探测，详见 docs/modules/batch-offline.md）：
#:   · `completion_window` 实测支持到 **`336h`（14 天）**；官方文档里那句
#:     "当前固定 24h"是错的（同页正文写的"1–14 天"才对）
#:   · 建任务成功码是 **HTTP 201**（不是 200），初始状态是文档未列的 `created`
#:   · 取消是**异步**的：先 `cancelling`，再查询才是 `cancelled`
#:   · 另有文档未提的 `GET /v1/batches` 与 `GET /v1/files` 列表接口
DEFAULT_LLM_BATCH = {
    "endpoint": "https://batch-api-cn.xiaomimimo.com/v1",
    "api_key": "",                      # 只写本地 yaml；example 里留空占位
    "model": "mimo-v2.6-flash",         # 批量仅支持 mimo-v2.6-pro / mimo-v2.6-flash
    "completion_window": "24h",         # 实测可到 336h；默认 24h（夜间闲时通常够）
    "timeout": 60,                      # 单次 HTTP 超时（秒）
    "max_retries": 3,                   # 只作用于幂等动作（见 batch_client 的重试策略）
}


#: 快照保留策略（`snapshot` 段）。
#:
#: 为什么值得单独配：快照是**不可重建产物**的最后一道后悔药——wiki 页面重编要
#: 花钱花时间且结果会漂移，unit_values 里的反馈演化值更是完全无法重建。保留份数
#: 是"磁盘 vs 安全感"的权衡：一份约 190 MB（索引库 176 MB + 两级 wiki 产物 5 MB），
#: 默认 **10 份约 1.8 GB**。磁盘紧就调小，想留更长历史就调大——不必改代码。
#:
#: 实测：整份 gzip 能压到 **67%**（176 MB → 119 MB，6.5 秒），10 份省约 0.5 GB。
#: 但快照的价值在"出事时能立刻恢复"，**可读、可直接打开**比省这点空间更重要
#: （压缩会把恢复路径从"拷回来"变成"先解压"，还多一层压缩文件损坏的风险）；
#: 库里 93% 是含向量 blob 的索引库，本来也压不动多少。故默认**不压缩**。
DEFAULT_SNAPSHOT = {
    "keep": 5,   # 最多保留几份（超出自动删最旧）
}

#: 日志滚动 / 归档策略（`logs` 段）。
#:
#: **规则**（用户确定）：
#:   · **作用域**：`rotate` 是**全局默认**，对所有日志生效；`files.<日志名>`
#:     覆盖它，单文件配置为空（或没写该项）= 回落全局。
#:   · **滚动规则二选一**（不是同时命中）：配了 `max_mb`（>0）→ 按**大小**滚动
#:     —— **这是最高优先级**；没有大小规则但 `daily` 为真 → 按**自然日首次写入**
#:     滚动；两者都没配 → 不滚动。
#:   · **归档**：`<日志目录>/<archive_dir>/<日志名>/<日志名>.<时间戳>` —— 先按
#:     日志名分文件夹，再把归档文件放进去。
#:   · **暂不压缩**：`compress: false` 时归档保留**原始文件数据**（配置位预留，
#:     将来打开即用 gzip）。
#:   · 时间维度的最低单位是**一个自然日（24 小时）**，没有小时级细粒度。
DEFAULT_LOGS = {
    "rotate": {
        "max_mb": 5,         # 超过多少 MB 滚动（0 = 不启用大小规则）
        "daily": False,      # 按自然日首次写入滚动（有大小规则时本项被忽略）
        "keep_days": 30,     # 归档保留天数，超出删最旧（0 = 不限）
        "compress": False,   # 归档时是否 gzip（当前保留原始文件）
        "archive_dir": "archive",   # 归档根目录（相对日志目录）
    },
    "files": {},             # 单文件覆盖，如 {wiki: {max_mb: 10}, tasks: {daily: true}}
}


#: 通知配置（`notify` 段）。**落库始终启用**（面板可查、带未读状态）；
#: 这里是可选的**推送通道** —— 目前支持 Pushplus（微信）。
#: 推送是**旁路**：失败只落日志，绝不影响落库与调用方（与 `notify.py` 同一哲学）。
DEFAULT_NOTIFY = {
    "pushplus": {
        "enabled": False,     # 默认关：推送是"对外发消息"，不该默认发生
        "token": "",          # pushplus.plus 的 token（用户自己的；只写本地 yaml）
        "topic": "",          # 群组编码（可选；留空 = 自己收）
        "timeout": 5,         # 单次推送超时（秒）—— 短超时，别拖慢调用方
    },
    #: 去重窗口（秒）：同一 `(category, title)` 在此窗口内只留一条。
    #: 为什么需要：失败重试与重复触发会刷屏，而 Pushplus 是**推微信** ——
    #: 刷屏的代价远大于"少一条通知"。0 = 不去重。
    "dedup_window_s": 300,
    #: **推送策略**：哪些事件值得推到微信。**落库不受它影响**（始终全留）。
    #:
    #: 两类开关是 **OR**（任一命中即推）—— 这样"只看警告以上"与"wiki 的动作我都
    #: 要知道"可以并存，不必去枚举"等级 × 类别"的组合：
    #:   · 按**类别**：`memory_write`（记忆写入）/ `wiki_trigger`
    #:     （wiki 更新相关，即 category ∈ `wiki` / `batch` / `trigger`）
    #:   · 按**等级**：`info`（一般消息）/ `warning`（紧急或警告）/ `error`（严重错误）
    "push_when": {
        "memory_write": False,   # ① 记忆写入
        "wiki_trigger": True,    # ② 触发 Wiki 更新（含离线批次提交 / 落盘 / 过期预警）
        "info": False,           # ③ 一般消息通知
        "warning": True,         # ④ 紧急或警告通知
        "error": True,           # ⑤ 严重错误通知
    },
}


#: 外部投喂转译层（`feed` 段）。设计见 `docs/feed-adapter-plan.md`。
#:
#: 三条路径互相独立、**都不进快照轮换**（快照 = 索引库 + wiki 产物）：
#:   · `db`        素材登记表 `materials.db` —— 独立库，只存元数据与指针
#:   · `store_dir` 三段物料 `raw/`（不可变原件）+ `mirror/`（中间形态）
#:   · `kb_dir`    最终 md（**交付物/知识库本体**）—— 与 `out_l2_mimo` **并列**，
#:                 全量 wiki 聚合**绝不碰它**（旁路根，投喂内容与自有 wiki 互不影响）
DEFAULT_FEED = {
    "db": "database/materials.db",
    "store_dir": "exports/feed_store",
    "kb_dir": "exports/wiki_work/out_feed",
    #: 取回阶段（M3 起真正生效）：单次抓取超时，消费点是 `service._fetch_timeout`。
    #:
    #: ⚠️ 这一段曾有一批**假开关**，2026-10-15 实测后删除：
    #: `convert.llm` / `convert.ocr`（转换阶段是否让 LLM / OCR 参与）、
    #: `fetch.renderer`（Playwright 二级渲染）、`depth_default`（加工深度）。
    #: 四个键**没有任何读取点** —— 能力还没做，开关先写好了。
    #:
    #: 为什么必须删而不是留着"以后用"：配置的语义是**承诺**
    #: （"改了它就会变"）。改了没反应、且**不报错**，用户只会以为自己改错了地方；
    #: 更坏的是**报错文案也在引用它**（`url.py` 曾指引"置
    #: `feed.fetch.renderer=playwright`"，指向一个不存在的开关），
    #: 把假的当成了真的。**能力真正落地时再加回来** —— 那时它自然带消费点。
    #:
    #: 附带教训（记在这里免得下次重踩）：`scripts/check_config_keys.py`
    #: **没能发现**这四个键，原因不是"键名出现在注释里骗过了它"，
    #: 而是 `agentmemhub.yaml.example` 当时**根本没有 `feed` 段** ——
    #: 审计只扫 example 与本地 yaml 里出现过的键，不在这两处的键**不在它视野内**。
    #: 所以"加配置键"和"写进 example"必须同一步做，否则防线是空的。
    "fetch": {
        "timeout": 20,
    },
    #: **允许投喂的格式**（后缀白名单）。见 `agentmemhub/feed/formats.py`。
    #: `exts` 留空 = 跟随转换器注册表（默认，加转换器就自动支持）；填了 = 只允许这些。
    #: 运行时还能被 `PUT /api/feed/formats` 覆盖（写状态文件，不改这里的 yaml）。
    "accept": {
        "exts": [],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并（嵌套 dict 递归合并，其余类型直接覆盖）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path: Path) -> dict:
    import yaml
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


class Config:
    """配置视图：合并 内置默认 < 配置文件 < 环境变量。"""

    def __init__(self, path: Optional[Path] = None, env=None):
        self._env = env if env is not None else os.environ
        cfg_path = path
        if cfg_path is None:
            named = self._env.get("AGENTMEMHUB_CONFIG", "").strip()
            if named:
                cfg_path = Path(named).expanduser()
            else:
                for cand in (PROJECT_ROOT / "agentmemhub.yaml",
                             PROJECT_ROOT / "database" / "config.yaml",
                             Path.home() / ".agentmemhub" / "config.yaml"):
                    if cand.exists():
                        cfg_path = cand
                        break
        self._file: dict = _load_yaml(cfg_path) if cfg_path and cfg_path.exists() else {}
        self._file_path = cfg_path

    # -- 基础目录 --------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """可写数据根目录（db / watermarks / 评分状态 / 托管 pid）。

        默认项目内 <项目根>/database/（随项目走，便于备份与整机迁移）；
        仍可用 AGENTMEM_HUB_DATA_DIR 环境变量或 yaml data_dir 覆盖（测试隔离用）。
        """
        v = self._env.get("AGENTMEM_HUB_DATA_DIR", "")
        if not v:
            v = str(self._get("data_dir", ""))
        if v:
            return self._resolve(v)
        return PROJECT_ROOT / "database"

    @property
    def db_path(self) -> Path:
        v = self._env.get("AGENTMEMHUB_DB", "")
        if not v:
            v = str(self._get("db_path", ""))
        if v:
            return self._resolve(v)
        return self.data_dir / "agentmemhub.db"

    @property
    def web_port(self) -> int:
        try:
            return int(self._env.get("AGENTMEMHUB_PORT", self._get("web", {}).get("port", 8086)))
        except (TypeError, ValueError):
            return 8086

    # -- 各 Agent Harness 数据位置 ---------------------------------------

    def agent_path(self, source: str) -> Optional[Path]:
        """显式配置的 source 数据路径（未配置返回 None → adapter 走官方默认探测）。"""
        sec = self._get("agents", {}) or {}
        if not isinstance(sec, dict):
            return None
        raw = sec.get(source, "")
        if not raw:
            return None
        return self._resolve(str(raw))

    # -- LLM（蒸馏 / 评分共用）-------------------------------------------

    #: 可以从 `llm` 段原样透传的标量字段。
    #: **新增 provider 开关时务必加到这里** —— 本段是白名单过滤，漏加会让配置
    #: 静默失效（实测踩过：`thinking` / `reasoning_effort` 写进了 yaml 却读不到，
    #: 表现为"开关不生效"，排查成本很高）。
    _LLM_SCALAR_KEYS = ("timeout", "max_total_seconds", "max_retries",
                        "backoff_base", "max_tokens", "temperature",
                        "thinking", "reasoning_effort", "repair_model")

    @property
    def llm(self) -> dict[str, Any]:
        """LLM 接入配置：{endpoint, api_key, model} + 可选的 headers/超参。

        优先级：env（AGENTMEMHUB_LLM_ENDPOINT / _API_KEY / _MODEL）> yaml `llm` 段。
        空值表示未配置，由调用方决定报错或降级。api_key 只读不打印。

        **使用方**：记忆蒸馏（distill.py）与批量评分（scoring.read_engine_llm）
        共用本段——换模型/换服务商只改这一处；蒸馏如需单独覆盖写 distillation.llm。
        headers：provider 特定请求头（部分服务商有硬性要求）。
        thinking / reasoning_effort：思考模式与强度（DeepSeek 系）。
        """
        sec = self._get("llm", {}) or {}
        if not isinstance(sec, dict):
            sec = {}
        out: dict[str, Any] = {
            "endpoint": str(self._env.get("AGENTMEMHUB_LLM_ENDPOINT", "")
                            or sec.get("endpoint", "") or ""),
            "api_key": str(self._env.get("AGENTMEMHUB_LLM_API_KEY", "")
                           or sec.get("api_key", "") or ""),
            "model": str(self._env.get("AGENTMEMHUB_LLM_MODEL", "")
                         or sec.get("model", "") or ""),
        }
        headers = sec.get("headers")
        if isinstance(headers, dict) and headers:
            out["headers"] = {str(k): str(v) for k, v in headers.items()}
        for k in self._LLM_SCALAR_KEYS:
            if sec.get(k) is not None:
                out[k] = sec[k]
        return out

    @classmethod
    def _merge_llm(cls, top: dict[str, Any], sub: dict[str, Any]) -> dict[str, Any]:
        """蒸馏的 llm 子段：标量留空继承顶层；headers 合并（子段优先）。"""
        out: dict[str, Any] = {
            k: (str(sub.get(k) or "") or str(top.get(k) or ""))
            for k in ("endpoint", "api_key", "model", "repair_model")
        }
        headers: dict[str, Any] = {}
        if isinstance(top.get("headers"), dict):
            headers.update(top["headers"])
        if isinstance(sub.get("headers"), dict):
            headers.update(sub["headers"])
        if headers:
            out["headers"] = headers
        for k in cls._LLM_SCALAR_KEYS:
            v = sub.get(k) if sub.get(k) is not None else top.get(k)
            if v is not None:
                out[k] = v
        return out

    # -- 记忆蒸馏 ---------------------------------------------------------

    @property
    def distillation(self) -> dict[str, Any]:
        """记忆蒸馏配置（已合并默认值，调用方无需处理缺键）。

        `llm` 子段留空的字段继承顶层 `llm` —— 默认只配一处即可两处共用；
        需要蒸馏走不同模型时再单独覆盖（如更便宜/更长上下文的模型）。
        """
        merged = _deep_merge(DEFAULT_DISTILL, self._get("distillation", {}) or {})
        merged["llm"] = self._merge_llm(self.llm, merged.get("llm") or {})
        return merged

    # -- LLM Wiki 编译 ----------------------------------------------------

    @property
    def wiki(self) -> dict[str, Any]:
        """LLM Wiki 编译配置（已合并默认值，调用方无需处理缺键）。

        `llm` 子段留空的字段继承顶层 `llm` —— 顶层配 provider / 密钥 / 请求头，
        这里只覆盖"要不要思考"这类差异项（见 DEFAULT_WIKI 的说明）。
        """
        merged = _deep_merge(DEFAULT_WIKI, self._get("wiki", {}) or {})
        merged["llm"] = self._merge_llm(self.llm, merged.get("llm") or {})
        return merged

    @property
    def wiki_l2(self) -> dict[str, Any]:
        """第二级（跨会话聚合）配置。

        `llm` 的继承链是 **顶层 llm → wiki.l2.llm**，刻意**跳过 wiki.llm**：
        `wiki.llm` 是"第一级用什么模型"的选择，级联下去会让第一级换 provider
        时连带改掉第二级（实测踩过：第一级用 DeepSeek 官方关推理，第二级
        因此丢掉了 Command Code 的凭据）。两级各自相对顶层覆盖，互不牵连。
        """
        merged = _deep_merge(DEFAULT_WIKI, self._get("wiki", {}) or {})
        l2 = dict(merged.get("l2") or {})
        l2["llm"] = self._merge_llm(self.llm, l2.get("llm") or {})
        return l2

    # -- 离线批处理（Batch API）--------------------------------------------

    @property
    def llm_batch(self) -> dict[str, Any]:
        """离线批处理配置（已合并默认值）。

        **不与顶层 `llm` 合并** —— 两套端点与凭据互相独立（见 DEFAULT_LLM_BATCH
        的说明）。`api_key` 为空即"没配好"，客户端应当直接拒绝而不是发请求。
        """
        return _deep_merge(DEFAULT_LLM_BATCH, self._get("llm_batch", {}) or {})

    # -- 快照 -------------------------------------------------------------

    @property
    def snapshot(self) -> dict[str, Any]:
        """快照配置（已合并默认值，调用方无需处理缺键）。

        `keep` = 最多保留几份，超出自动删最旧。非法值由 snapshot.keep_count()
        统一兜底（回退内置默认），此处只负责把 yaml 值原样递过去。
        """
        return _deep_merge(DEFAULT_SNAPSHOT, self._get("snapshot", {}) or {})

    # -- 日志 -------------------------------------------------------------

    @property
    def logs(self) -> dict[str, Any]:
        """日志滚动/归档配置（已合并默认值）。

        `rotate` 是全局默认（对所有日志生效），`files.<日志名>` 覆盖它——
        解析成"某个日志的生效策略"由 `agentmemhub.logs.rotate_policy(name)` 负责
        （那里才知道日志名与文件路径的对应关系）。
        """
        return _deep_merge(DEFAULT_LOGS, self._get("logs", {}) or {})

    # -- 通知 -------------------------------------------------------------

    @property
    def notify(self) -> dict[str, Any]:
        """通知配置（已合并默认值）：推送通道 + 去重窗口。

        落库（面板可查、带未读状态）**始终启用**；这里的 `pushplus` 只是可选的
        外部推送，`dedup_window_s` 防止失败重试把微信刷屏。
        """
        return _deep_merge(DEFAULT_NOTIFY, self._get("notify", {}) or {})

    # -- 外部投喂转译层 ----------------------------------------------------

    @property
    def feed(self) -> dict[str, Any]:
        """投喂转译层配置（已合并默认值）。

        **"脚本优先"分工不走配置**：脚本解析确定性、免费，LLM 只在质量自评
        不达标时兜底 —— 这是实现里的升级路径（§7.0.1），不是开关键。
        所以这里**没有** `convert.llm` / `convert.ocr`（细节见 `DEFAULT_FEED` 的注）。
        """
        return _deep_merge(DEFAULT_FEED, self._get("feed", {}) or {})

    def feed_paths(self) -> dict[str, Path]:
        """`feed` 的三个落盘位置（**已解析成绝对路径**）：`db` / `store` / `kb`。

        单列一个方法而不是散着 `_resolve`：三处路径必须来自**同一份配置**，
        否则会写出"原件进 A 目录、登记表却指向 B"这类难查的错位。
        """
        f = self.feed
        return {
            "db": self._resolve(str(f.get("db") or DEFAULT_FEED["db"])),
            "store": self._resolve(str(f.get("store_dir")
                                       or DEFAULT_FEED["store_dir"])),
            "kb": self._resolve(str(f.get("kb_dir") or DEFAULT_FEED["kb_dir"])),
        }

    # -- 内部 -------------------------------------------------------------

    def _get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._file
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _resolve(self, raw: str) -> Path:
        p = Path(os.path.expandvars(str(raw))).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def __repr__(self) -> str:  # pragma: no cover
        return f"Config(file={self._file_path or '<default>'})"


_instance: Optional[Config] = None


def config() -> Config:
    """进程级单例（测试可传 env/path 重建）。"""
    global _instance
    if _instance is None:
        _instance = Config()
    return _instance


def reset() -> None:
    """重置单例（测试用）。"""
    global _instance
    _instance = None


# ---------------------------------------------------------------------------
# LLM 生效配置视图（只读）
# ---------------------------------------------------------------------------

#: 四个 LLM 使用点：显示名 → 取值函数（返回该调用方**合并后**的 llm 段）。
#:
#: 为什么要这个视图（2026-09-22）：LLM 的覆盖项是**就地**的（`wiki.llm` /
#: `distillation.llm` / `wiki.l2.llm`）—— 这与日志的 `logs.files.<name>`、
#: 召回档位的 `retrieval.callers.<name>` 那种**集中式覆盖**不同。于是"某个
#: 调用方到底用哪个模型、哪些参数是继承的"要翻好几处配置才能拼出来。
#: 本函数把结果拼好，并标注每一项的来源。
_LLM_USERS: tuple[tuple[str, str, str], ...] = (
    ("记忆蒸馏", "distillation.llm", "distill"),
    ("批量评分", "llm（顶层，无覆盖）", "score"),
    ("Wiki 第一级", "wiki.llm", "wiki_l1"),
    ("Wiki 第二级", "wiki.l2.llm", "wiki_l2"),
)


def _llm_fields(cfg: dict) -> dict[str, Any]:
    """从一份 llm 配置里取"看得见的字段"（api_key 只报存在性，绝不回显）。"""
    out: dict[str, Any] = {}
    for k in ("endpoint", "model", "headers", "timeout", "max_tokens",
              "temperature", "thinking", "reasoning_effort", "repair_model"):
        v = (cfg or {}).get(k)
        if v is None or v == "" or v == {}:
            continue
        out[k] = v
    key = str((cfg or {}).get("api_key") or "")
    out["api_key"] = "（已设置）" if key else "（未设置）"
    return out


def llm_effective(conf: "Config | None" = None) -> dict[str, Any]:
    """各 LLM 使用点**最终生效**的配置 + 每项的来源（继承 / 覆盖）。

    返回 `{"top": {...}, "users": [{"name", "path", "values", "overrides",
    "inherits"}, ...]}`。`overrides` 是与顶层**取值不同**的字段（即该调用方
    真正覆盖了什么），`inherits` 是沿用顶层的字段。
    """
    c = conf or config()
    top = c.llm
    top_fields = _llm_fields(top)
    # 四个调用方各自的合并结果（与生产链路读的是同一份属性，不会各说各话）
    raw: dict[str, dict] = {
        "distill": (c.distillation or {}).get("llm") or {},
        "score": top,
        "wiki_l1": (c.wiki or {}).get("llm") or {},
        "wiki_l2": (c.wiki_l2 or {}).get("llm") or {},
    }
    users = []
    for name, path, key in _LLM_USERS:
        vals = _llm_fields(raw[key])
        overrides, inherits = [], []
        for k, v in vals.items():
            if k == "api_key":
                continue
            if top_fields.get(k) == v:
                inherits.append(k)
            else:
                overrides.append(k)
        users.append({"name": name, "path": path, "values": vals,
                      "overrides": overrides, "inherits": inherits})
    return {"top": {"path": "llm（顶层）", "values": top_fields}, "users": users}