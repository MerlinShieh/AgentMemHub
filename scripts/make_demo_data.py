"""生成一套**完全虚构**的演示数据，供 README 截图与本地演示使用。

为什么单独造而不是脱敏导出真实库：
脱敏只能删掉"看得见的敏感串"，删不掉**语义**——真实会话里的项目名、业务逻辑、
人物关系本身就是隐私。这里所有内容都是编的（虚构产品「星轨笔记」与虚构用户「小满」），
结构、字段、分布则与真实库一致，所以界面渲染出来的样子和真的一样。

产出（全部落在 `demo/` 下，不碰真实 `database/`）：

    demo/
      config.yaml              指向 demo 各路径的配置（配合 AGENTMEMHUB_CONFIG 用）
      data/agentmemhub.db      采集库：conversations + events
      data/session_rag.db      索引库：units + distilled_memories + conv_scores + unit_values
      data/materials.db        投喂素材登记
      data/logs/               日志目录（面板「操作日志」读它）
      wiki/out_l1_demo/        L1 页（按会话）
      wiki/out_l2_demo/        L2 页（按域）
      wiki/out_feed/           投喂交付物

用法：

    python scripts/make_demo_data.py            # 生成 / 覆盖 demo/
    python scripts/make_demo_data.py --clean    # 先删掉 demo/ 再生成

之后启动演示面板（另开端口，别动真实实例）：

    $env:AGENTMEMHUB_CONFIG = "demo/config.yaml"
    $env:AGENTMEMHUB_PORT = "8090"
    python -m agentmemhub serve --no-open --port 8090
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"

# 演示数据的时间基准：固定值（每次生成一致，截图可复现）
# 1792803600 = 2026-10-24 09:00:00 +08:00
BASE_TS = 1792803600
DAY = 86400


def _ts(days_ago: int, hour: int = 10, minute: int = 0) -> int:
    """相对 BASE_TS 往前推几天，再指定当天的小时/分钟（秒级时间戳）。"""
    return BASE_TS - days_ago * DAY + (hour - 9) * 3600 + minute * 60


# ---------------------------------------------------------------- 会话与事件

#: (source, id, 标题, 模型, 几天前, 事件列表)
#: 事件：(role, content, 可选 tool 信息)
CONVERSATIONS = [
    (
        "hermes", "h-7f31c2", "修复星轨笔记的标签树递归崩溃", "deepseek-v4.1-flash", 9,
        [
            ("user", "星轨笔记在标签嵌套超过五层时，打开侧边栏会直接白屏，控制台报 "
                     "Maximum call stack size exceeded。"),
            ("assistant", "先确认是渲染递归还是数据构建递归。白屏说明异常发生在 render 之前，"
                          "更可能是构建标签树时对父子关系做了无界递归。"),
            ("user", "我把数据贴出来：每个标签有 id 和 parent_id，但存在三条记录的 parent_id "
                     "指向自己。"),
            ("assistant", "那就对上了——自引用会让递归永远走不出来。修法有两层："
                          "先在建树时检测环（自引用与多节点成环），再做一层深度上限兜底。"),
            ("tool", "grep -rn \"function buildTree\" src/ | head", "shell"),
            ("assistant", "定位到 `src/tags/tree.ts` 的 buildTree。建议改成两遍扫描："
                          "第一遍建 id → node 索引，第二遍挂载父子并在挂载时检查是否回到祖先链，"
                          "回到祖先链的边直接丢弃并计数上报。"),
            ("user", "丢弃边的话用户数据不就丢了吗？"),
            ("assistant", "不丢——被丢弃的只是**父指针**，节点本身仍然建出来挂在根下，"
                          "并且在设置页给一条「有 3 个标签的父子关系异常，已平铺到根」的提示。"
                          "静默修正才是真的丢数据。"),
            ("tool", "python -m pytest tests/tags -q", "shell"),
            ("assistant", "补了三个用例：自引用、两节点成环、深度超限。测试从 12 条涨到 15 条，全绿。"),
        ],
    ),
    (
        "opencode", "oc-2b90ea", "给搜索加上中文分词", "qwen3-coder-plus", 7,
        [
            ("user", "搜索只能命中完整词，搜「笔记同步」搜不到「同步笔记」。"),
            ("assistant", "当前用的是 trigram 索引，对调换顺序无能为力。要不要引入分词器，"
                          "先看语料规模——如果单机笔记在十万字量级，倒排索引完全够用，"
                          "不必上向量。"),
            ("user", "大概三万多条笔记，平均每条四百字。"),
            ("assistant", "那就是一千万字量级，建倒排索引大约几十 MB，可以接受。"
                          "方案：写入时用 jieba 切词 + FTS5 的 unicode61 分词器，"
                          "查询侧对关键词也切一遍再用 AND 连接。"),
            ("tool", "git diff --stat src/search/", "shell"),
            ("assistant", "改动集中在三处：索引构建、查询改写、以及一个 `--rebuild-index` 的迁移命令。"
                          "注意老库不能原地升级，必须新建索引库再原子替换，否则切词规则一变，"
                          "旧文档的倒排项就全错了。"),
            ("user", "那多久能重建完？"),
            ("assistant", "实测三万条大约 40 秒，比担心的快。瓶颈不在切词在写盘。"),
        ],
    ),
    (
        "qwen", "q-4d771b", "设计离线同步的冲突策略", "deepseek-v4.1-flash", 5,
        [
            ("user", "两设备同时改同一篇笔记，同步后变成两份，用户以为是 bug。"),
            ("assistant", "这是典型的 last-write-wins 的代价。要解决得先定语义："
                          "笔记是不是应该支持段落级合并？"),
            ("user", "整篇的字符级 diff 试过，中文体验很差，经常合出乱码。"),
            ("assistant", "那退一步做「段落级三方合并」：以共同祖先为基准，"
                          "按 Markdown 的块级边界切段（空行分段），段是合并的最小单位。"
                          "段 id 用内容哈希，冲突只可能落在同一段上，这样人读得懂。"),
            ("user", "冲突了怎么展示？"),
            ("assistant", "不要用 diff 视图。直接生成「版本 A / 版本 B / 保留两者」三选一的卡片，"
                          "同一篇笔记里多个段落冲突就排成列表。人只做选择，不做合并。"),
            ("tool", "sqlite3 database/agentmemhub.db \".schema conversations\"", "shell"),
            ("assistant", "另外要为每次同步记一份祖先快照，否则三方合并没有基准。"
                          "快照按「最后一次成功同步时的内容哈希」存，别存全文。"),
        ],
    ),
    (
        "zcode", "z-a1c058", "排查同步后附件重复上传", "deepseek-v3.2", 3,
        [
            ("user", "一次同步会多出三四份同样的图片，占了几百兆。"),
            ("assistant", "先看附件命名规则。如果每轮同步都用新时间戳或新 uuid 当文件名，"
                          "服务端就永远认为这是新文件。"),
            ("tool", "find exports -name '*.png' | wc -l", "shell"),
            ("assistant", "确认了：附件落地时用的是 `f\"{int(time.time())}_{name}\"`，"
                          "同一张图每次上传都换名字。改成内容 sha256 做文件名，"
                          "上传前先查重，命中就只写引用。"),
            ("user", "已经重复的怎么清理？"),
            ("assistant", "写个一次性的巡检脚本：按 sha256 分组，保留最早一份，"
                          "其余把引用改指向它再删文件。跑之前必须备份数据库——"
                          "这是不可逆操作。"),
            ("assistant", "顺带一提，这个 bug 在测试里完全测不出来，因为测试用的是固定文件名。"
                          "补用例时要显式让同一张图上传两次。"),
        ],
    ),
    (
        "dsh", "d-90e6f4", "把导入导出改成流式", "deepseek-v4.1-flash", 2,
        [
            ("user", "导出两万条笔记时内存直接顶到 3 GB，机器卡死。"),
            ("assistant", "现在是全量读进列表再 json.dumps，峰值内存约等于数据量的三四倍。"
                          "改成逐条写：先写 `[`，之后每条 dump 一次并补逗号，最后写 `]`。"),
            ("user", "中途失败会留下半个文件吗？"),
            ("assistant", "会。所以写到临时文件，成功后原子替换。导出是只读操作，"
                          "留半个文件比直接报错更糟——用户会以为导出成功了。"),
            ("tool", "python -c \"import json; print('ok')\"", "shell"),
            ("assistant", "实测两万条导出峰值内存从 3 GB 降到 40 MB，耗时基本没变。"),
        ],
    ),
    (
        "hermes", "h-c3d812", "发布前的检查清单", "deepseek-v4.1-flash", 1,
        [
            ("user", "下周要发 1.4，帮我列一下发之前必须过的项。"),
            ("assistant", "按「坏了会不会丢数据」排序，前三项是数据层："
                          "① 数据库迁移在旧版本库副本上跑通且可回滚；"
                          "② 附件去重巡检跑一遍；③ 导出/导入往返一致。"),
            ("assistant", "界面层后置：离线状态提示、同步进度、冲突提示。"
                          "这些坏了不丢数据，只是难看。"),
            ("user", "知道了，先跑数据层的。"),
        ],
    ),
]

#: 会话级任务完成度评分（面板报表列）
CONV_SCORES = {
    ("hermes", "h-7f31c2"): 0.86,
    ("opencode", "oc-2b90ea"): 0.91,
    ("qwen", "q-4d771b"): 0.78,
    ("zcode", "z-a1c058"): 0.83,
    ("dsh", "d-90e6f4"): 0.95,
    ("hermes", "h-c3d812"): 0.62,
}


# ---------------------------------------------------------------- 蒸馏记忆

#: (source, conversation_id, type, topic, content, confidence, status, 域名)
MEMORIES = [
    ("hermes", "h-7f31c2", "decision", "标签树构建",
     "标签树构建改为两遍扫描：先建 id→node 索引，再挂载父子；挂载时若边回到祖先链则丢弃该父指针"
     "并把节点平铺到根，同时向用户上报修正条数。", "high", "new", "前端交互"),
    ("hermes", "h-7f31c2", "lesson", "递归",
     "自引用（parent_id 指向自己）会让建树递归永不终止；只加深度上限会把栈溢出变成静默截断，"
     "必须显式检测环并把被修正的节点数上报给用户。", "high", "new", "前端交互"),
    ("hermes", "h-7f31c2", "fact", "标签树",
     "星轨笔记的标签嵌套超过五层时，侧边栏白屏的直接原因是数据里存在三条 parent_id 自引用的记录。",
     "high", "new", "前端交互"),
    ("opencode", "oc-2b90ea", "decision", "搜索分词",
     "搜索索引改用 jieba 切词 + FTS5 unicode61；查询侧对关键词同样切词后用 AND 连接。"
     "十万字量级不需要上向量检索。", "high", "new", "检索与召回"),
    ("opencode", "oc-2b90ea", "lesson", "索引迁移",
     "切词规则变更后老索引库不能原地升级，必须新建索引库再原子替换——"
     "规则一变旧文档的倒排项就全错了，原地升级不会报错但结果全乱。", "high", "new", "检索与召回"),
    ("opencode", "oc-2b90ea", "fact", "性能",
     "三万条笔记（约一千万字）重建倒排索引实测 40 秒，瓶颈在写盘而非切词。",
     "medium", "new", "检索与召回"),
    ("qwen", "q-4d771b", "decision", "同步冲突",
     "离线同步冲突采用段落级三方合并：以共同祖先为基准，按 Markdown 空行分块，"
     "块 id 用内容哈希，冲突只落在同一块上。", "high", "new", "数据同步"),
    ("qwen", "q-4d771b", "decision", "冲突交互",
     "冲突不用 diff 视图，直接给「版本 A / 版本 B / 保留两者」三选一卡片，"
     "多处冲突排成列表，人只做选择不做合并。", "high", "new", "数据同步"),
    ("qwen", "q-4d771b", "lesson", "字符级 diff",
     "对中文笔记做字符级 diff 合并体验很差，经常合出语义断裂的乱码；"
     "合并的最小单位应该提升到段落。", "medium", "new", "数据同步"),
    ("qwen", "q-4d771b", "fact", "同步基准",
     "三方合并需要每次同步后保存祖先快照；快照按「最后一次成功同步时的内容哈希」存，不存全文。",
     "medium", "similar", "数据同步"),
    ("zcode", "z-a1c058", "lesson", "附件去重",
     "附件落地用 `{int(time.time())}_{name}` 命名时，同一张图每次上传都会换名，服务端永远认为"
     "是新文件；应改用内容 sha256 做文件名并在上传前查重。", "high", "new", "数据同步"),
    ("zcode", "z-a1c058", "lesson", "测试盲区",
     "用固定文件名写的上传测试永远发现不了重名问题；补用例时必须让同一张图上传两次并断言"
     "只落一份。", "high", "new", "数据同步"),
    ("zcode", "z-a1c058", "decision", "历史清理",
     "已重复附件用一次性巡检处理：按 sha256 分组保留最早一份，把引用改指向它再删文件；"
     "执行前强制备份数据库。", "medium", "new", "数据同步"),
    ("dsh", "d-90e6f4", "decision", "导出流式化",
     "导出改为逐条写 JSON 数组流（先写左括号、逐条 dump 补逗号、最后右括号），"
     "写临时文件后原子替换，避免中途失败留下半个文件。", "high", "new", "数据同步"),
    ("dsh", "d-90e6f4", "fact", "内存",
     "两万条笔记导出峰值内存从 3 GB 降到 40 MB，耗时基本不变。", "high", "new", "性能与资源"),
    ("dsh", "d-90e6f4", "lesson", "半成品文件",
     "导出是只读操作，留下半个文件比直接报错更糟——用户会以为导出成功了。", "medium", "new", "数据同步"),
    ("hermes", "h-c3d812", "preference", "发布流程",
     "发布前检查按「坏了会不会丢数据」排序：数据层（迁移可回滚、附件去重、导入导出往返）优先于"
     "界面层（离线提示、同步进度、冲突提示）。", "high", "new", "工程实践"),
    ("hermes", "h-c3d812", "fact", "版本计划",
     "1.4 版本的发布日期排在下周，数据层三项检查未通过就不发。", "medium", "new", "工程实践"),
]

#: 需要「明显有关联但被判为相似」的一对：与上一条内容近似的冗余样本
MEMORIES.append(
    ("qwen", "q-4d771b", "fact", "冲突粒度",
     "冲突合并以 Markdown 段落为最小单位，段 id 取内容哈希；同一段才可能冲突。",
     "medium", "duplicate", "数据同步"),
)

#: 手动写入（Agent 经 MCP 直写）的记忆：source='memory'
MANUAL_MEMORIES = [
    ("小满偏好 Markdown 里用「」而不是引号包中文术语。", "direct-2026-10-26", 0.75),
    ("星轨笔记的日志统一写入 logs/ 下，按天切分，保留 14 天。", "direct-2026-10-25", 0.6),
    ("给星轨笔记提 PR 时，标题用中文，正文必须附复现步骤。", "direct-2026-10-24", 0.6),
]


# ---------------------------------------------------------------- wiki 页

#: L2 页：(域名, 文件名, 标题, 正文, [关联页标题])
L2_PAGES = [
    ("前端交互", "01-标签树与渲染", "标签树构建与渲染",
     "标签树是侧边栏的核心数据结构。1.4 之前它由一次性递归构建，任何环都会让渲染直接白屏。\n\n"
     "## 两遍扫描\n\n第一遍建立 `id → node` 索引，第二遍按 `parent_id` 挂载。挂载时沿祖先链回溯，"
     "若遇到已在链上的节点说明成环，**丢弃这条父指针**并把节点平铺到根。\n\n"
     "> 为什么不用深度上限兜底：上限只是把栈溢出换成静默截断，用户看不到任何提示，"
     "却永久丢失了层级信息。", ["环检测与数据修正"]),
    ("前端交互", "02-环检测与数据修正", "环检测与数据修正",
     "环有三种来源：自引用（`parent_id == id`）、两节点互指、以及更长的链回环。\n\n"
     "## 修正策略\n\n- 节点本身**保留**，只丢父指针，平铺到根\n"
     "- 每次修正都累加计数，设置页展示「已修正 N 条异常父子关系」\n"
     "- 修正不改库里的原始数据，只在读取路径上生效\n\n"
     "静默修正比崩溃更危险：崩溃会有人来修，静默修正没人知道数据已经变了。", ["标签树构建与渲染"]),
    ("检索与召回", "01-分词与索引", "分词与索引方案",
     "中文笔记的关键词命中依赖分词质量。当前方案：写入时 jieba 切词，FTS5 用 `unicode61` 分词器，"
     "查询侧对关键词同样切词后用 AND 连接。\n\n"
     "## 量级判断\n\n三万条笔记约一千万字，倒排索引几十 MB，**不需要向量检索**。"
     "向量只在「同义不同词」的场景才有优势，而笔记搜索多数是关键词命中。\n\n"
     "## 迁移铁律\n\n切词规则一变，旧文档的倒排项就全错。必须新建索引库再原子替换，"
     "不能原地升级——原地升级不会报错，但结果是乱的全集。", ["索引重建与迁移"]),
    ("检索与召回", "02-索引重建与迁移", "索引重建与迁移",
     "重建流程：新库建表 → 逐批写入 → 校验条数 → 原子替换旧库文件。\n\n"
     "实测三万条约 40 秒，瓶颈在写盘。\n\n"
     "## 为什么必须整库重建\n\n关键词索引没有增量升级的余地：分词器是**索引级**属性，"
     "同一个表里不能混两套切词结果。要么全对，要么全错。", ["分词与索引方案"]),
    ("数据同步", "01-冲突合并", "离线同步的冲突合并",
     "冲突合并的最小单位是 **Markdown 段落**（空行分界），段 id 取内容哈希。\n\n"
     "## 三方合并\n\n以共同祖先为基准，比对两个版本的段落集合：\n\n"
     "| 情况 | 处理 |\n|---|---|\n| 只有 A 改 | 取 A |\n| 只有 B 改 | 取 B |\n"
     "| A、B 同段都改且不同 | 冲突，交给人选择 |\n\n"
     "祖先快照按「最后一次成功同步时的内容哈希」保存，不存全文。\n\n"
     "> 字符级 diff 对中文不友好，容易合出语义断裂的乱码，因此合并粒度必须提到段落。",
     ["冲突交互设计"]),
    ("数据同步", "02-冲突交互设计", "冲突交互设计",
     "冲突**不用 diff 视图**。人做的是选择，不是合并。\n\n"
     "每处冲突渲染成一张卡片：「版本 A」/「版本 B」/「保留两者」，多处冲突排成列表。\n\n"
     "## 为什么不用 diff\n\n中文的字符级 diff 看起来像乱码，用户会怀疑程序坏了。"
     "让用户在两个完整段落里选一个，认知负担最小。", ["离线同步的冲突合并"]),
    ("数据同步", "03-附件去重", "附件去重与清理",
     "附件以**内容 sha256** 命名，上传前先查重，命中只写引用。\n\n"
     "## 历史数据清理\n\n按 sha256 分组保留最早一份，其余把引用改指向它再删文件；"
     "执行前强制备份数据库——这是不可逆操作。\n\n"
     "## 测试盲区\n\n用固定文件名写的上传测试永远发现不了重名问题。"
     "补用例时必须让同一张图上传两次，并断言只落一份。", []),
    ("工程实践", "01-发布检查清单", "发布前检查清单（按丢数据风险排序）",
     "排序依据不是「好不好看」，而是**坏了会不会丢数据**。\n\n"
     "## 数据层（必过）\n\n1. 数据库迁移在旧版本库副本上跑通，且可回滚\n"
     "2. 附件去重巡检跑一遍\n3. 导出 / 导入往返一致\n\n"
     "## 界面层（可延后）\n\n离线状态提示、同步进度、冲突提示——坏了不丢数据，只是难看。", []),
    ("工程实践", "02-版本计划", "版本计划",
     "1.4 排在下周发布。数据层三项未通过则不发，不带病上线。\n\n"
     "## 与 1.3 的关系\n\n1.3 把同步做通了，但冲突处理是 last-write-wins，"
     "1.4 换成段落级三方合并，这是本次最大的行为变更。", ["发布前检查清单（按丢数据风险排序）"]),
]

#: L1 页：(source, conversation_id 后缀, 标题, 正文提要, 来源记忆 id 列表)
L1_PAGES = [
    ("hermes", "h-7f31c2", "标签树递归崩溃的定位与修复",
     "从白屏现象反推到自引用数据，确定两遍扫描 + 环检测的修法，并补了三个测试。"),
    ("opencode", "oc-2b90ea", "搜索分词方案选型与索引迁移",
     "对比向量与倒排的适用场景，选定 jieba + FTS5，并明确整库重建的迁移路径。"),
    ("qwen", "q-4d771b", "离线同步冲突的段落级合并",
     "确定段落为合并最小单位与三选一交互，指出字符级 diff 对中文不可用。"),
    ("zcode", "z-a1c058", "附件重复上传的根因与清理",
     "根因是时间戳命名导致无法去重，改为内容哈希命名并提供历史清理脚本。"),
    ("dsh", "d-90e6f4", "导出改流式与原子替换",
     "峰值内存 3 GB 降到 40 MB，写入临时文件后原子替换避免半成品。"),
    ("hermes", "h-c3d812", "发布检查清单的排序原则",
     "按丢数据风险而非界面观感排序，数据层三项为硬门槛。"),
]

#: 投喂素材：(doc_id, 标题, 正文)
FEED_DOCS = [
    ("fd-3a91c7", "Markdown 方言兼容性说明",
     "# Markdown 方言兼容性说明\n\n"
     "本文档汇总星轨笔记在导入第三方 Markdown 时遇到的方言差异。\n\n"
     "## 1. 标题层级\n\n部分工具允许跳级（`#` 直接到 `###`）。星轨笔记保留原层级，"
     "但在渲染时补空占位，避免目录树错位。\n\n"
     "## 2. 表格\n\nGFM 表格要求分隔行；缺少分隔行的表格按普通段落处理，"
     "**不做猜测性修复**——猜错比不猜更糟。\n\n"
     "## 3. 脚注\n\n支持 `[^1]` 形式。定义缺失时渲染成上标空链接并给出警告。\n\n"
     "## 4. 嵌入附件\n\n`![[file.png]]` 形式的 Wiki 链接按文件名在附件索引里查找；"
     "重名时按上传时间取最新，并在文档顶部提示。"),
    ("fd-8e42b0", "离线同步协议 v2 变更要点",
     "# 离线同步协议 v2 变更要点\n\n"
     "## 变更动机\n\nv1 采用 last-write-wins，两设备同时编辑会丢一侧内容。\n\n"
     "## 变更内容\n\n| 项 | v1 | v2 |\n|---|---|---|\n"
     "| 冲突处理 | 覆盖 | 段落级三方合并 |\n"
     "| 祖先快照 | 无 | 内容哈希 |\n"
     "| 附件命名 | 时间戳 | 内容 sha256 |\n\n"
     "## 兼容性\n\nv2 客户端可以读 v1 数据，反之不行。服务端按客户端声明的最低版本降级处理。\n\n"
     "## 回滚\n\n回滚只需把服务端协议版本调回 v1；**已合并的段落不会自动拆回**，"
     "需要人工处理，这一点必须在升级前告知用户。"),
]


# ---------------------------------------------------------------- 建库

def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


SCHEMA_SOURCE = """
CREATE TABLE IF NOT EXISTS conversations (
  source TEXT NOT NULL, id TEXT NOT NULL, title TEXT, cwd TEXT, model TEXT,
  created_at INTEGER, updated_at INTEGER, event_count INTEGER, roles_json TEXT,
  meta_json TEXT, signature TEXT, session_key TEXT, session_uid INTEGER,
  title_custom TEXT, PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS events (
  source TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
  role TEXT, content TEXT, tool_name TEXT, tool_input_json TEXT, tool_output TEXT,
  tool_status TEXT, reasoning TEXT, patch_file TEXT, patch_diff TEXT,
  shell_cmd TEXT, shell_output TEXT, shell_cwd TEXT, parent_id TEXT,
  time INTEGER, model TEXT, raw_json TEXT, src_id TEXT, turn_key TEXT,
  is_system INTEGER, PRIMARY KEY (source, conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS deleted_conversations (
  source TEXT, id TEXT, session_uid INTEGER, title TEXT, deleted_at INTEGER
);
CREATE TABLE IF NOT EXISTS memory_exclusions (
  source TEXT, conversation_id TEXT, turn_key TEXT, created_at INTEGER, note TEXT
);
"""

SCHEMA_INDEX = """
CREATE TABLE IF NOT EXISTS units (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
  conversation_id TEXT NOT NULL, seq INTEGER, role TEXT, turn_key TEXT,
  src_id TEXT, time INTEGER, title TEXT, text TEXT NOT NULL, chars INTEGER,
  legacy_id TEXT, tags TEXT, updated_at INTEGER, wiki_path TEXT, origin TEXT,
  doc_id TEXT, UNIQUE(source, conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS unit_values (
  unit_id INTEGER PRIMARY KEY, value REAL, r_human REAL, priority REAL,
  updated_at INTEGER, manual_value REAL
);
CREATE TABLE IF NOT EXISTS unit_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, unit_id INTEGER, channel TEXT,
  polarity TEXT, magnitude REAL, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS conv_scores (
  source TEXT, conversation_id TEXT, r_task REAL, updated_at INTEGER,
  PRIMARY KEY (source, conversation_id)
);
CREATE TABLE IF NOT EXISTS distilled_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, conversation_id TEXT,
  slice_key TEXT, turn_key TEXT, type TEXT, topic TEXT, content TEXT,
  confidence TEXT, status TEXT, dedup_of INTEGER, content_hash TEXT,
  prompt_ver TEXT, model TEXT, merged_from_json TEXT, created_at INTEGER,
  wiki_ignore_at INTEGER
);
CREATE TABLE IF NOT EXISTS distill_hashes (
  source TEXT, conversation_id TEXT, slice_key TEXT, content_hash TEXT,
  prompt_ver TEXT, model TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS ingest_meta (key TEXT PRIMARY KEY, value TEXT);
"""

SCHEMA_MATERIALS = """
CREATE TABLE IF NOT EXISTS materials (
  doc_id TEXT PRIMARY KEY, sha256 TEXT, kind TEXT, source_ref TEXT, title TEXT,
  status TEXT, error TEXT, error_kind TEXT, kb_path TEXT, mirror_path TEXT,
  raw_path TEXT, bytes INTEGER, quality REAL, warnings_json TEXT,
  created_at INTEGER, updated_at INTEGER
);
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_source_db(path: Path) -> dict[tuple[str, str], tuple[int, str]]:
    conn = _connect(path)
    conn.executescript(SCHEMA_SOURCE)
    uid = 0
    meta: dict[tuple[str, str], tuple[int, str]] = {}
    for source, cid, title, model, days, evs in CONVERSATIONS:
        uid += 1
        t0 = _ts(days, 9, 30)
        rows = []
        turn_no = 0
        for i, ev in enumerate(evs):
            role, content = ev[0], ev[1]
            kind = ev[2] if len(ev) > 2 else ""
            t = t0 + i * 137
            if role == "user":
                turn_no += 1
            turn_key = "t%02d" % turn_no
            rows.append((
                source, cid, i + 1, role, content,
                "shell" if kind == "shell" else None,   # tool_name
                json.dumps({"cmd": content}, ensure_ascii=False) if kind == "shell" else None,
                None,                                    # tool_output
                "ok" if kind == "shell" else None,       # tool_status
                t, model, None, "src-%s-%02d" % (cid, i + 1), turn_key, 0,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO events(source, conversation_id, seq, role, content,"
            " tool_name, tool_input_json, tool_output, tool_status, time, model,"
            " raw_json, src_id, turn_key, is_system)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        roles = sorted({r[0] for r in rows})
        conn.execute(
            "INSERT OR REPLACE INTO conversations(source, id, title, cwd, model,"
            " created_at, updated_at, event_count, roles_json, meta_json, signature,"
            " session_key, session_uid, title_custom)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source, cid, title, "~/projects/starlight-notes", model,
             t0, t0 + len(rows) * 137, len(rows), json.dumps(roles),
             json.dumps({"agent": source}), "sig-" + cid, cid, uid, None))
        meta[(source, cid)] = (uid, title)
    conn.commit()
    conn.close()
    return meta


def build_index_db(path: Path, conv_meta: dict) -> None:
    conn = _connect(path)
    conn.executescript(SCHEMA_INDEX)
    src_conn: dict[str, int] = {}

    # 1) 原始消息投影：role='user'/'assistant'，src_id='msg:...'
    for source, cid, title, model, days, evs in CONVERSATIONS:
        t0 = _ts(days, 9, 30)
        turn_no = 0
        for i, ev in enumerate(evs):
            role, content = ev[0], ev[1]
            if role == "user":
                turn_no += 1
            turn_key = "t%02d" % turn_no
            t = t0 + i * 137
            conn.execute(
                "INSERT OR IGNORE INTO units(source, conversation_id, seq, role,"
                " turn_key, src_id, time, title, text, chars, updated_at, origin)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (source, cid, i + 1, role, turn_key, "msg:" + _sha(content)[:16], t,
                 title, content, len(content), t, "native"))

    # 2) 蒸馏记忆 + 其投影 units（src_id = 'dst_' + content_hash）
    for idx, m in enumerate(MEMORIES):
        source, cid, mtype, topic, content, conf, status, _domain = m
        created = _ts(8 - idx % 8, 14, (idx * 7) % 60)
        ch = _sha(content)
        cur = conn.execute(
            "INSERT INTO distilled_memories(source, conversation_id, slice_key,"
            " turn_key, type, topic, content, confidence, status, dedup_of,"
            " content_hash, prompt_ver, model, merged_from_json, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source, cid, "%s#%d" % (cid, idx // 4), "t0%d" % (idx % 5 + 1), mtype,
             topic, content, conf, status, None, ch, "v1", "deepseek-v4.1-flash",
             None, created))
        if status == "duplicate":
            continue          # 重复项不投影，面板上 unit_id 为空、加权按钮禁用
        conn.execute(
            "INSERT OR IGNORE INTO units(source, conversation_id, seq, role,"
            " turn_key, src_id, time, title, text, chars, updated_at, origin)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (source, cid, 1000 + cur.lastrowid, "distilled", "t0%d" % (idx % 5 + 1),
             "dst_" + ch, created, topic, content, len(content), created, "native"))

    # 3) Agent 直写记忆：source='memory'
    for j, (content, cid, value) in enumerate(MANUAL_MEMORIES):
        created = BASE_TS - (j + 1) * DAY + 15 * 3600
        conn.execute(
            "INSERT OR IGNORE INTO units(source, conversation_id, seq, role,"
            " turn_key, src_id, time, title, text, chars, updated_at, origin)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("memory", cid, 1, "user", None, "mcp_" + _sha(content)[:16], created,
             "记忆", content, len(content), created, "native"))

    # 4) wiki 页投影：src_id='wiki_<path>'，origin='native'（外部投喂走 external）
    for dom, fname, title, body, _rel in L2_PAGES:
        rel = "out_l2_demo/%s/%s.md" % (dom, fname)
        conn.execute(
            "INSERT OR IGNORE INTO units(source, conversation_id, seq, role,"
            " src_id, time, title, text, chars, updated_at, wiki_path, origin)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("wiki", rel, 1, "page", "wiki_" + rel, _ts(6, 11, 0), title,
             body, len(body), _ts(6, 11, 0), rel, "native"))
    for doc_id, title, body in FEED_DOCS:
        rel = "out_feed/%s-%s.md" % (title, doc_id)
        conn.execute(
            "INSERT OR IGNORE INTO units(source, conversation_id, seq, role,"
            " src_id, time, title, text, chars, updated_at, wiki_path, origin, doc_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("wiki", rel, 1, "page", "wiki_" + rel, _ts(4, 16, 0), title,
             body, len(body), _ts(4, 16, 0), rel, "external", doc_id))

    # 5) 价值分
    for (uid_, value) in conn.execute(
            "SELECT id, 0.6 FROM units WHERE source='memory'").fetchall():
        conn.execute("INSERT OR REPLACE INTO unit_values(unit_id, value, r_human,"
                     " priority, updated_at) VALUES(?,?,?,?,?)",
                     (uid_, value, None, None, BASE_TS))
    for (uid_,) in conn.execute(
            "SELECT id FROM units WHERE role='distilled'").fetchall():
        conn.execute("INSERT OR REPLACE INTO unit_values(unit_id, value, r_human,"
                     " priority, updated_at) VALUES(?,?,?,?,?)",
                     (uid_, 0.3, None, None, BASE_TS))
    # 手动加权示例：让两条看起来被 ⭐ 锁定过（面板会显示「已锁定」）
    conn.execute("UPDATE unit_values SET manual_value=1.0, value=1.0, priority=1.0"
                 " WHERE unit_id IN (SELECT id FROM units"
                 "                   WHERE source='memory' ORDER BY id LIMIT 1)")
    conn.execute("UPDATE unit_values SET manual_value=0.2, value=0.2"
                 " WHERE unit_id IN (SELECT id FROM units"
                 "                   WHERE role='distilled' ORDER BY id LIMIT 1)")

    # 6) 会话评分
    for (source, cid), score in CONV_SCORES.items():
        conn.execute("INSERT OR REPLACE INTO conv_scores(source, conversation_id,"
                     " r_task, updated_at) VALUES(?,?,?,?)",
                     (source, cid, score, BASE_TS))

    conn.execute("INSERT OR REPLACE INTO ingest_meta(key, value) VALUES(?,?)",
                 ("last_ingest_at", str(BASE_TS)))
    conn.commit()
    conn.close()


def build_wiki(root: Path) -> None:
    l1 = root / "out_l1_demo"
    l2 = root / "out_l2_demo"
    feed = root / "out_feed"
    for d in (l1, l2, feed):
        d.mkdir(parents=True, exist_ok=True)

    for source, cid, title, digest in L1_PAGES:
        rel = Path("%s__%s" % (source, cid)) / "index.md"
        p = l1 / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "---\ntitle: %s\nsource: %s\nconversation_id: %s\n"
            "updated_at: %d\n---\n\n# %s\n\n%s\n"
            % (title, source, cid, BASE_TS, title, digest),
            encoding="utf-8")

    by_dom: dict[str, list] = {}
    for item in L2_PAGES:
        by_dom.setdefault(item[0], []).append(item)
    for dom, pages in by_dom.items():
        for dom_name, fname, title, body, related in pages:
            p = l2 / dom / (fname + ".md")
            p.parent.mkdir(parents=True, exist_ok=True)
            rel_md = ""
            if related:
                rel_md = "\n## 相关\n\n" + "".join("- [[%s]]\n" % r for r in related)
            p.write_text(
                "---\ntitle: %s\ndomain: %s\nupdated_at: %d\n---\n\n# %s\n\n%s\n%s"
                % (title, dom, BASE_TS, title, body, rel_md),
                encoding="utf-8")

    for doc_id, title, body in FEED_DOCS:
        p = feed / ("%s-%s.md" % (title, doc_id))
        p.write_text(
            "---\ntitle: %s\ndoc_id: %s\norigin: external\nupdated_at: %d\n---\n\n%s"
            % (title, doc_id, BASE_TS, body),
            encoding="utf-8")


def build_materials(path: Path, store_dir: Path, kb_dir: Path) -> None:
    conn = _connect(path)
    conn.executescript(SCHEMA_MATERIALS)
    (store_dir / "raw").mkdir(parents=True, exist_ok=True)
    (store_dir / "mirror").mkdir(parents=True, exist_ok=True)
    for doc_id, title, body in FEED_DOCS:
        raw = store_dir / "raw" / (doc_id + ".md")
        raw.write_text(body, encoding="utf-8")
        mirror = store_dir / "mirror" / doc_id
        mirror.mkdir(parents=True, exist_ok=True)
        (mirror / "source.md").write_text(body, encoding="utf-8")
        kb = kb_dir / ("%s-%s.md" % (title, doc_id))
        conn.execute(
            "INSERT OR REPLACE INTO materials(doc_id, sha256, kind, source_ref,"
            " title, status, kb_path, mirror_path, raw_path, bytes, quality,"
            " warnings_json, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, _sha(body), "md", "clip://starlight-docs/" + doc_id, title,
             "published", kb.as_posix(), (mirror / "source.md").as_posix(),
             raw.as_posix(), len(body.encode("utf-8")), 0.92, "[]",
             BASE_TS - 4 * DAY, BASE_TS - 3 * DAY))
    conn.commit()
    conn.close()


CONFIG_YAML = """# 演示用配置（仅供 README 截图 / 本地演示，与真实 agentmemhub.yaml 无关）
# 所有路径都落在 demo/ 之下，不会读写真实 database/ 与 exports/。
data_dir: demo/data
db_path: demo/data/agentmemhub.db

web:
  port: 8090

llm:
  endpoint: "https://api.example.invalid/chat/completions"
  api_key: ""
  model: "deepseek-v4.1-flash"

distillation:
  direct_bucket: daily

feed:
  db: demo/data/materials.db
  store_dir: demo/data/feed_store
  kb_dir: demo/wiki/out_feed

wiki:
  out_l1: demo/wiki/out_l1_demo
  out_l2: demo/wiki/out_l2_demo
"""


def main(argv: list[str]) -> int:
    if "--clean" in argv and DEMO.exists():
        shutil.rmtree(DEMO)
        print("已清理 demo/")
    data = DEMO / "data"
    wiki = DEMO / "wiki"
    (data / "logs").mkdir(parents=True, exist_ok=True)
    wiki.mkdir(parents=True, exist_ok=True)

    for stale in ("agentmemhub.db", "session_rag.db", "materials.db"):
        p = data / stale
        if p.exists():
            p.unlink()

    print("· 采集库 …")
    meta = build_source_db(data / "agentmemhub.db")
    print("· 索引库 …")
    build_index_db(data / "session_rag.db", meta)
    print("· wiki 产物 …")
    build_wiki(wiki)
    print("· 投喂素材 …")
    build_materials(data / "materials.db", data / "feed_store", wiki / "out_feed")
    (DEMO / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")

    # 操作日志：面板「操作日志」视图读 logs/ 下的结构化日志
    log = data / "logs" / "ops.log"
    lines = []
    for i, (ev, msg) in enumerate([
        ("ingest", "采集入库完成：6 个会话 / 58 条事件"),
        ("distill", "记忆蒸馏完成：19 条记忆，去重后 18 条"),
        ("wiki_update", "增量更新完成：L1 6 页 / L2 9 页"),
        ("feed_add", "投喂完成：Markdown 方言兼容性说明"),
        ("snapshot", "创建快照 snap-20261024-0900"),
    ]):
        t = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(BASE_TS + i * 3600))
        lines.append("%s  INFO   [ops]  %s" % (t, msg))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n完成。演示数据在 %s" % DEMO)
    print("启动演示面板：")
    print('  $env:AGENTMEMHUB_CONFIG = "demo/config.yaml"')
    print('  .venv\\Scripts\\python.exe -m agentmemhub serve --no-open --port 8090')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
