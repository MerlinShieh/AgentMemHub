"""`is_system_inject` 的判定边界（2026-10-26）。

为什么单独守这一条：把「用户真实消息」判成「系统注入」的代价不是少渲染一条，
而是**这条记忆永远不生成**，而且没有任何提示。

- `distill.py` 与 `rag/ingest.py` 都只取 `is_system = 0` 的事件；
- `agentmemhub clean --apply` 会 `DELETE FROM events WHERE is_system = 1`，
  把被误判的真实消息**物理删掉**。

历史上规则末尾有四个**无锚定**的裸词（`environment_context` / `runtime.context` /
`file.policy` / `approval.policy`），正文里任何位置出现就命中。现在只认它们作为
行首的标签/键出现。
"""
import pytest

from agentmemhub.models import is_system_inject


@pytest.mark.parametrize("text", [
    "Current runtime context:\n- cwd: D:/work",
    "The TodoWrite tool hasn't been used recently.",
    "[System: The active model for this chat has changed to deepseek-v4-flash]",
    "<system-reminder>do not print the token</system-reminder>",
    "<task-notification>\n<task-id>abc</task-id>\n</task-notification>",
    "<environment_context>\n<cwd>/tmp</cwd>\n</environment_context>",
    "environment_context: D:/work",
    "- approval_policy: never",
    "  file.policy: workspace-write",
])
def test_系统注入应被识别(text):
    assert is_system_inject(text) is True


@pytest.mark.parametrize("text", [
    "我把 runtime.context 改成按请求注入，省得每个模块都自己拼一遍",
    "这个字段叫 file.policy，后端读它决定能不能写盘",
    "用户问 approval.policy 是什么，我解释了半天他才明白",
    "我把 workspace 目录改到 D:/data，environment_context 那条报错就没了",
    "这次改动让 environment_context 和 file.policy 都由同一个中间件注入",
])
def test_正文里提到这些词不算系统注入(text):
    """正文中间出现这些字样是正常讨论，不能整条判成注入。"""
    assert is_system_inject(text) is False


def test_空文本不判系统():
    assert is_system_inject("") is False
    assert is_system_inject(None) is False
