# 贡献指南

感谢你对 AI Conversation Hub 的兴趣！本文档说明如何为新 agent 编写适配器。

## 添加自定义数据源（无需改代码）

如果你的 agent 对话能导出为以下三种格式之一，**完全不用写代码**，只需在设置里配置路径：

### JSONL 格式
每行一个 JSON 对象，包含 `id`、`title`、`messages`（数组，每条含 `role` 和 `text`）：
```jsonl
{"id": "conv-1", "title": "调试登录问题", "messages": [{"role": "user", "text": "登录报错了"}, {"role": "assistant", "text": "看一下错误日志"}]}
```

### Markdown 格式
```markdown
# 调试登录问题

**user**: 登录报错了

**assistant**: 看一下错误日志
```

### SQLite 格式
包含 `conversations` 和 `messages` 两张表的标准结构。

在「设置 → 数据源质量中心 → 配置路径」里添加你的自定义源即可。

## 编写内置适配器（需要改代码）

如果你想为某个 agent 编写内置适配器（像 Codex/Hermes/WorkBuddy/QoderWork/ZCode 那样自动发现），需要：

### 1. 在 `source_adapters.py` 添加加载函数

```python
def _load_your_agent(path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    读取该 agent 的数据，返回 (conversations, messages)。
    - conversations: 列表，每项是 {"id", "title", "preview", "workspace", "created_at", "updated_at", "message_count"}
    - messages: 字典，key=(source, conversation_id)，value=消息列表 [{"role","text","created_at"}]
    """
    items = []
    messages = {}
    # ... 读取你的 agent 数据 ...
    return items, messages
```

### 2. 注册到 EXTRA_SOURCES 和 LOADERS

```python
EXTRA_SOURCES = ("claude", "qoderwork", "zcode", "your_agent")  # 加上你的

# 文件底部
LOADERS = {
    "claude": _load_claude,
    "qoderwork": _load_qoderwork,
    "zcode": _load_zcode,
    "your_agent": _load_your_agent,  # 加上你的
}
```

### 3. 添加发现逻辑

在 `default_candidates()` 和 `_candidate_filenames()` 里加上你的 agent 的默认路径和特征文件名。

### 4. 添加验证逻辑

在 `validate_source()` 和 `estimate_conversations()` 里加上对 `your_agent` 的校验分支。

### 5. 添加显示标签

在 `SOURCE_LABELS` 里加上 `"your_agent": "Your Agent"`。

## 设计约束

- **只读**：适配器只能读取 agent 的数据，绝不写回
- **无依赖**：只用 Python 标准库（sqlite3/json/pathlib 等）
- **容错**：agent 数据可能 schema 变化，适配器要 graceful 降级
- **脱敏**：不要把凭证、配置文件、系统 prompt 纳入索引

## 提交贡献

1. Fork 仓库
2. 在你的分支上开发适配器
3. 确保语法通过：`python -c "import ast; ast.parse(open('source_adapters.py').read())"`
4. 提交 Pull Request，描述你接入的 agent 和测试情况

## 许可证提醒

提交的贡献将同样遵循项目的 [MIT](LICENSE) 许可证。
