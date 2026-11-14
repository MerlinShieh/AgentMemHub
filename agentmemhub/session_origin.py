"""来源会话识别 —— 给 Agent 直写的记忆补上「来自哪个会话」。

## 为什么需要这个模块

`memory_save` 写进来的记忆，落表时 `conversation_id` 是**按自然日分桶**
（`direct-YYYY-MM-DD`，见 `distill._direct_bucket()`），只服务于"别让 L1 重编
放大"。它回答了"哪天写的"，但**回答不了"哪个会话写的"**——同一主题的结论
散落在十几个真实会话里，事后无法回溯出处，也无法按会话归组。

## 会话 ID 从哪来

分三层，优先靠前：

1. **调用方显式传入**（`memory_save(source_session_id=...)`）——最准。
   DSH 里 Agent 可以读 `$env:DSH_SESSION_ID` 拿到原生会话 ID，
   但**每次多调一次 shell 要 ~3.3 秒**（DSH 的 shell 调用固定成本，
   实测 `echo ok` 就要 3.5s），所以不强制、由 Skill 按"顺手才取"的原则处理。
2. **本模块推断**（`infer_active_session()`）——零成本兜底。
3. **都没有就留空**——绝不猜错值。

## 为什么能推断（以及它的边界）

DSH 把每个会话写成 `~/.dsh/sessions/<编码后的 cwd>/<会话 ID>/session.v4.jsonl.zstd`：

- **子目录名就是会话 ID**，不需要解压文件；
- 会话进行中该文件持续追加，所以 **mtime ≈ 最后活动时刻**；
- 主会话目录名带 `session-` 前缀，**子代理（subagent）是裸 UUID**。

于是"取窗口内 mtime 最新、且优先主会话"就是一次合理推断。

**边界**：多个会话同时活跃时会选错（MCP server 是宿主级单例，收不到
"谁在调我"的信息——DSH 的 `tools/call` 不携带会话上下文）。所以推断结果
只当**溯源标签**用，绝不参与分组/召回判定；标错了不影响写入与检索。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

#: DSH 会话根目录（相对用户主目录）。每个工作区一个目录，其下每个会话一个子目录。
DSH_SESSIONS_REL = os.path.join(".dsh", "sessions")

#: 会话日志文件名。只认它——目录里没有这个文件，说明不是活跃会话。
DSH_SESSION_FILE = "session.v4.jsonl.zstd"

#: 推断的「仍然活跃」窗口（秒）。
#:
#: 取值依据：DSH 每次工具调用/助手消息都会追加写这个文件，实测会话进行中
#: 稳定在秒级；但模型长思考（无工具调用）时可能几十秒不写。取 10 分钟是
#: 为了覆盖"跑完一段任务后回头写记忆"的常见间隔，同时把昨天/上周的陈会话
#: 排除掉。宁可留空，也不给记忆挂上错误的会话。
FRESH_WINDOW_S = 600.0

#: DSH 主会话目录名前缀；子代理目录是裸 UUID（无前缀）。
DSH_MAIN_PREFIX = "session-"

#: DSH 这个来源的标识（值形如 `dsh:<会话 ID>`）。
DSH_SOURCE = "dsh"


def dsh_sessions_root() -> Path:
    """DSH 会话根目录（`~/.dsh/sessions`）。存在性由调用方判断。"""
    return Path(os.path.expanduser("~")) / DSH_SESSIONS_REL


def normalize_source_session(raw: object) -> str:
    """把入参规范化成 `<来源>:<会话 ID>`；空值/非法值返回空串。

    - 已带来源前缀（值里有 `:`）→ 原样保留（去空白），兼容
      `claude:<id>` / `cursor:<id>` 等其它 harness；
    - 裸值 → 按 DSH 会话处理，补 `dsh:` 前缀（当前唯一原生支持的 harness）。

    不做格式白名单校验：不同 harness 的会话 ID 形态各异（DSH 是
    `session-<uuid>`，其它可能完全不同），这里只负责"来源 + ID"的
    可区分性，格式由各 harness 自己决定。
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    if ":" in value:
        return value
    return f"{DSH_SOURCE}:{value}"


def _session_mtime(sess_dir: Path) -> Optional[float]:
    """会话最后活动时刻（epoch 秒）；读不到返回 None。"""
    try:
        log = sess_dir / DSH_SESSION_FILE
        if log.is_file():
            return log.stat().st_mtime
        # 老版本会话可能没有 v4 日志文件：退回目录自身 mtime（仍比没有强）。
        return sess_dir.stat().st_mtime
    except OSError:
        return None


def infer_active_session(*, root: Optional[Path] = None,
                         now: Optional[float] = None,
                         window_s: float = FRESH_WINDOW_S) -> str:
    """推断当前活跃的 DSH 会话，返回 `dsh:<会话 ID>`；判不出返回空串。

    参数全部可注入，便于测试（`root` 指临时目录、`now` 固定时钟）。

    选取规则（**主会话优先于子代理**，其次比 mtime）：
    会话进行中主会话文件一定在写（它在驱动子代理），所以只要主会话在窗口内
    就必然选中它，不会被子代理的更新写入抢走。

    任何异常都 fail-open 成空串：这是给写入路径打标签的旁路，**不能因为它
    让记忆写不进去**。
    """
    try:
        base = Path(root) if root is not None else dsh_sessions_root()
        if not base.is_dir():
            return ""
        clock = time.time() if now is None else float(now)
        best: Optional[tuple[tuple[int, float], str]] = None
        for workspace in base.iterdir():
            if not workspace.is_dir():
                continue
            try:
                sessions = list(workspace.iterdir())
            except OSError:
                continue
            for sess in sessions:
                if not sess.is_dir():
                    continue
                mtime = _session_mtime(sess)
                if mtime is None or clock - mtime > window_s:
                    continue
                prefer = 1 if sess.name.startswith(DSH_MAIN_PREFIX) else 0
                rank = (prefer, mtime)
                if best is None or rank > best[0]:
                    best = (rank, sess.name)
        if best is None:
            return ""
        return f"{DSH_SOURCE}:{best[1]}"
    except Exception:                           # noqa: BLE001
        return ""
